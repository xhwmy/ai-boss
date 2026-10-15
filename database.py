"""
数据库模块

使用 SQLite 存储投递历史、超预期岗位与低质量岗位黑名单。
数据库文件位于 data/boss_jobs.db；按岗位网址去重。
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from config import ROOT_DIR
from job_identity import job_id_from_url, job_key

DB_PATH = ROOT_DIR / "data" / "boss_jobs.db"


def _get_conn() -> sqlite3.Connection:
    """建立数据库连接（自动创建 data/ 目录）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """建表（幂等，可重复调用）"""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS starred_jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title    TEXT NOT NULL DEFAULT '',   -- 岗位名称
                company      TEXT NOT NULL DEFAULT '',   -- 公司名称
                company_size TEXT NOT NULL DEFAULT '',   -- 公司规模（如 100-499人）
                financing    TEXT NOT NULL DEFAULT '',   -- 融资/上市阶段（如 已上市 / C轮 / 未融资）
                listed       INTEGER,                    -- 是否上市 1=是 0=否 NULL=未知
                industry     TEXT NOT NULL DEFAULT '',   -- 所属行业
                salary       TEXT NOT NULL DEFAULT '',
                city         TEXT NOT NULL DEFAULT '',
                education    TEXT NOT NULL DEFAULT '',
                experience   TEXT NOT NULL DEFAULT '',
                job_type     TEXT NOT NULL DEFAULT '',
                url          TEXT UNIQUE,                -- BOSS 岗位网址
                match_score  INTEGER NOT NULL DEFAULT 0,
                reason       TEXT NOT NULL DEFAULT '',
                jd_text      TEXT NOT NULL DEFAULT '',   -- 完整 JD 文本（供回看）
                created_at   TEXT NOT NULL DEFAULT ''    -- 收藏时间
            )
        """)
        # v3.1 低质量岗位拉黑名单（三级：永久/短期/观察）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blacklist_jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title    TEXT NOT NULL DEFAULT '',   -- 岗位名称
                company      TEXT NOT NULL DEFAULT '',   -- 公司名称
                url          TEXT UNIQUE,                -- BOSS 岗位网址（去重键）
                reason       TEXT NOT NULL DEFAULT '',   -- 拉黑原因
                level        TEXT NOT NULL DEFAULT 'watch',  -- permanent 永久 / temporary 短期 / watch 观察
                source       TEXT NOT NULL DEFAULT 'auto',   -- auto 自动 / manual 人工
                hit_count    INTEGER NOT NULL DEFAULT 0, -- watch 命中次数（观察升级用）
                duration_days INTEGER,                   -- 短期时长（天），非短期为 NULL
                expires_at   TEXT NOT NULL DEFAULT '',   -- 短期到期时间（temporary 才有）
                match_score  INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT ''    -- 拉黑时间
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history_contacts (
                url TEXT PRIMARY KEY,
                job_title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                company_size TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                salary TEXT NOT NULL DEFAULT '',
                match_score INTEGER,
                contact_at TEXT NOT NULL
            )
        """)
        contact_columns = {row[1] for row in conn.execute("PRAGMA table_info(history_contacts)")}
        for name in ("followup_status", "followup_detail", "followup_checked_at"):
            if name not in contact_columns:
                conn.execute(f"ALTER TABLE history_contacts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history_events (
                url TEXT NOT NULL,
                event_type TEXT NOT NULL,
                happened_at TEXT NOT NULL,
                PRIMARY KEY (url, event_type)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history_matches (
                day TEXT NOT NULL,
                url TEXT NOT NULL,
                PRIMARY KEY (day, url)
            )
        """)
        # Older runs stored the full URL, including changing securityId values.
        # This table only counts matches, so its keys can be migrated safely.
        for day, url in conn.execute("SELECT day, url FROM history_matches").fetchall():
            key = job_key(url)
            if key != url:
                conn.execute("INSERT OR IGNORE INTO history_matches (day, url) VALUES (?, ?)",
                             (day, key))
                conn.execute("DELETE FROM history_matches WHERE day=? AND url=?", (day, url))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_contact_date ON history_contacts(contact_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_event_date ON history_events(happened_at)")
        conn.commit()
    finally:
        conn.close()


def record_history_contact(record):
    """Persist a confirmed BOSS contact. Reimporting JSON does not duplicate it."""
    url = str(record.get("url") or "").strip()
    if not url:
        return
    contact_at = str(record.get("timestamp") or datetime.now().isoformat(timespec="seconds"))
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO history_contacts
                (url, job_title, company, company_size, city, salary, match_score, contact_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                job_title=excluded.job_title, company=excluded.company,
                company_size=excluded.company_size, city=excluded.city,
                salary=excluded.salary, match_score=excluded.match_score
        """, (url, record.get("job_title") or "", record.get("company") or "",
              record.get("company_size") or "", record.get("city") or "",
              record.get("salary") or "", record.get("match_score"), contact_at))


def record_history_event(url, event_type, happened_at):
    if not url or not happened_at:
        return
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT OR IGNORE INTO history_events (url, event_type, happened_at)
            VALUES (?, ?, ?)
        """, (url, event_type, happened_at))


def record_history_followup(url, status, detail, checked_at):
    if not url:
        return
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            UPDATE history_contacts
            SET followup_status=?, followup_detail=?, followup_checked_at=?
            WHERE url=?
        """, (status, detail, checked_at, url))


def record_history_match(record):
    url = job_key(str(record.get("url") or "").strip())
    stamp = str(record.get("timestamp") or datetime.now().isoformat(timespec="seconds"))
    if not url:
        return
    with closing(_get_conn()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO history_matches (day, url) VALUES (?, ?)",
                     (stamp[:10], url))


def import_legacy_history():
    """Backfill older JSON results and phase state; safe to run after every restart."""
    results = ROOT_DIR / "results"
    if not results.exists():
        return
    for prefix, writer in (("applied", record_history_contact),
                           ("matched", record_history_match)):
        for path in results.glob(f"{prefix}_*.json"):
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict):
                        if not record.get("timestamp"):
                            record = {**record, "timestamp": path.stem[-10:]}
                        writer(record)
    state_path = results / "submission_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if isinstance(state, dict):
        for url, phases in state.items():
            if isinstance(phases, dict):
                for name in ("contact_sent", "workflow_enabled", "greeting_sent", "resume_sent"):
                    if phases.get(name):
                        record_history_event(url, name, phases[name])
                if phases.get("followup_checked_at"):
                    record_history_followup(
                        url, phases.get("followup_status", ""),
                        phases.get("followup_detail", ""), phases["followup_checked_at"])


def history_dates():
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("""
            SELECT dates.day,
                   (SELECT COUNT(*) FROM history_contacts c
                    WHERE substr(c.contact_at,1,10)=dates.day) AS contacted
            FROM (
                SELECT substr(contact_at,1,10) AS day FROM history_contacts
                UNION SELECT substr(happened_at,1,10) FROM history_events
                UNION SELECT day FROM history_matches
            ) dates
            WHERE dates.day != ''
            ORDER BY dates.day DESC
        """).fetchall()
        return [dict(row) for row in rows]


def history_report(day):
    day_text = day.isoformat()
    with closing(_get_conn()) as conn, conn:
        matches = conn.execute("SELECT COUNT(*) FROM history_matches WHERE day=?", (day_text,)).fetchone()[0]
        contacts = [dict(row) for row in conn.execute("""
            SELECT c.*, g.happened_at AS greeting_at, r.happened_at AS resume_at,
                   w.happened_at AS workflow_at
            FROM history_contacts c
            LEFT JOIN history_events g ON g.url=c.url AND g.event_type='greeting_sent'
            LEFT JOIN history_events r ON r.url=c.url AND r.event_type='resume_sent'
            LEFT JOIN history_events w ON w.url=c.url AND w.event_type='workflow_enabled'
            WHERE substr(c.contact_at,1,10)=? ORDER BY c.contact_at DESC
        """, (day_text,)).fetchall()]
        event_rows = [dict(row) for row in conn.execute("""
            SELECT e.url, e.event_type, e.happened_at,
                   COALESCE(c.job_title, '') AS job_title,
                   COALESCE(c.company, '') AS company
            FROM history_events e LEFT JOIN history_contacts c ON c.url=e.url
            WHERE substr(e.happened_at,1,10)=?
              AND e.event_type IN ('greeting_sent','resume_sent')
            ORDER BY e.happened_at DESC
        """, (day_text,)).fetchall()]
    records = [{
        "url": row["url"], "job_title": row["job_title"],
        "company": row["company"], "company_size": row["company_size"],
        "city": row["city"], "salary": row["salary"],
        "match_score": row["match_score"], "contact_at": row["contact_at"],
        "greeting_at": row["greeting_at"] or "", "resume_at": row["resume_at"] or "",
        "followup_status": row["followup_status"],
        "followup_detail": row["followup_detail"],
        "followup_checked_at": row["followup_checked_at"],
        "workflow_recorded": bool(row["workflow_at"]),
    } for row in contacts]
    events = [{
        "type": "contact", "time": row["contact_at"], "url": row["url"],
        "job_title": row["job_title"], "company": row["company"],
    } for row in contacts]
    events.extend({
        "type": "greeting" if row["event_type"] == "greeting_sent" else "resume",
        "time": row["happened_at"], "url": row["url"],
        "job_title": row["job_title"], "company": row["company"],
    } for row in event_rows)
    events.sort(key=lambda item: item["time"], reverse=True)
    return {
        "date": day_text,
        "totals": {
            "matched": matches, "contacted": len(contacts),
            "greetings_sent": sum(row["event_type"] == "greeting_sent" for row in event_rows),
            "resumes_sent": sum(row["event_type"] == "resume_sent" for row in event_rows),
            "pending_resumes_from_contacts": sum(bool(row["greeting_at"]) and not row["resume_at"] for row in contacts),
            "followups_need_attention": sum(row["followup_status"] in ("failed", "unconfirmed")
                                             and not row["resume_at"] for row in contacts),
        },
        "events": events, "records": records,
    }


def save_starred_job(job: dict):
    """保存/更新一个超预期岗位（按 url 去重，重复收藏更新分数与 JD，保留首次收藏时间）"""
    conn = _get_conn()
    try:
        url = job.get("url", "")
        job_id = job_id_from_url(url)
        if job_id:
            existing = conn.execute(
                "SELECT url FROM starred_jobs WHERE instr(url, ?) > 0 ORDER BY id LIMIT 1",
                (f"/job_detail/{job_id}.html",),
            ).fetchone()
            if existing:
                url = existing["url"]
        conn.execute("""
            INSERT INTO starred_jobs
                (job_title, company, company_size, financing, listed, industry,
                 salary, city, education, experience, job_type, url,
                 match_score, reason, jd_text, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                job_title    = excluded.job_title,
                company      = excluded.company,
                company_size = excluded.company_size,
                financing    = excluded.financing,
                listed       = excluded.listed,
                industry     = excluded.industry,
                salary       = excluded.salary,
                city         = excluded.city,
                education    = excluded.education,
                experience   = excluded.experience,
                job_type     = excluded.job_type,
                match_score  = excluded.match_score,
                reason       = excluded.reason,
                jd_text      = excluded.jd_text
        """, (
            job.get("job_title", ""), job.get("company", ""),
            job.get("company_size", ""), job.get("financing", ""),
            job.get("listed"), job.get("industry", ""),
            job.get("salary", ""), job.get("city", ""),
            job.get("education", ""), job.get("experience", ""),
            job.get("job_type", ""), url,
            int(job.get("match_score", 0) or 0), job.get("reason", ""),
            job.get("jd_text", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()
    finally:
        conn.close()


def get_starred_jobs(limit: int = 300) -> list:
    """获取全部超预期岗位（新→旧）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM starred_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_starred_job(job_id: int) -> bool:
    """删除一条超预期岗位记录"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM starred_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_starred_jobs() -> int:
    """清空全部超预期岗位记录，返回删除条数"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM starred_jobs")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ==================== 低质量岗位拉黑名单（v3.1） ====================


def save_blacklist_job(job: dict):
    """保存/更新一个黑名单岗位（按 url 去重，重复拉黑更新原因与等级）"""
    conn = _get_conn()
    try:
        url = job.get("url", "")
        job_id = job_id_from_url(url)
        if job_id:
            existing = conn.execute(
                "SELECT url FROM blacklist_jobs WHERE instr(url, ?) > 0 ORDER BY id LIMIT 1",
                (f"/job_detail/{job_id}.html",),
            ).fetchone()
            if existing:
                url = existing["url"]
        conn.execute("""
            INSERT INTO blacklist_jobs
                (job_title, company, url, reason, level, source, hit_count,
                 duration_days, expires_at, match_score, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                job_title    = excluded.job_title,
                company      = excluded.company,
                reason       = excluded.reason,
                level        = excluded.level,
                source       = excluded.source,
                hit_count    = excluded.hit_count,
                duration_days = excluded.duration_days,
                expires_at   = excluded.expires_at,
                match_score  = excluded.match_score
        """, (
            job.get("job_title", ""), job.get("company", ""),
            url, job.get("reason", ""),
            job.get("level", "watch"), job.get("source", "auto"),
            int(job.get("hit_count", 0) or 0),
            job.get("duration_days"),
            job.get("expires_at", ""),
            int(job.get("match_score", 0) or 0),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()
    finally:
        conn.close()


def get_blacklist_jobs(limit: int = 300) -> list:
    """获取全部黑名单岗位（新→旧）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM blacklist_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_blacklist_by_url(url: str) -> dict | None:
    """按网址查黑名单记录（返回任何等级，含已过期/watch，供跳过判断与升级使用）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM blacklist_jobs WHERE url = ?", (url,)
        ).fetchone()
        if not row:
            job_id = job_id_from_url(url)
            if job_id:
                row = conn.execute(
                    "SELECT * FROM blacklist_jobs WHERE instr(url, ?) > 0 ORDER BY id DESC LIMIT 1",
                    (f"/job_detail/{job_id}.html",),
                ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_blacklist_job(job_id: int) -> bool:
    """删除一条黑名单记录（忽略/解除）"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM blacklist_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_blacklist_jobs() -> int:
    """清空全部黑名单记录，返回删除条数"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM blacklist_jobs")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_blacklist_level(job_id: int, level: str, duration_days=None) -> bool:
    """更新黑名单等级（控制面板一键转永久/短期），返回是否更新成功"""
    conn = _get_conn()
    try:
        expires_at = ""
        if level == "temporary" and duration_days:
            expires_at = (datetime.now() + timedelta(days=int(duration_days))).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        cur = conn.execute(
            "UPDATE blacklist_jobs SET level=?, duration_days=?, expires_at=?, hit_count=0 WHERE id=?",
            (level, duration_days, expires_at, job_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
