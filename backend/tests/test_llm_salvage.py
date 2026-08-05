"""The XML-ish tool-output salvage — the failure mode that twice produced an
empty weekly review. Pure function, no DB."""
from app.llm import _salvage_xmlish_tool_input

SCHEMA = {
    "properties": {
        "summary": {"type": "string"},
        "flagged": {"type": ["boolean", "null"]},
        "count": {"type": "integer"},
        "sessions": {"type": "array"},
    }
}


def test_reconstructs_and_coerces_types():
    data = {
        "summary": (
            'Week looks good.'
            '<parameter name="flagged">false</parameter>'
            '<parameter name="count">3</parameter>'
            '<parameter name="sessions">[{"d": "x"}]</parameter>'
        ),
        "flagged": None,
        "count": None,
        "sessions": [],
    }
    out = _salvage_xmlish_tool_input(data, SCHEMA)
    assert out is not None
    assert out["summary"] == "Week looks good."
    # A boolean crammed as the string "false" must become False, not truthy.
    assert out["flagged"] is False
    assert out["count"] == 3
    assert out["sessions"] == [{"d": "x"}]


def test_refuses_when_embedded_json_is_truncated():
    # A non-string field whose JSON doesn't parse => refuse the whole salvage
    # (returns None) so the caller's retry/raise guard runs instead of shipping junk.
    data = {
        "summary": 'x<parameter name="sessions">[{"d":</parameter>',
        "sessions": [],
    }
    assert _salvage_xmlish_tool_input(data, SCHEMA) is None


def test_none_when_nothing_to_salvage():
    assert _salvage_xmlish_tool_input({"summary": "clean", "flagged": False}, SCHEMA) is None


# --------------------------------------------------------------- truncation must be loud

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _Stream:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._resp


def _patch_client(monkeypatch, resp):
    from app import llm

    class _Messages:
        def stream(self, **kw):
            return _Stream(resp)

    monkeypatch.setattr(llm, "get_client", lambda: _Block(messages=_Messages()))
    monkeypatch.setattr(llm, "get_settings",
                        lambda: _Block(model_for=lambda task: "claude-opus-5"))


def _call(**kw):
    from app.llm import call_tool
    return call_tool(task="macro_plan", system="s", content="c",
                     tool_name="record_sessions", tool_schema={"properties": {}}, **kw)


def test_a_truncated_tool_call_raises_instead_of_returning_a_partial_plan(monkeypatch):
    """The dangerous path: hitting the cap still yields a tool_use block, so without
    this guard a half-built plan is returned as if complete and then applied over the
    real window. Matters more now that adaptive thinking shares the token budget."""
    import pytest
    truncated = _Resp("max_tokens", [
        _Block(type="tool_use", name="record_sessions", input={"sessions": [{"date": "2026-08-10"}]}),
    ])
    _patch_client(monkeypatch, truncated)
    with pytest.raises(RuntimeError, match="truncated"):
        _call()


def test_a_complete_tool_call_returns_normally(monkeypatch):
    payload = {"sessions": [{"date": "2026-08-10"}, {"date": "2026-08-12"}]}
    _patch_client(monkeypatch, _Resp("tool_use", [
        _Block(type="tool_use", name="record_sessions", input=payload),
    ]))
    assert _call() == payload


def test_adaptive_thinking_is_off_by_default_and_opt_in(monkeypatch):
    """Thinking shares max_tokens with the output, so the frequent 4096-budget callers
    must not get it implicitly."""
    from app import llm
    seen = {}

    class _Messages:
        def stream(self, **kw):
            seen.update(kw)
            return _Stream(_Resp("tool_use", [
                _Block(type="tool_use", name="record_sessions", input={"ok": 1})]))

    monkeypatch.setattr(llm, "get_client", lambda: _Block(messages=_Messages()))
    monkeypatch.setattr(llm, "get_settings",
                        lambda: _Block(model_for=lambda task: "claude-opus-5"))

    _call()
    assert seen["thinking"] == {"type": "disabled"}
    _call(adaptive_thinking=True)
    assert seen["thinking"] == {"type": "adaptive"}
