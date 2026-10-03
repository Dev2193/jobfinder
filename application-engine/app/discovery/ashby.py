"""Ashby public job board API client (§4 priority 1 — free, structured,
unblocked). Verified live against real company boards before writing this
(Ramp, OpenAI, Notion, Linear all return real postings)."""

import re
from datetime import datetime, timezone

from app.discovery.client_utils import fetch_json

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_postings(company_slug: str) -> list[dict]:
    """Fetch raw postings for one Ashby job board slug. Returns [] on any
    failure (unknown slug, network error) rather than raising."""
    url = BASE_URL.format(slug=company_slug)
    data = fetch_json(url)
    if not data or "jobs" not in data:
        return []
    return data["jobs"]


def normalize(raw_job: dict, company_slug: str) -> dict:
    published_at = raw_job.get("publishedAt")
    posted_at = None
    if published_at:
        try:
            posted_at = datetime.fromisoformat(published_at.replace("Z", "+00:00")).isoformat()
        except ValueError:
            posted_at = published_at

    return {
        "source": "ashby",
        "url": raw_job.get("jobUrl", ""),
        "company": company_slug,
        "title": (raw_job.get("title") or "").strip(),
        "location": raw_job.get("location", ""),
        "work_mode": raw_job.get("workplaceType"),
        "employment_type": raw_job.get("employmentType"),
        "description_raw": _strip_html(raw_job.get("descriptionHtml", "")),
        "posted_at": posted_at,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw_job,
    }
