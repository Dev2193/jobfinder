"""§11b per-answer grading: rate out of 10, state what a top-tier candidate
would have said instead, name the one phrasing change to make. Behavioral
answers are additionally scored against STAR. Push back on vague answers
the way a real tough interviewer would, rather than grading a non-answer.
"""

import json
import sqlite3

from app.llm import MODEL_SONNET, cached_system, call_structured

SYSTEM_PROMPT = """You are a tough, demanding interviewer grading a
candidate's live answer. Be tough. Don't soften feedback. If the answer is
vague, generic, or dodges the question, do NOT grade it — set is_vague to
true and write a pointed push-back question a real interviewer would ask to
press for specifics, the same way a skeptical interviewer keeps pushing
until they get a real answer.

If the answer is substantive enough to grade: score it honestly out of 10
(most answers should NOT get 8+; reserve that for genuinely excellent
answers), state exactly what a top-tier candidate would have said instead,
and name the ONE specific phrasing change that would most improve it. For
behavioral questions, also check which STAR components (Situation, Task,
Action, Result) were present or missing.

Separately: if this answer reveals a real, true strength that the
candidate's resume actually covers but states too weakly or buries, flag
resume_gap_detected — this is a resume-emphasis problem, not a fabrication
one, so only flag it when the underlying claim is genuinely supported by
the resume content you're given."""

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_vague": {"type": "boolean"},
        "push_back_question": {
            "type": "string",
            "description": "Only set when is_vague is true: a pointed follow-up question pressing for specifics.",
        },
        "score": {"type": "integer", "description": "Out of 10. Only set when is_vague is false."},
        "star_check": {
            "type": "string",
            "description": "For behavioral questions only: which STAR components were present/missing.",
        },
        "ideal_answer": {"type": "string", "description": "What a top-tier candidate would have said instead."},
        "one_phrasing_change": {"type": "string"},
        "resume_gap_detected": {"type": "boolean"},
        "resume_gap_note": {
            "type": "string",
            "description": "If resume_gap_detected: which resume entry covers this and how it's currently buried/understated.",
        },
    },
    "required": ["is_vague", "ideal_answer", "one_phrasing_change", "resume_gap_detected"],
    "additionalProperties": False,
}


def _build_system(context: dict) -> list[dict]:
    """The candidate's tailored resume is identical across every question in
    a session (up to 8 grading calls, plus push-back retries) — cached so
    it's billed once instead of once per question."""
    entries_text = json.dumps(context.get("tailored_entries") or [], indent=2)
    return cached_system(
        SYSTEM_PROMPT,
        f"Candidate's actual tailored resume for this job (for resume-gap detection):\n{entries_text}",
    )


def _build_prompt(question: str, answer: str, is_behavioral: bool, is_pushback_followup: bool) -> str:
    kind = "behavioral (score against STAR)" if is_behavioral else "technical/role-specific"
    followup_note = (
        "\nThis is the candidate's revised answer after a push-back for vagueness — grade it now, don't push back twice."
        if is_pushback_followup
        else ""
    )
    return f"""Question type: {kind}
Question asked: {question}

Candidate's answer: {answer}
{followup_note}

Grade this answer per your instructions, using the candidate's tailored
resume given in the system prompt for resume-gap detection."""


def grade_answer(
    question: str,
    answer: str,
    context: dict,
    conn: sqlite3.Connection,
    job_id: int,
    *,
    is_behavioral: bool = False,
    is_pushback_followup: bool = False,
) -> dict:
    prompt = _build_prompt(question, answer, is_behavioral, is_pushback_followup)
    return call_structured(
        prompt,
        GRADE_SCHEMA,
        tool_name="emit_answer_grade",
        stage="interview_grading",
        model=MODEL_SONNET,
        system=_build_system(context),
        max_tokens=1024,
        conn=conn,
        job_id=job_id,
    )
