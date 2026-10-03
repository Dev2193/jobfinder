"""The ~25 standard portal questions, answered once and reused across every
application instead of re-generated per job (§3) — the single biggest token
saver alongside the fit-score prefilter in §5.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field

ANSWERS_BANK_PATH = Path(__file__).resolve().parent.parent / "data" / "answers_bank.json"

PREFER_NOT_TO_SAY = "prefer not to say"


class Demographics(BaseModel):
    gender: str = PREFER_NOT_TO_SAY
    race_ethnicity: str = PREFER_NOT_TO_SAY
    veteran_status: str = PREFER_NOT_TO_SAY
    disability_status: str = PREFER_NOT_TO_SAY
    lgbtq_identification: str = PREFER_NOT_TO_SAY
    pronouns: str = ""


class AnswersBank(BaseModel):
    # Experience
    years_experience_overall: str = ""
    years_experience_primary_skill: str = ""
    currently_employed: bool | None = None
    reason_for_interest_template: str = ""

    # Authorization / logistics
    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool | None = None
    open_to_remote: bool | None = None
    open_to_hybrid: bool | None = None
    at_least_18: bool | None = None
    has_non_compete: bool | None = None

    # Compensation / timing
    desired_salary: str = ""
    notice_period: str = ""
    earliest_start_date: str = ""

    # Referral
    referral_source: str = "Company careers page / job board"
    has_referral: bool = False
    referral_name: str = ""

    # Background
    highest_education_level: str = ""
    willing_background_check: bool | None = None
    willing_drug_test: bool | None = None
    has_felony_conviction: bool | None = None
    previously_employed_here: bool | None = None
    requires_accommodations: bool | None = None

    # Links
    linkedin_url: str = ""
    portfolio_url: str = ""
    github_url: str = ""

    demographics: Demographics = Field(default_factory=Demographics)

    # Anything a portal asks that isn't covered above, keyed by a normalized question string.
    other: dict[str, str] = Field(default_factory=dict)


def load_answers_bank(path: Path = ANSWERS_BANK_PATH) -> AnswersBank:
    if not path.exists():
        return AnswersBank()
    return AnswersBank.model_validate(json.loads(path.read_text()))


def save_answers_bank(bank: AnswersBank, path: Path = ANSWERS_BANK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bank.model_dump(), indent=2))


def prefill_from_master_resume(bank: AnswersBank, resume) -> AnswersBank:
    """Fill in the answers bank fields that are directly derivable from the master
    resume JSON, so the batch form only asks about what the resume can't answer."""
    data = bank.model_dump()
    if resume.identity.linkedin:
        data["linkedin_url"] = resume.identity.linkedin
    if resume.identity.github:
        data["github_url"] = resume.identity.github
    if resume.identity.portfolio:
        data["portfolio_url"] = resume.identity.portfolio
    if resume.preferences.notice_period:
        data["notice_period"] = resume.preferences.notice_period
    if resume.preferences.earliest_start:
        data["earliest_start_date"] = resume.preferences.earliest_start
    if resume.preferences.willing_to_relocate is not None:
        data["willing_to_relocate"] = resume.preferences.willing_to_relocate
    if resume.authorization.needs_sponsorship is not None:
        data["requires_sponsorship"] = resume.authorization.needs_sponsorship
    return AnswersBank.model_validate(data)
