"""§9 orchestration: draft cover letter + hiring manager email for a job,
running each through the free-text truth validator (retry once, then
NEEDS_HUMAN — same pattern as the Rewriter in §6.5) before landing in the
approval queue. Every draft carries the contact's details, the source URL,
and the email confidence score, per spec.
"""

import json
import sqlite3
from datetime import datetime, timezone

from app.db import log_event
from app.models import MasterResume
from app.outreach.cover_letter import fetch_company_grounding, generate_cover_letter
from app.outreach.hiring_manager_email import generate_hiring_manager_email
from app.rewriter.truth_validator import run_free_text_truth_validator

MAX_RETRIES = 1


def _guess_domain(company: str) -> str:
    import re

    return f"{re.sub(r'[^a-z0-9]', '', company.lower())}.com"


def _validate_and_persist(
    resume: MasterResume,
    job: dict,
    kind: str,
    subject: str,
    body: str,
    contact_id: int | None,
    conn: sqlite3.Connection,
    *,
    regenerate_fn,
) -> dict:
    validation = run_free_text_truth_validator(resume, kind, body, conn, job_id=job["id"])
    attempts = 1
    validator_log = [validation]

    while not validation["valid"] and attempts <= MAX_RETRIES:
        log_event(conn, "outreach_truth_validator_rejected", payload=json.dumps(validation["violations"]), job_id=job["id"])
        subject, body = regenerate_fn(validation["violations"])
        validation = run_free_text_truth_validator(resume, kind, body, conn, job_id=job["id"])
        validator_log.append(validation)
        attempts += 1

    now = datetime.now(timezone.utc).isoformat()
    status = "NEEDS_HUMAN" if not validation["valid"] else "DRAFT"

    existing = conn.execute(
        "SELECT id FROM outreach WHERE job_id = ? AND kind = ?", (job["id"], kind)
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO outreach (job_id, contact_id, subject, body, status, kind, validator_log, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job["id"], contact_id, subject, body, status, kind, json.dumps(validator_log), now, now),
        )
        outreach_id = cursor.lastrowid
    else:
        outreach_id = existing["id"]
        conn.execute(
            "UPDATE outreach SET contact_id = ?, subject = ?, body = ?, status = ?, validator_log = ?, updated_at = ? WHERE id = ?",
            (contact_id, subject, body, status, json.dumps(validator_log), now, outreach_id),
        )

    if not validation["valid"]:
        log_event(conn, "outreach_needs_human", payload=json.dumps(validation["violations"]), job_id=job["id"])
    conn.commit()
    return {"outreach_id": outreach_id, "status": status, "attempts": attempts, "subject": subject, "body": body}


def draft_cover_letter(resume: MasterResume, job: dict, contact_id: int | None, conn: sqlite3.Connection) -> dict:
    grounding = fetch_company_grounding(_guess_domain(job["company"]))

    def _generate():
        result = generate_cover_letter(resume, job, conn, grounding=grounding)
        return f"Cover letter — {job['title']} at {job['company']}", result["letter_text"]

    subject, body = _generate()

    def regenerate(violations):
        # Truth-validator violations aren't fed back into the cover letter
        # prompt in this build slice (the Rewriter's retry does this for
        # resume bullets) — a second attempt just regenerates fresh; if it
        # still fails, escalates to NEEDS_HUMAN like everything else.
        return _generate()

    return _validate_and_persist(resume, job, "cover_letter", subject, body, contact_id, conn, regenerate_fn=regenerate)


def draft_hiring_manager_email(
    resume: MasterResume,
    job: dict,
    application_ref: str,
    contact_id: int | None,
    contact_name: str | None,
    conn: sqlite3.Connection,
    *,
    is_alumni: bool = False,
    alumni_school: str | None = None,
) -> dict:
    def _generate():
        result = generate_hiring_manager_email(
            resume, job, application_ref, conn, contact_name=contact_name,
            is_alumni=is_alumni, alumni_school=alumni_school,
        )
        return result["subject"], result["body"]

    subject, body = _generate()

    def regenerate(violations):
        return _generate()

    return _validate_and_persist(
        resume, job, "hiring_manager_email", subject, body, contact_id, conn, regenerate_fn=regenerate
    )
