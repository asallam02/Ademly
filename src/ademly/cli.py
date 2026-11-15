"""Command line interface."""

import argparse
import json
import sys
from datetime import UTC, datetime

import anthropic
import httpx
from dotenv import load_dotenv

from ademly import config
from ademly.db import Database, IllegalTransition, UnknownJob
from ademly.discover.ats import BoardNotFound, fetch_board
from ademly.discover.companies import load_seed_companies
from ademly.discover.filters import SearchConfigMissing, load_search_config
from ademly.intake import master_resume as intake
from ademly.models import JobState

SAMPLE_BOARDS = [("greenhouse", "stripe"), ("lever", "spotify"), ("ashby", "ramp")]


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _load_approved_resume():
    """The master resume, only if this exact version was approved."""
    if not config.MASTER_RESUME_SOURCE.exists():
        raise RuntimeError(f"Missing {config.MASTER_RESUME_SOURCE}. See usage.md.")
    source = config.MASTER_RESUME_SOURCE.read_text()
    resume = intake.parse_master_resume(source, intake.load_policy(config.RESUME_POLICY))
    if not intake.is_approved(resume, config.MASTER_RESUME_APPROVAL):
        raise RuntimeError(
            "Your master resume isn't approved in its current form. "
            "Run `ademly init`, review it, then run `ademly init --approve`."
        )
    return resume


def _search_config():
    return load_search_config(config.SEARCH_CONFIG, config.SEARCH_CONFIG_EXAMPLE)


def cmd_init(args: argparse.Namespace) -> int:
    if not config.MASTER_RESUME_SOURCE.exists():
        return _fail(f"Missing {config.MASTER_RESUME_SOURCE}")
    source = config.MASTER_RESUME_SOURCE.read_text()
    policy = intake.load_policy(config.RESUME_POLICY)
    try:
        resume = intake.parse_master_resume(source, policy)
    except (intake.ResumeParseError, intake.PolicyError) as error:
        return _fail(f"Could not process the resume: {error}")

    config.DATA_DIR.mkdir(exist_ok=True)
    config.MASTER_RESUME_JSON.write_text(resume.model_dump_json(indent=2))
    processed = intake.apply_policy_to_text(source, policy)
    config.MASTER_RESUME_REVIEW.write_text(intake.render_review(source, processed, resume))

    print(f"Parsed {len(resume.entries)} entries and {len(resume.facts())} claims.")
    print(f"Review file: {config.MASTER_RESUME_REVIEW}")
    if args.approve:
        intake.write_approval(resume, config.MASTER_RESUME_APPROVAL)
        print("Approved. This exact version is now the source of truth.")
    elif intake.is_approved(resume, config.MASTER_RESUME_APPROVAL):
        print("Status: approved.")
    else:
        print("Status: NOT approved. After reviewing, run: ademly init --approve")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from ademly.llm import LLM
    from ademly.pipeline import discover

    try:
        resume = _load_approved_resume()
        search = _search_config()
    except (RuntimeError, SearchConfigMissing, intake.ResumeParseError, intake.PolicyError) as error:
        return _fail(str(error))

    db = Database(config.DB_PATH)
    try:
        report = discover(
            db,
            LLM(db=db),
            search,
            resume,
            seed_companies=load_seed_companies(config.COMPANIES_FILE),
            use_web_search=not args.no_web_search,
            max_rank=args.max_rank,
        )
    except anthropic.AuthenticationError:
        return _fail("The Anthropic API rejected the key. Check ANTHROPIC_API_KEY in .env.")
    finally:
        db.close()
    _print_report(report)
    return 0


def _print_report(report) -> None:
    print(f"Run {report.run_id}")
    if report.searches_run or report.searches_skipped:
        print(
            f"  Web searches: {report.searches_run} run, "
            f"{report.searches_skipped} skipped as recently searched"
        )
    print(
        f"  Companies: {report.companies_fetched} checked, "
        f"{report.companies_cooling_down} skipped for now, {report.companies_added} new"
    )
    for label in report.companies_invalid:
        print(f"    No job board found: {label}")
    for error in report.fetch_errors:
        print(f"    Error: {error}")
    if report.requeued:
        print(f"  Re-ranking {report.requeued} selected jobs, because your settings changed")
    print(f"  New postings: {report.new_postings}")
    if report.reconsidered:
        print(f"  Earlier rejections that pass your updated filters: {report.reconsidered}")
    for reason, count in report.filtered.most_common():
        print(f"    Filtered, {reason}: {count}")
    line = f"  Ranked: {report.ranked}"
    if report.rank_errors:
        line += f", {report.rank_errors} failed and will retry next run"
    if report.left_unranked:
        line += f", {report.left_unranked} waiting for the next run"
    print(line)
    for note in report.cooldowns_set:
        print(f"    Skipping for a while: {note}")
    print(f"  Selected: {len(report.selected)}")
    for job in report.selected:
        print(f"    [{job['id']}] {job['score']:3}  {job['company']} - {job['title']} ({job['location']})")
    print(f"  Estimated cost: ${report.cost_usd:.2f}")


def cmd_status(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    counts = db.counts()
    db.close()
    if not counts:
        print("No jobs recorded yet.")
    for state, count in sorted(counts.items()):
        print(f"{state:16} {count}")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    rows = db.list_jobs(args.state)
    db.close()
    if not rows:
        print("No matching jobs.")
    for job in rows:
        score = "" if job["score"] is None else job["score"]
        print(f"[{job['id']}] {job['state']:14} {score!s:>3}  {job['company']} - {job['title']}")
        print(f"      {job['location']} | {job['url']}")
        if args.details and job["rank_json"]:
            ranking = json.loads(job["rank_json"])
            print(f"      {ranking['summary']}")
            facts = []
            if ranking.get("pay_range"):
                facts.append(f"Pay: {ranking['pay_range']}")
            if ranking.get("years_required") is not None:
                facts.append(f"Asks for {ranking['years_required']}+ years")
            if ranking.get("mission_alignment"):
                facts.append(f"Mission: {ranking['mission_alignment']}")
            if ranking.get("product_oriented"):
                facts.append(f"Product role: {ranking['product_oriented']}")
            if facts:
                print(f"      {' | '.join(facts)}")
            if ranking["concerns"]:
                print(f"      Gaps: {'; '.join(ranking['concerns'])}")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    try:
        db.transition(args.job_id, JobState(args.outcome), args.notes)
    except (IllegalTransition, UnknownJob) as error:
        return _fail(f"Could not update the job: {error}")
    finally:
        db.close()
    print(f"Job {args.job_id} marked {args.outcome}.")
    return 0


def cmd_companies(args: argparse.Namespace) -> int:
    try:
        min_score = _search_config().min_score
    except SearchConfigMissing:
        min_score = 60
    db = Database(config.DB_PATH)
    try:
        if args.action in ("exclude", "include"):
            return _set_exclusion(db, args)
        rows = db.company_stats(min_score)
    finally:
        db.close()
    if not rows:
        print("No companies yet. They're added on the first `ademly run`.")
    now = datetime.now(UTC).isoformat()
    print(f"{'company':24} {'status':34} seen pass rank best sel applied skip")
    for row in rows:
        if row["status"] != "active":
            status = f"{row['status']}: {row['status_reason'] or ''}"
        elif row["cooldown_until"] and row["cooldown_until"] > now:
            status = f"paused to {row['cooldown_until'][:10]}"
        else:
            status = "active"
        if not args.all and row["seen"] == 0 and status == "active":
            continue
        best = "" if row["best_score"] is None else row["best_score"]
        print(
            f"{row['name'][:24]:24} {status[:34]:34} {row['seen']:4} {row['passed']:4} "
            f"{row['ranked']:4} {best!s:>4} {row['selected']:3} {row['applied']:7} {row['skipped']:4}"
        )
        if args.reasons and row["cooldown_reason"] and status.startswith("paused"):
            print(f"{'':25}why: {row['cooldown_reason']}")
    return 0


def _set_exclusion(db: Database, args: argparse.Namespace) -> int:
    if not args.name:
        return _fail(f"Usage: ademly companies {args.action} <name or board token>")
    matches = db.find_companies(args.name)
    if not matches:
        return _fail(f"No company named {args.name!r}. Run `ademly companies --all` to list them.")
    from ademly.pipeline import exclude_company

    for company in matches:
        if args.action == "exclude":
            dropped = exclude_company(db, company, args.reason or "excluded by you")
            note = f", {dropped} selected jobs removed" if dropped else ""
            print(f"{company['name']} ({company['ats']}): excluded{note}")
        else:
            db.set_company_status(company["id"], "active", None)
            db.set_cooldown(company["id"], None, None)
            print(f"{company['name']} ({company['ats']}): included")
    return 0


def cmd_searches(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    rows = db.searches()
    db.close()
    if not rows:
        print("No web searches yet.")
    for row in rows:
        error = f"  error: {row['error']}" if row["error"] else ""
        print(
            f"{row['ran_at'][:10]}  {row['results']:3} results, "
            f"{row['new_companies']} new companies  {row['query']}{error}"
        )
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    from ademly.labels import export_labels, score_labels

    db = Database(config.DB_PATH)
    try:
        if args.action == "export":
            written = export_labels(db, config.RANKING_LABELS, args.count)
            print(f"Wrote {written} jobs to {config.RANKING_LABELS}")
            print("Fill the label column with good or bad, then run: ademly label score")
            return 0
        if not config.RANKING_LABELS.exists():
            return _fail("No labels file yet. Run: ademly label export")
        min_score = _search_config().min_score
        result = score_labels(db, config.RANKING_LABELS, min_score)
    except SearchConfigMissing as error:
        return _fail(str(error))
    finally:
        db.close()
    if not result.labeled:
        return _fail("No labeled rows found. Use good or bad in the label column.")
    print(f"Agreement: {result.agreed}/{result.labeled} ({result.rate:.0%}). Target is 80%.")
    for line in result.disagreements:
        print(f"  {line}")
    return 0


def cmd_check_ats(args: argparse.Namespace) -> int:
    failed = False
    for ats, token in SAMPLE_BOARDS:
        try:
            postings = fetch_board(ats, token)
        except (BoardNotFound, httpx.HTTPError) as error:
            print(f"{ats:11} {token:10} FAILED: {error}")
            failed = True
            continue
        with_text = sum(1 for posting in postings if posting.description)
        print(f"{ats:11} {token:10} {len(postings)} postings, {with_text} with descriptions")
        if not postings or not with_text:
            failed = True
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ademly")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="process and review the master resume")
    init.add_argument("--approve", action="store_true", help="approve the current version")
    init.set_defaults(func=cmd_init)

    run = commands.add_parser("run", help="run the pipeline")
    run.add_argument("--stage", choices=["discover"], default="discover")
    run.add_argument("--no-web-search", action="store_true", help="only check known companies")
    run.add_argument(
        "--max-rank", type=int, default=config.MAX_RANK_PER_RUN,
        help="most postings to rank this run, which limits cost",
    )
    run.set_defaults(func=cmd_run)

    status = commands.add_parser("status", help="show job counts by state")
    status.set_defaults(func=cmd_status)

    jobs = commands.add_parser("jobs", help="list jobs")
    jobs.add_argument("--state", choices=[state.value for state in JobState])
    jobs.add_argument("--details", action="store_true", help="show the ranker's summary and gaps")
    jobs.set_defaults(func=cmd_jobs)

    mark = commands.add_parser("mark", help="record what happened with a job")
    mark.add_argument("job_id", type=int)
    mark.add_argument("outcome", choices=["applied", "skipped"])
    mark.add_argument("--notes", help="form questions asked, CAPTCHA seen, and so on")
    mark.set_defaults(func=cmd_mark)

    companies = commands.add_parser("companies", help="company history; exclude or include one")
    companies.add_argument("action", nargs="?", choices=["list", "exclude", "include"], default="list")
    companies.add_argument("name", nargs="?", help="company name or board token")
    companies.add_argument("--reason", help="why you're excluding it")
    companies.add_argument("--all", action="store_true", help="include companies with no jobs yet")
    companies.add_argument("--reasons", action="store_true", help="show why companies are paused")
    companies.set_defaults(func=cmd_companies)

    searches = commands.add_parser("searches", help="list past web searches")
    searches.set_defaults(func=cmd_searches)

    label = commands.add_parser("label", help="check the ranking against your judgment")
    label.add_argument("action", choices=["export", "score"])
    label.add_argument("--count", type=int, default=30)
    label.set_defaults(func=cmd_label)

    check = commands.add_parser("check-ats", help="confirm the job board APIs respond")
    check.set_defaults(func=cmd_check_ats)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(config.ROOT / ".env")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
