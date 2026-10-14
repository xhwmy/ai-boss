"""
BOSS 直聘自动投递 — Web 控制面板服务器
"""

import json
import time
import threading
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from boss_engine import BossEngine
from application_ledger import ApplicationLedger
import database  # ⭐ v3.0 超预期岗位收藏（SQLite）
import resume_upload  # 📎 v3.2 简历附件上传（.docx / .pdf）

app = Flask(__name__)
# 📎 v3.2 简历附件上传限制（10MB）
app.config["MAX_CONTENT_LENGTH"] = resume_upload.MAX_FILE_BYTES
app.config["UPLOAD_FOLDER"] = str(resume_upload.UPLOAD_DIR)

# 全局引擎实例
engine = BossEngine()


def _keep_awake_while_running():
    """Prevent Windows sleep only while the overnight engine is active."""
    import ctypes

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    kernel32 = ctypes.windll.kernel32
    while True:
        active = engine._status in (engine.RUNNING, engine.PAUSED, engine.STOPPING)
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0)
        kernel32.SetThreadExecutionState(flags)
        time.sleep(20)

# ⭐ v3.0 初始化数据库（幂等）
database.init_db()


# ==================== 页面路由 ====================

@app.route("/")
def index():
    """主页面"""
    return render_template("index.html")


@app.route("/starred")
def starred_page():
    """⭐ v3.0 超预期岗位收藏查看页（独立新页面）"""
    return render_template("starred.html")


@app.route("/blacklist")
def blacklist_page():
    """⭐ v3.1 低质量岗位黑名单查看页（独立新页面）"""
    return render_template("blacklist.html")


@app.route("/history")
def history_page():
    """按日期查看已保存的投递与消息发送记录。"""
    return render_template("history.html")


# ==================== 配置 API ====================

@app.route("/api/config", methods=["GET"])
def get_config():
    """获取当前配置"""
    return jsonify(engine.config)


@app.route("/api/config", methods=["POST"])
def update_config():
    """更新配置"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "请求数据为空"}), 400
    try:
        engine.update_config(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/resume", methods=["GET"])
def get_resume():
    """获取简历信息"""
    return jsonify(engine.resume_data)


@app.route("/api/resume", methods=["POST"])
def update_resume():
    """更新简历"""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "请求数据为空"}), 400
    try:
        engine.update_resume(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== 📎 简历附件上传 API（v3.2） ====================

@app.route("/api/resume/upload", methods=["GET"])
def get_resume_upload():
    """获取上传简历附件的状态"""
    return jsonify({"success": True, "upload": resume_upload.get_uploaded_meta()})


@app.route("/api/resume/upload", methods=["POST"])
def upload_resume():
    """上传简历附件（仅 .docx / .pdf）"""
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "未选择文件"}), 400
    try:
        meta = resume_upload.save_uploaded(file)
        engine.reload_resume()  # 重建简历文本，附件全文立即参与 AI 评分
        engine.add_log("info",
                       f"📎 已上传简历附件: {meta['filename']}（{meta['format']}，{meta['text_length']} 字），AI 将按新简历评分")
        return jsonify({"success": True, "upload": meta})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"上传失败: {e}"}), 500


@app.route("/api/resume/upload", methods=["DELETE"])
def clear_resume_upload():
    """删除上传的简历附件，恢复仅使用 resume.json"""
    resume_upload.clear_uploaded()
    engine.reload_resume()
    engine.add_log("info", "🗑️ 已删除上传的简历附件")
    return jsonify({"success": True})


@app.errorhandler(413)
def too_large(e):
    """上传文件超过 10MB 上限"""
    return jsonify({"success": False, "error": f"文件过大（超过 {resume_upload.MAX_FILE_MB}MB），请压缩后重试"}), 413


# ==================== 控制 API ====================

@app.route("/api/control/start", methods=["POST"])
def api_start():
    success, msg = engine.start()
    return jsonify({"success": success, "message": msg})


@app.route("/api/control/stop", methods=["POST"])
def api_stop():
    success, msg = engine.stop()
    return jsonify({"success": success, "message": msg})


@app.route("/api/control/pause", methods=["POST"])
def api_pause():
    success, msg = engine.pause()
    return jsonify({"success": success, "message": msg})


@app.route("/api/control/resume", methods=["POST"])
def api_resume():
    success, msg = engine.resume()
    return jsonify({"success": success, "message": msg})


@app.route("/api/control/mode", methods=["POST"])
def api_set_mode():
    """设置运行模式：auto（全自动）/ semi（半自动）"""
    data = request.get_json()
    mode = data.get("mode", "auto")
    if mode not in ["auto", "semi"]:
        return jsonify({"success": False, "error": "模式无效"}), 400
    engine.mode = mode
    engine.add_log("info", f"🔄 切换至{'全自动' if mode == 'auto' else '半自动'}模式")
    return jsonify({"success": True, "mode": mode})


# ==================== 状态 API ====================

@app.route("/api/status")
def api_status():
    """获取引擎状态"""
    return jsonify(engine.get_status_payload())


@app.route("/api/history")
def api_history():
    day_text = request.args.get("date", date.today().isoformat())
    try:
        selected = date.fromisoformat(day_text)
        if selected.isoformat() != day_text:
            raise ValueError("日期格式错误")
    except (TypeError, ValueError):
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    return jsonify(ApplicationLedger().daily_report(selected))


@app.route("/api/logs")
def api_logs():
    """获取日志"""
    n = request.args.get("n", 200, type=int)
    return jsonify(engine.get_logs(n))


@app.route("/api/reviews")
def api_reviews():
    """获取待审核列表"""
    return jsonify(engine.pending_reviews)


@app.route("/api/reviews/decide", methods=["POST"])
def api_review_decide():
    """提交人工审核决策"""
    data = request.get_json()
    job_id = data.get("job_id")
    decision = data.get("decision", {})
    if not job_id:
        return jsonify({"success": False, "error": "缺少 job_id"}), 400
    if not engine.submit_review_decision(job_id, decision):
        return jsonify({"success": False, "error": "审核项不存在或已处理，请刷新列表"}), 404
    engine.add_log("info", f"👤 人工决策已提交: {job_id}")
    return jsonify({"success": True})


# ==================== ⭐ 超预期岗位收藏 API ====================

@app.route("/api/starred")
def api_starred():
    """获取全部历史超预期岗位"""
    try:
        return jsonify(database.get_starred_jobs())
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/starred/<int:job_id>", methods=["DELETE"])
def api_starred_delete(job_id):
    """删除一条超预期岗位记录"""
    try:
        ok = database.delete_starred_job(job_id)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/starred/clear", methods=["POST"])
def api_starred_clear():
    """清空全部超预期岗位记录"""
    try:
        n = database.clear_starred_jobs()
        engine.add_log("info", f"🗑️ 已清空超预期岗位收藏（{n} 条）")
        return jsonify({"success": True, "cleared": n})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== 🚫 低质量岗位黑名单 API（v3.1） ====================

@app.route("/api/blacklist")
def api_blacklist():
    """获取全部黑名单岗位"""
    try:
        return jsonify(database.get_blacklist_jobs())
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/blacklist", methods=["POST"])
def api_blacklist_add():
    """手动添加黑名单（人工拉黑），body: {url, job_title, company, reason, level}"""
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "缺少岗位网址"}), 400
    level = data.get("level", "permanent")
    if level not in ("permanent", "temporary", "watch"):
        level = "permanent"
    expires_at = ""
    duration_days = None
    if level == "temporary":
        duration_days = int(data.get("duration_days")
                            or engine.config.get("blacklist", {}).get("temp_days", 30))
        expires_at = (datetime.now() + timedelta(days=duration_days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        database.save_blacklist_job({
            "job_title": data.get("job_title", ""),
            "company": data.get("company", ""),
            "url": url,
            "reason": data.get("reason", ""),
            "level": level,
            "source": "manual",
            "duration_days": duration_days,
            "expires_at": expires_at,
            "match_score": int(data.get("match_score", 0) or 0),
        })
        tag = "⛔ 永久" if level == "permanent" else "🕐 短期" if level == "temporary" else "👀 观察"
        engine.add_log("info", f"  🚫 已人工拉黑（{tag}）: {data.get('job_title', url)}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/blacklist/<int:job_id>", methods=["POST"])
def api_blacklist_update(job_id):
    """更新黑名单等级（watch 一键转永久/短期等），body: {level, duration_days?}"""
    data = request.get_json() or {}
    level = data.get("level", "")
    if level not in ("permanent", "temporary", "watch"):
        return jsonify({"success": False, "error": "等级无效"}), 400
    duration_days = data.get("duration_days")
    if duration_days is None and level == "temporary":
        duration_days = int(engine.config.get("blacklist", {}).get("temp_days", 30))
    try:
        ok = database.update_blacklist_level(job_id, level, duration_days)
        if ok:
            engine.add_log("info", f"  🚫 已更新黑名单等级为 {level}（id={job_id}）")
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/blacklist/<int:job_id>", methods=["DELETE"])
def api_blacklist_delete(job_id):
    """删除一条黑名单记录（忽略/解除）"""
    try:
        ok = database.delete_blacklist_job(job_id)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/blacklist/clear", methods=["POST"])
def api_blacklist_clear():
    """清空全部黑名单记录"""
    try:
        n = database.clear_blacklist_jobs()
        engine.add_log("info", f"🗑️ 已清空黑名单（{n} 条）")
        return jsonify({"success": True, "cleared": n})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== 日志 SSE 流 ====================

@app.route("/api/events")
def api_events():
    """Server-Sent Events 实时推送"""
    last_seq = max(0, request.args.get("after", 0, type=int))

    def generate():
        nonlocal last_seq
        while True:
            new_logs = [entry for entry in engine.get_logs(500)
                        if entry.get("seq", 0) > last_seq]
            if new_logs:
                last_seq = new_logs[-1]["seq"]
                yield f"data: {json.dumps({'type': 'logs', 'data': new_logs})}\n\n"

            # 同时推送状态（含待审核数）
            payload = engine.get_status_payload()
            yield f"data: {json.dumps({'type': 'status', 'data': payload})}\n\n"

            time.sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== 启动 ====================

if __name__ == "__main__":
    import os
    import sys
    from urllib.error import URLError
    from urllib.request import Request, urlopen
    # 兼容非 UTF-8 控制台（PowerShell/cmd 默认 GBK），避免 emoji 打印导致启动崩溃
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    port = int(os.environ.get("PORT", 5000))
    # A second launch reuses the existing panel; it must not create another engine.
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
            current = json.load(response)
        if "status" in current and "submission" in current:
            req = Request(f"http://127.0.0.1:{port}/api/control/start",
                          data=b"{}", headers={"Content-Type": "application/json"},
                          method="POST")
            with urlopen(req, timeout=5) as response:
                print(json.load(response))
            sys.exit(0)
    except (URLError, OSError, ValueError):
        pass
    print("=" * 50)
    print("  🚀 BOSS 直聘 AI 控制面板")
    print(f"  🌐 http://localhost:{port}")
    print("  ⏹️  按 Ctrl+C 停止服务器")
    print("=" * 50)
    if os.environ.get("BOSS_AUTOSTART", "1") != "0":
        threading.Timer(1.0, engine.start).start()
    if os.name == "nt":
        threading.Thread(target=_keep_awake_while_running, daemon=True,
                         name="keep-awake-while-running").start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
