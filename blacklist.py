"""
低质量岗位拉黑名单（v3.1）

三级模型：permanent 永久 / temporary 短期 / watch 观察。
供 boss_engine 与 main 共用，集中黑名单判定策略，避免重复。

判定来源：
- 劳务外包：关键词规则（零 AI 成本）+ AI 标记（is_outsourcing）兜底 → 永久
- 岗位名与 JD 不符：AI 标记（title_jd_consistent=false）→ 永久
- JD 要求远超简历：AI 标记（jd_far_exceeds_resume=true）→ 短期（到期自动解除）
- 评分过低：先进观察（watch），再次命中仍低分 → 升级永久（防误杀）
"""

import re
from datetime import datetime, timedelta

import database

DEFAULT_OUTSOURCING_KEYWORDS = [
    "劳务派遣", "劳务外包", "外包", "驻场", "外派",
    "人力外包", "人力派遣", "业务外包", "劳务",
]


def blacklist_config(config: dict) -> dict:
    """读取黑名单配置节（缺失时给默认值）"""
    bl = config.get("blacklist", {}) or {}
    return {
        "enabled": bool(bl.get("enabled", True)),
        "auto_threshold": int(bl.get("auto_threshold", 30) or 0),
        "temp_days": int(bl.get("temp_days", 30) or 30),
        "outsourcing_keywords": bl.get("outsourcing_keywords") or DEFAULT_OUTSOURCING_KEYWORDS,
    }


def match_outsourcing(text: str, keywords=None) -> bool:
    """Find positive outsourcing mentions without treating simple negations as proof."""
    if not text:
        return False
    kws = keywords or DEFAULT_OUTSOURCING_KEYWORDS
    for keyword in kws:
        if not keyword:
            continue
        for hit in re.finditer(re.escape(keyword), text):
            prefix = text[max(0, hit.start() - 8):hit.start()]
            if re.search(r"(?:非|不是|并非|不属于|无需|不需|无须|不接受|拒绝|不涉及)\s*$", prefix):
                continue
            return True
    return False


def check_skip(url: str):
    """打开岗位详情前调用。返回 (是否跳过, 记录)。

    - permanent            → (True, rec) 永远跳过
    - temporary 未过期     → (True, rec)
    - temporary 已过期     → 删除记录自动解除 → (False, None)
    - watch                → (False, rec) 不跳过，rec 供后续升级判断
    - 无记录               → (False, None)
    """
    rec = database.get_blacklist_by_url(url)
    if not rec:
        return False, None
    if rec["level"] == "permanent":
        return True, rec
    if rec["level"] == "temporary":
        exp = rec.get("expires_at") or ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if exp and exp > now:
            return True, rec
        # 已过期 → 自动解除，删除记录
        database.delete_blacklist_job(rec["id"])
        return False, None
    # watch → 不跳过
    return False, rec


def classify(job_url: str, jd_info: dict, jd_text: str, decision: dict,
             score: int, config: dict, existing=None) -> dict | None:
    """AI 判定 + 关键词 + 低分 → 决定等级并入库。

    existing: 之前 check_skip 返回的 watch 记录（用于低分二次命中升级）。
    返回 {"reason", "level", "should_skip"} 或 None（未拉黑）。
    注意：在 AI 分析前只做外包关键词预检时，可传
    decision={"job_quality": {"is_outsourcing": True}} 复用本函数入库。
    """
    bl = blacklist_config(config)
    if not bl["enabled"]:
        return None

    quality = decision.get("job_quality", {}) or {}
    is_out = bool(quality.get("is_outsourcing", False)) or match_outsourcing(
        jd_text, bl["outsourcing_keywords"]
    )
    title_bad = quality.get("title_jd_consistent", True) is False
    far_exceed = bool(quality.get("jd_far_exceeds_resume", False))
    auto_thr = bl["auto_threshold"]

    reason = level = None
    duration_days = None
    if is_out:
        reason, level = "劳务外包/派遣", "permanent"
    elif title_bad:
        reason, level = "岗位名与JD不符", "permanent"
    elif far_exceed:
        reason, level = "JD要求远超简历（短期）", "temporary"
        duration_days = bl["temp_days"]
    elif auto_thr > 0 and int(score or 0) < auto_thr:
        # 低分：观察期。watch 再次命中 → 升级永久
        if existing and existing.get("level") == "watch":
            reason, level = "低分观察后再次命中（评分过低）", "permanent"
        else:
            reason, level = "评分过低（观察中）", "watch"
    else:
        # 未命中任何条件：若之前在观察，说明已恢复达标 → 解除观察
        if existing and existing.get("level") == "watch":
            try:
                database.delete_blacklist_job(existing["id"])
            except Exception:
                pass
        return None

    hit_count = 1
    if existing and existing.get("level") == "watch":
        hit_count = int(existing.get("hit_count", 0) or 0) + 1

    expires_at = ""
    if level == "temporary" and duration_days:
        expires_at = (datetime.now() + timedelta(days=duration_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    record = {
        "job_title": jd_info.get("title", ""),
        "company": jd_info.get("company", ""),
        "url": existing.get("url", job_url) if existing else job_url,
        "reason": reason,
        "level": level,
        "source": "auto",
        "hit_count": hit_count,
        "duration_days": duration_days,
        "expires_at": expires_at,
        "match_score": int(score or 0),
    }
    try:
        database.save_blacklist_job(record)
    except Exception:
        return None
    return {"reason": reason, "level": level, "should_skip": level in ("permanent", "temporary")}


def is_blacklisted(url: str) -> bool:
    """便捷判断：该 URL 当前是否应跳过"""
    ok, _ = check_skip(url)
    return ok
