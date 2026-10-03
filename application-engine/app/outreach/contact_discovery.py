"""§8 Stage 6 — contact discovery. Priority order per spec:
  (a) recruiter/hiring manager named in the posting itself
  (b) the company's own team/about/leadership page
  (c) LinkedIn — never automated (bot-detection risk identical to the
      Cloudflare wall Stage 5 correctly refuses to touch, and LinkedIn's own
      terms prohibit this kind of scraping); instead we hand the candidate a
      ready-made search URL and flag NEEDS_HUMAN.

Hard limits enforced here: public sources only, no data brokers, name +
current role + public URL only (never a full profile), one contact attempt
per company+role, no personal/home contact details.
"""

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from app.models import MasterResume
from app.scoring.dedupe import normalize_company, normalize_title

USER_AGENT = "application-engine/0.1 (personal job search tool; contact via LinkedIn profile owner)"
REQUEST_TIMEOUT = 10.0

_TEAM_PAGE_PATHS = ["/about", "/team", "/leadership", "/about-us", "/company", "/company/team"]

# Posting-text patterns for an explicitly named contact (priority a).
_POSTING_CONTACT_PATTERNS = [
    re.compile(r"(?:Hiring Manager|Recruiter|Contact)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)", re.I),
    re.compile(r"reach out to\s+([A-Z][a-z]+ [A-Z][a-z]+)", re.I),
]
_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Very rough "Name — Title" / "Name, Title" heuristic for team/about pages.
# Deliberately conservative: requires a capitalized two-word name followed by
# a short title-like phrase, to keep false-positive rate low on prose pages.
_NAME_TITLE_PATTERN = re.compile(
    r"\b([A-Z][a-zA-Z'-]+ [A-Z][a-zA-Z'-]+)\s*[—\-,|]\s*"
    r"((?:Chief|VP|Vice President|Head of|Director of|Senior|Lead|Engineering|Talent|Recruit\w*|Hiring|People)[^.\n]{0,60})",
)

# Alumni outreach (§8/§9 enrichment): a bio on the company's own team/about
# page that mentions the candidate's alma mater, near a name, flags a
# genuine fellow alum at the company — a far warmer contact than a cold
# recruiter reach-out. Kept school-specific per the candidate's actual
# education rather than generalized, since a partial/loose school-name match
# (e.g. bare "Illinois") would false-positive on unrelated bios constantly.
UIUC_ALIASES = [
    "University of Illinois Urbana-Champaign",
    "University of Illinois at Urbana-Champaign",
    "UIUC",
    "Illinois Urbana-Champaign",
]
_ALUMNI_WINDOW_CHARS = 300  # bio-container window around an already-vetted name/title match
# Common two-capitalized-word false positives ("Our VP", "Computer Science",
# "Head of") that match the bare name shape but aren't people's names.
_ALUMNI_NAME_STOPWORDS = {
    "our", "the", "his", "her", "their", "vp", "ceo", "cto", "coo", "cfo", "hr",
    "chief", "head", "senior", "junior", "director", "manager", "lead", "team",
    "engineering", "product", "founder", "cofounder", "president", "vice",
    "computer", "science", "data", "business", "software", "electrical",
    "mechanical", "systems", "information", "school", "college", "university",
}


def _plausible_person_name(candidate: str) -> bool:
    words = candidate.split()
    if len(words) != 2:
        return False
    return not any(w.lower() in _ALUMNI_NAME_STOPWORDS or (w.isupper() and len(w) <= 3) for w in words)


def _guess_company_domain(job: dict) -> str | None:
    """The job's own URL is almost always ATS-hosted (boards.greenhouse.io,
    jobs.lever.co), not the company's real domain, so it can't be used
    directly. Fall back to a plain slug guess — good enough to attempt a
    read-only fetch; a wrong guess just yields no page, not an error."""
    company_slug = re.sub(r"[^a-z0-9]", "", job["company"].lower())
    return f"{company_slug}.com" if company_slug else None


def extract_contact_from_posting(description_raw: str) -> dict | None:
    """Priority (a): a recruiter/hiring manager named directly in the posting."""
    if not description_raw:
        return None
    for pattern in _POSTING_CONTACT_PATTERNS:
        match = pattern.search(description_raw)
        if match:
            email_match = _EMAIL_PATTERN.search(description_raw[max(0, match.start() - 100) : match.end() + 200])
            return {
                "name": match.group(1),
                "role": "",
                "email": email_match.group(0) if email_match else None,
                "source_url": None,
                "kind": "named_in_posting",
            }
    return None


def fetch_company_team_page(domain: str) -> tuple[str, str] | None:
    """Priority (b): try a handful of common team/about page paths on the
    company's own site. Returns (url, html) for the first one that responds,
    or None. Read-only GET requests only — same posture as Stage 4's
    discovery polling, no login, no interaction."""
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        for path in _TEAM_PAGE_PATHS:
            url = f"https://{domain}{path}"
            try:
                resp = client.get(url)
            except httpx.RequestError:
                continue
            if resp.status_code == 200:
                return url, resp.text
    return None


def extract_people_from_page(html: str) -> list[dict]:
    """Best-effort extraction of (name, title) pairs from a team/about page.
    Deliberately conservative — a missed contact just falls through to the
    LinkedIn-fallback step, which is safe; a wrong extraction would not be."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    people = []
    seen_names = set()
    for match in _NAME_TITLE_PATTERN.finditer(text):
        name, title = match.group(1).strip(), match.group(2).strip()
        if name not in seen_names:
            seen_names.add(name)
            people.append({"name": name, "role": title})
    return people[:10]  # never scrape more than a handful — we only need one plausible contact


def build_linkedin_search_url(company: str, role_hint: str = "recruiter") -> str:
    """Priority (c) fallback — a ready-made LinkedIn people-search URL for
    the candidate to open and check themselves. The engine never queries
    LinkedIn programmatically."""
    query = quote_plus(f"{company} {role_hint}")
    return f"https://www.linkedin.com/search/results/people/?keywords={query}"


def build_alumni_search_url(company: str, school: str = "University of Illinois Urbana-Champaign") -> str:
    """Same manual-check posture as build_linkedin_search_url — never
    queried automatically — just narrowed to a much warmer target: someone
    from the candidate's own school already working at this company."""
    query = quote_plus(f"{company} {school}")
    return f"https://www.linkedin.com/search/results/people/?keywords={query}"


def find_alumni_on_page(html: str, school_aliases: list[str] | None = None) -> list[dict]:
    """Best-effort: scan a team/about page for the candidate's alma mater
    mentioned in the same bio as a name. Two candidate sources, both
    deliberately conservative — free text is full of capitalized-word-pairs
    that aren't people ("Computer Science", "Head of"), so a plain nearest-
    match search over raw text is too false-positive-prone to trust:

    1. Structural: a heading tag (h1-h6) whose own text is a plausible
       person's name, checked against the bio text that follows it in
       document order up to the *next* heading — bounded this way (rather
       than the whole containing block) so it works whether each person is
       individually wrapped in their own card div or just a flat sequence
       of sibling heading/paragraph tags; the latter would otherwise leak
       one person's bio into the next one's window.
    2. Text-pattern: names already vetted by _NAME_TITLE_PATTERN (i.e.
       genuinely followed by a real title, not just any two capitalized
       words), checked against a window of surrounding text.

    A missed hit just falls back to alumni_search_url for a manual check,
    which is safe; a wrong extraction claiming someone is an alum when they
    aren't would not be."""
    aliases = school_aliases or UIUC_ALIASES
    school_pattern = re.compile("|".join(re.escape(a) for a in aliases), re.IGNORECASE)
    soup = BeautifulSoup(html, "html.parser")
    _heading_re = re.compile(r"^h[1-6]$")

    hits = []
    seen_names = set()

    for heading in soup.find_all(_heading_re):
        name = heading.get_text(strip=True)
        if not _plausible_person_name(name) or name in seen_names:
            continue
        bio_parts = []
        bio_len = 0
        for node in heading.next_elements:
            if isinstance(node, Tag) and _heading_re.match(node.name or ""):
                break  # next person's heading — stop, don't leak into their bio
            if isinstance(node, NavigableString):
                piece = str(node).strip()
                if piece:
                    bio_parts.append(piece)
                    bio_len += len(piece)
            if bio_len > _ALUMNI_WINDOW_CHARS:
                break
        school_match = school_pattern.search(" ".join(bio_parts))
        if school_match:
            seen_names.add(name)
            hits.append({"name": name, "school_mention": school_match.group(0)})

    text = soup.get_text(separator="\n")
    for name_title_match in _NAME_TITLE_PATTERN.finditer(text):
        name = name_title_match.group(1)
        if name in seen_names:
            continue
        window_start = max(0, name_title_match.start() - _ALUMNI_WINDOW_CHARS)
        window_end = min(len(text), name_title_match.end() + _ALUMNI_WINDOW_CHARS)
        school_match = school_pattern.search(text[window_start:window_end])
        if school_match:
            seen_names.add(name)
            hits.append({"name": name, "school_mention": school_match.group(0)})

    return hits[:5]


def _candidate_schools(resume: MasterResume | None) -> list[str]:
    if resume is None:
        return []
    return [edu.institution for edu in resume.education if edu.institution]


def discover_contact(job: dict, conn: sqlite3.Connection, resume: MasterResume | None = None) -> dict:
    """Run the full priority chain for one job, persist the result, and
    return it. Enforces one contact attempt per company+role: if a contact
    already exists for this normalized company+title, reuse it rather than
    searching again.

    Also runs an independent alumni-outreach check (§8/§9 enrichment): a
    fellow alum spotted in a company team-page bio, or at minimum a
    ready-made alumni-targeted search link, regardless of which priority
    tier found the primary contact — the two are separate signals (one is
    "who do I address this to," the other is "who's a warm intro")."""
    company_norm = normalize_company(job["company"])
    title_norm = normalize_title(job["title"])

    existing = conn.execute(
        """
        SELECT c.* FROM contacts c
        JOIN jobs j ON j.id = c.job_id
        WHERE ? = (SELECT company_norm FROM jobs WHERE id = j.id)
          AND ? = (SELECT title_norm FROM jobs WHERE id = j.id)
        LIMIT 1
        """,
        (company_norm, title_norm),
    ).fetchone()
    if existing is not None:
        return dict(existing)

    now = datetime.now(timezone.utc).isoformat()
    contact = extract_contact_from_posting(job.get("description_raw", ""))
    if contact:
        contact["source_url"] = job.get("url")

    team_page = None
    domain = _guess_company_domain(job)
    if domain:
        team_page = fetch_company_team_page(domain)

    if contact is None and team_page:
        url, html = team_page
        people = extract_people_from_page(html)
        if people:
            top = people[0]
            contact = {
                "name": top["name"], "role": top["role"], "email": None,
                "source_url": url, "kind": "company_site",
            }

    needs_human = contact is None
    needs_human_reason = None
    linkedin_url = None
    if needs_human:
        linkedin_url = build_linkedin_search_url(job["company"], job["title"])
        needs_human_reason = (
            "No contact found in the posting or on the company's site. "
            f"Manual LinkedIn search suggested: {linkedin_url}"
        )
        contact = {"name": None, "role": None, "email": None, "source_url": None, "kind": "linkedin_manual"}

    schools = _candidate_schools(resume)
    alumni_school = schools[0] if schools else UIUC_ALIASES[0]
    alumni_search_url = build_alumni_search_url(job["company"], alumni_school)
    is_alumni = False
    alumni_name_hint = None
    alumni_source = None
    if team_page:
        _, html = team_page
        alumni_hits = find_alumni_on_page(html, schools or UIUC_ALIASES)
        if alumni_hits:
            is_alumni = True
            alumni_source = "company_site_bio"
            alumni_name_hint = alumni_hits[0]["name"]

    cursor = conn.execute(
        """
        INSERT INTO contacts
            (job_id, name, role, email, email_confidence, linkedin_url, source_url,
             kind, discovered_at, needs_human, needs_human_reason,
             is_alumni, alumni_school, alumni_search_url, alumni_name_hint, alumni_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["id"], contact.get("name"), contact.get("role"), contact.get("email"), None,
            linkedin_url, contact.get("source_url"), contact.get("kind"), now,
            int(needs_human), needs_human_reason,
            int(is_alumni), alumni_school, alumni_search_url, alumni_name_hint, alumni_source,
        ),
    )
    conn.commit()

    result = dict(contact)
    result.update({
        "id": cursor.lastrowid, "job_id": job["id"], "needs_human": needs_human, "linkedin_url": linkedin_url,
        "is_alumni": is_alumni, "alumni_school": alumni_school, "alumni_search_url": alumni_search_url,
        "alumni_name_hint": alumni_name_hint, "alumni_source": alumni_source,
    })
    return result


def confirm_alumni(conn: sqlite3.Connection, contact_id: int, name: str | None = None) -> None:
    """LinkedIn's own alumni tool can't be queried automatically (same
    posture as the rest of this module) — a human checks alumni_search_url
    themselves and confirms what they found here. Only ever sets is_alumni
    True; there's no un-confirm, since the fact a candidate found *someone*
    doesn't stop being true."""
    conn.execute(
        "UPDATE contacts SET is_alumni = 1, alumni_source = 'human_confirmed', "
        "alumni_name_hint = COALESCE(?, alumni_name_hint) WHERE id = ?",
        (name.strip() if name and name.strip() else None, contact_id),
    )
    conn.commit()
