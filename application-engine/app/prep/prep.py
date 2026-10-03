"""§7 Stage 5 orchestration — opens the real application URL, fills every
field it can confidently and truthfully answer, screenshots the result, and
stops. Never clicks submit. A CAPTCHA/SSO wall/unmapped required field
routes the job to NEEDS_HUMAN with a direct link and a note about what's
missing, exactly per §1 rule 3 and §7.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.answers_bank import AnswersBank
from app.db import log_event
from app.models import MasterResume
from app.prep.application_ref import scrape_application_ref
from app.prep.form_filler import detect_bot_wall, fill_application
from app.shippy_export import record_submission

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "output"


def _safe_dirname(name: str) -> str:

    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "untitled"


def _upsert_application(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    resume_path: str | None,
    status: str,
    screenshot_path: str | None,
    blocked_reason: str | None,
    unmapped_fields: list[str],
    filled_fields: list[str],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute("SELECT id FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO applications
                (job_id, resume_path, status, screenshot_path, blocked_reason,
                 unmapped_fields, filled_fields, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, resume_path, status, screenshot_path, blocked_reason,
                json.dumps(unmapped_fields), json.dumps(filled_fields), now, now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE applications SET
                resume_path = ?, status = ?, screenshot_path = ?, blocked_reason = ?,
                unmapped_fields = ?, filled_fields = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (
                resume_path, status, screenshot_path, blocked_reason,
                json.dumps(unmapped_fields), json.dumps(filled_fields), now, job_id,
            ),
        )
    conn.commit()


def prep_job(
    job: dict,
    resume: MasterResume,
    answers_bank: AnswersBank,
    docx_path: Path | None,
    conn: sqlite3.Connection,
    *,
    headless: bool = True,
) -> dict:
    """Fill the real application form for one job and stop. Returns a
    summary dict. Never clicks submit — that is a separate action reserved
    for the approval UI, at the candidate's own later initiative."""
    job_id = job["id"]
    company, title = job["company"], job["title"]
    job_dir = SCREENSHOT_DIR / f"{_safe_dirname(company)}_{_safe_dirname(title)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = job_dir / "application_screenshot.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            bot_wall_reason = detect_bot_wall(page)
            if bot_wall_reason:
                page.screenshot(path=str(screenshot_path))
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                _upsert_application(
                    conn, job_id, resume_path=str(docx_path) if docx_path else None,
                    status="NEEDS_HUMAN", screenshot_path=str(screenshot_path),
                    blocked_reason=bot_wall_reason, unmapped_fields=[], filled_fields=[],
                )
                log_event(conn, "prep_blocked_bot_wall", payload=bot_wall_reason, job_id=job_id)
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": bot_wall_reason}

            result = fill_application(page, resume, answers_bank, docx_path)
            page.screenshot(path=str(screenshot_path), full_page=True)

            if result.get("no_form_detected"):
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                reason = (
                    "Could not find an application form on this page — the posting URL may lead to a "
                    "search/listing page rather than the specific job's apply form. Open the posting "
                    "manually to check."
                )
                _upsert_application(
                    conn, job_id, resume_path=str(docx_path) if docx_path else None,
                    status="NEEDS_HUMAN", screenshot_path=str(screenshot_path),
                    blocked_reason=reason, unmapped_fields=[], filled_fields=[],
                )
                log_event(conn, "prep_no_form_detected", payload=reason, job_id=job_id)
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": reason}

            if result["unmapped_required"]:
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                reason = f"Unmapped required fields: {result['unmapped_required']}"
                _upsert_application(
                    conn, job_id, resume_path=str(docx_path) if docx_path else None,
                    status="NEEDS_HUMAN", screenshot_path=str(screenshot_path),
                    blocked_reason=reason, unmapped_fields=result["unmapped_required"],
                    filled_fields=result["filled"],
                )
                log_event(conn, "prep_needs_human", payload=reason, job_id=job_id)
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": reason, "result": result}

            conn.execute("UPDATE jobs SET status = 'PREPPED' WHERE id = ?", (job_id,))
            _upsert_application(
                conn, job_id, resume_path=str(docx_path) if docx_path else None,
                status="PREPPED", screenshot_path=str(screenshot_path),
                blocked_reason=None, unmapped_fields=[], filled_fields=result["filled"],
            )
            log_event(conn, "job_prepped", job_id=job_id)
            return {"job_id": job_id, "status": "PREPPED", "result": result}
        finally:
            browser.close()


def submit_application(
    job: dict,
    resume: MasterResume,
    answers_bank: AnswersBank,
    docx_path: Path | None,
    conn: sqlite3.Connection,
    *,
    headless: bool = True,
) -> dict:
    """The ONLY function in this codebase that ever clicks a real submit
    button. §1 rule 1: "I click; the engine clicks. Never the reverse." This
    must only ever run because a human clicked the single bulk "Release"
    action first (/ready-to-submit in the web UI, or `engine release` on the
    CLI), on a job whose application is already PREPPED (i.e. prep_job
    already filled it with zero unmapped required fields). Re-fills the form
    fresh (the original browser session from prep isn't kept alive) before
    submitting, then scrapes the confirmation page for an application
    reference."""
    job_id = job["id"]
    application = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if application is None or application["status"] != "PREPPED":
        raise RuntimeError(
            f"Job {job_id} is not in PREPPED state (found: "
            f"{application['status'] if application else 'no application row'}) — refusing to submit."
        )

    company, title = job["company"], job["title"]
    job_dir = SCREENSHOT_DIR / f"{_safe_dirname(company)}_{_safe_dirname(title)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    confirmation_screenshot = job_dir / "confirmation_screenshot.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            bot_wall_reason = detect_bot_wall(page)
            if bot_wall_reason:
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                log_event(conn, "submit_blocked_bot_wall", payload=bot_wall_reason, job_id=job_id)
                conn.commit()
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": bot_wall_reason}

            result = fill_application(page, resume, answers_bank, docx_path)
            if result.get("no_form_detected"):
                reason = "Could not find an application form on re-fill (page changed since prep, or a transient load issue)."
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                log_event(conn, "submit_blocked_no_form", payload=reason, job_id=job_id)
                conn.commit()
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": reason}

            if result["unmapped_required"]:
                reason = f"Unmapped required fields on re-fill: {result['unmapped_required']}"
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                log_event(conn, "submit_blocked_unmapped", payload=reason, job_id=job_id)
                conn.commit()
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": reason}

            submit_button = page.get_by_role("button", name=re.compile("submit application", re.I))
            if submit_button.count() == 0:
                reason = "Could not locate the submit button"
                conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
                log_event(conn, "submit_blocked_no_button", payload=reason, job_id=job_id)
                conn.commit()
                return {"job_id": job_id, "status": "NEEDS_HUMAN", "reason": reason}

            submit_button.first.click()
            page.wait_for_timeout(3000)
            page.screenshot(path=str(confirmation_screenshot), full_page=True)

            confirmation_text = page.inner_text("body")
            application_ref = scrape_application_ref(confirmation_text, page.url)
            if not application_ref:
                # Fall back to the requisition number embedded in the original
                # posting URL (e.g. Greenhouse's gh_jid= query param), per §7.
                match = re.search(r"gh_jid=(\d+)", job.get("url", ""))
                application_ref = match.group(1) if match else None

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE applications SET status = 'SUBMITTED', application_ref = ?, submitted_at = ?, "
                "screenshot_path = ?, updated_at = ? WHERE job_id = ?",
                (application_ref, now, str(confirmation_screenshot), now, job_id),
            )
            conn.execute("UPDATE jobs SET status = 'SUBMITTED' WHERE id = ?", (job_id,))
            log_event(conn, "job_submitted", payload=application_ref or "", job_id=job_id)
            conn.commit()
            record_submission(job, application_ref)
            return {"job_id": job_id, "status": "SUBMITTED", "application_ref": application_ref}
        finally:
            browser.close()
