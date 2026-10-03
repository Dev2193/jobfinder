"""Greenhouse Job Board API client (§4 priority 1 — free, structured, unblocked)."""

import re
from datetime import datetime, timezone

from app.discovery.client_utils import fetch_json

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_postings(company_slug: str) -> list[dict]:
    """Fetch raw postings for one Greenhouse board slug. Returns [] on any
    failure (unknown slug, network error) rather than raising."""
    url = BASE_URL.format(slug=company_slug) + "?content=true"
    data = fetch_json(url)
    if not data or "jobs" not in data:
        return []
    return data["jobs"]


def normalize(raw_job: dict, company_slug: str) -> dict:
    location = (raw_job.get("location") or {}).get("name", "")
    updated_at = raw_job.get("updated_at")
    posted_at = None
    if updated_at:
        try:
            posted_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).isoformat()
        except ValueError:
            posted_at = updated_at
    return {
        "source": "greenhouse",
        "url": raw_job.get("absolute_url", ""),
        "company": company_slug,
        "title": raw_job.get("title", ""),
        "location": location,
        "work_mode": None,
        "employment_type": None,
        "description_raw": _strip_html(raw_job.get("content", "")),
        "posted_at": posted_at,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw_job,
    }
