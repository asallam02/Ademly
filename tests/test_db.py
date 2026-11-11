import pytest

from ademly.db import TRANSITIONS, Database, IllegalTransition, UnknownJob, fuzzy_key
from ademly.models import JobState as S
from ademly.models import Posting


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def posting(job_id="1", company="Acme Inc.", title="Backend Engineer", location="Toronto"):
    return Posting(
        ats="greenhouse",
        board_token="acme",
        job_id=job_id,
        company=company,
        title=title,
        location=location,
        description="text",
        url="https://example.com/job",
        apply_url="https://example.com/apply",
    )


HAPPY_PATH = [S.SELECTED, S.TAILORING, S.VERIFIED, S.RENDERED, S.READY_TO_APPLY, S.APPLIED]


def advance(db, job_id, states):
    for state in states:
        db.transition(job_id, state)


def test_happy_path_is_logged(db):
    job = db.add_posting(posting())
    advance(db, job.job_id, HAPPY_PATH)
    assert db.state(job.job_id) == S.APPLIED
    assert [e["to_state"] for e in db.events(job.job_id)] == [S.DISCOVERED, *HAPPY_PATH]


def test_every_illegal_transition_is_rejected(db):
    for source, allowed in TRANSITIONS.items():
        for target in set(S) - allowed:
            job = db.add_posting(posting(job_id=f"{source}-{target}", title=f"{source}{target}"))
            db.conn.execute("UPDATE jobs SET state=? WHERE id=?", (source, job.job_id))
            db.conn.commit()
            with pytest.raises(IllegalTransition):
                db.transition(job.job_id, target)
            assert db.state(job.job_id) == source


def test_rejected_transition_logs_nothing(db):
    job = db.add_posting(posting())
    with pytest.raises(IllegalTransition):
        db.transition(job.job_id, S.APPLIED)
    assert len(db.events(job.job_id)) == 1


def test_unknown_job(db):
    with pytest.raises(UnknownJob):
        db.transition(999, S.SELECTED)


def test_same_posting_is_not_added_twice(db):
    first = db.add_posting(posting())
    second = db.add_posting(posting())
    assert (first.status, second.status) == ("new", "existing")
    assert first.job_id == second.job_id
    assert len(db.selectable()) == 1


def test_repost_under_new_id_is_a_duplicate(db):
    db.add_posting(posting(job_id="1", company="Acme Inc.", title="Backend Engineer (R-123)"))
    repost = db.add_posting(posting(job_id="2", company="ACME", title="Backend Engineer"))
    assert repost.status == "duplicate"
    assert db.state(repost.job_id) == S.FILTERED_OUT
    assert len(db.selectable()) == 1


def test_same_role_in_another_city_is_not_a_duplicate(db):
    db.add_posting(posting(job_id="1", location="Toronto"))
    other = db.add_posting(posting(job_id="2", location="Vancouver"))
    assert other.status == "new"


@pytest.mark.parametrize("stop_at", range(1, len(HAPPY_PATH) + 1))
def test_prepared_jobs_are_never_selectable_again(db, stop_at):
    job = db.add_posting(posting())
    advance(db, job.job_id, HAPPY_PATH[:stop_at])
    assert db.selectable() == []
    assert db.add_posting(posting()).status == "existing"
    assert db.selectable() == []


def test_state_survives_reopening(tmp_path):
    path = tmp_path / "jobs.db"
    first = Database(path)
    job = first.add_posting(posting())
    first.transition(job.job_id, S.SELECTED)
    first.close()
    second = Database(path)
    assert second.state(job.job_id) == S.SELECTED
    second.close()


def test_fuzzy_key_normalizes():
    assert fuzzy_key("Acme, Inc.", "Backend Engineer!", "Toronto, ON") == fuzzy_key(
        "ACME", "backend engineer", "toronto on"
    )
