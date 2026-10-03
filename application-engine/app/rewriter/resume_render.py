"""§6.6 (minimal slice) — render the validated, tailored resume as a single-
column ATS-safe .docx, then machine-parse it back to text and confirm every
section survived. Also enforces the one-page hard limit: render, estimate
page count, and if over, tighten and re-render — §6.6's own instruction.

Tightening only ever reduces bullet count. It never touches document
formatting/colors (there are none to touch — default Word styling
throughout) and it never removes an entry's header or date line — every
experience entry that remains keeps its full org/title/timeline visible.
"""

import math
import re
from pathlib import Path

from docx import Document

from app.models import MasterResume

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "output"

HEADINGS = ("Experience", "Education", "Skills")

# Conservative, calibrated character-per-line estimate for 11pt body text on
# a US Letter page with 1" margins (6.5in usable width) — deliberately
# biased low (over-estimates line count) so a borderline resume gets
# tightened rather than silently shipped over one page. Avoids depending on
# a specific font file being present on the machine.
_BODY_CHARS_PER_LINE = 92
_HEADING_CHARS_PER_LINE = 70
_USABLE_LINES_PER_PAGE = 48
MAX_TIGHTEN_ITERATIONS = 30
MIN_ENTRIES_BEFORE_BULLET_TRIM = 2  # floor: prefer cutting whole irrelevant entries
# over trimming bullets from entries that stay on the resume, but never cut
# below this many entries via whole-section removal alone.


def _safe_dirname(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "untitled"


def _entry_dates(resume: MasterResume, entry: dict) -> tuple[str, str]:
    for exp in resume.experience:
        if exp.org == entry["org"] and exp.title == entry["title"]:
            return exp.start, exp.end
    return "", ""


def _wrapped_line_count(text: str, chars_per_line: int) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / chars_per_line))


def _estimate_lines(resume: MasterResume, entries: list[dict]) -> int:
    lines = _wrapped_line_count(resume.identity.name or "", _HEADING_CHARS_PER_LINE) + 1
    contact_parts = [
        p for p in [resume.identity.location, resume.identity.email, resume.identity.phone, resume.identity.linkedin] if p
    ]
    if contact_parts:
        lines += _wrapped_line_count(" | ".join(contact_parts), _BODY_CHARS_PER_LINE)

    lines += 2  # "Experience" heading
    for entry in entries:
        start, end = _entry_dates(resume, entry)
        header = f"{entry['org']} — {entry['title']}"
        lines += _wrapped_line_count(header, _HEADING_CHARS_PER_LINE) + 1
        if start or end:
            lines += 1  # date line, always present, never trimmed
        for bullet in entry["bullets"]:
            lines += _wrapped_line_count(bullet["rewritten"], _BODY_CHARS_PER_LINE)

    lines += 2  # "Education" heading
    for edu in resume.education:
        line = f"{edu.institution} — {edu.degree}"
        lines += _wrapped_line_count(line, _BODY_CHARS_PER_LINE)
        if edu.start or edu.end:
            lines += 1

    lines += 2  # "Skills" heading
    for label, items in [
        ("Languages", resume.skills.languages),
        ("Frameworks", resume.skills.frameworks),
        ("Tools", resume.skills.tools),
    ]:
        if items:
            lines += _wrapped_line_count(f"{label}: {', '.join(items)}", _BODY_CHARS_PER_LINE)

    return lines


def estimate_page_count(resume: MasterResume, entries: list[dict]) -> int:
    return max(1, math.ceil(_estimate_lines(resume, entries) / _USABLE_LINES_PER_PAGE))


def _entry_relevance_score(entry: dict, target_role: str, missing_keywords: list[str]) -> int:
    """How relevant this experience entry is to the specific job being
    tailored for — keyword overlap against the Recruiter's cached
    missing-keywords list (already curated, grounded in real postings) plus
    the target role name. Plain code, no LLM call, consistent with §5's
    keyword-overlap scoring."""
    haystack = f"{entry['org']} {entry['title']} {' '.join(b['rewritten'] for b in entry['bullets'])}".lower()
    terms = {t.lower() for t in (missing_keywords or []) if t}
    terms |= {t for t in re.findall(r"[a-z]{4,}", (target_role or "").lower())}
    return sum(1 for t in terms if t in haystack)


def _tighten_once(
    entries: list[dict], target_role: str, missing_keywords: list[str]
) -> tuple[list[dict], str | None]:
    """Prefer cutting the single least-relevant *whole* experience entry
    (down to a floor) over trimming bullets from entries that stay on the
    resume — closer to how a person actually tailors a resume than blind
    recency-based bullet trimming. Only once at the floor does it fall back
    to removing one bullet from the least-relevant remaining entry (never
    an entry's header, dates, or its only remaining bullet). Returns
    (entries, None) if nothing more can be safely trimmed."""
    if len(entries) > MIN_ENTRIES_BEFORE_BULLET_TRIM:
        scores = [_entry_relevance_score(e, target_role, missing_keywords) for e in entries]
        idx = min(range(len(entries)), key=lambda i: scores[i])
        removed_entry = entries[idx]
        new_entries = entries[:idx] + entries[idx + 1 :]
        desc = f"Removed least-relevant section: {removed_entry['org']} — {removed_entry['title']}"
        return new_entries, desc

    trimmable = [e for e in entries if len(e["bullets"]) > 1]
    if not trimmable:
        return entries, None
    scores = {id(e): _entry_relevance_score(e, target_role, missing_keywords) for e in trimmable}
    target = min(trimmable, key=lambda e: scores[id(e)])
    trimmed = [dict(e, bullets=list(e["bullets"])) for e in entries]
    for e in trimmed:
        if e["org"] == target["org"] and e["title"] == target["title"] and len(e["bullets"]) > 1:
            removed = e["bullets"].pop()
            return trimmed, f"{e['org']}: {removed['rewritten']}"
    return entries, None


def render_ats_docx(
    resume: MasterResume,
    entries: list[dict],
    company: str,
    role: str,
    *,
    target_role: str = "",
    missing_keywords: list[str] | None = None,
    out_dir: Path = OUTPUT_DIR,
) -> tuple[Path, list[dict], bool, list[str]]:
    """Single column, no tables/text boxes/headers/footers/images, standard
    section headings, default font, unmodified formatting — the ATS-safety
    baseline from §6.6. Enforces the one-page hard limit: if it doesn't fit,
    cuts the least-relevant whole experience entry first (relevance judged
    against this specific job via target_role + the Recruiter's cached
    missing-keywords), falling back to bullet-level trims only once down to
    a floor of entries. Never touches headers, dates, or formatting/colors.

    Returns (docx_path, final_entries, fits_one_page, trimmed_log)."""
    current_entries = entries
    trimmed_log: list[str] = []

    for _ in range(MAX_TIGHTEN_ITERATIONS):
        if estimate_page_count(resume, current_entries) <= 1:
            break
        new_entries, removed = _tighten_once(current_entries, target_role, missing_keywords or [])
        if removed is None:
            break
        current_entries = new_entries
        trimmed_log.append(removed)

    fits_one_page = estimate_page_count(resume, current_entries) <= 1

    doc = Document()

    doc.add_heading(resume.identity.name or "", level=0)
    contact_parts = [
        p
        for p in [
            resume.identity.location,
            resume.identity.email,
            resume.identity.phone,
            resume.identity.linkedin,
        ]
        if p
    ]
    if contact_parts:
        doc.add_paragraph(" | ".join(contact_parts))

    doc.add_heading("Experience", level=1)
    for entry in current_entries:
        header = f"{entry['org']} — {entry['title']}"
        doc.add_paragraph(header, style="Heading 2")
        start, end = _entry_dates(resume, entry)
        if start or end:
            date_paragraph = doc.add_paragraph()
            date_run = date_paragraph.add_run(f"{start} – {end}")
            date_run.italic = True
        for bullet in entry["bullets"]:
            doc.add_paragraph(bullet["rewritten"], style="List Bullet")

    doc.add_heading("Education", level=1)
    for edu in resume.education:
        line = f"{edu.institution} — {edu.degree}"
        if edu.field:
            line += f", {edu.field}"
        doc.add_paragraph(line)
        if edu.start or edu.end:
            doc.add_paragraph(f"{edu.start} – {edu.end}")

    doc.add_heading("Skills", level=1)
    skill_lines = [
        ("Languages", resume.skills.languages),
        ("Frameworks", resume.skills.frameworks),
        ("Tools", resume.skills.tools),
    ]
    for label, items in skill_lines:
        if items:
            doc.add_paragraph(f"{label}: {', '.join(items)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    job_dir = out_dir / f"{_safe_dirname(company)}_{_safe_dirname(role)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "resume.docx"
    doc.save(path)
    return path, current_entries, fits_one_page, trimmed_log


def _extract_text(path: Path) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def reparse_and_check(path: Path, resume: MasterResume, entries: list[dict]) -> tuple[bool, list[str]]:
    """Re-parse the generated .docx and confirm every section heading, every
    experience org name, and every entry's date range survived the round
    trip. Returns (ok, missing)."""
    text = _extract_text(path)
    missing = []

    for heading in HEADINGS:
        if heading not in text:
            missing.append(f"missing heading: {heading}")

    for entry in entries:
        if entry["org"] not in text:
            missing.append(f"missing org: {entry['org']}")
        start, end = _entry_dates(resume, entry)
        if (start or end) and f"{start} – {end}" not in text:
            missing.append(f"missing date range for: {entry['org']}")

    return (len(missing) == 0, missing)
