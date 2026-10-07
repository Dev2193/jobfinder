"""Exports submitted applications for manual import into the Shippy /
HeyClicky job dashboard — a separate local tool (a static HTML file with no
server, storing its own data in that browser tab's local storage). That
file already has real, independently-synced data in it, so this module
never touches it directly; it only writes application-engine's own export
file. A button added to the dashboard reads that file and merges it in —
see the "Import from Application Engine" button in that HTML.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

EXPORT_PATH = Path(__file__).resolve().parent.parent / "data" / "output" / "shippy_export.json"

# The dashboard file itself, so a pipeline run can cross-check against
# companies already tracked there (e.g. applied to via Gmail/manually) and
# skip filling out a duplicate application. Best-effort: this file lives
# outside the project and its exact location can vary by machine, so a
# missing/unreadable file just means "nothing to cross-check", not an error.
DASHBOARD_HTML_PATH = (
    Path.home() / "Library/Application Support/Clicky/projects/agents/shippy/output/job-dashboard.html"
)


def load_shippy_tracked_companies(html_path: Path = DASHBOARD_HTML_PATH) -> set[str]:
    """Best-effort extraction of every company name baked into the
    dashboard's job records (its seed data + whatever's been synced in).
    This only sees what's embedded in the file itself, not anything added
    later purely in the browser's local storage — a real limitation, since
    that file isn't updated when someone adds a job by hand in the browser."""
    if not html_path.exists():
        return set()
    try:
        html = html_path.read_text()
    except OSError:
        return set()
    return set(re.findall(r"company:\s*'([^']+)'", html))

_COLOR_PALETTE = ["#4d6bfe", "#1d9c72", "#cb4e52", "#7656d8", "#2c65f5", "#bc7911", "#111111", "#0b7285"]


def _color_for(company: str) -> str:
    return _COLOR_PALETTE[sum(ord(c) for c in company) % len(_COLOR_PALETTE)]


def record_submission(job: dict, application_ref: str | None) -> None:
    """Append (or update, if already recorded) one submitted application in
    Shippy's own job-record schema. Safe to call repeatedly — upserts by a
    stable id, so re-running never duplicates an entry."""
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if EXPORT_PATH.exists():
        try:
            existing = json.loads(EXPORT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []

    entry_id = f"ae-{job['id']}"
    company = job.get("company") or "Unknown"
    entry = {
        "id": entry_id,
        "company": company,
        "role": job.get("title") or "Role to review",
        "location": job.get("location") or "Location not set",
        "status": "applied",
        "round": "",
        "date": "",
        "updated": datetime.now(timezone.utc).strftime("%b %d"),
        "score": job.get("fit_score") or 82,
        "initials": company[:1].upper(),
        "color": _color_for(company),
        "requirements": [],
        "tips": [],
        "notes": f"Application ref: {application_ref}" if application_ref else "Submitted by Application Engine.",
        "url": job.get("url") or "",
        "source": "application-engine",
        "sourceUrl": job.get("url") or "",
    }

    existing = [e for e in existing if e.get("id") != entry_id]
    existing.append(entry)
    EXPORT_PATH.write_text(json.dumps(existing, indent=2))
