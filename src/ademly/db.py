"""SQLite job log, company history, and state machine."""

import re
import sqlite3
import threading
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
    # Only jobs rejected by the code filters return, when the settings change.
    S.FILTERED_OUT: {S.DISCOVERED},
    # Before tailoring starts, a selected job can still be dropped (its company
    # was excluded, or it duplicates another) or re-ranked (settings changed).
    S.SELECTED: {S.TAILORING, S.FILTERED_OUT, S.DISCOVERED} | _STOPPED,
    S.TAILORING: {S.VERIFIED} | _STOPPED,
    S.VERIFIED: {S.RENDERED} | _STOPPED,
    S.RENDERED: {S.READY_TO_APPLY} | _STOPPED,
    S.READY_TO_APPLY: {S.APPLIED, S.SKIPPED},
    S.APPLIED: set(),
    S.SKIPPED: {S.APPLIED},
    S.NEEDS_REVIEW: {S.TAILORING, S.SKIPPED},
    S.FAILED: {S.TAILORING, S.SKIPPED},
}

# States a job reaches only after being selected for a run.
SELECTED_OR_LATER = sorted(
    {S.SELECTED, S.READY_TO_APPLY, S.APPLIED, S.SKIPPED} | _WORKING | _STOPPED
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    ats TEXT NOT NULL,
    board_token TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'excluded', 'invalid')),
    status_reason TEXT,
    cooldown_until TEXT,
    cooldown_reason TEXT,
    cooldown_context TEXT,
    last_fetched_at TEXT,
    added_at TEXT NOT NULL,
    UNIQUE (ats, board_token)
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    query TEXT NOT NULL,
    results INTEGER NOT NULL,
    new_companies INTEGER NOT NULL,
    error TEXT,
    ran_at TEXT NOT NULL
);

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
    passed_filters INTEGER NOT NULL DEFAULT 0,
    location_match TEXT,
    score_context TEXT,
    rank_json TEXT,
    filter_context TEXT,
    eligible INTEGER,
    UNIQUE (ats, board_token, ats_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fuzzy ON jobs(fuzzy_key);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(ats, board_token);

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

# Columns added after the first release, for databases created before them.
_MIGRATIONS = {
    "jobs": {
        "passed_filters": "INTEGER NOT NULL DEFAULT 0",
        "location_match": "TEXT",
        "score_context": "TEXT",
        "rank_json": "TEXT",
        "filter_context": "TEXT",
        "eligible": "INTEGER",
    },
    "companies": {
        "cooldown_context": "TEXT",
    },
}

# A pause caused by the user's own choices, which settings changes don't lift.
USER_COOLDOWN = "user"

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


def iso(moment: datetime | None = None) -> str:
    """UTC timestamps in one fixed format, so they compare correctly as text."""
    return (moment or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def fuzzy_key(company: str, title: str, location: str) -> str:
    """Key that matches reposts of one role under a new ATS job ID."""
    name = _norm(_COMPANY_SUFFIX.sub(" ", company.casefold()))
    role = _norm(_REQ_NUMBER.sub(" ", title.casefold()))
    return f"{name}|{role}|{_norm(location)}"


def role_key(job) -> tuple[str, str, str]:
    """Matches one company's copies of a role posted for different locations."""
    return (job["ats"], job["board_token"], _norm(job["title"]))


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # LLM calls run in worker threads and log usage here, guarded by the lock.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        # Tables first, so an older jobs table can gain its new columns before
        # the index statements run.
        tables, _, rest = SCHEMA.partition("CREATE INDEX")
        self.conn.executescript(tables)
        for table, columns in _MIGRATIONS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        self.conn.executescript("CREATE INDEX" + rest)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # Runs

    def start_run(self, description: str, now: datetime | None = None) -> int:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (description, started_at) VALUES (?, ?)",
                (description, iso(now)),
            )
        return cursor.lastrowid

    def finish_run(self, run_id: int, now: datetime | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (iso(now), run_id))

    def run_cost(self, run_id: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) total FROM llm_calls WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["total"]

    # Jobs

    def add_posting(self, posting: Posting) -> AddResult:
        """Record a discovered posting, or report it as already known.

        A repost is stored as filtered_out so its new ATS ID is remembered too.
        """
        with self._lock, self.conn:
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
            now = iso()
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

    def transition(
        self, job_id: int, to: S, reason: str | None = None, run_id: int | None = None
    ) -> None:
        """Change a job's state and log it in one committed transaction."""
        with self._lock, self.conn:
            current = self.state(job_id)
            if to not in TRANSITIONS[current]:
                raise IllegalTransition(f"job {job_id}: {current} -> {to}")
            self.conn.execute(
                "UPDATE jobs SET state=?, updated_at=?, run_id=COALESCE(?, run_id) WHERE id=?",
                (to, iso(), run_id, job_id),
            )
            self._log_event(job_id, current, to, reason)

    def _log_event(self, job_id: int, old: S | None, new: S, reason: str | None) -> None:
        self.conn.execute(
            """INSERT INTO job_events (job_id, from_state, to_state, reason, created_at)
               VALUES (?,?,?,?,?)""",
            (job_id, old, new, reason, iso()),
        )

    def state(self, job_id: int) -> S:
        row = self.conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(f"no job with id {job_id}")
        return S(row["state"])

    def job(self, job_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(f"no job with id {job_id}")
        return row

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()

    def selectable(self) -> list[sqlite3.Row]:
        """Jobs eligible for selection. Anything further along never returns here."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY id", (S.DISCOVERED,)
        ).fetchall()

    def mark_passed(
        self, job_id: int, location_match: str, display_location: str | None = None
    ) -> None:
        """Record that a job passed the filters, showing the location that matched."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE jobs SET passed_filters=1, location_match=?,
                   location=COALESCE(?, location) WHERE id=?""",
                (location_match, display_location, job_id),
            )

    def set_filter_context(self, job_id: int, context: str | None) -> None:
        """Mark a job as rejected by the code filters under these settings."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE jobs SET filter_context=? WHERE id=?", (context, job_id))

    def requeue_outdated_selections(self, context: str) -> int:
        """Send selected jobs back for re-ranking if they were scored under old settings."""
        rows = self.conn.execute(
            """SELECT id FROM jobs WHERE state=?
               AND (score_context IS NULL OR score_context != ?)""",
            (S.SELECTED, context),
        ).fetchall()
        for row in rows:
            self.transition(row["id"], S.DISCOVERED, "search settings changed, re-ranking")
        return len(rows)

    def company_by_board(self, ats: str, board_token: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM companies WHERE ats=? AND board_token=?", (ats, board_token)
        ).fetchone()

    def deselect_company_jobs(self, ats: str, board_token: str, reason: str) -> int:
        rows = self.conn.execute(
            "SELECT id FROM jobs WHERE ats=? AND board_token=? AND state=?",
            (ats, board_token, S.SELECTED),
        ).fetchall()
        for row in rows:
            self.transition(row["id"], S.FILTERED_OUT, reason)
        return len(rows)

    def taken_roles(self) -> dict[tuple[str, str, str], int]:
        """Roles already selected in any run, keyed by board and normalized title."""
        placeholders = ",".join("?" * len(SELECTED_OR_LATER))
        rows = self.conn.execute(
            f"SELECT id, ats, board_token, title FROM jobs WHERE state IN ({placeholders}) ORDER BY id",
            SELECTED_OR_LATER,
        )
        taken: dict[tuple[str, str, str], int] = {}
        for row in rows:
            taken.setdefault(role_key(row), row["id"])
        return taken

    def close_unlisted(self, ats: str, board_token: str, listed: set[str]) -> int:
        """Drop discovered jobs that a board no longer lists."""
        rows = self.conn.execute(
            "SELECT id, ats_job_id FROM jobs WHERE ats=? AND board_token=? AND state=?",
            (ats, board_token, S.DISCOVERED),
        ).fetchall()
        closed = 0
        for row in rows:
            if row["ats_job_id"] not in listed:
                self.transition(row["id"], S.FILTERED_OUT, "no longer listed")
                closed += 1
        return closed

    def jobs_to_rank(self, context: str) -> list[sqlite3.Row]:
        """Open jobs that passed the filters but lack a score for this context."""
        return self.conn.execute(
            """SELECT * FROM jobs WHERE state=? AND passed_filters=1
               AND (score_context IS NULL OR score_context != ?)""",
            (S.DISCOVERED, context),
        ).fetchall()

    def save_ranking(
        self, job_id: int, score: int, context: str, rank_json: str, eligible: bool = True
    ) -> None:
        """Store a score. `eligible` is false when the job was ruled out on
        location, work authorization, years, pay, or industry, so its score
        says nothing about how well the company fits."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE jobs SET score=?, score_context=?, rank_json=?, eligible=? WHERE id=?",
                (score, context, rank_json, int(eligible), job_id),
            )

    def ranked_open_jobs(self, context: str, min_score: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM jobs WHERE state=? AND score_context=? AND score>=?
               ORDER BY score DESC, id""",
            (S.DISCOVERED, context, min_score),
        ).fetchall()

    def list_jobs(self, state: str | None = None) -> list[sqlite3.Row]:
        if state:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY score DESC, id", (state,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state NOT IN (?, ?) ORDER BY score DESC, id",
            (S.FILTERED_OUT, S.DISCOVERED),
        ).fetchall()

    def ranked_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE score IS NOT NULL ORDER BY score DESC, id"
        ).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state")
        return {row["state"]: row["n"] for row in rows}

    # Companies

    def upsert_company(
        self, ats: str, board_token: str, name: str, source: str, now: datetime | None = None
    ) -> tuple[int, bool]:
        """Add a company if it's new. Returns its ID and whether it was added."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM companies WHERE ats=? AND board_token=?", (ats, board_token)
            ).fetchone()
            if row:
                return row["id"], False
            cursor = self.conn.execute(
                """INSERT INTO companies (ats, board_token, name, source, added_at)
                   VALUES (?,?,?,?,?)""",
                (ats, board_token, name, source, iso(now)),
            )
        return cursor.lastrowid, True

    def company(self, company_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()

    def companies_to_fetch(
        self, now: datetime | None = None, current_contexts: set[str] | None = None
    ) -> list[sqlite3.Row]:
        """Active companies, except those still paused under the current settings.

        A pause set under settings that have since changed no longer applies.
        Pauses from the user's own choices always apply until they end.
        """
        rows = self.conn.execute(
            "SELECT * FROM companies WHERE status='active' ORDER BY id"
        ).fetchall()
        moment = iso(now)
        due = []
        for row in rows:
            paused = row["cooldown_until"] is not None and row["cooldown_until"] > moment
            if paused and current_contexts is not None:
                context = row["cooldown_context"]
                paused = context == USER_COOLDOWN or context in current_contexts
            if not paused:
                due.append(row)
        return due

    def find_companies(self, query: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM companies WHERE board_token=? COLLATE NOCASE OR name=? COLLATE NOCASE",
            (query, query),
        ).fetchall()

    def set_company_status(self, company_id: int, status: str, reason: str | None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE companies SET status=?, status_reason=? WHERE id=?",
                (status, reason, company_id),
            )

    def mark_fetched(
        self, company_id: int, now: datetime | None = None, name: str | None = None
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE companies SET last_fetched_at=?, name=COALESCE(?, name) WHERE id=?",
                (iso(now), name.strip() if name else None, company_id),
            )

    def set_cooldown(
        self,
        company_id: int,
        until: datetime | None,
        reason: str | None,
        context: str | None = None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE companies SET cooldown_until=?, cooldown_reason=?,
                   cooldown_context=? WHERE id=?""",
                (iso(until) if until else None, reason, context, company_id),
            )

    def company_stats(self, min_score: int) -> list[sqlite3.Row]:
        """Every company with its history across all runs."""
        placeholders = ",".join("?" * len(SELECTED_OR_LATER))
        return self.conn.execute(
            f"""SELECT c.*,
                COUNT(j.id) AS seen,
                COALESCE(SUM(j.passed_filters), 0) AS passed,
                COALESCE(SUM(j.score IS NOT NULL), 0) AS ranked,
                MAX(j.score) AS best_score,
                COALESCE(SUM(j.score IS NOT NULL AND COALESCE(j.eligible, 1) = 1), 0)
                    AS ranked_eligible,
                MAX(CASE WHEN COALESCE(j.eligible, 1) = 1 THEN j.score END) AS best_eligible,
                COALESCE(SUM(j.score >= ? AND COALESCE(j.eligible, 1) = 1), 0) AS good,
                COALESCE(SUM(j.state IN ({placeholders})), 0) AS selected,
                COALESCE(SUM(j.state = 'applied'), 0) AS applied,
                COALESCE(SUM(j.state = 'skipped'), 0) AS skipped
            FROM companies c
            LEFT JOIN jobs j ON j.ats = c.ats AND j.board_token = c.board_token
            GROUP BY c.id ORDER BY c.name COLLATE NOCASE""",
            (min_score, *SELECTED_OR_LATER),
        ).fetchall()

    # Searches

    def record_search(
        self,
        run_id: int | None,
        query: str,
        results: int,
        new_companies: int,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO searches (run_id, query, results, new_companies, error, ran_at)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, query, results, new_companies, error, iso(now)),
            )

    def recent_queries(self, since: datetime) -> set[str]:
        rows = self.conn.execute(
            "SELECT query FROM searches WHERE error IS NULL AND ran_at >= ?", (iso(since),)
        )
        return {row["query"] for row in rows}

    def searches(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM searches ORDER BY id DESC").fetchall()

    # LLM usage

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
        with self._lock, self.conn:
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
                    iso(),
                ),
            )
