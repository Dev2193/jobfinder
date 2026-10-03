"""§11b — the interview prep session itself: one question at a time,
waiting for a real answer before moving on. Round 1 is 5 technical
questions, Round 2 is 3 behavioral questions scored against STAR. Appends a
timestamped, never-overwritten transcript, persists the hireability score
for cross-session trend tracking, and feeds resume-emphasis gaps back into
the metrics_gaps queue per §11b's own instruction.

get_answer_fn is swappable: the CLI wires it to real input() for a live
terminal session; tests/verification pass a canned-answer function instead,
since this can't be driven through a non-interactive tool call.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.interview.context import build_interview_context
from app.interview.grading import grade_answer
from app.interview.questions import generate_questions
from app.interview.summary import generate_summary
from app.rewriter.metrics_gaps import record_gap

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "output"


def _safe_dirname(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "untitled"


def _ask_and_grade(
    question: str,
    is_behavioral: bool,
    context: dict,
    conn: sqlite3.Connection,
    job_id: int,
    get_answer_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> dict:
    print_fn(f"\n{question}")
    answer = get_answer_fn(question)
    grade = grade_answer(question, answer, context, conn, job_id, is_behavioral=is_behavioral)

    if grade["is_vague"] and grade.get("push_back_question"):
        print_fn(f"\n[Interviewer pushes back] {grade['push_back_question']}")
        followup_answer = get_answer_fn(grade["push_back_question"])
        answer = f"{answer}\n[after push-back] {followup_answer}"
        grade = grade_answer(
            question, followup_answer, context, conn, job_id,
            is_behavioral=is_behavioral, is_pushback_followup=True,
        )

    print_fn(f"Score: {grade.get('score', '?')}/10")
    print_fn(f"A top-tier answer: {grade['ideal_answer']}")
    print_fn(f"One change to make: {grade['one_phrasing_change']}")

    if grade.get("resume_gap_detected") and grade.get("resume_gap_note"):
        entries = context.get("tailored_entries") or []
        if entries:
            target = entries[0]
            record_gap(
                conn,
                org=target["org"],
                title=target["title"],
                original_bullet=target["bullets"][0]["rewritten"] if target.get("bullets") else question,
                needs_metric=grade["resume_gap_note"],
                suggested_range=None,
                gap_type="resume_emphasis",
            )

    return {"question": question, "answer": answer, "is_behavioral": is_behavioral, **grade}


def _render_transcript_section(session_number: int, job: dict, qa_transcript: list[dict], summary: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n---\n\n## Session {session_number} — {now}\n"]
    lines.append(f"**{job['company']} — {job['title']}**\n")

    lines.append("### Round 1: Technical\n")
    for qa in [q for q in qa_transcript if not q["is_behavioral"]]:
        lines.append(f"**Q: {qa['question']}**")
        lines.append(f"A: {qa['answer']}")
        lines.append(f"Score: {qa.get('score', '?')}/10 — {qa['one_phrasing_change']}\n")

    lines.append("### Round 2: Behavioral (STAR)\n")
    for qa in [q for q in qa_transcript if q["is_behavioral"]]:
        lines.append(f"**Q: {qa['question']}**")
        lines.append(f"A: {qa['answer']}")
        lines.append(f"STAR check: {qa.get('star_check', '—')}")
        lines.append(f"Score: {qa.get('score', '?')}/10 — {qa['one_phrasing_change']}\n")

    lines.append(f"### Hireability score: {summary['hireability_score']}/100\n")
    lines.append("**Three weakest answers:**")
    for w in summary["weakest_answers"]:
        lines.append(f"- \"{w['quoted_words']}\" — {w['why_it_lost_points']}")
    lines.append("\n**Rehearse before the real interview:**")
    for q in summary["questions_to_rehearse"]:
        lines.append(f"- {q}")
    lines.append(f"\n**Study plan:**\n{summary['study_plan']}\n")

    return "\n".join(lines)


def run_interview_session(
    job: dict,
    conn: sqlite3.Connection,
    *,
    get_answer_fn: Callable[[str], str],
    print_fn: Callable[[str], None] = print,
) -> dict:
    context = build_interview_context(job, conn)
    print_fn(f"Interview prep: {job['company']} — {job['title']} ({context['seniority']})\n")

    questions = generate_questions(context, conn, job["id"])
    qa_transcript = []

    print_fn("=== Round 1: Technical ===")
    for q in questions["technical_questions"]:
        qa_transcript.append(_ask_and_grade(q, False, context, conn, job["id"], get_answer_fn, print_fn))

    print_fn("\n=== Round 2: Behavioral (STAR) ===")
    for q in questions["behavioral_questions"]:
        qa_transcript.append(_ask_and_grade(q, True, context, conn, job["id"], get_answer_fn, print_fn))

    summary = generate_summary(qa_transcript, conn, job["id"])

    session_count = conn.execute(
        "SELECT COUNT(*) AS n FROM interview_sessions WHERE job_id = ?", (job["id"],)
    ).fetchone()["n"]
    session_number = session_count + 1

    job_dir = OUTPUT_DIR / f"{_safe_dirname(job['company'])}_{_safe_dirname(job['title'])}"
    job_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = job_dir / "interview_prep.md"

    section = _render_transcript_section(session_number, job, qa_transcript, summary)
    with open(transcript_path, "a") as f:
        f.write(section)

    conn.execute(
        "INSERT INTO interview_sessions (job_id, session_number, hireability_score, transcript_path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job["id"], session_number, summary["hireability_score"], str(transcript_path), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    print_fn(f"\n\n=== Hireability score: {summary['hireability_score']}/100 ===")
    print_fn(f"Transcript appended to: {transcript_path}")

    return {"session_number": session_number, "summary": summary, "transcript_path": str(transcript_path)}
