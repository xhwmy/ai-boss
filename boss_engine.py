"""
BOSS 直聘后台引擎 — 可控制/可暂停/支持人工干预
"""

import copy
import json
import os
import threading
import time
import queue
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    ROOT_DIR,
    load_config,
    load_resume,
    load_env,
    build_resume_text,
    build_scoring_rules,
    validate_config,
)
from browser import BossBrowser
from city_search import page_allocations, resolve_city_targets
from application_ledger import ApplicationLedger
from job_filters import minimum_monthly_salary_k, salary_floor_for_city, salary_range_for_city
from job_identity import job_key
from submission import SubmissionWorkflow, greeting_for
from analyzer import JDAnalyzer
import database  # ⭐ v3.0 超预期岗位收藏（SQLite）
import blacklist  # v3.1 低质量岗位拉黑名单（三级：永久/短期/观察）
from utils import setup_logger, random_delay, human_pause, save_result, Timer


class BossEngine:
    """可控制的后台引擎，运行在独立线程中"""

    # 状态常量
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"

    def __init__(self):
        self.logger = setup_logger("engine")

        # 线程控制
        self._thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 默认不暂停
        self._lock = threading.Lock()

        # 状态
        self._status = self.IDLE
        self._status_msg = "就绪"

        # 统计数据
        self._stats = {
            "total": 0, "applied": 0, "skipped": 0, "failed": 0,
            "skipped_no_match": 0, "skip_reasons": [],
        }
        self._start_time = None

        # 当前处理的岗位
        self._current_job = None

        # 日志环形缓冲区
        self._log_buffer = []
        self._max_logs = 500
        self._log_seq = int(time.time() * 1000)
        self._account = None
        self._incomplete_cities = []
        self._last_search_day = None

        # 待人工审核队列（半自动模式）
        self._reviews_path = ROOT_DIR / "results" / "pending_reviews.json"
        self._approvals_path = ROOT_DIR / "results" / "approved_reviews.json"
        self._pending_reviews = self._read_review_list(self._reviews_path)
        self._approved_reviews = self._read_review_list(self._approvals_path)
        self._review_queue = queue.Queue()
        for item in self._approved_reviews:
            if item.get("state") == "queued":
                self._review_queue.put(item)
        self._ledger = ApplicationLedger()
        self._last_ten_pause_count = 0

        # 配置（可运行时修改）
        self._config = load_config()
        self._resume = load_resume()
        self._env = load_env()
        self._resume_text = build_resume_text(self._resume)

        # 运行模式
        self._mode = "auto"  # 默认自动沟通；发送结果必须由 BOSS 页面确认

        # 模块引用
        self._analyzer = None
        self._browser = None
        self._submission = None

        # 已处理的 job id 集合（防止重复）
        self._processed_jobs = set()

    # ==================== 属性 ====================

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def status_msg(self) -> str:
        with self._lock:
            return self._status_msg

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    @property
    def current_job(self) -> dict:
        with self._lock:
            return self._current_job

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @mode.setter
    def mode(self, value: str):
        with self._lock:
            self._mode = value

    @property
    def pending_reviews(self) -> list:
        with self._lock:
            return list(self._pending_reviews)

    @property
    def config(self) -> dict:
        with self._lock:
            return dict(self._config)

    @property
    def resume_data(self) -> dict:
        """返回简历字典（注意：resume 是控制方法名，属性用 resume_data 避免冲突）"""
        with self._lock:
            return dict(self._resume)

    @property
    def elapsed(self) -> str:
        if self._start_time is None:
            return "0秒"
        e = time.time() - self._start_time
        if e < 60:
            return f"{e:.0f}秒"
        return f"{e / 60:.1f}分钟"

    # ==================== 状态管理 ====================

    def _set_status(self, status: str, msg: str = ""):
        with self._lock:
            self._status = status
            self._status_msg = msg

    def _set_current_job(self, job: dict):
        with self._lock:
            self._current_job = job

    def _inc_stat(self, key: str, val=1):
        with self._lock:
            if key in self._stats:
                self._stats[key] += val

    # ==================== 日志 ====================

    def add_log(self, level: str, message: str):
        with self._lock:
            self._log_seq = max(self._log_seq + 1, int(time.time() * 1000))
            entry = {
                "seq": self._log_seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            }
            self._log_buffer.append(entry)
            if len(self._log_buffer) > self._max_logs:
                self._log_buffer = self._log_buffer[-self._max_logs:]

    def get_logs(self, n: int = 200) -> list:
        with self._lock:
            return list(self._log_buffer[-n:])

    def _read_review_list(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            return [item for item in data if isinstance(item, dict) and item.get("job_id")]
        except (OSError, ValueError):
            return []

    def _save_review_list_locked(self, path, items):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(temp_path, path)

    # ==================== 配置管理 ====================

    def update_config(self, new_config: dict):
        """保存配置；新城市和页数由下一次搜索循环读取。"""
        with self._lock:
            # 保存到文件
            config_path = ROOT_DIR / "config.json"
            merged = copy.deepcopy(self._config)
            for section in ["search", "ai", "application", "browser", "scoring", "blacklist"]:
                if section in new_config:
                    merged[section].update(new_config[section])
            if "salary_ranges_by_city" in new_config.get("search", {}):
                rows = merged["search"]["salary_ranges_by_city"]
                if not isinstance(rows, list):
                    raise ValueError("地区薪资范围必须是多行配置")
                merged["search"]["city"] = "，".join(str(row.get("city") or "").strip()
                                                 for row in rows if isinstance(row, dict))
                merged["search"]["salary_floor_by_city"] = {}
            targets = resolve_city_targets(merged["search"].get("city", ""))
            page_allocations(merged["search"].get("page_count", 5), len(targets))
            validate_config(merged)
            temp_path = config_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temp_path, config_path)
            self._config = merged
        self.add_log("info", f"✅ 配置已更新")

    def update_resume(self, new_resume: dict):
        """运行时更新简历"""
        with self._lock:
            resume_path = ROOT_DIR / "resume.json"
            merged = dict(self._resume)
            merged.update(new_resume)
            self._resume = merged
            self._resume_text = build_resume_text(merged)
            with open(resume_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
        self.add_log("info", f"✅ 简历已更新")

    def reload_resume(self):
        """重新加载 resume.json 并重建简历文本。

        ⭐ v3.2 上传/删除简历附件后调用：build_resume_text 会读取上传简历的全文，
        重载后 _resume_text 立即包含附件内容，下一次 AI 评分即生效。
        """
        with self._lock:
            self._resume = load_resume()
            self._resume_text = build_resume_text(self._resume)

    # ==================== 人工干预 ====================

    def submit_review_decision(self, job_id: str, decision: dict):
        """提交人工审核结果（如同意投递则加入处理队列，由引擎异步执行）"""
        # 从待审核列表中取出该岗位信息
        with self._lock:
            review = None
            for i, r in enumerate(self._pending_reviews):
                if r.get("job_id") == job_id:
                    review = dict(r)
                    self._pending_reviews.pop(i)
                    self._save_review_list_locked(self._reviews_path, self._pending_reviews)
                    break
        if review is None:
            return False

        # ⭐ v3.1 人工拉黑（永久/短期），直接入库并从待审核移除
        if decision.get("blacklist", False) and review and review.get("url"):
            level = decision.get("level", "permanent")
            expires_at = ""
            duration_days = None
            if level == "temporary":
                duration_days = int(self._config.get("blacklist", {}).get("temp_days", 30) or 30)
                expires_at = (datetime.now() + timedelta(days=duration_days)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            try:
                database.save_blacklist_job({
                    "job_title": review.get("job_title", ""),
                    "company": review.get("company", ""),
                    "url": review.get("url", ""),
                    "reason": decision.get("reason", "") or "人工拉黑",
                    "level": level,
                    "source": "manual",
                    "duration_days": duration_days,
                    "expires_at": expires_at,
                    "match_score": review.get("match_score", 0),
                })
                self.add_log("info",
                             f"  🚫 已人工拉黑: {review.get('job_title', '')}"
                             f"（{'⛔ 永久' if level == 'permanent' else '🕐 短期'}）"
                             f"可在「🚫 黑名单」页面查看")
            except Exception as e:
                self.add_log("warn", f"  ⚠️ 人工拉黑失败: {e}")
                with self._lock:
                    self._pending_reviews.append(review)
                    self._save_review_list_locked(self._reviews_path, self._pending_reviews)
                return False
            return True

        if decision.get("should_apply", False) and review and review.get("url"):
            item = {
                "job_id": job_id,
                "decision": decision,
                "review": review,
                "state": "queued",
            }
            with self._lock:
                self._approved_reviews.append(item)
                self._save_review_list_locked(self._approvals_path, self._approved_reviews)
            self._review_queue.put(item)
            self.add_log("info", f"👤 审核通过，等待引擎投递: {review.get('job_title', '')}")
        else:
            self.add_log("info", f"👤 已跳过审核: {review.get('job_title', '') if review else job_id}")
        return True

    def _wait_for_quota(self) -> bool:
        """按已确认发送的持久化记录限制每天和每小时联系量。"""
        announced = ""
        while not self._stop_event.is_set():
            app_cfg = self._config["application"]
            if not self._pause_event.is_set():
                self._pause_event.wait(timeout=1)
                continue
            allowed, reason, next_at = self._ledger.quota(
                max(1, int(app_cfg.get("daily_limit", 50))),
                max(1, int(app_cfg.get("max_per_hour", 15))),
            )
            if allowed:
                return True
            if reason != announced:
                self.add_log("warn", f"  ⏳ {reason}")
                announced = reason
            if next_at is None:
                return False
            self._set_status(self.RUNNING, reason)
            seconds = max(1, min(10, (next_at - datetime.now()).total_seconds()))
            self._stop_event.wait(seconds)
        return False

    def _apply_current_job(self, record: dict) -> str:
        """发起沟通后继续发送个性化介绍；PDF 按平台限制等待双方回复。"""
        outcome = self._browser.contact_job()
        status = outcome.get("status")
        title = record.get("job_title", "未知岗位")
        if status == "sent":
            if self._ledger.record_sent(record):
                self._stats["applied"] += 1
                self.add_log("info", f"  ✅ BOSS 已确认发送: {title}（本轮 {self._stats['applied']} 个）")
                greeting = record.get("greeting") or greeting_for(self._resume, record)
                state, detail = self._submission.process(record, greeting)
                self._ledger.record_followup_status(record, state, detail)
                if state == "sent":
                    self.add_log("info", f"  📎 {title}：{detail}")
                elif state == "pending":
                    self.add_log("info", f"  ⏳ {title}：{detail}；收到招聘方回复后会自动补发")
                else:
                    self.add_log("warn", f"  ⚠️ {title}：{detail}；已保留待补发记录")
                return "sent"
            self.add_log("info", f"  📋 记录中已有该岗位，未重复计数: {title}")
            return "already"
        if status == "already":
            self._stats["skipped"] += 1
            self.add_log("info", f"  📋 BOSS 显示已沟通，跳过: {title}")
            return "already"

        self._stats["failed"] += 1
        reason = outcome.get("reason", "未知原因")
        save_result("unconfirmed", {**record, "reason": reason})
        raise RuntimeError(f"沟通结果未确认：{title}；{reason}。已停止，避免重复发送")

    def _process_reviews(self):
        """处理人工批准的岗位，使用与自动模式相同的发送和确认流程。"""
        while True:
            try:
                item = self._review_queue.get_nowait()
            except queue.Empty:
                return
            if not item.get("decision", {}).get("should_apply"):
                self._finish_approval(item)
                continue
            review = item.get("review", {})
            url = review.get("url", "")
            if not url or self._ledger.was_sent(url):
                self._finish_approval(item)
                continue
            if not self._wait_for_quota():
                self._review_queue.put(item)
                return
            title = review.get("job_title", "未知岗位")
            self.add_log("info", f"  👤 人工审核通过，正在沟通: {title}")
            try:
                opened = self._browser.open_job_detail(url)
            except Exception:
                self._review_queue.put(item)
                raise
            if not opened:
                self._stats["failed"] += 1
                self.add_log("warn", f"  ❌ 岗位详情未打开: {title}")
                self._review_queue.put(item)
                self._browser.back_to_search()
                return
            try:
                self._apply_current_job({
                    "job_title": title,
                    "company": review.get("company", ""),
                    "salary": review.get("salary", ""),
                    "match_score": review.get("match_score", 0),
                    "reason": "人工审核通过",
                    "url": url,
                })
            except Exception as exc:
                self._finish_approval(item, needs_manual=True)
                self.add_log("warn", f"  ⚠️ {title} 审核后沟通未确认，需人工核对：{exc}")
                self._browser.back_to_search()
                return
            self._finish_approval(item)
            self._browser.back_to_search()

    def _finish_approval(self, item, needs_manual=False):
        with self._lock:
            job_id = item.get("job_id")
            if needs_manual:
                for saved in self._approved_reviews:
                    if saved.get("job_id") == job_id:
                        saved["state"] = "needs_manual"
                        break
            else:
                self._approved_reviews = [saved for saved in self._approved_reviews
                                          if saved.get("job_id") != job_id]
            self._save_review_list_locked(self._approvals_path, self._approved_reviews)

    def _merge_auto_filters(self, filters: dict) -> dict:
        """智能筛选：未手动指定的筛选项，自动从简历推导默认值。

        经验：在校生/应届/无经验 → 「在校/应届」
        学历：简历 education 直接映射（本科/硕士/博士）
        类型：期望实习→「实习」；只期望全职/正式→「全职」；两者都要→不限
        薪资：BOSS 档位较粗（3K以下/3-5K/...），为避免误筛不做自动映射
        """
        result = dict(filters or {})
        edu = self._resume.get("education", "")
        status = self._resume.get("current_status", "") or ""
        exp = self._resume.get("expectations", {})
        years = int(self._resume.get("years_of_experience", 0) or 0)

        if not result.get("experience"):
            # Zero *full years* can still mean the person already has a job.
            # Do not silently narrow an employed graduate to student openings.
            student_status = ("在校" in status or "应届" in status) and not (
                "现任" in status or "在职" in status)
            if student_status or (years == 0 and not status):
                result["experience"] = "在校/应届"
        if not result.get("education") and edu:
            result["education"] = edu
        if not result.get("job_type"):
            jt = exp.get("job_type", "")
            if "实习" in jt and "全职" not in jt and "正式" not in jt:
                result["job_type"] = "实习"
            elif ("全职" in jt or "正式" in jt) and "实习" not in jt:
                result["job_type"] = "全职"
        return result

    # ==================== 控制命令 ====================

    def start(self):
        if self._status == self.PAUSED:
            return self.resume()
        if self._thread and self._thread.is_alive():
            return False, "引擎正在运行或尚未完全停止"

        self._stop_event.clear()
        self._pause_event.set()
        self._stats = {"total": 0, "applied": 0, "skipped": 0,
                       "failed": 0, "skipped_no_match": 0, "skip_reasons": []}
        self._pending_reviews = self._read_review_list(self._reviews_path)
        self._approved_reviews = self._read_review_list(self._approvals_path)
        self._review_queue = queue.Queue()
        for item in self._approved_reviews:
            if item.get("state") == "queued":
                self._review_queue.put(item)
        self._last_ten_pause_count = 0
        self._processed_jobs = set()
        self._log_buffer = []
        self._account = None
        self._incomplete_cities = []
        self._last_search_day = None
        self._start_time = time.time()

        # 重新加载配置
        with self._lock:
            self._config = load_config()
            self._resume = load_resume()
            self._env = load_env()
            self._resume_text = build_resume_text(self._resume)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True, "已启动"

    def stop(self):
        if not self._thread or not self._thread.is_alive():
            return False, "引擎未运行"
        self._stop_event.set()
        self._pause_event.set()  # A paused worker must wake to observe stop.
        self._set_status(self.STOPPING, "正在停止...")
        self.add_log("warn", "🛑 正在停止引擎...")
        return True, "正在停止"

    def pause(self):
        if self._status != self.RUNNING:
            return False, f"当前状态 {self._status}，无法暂停"
        self._pause_event.clear()
        self._set_status(self.PAUSED, "已暂停（等待当前任务完成后暂停）")
        self.add_log("warn", "⏸️  暂停中...")
        return True, "正在暂停"

    def resume(self):
        if self._status != self.PAUSED:
            return False, f"当前状态 {self._status}，无法继续"
        self._pause_event.set()
        self._set_status(self.RUNNING, "运行中")
        self.add_log("info", "▶️  继续运行")
        return True, "已继续"

    def get_status_payload(self) -> dict:
        """获取完整的状态信息（供 API 调用）"""
        with self._lock:
            return {
                "status": self._status,
                "status_msg": self._status_msg,
                "stats": dict(self._stats),
                "current_job": self._current_job,
                "mode": self._mode,
                "account": dict(self._account) if self._account else None,
                "incomplete_cities": list(self._incomplete_cities),
                "elapsed": self.elapsed,
                "pending_reviews": len(self._pending_reviews),
                "queued_approvals": self._review_queue.qsize(),
                "manual_approvals": sum(item.get("state") == "needs_manual"
                                        for item in self._approved_reviews),
                "submission": self._ledger.summary(),
                "ai_connection": self._analyzer.connection_status() if self._analyzer else None,
                "config": {
                    "search": {k: v for k, v in self._config.get("search", {}).items()},
                    "ai": {k: v for k, v in self._config.get("ai", {}).items()},
                    "application": {k: v for k, v in self._config.get("application", {}).items()},
                    "scoring": {k: v for k, v in self._config.get("scoring", {}).items()},
                    "blacklist": {k: v for k, v in self._config.get("blacklist", {}).items()},
                },
                "resume_preview": {
                    "name": self._resume.get("name", ""),
                    "school": self._resume.get("school", ""),
                    "target_roles": self._resume.get("target_roles", []),
                },
            }

    # ==================== 核心运行循环 ====================

    def _run(self):
        """引擎主循环（在后台线程中运行）"""
        self._set_status(self.RUNNING, "正在初始化...")
        self.add_log("info", "🚀 BOSS 直聘自动沟通引擎启动")

        # ⭐ v3.0 初始化数据库（超预期岗位收藏表）
        try:
            database.init_db()
        except Exception as e:
            self.add_log("warn", f"  ⚠️ 数据库初始化失败（超预期收藏不可用）: {e}")

        # 检查 API Key
        if not self._env["api_key"] or self._env["api_key"] == "sk-your-deepseek-api-key-here":
            self.add_log("error", "❌ 未配置 DeepSeek API Key！请在 .env 中设置")
            self._set_status(self.ERROR, "API Key 未配置")
            self._finish_run()
            return

        # 初始化 AI 分析器
        self.add_log("info", "🤖 正在初始化 AI 分析器...")
        self._analyzer = JDAnalyzer(api_key=self._env["api_key"],
                                    api_base=self._env["api_base"], model=self._env["model"],
                                    http_proxy=self._env.get("ai_http_proxy", ""))
        if not self._analyzer.is_ready():
            self.add_log("error", "❌ AI 分析器初始化失败")
            self._set_status(self.ERROR, "AI 初始化失败")
            self._finish_run()
            return
        self.add_log("info", "✅ AI 分析器就绪")

        # 浏览器或扩展可能暂时无法响应（例如页面加载、电脑锁屏）。
        # Keep the service alive and retry instead of ending the overnight run.
        while not self._stop_event.is_set():
            try:
                self.add_log("info", "🌐 正在连接 KIMI Webbridge...")
                self._browser = BossBrowser(self._config)
                self._browser.start()
                self._submission = SubmissionWorkflow(self._browser.wb, self._ledger, self.add_log)
                self.add_log("info", "✅ Webbridge 连接成功！")
                self.add_log("info", "🔑 检查登录状态...")
                self._browser.ensure_logged_in()
                account = self._browser.verify_account_resume(self._resume.get("name", ""))
                with self._lock:
                    self._account = account
                break
            except Exception as exc:
                observed = getattr(self._browser, "last_account", None) if self._browser else None
                if observed:
                    with self._lock:
                        self._account = observed
                self.add_log("warn", f"⚠️ 浏览器暂不可用：{exc}；60 秒后自动重试")
                if self._browser:
                    try:
                        self._browser.close()
                    except Exception:
                        pass
                self._set_status(self.RUNNING, "等待浏览器恢复，60 秒后重试")
                if self._stop_event.wait(60):
                    self._finish_run()
                    return
        if self._stop_event.is_set():
            self._finish_run()
            return

        # 执行搜索与投递循环
        try:
            self._complete_pending_once()
            self._last_search_day = datetime.now().date()
            self._run_search_cycle()
            self._monitor_followups()
        except Exception as e:
            self.add_log("error", f"❌ 运行异常: {e}")
            self._set_status(self.ERROR, f"异常: {str(e)[:50]}")
        finally:
            self._finish_run()

    def _finish_run(self):
        """Release connections and leave the panel in a usable state on every exit path."""
        if self._browser:
            try:
                self._browser.close()
            except Exception as exc:
                self.add_log("warn", f"⚠️ 关闭浏览器桥接失败：{exc}")
            self._browser = None
        if self._analyzer:
            self._analyzer.close()
            self._analyzer = None
        if self._status != self.ERROR:
            self._set_status(self.IDLE, "已完成")
        self.add_log("info", f"👋 引擎结束，共处理 {self._stats['total']} 个岗位，"
                              f"已确认沟通 {self._stats['applied']} 个")

    def _run_search_cycle(self):
        """Search all configured cities once; the monitor schedules later days."""
        targets = resolve_city_targets(self._config["search"].get("city", ""))
        page_limits = page_allocations(self._config["search"].get("page_count", 5), len(targets))
        for (city_name, city_code), city_pages in zip(targets, page_limits):
            if self._stop_event.is_set():
                break
            self.add_log("info", f"🌆 城市 {city_name}：最多搜索 {city_pages} 批")
            selected_range = salary_range_for_city(
                self._config["search"].get("salary_ranges_by_city"), city_name)
            if selected_range:
                _, min_k, max_k = selected_range
                shown_range = (f"{min_k:g}–{max_k:g}K" if max_k is not None
                               else f"{min_k:g}K 以上")
                self.add_log("info", f"💰 {city_name}：岗位月薪下限须在 {shown_range}")
            for attempt in range(3):
                if self._stop_event.is_set():
                    break
                try:
                    self._main_loop(city_code, city_name, city_pages)
                    break
                except Exception as exc:
                    self.add_log("warn", f"⚠️ {city_name} 搜索中断（第 {attempt + 1} 次）：{exc}")
                    if attempt == 2:
                        self.add_log("error", f"❌ {city_name} 搜索重试未恢复；继续下一城市")
                        with self._lock:
                            self._incomplete_cities.append(city_name)
                        break
                    if self._stop_event.wait(30):
                        break
                    self._complete_pending_once()

    def _complete_pending_once(self):
        pending = self._ledger.pending()
        for record in pending:
            if self._stop_event.is_set():
                return
            greeting = record.get("greeting") or greeting_for(self._resume, record)
            try:
                status, detail = self._submission.process(record, greeting)
                self._ledger.record_followup_status(record, status, detail)
                self.add_log("info" if status in ("sent", "pending") else "warn",
                             f"  📎 待补发 {record.get('job_title', '')}：{detail}")
            except Exception as exc:
                self.add_log("warn", f"  ⚠️ 待补发 {record.get('job_title', '')}：{exc}")

    def _monitor_followups(self):
        """Keep the browser worker alive for human approvals and unlocked resumes."""
        next_resume_check = time.monotonic() + 300
        while not self._stop_event.is_set():
            if not self._pause_event.is_set():
                self._stop_event.wait(2)
                continue
            daily_time = self._config.get("search", {}).get("daily_search_time", "09:00")
            now = datetime.now()
            if (daily_time and self._last_search_day and now.date() > self._last_search_day
                    and now.strftime("%H:%M") >= daily_time):
                self._last_search_day = now.date()
                with self._lock:
                    self._stats = {"total": 0, "applied": 0, "skipped": 0,
                                   "failed": 0, "skipped_no_match": 0, "skip_reasons": []}
                    self._incomplete_cities = []
                self._processed_jobs.clear()
                self._last_ten_pause_count = 0
                self.add_log("info", f"📅 开始 {now.date()} 的定时搜索")
                try:
                    self._run_search_cycle()
                except Exception as exc:
                    self.add_log("error", f"❌ 今日定时搜索异常：{exc}；明日继续尝试")
                next_resume_check = time.monotonic() + 300
                continue
            self._process_reviews()
            if time.monotonic() >= next_resume_check:
                self._complete_pending_once()
                next_resume_check = time.monotonic() + 300
            resume_count = len(self._ledger.pending())
            with self._lock:
                review_count = len(self._pending_reviews)
            if not resume_count and not review_count and self._review_queue.empty() and not daily_time:
                return
            next_search = (f"；下次搜索 {daily_time}" if daily_time else "")
            self._set_status(self.RUNNING,
                             f"等待后续处理：待审核 {review_count}，待完成会话 {resume_count}{next_search}")
            self._stop_event.wait(2)

    def _main_loop(self, city_code="", city_name="不限", page_limit=None):
        """搜索→分析→投递 主循环

        每个关键词只搜索一次，之后**停留在搜索页**：
          - 通过「左侧岗位列表下拉滚动」懒加载更多岗位（BOSS 原生机制）
          - 或点击「下一页」按钮（兜底）
        不再每页重新导航搜索，避免只取到第一页的数据。
        """
        search_cfg = self._config["search"]
        app_cfg = self._config["application"]
        ai_cfg = self._config["ai"]
        allowed, reason, next_at = self._ledger.quota(
            max(1, int(app_cfg.get("daily_limit", 50))),
            max(1, int(app_cfg.get("max_per_hour", 15))),
        )
        if not allowed and next_at is None:
            self.add_log("warn", f"⏹️  {reason}，本轮不再搜索")
            return
        threshold = ai_cfg.get("match_threshold", 60)
        keywords_to_try = [search_cfg["keyword"]] + search_cfg.get("secondary_keywords", [])
        city = city_code
        max_pages = page_limit or search_cfg.get("page_count", 5)

        # ── 网页内置筛选（经验/学历/薪资/类型），未设置的自动从简历推导 ──
        filters = dict(search_cfg.get("filters", {}))
        if search_cfg.get("salary_ranges_by_city"):
            # The BOSS dropdown is global and coarse; exact per-city ranges
            # are checked from each job's displayed monthly pay instead.
            filters["salary"] = ""
        if search_cfg.get("auto_filters_from_resume", False):
            filters = self._merge_auto_filters(filters)
        active_filters = {k: v for k, v in filters.items() if v and v != "不限"}
        if active_filters:
            desc = "、".join(f"{k}={v}" for k, v in active_filters.items())
            self.add_log("info", f"🎚️ 本次将应用 BOSS 网页内置筛选: {desc}")

        kw_index = 0
        while kw_index < len(keywords_to_try):
            keyword = keywords_to_try[kw_index]

            # ── 停止/暂停/上限 检查 ──
            if self._stop_event.is_set():
                self.add_log("info", "🛑 收到停止信号")
                return
            if not self._pause_event.is_set():
                self.add_log("warn", "⏸️  引擎已暂停")
                self._set_status(self.PAUSED, "已暂停")
                self._pause_event.wait()
                if self._stop_event.is_set():
                    return
                self.add_log("info", "▶️  继续运行")
                self._set_status(self.RUNNING, "继续运行")
            if self._stats["applied"] >= app_cfg.get("daily_limit", 50):
                self.add_log("info", f"📊 达到每日上限 ({app_cfg['daily_limit']}个)")
                break

            # 处理已完成的人工审核投递
            self._process_reviews()

            self.add_log("info", f"🔍 搜索关键词: {keyword}（城市: {city_name}，代码: {city or '不限'}）")
            self._set_status(self.RUNNING, f"搜索: {keyword}")

            # ── 搜索（每个关键词只导航一次，后续靠下拉/翻页加载）──
            found = self._browser.search_jobs(keyword=keyword, city=city, filters=filters)
            if not found:
                self.add_log("info", "🔄 该关键词搜索失败或无结果")
                kw_index += 1
                if kw_index < len(keywords_to_try):
                    self.add_log("info", f"🔄 切换关键词: {keywords_to_try[kw_index]}")
                continue

            # 首次搜索成功后不再尝试备选关键词（备选词仅作兜底）
            page = 1
            # A browser timeout can restart this search in the same run. Keep
            # completed URLs out of later attempts instead of rescoring them.
            seen_urls = set(self._processed_jobs)
            while page <= max_pages:
                # ── 停止/暂停/上限 检查 ──
                if self._stop_event.is_set():
                    return
                if not self._pause_event.is_set():
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        return
                if self._stats["applied"] >= app_cfg.get("daily_limit", 50):
                    break

                self._process_reviews()

                # ── 获取本批新岗位 URL（去重）──
                time.sleep(2)
                new_urls = []
                for url in self._browser.get_job_urls():
                    key = job_key(url)
                    if key not in seen_urls:
                        seen_urls.add(key)
                        new_urls.append(url)
                if new_urls:
                    self.add_log("info", f"📋 第 {page} 批: {len(new_urls)} 个岗位（累计 {len(seen_urls)}）")
                    for idx, job_url in enumerate(new_urls):
                        if self._stop_event.is_set():
                            return
                        if not self._pause_event.is_set():
                            self._pause_event.wait()
                            if self._stop_event.is_set():
                                return
                        if self._stats["applied"] >= app_cfg.get("daily_limit", 50):
                            break
                        result = self._process_job(job_url, idx, len(new_urls), page,
                                                   threshold, app_cfg, city_name)
                        if result == "retry" and not self._stop_event.wait(5):
                            self.add_log("info", "  🔄 此岗位重试一次")
                            result = self._process_job(job_url, idx, len(new_urls), page,
                                                       threshold, app_cfg, city_name)
                        if result not in ("retry", "stop", "quota"):
                            self._processed_jobs.add(job_key(job_url))
                        if result in ("stop", "quota"):
                            if result == "stop":
                                self.add_log("warn", "⏹️  当前批次已停止")
                            return
                else:
                    self.add_log("info", "  本批无新岗位")

                if self._stats["applied"] >= app_cfg.get("daily_limit", 50):
                    break

                # ── 加载更多岗位：优先左侧列表下拉滚动，其次「下一页」按钮 ──
                self._set_status(self.RUNNING, f"加载第 {page+1} 批")
                more = self._browser.load_more_jobs()
                if more > 0:
                    self.add_log("info", f"🔄 左侧下拉加载更多（新增 {more} 个岗位链接）")
                elif self._browser.has_next_page():
                    self._browser.go_next_page()
                    self.add_log("info", f"📄 已点击下一页（第 {page+1} 批）")
                else:
                    self.add_log("info", "📋 已加载到底，本关键词结束")
                    break
                page += 1
                random_delay(3, 6)

            # 处理剩余的人工审核投递
            self._process_reviews()
            break  # 主关键词处理完成，不再自动切换备选词

        self.add_log("info", f"🏁 处理完成，共 {self._stats['total']} 个岗位，"
                             f"已确认沟通 {self._stats['applied']} 个")

    def _process_job(self, job_url, idx, total, page, threshold, app_cfg,
                     city_name="不限"):
        """处理单个岗位：打开详情 → 检查 → AI 分析 → 投递/跳过

        返回 "stop" 表示需要停止搜索（半自动待审核超限），否则返回 None。
        """
        # 每投递 10 个暂停
        applied_count = self._stats["applied"]
        if applied_count > 0 and applied_count % 10 == 0 and applied_count != self._last_ten_pause_count:
            self._last_ten_pause_count = applied_count
            pause_sec = app_cfg.get("apply_10_pause_minutes", 3) * 60
            self.add_log("info", f"⏸️  已沟通 {applied_count} 个，暂停 {pause_sec//60} 分钟")
            if self._stop_event.wait(pause_sec):
                return "stop"

        self._stats["total"] += 1
        self.add_log("info", f"── [{self._stats['total']}] 岗位 {idx+1}/{total} ──")

        # ── ⭐ v3.1 黑名单检查（打开详情前）：命中直接跳过，不读 JD、不调 AI ──
        bl_cfg = blacklist.blacklist_config(self._config)
        bl_rec = None
        if bl_cfg["enabled"]:
            skip_bl, bl_rec = blacklist.check_skip(job_url)
            if skip_bl:
                self._stats["skipped"] += 1
                self.add_log("info", f"  🚫 黑名单跳过（{bl_rec.get('reason', '低质岗位')}）")
                random_delay(2, 4)
                return

        if self._ledger.was_sent(job_url):
            self._stats["skipped"] += 1
            self.add_log("info", "  📋 投递记录中已有该岗位，跳过")
            return

        # ── 打开岗位详情 ──
        self._set_status(self.RUNNING, f"打开详情 {idx+1}")
        if not self._browser.open_job_detail(job_url):
            self._stats["failed"] += 1
            self.add_log("warn", "  ⚠️ 打开详情失败")
            random_delay(2, 4)
            self._browser.back_to_search()
            return "retry"

        time.sleep(1)

        # ── 检查验证码 ──
        if self._browser.has_captcha():
            self.add_log("warn", "🔐 检测到验证码")
            self._browser.handle_captcha()

        # ── 检查是否已投递 ──
        if self._browser.has_applied():
            self.add_log("info", "  📋 BOSS 显示已沟通，跳过")
            self._stats["skipped"] += 1
            self._browser.back_to_search()
            random_delay(2, 4)
            return

        # ── 提取 JD ──
        jd_info = self._browser.extract_jd_text()
        jd_text = self._browser.full_jd_text(jd_info)
        job_title = jd_info.get("title", "未知岗位")
        self._set_current_job({
            "title": job_title,
            "company": jd_info.get("company", ""),
            "company_size": jd_info.get("company_size", ""),
            "financing": jd_info.get("financing", ""),
            "listed": jd_info.get("listed"),
            "industry": jd_info.get("industry", ""),
            "salary": jd_info.get("salary", ""),
            "url": job_url, "page": page,
        })

        if not jd_text.strip():
            self.add_log("warn", "  ⚠️ JD 内容为空，跳过")
            self._stats["failed"] += 1
            self._browser.back_to_search()
            random_delay(2, 4)
            return "retry"

        if city_name != "不限":
            actual_city = str(jd_info.get("city") or "").strip()
            if not actual_city.startswith(city_name):
                reason = f"岗位城市 {actual_city or '未识别'} 不属于本轮 {city_name}"
                self._stats["skipped"] += 1
                self.add_log("info", f"  ⏭️ {reason}，未发起沟通")
                save_result("skipped", {
                    "job_title": job_title,
                    "company": jd_info.get("company", ""),
                    "city": actual_city,
                    "url": job_url,
                    "reason": reason,
                })
                self._browser.back_to_search()
                return

        # A mixed role can score well on technical keywords while spending
        # most of the working day outside the user's target function.
        excluded_titles = self._config.get("search", {}).get(
            "excluded_title_keywords", ["内容审核", "内容审查"])
        if any(word and word in job_title for word in excluded_titles):
            reason = "岗位名称含非目标职责"
            self._stats["skipped"] += 1
            self.add_log("info", f"  ⏭️ 跳过 {job_title}（{reason}）")
            save_result("skipped", {
                "job_title": job_title,
                "company": jd_info.get("company", ""),
                "url": job_url,
                "reason": reason,
            })
            self._browser.back_to_search()
            return

        ranges = self._config.get("search", {}).get("salary_ranges_by_city")
        range_setting = salary_range_for_city(ranges, jd_info.get("city", ""), city_name)
        floor_setting = (None if ranges is not None else salary_floor_for_city(
            self._config.get("search", {}).get("salary_floor_by_city", {}),
            jd_info.get("city", ""), city_name))
        if range_setting or floor_setting:
            floor_city, floor_k = range_setting[:2] if range_setting else floor_setting
            ceiling_k = range_setting[2] if range_setting else None
            monthly_min_k = minimum_monthly_salary_k(jd_info.get("salary", ""))
            if (monthly_min_k is None or monthly_min_k < floor_k or
                    (ceiling_k is not None and monthly_min_k > ceiling_k)):
                reason = (f"月薪无法核对，未达到{floor_city}最低月薪筛选条件"
                          if monthly_min_k is None else
                          f"月薪下限 {monthly_min_k:g}K 不在{floor_city}设置的 "
                          f"{floor_k:g}K–{ceiling_k:g}K 内" if ceiling_k is not None else
                          f"月薪下限 {monthly_min_k:g}K 低于{floor_city}设置的 {floor_k:g}K")
                self._stats["skipped"] += 1
                self.add_log("info", f"  ⏭️ 跳过 {job_title}（{reason}）")
                save_result("skipped", {
                    "job_title": job_title,
                    "company": jd_info.get("company", ""),
                    "city": jd_info.get("city", ""),
                    "salary": jd_info.get("salary", ""),
                    "url": job_url,
                    "reason": reason,
                })
                self._browser.back_to_search()
                return

        # ── ⭐ v3.1 劳务外包关键词预检（零 AI 成本）：命中直接拉黑跳过，省一次 AI 调用 ──
        if bl_cfg["enabled"] and blacklist.match_outsourcing(
                jd_text, bl_cfg["outsourcing_keywords"]):
            blacklist.classify(job_url, jd_info, jd_text,
                               {"job_quality": {"is_outsourcing": True}},
                               0, self._config, existing=bl_rec)
            self._stats["skipped"] += 1
            self.add_log("info", f"  🚫 劳务外包岗位已拉黑: {job_title}")
            self._browser.back_to_search()
            random_delay(2, 4)
            return

        # ── AI 分析（评分规则实时读取，面板修改即刻生效）──
        self._set_status(self.RUNNING, f"分析: {job_title}")
        decision = self._analyzer.analyze(
            jd_text=jd_text,
            resume_text=self._resume_text,
            threshold=threshold,
            scoring_rules=build_scoring_rules(self._config),
        ) if self._analyzer.is_ready() else {
            "match_score": 0, "should_apply": False,
            "reason": "AI 未配置", "key_matches": [], "key_gaps": [],
        }
        if decision.get("reason") in ("AI 不可用", "AI 未配置"):
            # An API outage is not a job-quality judgment. In particular,
            # score 0 must not be written to the persistent blacklist.
            self._stats["failed"] += 1
            self.add_log("warn", f"  ⚠️ {job_title}：AI 暂不可用，本轮不评估或拉黑")
            self._browser.back_to_search()
            return "retry"

        # ⭐ v3.0 评分分级：超预期（≥over_threshold）/ 符合预期（≥threshold）/ 不符合
        score = decision.get("match_score", 0)
        over_threshold = int(self._config.get("ai", {}).get("over_match_threshold", 80) or 80)
        if score >= over_threshold:
            match_level = "超预期"
        elif score >= threshold:
            match_level = "符合预期"
        else:
            match_level = "不符合"

        # ── ⭐ v3.1 黑名单自动判定：外包/名不副实→永久，远超简历→短期，低分→观察/升级 ──
        bl_act = blacklist.classify(job_url, jd_info, jd_text, decision, score,
                                    self._config, existing=bl_rec) if bl_cfg["enabled"] else None
        if bl_act:
            level_tag = "⛔ 永久" if bl_act["level"] == "permanent" else \
                        "🕐 短期" if bl_act["level"] == "temporary" else "👀 观察"
            self.add_log("info", f"  🚫 低质岗位已拉黑（{bl_act['reason']}）[{level_tag}]")
            if bl_act.get("should_skip"):
                self._stats["skipped"] += 1
                self._browser.back_to_search()
                random_delay(2, 4)
                return

        # ⭐ v3.0 详细岗位信息（含 BOSS 网址 / 公司规模 / 是否上市 / 行业）
        # 符合预期 + 超预期岗位都保存详细信息到 results/matched_*.json 并写入日志（含可点击网址）；
        # 超预期岗位额外收藏到 SQLite 数据库，可在控制面板新页面查看历史。
        detail_record = {
            "job_title": job_title,
            "company": jd_info.get("company", ""),
            "company_size": jd_info.get("company_size", ""),
            "financing": jd_info.get("financing", ""),
            "listed": jd_info.get("listed"),
            "industry": jd_info.get("industry", ""),
            "salary": jd_info.get("salary", ""),
            "city": jd_info.get("city", ""),
            "education": jd_info.get("education", ""),
            "experience": jd_info.get("experience", ""),
            "job_type": jd_info.get("job_type", ""),
            "url": job_url,
            "match_score": score,
            "reason": decision.get("reason", ""),
            "match_level": match_level,
        }
        if match_level in ("超预期", "符合预期"):
            try:
                save_result("matched", detail_record)
                database.record_history_match(detail_record)
            except Exception as e:
                self.add_log("warn", f"  ⚠️ 保存详细结果失败: {e}")
            listed_txt = ""
            if jd_info.get("listed") is True:
                listed_txt = "已上市"
            elif jd_info.get("listed") is False:
                listed_txt = "未上市"
            elif jd_info.get("financing"):
                listed_txt = str(jd_info["financing"])
            det = (f"  📌 {match_level}岗位（{score}分）: {job_title}"
                   f" | {jd_info.get('company', '-')}"
                   f" | 规模:{jd_info.get('company_size', '-')}"
                   f" | 上市:{listed_txt or '-'}"
                   f"{' | 行业:' + jd_info['industry'] if jd_info.get('industry') else ''}")
            self.add_log("info", det)
            self.add_log("info", f"     🔗 {job_url}")
        if match_level == "超预期":
            try:
                database.save_starred_job({**detail_record, "jd_text": jd_text})
                self.add_log("info", f"  ⭐ 超预期岗位已收藏（可在控制面板「⭐ 超预期岗位」页面查看）")
            except Exception as e:
                self.add_log("warn", f"  ⚠️ 收藏超预期岗位失败: {e}")

        # ── 半自动模式：加入待审核列表，不阻塞，继续搜索 ──
        job_id = f"p{page}_c{idx}_{int(time.time())}"
        should_apply = bool(decision.get("should_apply")) and score >= threshold

        if self._mode == "semi":
            # 先处理已完成的人工审核投递
            self._process_reviews()

            review_item = {
                "job_id": job_id,
                "job_title": job_title,
                "company": jd_info.get("company", ""),        # ⭐ v2.6 公司名称
                "company_size": jd_info.get("company_size", ""),  # ⭐ v3.0 公司规模
                "financing": jd_info.get("financing", ""),        # ⭐ v3.0 融资/上市
                "listed": jd_info.get("listed"),                  # ⭐ v3.0 是否上市
                "industry": jd_info.get("industry", ""),          # ⭐ v3.0 行业
                "salary": jd_info.get("salary", ""),
                "education": jd_info.get("education", ""),    # ⭐ v2.6 学历要求
                "experience": jd_info.get("experience", ""),  # ⭐ v2.6 工作经验
                "city": jd_info.get("city", ""),              # ⭐ v2.6 城市
                "url": job_url,  # 保存 URL 供后续人工审核投递用
                "match_score": decision.get("match_score", 0),
                "match_level": match_level,                   # ⭐ v3.0 评分分级
                "ai_decision": "投递" if should_apply else "跳过",
                "reason": decision.get("reason", ""),
                "key_matches": decision.get("key_matches", []),
                "key_gaps": decision.get("key_gaps", []),
                "page": page,
                "status": "pending",
            }
            with self._lock:
                self._pending_reviews.append(review_item)
                self._save_review_list_locked(self._reviews_path, self._pending_reviews)
                pending_count = len(self._pending_reviews)
            self.add_log("info", f"  🤔 已加入审核列表 (共{pending_count}条待审, AI建议: {review_item['ai_decision']})")

            # 半自动模式下引擎不直接投递，由人工决定
            should_apply = False

            # 安全上限：待审核超过 50 条时停止搜索
            if pending_count >= 50:
                self.add_log("warn", f"⚠️ 待审核岗位已达 50 条上限（防数据泄露），停止搜索")
                return "stop"
            self._browser.back_to_search()
            random_delay(*app_cfg.get("delay_range_seconds", [3, 8]))
            return

        # ── 执行投递或跳过 ──
        if should_apply:
            if not self._wait_for_quota():
                return "quota"
            self._set_status(self.RUNNING, f"沟通: {job_title}")
            self.add_log("info", f"  🚀 开始沟通（匹配度: {score}%）")
            self._apply_current_job({
                "job_title": job_title,
                "greeting": greeting_for(self._resume, {"job_title": job_title, "jd_text": jd_text}),
                "company": jd_info.get("company", ""),
                "company_size": jd_info.get("company_size", ""),
                "financing": jd_info.get("financing", ""),
                "listed": jd_info.get("listed"),
                "salary": jd_info.get("salary", ""),
                "city": jd_info.get("city", ""),
                "industry": jd_info.get("industry", ""),
                "education": jd_info.get("education", ""),
                "experience": jd_info.get("experience", ""),
                "job_type": jd_info.get("job_type", ""),
                "match_level": match_level,
                "match_score": score,
                "reason": decision.get("reason", ""),
                "url": job_url,
            })
            self._browser.back_to_search()
        else:
            self._stats["skipped"] += 1
            reason = decision.get("reason", "AI 建议跳过")
            with self._lock:
                if reason:
                    self._stats.setdefault("skip_reasons", []).append(reason)
            self.add_log("info", f"  ⏭️ 跳过 ({reason})")
            if decision.get("match_score", 0) > 0:
                self._stats["skipped_no_match"] += 1
            save_result("skipped", {
                "job_title": job_title,
                "company": jd_info.get("company", ""),
                "company_size": jd_info.get("company_size", ""),
                "financing": jd_info.get("financing", ""),
                "listed": jd_info.get("listed"),
                "salary": jd_info.get("salary", ""),
                "match_score": score, "reason": reason,
                "url": job_url,  # ⭐ v3.0 保存 BOSS 网址供回看
            })
            self._browser.back_to_search()

        # ── 岗位间延时 ──
        delay_range = app_cfg.get("delay_range_seconds", [3, 8])
        random_delay(delay_range[0], delay_range[1])
