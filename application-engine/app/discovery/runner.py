"""Stage 2 orchestration (§4): poll every company in the universe file, write
raw postings to disk before parsing, and insert new ones into `jobs` as
DISCOVERED. Meant to run on a schedule (cron/launchd), not on demand.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from app.db import log_event
from app.discovery import ashby, greenhouse, lever, smartrecruiters
from app.discovery.company_universe import CompanyEntry, load_company_universe
from app.scoring.dedupe import normalize_company, normalize_location, normalize_title

RAW_POSTINGS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "raw_postings"

SOURCE_CLIENTS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
}

# Be polite between company polls even though these are sanctioned public APIs (§1 rule 6).
DELAY_BETWEEN_COMPANIES_SECONDS = 0.5


def _write_raw(source: str, slug: str, raw_jobs: list[dict]) -> Path:
    RAW_POSTINGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_POSTINGS_DIR / f"{source}_{slug}_{ts}.json"
    path.write_text(json.dumps(raw_jobs, indent=2))
    return path


def _insert_job(conn: sqlite3.Connection, normalized: dict) -> bool:
    """Insert one normalized posting as DISCOVERED. Returns True if inserted,
    False if a job with this URL already exists (source-level dedupe)."""
    existing = conn.execute("SELECT id FROM jobs WHERE url = ?", (normalized["url"],)).fetchone()
    if existing or not normalized["url"]:
        return False
    conn.execute(
        """
        INSERT INTO jobs (
            source, url, company, title, location, work_mode, employment_type,
            salary_min, salary_max, currency, posted_at, discovered_at,
            description_raw, content_hash, company_norm, title_norm, location_norm,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISCOVERED')
        """,
        (
            normalized["source"],
            normalized["url"],
            normalized["company"],
            normalized["title"],
            normalized.get("location"),
            normalized.get("work_mode"),
            normalized.get("employment_type"),
            normalized.get("salary_min"),
            normalized.get("salary_max"),
            normalized.get("currency"),
            normalized.get("posted_at"),
            normalized["discovered_at"],
            normalized.get("description_raw", ""),
            "",  # content_hash is filled in during scoring (§5), not discovery
            normalize_company(normalized["company"]),
            normalize_title(normalized["title"]),
            normalize_location(normalized.get("location")),
        ),
    )
    return True


def run_discovery(conn: sqlite3.Connection, universe_path: Path | None = None) -> dict:
    entries = load_company_universe(universe_path) if universe_path else load_company_universe()
    summary = {"companies_polled": 0, "postings_seen": 0, "jobs_inserted": 0, "errors": []}

    for entry in entries:
        client = SOURCE_CLIENTS.get(entry.ats)
        if client is None:
            summary["errors"].append(f"unknown ATS '{entry.ats}' for slug '{entry.slug}'")
            continue

        try:
            raw_jobs = client.fetch_postings(entry.slug)
        except Exception as exc:  # a single dead/misconfigured slug must not kill the run
            summary["errors"].append(f"{entry.ats}:{entry.slug} -> {exc}")
            continue

        summary["companies_polled"] += 1
        summary["postings_seen"] += len(raw_jobs)

        if raw_jobs:
            _write_raw(entry.ats, entry.slug, raw_jobs)

        for raw_job in raw_jobs:
            normalized = client.normalize(raw_job, entry.slug)
            if _insert_job(conn, normalized):
                summary["jobs_inserted"] += 1

        log_event(
            conn,
            "discovery_poll",
            payload=json.dumps({"ats": entry.ats, "slug": entry.slug, "postings_seen": len(raw_jobs)}),
        )
        conn.commit()
        time.sleep(DELAY_BETWEEN_COMPANIES_SECONDS)

    return summary
