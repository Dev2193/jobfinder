"""§6.4 Metrics gap queue — the automation fix.

The Rewriter's native instruction is to ask the candidate for a missing
number rather than invent one. An unattended pipeline can't ask mid-run, so
gaps accumulate here instead, deduplicated by bullet — the same master-resume
bullet appears across every job that touches that experience entry, so this
is one question, not eighty. Answers are written back into the master resume
JSON so every future application inherits them permanently.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.models import MasterResume

MASTER_RESUME_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "master_resume.json"


def bullet_key(org: str, title: str, original_bullet: str) -> str:
    """Stable identifier for a master-resume bullet, independent of which job
    triggered the Rewriter pass that surfaced the gap."""
    raw = f"{org}::{title}::{original_bullet}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record_gap(
    conn: sqlite3.Connection,
    *,
    org: str,
    title: str,
    original_bullet: str,
    needs_metric: str,
    suggested_range: str | None,
    gap_type: str = "missing_metric",
) -> None:
    """Insert a gap if this bullet hasn't been flagged (or resolved) before.
    Silently no-ops on an existing row — the same bullet surfacing across
    many jobs' Rewriter passes should not create duplicate rows or re-open
    something the candidate already answered.

    gap_type distinguishes the Rewriter's original use (§6.4, a bullet with
    no available metric) from §11b's reuse of this same queue for
    interview-surfaced "resume covers this but buries it" suggestions."""
    key = bullet_key(org, title, original_bullet)
    existing = conn.execute("SELECT id FROM metrics_gaps WHERE bullet_key = ?", (key,)).fetchone()
    if existing is not None:
        return
    conn.execute(
        "INSERT INTO metrics_gaps (bullet_key, org, title, original_bullet, needs_metric, "
        "suggested_range, gap_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (key, org, title, original_bullet, needs_metric, suggested_range, gap_type, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def list_unresolved_gaps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM metrics_gaps WHERE resolved = 0 ORDER BY created_at").fetchall()


def resolve_gap(conn: sqlite3.Connection, gap_id: int, answer: str) -> None:
    """Mark a gap resolved and write the answer back into the master resume
    JSON by appending it parenthetically to the original bullet — a simple,
    deterministic edit (no second LLM call) that the candidate can refine
    further via the resume review UI's JSON editor if the phrasing needs work."""
    gap = conn.execute("SELECT * FROM metrics_gaps WHERE id = ?", (gap_id,)).fetchone()
    if gap is None:
        raise ValueError(f"No metrics_gaps row with id {gap_id}")

    conn.execute(
        "UPDATE metrics_gaps SET resolved = 1, answer = ?, resolved_at = ? WHERE id = ?",
        (answer, datetime.now(timezone.utc).isoformat(), gap_id),
    )
    conn.commit()

    if not MASTER_RESUME_PATH.exists():
        return
    resume = MasterResume.model_validate(json.loads(MASTER_RESUME_PATH.read_text()))
    for exp in resume.experience:
        if exp.org == gap["org"] and exp.title == gap["title"]:
            for i, bullet in enumerate(exp.bullets):
                if bullet == gap["original_bullet"]:
                    exp.bullets[i] = f"{bullet} ({answer})"
    MASTER_RESUME_PATH.write_text(json.dumps(resume.model_dump(), indent=2))
