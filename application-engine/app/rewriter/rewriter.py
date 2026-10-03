"""§6.3 Rewriter — runs per job, orchestrated with the §6.5 truth validator
and §6.4 metrics gap queue (§6: "Do not run all four [skills] per job — that
is the difference between a workable token budget and an unworkable one").

Rewriter rule 5 (layer in missing keywords, only where truthful) is the
highest-risk rule in the stack, which is why every pass is immediately
checked by the truth validator before anything is persisted or a job moves
to TAILORED.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from app.db import log_event
from app.llm import MODEL_SONNET, cached_system, call_structured
from app.models import MasterResume
from app.rewriter.metrics_gaps import record_gap
from app.rewriter.truth_validator import run_truth_validator

MAX_RETRIES = 1  # one retry after a validator rejection, then NEEDS_HUMAN (§6.5)

# Boilerplate commonly padding job descriptions — stripped before the posting
# reaches the model, per §10's cost-control guidance to trim descriptions to
# responsibilities + requirements only.
_BOILERPLATE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r".*equal opportunity employer.*",
        r".*(diversity|inclusion) is (a|one of).*",
        r".*background check.*",
        r".*visa sponsorship.*",
        r".*benefits (include|package).*",
        r".*we are committed to.*",
        r".*physical requirements.*",
        r".*base pay range.*",
        r".*compensation (range|package).*",
    ]
]
MAX_POSTING_CHARS = 3200  # ~800 tokens, per §10


def trim_posting(description: str) -> str:
    paragraphs = re.split(r"\n{2,}", description or "")
    kept = [
        p.strip()
        for p in paragraphs
        if p.strip() and not any(pat.match(p.strip()) for pat in _BOILERPLATE_PATTERNS)
    ]
    return "\n\n".join(kept)[:MAX_POSTING_CHARS]


SYSTEM_PROMPT = """You are a resume writer who has coached candidates into
Meta, Google, Amazon, and Fortune 500 roles, applying Google's XYZ formula to
every bullet:

Accomplished [X] as measured by [Y], by doing [Z]
X = the impact or result · Y = the metric, percentage, or measurable outcome · Z = the specific action or method

Rules:
1. Every bullet leads with a strong action verb. Never "responsible for", "helped with", "assisted in".
2. Every bullet should carry a number, percentage, currency figure, or measurable outcome — but
   NEVER invent one, and NEVER compute a new derived number (a percentage, ratio, or rate) from
   figures already in the resume, even if the arithmetic is correct. Only use a number that
   already appears verbatim in the original bullet or elsewhere in the resume. If the original
   bullet and the candidate's resume don't support a real, already-stated number for this bullet,
   set has_metric to false and describe what's missing instead of guessing, estimating, or
   calculating one. This rule is a hard constraint, not a suggestion — an unquantified bullet is
   always better than a fabricated or derived number.
3. One line per bullet, two maximum.
4. Match vocabulary to the language used in the job posting, where truthful.
5. Layer in missing keywords from the recruiter analysis — ONLY where truthful. A keyword that
   doesn't describe work the candidate actually did does not go in, regardless of how often it
   appears in the posting. Do not add a specific named tool, framework, or technology unless the
   original bullet or resume already supports it.
6. Cut filler words: "various", "multiple", "different", "successfully", "effectively".
7. Never add a specific detail — a collaborator type, team name, tool, technology, dataset, or
   method — that isn't already stated in the original bullet or elsewhere in the resume, even if
   it reads as a natural elaboration. "Co-engineering the system" cannot become "collaborating
   with clinical teams" unless "clinical teams" is already named somewhere in the resume."""

REWRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "org": {"type": "string"},
                    "title": {"type": "string"},
                    "bullets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "original": {"type": "string"},
                                "rewritten": {"type": "string"},
                                "has_metric": {"type": "boolean"},
                                "is_estimate": {
                                    "type": "boolean",
                                    "description": "True if the metric in `rewritten` is an "
                                    "approximation carried over from the original bullet's own "
                                    "language, not a hard fact.",
                                },
                                "needs_metric": {
                                    "type": "string",
                                    "description": "Only set when has_metric is false: what "
                                    "quantity is missing (e.g. 'team size', 'percentage improvement').",
                                },
                                "suggested_range": {"type": "string"},
                            },
                            "required": ["original", "rewritten", "has_metric", "is_estimate"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["org", "title", "bullets"],
                "additionalProperties": False,
            },
        },
        "top_changes": {
            "type": "array",
            "description": "The 5 highest-impact changes, each with a before/after and a "
            "one-line rationale.",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["rank", "before", "after", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entries", "top_changes"],
    "additionalProperties": False,
}


def _build_system(resume: MasterResume) -> list[dict]:
    """The rules text and the candidate's experience section are identical
    across every Rewriter call until the resume itself changes — cached as
    two breakpoints so they're billed once (cache write) and read cheaply
    (~0.1x) on every subsequent job, instead of full price every time."""
    experience_text = json.dumps([e.model_dump() for e in resume.experience], indent=2)
    return cached_system(
        SYSTEM_PROMPT,
        f"Candidate's experience section (the only source of truth for what they actually did):\n{experience_text}",
    )


def _build_prompt(
    target_role: str,
    missing_keywords: list[str],
    trimmed_posting: str,
    retry_violations: list[dict] | None = None,
) -> str:
    keywords_text = ", ".join(missing_keywords) if missing_keywords else "(none cached yet)"

    retry_note = ""
    if retry_violations:
        violations_text = json.dumps(retry_violations, indent=2)
        retry_note = f"""

Your previous attempt was rejected by the truth validator for these
violations — do not repeat them. Every claim must be traceable to the
candidate's actual experience given in the system prompt, with no invented
tools, metrics, or entities:

{violations_text}"""

    return f"""Target role: {target_role or "(not specified)"}

Missing keywords from the recruiter analysis for this role family (layer
these in only where truthful — see rule 5):
{keywords_text}

Job posting (trimmed):
{trimmed_posting}
{retry_note}

Rewrite every bullet in the experience section (given in the system prompt)
per your instructions, and identify the 5 highest-impact changes with
before/after and rationale."""


def _content_hash(resume: MasterResume, job_content_hash: str) -> str:
    resume_json = json.dumps(resume.model_dump(), sort_keys=True)
    return hashlib.sha256(f"{resume_json}::{job_content_hash}".encode("utf-8")).hexdigest()


def call_rewriter(
    resume: MasterResume,
    target_role: str,
    missing_keywords: list[str],
    trimmed_posting: str,
    conn: sqlite3.Connection,
    *,
    job_id: int | None = None,
    retry_violations: list[dict] | None = None,
) -> dict:
    prompt = _build_prompt(target_role, missing_keywords, trimmed_posting, retry_violations)
    return call_structured(
        prompt,
        REWRITER_SCHEMA,
        tool_name="emit_rewritten_resume",
        stage="rewriter",
        model=MODEL_SONNET,
        system=_build_system(resume),
        max_tokens=4096,
        conn=conn,
        job_id=job_id,
    )


def _bullet_pairs(rewritten: dict) -> list[dict]:
    pairs = []
    for entry in rewritten["entries"]:
        for bullet in entry["bullets"]:
            pairs.append(
                {
                    "org": entry["org"],
                    "title": entry["title"],
                    "original": bullet["original"],
                    "rewritten": bullet["rewritten"],
                }
            )
    return pairs


def tailor_job(
    job: dict,
    resume: MasterResume,
    target_role: str,
    conn: sqlite3.Connection,
) -> dict:
    """Run Rewriter -> truth validator (with one retry on rejection) for a
    single job. Persists the result to tailored_resumes, records any metrics
    gaps, and updates the job's status. Returns a summary dict."""
    job_id = job["id"]

    role_family = None
    if job.get("role_family_id"):
        role_family = conn.execute(
            "SELECT * FROM role_families WHERE id = ?", (job["role_family_id"],)
        ).fetchone()
    missing_keywords = json.loads(role_family["missing_keywords"]) if role_family and role_family["missing_keywords"] else []

    trimmed = trim_posting(job.get("description_raw", ""))
    content_hash = _content_hash(resume, job.get("content_hash", ""))

    existing = conn.execute("SELECT * FROM tailored_resumes WHERE job_id = ?", (job_id,)).fetchone()
    if existing is not None and existing["content_hash"] == content_hash and existing["status"] != "NEEDS_HUMAN":
        return {"job_id": job_id, "status": existing["status"], "reused": True}

    rewritten = call_rewriter(resume, target_role, missing_keywords, trimmed, conn, job_id=job_id)
    validation = run_truth_validator(resume, _bullet_pairs(rewritten), conn, job_id=job_id)
    attempts = 1
    validator_log = [validation]

    while not validation["valid"] and attempts <= MAX_RETRIES:
        log_event(conn, "truth_validator_rejected", payload=json.dumps(validation["violations"]), job_id=job_id)
        rewritten = call_rewriter(
            resume, target_role, missing_keywords, trimmed, conn,
            job_id=job_id, retry_violations=validation["violations"],
        )
        validation = run_truth_validator(resume, _bullet_pairs(rewritten), conn, job_id=job_id)
        validator_log.append(validation)
        attempts += 1

    now = datetime.now(timezone.utc).isoformat()

    if not validation["valid"]:
        conn.execute(
            """
            INSERT INTO tailored_resumes
                (job_id, content_hash, rewritten_bullets, before_after, status,
                 validator_attempts, validator_log, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'NEEDS_HUMAN', ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                content_hash=excluded.content_hash, rewritten_bullets=excluded.rewritten_bullets,
                before_after=excluded.before_after, status='NEEDS_HUMAN',
                validator_attempts=excluded.validator_attempts, validator_log=excluded.validator_log,
                updated_at=excluded.updated_at
            """,
            (
                job_id, content_hash, json.dumps(rewritten["entries"]), json.dumps(rewritten["top_changes"]),
                attempts, json.dumps(validator_log), now, now,
            ),
        )
        conn.execute("UPDATE jobs SET status = 'NEEDS_HUMAN' WHERE id = ?", (job_id,))
        log_event(conn, "tailoring_needs_human", payload=json.dumps(validation["violations"]), job_id=job_id)
        conn.commit()
        return {"job_id": job_id, "status": "NEEDS_HUMAN", "attempts": attempts, "reused": False}

    for entry in rewritten["entries"]:
        for bullet in entry["bullets"]:
            if not bullet["has_metric"]:
                record_gap(
                    conn,
                    org=entry["org"],
                    title=entry["title"],
                    original_bullet=bullet["original"],
                    needs_metric=bullet.get("needs_metric", "unspecified metric"),
                    suggested_range=bullet.get("suggested_range"),
                )

    conn.execute(
        """
        INSERT INTO tailored_resumes
            (job_id, content_hash, rewritten_bullets, before_after, status,
             validator_attempts, validator_log, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'VALIDATED', ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            content_hash=excluded.content_hash, rewritten_bullets=excluded.rewritten_bullets,
            before_after=excluded.before_after, status='VALIDATED',
            validator_attempts=excluded.validator_attempts, validator_log=excluded.validator_log,
            updated_at=excluded.updated_at
        """,
        (
            job_id, content_hash, json.dumps(rewritten["entries"]), json.dumps(rewritten["top_changes"]),
            attempts, json.dumps(validator_log), now, now,
        ),
    )
    conn.execute("UPDATE jobs SET status = 'TAILORED' WHERE id = ?", (job_id,))
    log_event(conn, "job_tailored", job_id=job_id)
    conn.commit()
    return {"job_id": job_id, "status": "TAILORED", "attempts": attempts, "reused": False}
