"""§9B Hiring manager email — sent after submission, not instead of it.
Under 150 words, includes the application reference and exact role title,
subject format "{Role} — application {REF} — {My Name}". No attachments in
the first email. One follow-up permitted after 7 days if no reply, then
stop permanently — enforced in code, not just prompted.

Template-first per §10: four fixed skeletons, with only the one
company-specific sentence generated per job (~40 tokens out) rather than
writing the whole email from scratch every time — the single largest
per-outreach cost saving available, same principle as the Recruiter cache.
"""

import random
import sqlite3
from datetime import datetime, timedelta, timezone

from app.llm import MODEL_HAIKU, cached_system, call_text
from app.models import MasterResume

FOLLOW_UP_WAIT_DAYS = 7
WORD_LIMIT = 150

_TEMPLATES = [
    """Hi {contact_greeting},

I recently applied for the {role} position at {company} (application {ref}). {hook}

I'd welcome the chance to discuss how my background could contribute to the team. Thank you for your time and consideration.

Best,
{my_name}""",
    """Hello {contact_greeting},

I wanted to follow up on my application for {role} at {company} (ref {ref}). {hook}

Happy to share more about my background whenever convenient — thank you for considering my application.

Best regards,
{my_name}""",
    """Hi {contact_greeting},

I recently submitted my application for the {role} role at {company} (application {ref}). {hook}

I'd love the opportunity to talk more about how I could contribute. Thanks for your time.

Sincerely,
{my_name}""",
    """Hello {contact_greeting},

I applied for the {role} position at {company} this week (application {ref}). {hook}

I'd appreciate the chance to connect and learn more about the team's work. Thank you for your consideration.

Best,
{my_name}""",
]

_FOLLOW_UP_TEMPLATE = """Hi {contact_greeting},

Following up on my application for {role} at {company} (application {ref}) — I know things get busy, so just wanted to make sure it didn't get lost. {hook}

Thank you again for your time.

Best,
{my_name}"""

_HOOK_SYSTEM_PROMPT = """Write exactly one sentence (under 30 words) connecting
the candidate's strongest, most relevant piece of experience to what this
specific job posting asks for. Write it in FIRST PERSON, as the candidate
speaking about their own work ("my experience building...", never "Dev's
experience" or any third-person reference to the candidate by name) — this
sentence is inserted directly into an email the candidate is sending.
Ground it only in facts present in the resume and posting given — never
invent anything. Output only the sentence, no preamble."""

# Only ever added when the contact has been separately confirmed as a
# fellow alum (contacts.is_alumni) — this instruction never introduces the
# fact itself, it just permits mentioning a fact that's already true.
_ALUMNI_HOOK_ADDENDUM = """The recipient is a fellow alum of {school} — you
may naturally weave in a brief, first-person mention of that shared
connection (e.g. "as a fellow {school} student/alum...") alongside the
experience point, still in one sentence under 30 words. Do not overstate the
connection or claim anything about the recipient you weren't told."""


def _generate_hook(
    resume: MasterResume,
    job: dict,
    conn: sqlite3.Connection,
    *,
    is_alumni: bool = False,
    alumni_school: str | None = None,
) -> str:
    prompt = f"""Job posting (trimmed):
{(job.get('description_raw') or '')[:1500]}

Write the one-sentence hook using the candidate's resume given in the system prompt."""
    system_parts = [_HOOK_SYSTEM_PROMPT]
    if is_alumni and alumni_school:
        system_parts.append(_ALUMNI_HOOK_ADDENDUM.format(school=alumni_school))
    system_parts.append(f"Candidate's resume:\n{resume.model_dump_json(indent=2)}")
    return call_text(
        prompt,
        stage="outreach_hook",
        model=MODEL_HAIKU,
        system=cached_system(*system_parts),
        max_tokens=100,
        conn=conn,
        job_id=job.get("id"),
    ).strip()


def build_subject(role: str, application_ref: str, my_name: str) -> str:
    return f"{role} — application {application_ref} — {my_name}"


def generate_hiring_manager_email(
    resume: MasterResume,
    job: dict,
    application_ref: str,
    conn: sqlite3.Connection,
    *,
    contact_name: str | None = None,
    is_follow_up: bool = False,
    is_alumni: bool = False,
    alumni_school: str | None = None,
) -> dict:
    hook = _generate_hook(resume, job, conn, is_alumni=is_alumni, alumni_school=alumni_school)
    contact_greeting = contact_name.split()[0] if contact_name else "there"
    template = _FOLLOW_UP_TEMPLATE if is_follow_up else random.choice(_TEMPLATES)

    body = template.format(
        contact_greeting=contact_greeting,
        role=job["title"],
        company=job["company"],
        ref=application_ref,
        hook=hook,
        my_name=resume.identity.name,
    )
    subject = build_subject(job["title"], application_ref, resume.identity.name)
    return {"subject": subject, "body": body, "word_count": len(body.split())}


def can_send_follow_up(outreach_row: dict) -> tuple[bool, str]:
    """One follow-up, ever, only after 7+ days with no reply recorded. Once
    follow_up_sent_at is set, a second follow-up is structurally impossible."""
    if outreach_row.get("follow_up_sent_at"):
        return False, "A follow-up has already been sent for this outreach — stopping permanently, per policy."
    if not outreach_row.get("sent_at"):
        return False, "The original email hasn't been sent yet."
    sent_at = datetime.fromisoformat(outreach_row["sent_at"])
    if datetime.now(timezone.utc) - sent_at < timedelta(days=FOLLOW_UP_WAIT_DAYS):
        return False, f"Fewer than {FOLLOW_UP_WAIT_DAYS} days have passed since the original email."
    return True, ""
