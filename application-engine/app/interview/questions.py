"""§11b Round 1/2 question generation: the 5 hardest, most realistic
technical/role-specific questions at this level, plus 3 behavioral
questions to be scored against STAR. Grounded in the actual tailored
resume for this job, the posting, and the company — one structured call,
not one per question.
"""

import json
import sqlite3

from app.llm import MODEL_SONNET, cached_system, call_structured

SYSTEM_PROMPT = """You are the hiring manager for this role at this company
type, with 8+ years of experience hiring for this exact position. Write
interview questions the way a real, demanding interviewer would — specific
to this candidate's actual background and this company's stated needs, not
generic questions that could apply to any candidate. The technical questions
should be the hardest, most realistic ones someone at this seniority level
would actually face — the kind that separate strong candidates from weak
ones, not softball warmups."""

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "technical_questions": {
            "type": "array",
            "description": "EXACTLY 5 of the hardest, most realistic technical/role-specific "
            "questions at this level — no more, no fewer.",
            "items": {"type": "string"},
        },
        "behavioral_questions": {
            "type": "array",
            "description": "EXACTLY 3 behavioral questions to be scored against STAR (Situation, "
            "Task, Action, Result) — no more, no fewer.",
            "items": {"type": "string"},
        },
    },
    "required": ["technical_questions", "behavioral_questions"],
    "additionalProperties": False,
}


def _build_system(context: dict) -> list[dict]:
    entries_text = (
        json.dumps(context["tailored_entries"], indent=2)
        if context["tailored_entries"]
        else "(no tailored resume on file for this job yet — base questions on the posting alone)"
    )
    company_hint = context["company_type_hint"] or "(no company site content available)"
    return cached_system(
        SYSTEM_PROMPT,
        f"""Target role: {context['target_role']} at {context['company']}
Seniority: {context['seniority']}

Job posting (trimmed):
{(context['job_description'] or '')[:2000]}

Company context (from their own site):
{company_hint}

The tailored resume actually prepared for this specific application — this
is what the company received, so ground your questions in these actual
claims (probe them, don't just restate them):
{entries_text}""",
    )


def generate_questions(context: dict, conn: sqlite3.Connection, job_id: int) -> dict:
    prompt = "Generate 5 technical/role-specific questions and 3 behavioral questions per your instructions."
    return call_structured(
        prompt,
        QUESTIONS_SCHEMA,
        tool_name="emit_interview_questions",
        stage="interview_questions",
        model=MODEL_SONNET,
        system=_build_system(context),
        max_tokens=2048,
        conn=conn,
        job_id=job_id,
    )
