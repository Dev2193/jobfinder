"""Search-filter config, persisted to config.yaml so it's set once (§3)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

EMPLOYMENT_TYPES = ("full-time", "part-time", "internship", "contract", "apprenticeship")
SENIORITIES = ("intern", "entry", "mid", "senior")
POSTED_WITHIN_OPTIONS = ("24h", "3d", "7d", "14d", "30d")

# Small synonym expansion seed list so a keyword search also matches its common aliases.
# Extend freely as gaps in matching turn up.
DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "swe": ["software engineer", "software developer", "backend developer", "frontend developer"],
    "software engineer": ["swe", "software developer"],
    "ml engineer": ["machine learning engineer", "ai engineer", "ml"],
    "data scientist": ["data science", "applied scientist"],
    "product manager": ["pm", "product owner"],
}


class LocationFilter(BaseModel):
    cities: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    remote: bool = True
    hybrid: bool = True
    radius_miles: int | None = None
    # Hard exclusion (§5), not a scoring penalty: a posting whose location
    # clearly indicates one of these countries never surfaces at all — this
    # exists specifically so "remote" doesn't quietly let a US-only remote
    # role back in when the candidate is searching outside the US.
    excluded_countries: list[str] = Field(default_factory=list)


class SalaryFilter(BaseModel):
    min: int | None = None
    currency: str = "USD"
    # Most postings hide salary; defaulting this off kills ~70% of the funnel (§3).
    include_unlisted: bool = True


class SearchFilters(BaseModel):
    location: LocationFilter = Field(default_factory=LocationFilter)
    employment_type: list[Literal["full-time", "part-time", "internship", "contract", "apprenticeship"]] = Field(
        default_factory=lambda: ["internship", "full-time"]
    )
    keywords: list[str] = Field(default_factory=list)
    # Feeds the Diagnoser/Recruiter skill prompts (§6.1/§6.2) so they never have
    # to stop and ask mid-run; keywords[0] doubles as "target role" if unset.
    # Multi-select (§3): a candidate is often targeting several distinct role
    # families at once (e.g. "AI/ML Research Intern" and "Strategy Intern") —
    # every downstream consumer only ever wants the single prose string these
    # join into (see target_role_text/industry_text below), so nothing past
    # this config layer needs to know it's a list.
    target_role: list[str] = Field(default_factory=list)
    industry: list[str] = Field(default_factory=list)
    synonyms: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_SYNONYMS))
    salary: SalaryFilter = Field(default_factory=SalaryFilter)
    posted_within: Literal["24h", "3d", "7d", "14d", "30d"] = "7d"
    seniority: list[Literal["intern", "entry", "mid", "senior"]] = Field(default_factory=lambda: ["intern", "entry"])
    exclusion_list: list[str] = Field(default_factory=list)
    weekly_application_cap: int = 80
    # Undergrad-appropriate filtering (§5 hard exclusions, not just scoring
    # penalties): a posting requiring more years than this, or an advanced
    # degree as a hard requirement, gets auto-skipped outright rather than
    # just scored lower — a "Director" role with great keyword overlap
    # should never show up at all, not just rank lower.
    max_years_experience: int | None = 2
    allow_advanced_degree_required: bool = False

    @field_validator("target_role", "industry", mode="before")
    @classmethod
    def _coerce_legacy_single_value(cls, v):
        """target_role/industry used to be a single string; coerce old
        config.yaml files (and any other single-string input) into a
        one-item list rather than losing the value or erroring on load."""
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v


def target_role_text(cfg: "SearchFilters") -> str:
    """Join the multi-select target roles into the single prose string every
    downstream prompt expects (Diagnoser, Recruiter, Rewriter). Falls back to
    the first keyword if no target role was set at all — same fallback every
    call site used before this was centralized here."""
    if cfg.target_role:
        return ", ".join(cfg.target_role)
    return cfg.keywords[0] if cfg.keywords else ""


def industry_text(cfg: "SearchFilters") -> str:
    return ", ".join(cfg.industry)


def expand_keywords(keywords: list[str], synonyms: dict[str, list[str]]) -> set[str]:
    expanded = {k.lower() for k in keywords}
    for kw in list(expanded):
        expanded.update(s.lower() for s in synonyms.get(kw, []))
    return expanded


def load_config(path: Path = CONFIG_PATH) -> SearchFilters:
    if not path.exists():
        cfg = SearchFilters()
        save_config(cfg, path)
        return cfg
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return SearchFilters.model_validate(raw)


def save_config(cfg: SearchFilters, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(cfg.model_dump(), f, sort_keys=False)
