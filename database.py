"""
数据库模块

使用 SQLite 存储「超预期岗位」历史记录 与「低质量岗位黑名单」，供控制面板新页面（/starred、/blacklist）查看。
数据库文件位于 data/boss_jobs.db，均按岗位网址去重（重复记录自动更新）。
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config import ROOT_DIR

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
        conn.commit()
    finally:
        conn.close()


def save_starred_job(job: dict):
    """保存/更新一个超预期岗位（按 url 去重，重复收藏更新分数与 JD，保留首次收藏时间）"""
    conn = _get_conn()
    try:
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
            job.get("job_type", ""), job.get("url", ""),
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
            job.get("url", ""), job.get("reason", ""),
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
