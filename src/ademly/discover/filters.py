"""Search settings and the free, code-only filters applied before ranking."""

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ademly.models import Posting

DEFAULT_TITLE_INCLUDE = ["engineer", "developer", "sre", "devops", "reliability"]
DEFAULT_TITLE_EXCLUDE = [
    "senior", "sr", "staff", "principal", "lead", "manager", "director", "head",
    "vp", "vice president", "intern", "internship", "co-op", "coop", "new grad",
    "sales", "solutions", "support", "customer", "field", "recruiter",
    "advocate", "relations", "ios", "android", "mobile", "frontend", "front-end",
    "front end", "hardware", "firmware",
]

LocationMatch = Literal["match", "maybe", "no"]

_REMOTE = re.compile(r"\b(remote|distributed|work from home|wfh)\b", re.I)
_CANADA = re.compile(r"\b(canada|ontario|british columbia)\b", re.I)
_BROAD = re.compile(r"\b(north america|americas|worldwide|global|anywhere)\b", re.I)
# Words that can sit next to "remote" without naming a place.
_REMOTE_FILLER = re.compile(
    r"\b(remote|distributed|work from home|wfh|hybrid|friendly|first|only|based|"
    r"position|role|travel|required|occasional|flexible|flex|full|time|optional)\b",
    re.I,
)
_TIMEZONE = re.compile(
    r"\b(est|edt|pst|pdt|cst|mst|et|pt|ct|mt|eastern|pacific|central|mountain|"
    r"time ?zones?|tz|utc|gmt)\b",
    re.I,
)


def _names_another_place(place: str) -> bool:
    """True for 'Remote - Mexico' or 'NZ-New Zealand-Remote', false for 'Remote'.

    Called only for places that don't mention Canada or an acceptable city.
    """
    rest = _REMOTE_FILLER.sub(" ", place)
    if _TIMEZONE.search(rest):
        return False
    return bool(re.sub(r"[\W_]+", "", rest))


class WorkAuthorization(BaseModel):
    canada: bool = True
    united_states: bool = False
    needs_sponsorship_in_canada: bool = False


class SearchConfig(BaseModel):
    description: str
    preferences: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    remote_canada: bool = True
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    min_salary: int | None = None
    target_salary: tuple[int, int] | None = None
    salary_currency: str = "CAD"
    max_years_required: int | None = None
    preferred_years_required: int | None = None
    title_include: list[str] = Field(default_factory=lambda: list(DEFAULT_TITLE_INCLUDE))
    title_exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_TITLE_EXCLUDE))
    max_posting_age_days: int = 30
    min_score: int = 60
    search_queries: list[str] = Field(default_factory=list)

    def fingerprint(self) -> str:
        """Changes whenever a setting that affects ranking changes."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def filter_fingerprint(self) -> str:
        """Changes whenever a setting used by the code filters changes."""
        relevant = self.model_dump(
            include={
                "title_include", "title_exclude", "cities", "remote_canada",
                "max_posting_age_days", "max_years_required",
            }
        )
        return hashlib.sha256(repr(sorted(relevant.items())).encode()).hexdigest()


class SearchConfigMissing(Exception):
    pass


def load_search_config(path: Path, example: Path | None = None) -> SearchConfig:
    if not path.exists():
        hint = f" Copy {example.name} to {path} and edit it." if example else ""
        raise SearchConfigMissing(f"Missing {path}.{hint}")
    return SearchConfig.model_validate(yaml.safe_load(path.read_text()) or {})


def _terms(terms: list[str], whole_word: bool) -> re.Pattern | None:
    if not terms:
        return None
    end = r"(?!\w)" if whole_word else ""
    alternatives = "|".join(re.escape(term) for term in terms)
    return re.compile(rf"(?<!\w)(?:{alternatives}){end}", re.I)


def title_rejection(title: str, config: SearchConfig) -> str | None:
    exclude = _terms(config.title_exclude, whole_word=True)
    if exclude and (match := exclude.search(title)):
        return f"title contains '{match.group(0).lower()}'"
    include = _terms(config.title_include, whole_word=False)
    if include and not include.search(title):
        return "title outside target roles"
    return None


def location_match(posting: Posting, config: SearchConfig) -> LocationMatch:
    """'match' is clearly acceptable, 'maybe' is left for the ranker to judge."""
    cities = _terms(config.cities, whole_word=True)
    verdict: LocationMatch = "no"
    for place in posting.locations or [posting.location]:
        if cities and cities.search(place):
            return "match"
        if _CANADA.search(place):
            if config.remote_canada and (_REMOTE.search(place) or posting.remote):
                return "match"
            verdict = "maybe"
        elif _BROAD.search(place) or (_REMOTE.search(place) and not _names_another_place(place)):
            verdict = "maybe"
    return verdict


def best_location(posting: Posting, config: SearchConfig) -> str:
    """The listed location most relevant to the search, for display."""
    cities = _terms(config.cities, whole_word=True)
    places = posting.locations or [posting.location]
    for test in (
        lambda place: cities and cities.search(place),
        lambda place: _CANADA.search(place),
        lambda place: _BROAD.search(place) or _REMOTE.search(place),
    ):
        for place in places:
            if test(place):
                return place
    return posting.location


def too_old(posting: Posting, config: SearchConfig, now: datetime) -> bool:
    if not posting.published_at:
        return False
    try:
        published = datetime.fromisoformat(posting.published_at)
    except ValueError:
        return False
    if published.tzinfo is None:
        return False
    return now - published > timedelta(days=config.max_posting_age_days)


_YEARS = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?years?"
    r"(?:['’]s?)?(?:\s+of)?(?:\s+[\w/+#.-]+){0,4}?\s+experience",
    re.I,
)


def years_required(text: str) -> int | None:
    """The smallest 'N+ years ... experience' figure in a posting, if any.

    The smallest is used because postings often add higher figures for
    alternative paths, preferred qualifications, or the team's experience.
    """
    figures = [int(match.group(1)) for match in _YEARS.finditer(text)]
    figures = [figure for figure in figures if 0 < figure < 30]
    return min(figures) if figures else None


def hard_filter(
    posting: Posting, config: SearchConfig, now: datetime
) -> tuple[str | None, LocationMatch]:
    """Returns a rejection reason, or None, and the location verdict."""
    if reason := title_rejection(posting.title, config):
        return reason, "no"
    verdict = location_match(posting, config)
    if verdict == "no":
        return "location outside search area", verdict
    if too_old(posting, config, now):
        return f"posted more than {config.max_posting_age_days} days ago", verdict
    if config.max_years_required is not None:
        years = years_required(posting.description)
        if years is not None and years > config.max_years_required:
            return f"asks for {years}+ years of experience", verdict
    return None, verdict
