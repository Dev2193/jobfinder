"""§11b — auto-populate interview context from the DB rather than asking.
Grounds the mock interview in the tailored resume actually prepared for that
job (not the master resume), so the interviewer's questions and scoring
reflect what the company actually received.
"""

import json
import re
import sqlite3

from app.outreach.cover_letter import fetch_company_grounding

MAX_COMPANY_GROUNDING_CHARS = 1500


def _guess_domain(company: str) -> str:
    return f"{re.sub(r'[^a-z0-9]', '', company.lower())}.com"


def build_interview_context(job: dict, conn: sqlite3.Connection) -> dict:
    """Returns {target_role, seniority, company_type_hint, tailored_entries,
    candidate_name}. tailored_entries is the actual rewritten resume content
    for this job if it exists (preferred — "what the company received"),
    falling back to the master resume's experience section only if this job
    was never tailored (e.g. testing before Stage 4 ran)."""
    seniority = "unspecified"
    if job.get("role_family_id"):
        family = conn.execute(
            "SELECT seniority FROM role_families WHERE id = ?", (job["role_family_id"],)
        ).fetchone()
        if family and family["seniority"]:
            seniority = family["seniority"]

    tailored = conn.execute(
        "SELECT rewritten_bullets FROM tailored_resumes WHERE job_id = ?", (job["id"],)
    ).fetchone()
    tailored_entries = json.loads(tailored["rewritten_bullets"]) if tailored else None

    company_type_hint = fetch_company_grounding(_guess_domain(job["company"]))
    if company_type_hint:
        company_type_hint = company_type_hint[:MAX_COMPANY_GROUNDING_CHARS]

    return {
        "target_role": job["title"],
        "seniority": seniority,
        "company": job["company"],
        "company_type_hint": company_type_hint,
        "job_description": job.get("description_raw", ""),
        "tailored_entries": tailored_entries,
    }
