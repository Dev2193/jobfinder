"""Lever Postings API client (§4 priority 1 — free, structured, unblocked)."""

from datetime import datetime, timezone

from app.discovery.client_utils import fetch_json

BASE_URL = "https://api.lever.co/v0/postings/{slug}"


def fetch_postings(company_slug: str) -> list[dict]:
    """Fetch raw postings for one Lever site slug. Returns [] on any failure."""
    url = BASE_URL.format(slug=company_slug) + "?mode=json"
    data = fetch_json(url)
    if not isinstance(data, list):
        return []
    return data


def normalize(raw_job: dict, company_slug: str) -> dict:
    categories = raw_job.get("categories") or {}
    created_at_ms = raw_job.get("createdAt")
    posted_at = None
    if created_at_ms:
        posted_at = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc).isoformat()
    description = raw_job.get("descriptionPlain") or raw_job.get("description") or ""
    return {
        "source": "lever",
        "url": raw_job.get("hostedUrl", ""),
        "company": company_slug,
        "title": raw_job.get("text", ""),
        "location": categories.get("location", ""),
        "work_mode": None,
        "employment_type": categories.get("commitment"),
        "description_raw": description,
        "posted_at": posted_at,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw_job,
    }
