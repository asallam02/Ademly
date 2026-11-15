"""Read-only connectors for public ATS job boards."""

import html
import re
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx

from ademly.models import Posting

USER_AGENT = "ademly/0.1"
TIMEOUT = 30.0

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}


class BoardNotFound(Exception):
    pass


class _TextExtractor(HTMLParser):
    """Keeps visible text only, so hidden scripts and styles are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def display_name(token: str) -> str:
    return token.replace("-", " ").replace("_", " ").title()


_display_name = display_name


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _get(client: httpx.Client, url: str, params: dict, label: str):
    response = client.get(url, params=params)
    if response.status_code == 404:
        raise BoardNotFound(label)
    response.raise_for_status()
    return response.json()


def fetch_greenhouse(client: httpx.Client, token: str) -> list[Posting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    data = _get(client, url, {"content": "true"}, f"greenhouse/{token}")
    postings = []
    for job in data["jobs"]:
        location = job["location"]["name"]
        offices = [office["name"] for office in job.get("offices") or []]
        locations = _unique([location, *offices])
        postings.append(
            Posting(
                ats="greenhouse",
                board_token=token,
                job_id=str(job["id"]),
                company=job.get("company_name") or _display_name(token),
                title=job["title"].strip(),
                location=location,
                locations=locations,
                remote=any("remote" in place.casefold() for place in locations),
                # Greenhouse escapes its HTML, so it must be unescaped first.
                description=html_to_text(html.unescape(job.get("content") or "")),
                url=job["absolute_url"],
                apply_url=job["absolute_url"],
                published_at=job.get("first_published") or job.get("updated_at"),
            )
        )
    return postings


def fetch_lever(client: httpx.Client, token: str) -> list[Posting]:
    url = f"https://api.lever.co/v0/postings/{token}"
    data = _get(client, url, {"mode": "json"}, f"lever/{token}")
    postings = []
    for job in data:
        categories = job.get("categories") or {}
        location = categories.get("location") or ""
        sections = [job.get("descriptionPlain") or ""]
        for block in job.get("lists") or []:
            sections.append(block.get("text") or "")
            sections.append(html_to_text(block.get("content") or ""))
        sections.append(job.get("additionalPlain") or "")
        created = job.get("createdAt")
        postings.append(
            Posting(
                ats="lever",
                board_token=token,
                job_id=job["id"],
                company=_display_name(token),
                title=job["text"].strip(),
                location=location,
                locations=_unique([location, *(categories.get("allLocations") or [])]),
                remote=job.get("workplaceType") == "remote",
                description="\n".join(s.strip() for s in sections if s.strip()),
                url=job["hostedUrl"],
                apply_url=job["applyUrl"],
                published_at=(
                    datetime.fromtimestamp(created / 1000, UTC).isoformat()
                    if created
                    else None
                ),
            )
        )
    return postings


def fetch_ashby(client: httpx.Client, token: str) -> list[Posting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    data = _get(client, url, {}, f"ashby/{token}")
    postings = []
    for job in data["jobs"]:
        if job.get("isListed") is False:
            continue
        location = job.get("location") or ""
        secondary = [s.get("location") or "" for s in job.get("secondaryLocations") or []]
        postings.append(
            Posting(
                ats="ashby",
                board_token=token,
                job_id=job["id"],
                company=_display_name(token),
                title=job["title"].strip(),
                location=location,
                locations=_unique([location, *secondary]),
                remote=job.get("isRemote"),
                description=(job.get("descriptionPlain") or "").strip(),
                url=job["jobUrl"],
                apply_url=job["applyUrl"],
                published_at=job.get("publishedAt"),
            )
        )
    return postings


FETCHERS: dict[str, Callable[[httpx.Client, str], list[Posting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def make_client(**kwargs) -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        **kwargs,
    )


def fetch_board(ats: str, token: str, client: httpx.Client | None = None) -> list[Posting]:
    if client is not None:
        return FETCHERS[ats](client, token)
    with make_client() as owned:
        return FETCHERS[ats](owned, token)
