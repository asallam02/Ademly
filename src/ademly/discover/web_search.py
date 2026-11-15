"""Finds new company job boards through Claude's web search tool.

Only URLs are taken from the results. Each board is then fetched from the ATS's
own API, so nothing the search returns is trusted as job data.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from ademly.llm import LLM

ATS_DOMAINS = ["greenhouse.io", "lever.co", "ashbyhq.com"]
MAX_CONTINUATIONS = 3

_BOARD_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?(?:.*&)?for=([\w-]+)")),
    ("greenhouse", re.compile(r"^https?://(?:job-boards|boards)\.greenhouse\.io/(?!embed\b)([\w-]+)")),
    ("lever", re.compile(r"^https?://jobs\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/([\w.%-]+)")),
]


@dataclass
class SearchResult:
    query: str
    urls: list[str] = field(default_factory=list)
    error: str | None = None


def board_from_url(url: str) -> tuple[str, str] | None:
    for ats, pattern in _BOARD_PATTERNS:
        if match := pattern.search(url):
            return ats, unquote(match.group(1))
    return None


def run_web_search(llm: LLM, queries: list[str], run_id: int | None = None) -> list[SearchResult]:
    prompt = (
        "Run each of these web searches exactly as written, one search per line. "
        "When all of them are done, reply with the single word: done\n\n" + "\n".join(queries)
    )
    messages: list = [{"role": "user", "content": prompt}]
    tool = {
        "type": "web_search_20260209",
        "name": "web_search",
        "allowed_domains": ATS_DOMAINS,
        "max_uses": len(queries),
    }
    by_tool_use: dict[str, SearchResult] = {}
    ordered: list[SearchResult] = []

    for _ in range(MAX_CONTINUATIONS + 1):
        response = llm.create(
            stage="search",
            effort_key="extract",
            run_id=run_id,
            max_tokens=4000,
            messages=messages,
            tools=[tool],
        )
        for block in response.content:
            if block.type == "server_tool_use" and block.name == "web_search":
                result = SearchResult(query=str(block.input.get("query", "")))
                by_tool_use[block.id] = result
                ordered.append(result)
            elif block.type == "web_search_tool_result":
                result = by_tool_use.get(block.tool_use_id)
                if result is None:
                    continue
                # A successful result is a list; an error is a single object.
                if isinstance(block.content, list):
                    result.urls = [item.url for item in block.content]
                else:
                    result.error = getattr(block.content, "error_code", "unknown_error")
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    return ordered
