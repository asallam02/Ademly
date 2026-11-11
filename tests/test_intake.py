import re

import pytest

from ademly import config
from ademly.intake.master_resume import (
    PolicyError,
    Replacement,
    RequiredTerm,
    ResumeParseError,
    ResumePolicy,
    is_approved,
    load_policy,
    parse_master_resume,
    resume_hash,
    write_approval,
)

SAMPLE = """# Sam Example

sam@example.com | (555)-123-4567 | [linkedin.com/in/sam](https://linkedin.com/in/sam) | Toronto, ON

---

## Technical Skills

- **Programming Languages:** Java, R
- **Tools:** Docker
- **Certifications:** Cloud Basics

---

## Education

### Example University — Vancouver, BC
*May 2024*

Bachelor of Science

---

## Work Experience

### Acme — Toronto, ON
**Developer** | *Aug 2024 – Present*
*Java, Docker, React*

#### Client Assignment: Secret Bank
*Oct 2024 – Present*

- Improved throughput by up to 40% using Java
- Saved ~30% on costs

#### Internal
- Mentored interns by:
  - Providing technical support
  - Leading weekly 1-1s

---

## Projects

### Widget — A Challenge
*Jan 2022 – Apr 2022*

- Built a model using R

---

## Awards and Recognitions

- **Big Prize** 1st place (10 teams) — *Apr 2020*

---

## Related Experience

### Club, Example University
*Sep 2019 – May 2022*

#### Co-lead
- Led 9 people
"""


def test_parses_structure():
    resume = parse_master_resume(SAMPLE)
    assert resume.contact.name == "Sam Example"
    assert resume.contact.email == "sam@example.com"
    assert resume.contact.location == "Toronto, ON"
    assert resume.certifications == ["Cloud Basics"]
    assert resume.education[0].date == "May 2024"
    assert resume.awards[0].detail == "1st place (10 teams)"
    work = resume.entries[0]
    assert (work.organization, work.location, work.title) == ("Acme", "Toronto, ON", "Developer")
    assert work.dates == "Aug 2024 – Present"
    assert work.technologies == ["Java", "Docker", "React"]
    assert work.groups[0].dates == "Oct 2024 – Present"


def test_nested_bullets_become_complete_claims():
    resume = parse_master_resume(SAMPLE)
    texts = [fact.text for fact in resume.entries[0].groups[1].facts]
    assert texts == [
        "Mentored interns by providing technical support",
        "Mentored interns by leading weekly 1-1s",
    ]


def test_fact_ids_are_unique_and_stable():
    first = parse_master_resume(SAMPLE)
    second = parse_master_resume(SAMPLE)
    ids = [fact.id for fact in first.facts()]
    assert len(ids) == len(set(ids))
    assert ids[0] == "work.acme.g1.b1"
    assert resume_hash(first) == resume_hash(second)


@pytest.mark.parametrize(
    "broken",
    [
        SAMPLE.replace("- Led 9 people", "Led 9 people, without a bullet"),
        SAMPLE.replace("## Projects", "## Hobbies"),
        SAMPLE.replace("- Mentored interns by:", "- Mentored interns"),
    ],
)
def test_unrecognized_content_fails_loudly(broken):
    with pytest.raises(ResumeParseError):
        parse_master_resume(broken)


def test_replacement_and_spelling_apply():
    policy = ResumePolicy(
        replacements=[Replacement(find="Secret Bank", replace="A Bank")],
        spelling={"Acme": "ACME"},
    )
    resume = parse_master_resume(SAMPLE, policy)
    assert resume.entries[0].organization == "ACME"
    assert resume.entries[0].groups[0].name == "Client Assignment: A Bank"


def test_stale_replacement_fails():
    policy = ResumePolicy(replacements=[Replacement(find="Not There", replace="x")])
    with pytest.raises(PolicyError):
        parse_master_resume(SAMPLE, policy)


def test_forbidden_term_fails():
    with pytest.raises(PolicyError):
        parse_master_resume(SAMPLE, ResumePolicy(forbidden_terms=["secret bank"]))


def test_required_terms_attach_to_fact():
    policy = ResumePolicy(
        required_terms=[RequiredTerm(contains="by up to 40%", terms=["up to 40%"])]
    )
    resume = parse_master_resume(SAMPLE, policy)
    assert resume.entries[0].groups[0].facts[0].required_terms == ["up to 40%"]


def test_required_term_with_no_match_fails():
    policy = ResumePolicy(required_terms=[RequiredTerm(contains="nope", terms=["x"])])
    with pytest.raises(PolicyError):
        parse_master_resume(SAMPLE, policy)


def test_additional_skill_needs_evidence():
    assert parse_master_resume(SAMPLE, ResumePolicy(additional_skills=["React"]))
    with pytest.raises(PolicyError):
        parse_master_resume(SAMPLE, ResumePolicy(additional_skills=["Rust"]))


def test_short_skill_name_matches_case_sensitively():
    resume = parse_master_resume(SAMPLE)
    r_skill = next(skill for skill in resume.skills if skill.name == "R")
    assert r_skill.evidence == ["project.widget.g1.b1"]


def test_approval_is_tied_to_exact_content(tmp_path):
    path = tmp_path / "approved.json"
    resume = parse_master_resume(SAMPLE)
    assert not is_approved(resume, path)
    write_approval(resume, path)
    assert is_approved(resume, path)
    changed = parse_master_resume(SAMPLE.replace("Led 9 people", "Led 10 people"))
    assert not is_approved(changed, path)


@pytest.mark.skipif(
    not (config.MASTER_RESUME_SOURCE.exists() and config.RESUME_POLICY.exists()),
    reason="personal resume files are not present",
)
def test_real_resume_loses_no_claims():
    source = config.MASTER_RESUME_SOURCE.read_text()
    policy = load_policy(config.RESUME_POLICY)
    resume = parse_master_resume(source, policy)

    entry_sections = re.split(r"^## ", source, flags=re.M)
    claims = 0
    for section in entry_sections:
        if not section.startswith(("Work Experience", "Internships", "Projects", "Related")):
            continue
        lines = section.splitlines()
        for index, line in enumerate(lines):
            is_parent = (
                line.startswith("- ")
                and index + 1 < len(lines)
                and lines[index + 1].startswith("  - ")
            )
            if re.match(r"^\s*- ", line) and not is_parent:
                claims += 1
    assert len(resume.facts()) == claims

    dumped = resume.model_dump_json()
    for term in policy.forbidden_terms:
        assert not re.search(rf"(?<!\w){re.escape(term)}(?!\w)", dumped, re.IGNORECASE)
