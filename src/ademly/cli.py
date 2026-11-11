"""Command line interface."""

import argparse
import sys

import httpx
from dotenv import load_dotenv

from ademly import config
from ademly.db import Database, IllegalTransition, UnknownJob
from ademly.discover.ats import BoardNotFound, fetch_board
from ademly.intake import master_resume as intake
from ademly.models import JobState

SAMPLE_BOARDS = [("greenhouse", "stripe"), ("lever", "spotify"), ("ashby", "ramp")]


def cmd_init(args: argparse.Namespace) -> int:
    if not config.MASTER_RESUME_SOURCE.exists():
        print(f"Missing {config.MASTER_RESUME_SOURCE}", file=sys.stderr)
        return 1
    source = config.MASTER_RESUME_SOURCE.read_text()
    policy = intake.load_policy(config.RESUME_POLICY)
    try:
        resume = intake.parse_master_resume(source, policy)
    except (intake.ResumeParseError, intake.PolicyError) as error:
        print(f"Could not process the resume: {error}", file=sys.stderr)
        return 1

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


def cmd_status(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    counts = db.counts()
    db.close()
    if not counts:
        print("No jobs recorded yet.")
    for state, count in sorted(counts.items()):
        print(f"{state:16} {count}")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    try:
        db.transition(args.job_id, JobState(args.outcome), args.notes)
    except (IllegalTransition, UnknownJob) as error:
        print(f"Could not update the job: {error}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"Job {args.job_id} marked {args.outcome}.")
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

    status = commands.add_parser("status", help="show job counts by state")
    status.set_defaults(func=cmd_status)

    mark = commands.add_parser("mark", help="record what happened with a job")
    mark.add_argument("job_id", type=int)
    mark.add_argument("outcome", choices=["applied", "skipped"])
    mark.add_argument("--notes", help="form questions asked, CAPTCHA seen, and so on")
    mark.set_defaults(func=cmd_mark)

    check = commands.add_parser("check-ats", help="confirm the job board APIs respond")
    check.set_defaults(func=cmd_check_ats)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(config.ROOT / ".env")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
