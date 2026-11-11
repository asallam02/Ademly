"""Exercises the real SDK against a local stub of the Messages API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest
from pydantic import BaseModel

from ademly.db import Database
from ademly.llm import LLM, LLMInvalidOutput, LLMRefused, LLMTruncated, estimate_cost


class Answer(BaseModel):
    value: str


def message(text='{"value": "ok"}', stop_reason="end_turn"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 0,
        },
    }


@pytest.fixture
def stub():
    state = {"status": 200, "body": message(), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["content-length"])
            state["requests"].append(
                {"body": json.loads(self.rfile.read(length)), "headers": dict(self.headers)}
            )
            payload = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["client"] = anthropic.Anthropic(
        api_key="test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0
    )
    yield state
    server.shutdown()
    server.server_close()


def call(llm):
    return llm.parse(stage="verify", system="stable", user="job", schema=Answer)


def test_returns_validated_output_and_sends_expected_request(stub):
    assert call(LLM(client=stub["client"])).value == "ok"
    request = stub["requests"][0]
    body = request["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in body
    assert "tool_choice" not in body
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]


def test_refusal_raises(stub):
    stub["body"] = message(text="", stop_reason="refusal")
    db = Database(":memory:")
    with pytest.raises(LLMRefused):
        call(LLM(client=stub["client"], db=db))
    assert db.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    db.close()


def test_truncation_raises(stub):
    stub["body"] = message(text='{"val', stop_reason="max_tokens")
    with pytest.raises(LLMTruncated):
        call(LLM(client=stub["client"]))


@pytest.mark.parametrize("text", ["not json", '{"wrong_field": 1}'])
def test_invalid_output_raises(stub, text):
    stub["body"] = message(text=text)
    with pytest.raises(LLMInvalidOutput):
        call(LLM(client=stub["client"]))


def test_thinking_blocks_are_ignored(stub):
    body = message()
    body["content"].insert(0, {"type": "thinking", "thinking": "", "signature": "s"})
    stub["body"] = body
    assert call(LLM(client=stub["client"])).value == "ok"


def test_rate_limit_surfaces_typed_error(stub):
    stub["status"] = 429
    stub["body"] = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    with pytest.raises(anthropic.RateLimitError):
        call(LLM(client=stub["client"]))


def test_usage_is_logged(stub):
    db = Database(":memory:")
    call(LLM(client=stub["client"], db=db))
    row = db.conn.execute("SELECT * FROM llm_calls").fetchone()
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (1000, 500, 2000)
    assert row["cost_usd"] == pytest.approx(0.0144)
    db.close()


def test_cost_estimate():
    class Usage:
        input_tokens = 1_000_000
        output_tokens = 1_000_000
        cache_read_input_tokens = None
        cache_creation_input_tokens = None

    assert estimate_cost(Usage()) == pytest.approx(24.0)
