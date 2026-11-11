"""Shared schemas."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Contact(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    """One claim from the master resume, the unit that tailored bullets cite."""

    id: str
    text: str
    skills: list[str] = Field(default_factory=list)
    # A bullet citing this fact must keep these terms, such as "projected".
    required_terms: list[str] = Field(default_factory=list)


class FactGroup(BaseModel):
    """A sub-heading inside an entry, such as a client or a role."""

    id: str
    name: str | None = None
    dates: str | None = None
    facts: list[Fact]


class Entry(BaseModel):
    """A job, internship, project, or activity. Code copies these fields verbatim."""

    id: str
    kind: Literal["work", "internship", "project", "activity"]
    organization: str
    location: str | None = None
    title: str | None = None
    dates: str | None = None
    technologies: list[str] = Field(default_factory=list)
    groups: list[FactGroup]


class Education(BaseModel):
    institution: str
    location: str | None = None
    date: str
    degree: str


class Award(BaseModel):
    name: str
    detail: str
    date: str


class Skill(BaseModel):
    name: str
    category: str
    listed: bool  # True if it appears in the Technical Skills section
    evidence: list[str] = Field(default_factory=list)  # fact or entry IDs


class MasterResume(BaseModel):
    contact: Contact
    skills: list[Skill]
    certifications: list[str]
    education: list[Education]
    entries: list[Entry]
    awards: list[Award]

    def facts(self) -> list[Fact]:
        return [f for e in self.entries for g in e.groups for f in g.facts]


class Posting(BaseModel):
    """A job as fetched from an ATS. Description text is untrusted input."""

    ats: Literal["greenhouse", "lever", "ashby"]
    board_token: str
    job_id: str
    company: str
    title: str
    location: str
    locations: list[str] = Field(default_factory=list)
    remote: bool | None = None
    description: str
    url: str
    apply_url: str
    published_at: str | None = None


class JobState(StrEnum):
    DISCOVERED = "discovered"
    FILTERED_OUT = "filtered_out"
    SELECTED = "selected"
    TAILORING = "tailoring"
    VERIFIED = "verified"
    RENDERED = "rendered"
    READY_TO_APPLY = "ready_to_apply"
    APPLIED = "applied"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
