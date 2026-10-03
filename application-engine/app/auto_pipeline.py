"""Auto-fill pipeline, with a per-application human checkpoint before
anything is actually submitted:
  - No resume rewriting/re-rendering. The original uploaded resume file
    (PDF/DOCX, exactly as uploaded) is what gets attached to every
    application; "tailoring" is the existing keyword-overlap fit score
    (app/scoring/fit_score.py) deciding which postings are worth applying
    to, not editing the file itself.
  - Running the pipeline discovers, scores, and fills out the real
    application form for every eligible job, stopping at PREPPED. Nothing
    is submitted at that point. Each filled application then waits in the
    progress report (GET /ready-to-submit) for an explicit per-item Submit
    or Discard click — Submit fires submit_one, Discard fires discard_one
    and that job is never auto-resubmitted.

Jobs that hit a genuine safety escalation — a bot-detection wall, an
unmapped required field the engine can't truthfully answer without
guessing — land in NEEDS_HUMAN, same as always; that is not the review
step described above, it's the non-negotiable "never guess, never solve
CAPTCHAs, never fabricate" rule from §1.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.answers_bank import AnswersBank, load_answers_bank
from app.config import SearchFilters, industry_text, target_role_text
from app.discovery.runner import run_discovery
from app.models import MasterResume
from app.prep.prep import prep_job, submit_application
from app.readiness import evaluate_readiness
from app.recruiter.recruiter import run_recruiter
from app.resume_parser import get_active_resume_file_path
from app.scoring.dedupe import normalize_company, title_token_overlap
from app.scoring.pipeline import run_dedupe_and_score
from app.scoring.role_family import assign_role_families

# Standing guardrails (per explicit request), applied on every run:
#  - never more than this many applications at any one company, counting
#    everything already on file (any status) plus what this run would add
#  - a job only qualifies to be filled if its title matches one of the
#    configured target roles, OR its fit score clears this floor — a job
#    that's neither an explicit target-role match nor a strong scorer
#    doesn't get auto-filled, no matter how much of the batch it would fill
MAX_APPLICATIONS_PER_COMPANY = 2
MIN_FIT_SCORE_WITHOUT_ROLE_MATCH = 75
TARGET_ROLE_OVERLAP_THRESHOLD = 0.3

# Broadened target-role matching: specific role-family phrases rather than
# loose word overlap. Bare "engineer" is deliberately excluded — it would
# also match Security Engineer, Solutions Architect, Design Engineer, etc.,
# which aren't AI/ML/software-adjacent just because they share that one
# generic word. Each pattern is a real, specific role family instead.
_TARGET_ROLE_FAMILY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bai\b", r"\bartificial intelligence\b",
        r"\bmachine learning\b", r"\bml\b",
        r"\bdata scien(ce|tist)\b",
        r"\bresearch (scientist|engineer|intern)\b", r"\bapplied (scientist|research)\b",
        r"\bsoftware (engineer|developer)\b", r"\bswe\b",
        r"\bnlp\b", r"\bcomputer vision\b",
    ]
]


def _matches_target_role(title: str, target_roles: list[str]) -> bool:
    if any(title_token_overlap(title, role) >= TARGET_ROLE_OVERLAP_THRESHOLD for role in target_roles if role):
        return True
    return any(p.search(title) for p in _TARGET_ROLE_FAMILY_PATTERNS)


# Role-category dedup (per explicit request): once one application has ever
# been sent in a category, no more get auto-filled in that category, across
# every company, forever — on top of, not instead of, the per-company cap.
# Deliberately narrower than _TARGET_ROLE_FAMILY_PATTERNS above (which also
# treats "software engineer" as AI-adjacent for the purpose of deciding
# whether a job is worth considering at all): a plain "Software Engineer
# Intern" isn't specifically an AI role, so it's left uncategorized here —
# only titles that are actually ABOUT AI/ML/research, or specifically about
# product management/design, are capped at one-ever. Add more categories
# here if new target roles are configured later.
_ROLE_CATEGORY_PATTERNS = {
    "AI": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bai\b", r"\bartificial intelligence\b",
            r"\bmachine learning\b", r"\bml\b",
            r"\bdata scien(ce|tist)\b",
            r"\bresearch (scientist|engineer|intern)\b", r"\bapplied (scientist|research)\b",
            r"\bnlp\b", r"\bcomputer vision\b",
        ]
    ],
    "PM_PD": [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bproduct manager\b", r"\bproduct management\b",
            r"\bproduct designer\b", r"\bproduct design\b",
        ]
    ],
}


def classify_role_category(title: str) -> str | None:
    for category, patterns in _ROLE_CATEGORY_PATTERNS.items():
        if any(p.search(title) for p in patterns):
            return category
    return None


# Both caps below only count PREPPED/SUBMITTED — a real, filled-out
# application either sitting ready to go or already sent. A NEEDS_HUMAN row
# is a failed automation attempt (nothing was ever filled or sent) and
# DISCARDED was explicitly rejected before it went anywhere — neither
# should burn a company's or category's limited slots.
_COUNTS_TOWARD_CAP = ("PREPPED", "SUBMITTED")


def _used_role_categories(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT j.title FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.status IN (?, ?)",
        _COUNTS_TOWARD_CAP,
    ).fetchall()
    used = set()
    for r in rows:
        category = classify_role_category(r["title"])
        if category:
            used.add(category)
    return used


def _company_application_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT j.company FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.status IN (?, ?)",
        _COUNTS_TOWARD_CAP,
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        key = normalize_company(r["company"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def run_auto_pipeline(
    conn: sqlite3.Connection,
    resume: MasterResume,
    cfg: SearchFilters,
    *,
    headless: bool = True,
    limit: int | None = None,
    exclude_companies: set[str] | None = None,
) -> dict:
    """Fill (never submit) every SCORED job that doesn't already have an
    application on file. This is the batch replacement for running
    `engine prep <id>` one job at a time.

    Two standing guardrails apply on every call, not just when explicitly
    asked for a batch — see MAX_APPLICATIONS_PER_COMPANY and
    MIN_FIT_SCORE_WITHOUT_ROLE_MATCH above: at most 2 applications per
    company ever, and a job must either match a configured target role by
    title or clear a 75+ fit score to qualify at all.

    limit, when given, caps how many jobs get filled this call (highest fit
    score first) — useful for a first small batch before committing to a
    long run against many real company sites.

    exclude_companies, when given, is a set of raw (not yet normalized)
    company names to skip entirely — e.g. companies already tracked in a
    separate job-search dashboard, to avoid duplicate applications."""
    answers_bank = load_answers_bank()
    resume_file_path = get_active_resume_file_path(conn)

    jobs = conn.execute(
        """
        SELECT j.* FROM jobs j
        LEFT JOIN applications a ON a.job_id = j.id
        WHERE j.status IN ('SCORED', 'TAILORED')
          AND (a.id IS NULL OR a.status NOT IN ('PREPPED', 'SUBMITTED', 'NEEDS_HUMAN', 'DISCARDED'))
        ORDER BY j.fit_score DESC
        """
    ).fetchall()
    jobs = [dict(j) for j in jobs]

    skipped_overlap = []
    if exclude_companies:
        excluded_norm = {normalize_company(c) for c in exclude_companies}
        kept = []
        for job in jobs:
            if normalize_company(job["company"]) in excluded_norm:
                skipped_overlap.append({"job_id": job["id"], "company": job["company"], "title": job["title"]})
            else:
                kept.append(job)
        jobs = kept

    skipped_below_threshold = []
    kept = []
    for job in jobs:
        if job["fit_score"] >= MIN_FIT_SCORE_WITHOUT_ROLE_MATCH or _matches_target_role(job["title"], cfg.target_role):
            kept.append(job)
        else:
            skipped_below_threshold.append({"job_id": job["id"], "company": job["company"], "title": job["title"], "fit_score": job["fit_score"]})
    jobs = kept

    skipped_company_cap = []
    company_counts = _company_application_counts(conn)
    kept = []
    for job in jobs:
        key = normalize_company(job["company"])
        if company_counts.get(key, 0) >= MAX_APPLICATIONS_PER_COMPANY:
            skipped_company_cap.append({"job_id": job["id"], "company": job["company"], "title": job["title"]})
        else:
            kept.append(job)
            company_counts[key] = company_counts.get(key, 0) + 1
    jobs = kept

    skipped_category_cap = []
    used_categories = _used_role_categories(conn)
    kept = []
    for job in jobs:
        category = classify_role_category(job["title"])
        if category and category in used_categories:
            skipped_category_cap.append({"job_id": job["id"], "company": job["company"], "title": job["title"], "category": category})
        else:
            kept.append(job)
            if category:
                used_categories.add(category)
    jobs = kept

    if limit is not None:
        jobs = jobs[:limit]

    summary = {
        "prepped": [], "needs_human": [],
        "skipped_overlap": skipped_overlap,
        "skipped_below_threshold": skipped_below_threshold,
        "skipped_company_cap": skipped_company_cap,
        "skipped_category_cap": skipped_category_cap,
    }
    for job in jobs:
        result = prep_job(job, resume, answers_bank, resume_file_path, conn, headless=headless)
        entry = {"job_id": job["id"], "company": job["company"], "title": job["title"], "location": job.get("location")}
        if result.get("status") == "PREPPED":
            summary["prepped"].append(entry)
        else:
            summary["needs_human"].append({**entry, "reason": result.get("reason") or "could not complete"})

    return summary


def run_full_pipeline(
    conn: sqlite3.Connection,
    resume: MasterResume,
    cfg: SearchFilters,
    *,
    headless: bool = True,
    limit: int | None = None,
    exclude_companies: set[str] | None = None,
) -> dict:
    """Discover -> dedupe/score -> fill -> cluster -> Recruiter, in that
    order. Stops at PREPPED — nothing here submits anything. Shared by both
    `engine auto-run` and the web UI's POST /run so the two surfaces can't
    drift out of sync. limit/exclude_companies are forwarded to
    run_auto_pipeline — see its docstring.

    Fill runs before Recruiter deliberately: filling out real application
    forms has zero LLM dependency (pure Playwright + regex field
    resolution), while Recruiter is an LLM call whose output (missing-
    keyword extraction) only feeds outreach drafting later, not the fill
    step itself. Recruiter failing — e.g. an API billing issue — must never
    block the thing the person actually asked for, so it's wrapped and
    reported as a soft failure instead of raising."""
    # This is the single start gate shared by the web UI and CLI.  It runs
    # before discovery, scoring, browser automation, or recruiter calls so a
    # known local blocker cannot turn into a partial run or a paid request.
    readiness = evaluate_readiness(conn)
    if not readiness["can_start"]:
        return {
            "readiness": readiness,
            "discover": {"companies_polled": 0, "postings_seen": 0, "jobs_inserted": 0, "errors": []},
            "score": {"scored": 0, "auto_skipped": 0, "below_cap": 0},
            "families": None,
            "recruiter": None,
            "recruiter_error": None,
            "prepped": [],
            "needs_human": [],
            "skipped_overlap": [],
            "skipped_below_threshold": [],
            "skipped_company_cap": [],
            "skipped_category_cap": [],
        }

    discover_summary = run_discovery(conn)
    score_summary = run_dedupe_and_score(conn, resume, cfg)
    pipeline_summary = run_auto_pipeline(
        conn, resume, cfg, headless=headless, limit=limit, exclude_companies=exclude_companies
    )

    try:
        family_summary = assign_role_families(conn)
        recruiter_summary = run_recruiter(conn, resume, target_role_text(cfg), industry_text(cfg))
        recruiter_error = None
    except Exception as exc:
        family_summary = None
        recruiter_summary = None
        recruiter_error = str(exc)

    return {
        "readiness": readiness,
        "discover": discover_summary,
        "score": score_summary,
        "families": family_summary,
        "recruiter": recruiter_summary,
        "recruiter_error": recruiter_error,
        "prepped": pipeline_summary["prepped"],
        "needs_human": pipeline_summary["needs_human"],
        "skipped_overlap": pipeline_summary["skipped_overlap"],
        "skipped_below_threshold": pipeline_summary["skipped_below_threshold"],
        "skipped_company_cap": pipeline_summary["skipped_company_cap"],
        "skipped_category_cap": pipeline_summary["skipped_category_cap"],
    }


def submit_one(conn: sqlite3.Connection, resume: MasterResume, answers_bank: AnswersBank, job_id: int) -> dict:
    """The per-application Submit action — the only path (besides the bulk
    release_all_prepped fallback) that clicks a real submit button. Only
    ever acts on a job already PREPPED."""
    row = conn.execute(
        "SELECT a.resume_path, j.* FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.job_id = ? AND a.status = 'PREPPED'",
        (job_id,),
    ).fetchone()
    if row is None:
        return {"job_id": job_id, "status": "NOT_FOUND", "reason": "No PREPPED application with this job id."}
    row = dict(row)
    docx_path = Path(row["resume_path"]) if row["resume_path"] else None
    return submit_application(row, resume, answers_bank, docx_path, conn)


def discard_one(conn: sqlite3.Connection, job_id: int) -> dict:
    """The per-application Discard action — marks the application as
    discarded so it never gets submitted and never gets auto-refilled by a
    later pipeline run (excluded from run_auto_pipeline's eligible-jobs
    query)."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "UPDATE applications SET status = 'DISCARDED', updated_at = ? WHERE job_id = ? AND status = 'PREPPED'",
        (now, job_id),
    )
    conn.execute("UPDATE jobs SET status = 'DISCARDED' WHERE id = ? AND status != 'SUBMITTED'", (job_id,))
    conn.commit()
    return {"job_id": job_id, "status": "DISCARDED" if cursor.rowcount else "NOT_FOUND"}


def release_all_prepped(
    conn: sqlite3.Connection,
    resume: MasterResume,
    answers_bank: AnswersBank,
    *,
    headless: bool = True,
) -> dict:
    """Bulk convenience action: submit every currently-PREPPED application
    in one go, for anyone who's reviewed the whole progress report and
    wants to release all of it at once instead of clicking Submit on each."""
    rows = conn.execute(
        """
        SELECT a.job_id, a.resume_path, j.company, j.title, j.location
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.status = 'PREPPED'
        """
    ).fetchall()

    summary = {"submitted": [], "needs_human": []}
    for row in rows:
        row = dict(row)
        job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)).fetchone())
        docx_path = row["resume_path"]

        result = submit_application(
            job, resume, answers_bank, Path(docx_path) if docx_path else None, conn, headless=headless
        )
        entry = {
            "job_id": row["job_id"], "company": row["company"], "title": row["title"], "location": row["location"],
        }
        if result.get("status") == "SUBMITTED":
            summary["submitted"].append({**entry, "application_ref": result.get("application_ref")})
        else:
            summary["needs_human"].append({**entry, "reason": result.get("reason")})

    return summary
