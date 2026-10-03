"""Thin Claude API wrapper: structured (tool-forced) calls + token_ledger logging.

Route-by-difficulty (§10): only intake parsing and the Diagnoser hit the model in this
build slice, and both run once, not per-job, so a stronger model is fine here. Cheaper
models come in when the Rewriter/outreach stages (§6.3, §9) are built.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-5"

_client: Anthropic | None = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to a .env file in the project root "
                "or export it in your shell before running the engine."
            )
        _client = Anthropic(api_key=api_key)
    return _client


def log_usage(
    conn: sqlite3.Connection | None,
    stage: str,
    model: str,
    usage,
    job_id: int | None = None,
) -> None:
    if conn is None:
        return
    conn.execute(
        "INSERT INTO token_ledger (ts, stage, job_id, model, input_tokens, output_tokens, cached_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            stage,
            job_id,
            model,
            usage.input_tokens,
            usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )
    conn.commit()


def cached_system(*parts: str) -> list[dict]:
    """Build a system prompt as cache-eligible blocks — each non-empty part
    gets its own ephemeral cache_control breakpoint (max 4/request). Pass
    the frozen instructions first, then the master resume JSON: both are
    identical across every call in a stage until the resume itself changes,
    so this is where the "single largest saving available" (§10) actually
    comes from — without it, that stable content is billed at full price on
    every single call instead of the ~90%-cheaper cache-read rate."""
    return [{"type": "text", "text": p, "cache_control": {"type": "ephemeral"}} for p in parts if p]


def call_structured(
    prompt: str,
    schema: dict,
    tool_name: str,
    *,
    stage: str,
    model: str = MODEL_SONNET,
    system: str | list[dict] | None = None,
    max_tokens: int = 4096,
    conn: sqlite3.Connection | None = None,
    job_id: int | None = None,
    strict: bool = True,
) -> dict:
    """Force a single tool call whose input matches `schema`; return the
    validated dict. strict=True (the default) makes the API itself enforce
    the schema exactly — every `required` field guaranteed present — rather
    than relying on the model to comply on its own, which it sometimes
    doesn't (observed: a malformed response missing a required field under
    an unusual/compound prompt). Schemas must have additionalProperties:
    false at every object level for strict mode to apply; pass strict=False
    to fall back to advisory-only validation for a schema that can't meet
    that (e.g. one still being migrated)."""
    client = get_client()
    kwargs = {}
    if system:
        kwargs["system"] = system
    tool_def = {"name": tool_name, "description": f"Emit {tool_name}", "input_schema": schema}
    if strict:
        tool_def["strict"] = True
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[tool_def],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    log_usage(conn, stage, model, response.usage, job_id)
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise RuntimeError(f"Model did not call the expected tool `{tool_name}`. Raw response: {response.content}")


def call_text(
    prompt: str,
    *,
    stage: str,
    model: str = MODEL_SONNET,
    system: str | list[dict] | None = None,
    max_tokens: int = 4096,
    conn: sqlite3.Connection | None = None,
    job_id: int | None = None,
) -> str:
    client = get_client()
    kwargs = {}
    if system:
        kwargs["system"] = system
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    log_usage(conn, stage, model, response.usage, job_id)
    return "".join(block.text for block in response.content if block.type == "text")
