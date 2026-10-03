"""§6.5 Truth validator — blocking, runs after every Rewriter pass.

Every organization, job title, date range, degree, certification, and
numeric metric in the Rewriter's output must appear in the master resume
JSON. This specifically catches the highest-risk interaction in the stack:
Rewriter rule 5 (layer in missing keywords) pushing against reality. Keyword
insertion is permitted only when it renames work already documented — not
when it invents a tool/technology/metric the candidate never used.
"""

import json
import sqlite3

from app.llm import MODEL_SONNET, cached_system, call_structured
from app.models import MasterResume

SYSTEM_PROMPT = """You are a strict fact-checker comparing a candidate's
rewritten resume bullets against their verified master resume record. Flag
ONLY genuine fabrications: a new organization, job title, date range,
degree, certification, or numeric metric that does not appear anywhere in
the master resume and is not a legitimate rewording of something that does.

Do NOT flag: stronger action verbs, reordered phrasing, or a tool/technology
name that is simply a more precise name for work already described in the
original bullet (e.g. if the original says "built a machine learning model"
and a job posting's own language is "PyTorch", using "PyTorch" is still a
fabrication unless the master resume or original bullet already names that
specific tool — when in doubt about a specific named tool/framework/metric
that isn't in the master resume, flag it. Rewording action verbs or sentence
structure without inventing new specific claims is never a violation."""

TRUTH_VALIDATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean", "description": "True only if there are zero violations."},
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "org": {"type": "string"},
                    "title": {"type": "string"},
                    "rewritten_text": {"type": "string"},
                    "violation_type": {
                        "type": "string",
                        "enum": ["fabricated_entity", "fabricated_metric", "unsupported_keyword", "other"],
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["org", "title", "rewritten_text", "violation_type", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["valid", "violations"],
    "additionalProperties": False,
}


def _resume_block(resume: MasterResume) -> str:
    resume_text = json.dumps(resume.model_dump(), indent=2)
    return f"Master resume JSON (the full, verified source of truth — nothing outside this document is true):\n\n{resume_text}"


def _build_prompt(bullet_pairs: list[dict]) -> str:
    pairs_text = json.dumps(bullet_pairs, indent=2)
    return f"""Rewritten bullets to check, each as {{org, title, original, rewritten}}:

{pairs_text}

For each rewritten bullet, check whether it introduces any organization, job
title, date range, degree, certification, or numeric metric not grounded in
the master resume JSON given in the system prompt. Report every violation found."""


def run_truth_validator(
    resume: MasterResume,
    bullet_pairs: list[dict],
    conn: sqlite3.Connection,
    *,
    job_id: int | None = None,
) -> dict:
    """bullet_pairs: list of {org, title, original, rewritten} dicts. Returns
    {"valid": bool, "violations": [...]}."""
    prompt = _build_prompt(bullet_pairs)
    return call_structured(
        prompt,
        TRUTH_VALIDATOR_SCHEMA,
        tool_name="emit_truth_validation",
        stage="truth_validator",
        model=MODEL_SONNET,
        system=cached_system(SYSTEM_PROMPT, _resume_block(resume)),
        max_tokens=2048,
        conn=conn,
        job_id=job_id,
    )


_FREE_TEXT_SYSTEM_PROMPT = """You are a strict fact-checker comparing a
candidate's free-text writing (a cover letter or outreach email) against
their verified master resume record. Flag ONLY genuine fabrications: a new
organization, job title, date range, degree, certification, or numeric
metric describing the CANDIDATE's own background that does not appear
anywhere in the master resume. Do not flag facts about the target company
itself (those aren't checkable against the candidate's resume) — only flag
claims about what the candidate has done, built, or achieved."""


def _build_free_text_prompt(label: str, text: str) -> str:
    return f"""{label} to check:

{text}

Check every claim this text makes about the CANDIDATE's own background,
experience, or achievements against the master resume JSON given in the
system prompt. Report every unsupported claim as a violation. Do not flag
statements about the target company — those are not checkable against the
candidate's resume."""


def run_free_text_truth_validator(
    resume: MasterResume,
    label: str,
    text: str,
    conn: sqlite3.Connection,
    *,
    job_id: int | None = None,
) -> dict:
    """Same enforcement as run_truth_validator, but for prose (cover letters,
    outreach emails) instead of bullet pairs. Returns {"valid", "violations"}."""
    prompt = _build_free_text_prompt(label, text)
    return call_structured(
        prompt,
        TRUTH_VALIDATOR_SCHEMA,
        tool_name="emit_truth_validation",
        stage="truth_validator_free_text",
        model=MODEL_SONNET,
        system=cached_system(_FREE_TEXT_SYSTEM_PROMPT, _resume_block(resume)),
        max_tokens=2048,
        conn=conn,
        job_id=job_id,
    )
