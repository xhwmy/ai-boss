"""Stable identity for BOSS job links whose query parameters change over time."""

import re
from urllib.parse import urlsplit, urlunsplit


_JOB_ID = re.compile(r"^/job_detail/([A-Za-z0-9_-]+)\.html$")


def job_id_from_url(url: str) -> str:
    path = urlsplit(str(url or "")).path
    match = _JOB_ID.fullmatch(path)
    return match.group(1) if match else ""


def job_key(url: str) -> str:
    """Keep the navigation URL intact, but compare jobs by their stable ID."""
    job_id = job_id_from_url(url)
    if job_id:
        return f"boss:{job_id}"
    parts = urlsplit(str(url or ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
