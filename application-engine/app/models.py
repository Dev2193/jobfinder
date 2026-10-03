"""The master resume JSON schema (§3) — everything downstream reads from this."""

from pydantic import BaseModel, ConfigDict, Field


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution: str = ""
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""
    coursework: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    bullets: list[str] = Field(default_factory=list)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    description: str = ""
    tech: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    link: str = ""


class Skills(BaseModel):
    model_config = ConfigDict(extra="forbid")

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_authorized_in: list[str] = Field(default_factory=list)
    needs_sponsorship: bool | None = None


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_period: str = ""
    earliest_start: str = ""
    willing_to_relocate: bool | None = None


class MasterResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: Identity = Field(default_factory=Identity)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    certifications: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    authorization: Authorization = Field(default_factory=Authorization)
    preferences: Preferences = Field(default_factory=Preferences)
