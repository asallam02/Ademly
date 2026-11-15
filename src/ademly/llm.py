"""Thin wrapper over the Anthropic SDK: refusal handling, usage logging, validation."""

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from ademly.config import EFFORT, MODEL, PRICE_PER_MTOK
from ademly.db import Database

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """A call finished without a usable result."""


class LLMRefused(LLMError):
    pass


class LLMTruncated(LLMError):
    pass


class LLMInvalidOutput(LLMError):
    pass


# Errors that affect one call, not the whole run. Credential and request-shape
# errors are left to propagate, because every later call would fail the same way.
TRANSIENT_ERRORS = (
    LLMError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


def estimate_cost(usage) -> float:
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    total = (
        usage.input_tokens * PRICE_PER_MTOK["input"]
        + usage.output_tokens * PRICE_PER_MTOK["output"]
        + cache_read * PRICE_PER_MTOK["cache_read"]
        + cache_write * PRICE_PER_MTOK["cache_write"]
    )
    return total / 1_000_000


class LLM:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        db: Database | None = None,
        model: str = MODEL,
    ):
        self.client = client or anthropic.Anthropic()
        self.db = db
        self.model = model

    def create(
        self,
        *,
        stage: str,
        effort_key: str | None = None,
        run_id: int | None = None,
        job_id: int | None = None,
        **request: Any,
    ):
        """Send one request, log its usage, and raise if the model declined."""
        output_config = {"effort": EFFORT[effort_key or stage], **request.pop("output_config", {})}
        response = self.client.beta.messages.create(
            model=self.model,
            output_config=output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **request,
        )
        if self.db is not None:
            usage = response.usage
            self.db.log_llm_call(
                stage=stage,
                model=response.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                cost_usd=estimate_cost(usage),
                run_id=run_id,
                job_id=job_id,
            )
        if response.stop_reason == "refusal":
            raise LLMRefused(f"{stage}: the model declined this request")
        return response

    def parse(
        self,
        *,
        stage: str,
        system: str,
        user: str,
        schema: type[T],
        effort_key: str | None = None,
        max_tokens: int = 16000,
        run_id: int | None = None,
        job_id: int | None = None,
    ) -> T:
        """Run one call and return a validated instance of `schema`.

        `system` should hold content that repeats across calls, because it is
        cached. Per-job content, including untrusted postings, belongs in `user`.
        """
        # create() rather than the SDK's parse(): parse() validates before the
        # stop reason can be checked, so refusals and truncation would surface
        # as JSON errors.
        response = self.create(
            stage=stage,
            effort_key=effort_key,
            run_id=run_id,
            job_id=job_id,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(schema),
                },
            },
        )
        if response.stop_reason == "max_tokens":
            raise LLMTruncated(f"{stage}: output hit max_tokens={max_tokens}")
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return schema.model_validate_json(text)
        except ValidationError as error:
            raise LLMInvalidOutput(f"{stage}: output failed validation") from error
