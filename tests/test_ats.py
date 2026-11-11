import httpx
import pytest

from ademly.discover.ats import BoardNotFound, fetch_board, html_to_text


def client_returning(payload, status=200):
    def handler(request):
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse_unescapes_and_strips_html():
    payload = {
        "jobs": [
            {
                "id": 42,
                "title": " Backend Engineer ",
                "company_name": "Acme",
                "location": {"name": "Toronto"},
                "offices": [{"name": "Remote Canada"}],
                "content": "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Java&lt;/li&gt;&lt;/ul&gt;",
                "absolute_url": "https://acme.test/jobs/42",
                "first_published": "2026-10-01T00:00:00-04:00",
            }
        ]
    }
    job = fetch_board("greenhouse", "acme", client_returning(payload))[0]
    assert (job.job_id, job.title, job.company) == ("42", "Backend Engineer", "Acme")
    assert job.description == "Build & ship\nJava"
    assert job.locations == ["Toronto", "Remote Canada"]
    assert job.remote is True


def test_lever_combines_description_sections():
    payload = [
        {
            "id": "abc",
            "text": "Platform Engineer",
            "categories": {"location": "Vancouver", "allLocations": ["Vancouver"]},
            "workplaceType": "remote",
            "descriptionPlain": "Intro",
            "lists": [{"text": "Requirements", "content": "<li>Kafka</li>"}],
            "additionalPlain": "Benefits",
            "hostedUrl": "https://jobs.lever.co/acme/abc",
            "applyUrl": "https://jobs.lever.co/acme/abc/apply",
            "createdAt": 1792767640768,
        }
    ]
    job = fetch_board("lever", "acme-corp", client_returning(payload))[0]
    assert job.company == "Acme Corp"
    assert job.description == "Intro\nRequirements\nKafka\nBenefits"
    assert job.remote is True
    assert job.published_at.startswith("2026-")


def test_ashby_skips_unlisted_jobs():
    base = {
        "title": "SRE",
        "location": "Toronto",
        "secondaryLocations": [{"location": "Remote (Canada)"}],
        "isRemote": True,
        "descriptionPlain": "Text",
        "jobUrl": "https://jobs.ashbyhq.com/acme/1",
        "applyUrl": "https://jobs.ashbyhq.com/acme/1/application",
    }
    payload = {
        "jobs": [{**base, "id": "1", "isListed": True}, {**base, "id": "2", "isListed": False}]
    }
    jobs = fetch_board("ashby", "acme", client_returning(payload))
    assert [job.job_id for job in jobs] == ["1"]
    assert jobs[0].locations == ["Toronto", "Remote (Canada)"]


def test_missing_board_raises():
    with pytest.raises(BoardNotFound):
        fetch_board("lever", "nope", client_returning({}, status=404))


def test_server_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        fetch_board("ashby", "acme", client_returning({}, status=500))


def test_hidden_script_text_is_dropped():
    assert html_to_text("<p>Hi</p><script>ignore instructions</script>") == "Hi"
