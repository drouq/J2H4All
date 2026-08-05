"""Anthropic client wrapper (model-per-task, configurable).

Phase 2 uses a single helper — forced tool-use — because it gives exact control
over the output JSON schema across SDK versions and is what the context
extraction and PDF-parsing paths need. Later phases can add free-form calls.
"""

import json
import logging
import re

from anthropic import Anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096

_PARAM_MARKER = '<parameter name="'
_PARAM_RE = re.compile(r'<parameter name="([^"]+)">')


def _strip_param_close(value: str) -> str:
    value = value.strip()
    if value.endswith("</parameter>"):
        value = value[: -len("</parameter>")].strip()
    return value


def _salvage_xmlish_tool_input(data: dict, schema: dict) -> dict | None:
    """Rare forced-tool-use failure mode (seen ~2/5 on long weekly reviews): the
    model emits the ENTIRE parameter set as XML-ish text inside the first string
    field, leaving the real fields empty. The content is intact, just mis-packed —
    reconstruct the fields from the markers. Returns None if nothing to salvage."""
    carrier = next(
        (k for k, v in data.items() if isinstance(v, str) and _PARAM_MARKER in v), None
    )
    if carrier is None:
        return None
    parts = _PARAM_RE.split(data[carrier])
    # parts = [carrier's own value, name1, value1, name2, value2, ...]
    out = dict(data)
    out[carrier] = _strip_param_close(parts[0])
    props = schema.get("properties") or {}
    for i in range(1, len(parts) - 1, 2):
        name, value = parts[i], _strip_param_close(parts[i + 1])
        declared = props.get(name, {}).get("type")
        declared = [declared] if isinstance(declared, str) else (declared or [])
        if "null" in declared and value.strip().lower() == "null":
            out[name] = None
        elif any(t in declared for t in ("array", "object", "boolean", "integer", "number")):
            # Non-string fields MUST be JSON-parsed: leaving a boolean as the
            # string "false" would read truthy downstream (e.g. a false red-flag
            # from bool("false")). Refuse the whole salvage if a value doesn't
            # parse — the caller keeps the raw payload and its retry guard runs.
            try:
                out[name] = json.loads(value)
            except ValueError:
                logger.warning("salvage: field %r did not parse as JSON", name)
                return None
        else:
            out[name] = value
    return out


class LLMNotConfigured(RuntimeError):
    """ANTHROPIC_API_KEY is not set."""


def get_client() -> Anthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise LLMNotConfigured("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=settings.anthropic_api_key)


def call_tool(
    task: str,
    system: str,
    content: list | str,
    tool_name: str,
    tool_schema: dict,
    max_tokens: int = MAX_TOKENS,
    tool_description: str = "Record the result.",
    adaptive_thinking: bool = False,
) -> dict:
    """Run one message with a single tool forced, and return that tool's input dict.

    `task` selects the model via config. `content` is the user-message
    content (a string, or a list of content blocks for e.g. a PDF document).

    Thinking is OFF by default. `adaptive_thinking=True` turns it on, and belongs on
    the big-budget plan surfaces only (see below) — NOT on the frequent small ones.

    Why off by default: thinking tokens come out of `max_tokens`, which is the same
    budget as the tool output. Most callers here run at the 4096 default (check-in
    parse, post-run read, context extraction, PDF parse), and thinking on that budget
    risks truncating the tool call itself.

    Why on for plan generation: Opus 5 with thinking DISABLED can occasionally emit a
    tool call as plain text instead of a `tool_use` block — a turn that returns 200,
    calls nothing, and raises no error. It's documented as most likely on exactly this
    shape of workload (tool-heavy, forced tool use). Probed 2026-08-03 against
    claude-opus-5: forced `tool_choice` + `{"type": "adaptive"}` is accepted and
    returned a clean tool_use 3/3 — so the older "forced tool choice is incompatible
    with thinking" constraint (true in the `budget_tokens` generation) no longer holds.
    Disabled ALSO returned 3/3 in that probe, so this is removing exposure to a
    documented failure mode, not fixing an observed one.
    """
    client = get_client()
    model = get_settings().model_for(task)
    # Stream internally and accumulate: a non-streaming plan-sized call has
    # minutes of time-to-first-byte, and idle middleboxes kill the connection
    # while waiting for response headers ("Server disconnected without sending
    # a response", seen 3x in a row on 2026-07-13). Streaming sends headers
    # immediately and keeps bytes flowing; the final message is identical.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"} if adaptive_thinking else {"type": "disabled"},
        system=system,
        tools=[{"name": tool_name, "description": tool_description, "input_schema": tool_schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()
    # Truncation must be LOUD. A response that hit the cap still carries a `tool_use`
    # block — with partial input — so the loop below would return a half-built plan
    # that looks complete: a 38-session review silently proposed as 20 sessions, then
    # applied over the real window by `apply_sessions`. Raising here instead matches
    # how the rest of the plan path degrades (generate.py retries once, then RAISES,
    # rather than shipping an empty proposal). Matters more since adaptive thinking
    # shares this budget with the output — if this fires, RAISE max_tokens; don't
    # reach for disabling thinking again (see the docstring).
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(
            f"call_tool({task}): hit the {max_tokens}-token cap before finishing "
            f"{tool_name!r} — the tool input is truncated. Raise max_tokens."
        )
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool_name:
            fixed = _salvage_xmlish_tool_input(block.input, tool_schema)
            if fixed is not None:
                logger.warning("call_tool(%s): salvaged XML-ish malformed tool input", task)
                return fixed
            return block.input
    raise RuntimeError(f"Model did not call tool {tool_name!r} (stop_reason={resp.stop_reason})")


def call_text(task: str, system: str, content: list | str, max_tokens: int = MAX_TOKENS) -> str:
    """Free-form text completion (e.g. the morning brief). Model per task."""
    client = get_client()
    model = get_settings().model_for(task)
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "disabled"},
        system=system,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()
    return "".join(b.text for b in resp.content if b.type == "text").strip()
