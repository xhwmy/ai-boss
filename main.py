"""
BOSS 直聘自动投递脚本 — 主程序入口

使用方式：
  1. 编辑 config.json（搜索条件）
  2. 编辑 resume.json（个人信息）
  3. 编辑 .env（填入 DeepSeek API Key）
  4. 运行：python main.py
"""

import sys
import time
from datetime import datetime

from config import (
    load_config,
    load_resume,
    load_env,
    build_resume_text,
    build_scoring_rules,
    validate_config,
)
from browser import BossBrowser
from analyzer import JDAnalyzer
import database  # ⭐ v3.0 超预期岗位收藏（SQLite）
import blacklist  # v3.1 低质量岗位拉黑名单（三级：永久/短期/观察）
from utils import (
    setup_logger,
    random_delay,
    human_pause,
    save_result,
    Timer,
)

logger = setup_logger("main")


def print_banner():
    """打印启动横幅"""
    print("=" * 56)
    print("  🚀  BOSS 直聘 AI 自动投递助手")
    print("  📅 ", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 56)
    print()


def print_statistics(results: dict):
    """打印本次运行统计"""
    total = results.get("total", 0)
    applied = results.get("applied", 0)
    skipped = results.get("skipped", 0)
    failed = results.get("failed", 0)
    elapsed = results.get("elapsed", "0秒")

    print()
    print("=" * 56)
    print("  📊  运行报告")
    print("=" * 56)
    print(f"  处理岗位: {total} 个")
    print(f"  ✅ 已投递: {applied} 个")
    print(f"  ⏭️  已跳过: {skipped} 个")
    print(f"  ❌ 失败:   {failed} 个")
    print(f"  ⏱️  耗时:   {elapsed}")
    print("=" * 56)
    print()

    if applied > 0:
        print("  📁 详细结果已保存到 results/ 目录")
    print()


def main():
    print_banner()

    # ── 1. 加载配置 ──
    logger.info("📁 正在加载配置...")
    try:
        config = load_config()
        resume = load_resume()
        env = load_env()
        validate_config(config)
    except FileNotFoundError as e:
        logger.error(f"❌ {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"❌ 配置错误: {e}")
        sys.exit(1)

    # ⭐ v3.0 初始化数据库（超预期岗位收藏表）
    try:
        database.init_db()
    except Exception as e:
        logger.warning(f"⚠️ 数据库初始化失败（超预期收藏不可用）: {e}")

    # 构建简历文本（供 AI 分析使用）
    resume_text = build_resume_text(resume)
    logger.info("✅ 配置加载完成")
    logger.info(f"🔍 搜索: {config['search']['keyword']} · {config['search'].get('city', '全国')}")
    logger.info(f"👤 求职者: {resume.get('name', '未知')}")

    # 检查 API Key
    if not env["api_key"] or env["api_key"] == "sk-your-deepseek-api-key-here":
        logger.error("❌ 请先在 .env 文件中填入 DeepSeek API Key！")
        logger.error("   获取地址: https://platform.deepseek.com/")
        sys.exit(1)

    # ── 2. 初始化 AI 分析器 ──
    analyzer = JDAnalyzer(api_key=env["api_key"], api_base=env["api_base"], model=env["model"])
    if not analyzer.is_ready():
        logger.warning("⚠️ AI 分析器未就绪，将跳过所有岗位（仅浏览）")
        logger.warning("   但脚本仍可运行，方便你测试浏览器功能")

    # ── 3. 启动浏览器 ──
    logger.info("🌐 正在启动浏览器...")
    browser = BossBrowser(config)
    try:
        browser.start()
    except Exception as e:
        logger.error(f"❌ 浏览器连接失败: {e}")
        logger.error("   请确认: 1) Edge 浏览器已打开 2) KIMI Webbridge 扩展已安装")
        sys.exit(1)

    # ── 4. 确保已登录 ──
    try:
        browser.ensure_logged_in()
    except Exception as e:
        logger.error(f"❌ 登录过程出错: {e}")
        browser.close()
        sys.exit(1)

    # ── 5. 搜索与投递循环 ──
    search_cfg = config["search"]
    app_cfg = config["application"]
    ai_cfg = config["ai"]
    threshold = ai_cfg.get("match_threshold", 60)

    # 统计
    stats = {"total": 0, "applied": 0, "skipped": 0, "failed": 0, "skipped_no_match": 0}
    timer = Timer()
    timer.start()

    keywords_to_try = [search_cfg["keyword"]] + search_cfg.get("secondary_keywords", [])

    try:
        current_page = 1
        max_pages = search_cfg.get("page_count", 5)
        kw_index = 0

        while current_page <= max_pages and kw_index < len(keywords_to_try):
            # 执行搜索
            found = browser.search_jobs(
                keyword=keywords_to_try[kw_index],
                city=search_cfg.get("city", ""),
            )

            if not found:
                kw_index += 1
                if kw_index < len(keywords_to_try):
                    logger.info(f"🔄 切换关键词: {keywords_to_try[kw_index]}")
                    current_page = 1
                continue

            kw_index = 0  # 搜索成功，重置关键词

            logger.info(f"\n📄 === 第 {current_page} 页 / 共 {max_pages} 页 ===")

            # 获取岗位 URL 列表
            time.sleep(3)
            job_urls = browser.get_job_urls()
            logger.info(f"   本页找到 {len(job_urls)} 个岗位")

            if not job_urls:
                logger.info("   本页无岗位，准备翻页")
                if browser.has_next_page():
                    browser.go_next_page()
                    current_page += 1
                else:
                    break
                continue

            # 遍历每个岗位
            for idx, job_url in enumerate(job_urls):
                # 检查投递上限
                if stats["applied"] >= app_cfg.get("daily_limit", 50):
                    logger.info(f"\n📊 已达到每日投递上限（{app_cfg['daily_limit']}个），停止投递")
                    break

                # 检查每小时上限，打满 10 个暂停一次
                if stats["applied"] > 0 and stats["applied"] % 10 == 0:
                    pause_min = app_cfg.get("apply_10_pause_minutes", 3)
                    human_pause(pause_min * 60, logger,
                                f"已投递 {stats['applied']} 个，暂停 {pause_min} 分钟")

                stats["total"] += 1
                print()  # 空行分隔
                logger.info(f"── [{stats['total']}] 处理第 {idx+1}/{len(job_urls)} 个岗位 ──")

                # ⭐ v3.1 黑名单检查（打开详情前）：命中直接跳过，不读 JD、不调 AI
                bl_cfg = blacklist.blacklist_config(config)
                bl_rec = None
                if bl_cfg["enabled"]:
                    skip_bl, bl_rec = blacklist.check_skip(job_url)
                    if skip_bl:
                        stats["skipped"] += 1
                        logger.info(f"  🚫 黑名单跳过（{bl_rec.get('reason', '低质岗位')}）")
                        random_delay(2, 4)
                        continue

                # 打开岗位详情（直接导航到详情页）
                if not browser.open_job_detail(job_url):
                    stats["failed"] += 1
                    save_result("errors", {
                        "reason": "打开详情页失败",
                        "page": current_page,
                        "url": job_url,
                    })
                    browser.back_to_search()
                    continue

                # 检测验证码
                if browser.has_captcha():
                    browser.handle_captcha()

                # 检查是否已投递
                if browser.has_applied():
                    logger.info("  📋 该岗位已投递过，跳过")
                    stats["skipped"] += 1
                    browser.back_to_search()
                    continue

                # 提取 JD 文本
                jd_info = browser.extract_jd_text()
                jd_full_text = browser.full_jd_text(jd_info)

                if not jd_full_text.strip():
                    logger.info("  ⚠️ 无法获取 JD 内容，跳过")
                    stats["failed"] += 1
                    save_result("errors", {
                        "job_title": jd_info.get("title", "未知"),
                        "reason": "JD 内容为空",
                    })
                    browser.back_to_search()
                    continue

                # ⭐ v3.1 劳务外包关键词预检（零 AI 成本）：命中直接拉黑跳过，省一次 AI 调用
                if bl_cfg["enabled"] and blacklist.match_outsourcing(
                        jd_full_text, bl_cfg["outsourcing_keywords"]):
                    blacklist.classify(job_url, jd_info, jd_full_text,
                                       {"job_quality": {"is_outsourcing": True}},
                                       0, config, existing=bl_rec)
                    stats["skipped"] += 1
                    logger.info(f"  🚫 劳务外包岗位已拉黑: {jd_info.get('title', '未知')}")
                    browser.back_to_search()
                    random_delay(2, 4)
                    continue

                # AI 分析匹配度（评分规则来自 config.json 的 scoring 节，可自定义）
                if analyzer.is_ready():
                    decision = analyzer.analyze(
                        jd_text=jd_full_text,
                        resume_text=resume_text,
                        threshold=threshold,
                        scoring_rules=build_scoring_rules(config),
                    )
                else:
                    decision = {"match_score": 0, "should_apply": False,
                                "reason": "AI 未配置", "key_matches": [], "key_gaps": []}

                # ⭐ v3.0 评分分级：超预期（≥over_threshold）/ 符合预期（≥threshold）/ 不符合
                score = decision.get("match_score", 0)
                over_threshold = int(config.get("ai", {}).get("over_match_threshold", 80) or 80)
                if score >= over_threshold:
                    match_level = "超预期"
                elif score >= threshold:
                    match_level = "符合预期"
                else:
                    match_level = "不符合"

                # ⭐ v3.1 黑名单自动判定：外包/名不副实→永久，远超简历→短期，低分→观察/升级
                bl_act = blacklist.classify(job_url, jd_info, jd_full_text, decision, score,
                                            config, existing=bl_rec) if bl_cfg["enabled"] else None
                if bl_act:
                    level_tag = "⛔ 永久" if bl_act["level"] == "permanent" else \
                                "🕐 短期" if bl_act["level"] == "temporary" else "👀 观察"
                    logger.info(f"  🚫 低质岗位已拉黑（{bl_act['reason']}）[{level_tag}]")
                    if bl_act.get("should_skip"):
                        stats["skipped"] += 1
                        browser.back_to_search()
                        random_delay(2, 4)
                        continue

                # ⭐ v3.0 符合预期 + 超预期：保存详细信息（含 BOSS 网址/公司规模/是否上市）
                if match_level in ("超预期", "符合预期"):
                    detail_record = {
                        "job_title": jd_info.get("title", ""),
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
                    save_result("matched", detail_record)
                    logger.info(f"  📌 {match_level}岗位（{score}分）: {jd_info.get('title','')} "
                                f"| {jd_info.get('company','-')} | 规模:{jd_info.get('company_size','-')} "
                                f"| 上市:{jd_info.get('financing','-')}")
                    logger.info(f"     🔗 {job_url}")
                    # 超预期额外收藏到数据库
                    if match_level == "超预期":
                        try:
                            database.save_starred_job({**detail_record, "jd_text": jd_full_text})
                            logger.info(f"  ⭐ 超预期岗位已收藏（可在控制面板「⭐ 超预期岗位」查看）")
                        except Exception as e:
                            logger.warning(f"  ⚠️ 收藏超预期岗位失败: {e}")

                # 根据决策操作
                if decision.get("should_apply", False):
                    logger.info("  🚀 尝试投递...")

                    if browser.click_apply():
                        browser.send_greeting()
                        browser.back_to_search()
                        stats["applied"] += 1
                        save_result("applied", {
                            "job_title": jd_info.get("title", ""),
                            "company": jd_info.get("company", ""),
                            "company_size": jd_info.get("company_size", ""),
                            "financing": jd_info.get("financing", ""),
                            "listed": jd_info.get("listed"),
                            "salary": jd_info.get("salary", ""),
                            "match_score": decision.get("match_score", 0),
                            "reason": decision.get("reason", ""),
                            "url": job_url,  # ⭐ v3.0 保存 BOSS 网址供回看
                            "page": current_page,
                        })
                        logger.info(f"  ✅ 投递成功！({stats['applied']}/{app_cfg.get('daily_limit', 50)})")
                    else:
                        stats["failed"] += 1
                        browser.back_to_search()
                        save_result("errors", {
                            "job_title": jd_info.get("title", ""),
                            "reason": "点击立即沟通失败",
                        })
                else:
                    stats["skipped"] += 1
                    if decision.get("match_score", 0) > 0:
                        stats["skipped_no_match"] += 1
                    save_result("skipped", {
                        "job_title": jd_info.get("title", ""),
                        "company": jd_info.get("company", ""),
                        "company_size": jd_info.get("company_size", ""),
                        "financing": jd_info.get("financing", ""),
                        "listed": jd_info.get("listed"),
                        "salary": jd_info.get("salary", ""),
                        "match_score": decision.get("match_score", 0),
                        "reason": decision.get("reason", ""),
                        "key_matches": decision.get("key_matches", []),
                        "key_gaps": decision.get("key_gaps", []),
                        "url": job_url,  # ⭐ v3.0 保存 BOSS 网址供回看
                    })
                    browser.back_to_search()

                # 每个岗位之间随机延时
                delay_range = app_cfg.get("delay_range_seconds", [3, 8])
                random_delay(delay_range[0], delay_range[1])

            # 检查是否达到每日上限
            if stats["applied"] >= app_cfg.get("daily_limit", 50):
                break

            # 翻到下一页
            if browser.has_next_page():
                browser.go_next_page()
                current_page += 1
                random_delay(3, 6)
            else:
                logger.info("📋 没有更多页面了")
                break

    except KeyboardInterrupt:
        logger.info("\n🛑 用户中断运行")
    except Exception as e:
        logger.error(f"❌ 运行异常: {e}", exc_info=True)
    finally:
        # ── 6. 生成报告 ──
        stats["elapsed"] = timer.elapsed()
        print_statistics(stats)

        # ── 7. 关闭浏览器 ──
        browser.close()
        logger.info("👋 脚本结束，感谢使用！")


if __name__ == "__main__":
    main()
