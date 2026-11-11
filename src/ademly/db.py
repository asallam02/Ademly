"""SQLite job log and state machine."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ademly.models import JobState as S
from ademly.models import Posting

_WORKING = {S.SELECTED, S.TAILORING, S.VERIFIED, S.RENDERED}
_STOPPED = {S.NEEDS_REVIEW, S.FAILED}

TRANSITIONS: dict[S, set[S]] = {
    S.DISCOVERED: {S.FILTERED_OUT, S.SELECTED},
    S.FILTERED_OUT: set(),
    S.SELECTED: {S.TAILORING} | _STOPPED,
    S.TAILORING: {S.VERIFIED} | _STOPPED,
    S.VERIFIED: {S.RENDERED} | _STOPPED,
    S.RENDERED: {S.READY_TO_APPLY} | _STOPPED,
    S.READY_TO_APPLY: {S.APPLIED, S.SKIPPED},
    S.APPLIED: set(),
    S.SKIPPED: {S.APPLIED},
    S.NEEDS_REVIEW: {S.TAILORING, S.SKIPPED},
    S.FAILED: {S.TAILORING, S.SKIPPED},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    ats TEXT NOT NULL,
    board_token TEXT NOT NULL,
    ats_job_id TEXT NOT NULL,
    fuzzy_key TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    url TEXT NOT NULL,
    apply_url TEXT NOT NULL,
    posting_json TEXT NOT NULL,
    state TEXT NOT NULL,
    score INTEGER,
    run_id INTEGER REFERENCES runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (ats, board_token, ats_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fuzzy ON jobs(fuzzy_key);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    keyword TEXT NOT NULL,
    importance TEXT NOT NULL CHECK (importance IN ('required', 'preferred')),
    UNIQUE (job_id, keyword)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    job_id INTEGER REFERENCES jobs(id),
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""

_COMPANY_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh)\b\.?")
_REQ_NUMBER = re.compile(r"\b(req|r|jr|job)[-#\s]*\d+\b")


class IllegalTransition(Exception):
    pass


class UnknownJob(Exception):
    pass


@dataclass(frozen=True)
class AddResult:
    job_id: int
    status: Literal["new", "existing", "duplicate"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def fuzzy_key(company: str, title: str, location: str) -> str:
    """Key that matches reposts of one role under a new ATS job ID."""
    name = _norm(_COMPANY_SUFFIX.sub(" ", company.casefold()))
    role = _norm(_REQ_NUMBER.sub(" ", title.casefold()))
    return f"{name}|{role}|{_norm(location)}"


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def add_posting(self, posting: Posting) -> AddResult:
        """Record a discovered posting, or report it as already known.

        A repost is stored as filtered_out so its new ATS ID is remembered too.
        """
        with self.conn:
            existing = self.conn.execute(
                "SELECT id FROM jobs WHERE ats=? AND board_token=? AND ats_job_id=?",
                (posting.ats, posting.board_token, posting.job_id),
            ).fetchone()
            if existing:
                return AddResult(existing["id"], "existing")

            key = fuzzy_key(posting.company, posting.title, posting.location)
            twin = self.conn.execute(
                "SELECT id FROM jobs WHERE fuzzy_key=? ORDER BY id LIMIT 1", (key,)
            ).fetchone()
            state = S.FILTERED_OUT if twin else S.DISCOVERED
            now = _now()
            cursor = self.conn.execute(
                """INSERT INTO jobs (ats, board_token, ats_job_id, fuzzy_key, company,
                   title, location, url, apply_url, posting_json, state, created_at,
                   updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    posting.ats,
                    posting.board_token,
                    posting.job_id,
                    key,
                    posting.company,
                    posting.title,
                    posting.location,
                    posting.url,
                    posting.apply_url,
                    posting.model_dump_json(),
                    state,
                    now,
                    now,
                ),
            )
            job_id = cursor.lastrowid
            reason = f"duplicate of job {twin['id']}" if twin else "discovered"
            self._log_event(job_id, None, state, reason)
        return AddResult(job_id, "duplicate" if twin else "new")

    def transition(self, job_id: int, to: S, reason: str | None = None) -> None:
        """Change a job's state and log it in one committed transaction."""
        with self.conn:
            current = self.state(job_id)
            if to not in TRANSITIONS[current]:
                raise IllegalTransition(f"job {job_id}: {current} -> {to}")
            self.conn.execute(
                "UPDATE jobs SET state=?, updated_at=? WHERE id=?", (to, _now(), job_id)
            )
            self._log_event(job_id, current, to, reason)

    def _log_event(self, job_id: int, old: S | None, new: S, reason: str | None) -> None:
        self.conn.execute(
            """INSERT INTO job_events (job_id, from_state, to_state, reason, created_at)
               VALUES (?,?,?,?,?)""",
            (job_id, old, new, reason, _now()),
        )

    def state(self, job_id: int) -> S:
        row = self.conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(f"no job with id {job_id}")
        return S(row["state"])

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()

    def selectable(self) -> list[sqlite3.Row]:
        """Jobs eligible for selection. Anything further along never returns here."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY id", (S.DISCOVERED,)
        ).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state")
        return {row["state"]: row["n"] for row in rows}

    def log_llm_call(
        self,
        *,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        run_id: int | None = None,
        job_id: int | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO llm_calls (run_id, job_id, stage, model, input_tokens,
                   output_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
                   created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    job_id,
                    stage,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                    _now(),
                ),
            )
