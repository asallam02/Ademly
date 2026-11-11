"""Turns master_resume.md into the validated source of truth.

Parsing is deterministic and strict. A line the parser doesn't recognize raises
an error, so a claim can't be silently lost or altered on the way in.
"""

import difflib
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ademly.models import (
    Award,
    Contact,
    Education,
    Entry,
    Fact,
    FactGroup,
    MasterResume,
    Skill,
)

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4}"
_DATES = re.compile(rf"^{_MONTH}(?: – (?:{_MONTH}|Present))?$")
_ITALIC = re.compile(r"^\*([^*]+)\*$")
_TITLE = re.compile(r"^\*\*(.+?)\*\* \| \*(.+?)\*$")
_BULLET = re.compile(r"^(\s*)- (.+)$")
_SKILL_LINE = re.compile(r"^- \*\*(.+?):\*\* (.+)$")
_AWARD = re.compile(r"^- \*\*(.+?)\*\* (.+?) — \*(.+?)\*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_PHONE = re.compile(r"^[\d()\-+\s.]{7,}$")

ENTRY_SECTIONS = {
    "Work Experience": "work",
    "Internships": "internship",
    "Projects": "project",
    "Related Experience": "activity",
}


class ResumeParseError(Exception):
    pass


class PolicyError(Exception):
    pass


class Replacement(BaseModel):
    find: str
    replace: str


class RequiredTerm(BaseModel):
    contains: str
    terms: list[str]


class ResumePolicy(BaseModel):
    """The user's decisions about what the system may claim."""

    # Each must match, so a stale rule fails instead of silently doing nothing.
    replacements: list[Replacement] = Field(default_factory=list)
    # Whole-word spelling fixes. These may match nothing.
    spelling: dict[str, str] = Field(default_factory=dict)
    # Must not appear anywhere in the processed resume.
    forbidden_terms: list[str] = Field(default_factory=list)
    required_terms: list[RequiredTerm] = Field(default_factory=list)
    # Skills not in the Technical Skills section. Each needs evidence.
    additional_skills: list[str] = Field(default_factory=list)


def load_policy(path: Path) -> ResumePolicy:
    if not path.exists():
        return ResumePolicy()
    return ResumePolicy.model_validate(yaml.safe_load(path.read_text()) or {})


def apply_policy_to_text(markdown: str, policy: ResumePolicy) -> str:
    text = markdown
    for rule in policy.replacements:
        if rule.find not in text:
            raise PolicyError(f"replacement did not match anything: {rule.find!r}")
        text = text.replace(rule.find, rule.replace)
    for wrong, right in policy.spelling.items():
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text)
    for term in policy.forbidden_terms:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
            raise PolicyError(f"forbidden term remains in the resume: {term!r}")
    return text


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _split_blocks(lines: list[str], prefix: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split lines on a heading prefix, returning preamble and (heading, body) pairs."""
    preamble: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith(prefix):
            blocks.append((line[len(prefix) :].strip(), []))
        elif blocks:
            blocks[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, blocks


def _content(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip() and line.strip() != "---"]


def _lower_first(text: str) -> str:
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _flatten_bullets(items: list[tuple[str, list[str]]]) -> list[str]:
    """A parent ending in ':' becomes one complete claim per child."""
    claims: list[str] = []
    for text, children in items:
        if not children:
            claims.append(text)
            continue
        if not text.endswith(":"):
            raise ResumeParseError(f"nested bullets under a non-list line: {text!r}")
        lead = text[:-1].rstrip()
        claims.extend(f"{lead} {_lower_first(child)}" for child in children)
    return claims


def _parse_entry(heading: str, body: list[str], kind: str) -> Entry:
    if kind == "project":
        organization, location = heading, None
    elif " — " in heading:
        organization, location = heading.split(" — ", 1)
    else:
        organization, location = heading, None

    title = dates = None
    technologies: list[str] = []
    # Each group is [name, dates, bullet items].
    groups: list[list] = [[None, None, []]]

    for line in _content(body):
        current = groups[-1]
        if line.startswith("#### "):
            groups.append([line[5:].strip(), None, []])
        elif match := _TITLE.match(line):
            title, dates = match.group(1), match.group(2)
        elif match := _ITALIC.match(line):
            value = match.group(1).strip()
            if _DATES.match(value):
                if current[0] is not None:
                    current[1] = value
                else:
                    dates = value
            elif title and not technologies:
                technologies = [item.strip() for item in value.split(",")]
            else:
                raise ResumeParseError(f"unrecognized italic line: {line!r}")
        elif match := _BULLET.match(line):
            indent, text = match.group(1), match.group(2).strip()
            if indent:
                if not current[2]:
                    raise ResumeParseError(f"nested bullet with no parent: {line!r}")
                current[2][-1][1].append(text)
            else:
                current[2].append((text, []))
        else:
            raise ResumeParseError(f"unrecognized line in {heading!r}: {line!r}")

    short_name = organization.split(",")[0] if kind == "activity" else organization
    if kind == "project":
        short_name = heading.split(" — ")[0]
    entry_id = f"{kind}.{_slug(short_name)}"

    fact_groups = []
    for name, group_dates, items in groups:
        if not items:
            if name is not None:
                raise ResumeParseError(f"heading with no bullets: {name!r}")
            continue
        group_id = f"{entry_id}.g{len(fact_groups) + 1}"
        facts = [
            Fact(id=f"{group_id}.b{i}", text=claim)
            for i, claim in enumerate(_flatten_bullets(items), start=1)
        ]
        fact_groups.append(
            FactGroup(id=group_id, name=name, dates=group_dates, facts=facts)
        )

    return Entry(
        id=entry_id,
        kind=kind,
        organization=organization.strip(),
        location=location.strip() if location else None,
        title=title,
        dates=dates,
        technologies=technologies,
        groups=fact_groups,
    )


def _parse_contact(name: str, line: str) -> Contact:
    contact = Contact(name=name)
    for part in (piece.strip() for piece in line.split(" | ")):
        if link := _LINK.match(part):
            contact.links.append(link.group(2))
        elif _EMAIL.match(part):
            contact.email = part
        elif _PHONE.match(part):
            contact.phone = part
        else:
            contact.location = part
    return contact


def _parse_education(heading: str, body: list[str]) -> Education:
    lines = _content(body)
    date_match = _ITALIC.match(lines[0]) if lines else None
    if len(lines) != 2 or not date_match:
        raise ResumeParseError(f"unexpected education block: {heading!r}")
    institution, _, location = heading.partition(" — ")
    return Education(
        institution=institution,
        location=location or None,
        date=date_match.group(1),
        degree=lines[1],
    )


def _term_regex(term: str) -> re.Pattern:
    # Very short names like "R" must match case, or they'd match stray letters.
    flags = 0 if len(term) <= 2 else re.IGNORECASE
    return re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", flags)


def _attach_skills(
    entries: list[Entry], listed: list[tuple[str, str]], additional: list[str]
) -> list[Skill]:
    skills = []
    pairs = [(name, category, True) for name, category in listed]
    pairs += [(name, "Additional", False) for name in additional]
    for name, category, is_listed in pairs:
        pattern = _term_regex(name)
        evidence: list[str] = []
        for entry in entries:
            if any(pattern.fullmatch(tech) for tech in entry.technologies):
                evidence.append(entry.id)
            for group in entry.groups:
                for fact in group.facts:
                    if pattern.search(fact.text):
                        evidence.append(fact.id)
                        fact.skills.append(name)
        if not is_listed and not evidence:
            raise PolicyError(f"additional skill has no evidence in the resume: {name!r}")
        skills.append(Skill(name=name, category=category, listed=is_listed, evidence=evidence))
    return skills


def parse_master_resume(markdown: str, policy: ResumePolicy | None = None) -> MasterResume:
    policy = policy or ResumePolicy()
    lines = apply_policy_to_text(markdown, policy).splitlines()

    preamble, sections = _split_blocks(lines, "## ")
    header = _content(preamble)
    if len(header) != 2 or not header[0].startswith("# "):
        raise ResumeParseError("expected a '# Name' line followed by a contact line")
    contact = _parse_contact(header[0][2:].strip(), header[1])

    listed: list[tuple[str, str]] = []
    certifications: list[str] = []
    education: list[Education] = []
    entries: list[Entry] = []
    awards: list[Award] = []

    for title, body in sections:
        if title == "Technical Skills":
            for line in _content(body):
                match = _SKILL_LINE.match(line)
                if not match:
                    raise ResumeParseError(f"unrecognized skills line: {line!r}")
                items = [item.strip() for item in match.group(2).split(",")]
                if match.group(1) == "Certifications":
                    certifications.extend(items)
                else:
                    listed.extend((item, match.group(1)) for item in items)
        elif title == "Education":
            _, blocks = _split_blocks(body, "### ")
            education.extend(_parse_education(h, b) for h, b in blocks)
        elif title == "Awards and Recognitions":
            for line in _content(body):
                match = _AWARD.match(line)
                if not match:
                    raise ResumeParseError(f"unrecognized award line: {line!r}")
                awards.append(
                    Award(name=match.group(1), detail=match.group(2), date=match.group(3))
                )
        elif title in ENTRY_SECTIONS:
            preface, blocks = _split_blocks(body, "### ")
            if _content(preface):
                raise ResumeParseError(f"text before first entry in {title!r}")
            entries.extend(_parse_entry(h, b, ENTRY_SECTIONS[title]) for h, b in blocks)
        else:
            raise ResumeParseError(f"unknown section: {title!r}")

    skills = _attach_skills(entries, listed, policy.additional_skills)
    resume = MasterResume(
        contact=contact,
        skills=skills,
        certifications=certifications,
        education=education,
        entries=entries,
        awards=awards,
    )
    _apply_required_terms(resume, policy)
    _validate(resume)
    return resume


def _apply_required_terms(resume: MasterResume, policy: ResumePolicy) -> None:
    for rule in policy.required_terms:
        matches = [fact for fact in resume.facts() if rule.contains in fact.text]
        if not matches:
            raise PolicyError(f"required_terms did not match any fact: {rule.contains!r}")
        for fact in matches:
            missing = [term for term in rule.terms if term not in fact.text]
            if missing:
                raise PolicyError(f"fact {fact.id} lacks required terms: {missing}")
            fact.required_terms = rule.terms


def _validate(resume: MasterResume) -> None:
    ids = [entry.id for entry in resume.entries]
    ids += [fact.id for fact in resume.facts()]
    duplicates = {item for item in ids if ids.count(item) > 1}
    if duplicates:
        raise ResumeParseError(f"duplicate IDs: {sorted(duplicates)}")


def resume_hash(resume: MasterResume) -> str:
    return hashlib.sha256(resume.model_dump_json().encode()).hexdigest()


def render_review(source: str, processed: str, resume: MasterResume) -> str:
    """A summary for the user: what was changed, then every parsed claim by ID."""
    diff = list(
        difflib.unified_diff(
            source.splitlines(), processed.splitlines(), "original", "processed", lineterm="", n=0
        )
    )
    out = ["# Master resume review", "", "## Changes made to your text", "", "```diff"]
    out += diff or ["(none)"]
    out += ["```", "", "## Skills", ""]
    for skill in resume.skills:
        origin = "listed" if skill.listed else "added from your experience"
        out.append(f"- **{skill.name}** ({origin}): {len(skill.evidence)} supporting items")
    out += ["", "## Claims", ""]
    for entry in resume.entries:
        heading = " | ".join(x for x in (entry.organization, entry.title, entry.dates) if x)
        out += [f"### {heading}", ""]
        for group in entry.groups:
            if group.name:
                out += [f"**{group.name}**" + (f" ({group.dates})" if group.dates else ""), ""]
            for fact in group.facts:
                lock = f" [must keep: {', '.join(fact.required_terms)}]" if fact.required_terms else ""
                out.append(f"- `{fact.id}` {fact.text}{lock}")
            out.append("")
    return "\n".join(out)


def write_approval(resume: MasterResume, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "sha256": resume_hash(resume),
                "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            indent=2,
        )
    )


def is_approved(resume: MasterResume, path: Path) -> bool:
    if not path.exists():
        return False
    return json.loads(path.read_text()).get("sha256") == resume_hash(resume)
