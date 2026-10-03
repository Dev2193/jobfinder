"""SmartRecruiters public Postings API client (§4 priority 1). Verified live
against a real company board before writing this.

Unlike Greenhouse/Lever/Ashby, SmartRecruiters' list endpoint doesn't
include the full job description OR a human-facing posting URL — both only
appear in a second, per-posting detail call, and the posting URL is
required (jobs.url is NOT NULL UNIQUE, and Stage 5 needs a real page to
open). So every posting needs its detail fetched, not just an enrichment
subset; the cap below bounds total requests per company to stay polite
(§1 rule 6) and keep a single discovery run tractable.
"""

import re
import time
from datetime import datetime, timezone

from app.discovery.client_utils import fetch_json

LIST_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
PAGE_SIZE = 100
MAX_POSTINGS_PER_COMPANY = 100
DETAIL_FETCH_DELAY_SECONDS = 0.2


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _fetch_list(company_slug: str) -> list[dict]:
    postings = []
    offset = 0
    while offset < MAX_POSTINGS_PER_COMPANY:
        url = f"{LIST_URL.format(slug=company_slug)}?limit={PAGE_SIZE}&offset={offset}"
        data = fetch_json(url)
        if not data or "content" not in data:
            break
        content = data["content"]
        postings.extend(content)
        total_found = data.get("totalFound", len(content))
        offset += PAGE_SIZE
        if offset >= total_found or not content:
            break
    return postings


def _fetch_detail(company_slug: str, posting_id: str) -> dict | None:
    url = DETAIL_URL.format(slug=company_slug, posting_id=posting_id)
    return fetch_json(url)


def fetch_postings(company_slug: str) -> list[dict]:
    """Fetch postings for one SmartRecruiters company slug, with each one's
    detail (description + real posting URL) enriched via a second call.
    Returns [] on any failure rather than raising. A summary whose detail
    fetch fails is skipped entirely — with no postingUrl it can't satisfy
    jobs.url's NOT NULL UNIQUE constraint, and a broken URL would silently
    fail Stage 5's form-filling later."""
    summaries = _fetch_list(company_slug)
    if not summaries:
        return []

    enriched = []
    for summary in summaries:
        detail = _fetch_detail(company_slug, summary["id"])
        time.sleep(DETAIL_FETCH_DELAY_SECONDS)
        if detail and detail.get("postingUrl"):
            enriched.append(detail)
    return enriched


def normalize(raw_job: dict, company_slug: str) -> dict:
    location = raw_job.get("location") or {}
    location_str = location.get("fullLocation", "")
    work_mode = "Remote" if location.get("remote") else ("Hybrid" if location.get("hybrid") else None)

    description_raw = ""
    job_ad = raw_job.get("jobAd")
    if job_ad and "sections" in job_ad:
        parts = [
            job_ad["sections"].get("jobDescription", {}).get("text", ""),
            job_ad["sections"].get("qualifications", {}).get("text", ""),
        ]
        description_raw = _strip_html(" ".join(p for p in parts if p))

    released_date = raw_job.get("releasedDate")
    posted_at = None
    if released_date:
        try:
            posted_at = datetime.fromisoformat(released_date.replace("Z", "+00:00")).isoformat()
        except ValueError:
            posted_at = released_date

    employment = raw_job.get("typeOfEmployment") or {}

    return {
        "source": "smartrecruiters",
        "url": raw_job.get("postingUrl", ""),
        "company": company_slug,
        "title": raw_job.get("name", ""),
        "location": location_str,
        "work_mode": work_mode,
        "employment_type": employment.get("label"),
        "description_raw": description_raw,
        "posted_at": posted_at,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw_job,
    }
