import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ademly import config
from ademly.db import Database
from ademly.discover.companies import SeedCompany, assess_fit
from ademly.discover.filters import SearchConfig, hard_filter, location_match, title_rejection
from ademly.discover.rank import Ranking, build_system_prompt, posting_message
from ademly.discover.web_search import board_from_url, run_web_search
from ademly.discover.ats import BoardNotFound
from ademly.intake.master_resume import parse_master_resume
from ademly.labels import export_labels, score_labels
from ademly.llm import LLMRefused
from ademly.models import JobState, Posting
from ademly.pipeline import discover, selection

from tests.test_intake import SAMPLE

NOW = datetime(2026, 11, 15, tzinfo=UTC)
SEARCH = SearchConfig(
    description="Backend roles",
    cities=["Toronto", "Vancouver"],
    search_queries=["backend engineer Toronto", "platform engineer remote Canada"],
)


def make_posting(token="acme", job_id="1", title="Backend Engineer", location="Toronto, ON",
                 locations=None, remote=None, published=None, ats="greenhouse"):
    return Posting(
        ats=ats, board_token=token, job_id=job_id, company=token.title(), title=title,
        location=location, locations=locations or [location], remote=remote,
        description="Build services in Java.", url=f"https://x.test/{token}/{job_id}",
        apply_url=f"https://x.test/{token}/{job_id}/apply",
        published_at=(published or NOW - timedelta(days=3)).isoformat(),
    )


# Filters


@pytest.mark.parametrize(
    "title, rejected",
    [
        ("Backend Engineer", False),
        ("Software Engineer II, Platform", False),
        ("Site Reliability Engineer", False),
        ("Senior Backend Engineer", True),
        ("Sr. Software Engineer", True),
        ("Engineering Manager", True),
        ("Solutions Engineer", True),
        ("Data Analyst", True),
        ("Leading Edge Engineer", False),
    ],
)
def test_title_filter(title, rejected):
    assert (title_rejection(title, SEARCH) is not None) is rejected


@pytest.mark.parametrize(
    "locations, remote, expected",
    [
        (["Toronto, ON"], None, "match"),
        (["New York, NY", "Vancouver, BC"], None, "match"),
        (["Remote (Canada)"], True, "match"),
        (["Canada"], True, "match"),
        (["Canada"], False, "maybe"),
        (["Remote"], True, "maybe"),
        (["North America"], None, "maybe"),
        (["Remote (US)"], True, "no"),
        (["San Francisco, CA"], None, "no"),
        (["Dublin"], None, "no"),
        (["NZ-New Zealand-Remote"], True, "no"),
        (["Remote - Mexico", "Mexico"], True, "no"),
        (["Remote, Poland", "EMEA"], True, "no"),
        (["Remote Ireland"], True, "no"),
        (["Remote - Seattle", "Remote - SF Bay Area"], True, "no"),
        (["Remote-Friendly (Travel Required) | San Francisco, CA"], True, "no"),
        (["Remote (Eastern Time)"], True, "maybe"),
        (["Remote - Hybrid"], True, "maybe"),
        (["Remote - USA", "Remote - Canada"], True, "match"),
        (["Ukraine Anywhere"], None, "maybe"),
    ],
)
def test_location_match(locations, remote, expected):
    posting = make_posting(location=locations[0], locations=locations, remote=remote)
    assert location_match(posting, SEARCH) == expected


def test_remote_canada_can_be_turned_off():
    search = SEARCH.model_copy(update={"remote_canada": False})
    posting = make_posting(location="Remote (Canada)", remote=True)
    assert location_match(posting, search) == "maybe"


def test_old_postings_are_filtered():
    old = make_posting(published=NOW - timedelta(days=45))
    reason, _ = hard_filter(old, SEARCH, NOW)
    assert reason == "posted more than 30 days ago"
    assert hard_filter(make_posting(), SEARCH, NOW) == (None, "match")


# Web search


@pytest.mark.parametrize(
    "url, board",
    [
        ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", ("greenhouse", "acme")),
        ("https://jobs.lever.co/acme-co/abc-123", ("lever", "acme-co")),
        ("https://jobs.ashbyhq.com/Acme%20Inc/xyz", ("ashby", "Acme Inc")),
        ("https://www.greenhouse.io/blog/post", None),
        ("https://example.com/jobs.lever.co/acme", None),
    ],
)
def test_board_from_url(url, board):
    assert board_from_url(url) == board


def block(**fields):
    return SimpleNamespace(**fields)


class FakeSearchLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def test_web_search_collects_urls_errors_and_continues_after_pause():
    first = SimpleNamespace(
        stop_reason="pause_turn",
        content=[
            block(type="server_tool_use", name="web_search", id="a", input={"query": "q1"}),
            block(type="web_search_tool_result", tool_use_id="a",
                  content=[block(url="https://jobs.lever.co/acme/1")]),
        ],
    )
    second = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            block(type="server_tool_use", name="web_search", id="b", input={"query": "q2"}),
            block(type="web_search_tool_result", tool_use_id="b",
                  content=block(error_code="too_many_requests")),
            block(type="text", text="done"),
        ],
    )
    llm = FakeSearchLLM([first, second])
    results = run_web_search(llm, ["q1", "q2"])
    assert [(r.query, r.urls, r.error) for r in results] == [
        ("q1", ["https://jobs.lever.co/acme/1"], None),
        ("q2", [], "too_many_requests"),
    ]
    tool = llm.requests[0]["tools"][0]
    assert tool["allowed_domains"] == ["greenhouse.io", "lever.co", "ashbyhq.com"]
    assert len(llm.requests[1]["messages"]) == 2


# Ranking prompt


def test_prompt_excludes_contact_details_and_wraps_posting():
    resume = parse_master_resume(SAMPLE)
    system = build_system_prompt(resume, SEARCH, NOW.date())
    assert "sam@example.com" not in system and "555" not in system
    assert "Improved throughput by up to 40%" in system
    message = posting_message(make_posting())
    assert message.startswith("<posting>") and message.endswith("</posting>")


# Company fit


def stats(**overrides):
    base = {"skipped": 0, "applied": 0, "ranked_eligible": 0, "good": 0, "best_eligible": None}
    return base | overrides


def test_fit_rules():
    assert assess_fit(stats(), passed_now=2, min_score=60) is None
    assert assess_fit(stats(), passed_now=0, min_score=60).days == config.COOLDOWN_NO_MATCHES_DAYS
    low = assess_fit(stats(ranked_eligible=4, good=0, best_eligible=48), passed_now=3, min_score=60)
    assert low.days == config.COOLDOWN_LOW_FIT_DAYS and "best score 48" in low.reason
    assert assess_fit(stats(ranked_eligible=3, good=0), passed_now=3, min_score=60) is None
    skipped = assess_fit(stats(skipped=3, applied=0, ranked_eligible=5, good=5), passed_now=3, min_score=60)
    assert skipped.days == config.COOLDOWN_SKIPPED_DAYS
    assert assess_fit(stats(skipped=3, applied=1), passed_now=3, min_score=60) is None


def test_ineligible_roles_do_not_pause_a_company(db):
    postings = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(5)]
    overrides = {f"Backend Engineer {i}": {"location_eligible": "no"} for i in range(3)}
    overrides["Backend Engineer 3"] = {"years_required": 6}
    llm = FakeLLM(scores={"Backend Engineer 4": 40}, overrides=overrides)
    report = run_with(db, llm, {"acme": postings}, strict_search())
    assert report.cooldowns_set == []
    assert db.find_companies("acme")[0]["cooldown_reason"] is None


def test_eligible_low_scores_still_pause_a_company(db):
    postings = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(5)]
    overrides = {"Backend Engineer 0": {"location_eligible": "no", "score": 90}}
    llm = FakeLLM(scores={f"Backend Engineer {i}": 40 for i in range(1, 5)}, overrides=overrides)
    report = run_with(db, llm, {"acme": postings}, strict_search())
    assert any("best score 40 across 4 eligible roles" in note for note in report.cooldowns_set)


# Selection


def test_selection_respects_caps():
    jobs = [{"id": i, "ats": "greenhouse", "board_token": "big", "title": f"Role {i}"} for i in range(8)]
    jobs += [{"id": 100 + i, "ats": "ashby", "board_token": "small", "title": f"Role {i}"} for i in range(3)]
    chosen = selection(jobs, max_total=6, max_per_company=5)
    assert [job["id"] for job in chosen] == [0, 1, 2, 3, 4, 100]


def test_selection_picks_each_role_once():
    jobs = [
        {"id": 1, "ats": "greenhouse", "board_token": "a", "title": "Software Engineer II, Backend"},
        {"id": 2, "ats": "greenhouse", "board_token": "a", "title": "Software Engineer II - Backend"},
        {"id": 3, "ats": "greenhouse", "board_token": "b", "title": "Software Engineer II, Backend"},
        {"id": 4, "ats": "greenhouse", "board_token": "a", "title": "Platform Engineer"},
    ]
    taken = {("greenhouse", "a", "platform engineer")}
    assert [job["id"] for job in selection(jobs, 20, 5, taken)] == [1, 3]


def test_location_twins_are_selected_once(db):
    twins = [
        make_posting(job_id="us", title="Backend Engineer", location="Remote US",
                     locations=["Remote US", "Remote Canada"], remote=True),
        make_posting(job_id="ca", title="Backend Engineer", location="Remote Canada", remote=True),
    ]
    report = run(db, FakeLLM(), {"acme": twins}, use_web_search=False)
    assert len(report.selected) == 1
    assert report.selected[0]["location"] == "Remote Canada"
    again = run(db, FakeLLM(), {"acme": twins}, use_web_search=False, now=NOW + timedelta(days=1))
    assert again.selected == []


def test_excluded_seed_drops_its_selected_jobs(db):
    report = run(db, FakeLLM(), {"acme": [make_posting()]}, use_web_search=False)
    job_id = report.selected[0]["id"]
    discover(
        db, FakeLLM(), SEARCH, parse_master_resume(SAMPLE),
        seed_companies=[SeedCompany(ats="greenhouse", token="acme", name="Acme",
                                    exclude=True, reason="aggregator")],
        fetch=board_fetcher({}), web_search=fake_search, use_web_search=False, now=NOW,
    )
    assert db.state(job_id) == JobState.FILTERED_OUT
    assert db.company(db.find_companies("acme")[0]["id"])["status"] == "excluded"


def test_deselect_duplicate_roles(db):
    from ademly.pipeline import deselect_duplicate_roles

    ids = []
    for job_id, score in (("1", 70), ("2", 80)):
        added = db.add_posting(make_posting(job_id=job_id, location=f"City {job_id}"))
        db.save_ranking(added.job_id, score, "ctx", "{}")
        db.transition(added.job_id, JobState.SELECTED)
        ids.append(added.job_id)
    assert deselect_duplicate_roles(db) == 1
    assert db.state(ids[0]) == JobState.FILTERED_OUT
    assert db.state(ids[1]) == JobState.SELECTED


# Full discovery run with fake boards and a fake ranker


def ranking(**overrides):
    fields = dict(
        location_eligible="yes", work_authorization_ok="yes", excluded_industry=None,
        excluded_industry_is_main_business=False, years_required=None, pay_range=None,
        pay_max_cad=None, product_oriented="yes", mission_alignment="some",
        stepping_stone="strong", seniority="good_fit", score=75, strengths=["Java"],
        concerns=[], summary="ok",
    )
    return Ranking(**(fields | overrides))


class FakeLLM:
    """Scores by title. Titles containing 'Refuse' make the call fail.

    `overrides` maps a title to extra Ranking fields for that posting.
    """

    def __init__(self, db=None, scores=None, overrides=None):
        self.db = db
        self.scores = scores or {}
        self.overrides = overrides or {}
        self.ranked = []

    def parse(self, *, user, **kwargs):
        title = next(line for line in user.splitlines() if line.startswith("Title: "))[7:]
        self.ranked.append(title)
        if "Refuse" in title:
            raise LLMRefused("declined")
        return ranking(**({"score": self.scores.get(title, 75)} | self.overrides.get(title, {})))


def fake_search(llm, queries, run_id=None):
    from ademly.discover.web_search import SearchResult

    return [SearchResult(q, ["https://jobs.ashbyhq.com/found/1"]) for q in queries]


def board_fetcher(boards):
    def fetch(ats, token):
        if token not in boards:
            raise BoardNotFound(token)
        return boards[token]
    return fetch


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def run(db, llm, boards, **kwargs):
    return discover(
        db, llm, SEARCH, parse_master_resume(SAMPLE),
        seed_companies=[SeedCompany(ats="greenhouse", token="acme", name="Acme"),
                        SeedCompany(ats="lever", token="gone", name="Gone")],
        fetch=board_fetcher(boards), web_search=fake_search, now=kwargs.pop("now", NOW), **kwargs,
    )


def test_discover_end_to_end(db):
    acme = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(7)]
    acme += [
        make_posting(job_id="sr", title="Senior Backend Engineer"),
        make_posting(job_id="us", title="Backend Engineer US", location="Remote (US)", remote=True),
        make_posting(job_id="low", title="Backend Engineer Low"),
        make_posting(job_id="ref", title="Backend Engineer Refuse"),
    ]
    found = [make_posting(token="found", job_id="f1", title="Platform Engineer", ats="ashby")]
    llm = FakeLLM(scores={"Backend Engineer Low": 40})

    report = run(db, llm, {"acme": acme, "found": found})

    assert report.searches_run == 2
    assert report.companies_added == 3  # two seeds, one found by search
    assert report.companies_invalid == ["Gone (lever)"]
    assert report.filtered["title contains 'senior'"] == 1
    assert report.filtered["location outside search area"] == 1
    assert report.ranked == 9 and report.rank_errors == 1
    assert "score below minimum" in report.filtered
    by_company = {}
    for job in report.selected:
        by_company[job["company"]] = by_company.get(job["company"], 0) + 1
    assert by_company == {"Acme": 5, "Found": 1}

    # A second run repeats nothing: no searches, no re-ranking, no reselection.
    llm.ranked.clear()
    second = run(db, llm, {"acme": acme, "found": found}, now=NOW + timedelta(days=1))
    assert second.searches_run == 0 and second.searches_skipped == 2
    assert llm.ranked == ["Backend Engineer Refuse"]  # only the failed one retries
    assert [job["title"] for job in second.selected] == ["Backend Engineer 5", "Backend Engineer 6"]
    assert second.selected[0]["state"] == JobState.SELECTED


def test_low_fit_company_is_paused_then_skipped(db):
    weak = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(4)]
    llm = FakeLLM(scores={f"Backend Engineer {i}": 30 for i in range(4)})
    report = run(db, llm, {"acme": weak}, use_web_search=False)
    assert report.selected == []
    assert any("best score 30" in note for note in report.cooldowns_set)

    later = run(db, llm, {"acme": weak}, use_web_search=False, now=NOW + timedelta(days=10))
    assert later.companies_fetched == 0 and later.companies_cooling_down == 1

    after = run(db, llm, {"acme": weak}, use_web_search=False, now=NOW + timedelta(days=61))
    assert after.companies_fetched == 1


def test_excluded_company_is_never_fetched(db):
    run(db, FakeLLM(), {"acme": [make_posting()]}, use_web_search=False)
    company = db.find_companies("acme")[0]
    db.set_company_status(company["id"], "excluded", "not interested")
    report = run(db, FakeLLM(), {"acme": [make_posting(job_id="2")]}, use_web_search=False,
                 now=NOW + timedelta(days=30))
    assert report.companies_fetched == 0


def test_closed_postings_leave_the_queue(db):
    llm = FakeLLM(scores={"Backend Engineer": 75})
    run(db, llm, {"acme": [make_posting(job_id=str(i), title="Backend Engineer") for i in range(1)]},
        use_web_search=False, max_rank=0)
    job = db.list_jobs(JobState.DISCOVERED)[0]
    run(db, llm, {"acme": []}, use_web_search=False, now=NOW + timedelta(days=15))
    assert db.state(job["id"]) == JobState.FILTERED_OUT
    assert db.events(job["id"])[-1]["reason"] == "no longer listed"


def test_max_rank_limits_spending(db):
    postings = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(6)]
    llm = FakeLLM()
    report = run(db, llm, {"acme": postings}, use_web_search=False, max_rank=2)
    assert report.ranked == 2 and report.left_unranked == 4


# Pay, experience, industry, and settings changes


@pytest.mark.parametrize(
    "text, years",
    [
        ("You have 5+ years of professional software engineering experience.", 5),
        ("3-5 years of experience building APIs", 3),
        ("At least 4 years experience with Java", 4),
        ("2+ years’ experience with Kafka. 7+ years of experience preferred.", 2),
        ("We have been building for 10 years.", None),
        ("Experience: 5+ years", None),
        ("3.5 years of experience", None),
    ],
)
def test_years_required_from_text(text, years):
    from ademly.discover.filters import years_required

    assert years_required(text) == years


def strict_search(**overrides):
    return SEARCH.model_copy(update={"min_salary": 120000, "max_years_required": 4, **overrides})


def run_with(db, llm, boards, search, now=NOW, seeds=(SeedCompany(ats="greenhouse", token="acme", name="Acme"),)):
    return discover(db, llm, search, parse_master_resume(SAMPLE), seed_companies=list(seeds),
                    fetch=board_fetcher(boards), web_search=fake_search, use_web_search=False, now=now)


def test_years_in_text_are_filtered_for_free(db):
    senior = make_posting(job_id="1", title="Senior Backend Engineer")
    senior.description = "Requires 6+ years of backend experience."
    fits = make_posting(job_id="2", title="Senior Backend Engineer II")
    fits.description = "Requires 3+ years of backend experience."
    llm = FakeLLM()
    report = run_with(db, llm, {"acme": [senior, fits]}, strict_search(title_exclude=[]))
    assert report.filtered["asks for more years of experience than your limit"] == 1
    assert llm.ranked == ["Senior Backend Engineer II"]


def test_ranker_findings_reject_jobs(db):
    postings = [make_posting(job_id=str(i), title=title) for i, title in
                enumerate(["Backend Engineer A", "Backend Engineer B", "Backend Engineer C"])]
    llm = FakeLLM(overrides={
        "Backend Engineer A": {"pay_max_cad": 110000},
        "Backend Engineer B": {"years_required": 6},
        "Backend Engineer C": {"pay_max_cad": 125000, "years_required": 3},
    })
    report = run_with(db, llm, {"acme": postings}, strict_search())
    assert [job["title"] for job in report.selected] == ["Backend Engineer C"]
    reasons = {event["reason"] for job in db.list_jobs(JobState.FILTERED_OUT) for event in db.events(job["id"])}
    assert any(r.startswith("ranker: pay below minimum") for r in reasons)
    assert "ranker: asks for 6+ years" in reasons


def test_excluded_industry_company_is_excluded(db):
    postings = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(3)]
    llm = FakeLLM(overrides={"Backend Engineer 0": {
        "excluded_industry": "buy now, pay later", "excluded_industry_is_main_business": True}})
    report = run_with(db, llm, {"acme": postings}, strict_search())
    assert report.selected == []
    assert db.find_companies("acme")[0]["status"] == "excluded"


def test_excluded_industry_team_only_rejects_that_job(db):
    postings = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(2)]
    llm = FakeLLM(overrides={"Backend Engineer 0": {
        "excluded_industry": "buy now, pay later", "excluded_industry_is_main_business": False}})
    report = run_with(db, llm, {"acme": postings}, strict_search())
    assert [job["title"] for job in report.selected] == ["Backend Engineer 1"]
    assert db.find_companies("acme")[0]["status"] == "active"


def test_excluded_companies_setting(db):
    run_with(db, FakeLLM(), {"acme": [make_posting()]}, strict_search())
    report = run_with(db, FakeLLM(), {"acme": [make_posting(job_id="2", title="Platform Engineer")]},
                      strict_search(excluded_companies=["Acme"]), now=NOW + timedelta(days=1))
    assert report.companies_fetched == 0
    assert db.list_jobs(JobState.SELECTED) == []


def test_settings_change_requeues_selected_jobs(db):
    first = run_with(db, FakeLLM(), {"acme": [make_posting()]}, strict_search())
    job_id = first.selected[0]["id"]
    llm = FakeLLM(scores={"Backend Engineer": 40})
    second = run_with(db, llm, {"acme": [make_posting()]}, strict_search(min_score=65),
                      now=NOW + timedelta(days=1))
    assert second.requeued == 1 and llm.ranked == ["Backend Engineer"]
    assert db.state(job_id) == JobState.FILTERED_OUT


def test_loosened_filters_bring_back_rejected_jobs(db):
    senior = make_posting(title="Senior Backend Engineer")
    strict = strict_search(title_exclude=["senior"], max_posting_age_days=90)
    run_with(db, FakeLLM(), {"acme": [senior]}, strict)
    job = db.list_jobs(JobState.FILTERED_OUT)[0]

    unchanged = run_with(db, FakeLLM(), {"acme": [senior]}, strict, now=NOW + timedelta(days=15))
    assert unchanged.reconsidered == 0

    loose = run_with(db, FakeLLM(), {"acme": [senior]},
                     strict_search(title_exclude=[], max_posting_age_days=90),
                     now=NOW + timedelta(days=30))
    assert loose.reconsidered == 1
    assert [j["id"] for j in loose.selected] == [job["id"]]


def test_no_match_pause_ends_when_filters_change(db):
    senior = make_posting(title="Senior Backend Engineer")
    run_with(db, FakeLLM(), {"acme": [senior]}, strict_search(title_exclude=["senior"]))
    same = run_with(db, FakeLLM(), {"acme": [senior]}, strict_search(title_exclude=["senior"]),
                    now=NOW + timedelta(days=1))
    assert same.companies_fetched == 0
    changed = run_with(db, FakeLLM(), {"acme": [senior]}, strict_search(title_exclude=[]),
                       now=NOW + timedelta(days=2))
    assert changed.companies_fetched == 1 and changed.reconsidered == 1


def test_low_fit_pause_ends_when_ranking_settings_change(db):
    weak = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(4)]
    llm = FakeLLM(scores={f"Backend Engineer {i}": 30 for i in range(4)})
    run_with(db, llm, {"acme": weak}, strict_search())
    same = run_with(db, llm, {"acme": weak}, strict_search(), now=NOW + timedelta(days=1))
    assert same.companies_fetched == 0
    changed = run_with(db, llm, {"acme": weak}, strict_search(description="Different goals"),
                       now=NOW + timedelta(days=2))
    assert changed.companies_fetched == 1


def test_user_skip_pause_survives_settings_changes(db):
    from ademly.db import USER_COOLDOWN

    run_with(db, FakeLLM(), {"acme": [make_posting()]}, strict_search())
    company = db.find_companies("acme")[0]
    db.set_cooldown(company["id"], NOW + timedelta(days=60), "skipped", USER_COOLDOWN)
    changed = run_with(db, FakeLLM(), {"acme": [make_posting()]},
                       strict_search(description="Different goals"), now=NOW + timedelta(days=1))
    assert changed.companies_fetched == 0


def test_reposts_and_low_scores_are_not_reconsidered(db):
    llm = FakeLLM(scores={"Backend Engineer": 40})
    run_with(db, llm, {"acme": [make_posting()]}, strict_search(title_exclude=["senior"]))
    later = run_with(db, llm, {"acme": [make_posting()]}, strict_search(title_exclude=[]),
                     now=NOW + timedelta(days=15))
    assert later.reconsidered == 0


def test_prompt_includes_criteria():
    resume = parse_master_resume(SAMPLE)
    search = strict_search(
        preferences=["Product work"], excluded_industries=["Gambling"],
        target_salary=(130000, 150000), preferred_years_required=3,
    )
    system = build_system_prompt(resume, search, NOW.date())
    for text in ("Product work", "Gambling", "120,000 CAD", "130,000 CAD to 150,000 CAD",
                 "more than 4 years", "3 years or fewer"):
        assert text in system


# Labels


def test_label_export_and_score(db, tmp_path):
    postings = [make_posting(job_id=str(i), title=f"Backend Engineer {i}") for i in range(4)]
    scores = {f"Backend Engineer {i}": s for i, s in enumerate([90, 80, 50, 30])}
    run(db, FakeLLM(scores=scores), {"acme": postings}, use_web_search=False)
    path = tmp_path / "labels.csv"
    assert export_labels(db, path, count=30) == 4
    text = path.read_text()
    assert "score" not in text.splitlines()[0]
    rows = text.splitlines()
    labels = ["good", "bad", "bad", "good"]  # agrees on rows 1 and 3 only
    path.write_text("\n".join([rows[0]] + [r + labels[i] for i, r in enumerate(rows[1:])]) + "\n")
    result = score_labels(db, path, min_score=60)
    assert (result.labeled, result.agreed) == (4, 2)


# Database migration


def test_old_database_gains_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE jobs (id INTEGER PRIMARY KEY, ats TEXT NOT NULL, board_token TEXT NOT NULL,
           ats_job_id TEXT NOT NULL, fuzzy_key TEXT NOT NULL, company TEXT NOT NULL,
           title TEXT NOT NULL, location TEXT NOT NULL, url TEXT NOT NULL, apply_url TEXT NOT NULL,
           posting_json TEXT NOT NULL, state TEXT NOT NULL, score INTEGER, run_id INTEGER,
           created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
           UNIQUE (ats, board_token, ats_job_id))"""
    )
    conn.commit()
    conn.close()
    database = Database(path)
    job = database.add_posting(make_posting())
    database.mark_passed(job.job_id, "match")
    assert database.job(job.job_id)["passed_filters"] == 1
    database.close()
