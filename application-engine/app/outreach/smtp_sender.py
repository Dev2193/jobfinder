"""§9: send approved outreach via the candidate's own SMTP credentials from
a .env file — the engine reads them from the environment and never logs or
displays them (§9's own requirement). Only ever sends outreach rows already
APPROVED by a human; nothing here can send a DRAFT or NEEDS_HUMAN item.
"""

import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

from dotenv import load_dotenv

from app.db import log_event

load_dotenv()

APPROVED_STATUS = "APPROVED"
SENT_STATUS = "SENT"


class SMTPNotConfigured(Exception):
    pass


def _smtp_credentials() -> dict:
    host = os.environ.get("SMTP_HOST")
    port = os.environ.get("SMTP_PORT")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not all([host, port, user, password]):
        raise SMTPNotConfigured(
            "SMTP_HOST, SMTP_PORT, SMTP_USER, and SMTP_PASSWORD must all be set in .env before sending."
        )
    return {"host": host, "port": int(port), "user": user, "password": password}


def send_outreach(conn: sqlite3.Connection, outreach_id: int) -> dict:
    """Send exactly one APPROVED outreach item. Never sends anything else —
    refuses outright if the row isn't APPROVED, or has no resolved contact
    email, or credentials aren't configured. Credentials are read from the
    environment and never appear in any return value, log, or event payload."""
    row = conn.execute(
        """
        SELECT o.*, c.email AS contact_email, j.company, j.title
        FROM outreach o
        JOIN jobs j ON j.id = o.job_id
        LEFT JOIN contacts c ON c.id = o.contact_id
        WHERE o.id = ?
        """,
        (outreach_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No outreach row with id {outreach_id}")
    row = dict(row)

    if row["status"] != APPROVED_STATUS:
        raise RuntimeError(f"Outreach {outreach_id} is not APPROVED (status: {row['status']}) — refusing to send.")
    if not row.get("contact_email"):
        raise RuntimeError(f"Outreach {outreach_id} has no resolved contact email — refusing to send.")

    creds = _smtp_credentials()

    message = EmailMessage()
    message["Subject"] = row["subject"]
    message["From"] = creds["user"]
    message["To"] = row["contact_email"]
    message.set_content(row["body"])
    # §9: "No attachments in the first email." Never attach a file here.

    with smtplib.SMTP(creds["host"], creds["port"], timeout=15) as smtp:
        smtp.starttls()
        smtp.login(creds["user"], creds["password"])
        smtp.send_message(message)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE outreach SET status = ?, sent_at = ?, updated_at = ? WHERE id = ?", (SENT_STATUS, now, now, outreach_id))
    log_event(conn, "outreach_sent", job_id=row["job_id"])
    conn.commit()
    return {"outreach_id": outreach_id, "status": SENT_STATUS, "sent_at": now}
