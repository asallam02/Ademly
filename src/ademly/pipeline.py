"""Runs the pipeline stages."""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from ademly import config
from ademly.db import USER_COOLDOWN, Database, role_key
from ademly.discover import companies as company_rules
from ademly.discover.ats import BoardNotFound, display_name, fetch_board
from ademly.discover.filters import SearchConfig, best_location, hard_filter
from ademly.discover.rank import RANK_PROMPT_VERSION, Ranking, build_system_prompt, rank_posting
from ademly.discover.web_search import board_from_url, run_web_search
from ademly.intake.master_resume import resume_hash
from ademly.llm import LLM, TRANSIENT_ERRORS
from ademly.models import JobState, MasterResume, Posting


@dataclass
class DiscoverReport:
    run_id: int
    searches_run: int = 0
    searches_skipped: int = 0
    companies_added: int = 0
    companies_fetched: int = 0
    companies_cooling_down: int = 0
    companies_invalid: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)
    new_postings: int = 0
    filtered: Counter = field(default_factory=Counter)
    requeued: int = 0
    reconsidered: int = 0
    ranked: int = 0
    rank_errors: int = 0
    left_unranked: int = 0
    selected: list = field(default_factory=list)
    cooldowns_set: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


def selection(
    candidates, max_total: int, max_per_company: int, taken: set | None = None
) -> list:
    """Pick the best jobs, already sorted by score, within the caps.

    A role is picked once, even when its company posts copies of it for
    different locations, and never if an earlier run already picked it.
    """
    taken = set(taken or ())
    chosen, per_company = [], Counter()
    for job in candidates:
        company = (job["ats"], job["board_token"])
        role = role_key(job)
        if per_company[company] >= max_per_company or role in taken:
            continue
        chosen.append(job)
        per_company[company] += 1
        taken.add(role)
        if len(chosen) == max_total:
            break
    return chosen


def ranking_context(resume: MasterResume, search: SearchConfig) -> str:
    raw = resume_hash(resume) + search.fingerprint() + RANK_PROMPT_VERSION
    return hashlib.sha256(raw.encode()).hexdigest()


def _published(job) -> str:
    return Posting.model_validate_json(job["posting_json"]).published_at or ""


def discover(
    db: Database,
    llm: LLM,
    search: SearchConfig,
    resume: MasterResume,
    *,
    seed_companies: list[company_rules.SeedCompany] = (),
    use_web_search: bool = True,
    max_rank: int = config.MAX_RANK_PER_RUN,
    fetch: Callable[[str, str], list[Posting]] = fetch_board,
    web_search: Callable = run_web_search,
    now: datetime | None = None,
) -> DiscoverReport:
    now = now or datetime.now(UTC)
    report = DiscoverReport(run_id=db.start_run(search.description, now))
    run_id = report.run_id

    context = ranking_context(resume, search)
    report.requeued = db.requeue_outdated_selections(context)

    for seed in seed_companies:
        company_id, added = db.upsert_company(seed.ats, seed.token, seed.name, "seed", now)
        report.companies_added += added
        if seed.exclude:
            exclude_company(db, db.company(company_id), seed.reason or "excluded in companies.yaml")

    if use_web_search and search.search_queries:
        _search_for_companies(db, llm, search, web_search, report, now)

    _apply_excluded_companies(db, search)
    contexts = cooldown_contexts(search, context)
    fetched_boards, passed_now = _fetch_boards(db, search, fetch, report, now, contexts)

    excluded = _rank_jobs(db, llm, search, resume, context, fetched_boards, max_rank, report, now)
    fetched_boards -= excluded

    candidates = [
        job
        for job in db.ranked_open_jobs(context, search.min_score)
        if (job["ats"], job["board_token"]) in fetched_boards
    ]
    taken = set(db.taken_roles())
    for job in selection(candidates, config.MAX_JOBS_PER_RUN, config.MAX_JOBS_PER_COMPANY, taken):
        db.transition(job["id"], JobState.SELECTED, f"score {job['score']}", run_id=run_id)
        report.selected.append(db.job(job["id"]))

    _update_company_fit(db, search, passed_now, report, now, contexts)

    db.finish_run(run_id)
    report.cost_usd = db.run_cost(run_id)
    return report


def _search_for_companies(db, llm, search, web_search, report, now) -> None:
    recent = db.recent_queries(now - timedelta(days=config.SEARCH_REPEAT_DAYS))
    todo = [query for query in search.search_queries if query not in recent]
    report.searches_skipped = len(search.search_queries) - len(todo)
    todo = todo[: config.MAX_SEARCHES_PER_RUN]
    if not todo:
        return
    try:
        results = web_search(llm, todo, run_id=report.run_id)
    except TRANSIENT_ERRORS as error:
        report.fetch_errors.append(f"web search failed: {error}")
        return
    for result in results:
        added = 0
        for board in {board_from_url(url) for url in result.urls} - {None}:
            ats, token = board
            if db.upsert_company(ats, token, display_name(token), "search", now)[1]:
                added += 1
        report.companies_added += added
        report.searches_run += 1
        db.record_search(report.run_id, result.query, len(result.urls), added, result.error, now)


def _apply_excluded_companies(db, search) -> None:
    for name in search.excluded_companies:
        for company in db.find_companies(name):
            exclude_company(db, company, "excluded in your search settings")


def cooldown_contexts(search: SearchConfig, rank_context: str) -> dict[str, str]:
    """The current settings each kind of company pause is based on."""
    return {
        "filters": f"filters:{search.filter_fingerprint()}",
        "ranking": f"ranking:{rank_context}",
        "user": USER_COOLDOWN,
    }


def _fetch_boards(db, search, fetch, report, now, contexts):
    all_active = db.conn.execute("SELECT COUNT(*) n FROM companies WHERE status='active'").fetchone()["n"]
    companies = db.companies_to_fetch(now, set(contexts.values()))
    report.companies_cooling_down = all_active - len(companies)

    def load(company):
        try:
            return company, fetch(company["ats"], company["board_token"]), None
        except (BoardNotFound, httpx.HTTPError) as error:
            return company, None, error

    with ThreadPoolExecutor(config.FETCH_CONCURRENCY) as pool:
        results = list(pool.map(load, companies))

    filter_context = search.filter_fingerprint()
    fetched_boards: set[tuple[str, str]] = set()
    passed_now: dict[int, int] = {}
    for company, postings, error in results:
        label = f"{company['name']} ({company['ats']})"
        if isinstance(error, BoardNotFound):
            db.set_company_status(company["id"], "invalid", "job board not found")
            report.companies_invalid.append(label)
            continue
        if error is not None:
            report.fetch_errors.append(f"{label}: {error}")
            continue

        greenhouse_name = postings[0].company if postings and company["ats"] == "greenhouse" else None
        db.mark_fetched(company["id"], now, name=greenhouse_name)
        fetched_boards.add((company["ats"], company["board_token"]))
        report.companies_fetched += 1

        passed = 0
        for posting in postings:
            added = db.add_posting(posting)
            if added.status == "new":
                report.new_postings += 1
                _apply_hard_filter(db, added.job_id, posting, search, filter_context, now, report)
            elif added.status == "duplicate":
                report.filtered["repost of a known job"] += 1
            else:
                job = db.job(added.job_id)
                stale = job["filter_context"] and job["filter_context"] != filter_context
                if job["state"] == JobState.FILTERED_OUT and stale:
                    _reconsider(db, job, posting, search, filter_context, now, report)
            if db.job(added.job_id)["passed_filters"]:
                passed += 1
        db.close_unlisted(company["ats"], company["board_token"], {p.job_id for p in postings})
        passed_now[company["id"]] = passed
    return fetched_boards, passed_now


def _apply_hard_filter(db, job_id, posting, search, filter_context, now, report) -> None:
    reason, verdict = hard_filter(posting, search, now)
    if reason:
        db.transition(job_id, JobState.FILTERED_OUT, reason)
        # Remembered so the job can be reconsidered if the filter settings change.
        db.set_filter_context(job_id, filter_context)
        report.filtered[_reason_group(reason)] += 1
    else:
        db.mark_passed(job_id, verdict, best_location(posting, search))


def _reconsider(db, job, posting, search, filter_context, now, report) -> None:
    """Re-check a job the code filters rejected under earlier settings."""
    reason, verdict = hard_filter(posting, search, now)
    if reason:
        db.set_filter_context(job["id"], filter_context)
        return
    db.transition(job["id"], JobState.DISCOVERED, "passes the updated filters")
    db.set_filter_context(job["id"], None)
    db.mark_passed(job["id"], verdict, best_location(posting, search))
    report.reconsidered += 1


def _reason_group(reason: str) -> str:
    if reason.startswith("asks for "):
        return "asks for more years of experience than your limit"
    return reason


def _rank_jobs(db, llm, search, resume, context, fetched_boards, max_rank, report, now) -> set:
    waiting = [
        job
        for job in db.jobs_to_rank(context)
        if (job["ats"], job["board_token"]) in fetched_boards
    ]
    # Clear location matches first, newest first within each group.
    waiting.sort(key=_published, reverse=True)
    waiting.sort(key=lambda job: job["location_match"] != "match")
    batch = waiting[:max_rank]
    report.left_unranked = len(waiting) - len(batch)
    excluded_boards: set[tuple[str, str]] = set()
    if not batch:
        return excluded_boards

    system = build_system_prompt(resume, search, now.date())

    def score(job):
        posting = Posting.model_validate_json(job["posting_json"])
        try:
            return job, rank_posting(llm, system, posting, report.run_id, job["id"]), None
        except TRANSIENT_ERRORS as error:
            return job, None, error

    # Only the workers' usage logging touches the database until all are done.
    with ThreadPoolExecutor(config.RANK_CONCURRENCY) as pool:
        results = list(pool.map(score, batch))

    for job, ranking, error in results:
        if error is not None:
            report.rank_errors += 1
            continue
        report.ranked += 1
        reason = _ranking_rejection(ranking, search)
        eligible = reason is None or reason.startswith("score below")
        db.save_ranking(job["id"], ranking.score, context, ranking.model_dump_json(), eligible)
        board = (job["ats"], job["board_token"])
        if board in excluded_boards:
            db.transition(job["id"], JobState.FILTERED_OUT, "company excluded this run")
            continue
        if reason:
            db.transition(job["id"], JobState.FILTERED_OUT, reason)
            report.filtered[reason.split(":")[0]] += 1
        if ranking.excluded_industry and ranking.excluded_industry_is_main_business:
            company = db.company_by_board(*board)
            if company is not None:
                exclude_company(db, company, f"excluded industry: {ranking.excluded_industry}")
                report.cooldowns_set.append(
                    f"{company['name']}: excluded, industry is {ranking.excluded_industry}"
                )
            excluded_boards.add(board)
    return excluded_boards


def _ranking_rejection(ranking: Ranking, search: SearchConfig) -> str | None:
    if ranking.location_eligible == "no":
        return "ranker: location not eligible"
    if ranking.work_authorization_ok == "no":
        return "ranker: work authorization required"
    if ranking.excluded_industry:
        return f"ranker: excluded industry ({ranking.excluded_industry})"
    if (
        search.max_years_required is not None
        and ranking.years_required is not None
        and ranking.years_required > search.max_years_required
    ):
        return f"ranker: asks for {ranking.years_required}+ years"
    if search.min_salary and ranking.pay_max_cad is not None and ranking.pay_max_cad < search.min_salary:
        return f"ranker: pay below minimum (top of range {ranking.pay_max_cad:,} CAD)"
    if ranking.score < search.min_score:
        return f"score below minimum: {ranking.score} < {search.min_score}"
    return None


def _update_company_fit(db, search, passed_now, report, now, contexts) -> None:
    stats = {row["id"]: row for row in db.company_stats(search.min_score)}
    for company_id, passed in passed_now.items():
        row = stats[company_id]
        cooldown = company_rules.assess_fit(row, passed, search.min_score)
        if cooldown is None:
            db.set_cooldown(company_id, None, None)
            continue
        db.set_cooldown(
            company_id,
            company_rules.cooldown_until(now, cooldown),
            cooldown.reason,
            contexts[cooldown.basis],
        )
        if cooldown.days > config.COOLDOWN_NO_MATCHES_DAYS:
            report.cooldowns_set.append(f"{row['name']}: {cooldown.reason}")


def exclude_company(db: Database, company, reason: str) -> int:
    """Stop checking a company and drop its jobs that are selected but not yet tailored."""
    if company["status"] != "excluded":
        db.set_company_status(company["id"], "excluded", reason)
    return db.deselect_company_jobs(company["ats"], company["board_token"], f"company excluded: {reason}")


def deselect_duplicate_roles(db: Database) -> int:
    """Keep one selected copy of each role, the highest scoring."""
    selected = sorted(
        db.list_jobs(JobState.SELECTED),
        key=lambda job: (-(job["score"] or 0), job["location_match"] != "match", job["id"]),
    )
    kept: dict = {}
    dropped = 0
    for job in selected:
        key = role_key(job)
        if key in kept:
            db.transition(job["id"], JobState.FILTERED_OUT, f"same role as job {kept[key]}")
            dropped += 1
        else:
            kept[key] = job["id"]
    return dropped


def ranking_details(job) -> dict:
    return json.loads(job["rank_json"]) if job["rank_json"] else {}
