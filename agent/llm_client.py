"""Thin, instrumented wrapper around the one thing that crosses a process
boundary to Claude (HLD.md §1) -- routed through OpenRouter (OpenAI-Chat-
Completions-compatible), which proxies to Anthropic under an
"anthropic/..." model slug (config.MODEL_CHEAP/MODEL_STRONG). Every
structured call goes through `call_structured`: force a tool call shaped
like the Pydantic schema, parse straight into it -- no regex/manual JSON
parsing of free text anywhere else in the codebase.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterator, Literal, Type, TypeVar

import numpy as np
import openai
from pydantic import BaseModel, ValidationError

from agent.config import MODEL_CHEAP, MODEL_EMBEDDING, MODEL_STRONG, OPENROUTER_BASE_URL

T = TypeVar("T", bound=BaseModel)
Tier = Literal["cheap", "strong"]

_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        # api_key is passed explicitly (OPENROUTER_API_KEY) rather than
        # relying on the SDK's OPENAI_API_KEY env fallback, so there's no
        # ambiguity about which key/provider is in use.
        _client = openai.OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            timeout=60.0,  # the SDK default (600s) is too long to fail fast on
        )
    return _client


def _model_for(tier: Tier) -> str:
    return MODEL_CHEAP if tier == "cheap" else MODEL_STRONG


def call_structured(
    prompt: str,
    schema: Type[T],
    tier: Tier = "cheap",
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    trace=None,
    trace_label: str | None = None,
) -> T:
    """One structured Claude call (via OpenRouter), forced through a tool
    matching `schema`.

    One bounded retry (2 attempts total) on a malformed/invalid tool call,
    then raise -- this can genuinely happen occasionally, so it gets one
    retry, not an open-ended loop (CLAUDE.md's "don't add fallbacks for
    scenarios that can't happen" -- this one can).
    """
    model = _model_for(tier)
    tool_name = f"emit_{schema.__name__.lower()}"
    tool_schema = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": f"Emit a single {schema.__name__} record matching the given schema exactly.",
            "parameters": schema.model_json_schema(),
        },
    }

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error: Exception | None = None
    for attempt in range(1, 3):
        start = time.monotonic()
        response = _get_client().chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            tools=[tool_schema],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
        latency = time.monotonic() - start

        message = response.choices[0].message
        usage = response.usage
        if trace is not None:
            trace.log_llm_call(
                label=trace_label or schema.__name__,
                model=model,
                tier=tier,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                latency_seconds=latency,
                attempt=attempt,
            )

        tool_calls = message.tool_calls or []
        tool_call = next((c for c in tool_calls if c.function.name == tool_name), None)
        if tool_call is None:
            last_error = RuntimeError(
                f"{schema.__name__}: model did not return a tool call "
                f"(finish_reason={response.choices[0].finish_reason})"
            )
            continue

        error_detail: str | None = None
        try:
            arguments = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as exc:
            last_error = exc
            error_detail = f"Your arguments were not valid JSON: {exc}"
        else:
            try:
                return schema.model_validate(arguments)
            except ValidationError as exc:
                last_error = exc
                error_detail = f"Your arguments failed schema validation: {exc}"

        # Feed the concrete error back to the model on retry (seen live: an
        # enum-literal mixup reproduced itself across both attempts when
        # retried against an identical prompt with no feedback) so the
        # second attempt has an actual chance to fix what was wrong, rather
        # than blindly repeating the same mistake.
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": f"{error_detail}\n\nRe-emit the full tool call with every field corrected.",
            }
        )

    raise RuntimeError(
        f"call_structured failed after 2 attempts for {schema.__name__}: {last_error}"
    )


def stream_text(
    prompt: str,
    *,
    system: str | None = None,
    tier: Tier = "strong",
    max_tokens: int = 1024,
    trace=None,
    trace_label: str | None = None,
) -> Iterator[str]:
    """One plain-text (non-tool) completion, streamed: yields the model's text
    as it is produced (the UI follow-up chat's typewriter effect) and logs the
    call's usage/latency to `trace` once the stream finishes. No bounded retry
    the way `call_structured` has -- there is no schema to validate here, so a
    malformed completion isn't a failure mode; a mid-stream transport error just
    surfaces to the caller."""
    model = _model_for(tier)
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})

    start = time.monotonic()
    stream = _get_client().chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
        stream=True,
        # include_usage makes the final chunk carry prompt/completion token
        # totals; without it streamed responses report usage=None.
        stream_options={"include_usage": True},
    )
    input_tokens = 0
    output_tokens = 0
    for chunk in stream:
        if chunk.usage is not None:
            input_tokens = chunk.usage.prompt_tokens or 0
            output_tokens = chunk.usage.completion_tokens or 0
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta is not None and delta.content:
                yield delta.content
    latency = time.monotonic() - start
    if trace is not None:
        trace.log_llm_call(
            label=trace_label or "stream_text",
            model=model,
            tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=latency,
            attempt=1,
        )


def call_embeddings(
    texts: list[str], *, trace=None, trace_label: str | None = None
) -> np.ndarray:
    """Batched embedding call via OpenRouter's OpenAI-compatible /embeddings
    endpoint (agent/dedup.py's clustering, eval/run_eval.py's independent
    coverage cross-check) -- returns L2-normalized vectors so callers can
    take a plain dot product as cosine similarity, same contract the prior
    local model provided.
    """
    start = time.monotonic()
    response = _get_client().embeddings.create(model=MODEL_EMBEDDING, input=texts)
    latency = time.monotonic() - start

    if trace is not None:
        usage = response.usage
        trace.log_llm_call(
            label=trace_label or "embedding",
            model=MODEL_EMBEDDING,
            tier="embedding",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=0,
            latency_seconds=latency,
            attempt=1,
        )

    vectors = np.array([d.embedding for d in response.data], dtype=np.float64)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms
