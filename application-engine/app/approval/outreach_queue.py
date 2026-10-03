"""Approval queue for outreach drafts (§9): cover letters and hiring-manager
emails, each showing the contact's details, the source URL where they were
found, and the email confidence score — same approve/reject/edit-then-
approve pattern as the resume approval queue in queue.py.
"""

import sqlite3
from datetime import datetime, timezone

from app.db import log_event

PENDING_STATUS = "DRAFT"
APPROVED_STATUS = "APPROVED"
REJECTED_STATUS = "REJECTED"
NEEDS_HUMAN_STATUS = "NEEDS_HUMAN"

_SELECT = """
    SELECT o.*, j.company, j.title, j.url AS job_url,
           c.id AS contact_id, c.name AS contact_name, c.role AS contact_role, c.email AS contact_email,
           c.email_confidence, c.linkedin_url, c.source_url AS contact_source_url,
           c.is_alumni, c.alumni_school, c.alumni_search_url, c.alumni_name_hint, c.alumni_source
    FROM outreach o
    JOIN jobs j ON j.id = o.job_id
    LEFT JOIN contacts c ON c.id = o.contact_id
"""


def list_pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"{_SELECT} WHERE o.status = ? ORDER BY o.updated_at DESC", (PENDING_STATUS,)).fetchall()
    return [dict(r) for r in rows]


def list_needs_human(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"{_SELECT} WHERE o.status = ? ORDER BY o.updated_at DESC", (NEEDS_HUMAN_STATUS,)).fetchall()
    return [dict(r) for r in rows]


def list_approved(conn: sqlite3.Connection) -> list[dict]:
    """Approved but not yet sent — the Send button still needs one more
    explicit click per item (§1 rule 1), so these sit here until then."""
    rows = conn.execute(f"{_SELECT} WHERE o.status = ? ORDER BY o.approved_at DESC", (APPROVED_STATUS,)).fetchall()
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, outreach_id: int) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE o.id = ?", (outreach_id,)).fetchone()
    return dict(row) if row else None


def approve(conn: sqlite3.Connection, outreach_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE outreach SET status = ?, approved_at = ?, updated_at = ? WHERE id = ?",
        (APPROVED_STATUS, now, now, outreach_id),
    )
    item = get_item(conn, outreach_id)
    log_event(conn, "outreach_approved", job_id=item["job_id"] if item else None)
    conn.commit()


def reject(conn: sqlite3.Connection, outreach_id: int, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE outreach SET status = ?, rejection_reason = ?, updated_at = ? WHERE id = ?",
        (REJECTED_STATUS, reason, now, outreach_id),
    )
    item = get_item(conn, outreach_id)
    log_event(conn, "outreach_rejected", payload=reason, job_id=item["job_id"] if item else None)
    conn.commit()


def edit_then_approve(conn: sqlite3.Connection, outreach_id: int, subject: str, body: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE outreach SET subject = ?, body = ?, status = ?, approved_at = ?, updated_at = ? WHERE id = ?",
        (subject, body, APPROVED_STATUS, now, now, outreach_id),
    )
    item = get_item(conn, outreach_id)
    log_event(conn, "outreach_edited_and_approved", job_id=item["job_id"] if item else None)
    conn.commit()
