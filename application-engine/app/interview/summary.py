"""§11b end-of-session summary: overall hireability score out of 100, the
three weakest answers quoting the specific words that lost points, the
three questions to rehearse before the real interview, and a short study
plan for the gap areas.
"""

import json
import sqlite3

from app.llm import MODEL_SONNET, call_structured

SYSTEM_PROMPT = """You are the same tough interviewer who just ran this mock
interview, now writing the final assessment. Be honest and specific — quote
the candidate's actual words when identifying weak answers, don't
paraphrase generically. The hireability score should reflect genuine
interview performance: most candidates should NOT score above 75 on a
realistic first attempt."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "hireability_score": {"type": "integer", "description": "Overall hireability, 0-100."},
        "weakest_answers": {
            "type": "array",
            "description": "EXACTLY 3 weakest answers — no more, no fewer.",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "quoted_words": {"type": "string", "description": "The specific words from the candidate's answer that lost points."},
                    "why_it_lost_points": {"type": "string"},
                },
                "required": ["question", "quoted_words", "why_it_lost_points"],
                "additionalProperties": False,
            },
        },
        "questions_to_rehearse": {
            "type": "array",
            "description": "EXACTLY 3 questions to rehearse — no more, no fewer.",
            "items": {"type": "string"},
        },
        "study_plan": {"type": "string"},
    },
    "required": ["hireability_score", "weakest_answers", "questions_to_rehearse", "study_plan"],
    "additionalProperties": False,
}


def generate_summary(qa_transcript: list[dict], conn: sqlite3.Connection, job_id: int) -> dict:
    prompt = f"""Full transcript of this mock interview (question, answer,
score, and feedback for each):

{json.dumps(qa_transcript, indent=2)}

Write the final assessment per your instructions."""
    return call_structured(
        prompt,
        SUMMARY_SCHEMA,
        tool_name="emit_interview_summary",
        stage="interview_summary",
        model=MODEL_SONNET,
        system=SYSTEM_PROMPT,
        max_tokens=2048,
        conn=conn,
        job_id=job_id,
    )
