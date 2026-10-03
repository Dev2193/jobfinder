"""Fit score 0-100, computed with plain code — no model call (§5). This is the
single biggest token saver: it filters the funnel before any LLM sees a posting.
"""

import re
from datetime import datetime, timezone

from app.config import SearchFilters, expand_keywords, target_role_text
from app.models import MasterResume

SENIORITY_MARKERS = {
    "intern": ["intern", "internship", "co-op", "coop"],
    "entry": ["entry level", "entry-level", "new grad", "graduate", "junior", "jr"],
    "mid": ["mid level", "mid-level", "ii"],
    "senior": [
        "senior", "sr", "staff", "principal", "lead", "manager", "director",
        "vp", "vice president", "chief", "president", "head of",
        "executive director", "chief executive", "executive vp",
    ],
}

# Word-boundary matching, not substring containment — plain `marker in title`
# false-positives constantly (e.g. "intern" inside "Internal Audit", "ii"
# inside "Engineering"). Trailing "."/space in a marker is stripped before
# building the pattern since \b already matches at a word/non-word transition
# (including right before a period or space).
_SENIORITY_PATTERNS = {
    level: [re.compile(rf"\b{re.escape(m.rstrip('. '))}\b") for m in markers]
    for level, markers in SENIORITY_MARKERS.items()
}

# Best-effort keyword flags for §5's auto-skip list. These are heuristics on free
# text, not certainties — they bias toward skipping obvious junk, not toward
# perfect precision. A false skip is reviewable (job stays in the DB); a missed
# skip just costs one more scored-but-low-fit row.
_UNPAID_RE = re.compile(r"\bunpaid\b", re.IGNORECASE)
_MLM_RE = re.compile(
    r"\b(commission[\s-]only|multi[\s-]level marketing|\bmlm\b|be your own boss|network marketing|1099 independent contractor)\b",
    re.IGNORECASE,
)
_STAFFING_REPOST_RE = re.compile(
    r"\b(on behalf of our client|confidential client|our client is seeking|staffing agency)\b",
    re.IGNORECASE,
)
KNOWN_STAFFING_AGENCIES = {
    "robert half", "randstad", "insight global", "teksystems", "kforce",
    "aerotek", "adecco", "kelly services", "manpower", "cybercoders",
}

POSTED_WITHIN_DAYS = {"24h": 1, "3d": 3, "7d": 7, "14d": 14, "30d": 30}

# US-location detection, for excluded_countries (§5 hard exclusions). Best
# effort against real job-board location strings, which are inconsistent —
# "US-San Francisco", "United States", bare "US", "San Francisco, CA",
# "NYC, SF", "Remote in the US" all need to match; "IN-Bengaluru", "Toronto",
# "United Kingdom" must not. Verified against real discovered-job location
# strings before shipping (see fit_score tests / live pipeline run).
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_US_CITY_FULLNAMES = [
    "san francisco", "south san francisco", "new york", "chicago", "seattle",
    "los angeles", "austin", "denver", "atlanta", "washington dc",
    "washington, dc", "boston", "san diego", "mountain view", "menlo park",
    "palo alto", "san jose", "philadelphia", "miami", "dallas", "houston",
    "portland", "minneapolis", "detroit", "phoenix", "nashville", "raleigh",
    "durham", "pittsburgh", "columbus", "salt lake city",
]
_US_TEXT_RE = re.compile(r"\b(united states|usa)\b", re.IGNORECASE)
_US_TOKEN_RE = re.compile(r"\bUS\b")  # case-sensitive: bare "US"/"US-..." only, not "us" as a word inside other text
_US_STATE_SUFFIX_RE = re.compile(r",\s*(?:" + "|".join(_US_STATE_ABBR) + r")\b")
# Case-sensitive city abbreviations: lowercase "sf"/"la"/"ny" etc. collide with
# real words far too often to match case-insensitively.
_US_CITY_ABBR_RE = re.compile(r"\b(NYC|SF|LA|SD|SEA|CHI|NY)\b")
_US_CITY_FULLNAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in _US_CITY_FULLNAMES) + r")\b", re.IGNORECASE
)


def is_us_location(location: str) -> bool:
    """True if the location string clearly indicates a US location — used to
    hard-exclude US postings when the candidate is searching outside the US.
    A miss just leaves one extra row to score normally (safe failure mode);
    over-matching is the risk we guard against instead, hence case-sensitive
    abbreviations and full-word matching throughout."""
    if not location:
        return False
    return bool(
        _US_TEXT_RE.search(location)
        or _US_TOKEN_RE.search(location)
        or _US_STATE_SUFFIX_RE.search(location)
        or _US_CITY_ABBR_RE.search(location)
        or _US_CITY_FULLNAME_RE.search(location)
    )


_COUNTRY_US_ALIASES = {"united states", "us", "usa"}

# Undergrad-appropriate hard filters (§5). These are auto-skips, not scoring
# penalties — a "Director of Engineering" with strong keyword overlap should
# never surface at all, not just rank lower, and a role requiring 8+ years
# or a required PhD is not something an undergrad can truthfully apply to.
_YEARS_REQUIRED_RE = re.compile(
    r"(\d{1,2})\+?\s*(?:[\-–]\s*\d{1,2}|\s*to\s*\d{1,2})?\s*\+?\s*years?\s*(?:of\s+)?(?:[\w-]+\s+){0,2}experience",
    re.IGNORECASE,
)
_ADVANCED_DEGREE_REQUIRED_RE = re.compile(
    r"\b(ph\.?d\.?|doctorate|master'?s degree|m\.?s\.? degree)\b[^.]{0,40}\b(required|is required|must have|mandatory)\b"
    r"|\brequire[sd]?\b[^.]{0,40}\b(ph\.?d\.?|doctorate|master'?s degree|m\.?s\.? degree)\b",
    re.IGNORECASE,
)


def detect_min_years_required(description: str) -> int | None:
    """Lowest 'N years experience' figure explicitly stated, if any. Best
    effort — a posting phrased unusually just won't match, which is a safe
    failure mode (falls through to normal scoring rather than a bad skip)."""
    matches = _YEARS_REQUIRED_RE.findall(description or "")
    if not matches:
        return None
    return min(int(m) for m in matches)


def requires_advanced_degree(description: str) -> bool:
    """True only for an explicit hard requirement ('PhD required',
    'requires a Master's degree') — never for 'preferred' or 'a plus'
    phrasing, which an undergrad can still reasonably apply against."""
    return bool(_ADVANCED_DEGREE_REQUIRED_RE.search(description or ""))


def classify_auto_skip(job: dict, cfg: SearchFilters | None = None, resume: MasterResume | None = None) -> str | None:
    text = f"{job.get('title', '')} {job.get('description_raw', '')}"
    employment_type = job.get("employment_type") or ""
    is_internship = "intern" in (
        detect_seniority(job.get("title", "")),
        detect_seniority(employment_type),
    )

    if is_internship and _UNPAID_RE.search(text):
        return "unpaid_internship"
    if _MLM_RE.search(text):
        return "mlm_or_commission_only"
    if job.get("company", "").lower() in KNOWN_STAFFING_AGENCIES or _STAFFING_REPOST_RE.search(text):
        return "staffing_agency_repost"

    if cfg is not None:
        job_seniority = detect_seniority(job.get("title", ""))
        if job_seniority is not None and job_seniority not in cfg.seniority:
            return f"seniority_mismatch_{job_seniority}"

        # Title-based seniority detection only catches an EXPLICIT mismatch
        # (e.g. a title containing "Senior"); a plain title with no
        # seniority marker at all — "Software Engineer, Developer
        # Infrastructure" — gives no signal either way and would otherwise
        # slip through even when it's a genuine full-time role (confirmed
        # via employment_type='FullTime' on real postings that reached
        # this far). When the user has restricted to intern-only, require
        # a positive internship signal instead of just the absence of a
        # conflicting one — missing an unusually-labeled internship is a
        # safe failure (falls through to SKIPPED, reviewable); letting a
        # full-time role through into an intern-only search is not.
        if cfg.seniority == ["intern"] and not is_internship:
            return "not_confirmed_internship"

        if cfg.max_years_experience is not None:
            min_years = detect_min_years_required(job.get("description_raw", ""))
            if min_years is not None and min_years > cfg.max_years_experience:
                return f"requires_{min_years}_years_experience"

        if not cfg.allow_advanced_degree_required and requires_advanced_degree(job.get("description_raw", "")):
            return "requires_advanced_degree"

        excluded = {c.lower() for c in cfg.location.excluded_countries}
        if excluded & _COUNTRY_US_ALIASES and is_us_location(job.get("location", "")):
            return "excluded_country_united_states"

        if resume is not None and not has_keyword_relevance(job, resume, cfg):
            return "no_relevant_keyword_overlap"

    return None


def detect_seniority(title: str) -> str | None:
    title_lower = title.lower()
    for level, patterns in _SENIORITY_PATTERNS.items():
        if any(p.search(title_lower) for p in patterns):
            return level
    return None


def _match_terms(cfg: SearchFilters) -> set[str]:
    """Config-side matching terms: explicit keywords (synonym-expanded) plus
    the target role itself — e.g. "AI Research Intern" contributes "research"
    or similar tokens even when cfg.keywords is empty, which it very often
    is (target_role is what most people actually fill in first)."""
    config_terms = expand_keywords(cfg.keywords, cfg.synonyms)
    role_terms = {t for t in re.findall(r"[a-z]{4,}", target_role_text(cfg).lower())}
    return config_terms | role_terms


def _resume_terms(resume: MasterResume) -> set[str]:
    return {t.lower() for t in (resume.skills.languages + resume.skills.frameworks + resume.skills.tools)}


def _keyword_score(job: dict, resume: MasterResume, cfg: SearchFilters) -> float:
    terms = _resume_terms(resume) | _match_terms(cfg)
    if not terms:
        return 17.5  # neutral half-credit when we have nothing to match against yet

    haystack = f"{job.get('title', '')} {job.get('description_raw', '')}".lower()
    matches = sum(1 for term in terms if term and term in haystack)
    capped_terms = min(len(terms), 15)
    return min(35.0, (matches / capped_terms) * 35.0) if capped_terms else 0.0


def has_keyword_relevance(job: dict, resume: MasterResume, cfg: SearchFilters) -> bool:
    """True if the posting has ANY overlap with the resume's skills or the
    target role/keywords — a hard floor (§5): a posting with literally zero
    relevance to what the candidate is looking for should never surface no
    matter how favorable its location/seniority/freshness signals are.
    Without this floor, a completely unrelated posting (e.g. a fitness
    instructor role, for someone targeting AI research) can still rack up
    30-65 points from non-relevance factors alone and outrank genuinely
    relevant jobs. Returns True (don't block) when there's nothing to match
    against yet, since that's a config gap, not evidence of irrelevance."""
    terms = _resume_terms(resume) | _match_terms(cfg)
    if not terms:
        return True
    haystack = f"{job.get('title', '')} {job.get('description_raw', '')}".lower()
    return any(term and term in haystack for term in terms)


def _seniority_score(job: dict, cfg: SearchFilters) -> float:
    job_seniority = detect_seniority(job.get("title", ""))
    if job_seniority is None:
        return 10.0  # unknown — don't penalize hard, just don't reward either
    if job_seniority in cfg.seniority:
        return 20.0
    return 0.0


def _location_score(job: dict, cfg: SearchFilters) -> float:
    location = (job.get("location") or "").lower()
    work_mode = (job.get("work_mode") or "").lower()
    is_remote = cfg.location.remote and ("remote" in location or "remote" in work_mode)
    is_hybrid = cfg.location.hybrid and ("hybrid" in location or "hybrid" in work_mode)
    if is_remote or is_hybrid:
        return 20.0
    city_or_country_match = any(
        term.lower() in location for term in (cfg.location.cities + cfg.location.countries)
    )
    if city_or_country_match:
        return 20.0
    if not location:
        return 5.0
    return 0.0


def _salary_score(job: dict, cfg: SearchFilters) -> float:
    salary_min = job.get("salary_min")
    if salary_min is None:
        return 10.0 if cfg.salary.include_unlisted else 0.0
    if cfg.salary.min is None or salary_min >= cfg.salary.min:
        return 15.0
    return 0.0


def _freshness_score(job: dict) -> float:
    posted_at = job.get("posted_at")
    if not posted_at:
        return 3.0
    try:
        posted = datetime.fromisoformat(posted_at)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
    except ValueError:
        return 3.0
    age_days = (datetime.now(timezone.utc) - posted).total_seconds() / 86400
    if age_days < 1:
        return 10.0
    if age_days < 3:
        return 8.0
    if age_days < 7:
        return 6.0
    if age_days < 14:
        return 4.0
    if age_days < 30:
        return 2.0
    return 0.0


def compute_fit_score(job: dict, resume: MasterResume, cfg: SearchFilters) -> int:
    total = (
        _keyword_score(job, resume, cfg)
        + _seniority_score(job, cfg)
        + _location_score(job, cfg)
        + _salary_score(job, cfg)
        + _freshness_score(job)
    )
    return max(0, min(100, round(total)))
