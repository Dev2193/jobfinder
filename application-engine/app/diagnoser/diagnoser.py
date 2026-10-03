"""§6.1 Diagnoser — runs once at intake, blocks Stage 4 until resolved.

Structural/formatting faults belong to the resume, not the posting, so this
runs only when the master resume's content hash changes. Every ATS-killer it
finds must be marked resolved or explicitly waived before any job is tailored.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import SearchFilters, industry_text, target_role_text
from app.llm import MODEL_SONNET, cached_system, call_structured
from app.models import MasterResume
from app.resume_parser import content_hash

DIAGNOSIS_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data"

SYSTEM_PROMPT = """You are a senior ATS evaluator who has reviewed 10,000+ resumes
for the target role. Be brutally specific. Quote the candidate's actual lines
back to them. Do not soften feedback."""

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "ats_killers": {
            "type": "array",
            "description": "Formatting/parsing/layout faults causing auto-rejection or burial "
            "(tables, columns, headers, graphics, fonts, date formats, file-type risk, etc).",
            "items": {"type": "string"},
        },
        "section_diagnosis": {
            "type": "array",
            "description": "For summary, experience, skills, and education: the single weakest "
            "bullet/sentence and why it fails ATS scoring or recruiter scanning.",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "weakest_line": {"type": "string"},
                    "why_it_fails": {"type": "string"},
                },
                "required": ["section", "weakest_line", "why_it_fails"],
                "additionalProperties": False,
            },
        },
        "missing_signals": {
            "type": "array",
            "description": "What hiring managers for this role expect to see that is absent.",
            "items": {"type": "string"},
        },
        "top_fixes": {
            "type": "array",
            "description": "The top 5 fixes ranked by impact, including a before/after for at "
            "least one bullet.",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "change": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                },
                "required": ["rank", "change"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ats_killers", "section_diagnosis", "missing_signals", "top_fixes"],
    "additionalProperties": False,
}


def _build_system(resume: MasterResume) -> list[dict]:
    resume_text = json.dumps(resume.model_dump(), indent=2)
    return cached_system(
        SYSTEM_PROMPT,
        "Candidate's resume, as structured JSON (this is the full source of truth — "
        f"do not assume anything not present here):\n\n{resume_text}",
    )


def _build_prompt(target_role: str, industry: str, seniority: str) -> str:
    today = datetime.now(timezone.utc).strftime("%B %Y")
    return f"""Today's date is {today}. Use this, not any assumption from your training
data, to judge whether a resume date is past, present, or genuinely future-dated —
do not flag a date as an ATS-killer just because it looks recent or post-dates your
training cutoff.

Target role: {target_role or "(not specified)"}
Industry: {industry or "(not specified)"}
Seniority: {seniority or "(not specified)"}

Produce your evaluation in four sections per your instructions: ATS-killers,
section-by-section diagnosis, missing signals, and top 5 fixes ranked by impact."""


def render_markdown(diagnosis: dict) -> str:
    lines = ["# Resume Diagnosis", ""]
    lines.append("## ATS-killers")
    if diagnosis["ats_killers"]:
        for item in diagnosis["ats_killers"]:
            lines.append(f"- [ ] {item}")
    else:
        lines.append("_None found._")
    lines.append("")

    lines.append("## Section-by-section diagnosis")
    for entry in diagnosis["section_diagnosis"]:
        lines.append(f"### {entry['section']}")
        lines.append(f"> {entry['weakest_line']}")
        lines.append("")
        lines.append(entry["why_it_fails"])
        lines.append("")

    lines.append("## Missing signals")
    for item in diagnosis["missing_signals"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## Top 5 fixes ranked by impact")
    for fix in sorted(diagnosis["top_fixes"], key=lambda f: f.get("rank", 0)):
        lines.append(f"{fix.get('rank', '?')}. **{fix['change']}**")
        if fix.get("before"):
            lines.append(f"   - Before: {fix['before']}")
        if fix.get("after"):
            lines.append(f"   - After: {fix['after']}")
    lines.append("")

    return "\n".join(lines)


def run_diagnoser(
    resume: MasterResume,
    cfg: SearchFilters,
    conn: sqlite3.Connection,
    *,
    out_dir: Path = DIAGNOSIS_OUTPUT_DIR,
) -> int:
    """Run the Diagnoser, persist diagnosis.md + the blocking checklist, and
    return the new diagnosis's id. Does not run again if the resume hash is
    unchanged and an unresolved diagnosis for it already exists — call
    `needs_diagnosis` first to check."""
    target_role = target_role_text(cfg)
    seniority = ", ".join(cfg.seniority)

    prompt = _build_prompt(target_role, industry_text(cfg), seniority)
    diagnosis = call_structured(
        prompt,
        DIAGNOSIS_SCHEMA,
        tool_name="emit_diagnosis",
        stage="diagnoser",
        model=MODEL_SONNET,
        system=_build_system(resume),
        max_tokens=4096,
        conn=conn,
    )

    resume_hash = content_hash(resume)
    version_row = conn.execute(
        "SELECT id FROM resume_versions WHERE content_hash = ?", (resume_hash,)
    ).fetchone()
    if version_row is None:
        raise RuntimeError(
            "No resume_versions row for this resume's content hash — save the "
            "master resume (resume_parser.save_master_resume) before diagnosing it."
        )
    resume_version_id = version_row["id"]

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "diagnosis.md"
    md_path.write_text(render_markdown(diagnosis))

    cursor = conn.execute(
        "INSERT INTO diagnoses (resume_version_id, created_at, markdown_path, status) "
        "VALUES (?, ?, ?, 'BLOCKING')",
        (resume_version_id, datetime.now(timezone.utc).isoformat(), str(md_path)),
    )
    diagnosis_id = cursor.lastrowid

    ordinal = 0
    for item in diagnosis["ats_killers"]:
        ordinal += 1
        conn.execute(
            "INSERT INTO diagnosis_items (diagnosis_id, category, ordinal, text) VALUES (?, 'ats_killer', ?, ?)",
            (diagnosis_id, ordinal, item),
        )
    for entry in diagnosis["section_diagnosis"]:
        ordinal += 1
        text = f"{entry['section']}: {entry['weakest_line']} — {entry['why_it_fails']}"
        conn.execute(
            "INSERT INTO diagnosis_items (diagnosis_id, category, ordinal, text) VALUES (?, 'section_diagnosis', ?, ?)",
            (diagnosis_id, ordinal, text),
        )
    for item in diagnosis["missing_signals"]:
        ordinal += 1
        conn.execute(
            "INSERT INTO diagnosis_items (diagnosis_id, category, ordinal, text) VALUES (?, 'missing_signal', ?, ?)",
            (diagnosis_id, ordinal, item),
        )
    for fix in diagnosis["top_fixes"]:
        ordinal += 1
        text = fix["change"]
        if fix.get("before") or fix.get("after"):
            text += f" (before: {fix.get('before', '')!r} / after: {fix.get('after', '')!r})"
        conn.execute(
            "INSERT INTO diagnosis_items (diagnosis_id, category, ordinal, text) VALUES (?, 'top_fix', ?, ?)",
            (diagnosis_id, ordinal, text),
        )

    conn.commit()
    return diagnosis_id


def needs_diagnosis(resume: MasterResume, conn: sqlite3.Connection) -> bool:
    """True only if this exact resume hash has never been diagnosed. An
    un-cleared (BLOCKING) diagnosis does NOT need to re-run — the gate is
    cleared by resolving/waiving items via resolve_gate, not by re-invoking
    the model. Re-running on every page view would burn a fresh LLM call on
    every visit to the diagnosis page."""
    resume_hash = content_hash(resume)
    version_row = conn.execute(
        "SELECT id FROM resume_versions WHERE content_hash = ?", (resume_hash,)
    ).fetchone()
    if version_row is None:
        return True
    diag_row = conn.execute(
        "SELECT id FROM diagnoses WHERE resume_version_id = ? ORDER BY id DESC LIMIT 1",
        (version_row["id"],),
    ).fetchone()
    return diag_row is None


def resolve_gate(conn: sqlite3.Connection, diagnosis_id: int) -> bool:
    """Check every ats_killer item is resolved or waived; if so, flip the
    diagnosis to CLEARED and unblock Stage 4. Returns whether it's cleared."""
    blocking = conn.execute(
        "SELECT COUNT(*) AS n FROM diagnosis_items "
        "WHERE diagnosis_id = ? AND category = 'ats_killer' AND resolved = 0 AND waived = 0",
        (diagnosis_id,),
    ).fetchone()["n"]
    if blocking == 0:
        conn.execute("UPDATE diagnoses SET status = 'CLEARED' WHERE id = ?", (diagnosis_id,))
        conn.commit()
        return True
    return False


def set_item_state(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    resolved: bool | None = None,
    waived: bool | None = None,
) -> None:
    if resolved is not None:
        conn.execute("UPDATE diagnosis_items SET resolved = ? WHERE id = ?", (int(resolved), item_id))
    if waived is not None:
        conn.execute("UPDATE diagnosis_items SET waived = ? WHERE id = ?", (int(waived), item_id))
    conn.commit()
