"""持久化的投递记录与额度检查。"""

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from config import ROOT_DIR
import database
from job_identity import job_key
from utils import save_result


logger = logging.getLogger(__name__)


class ApplicationLedger:
    def __init__(self):
        self.results_dir = Path(ROOT_DIR) / "results"
        self.state_path = self.results_dir / "submission_state.json"
        self._sent_keys = {
            job_key(item.get("url"))
            for path in self.results_dir.glob("applied_*.json")
            for item in self._read(path) if item.get("url")
        }

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def was_sent(self, url):
        return bool(url) and job_key(url) in self._sent_keys

    def recent_times(self, now=None):
        now = now or datetime.now()
        times = []
        for day in (now.date(), (now - timedelta(days=1)).date()):
            path = self.results_dir / f"applied_{day.isoformat()}.json"
            for item in self._read(path):
                try:
                    times.append(datetime.fromisoformat(item["timestamp"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return times

    def quota(self, daily_limit, max_per_hour, now=None):
        """返回 (可投递, 原因, 下次可尝试时间)。"""
        now = now or datetime.now()
        times = self.recent_times(now)
        today = sum(t.date() == now.date() for t in times)
        if today >= daily_limit:
            return False, f"今日已达到 {daily_limit} 个沟通上限", None
        hour_times = sorted(t for t in times if now - timedelta(hours=1) <= t <= now)
        if len(hour_times) >= max_per_hour:
            return False, f"最近一小时已达到 {max_per_hour} 个沟通上限", hour_times[0] + timedelta(hours=1)
        return True, "", None

    def record_sent(self, data):
        if self.was_sent(data.get("url")):
            return False
        save_result("applied", dict(data))
        self._sent_keys.add(job_key(data.get("url")))
        try:
            database.record_history_contact(data)
        except sqlite3.Error:
            logger.exception("SQLite 历史记录写入失败；下次启动会从 JSON 恢复")
        self.mark_phase(data, "contact_sent")
        self.mark_phase(data, "workflow_enabled")
        return True

    def _state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, state):
        self.results_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def phase(self, url, name):
        return bool(self._state().get(url, {}).get(name))

    def mark_phase(self, record, name):
        url = record.get("url", "")
        if not url:
            return
        state = self._state()
        item = state.setdefault(url, {})
        item.update({k: v for k, v in record.items() if k in
                     ("url", "job_title", "company", "greeting")})
        item[name] = datetime.now().isoformat(timespec="seconds")
        self._write_state(state)
        try:
            database.record_history_event(url, name, item[name])
        except sqlite3.Error:
            logger.exception("SQLite 动作记录写入失败；下次启动会从 JSON 恢复")

    def record_followup_status(self, record, status, detail):
        """Save why a contacted job is still waiting for its PDF."""
        url = record.get("url", "")
        if not url:
            return
        checked_at = datetime.now().isoformat(timespec="seconds")
        state = self._state()
        item = state.setdefault(url, {})
        item.update({k: v for k, v in record.items() if k in
                     ("url", "job_title", "company")})
        item.update({"followup_status": status,
                     "followup_detail": str(detail or "")[:300],
                     "followup_checked_at": checked_at})
        self._write_state(state)
        try:
            database.record_history_followup(url, status, item["followup_detail"], checked_at)
        except sqlite3.Error:
            logger.exception("SQLite 后续状态写入失败；下次启动会从 JSON 恢复")

    def pending(self):
        """Jobs contacted by this version and still missing introduction or PDF."""
        state = self._state()
        out = []
        for path in self.results_dir.glob("applied_*.json"):
            for record in self._read(path):
                url = record.get("url", "")
                phases = state.get(url, {})
                if url and phases.get("workflow_enabled") and not (
                        phases.get("greeting_sent") and phases.get("resume_sent")):
                    out.append({**record, **phases})
        return out

    def summary(self):
        records = [v for v in self._state().values() if v.get("workflow_enabled")]
        contacted = sum(len(self._read(path)) for path in self.results_dir.glob("applied_*.json"))
        return {
            "contacted": contacted,
            "greetings_sent": sum(bool(v.get("greeting_sent")) for v in records),
            "resumes_sent": sum(bool(v.get("resume_sent")) for v in records),
            "pending_resumes": sum(bool(v.get("greeting_sent")) and not v.get("resume_sent")
                                   for v in records),
            "pending_workflows": sum(not (v.get("greeting_sent") and v.get("resume_sent"))
                                     for v in records),
            "needs_attention": sum(v.get("followup_status") in ("failed", "unconfirmed")
                                   and not v.get("resume_sent") for v in records),
        }

    def daily_report(self, day: date):
        """Read the persistent SQLite date report."""
        return database.history_report(day)
