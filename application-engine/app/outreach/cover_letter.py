"""§9A Cover letter — generated only when the application requires or offers
one. Grounded in one concrete, verifiable detail from the company's own
site (never generic praise, and never a model-invented "detail"), the
single strongest match between the candidate's background and the role's
stated need, and a close. Under 300 words. Same truth validator as the
Rewriter, adapted for free text.
"""

import httpx
from bs4 import BeautifulSoup

from app.llm import MODEL_SONNET, cached_system, call_structured
from app.models import MasterResume

USER_AGENT = "application-engine/0.1 (personal job search tool; contact via LinkedIn profile owner)"
REQUEST_TIMEOUT = 10.0
MAX_GROUNDING_CHARS = 3000
WORD_LIMIT = 300

SYSTEM_PROMPT = """You are writing a cover letter for a candidate. Structure:
1. Why this company specifically — ONE concrete, verifiable detail drawn only
   from the "Company grounding content" you're given (a real product feature,
   engineering blog post, or recent announcement). Never generic praise like
   "innovative" or "great culture." If no grounding content is provided, draw
   the detail from the job posting itself rather than inventing one.
2. The single strongest match between the candidate's background and the
   role's stated need, with concrete evidence from their actual resume.
3. A brief close.

Under 300 words total. Never invent anything about the candidate's
background that isn't in their resume — a fabricated cover letter claim is
the same violation as a fabricated resume bullet."""

COVER_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "letter_text": {"type": "string"},
        "company_detail_used": {
            "type": "string",
            "description": "The specific grounding detail used in point 1, quoted or closely paraphrased.",
        },
    },
    "required": ["letter_text", "company_detail_used"],
    "additionalProperties": False,
}


def fetch_company_grounding(domain: str) -> str | None:
    """Best-effort fetch of the company's own homepage as grounding content
    for the "why this company" hook — never the model's own assumption."""
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(f"https://{domain}")
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:MAX_GROUNDING_CHARS] if text else None
    except httpx.RequestError:
        return None


def _build_prompt(job: dict, grounding: str | None) -> str:
    grounding_text = grounding or "(none available — draw the detail from the job posting below instead)"
    return f"""Target role: {job['title']} at {job['company']}

Job posting (trimmed):
{(job.get('description_raw') or '')[:2000]}

Company grounding content (from the company's own site):
{grounding_text}

Write the cover letter per your instructions, using the candidate's resume
given in the system prompt."""


def generate_cover_letter(resume: MasterResume, job: dict, conn, *, grounding: str | None = None) -> dict:
    prompt = _build_prompt(job, grounding)
    result = call_structured(
        prompt,
        COVER_LETTER_SCHEMA,
        tool_name="emit_cover_letter",
        stage="cover_letter",
        model=MODEL_SONNET,
        system=cached_system(SYSTEM_PROMPT, f"Candidate's resume:\n{resume.model_dump_json(indent=2)}"),
        max_tokens=1024,
        conn=conn,
        job_id=job.get("id"),
    )
    result["word_count"] = len(result["letter_text"].split())
    return result
