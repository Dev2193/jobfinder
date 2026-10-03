"""Read-only visibility into tailoring outcomes. The human-approval gate
that used to sit between "tailored + validated" and "filled application
form" has been removed (per explicit request) — a validated tailored resume
now proceeds straight to prep via app.auto_pipeline. What's left here is
just NEEDS_HUMAN visibility: jobs where the Rewriter + truth-validator loop
rejected twice, which is a genuine safety escalation (§1's "never fabricate"
rule), not an approval step.
"""

import sqlite3

NEEDS_HUMAN_STATUS = "NEEDS_HUMAN"


def list_needs_human(conn: sqlite3.Connection) -> list[dict]:
    """Jobs where the Rewriter+truth-validator loop failed twice — these
    need a person to look at the violations and decide what to do."""
    rows = conn.execute(
        """
        SELECT tr.*, j.company, j.title, j.url, j.fit_score, j.location
        FROM tailored_resumes tr
        JOIN jobs j ON j.id = tr.job_id
        WHERE tr.status = ?
        ORDER BY tr.updated_at DESC
        """,
        (NEEDS_HUMAN_STATUS,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, tailored_resume_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT tr.*, j.company, j.title, j.url, j.fit_score, j.location, j.description_raw
        FROM tailored_resumes tr
        JOIN jobs j ON j.id = tr.job_id
        WHERE tr.id = ?
        """,
        (tailored_resume_id,),
    ).fetchone()
    return dict(row) if row else None
