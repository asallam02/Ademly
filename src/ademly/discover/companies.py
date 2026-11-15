"""The company list and the rules for skipping companies that don't fit."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from ademly import config


class SeedCompany(BaseModel):
    ats: str
    token: str
    name: str
    exclude: bool = False
    reason: str | None = None


def load_seed_companies(path: Path) -> list[SeedCompany]:
    if not path.exists():
        return []
    return [SeedCompany.model_validate(item) for item in yaml.safe_load(path.read_text()) or []]


@dataclass(frozen=True)
class Cooldown:
    days: int
    reason: str
    # What the judgment rests on: the filter settings, the ranking settings, or
    # the user's own choices. A pause ends early when its basis changes.
    basis: Literal["filters", "ranking", "user"]


def assess_fit(stats, passed_now: int, min_score: int) -> Cooldown | None:
    """Decide whether to skip a company for a while, from its full history.

    `stats` is a row from Database.company_stats. `passed_now` counts the
    company's currently listed roles that passed the filters.
    """
    if stats["skipped"] >= config.SKIPPED_WITHOUT_APPLYING and stats["applied"] == 0:
        return Cooldown(
            config.COOLDOWN_SKIPPED_DAYS,
            f"you skipped {stats['skipped']} of its roles and applied to none",
            "user",
        )
    # Only roles the job seeker could take count: one ruled out on location,
    # years, or pay says nothing about whether the company is a good fit.
    if stats["ranked_eligible"] >= config.LOW_FIT_MIN_RANKED and stats["good"] == 0:
        return Cooldown(
            config.COOLDOWN_LOW_FIT_DAYS,
            f"best score {stats['best_eligible']} across {stats['ranked_eligible']} "
            f"eligible roles (minimum is {min_score})",
            "ranking",
        )
    if passed_now == 0:
        return Cooldown(
            config.COOLDOWN_NO_MATCHES_DAYS, "no open roles matched your filters", "filters"
        )
    return None


def cooldown_until(now: datetime, cooldown: Cooldown) -> datetime:
    return now + timedelta(days=cooldown.days)
