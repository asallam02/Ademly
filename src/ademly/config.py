"""Paths and tunable settings."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"

MASTER_RESUME_SOURCE = ROOT / "master_resume.md"
RESUME_POLICY = DATA_DIR / "resume_policy.yaml"
MASTER_RESUME_JSON = DATA_DIR / "master_resume.json"
MASTER_RESUME_REVIEW = DATA_DIR / "master_resume.review.md"
MASTER_RESUME_APPROVAL = DATA_DIR / "master_resume.approved.json"
SEARCH_CONFIG = DATA_DIR / "search.yaml"
SEARCH_CONFIG_EXAMPLE = ROOT / "search.example.yaml"
COMPANIES_FILE = ROOT / "companies.yaml"
RANKING_LABELS = DATA_DIR / "ranking_labels.csv"
DB_PATH = DATA_DIR / "ademly.db"

MODEL = "claude-opus-5-5"

# Opus 5.5 defaults to medium, so every stage sets effort explicitly.
EFFORT = {
    "extract": "low",
    "rank": "medium",
    "tailor": "high",
    "verify": "high",
    "report": "medium",
}

# USD per million tokens for MODEL. Cache writes assume the 5-minute TTL.
PRICE_PER_MTOK = {
    "input": 4.00,
    "output": 20.00,
    "cache_read": 0.20,
    "cache_write": 5.00,
}

MAX_JOBS_PER_RUN = 20
MAX_JOBS_PER_COMPANY = 5
TAILOR_CONCURRENCY = 5
MAX_TAILOR_ROUNDS = 3

# Discovery
MAX_RANK_PER_RUN = 40        # caps ranking cost per run; the rest wait for later runs
RANK_CONCURRENCY = 5
FETCH_CONCURRENCY = 8
MAX_SEARCHES_PER_RUN = 6
SEARCH_REPEAT_DAYS = 14      # a query isn't repeated within this window

# Company history: when a company is skipped for a while
COOLDOWN_NO_MATCHES_DAYS = 14     # no open roles passed the filters
COOLDOWN_LOW_FIT_DAYS = 60        # roles get ranked, but never well
LOW_FIT_MIN_RANKED = 4            # ranked roles needed before judging fit
COOLDOWN_SKIPPED_DAYS = 60        # you keep skipping its roles
SKIPPED_WITHOUT_APPLYING = 3
