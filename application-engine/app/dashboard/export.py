"""§11 full CSV export — one row per job, joined with its application and
outreach state, so the candidate can answer "what did I send this company
and when" outside the app too.
"""

import csv
import io
import sqlite3

COLUMNS = [
    "job_id", "company", "title", "source", "url", "fit_score", "status",
    "application_status", "application_ref", "submitted_at",
    "outreach_kind", "outreach_status", "outreach_sent_at",
]


def export_csv(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT
            j.id AS job_id, j.company, j.title, j.source, j.url, j.fit_score, j.status,
            a.status AS application_status, a.application_ref, a.submitted_at,
            o.kind AS outreach_kind, o.status AS outreach_status, o.sent_at AS outreach_sent_at
        FROM jobs j
        LEFT JOIN applications a ON a.job_id = j.id
        LEFT JOIN outreach o ON o.job_id = j.id AND o.kind = 'hiring_manager_email'
        ORDER BY j.id
        """
    ).fetchall()

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row[col] for col in COLUMNS})
    return buffer.getvalue()
