"""持久化的投递记录与额度检查。"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from config import ROOT_DIR
from utils import save_result


class ApplicationLedger:
    def __init__(self):
        self.results_dir = Path(ROOT_DIR) / "results"
        self.state_path = self.results_dir / "submission_state.json"

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def was_sent(self, url):
        if not url:
            return False
        for path in self.results_dir.glob("applied_*.json"):
            if any(item.get("url") == url for item in self._read(path)):
                return True
        return False

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
        self.mark_phase(data, "contact_sent")
        self.mark_phase(data, "workflow_enabled")
        return True

    def _state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

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
        self.results_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

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
        }

    def daily_report(self, day: date):
        """Build a date report from durable contact and workflow records.

        Contact counts use the day the BOSS conversation was confirmed. Message
        and PDF counts use their own confirmed send dates, which can be later.
        """
        day_text = day.isoformat()
        applied = self._read(self.results_dir / f"applied_{day_text}.json")
        matched = self._read(self.results_dir / f"matched_{day_text}.json")
        state = self._state()

        def happened_on(value):
            return isinstance(value, str) and value[:10] == day_text

        phases = [item for item in state.values() if isinstance(item, dict)]
        rows = []
        seen_urls = set()
        for record in applied:
            if not isinstance(record, dict):
                continue
            url = record.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            progress = state.get(url, {})
            if not isinstance(progress, dict):
                progress = {}
            rows.append({
                "url": url,
                "job_title": record.get("job_title", ""),
                "company": record.get("company", ""),
                "company_size": record.get("company_size", ""),
                "city": record.get("city", ""),
                "salary": record.get("salary", ""),
                "match_score": record.get("match_score"),
                "contact_at": record.get("timestamp") or progress.get("contact_sent", ""),
                "greeting_at": progress.get("greeting_sent", ""),
                "resume_at": progress.get("resume_sent", ""),
                "workflow_recorded": bool(progress.get("workflow_enabled")),
            })
        rows.sort(key=lambda item: item["contact_at"] or "", reverse=True)

        events = [{
            "type": "contact",
            "time": item["contact_at"],
            "url": item["url"],
            "job_title": item["job_title"],
            "company": item["company"],
        } for item in rows]
        for url, progress in state.items():
            if not isinstance(progress, dict):
                continue
            for phase, event_type in (("greeting_sent", "greeting"),
                                      ("resume_sent", "resume")):
                if happened_on(progress.get(phase)):
                    events.append({
                        "type": event_type,
                        "time": progress[phase],
                        "url": url,
                        "job_title": progress.get("job_title", ""),
                        "company": progress.get("company", ""),
                    })
        events.sort(key=lambda item: item["time"] or "", reverse=True)

        return {
            "date": day_text,
            "totals": {
                "matched": len(matched),
                "contacted": len(rows),
                "greetings_sent": sum(happened_on(item.get("greeting_sent")) for item in phases),
                "resumes_sent": sum(happened_on(item.get("resume_sent")) for item in phases),
                "pending_resumes_from_contacts": sum(
                    bool(item["greeting_at"]) and not item["resume_at"] for item in rows
                ),
            },
            "events": events,
            "records": rows,
        }
