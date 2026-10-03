"""PDF/DOCX -> structured master resume JSON (§3).

Extraction is deterministic (pdfplumber / python-docx); structuring the raw text into
the master JSON is one Claude call, because resumes vary wildly in layout and a
one-time LLM structuring pass handles dates/bullet-boundary edge cases far more
robustly than regex heuristics. This runs once at intake (and again only if the
resume file changes), so it does not compete with the per-job token budget in §10.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
from docx import Document

from app.llm import MODEL_SONNET, call_structured
from app.models import MasterResume

MASTER_RESUME_DIR = Path(__file__).resolve().parent.parent / "data"

STRUCTURING_SYSTEM_PROMPT = """You are a precise resume-to-JSON structuring engine.
You will be given the raw extracted text of a resume. Convert it into the exact
JSON schema you have been given as a tool, and call that tool with the result.

Rules:
- Never invent content. If a field is not present in the source text, leave it empty
  (empty string / empty list / null), do not guess or fabricate.
- Preserve bullet text close to verbatim; only fix obvious extraction artifacts
  (stray whitespace, broken line wraps), never rephrase or embellish content.
- Preserve dates exactly as written in the source (e.g. "January 2026 (ongoing)"),
  do not normalize or reformat them.
- Each distinct job/role/position is its own entry in `experience`, even if bullet
  boundaries in the raw text are ambiguous from PDF extraction — use indentation,
  bullet markers, and paragraph breaks to infer boundaries.
"""


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    raise ValueError(f"Unsupported resume file type: {suffix}")


def _extract_pdf_text(path: Path) -> str:
    chunks = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chunks.append(text)
    return "\n".join(chunks)


def _extract_docx_text(path: Path) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def structure_resume(raw_text: str, *, conn: sqlite3.Connection | None = None) -> MasterResume:
    schema = MasterResume.model_json_schema()
    prompt = f"Raw resume text:\n\n{raw_text}"
    result = call_structured(
        prompt,
        schema,
        tool_name="emit_master_resume",
        stage="intake_resume_parse",
        model=MODEL_SONNET,
        system=STRUCTURING_SYSTEM_PROMPT,
        max_tokens=4096,
        conn=conn,
        # The API's strict-mode grammar compiler rejects this schema outright
        # ("compiled grammar is too large") — the $defs/$ref + nullable-anyOf
        # shape Pydantic emits for MasterResume, not a size problem (the raw
        # schema is ~4KB). Pydantic's own model_validate() below still
        # enforces the schema on the result, so this doesn't weaken
        # correctness — it just loses the belt on top of the suspenders.
        strict=False,
    )
    return MasterResume.model_validate(result)


def content_hash(resume: MasterResume) -> str:
    canonical = json.dumps(resume.model_dump(), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_master_resume(
    resume: MasterResume,
    conn: sqlite3.Connection,
    *,
    out_dir: Path = MASTER_RESUME_DIR,
    source_file_path: str | None = None,
) -> tuple[str, Path]:
    """Persist the (user-corrected) master JSON and register it in resume_versions.
    Returns (content_hash, path). No-ops the DB insert if this exact hash already exists.

    source_file_path, when given, is the original uploaded resume file (PDF/DOCX)
    exactly as uploaded — per explicit request, this is what actually gets
    attached to job applications now, never a regenerated document. Kept
    separate from json_path (the structured data used for scoring/matching)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    h = content_hash(resume)
    path = out_dir / "master_resume.json"
    path.write_text(json.dumps(resume.model_dump(), indent=2))

    existing = conn.execute(
        "SELECT id FROM resume_versions WHERE content_hash = ?", (h,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO resume_versions (content_hash, json_path, source_file_path, created_at) VALUES (?, ?, ?, ?)",
            (h, str(path), source_file_path, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    elif source_file_path:
        conn.execute(
            "UPDATE resume_versions SET source_file_path = ? WHERE id = ?", (source_file_path, existing["id"])
        )
        conn.commit()
    return h, path


def get_active_resume_file_path(conn: sqlite3.Connection) -> Path | None:
    """The original uploaded resume file (PDF/DOCX) from the most recent
    resume_versions row — what actually gets attached to job applications."""
    row = conn.execute(
        "SELECT source_file_path FROM resume_versions WHERE source_file_path IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return Path(row["source_file_path"]) if row and row["source_file_path"] else None


def load_master_resume(path: Path = MASTER_RESUME_DIR / "master_resume.json") -> MasterResume | None:
    if not path.exists():
        return None
    return MasterResume.model_validate(json.loads(path.read_text()))


def parse_resume_file(path: Path, *, conn: sqlite3.Connection | None = None) -> MasterResume:
    raw_text = extract_text(path)
    return structure_resume(raw_text, conn=conn)
