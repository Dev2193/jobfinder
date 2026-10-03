"""§6.2 Recruiter — runs once per role family, not per job.

Grounded in the concatenated, deduplicated requirements sections of every
posting discovered so far in that family, rather than model recall — the
spec calls this out explicitly since "trending in 2026" claims about a
market the model may not have current data on are otherwise unverifiable.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.llm import MODEL_SONNET, cached_system, call_structured
from app.models import MasterResume

INVALIDATE_AFTER_DAYS = 30
INVALIDATE_AFTER_NEW_POSTINGS = 10

# Cap on concatenated posting text sent per family — keeps a single Recruiter
# call bounded regardless of how many postings a family accumulates. This is
# a simple length cap, not the smarter boilerplate-stripping trim that §6.6
# builds for the Rewriter; good enough for grounding keyword extraction.
MAX_POSTING_CHARS = 1200
MAX_TOTAL_CONTEXT_CHARS = 8000

SYSTEM_PROMPT = """You are a senior recruiter with 10 years placing candidates
into the target role, reading 1,000+ live job descriptions a month. Ground
every claim in the job postings you are given — if the postings don't support
a claim (especially about "trending" skills), do not make it. You do not have
reliable knowledge of the current job market on your own; the postings are
your only source of truth for what's currently in demand."""

RECRUITER_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "description": "Top 15 keywords/skills appearing most often in the supplied postings, "
            "ranked by frequency, each tagged technical/soft/tool.",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "category": {"type": "string", "enum": ["technical", "soft", "tool"]},
                },
                "required": ["term", "category"],
                "additionalProperties": False,
            },
        },
        "missing_from_resume": {
            "type": "array",
            "description": "Which of the above keywords are absent from the candidate's resume, or "
            "present but buried. One entry per gap.",
            "items": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "status": {"type": "string", "enum": ["missing", "buried"]},
                    "note": {"type": "string"},
                },
                "required": ["keyword", "status"],
                "additionalProperties": False,
            },
        },
        "trending_skills": {
            "type": "array",
            "description": "Skills rising in this role that most candidates aren't yet listing. "
            "Must be grounded in the supplied postings — if they don't support a claim, omit it.",
            "items": {"type": "string"},
        },
        "buzzwords_to_cut": {
            "type": "array",
            "description": "Overused, low-signal phrasing, quoting what's currently in the resume.",
            "items": {"type": "string"},
        },
        "ranked_actions": {
            "type": "array",
            "description": "The 5 changes that move the resume from screened-out to shortlist "
            "fastest, ranked by impact.",
            "items": {"type": "string"},
        },
    },
    "required": ["keywords", "missing_from_resume", "trending_skills", "buzzwords_to_cut", "ranked_actions"],
    "additionalProperties": False,
}


def _trim_posting(description: str) -> str:
    return (description or "").strip()[:MAX_POSTING_CHARS]


def _build_grounding_context(jobs: list[dict]) -> str:
    seen: set[str] = set()
    chunks: list[str] = []
    total = 0
    for job in jobs:
        trimmed = _trim_posting(job.get("description_raw", ""))
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        chunk = f"### {job['company']} — {job['title']}\n{trimmed}"
        if total + len(chunk) > MAX_TOTAL_CONTEXT_CHARS:
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n\n".join(chunks)


def _build_system(resume: MasterResume, target_role: str, industry: str) -> list[dict]:
    """Resume + target role/industry are identical across every role-family
    call in a session — cached so they're billed once instead of once per
    family (~5 calls for 80 jobs, per §6)."""
    resume_text = json.dumps(resume.model_dump(), indent=2)
    return cached_system(
        SYSTEM_PROMPT,
        f"""Target role: {target_role or "(not specified)"}
Industry: {industry or "(not specified)"}

Candidate's resume, as structured JSON:

{resume_text}""",
    )


def _build_prompt(seniority: str, grounding_context: str) -> str:
    return f"""Seniority: {seniority or "(not specified)"}

Live postings currently open for this role family (this is your only grounding
for keyword frequency and "trending" claims — do not supplement from your own
training-data assumptions about the market):

{grounding_context or "(no postings available yet for this family)"}

Produce your evaluation per your instructions: top 15 keywords/skills, which
are missing or buried in the resume, grounded trending skills, buzzwords to
cut, and a ranked action list of the 5 highest-impact changes."""


def needs_recruiter_run(family_row: sqlite3.Row, current_job_count: int) -> bool:
    """True if this family has never been run, or is stale per §6.2's
    invalidation rule: 30 days elapsed, or 10+ new postings since last run."""
    if family_row["updated_at"] is None:
        return True
    updated_at = datetime.fromisoformat(family_row["updated_at"])
    age = datetime.now(timezone.utc) - updated_at
    if age > timedelta(days=INVALIDATE_AFTER_DAYS):
        return True
    new_postings = current_job_count - family_row["postings_at_last_run"]
    return new_postings >= INVALIDATE_AFTER_NEW_POSTINGS


def run_recruiter_for_family(
    family_row: sqlite3.Row,
    jobs: list[dict],
    resume: MasterResume,
    target_role: str,
    industry: str,
    conn: sqlite3.Connection,
) -> dict:
    """Run the Recruiter once for this family, persist missing_keywords +
    the full output to role_families, and return the parsed result."""
    grounding_context = _build_grounding_context(jobs)
    prompt = _build_prompt(family_row["seniority"], grounding_context)

    result = call_structured(
        prompt,
        RECRUITER_SCHEMA,
        tool_name="emit_recruiter_analysis",
        stage="recruiter",
        model=MODEL_SONNET,
        system=_build_system(resume, target_role, industry),
        max_tokens=4096,
        conn=conn,
        job_id=None,
    )

    missing_keywords = [item["keyword"] for item in result["missing_from_resume"]]
    conn.execute(
        "UPDATE role_families SET missing_keywords = ?, recruiter_output = ?, "
        "updated_at = ?, postings_at_last_run = ? WHERE id = ?",
        (
            json.dumps(missing_keywords),
            json.dumps(result),
            datetime.now(timezone.utc).isoformat(),
            len(jobs),
            family_row["id"],
        ),
    )
    conn.commit()
    return result


def run_recruiter(
    conn: sqlite3.Connection,
    resume: MasterResume,
    target_role: str,
    industry: str,
    *,
    force: bool = False,
    label_filter: list[str] | None = None,
) -> dict:
    """Run the Recruiter for every role family that needs it (new or stale
    per §6.2's cache invalidation rule). Skips families that are already
    fresh, so re-running costs nothing beyond the first pass.

    `label_filter`, when given, restricts the run to families whose label
    contains any of the given substrings (case-insensitive) — a cost control
    for scoping to relevant families rather than paying for every one-off
    role a broad discovery pass happened to pull in."""
    families = conn.execute("SELECT * FROM role_families").fetchall()
    if label_filter:
        needles = [n.lower() for n in label_filter]
        families = [f for f in families if any(n in f["label"].lower() for n in needles)]
    summary = {"ran": [], "skipped_fresh": []}

    for family_row in families:
        jobs = conn.execute(
            "SELECT company, title, description_raw FROM jobs WHERE role_family_id = ?",
            (family_row["id"],),
        ).fetchall()
        jobs = [dict(j) for j in jobs]
        if not jobs:
            continue

        if not force and not needs_recruiter_run(family_row, len(jobs)):
            summary["skipped_fresh"].append(family_row["label"])
            continue

        run_recruiter_for_family(family_row, jobs, resume, target_role, industry, conn)
        summary["ran"].append(family_row["label"])

    return summary
