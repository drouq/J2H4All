"""Coach-proposed plan changes (PRD §11): a change discussed on Telegram becomes a
PENDING sessions proposal — never applied without approval — so it can reach the
plan/calendar/watch. Covers the tool → proposal path and the response parsing.
"""
import types
from datetime import date, timedelta

from app.coach import chat
from app.plan import proposals


def _future_sessions():
    d = (date.today() + timedelta(days=3)).isoformat()
    return [{"date": d, "type": "long_run", "title": "Long run (moved)", "purpose": "durability"}]


def test_proposal_from_tool_creates_pending_sessions_proposal(db):
    p = chat._proposal_from_tool(db, {
        "summary": "Move the long run to Saturday.",
        "change_note": "Long run Sun -> Sat per request.",
        "sessions": _future_sessions(),
    })
    assert p is not None
    pending = proposals.list_pending(db)
    assert len(pending) == 1
    assert pending[0]["kind"] == "sessions"
    assert pending[0]["origin"] == "coach_chat"     # not applied — awaits approval
    assert pending[0]["status"] == "pending"


def test_proposal_from_tool_none_on_empty_sessions(db):
    assert chat._proposal_from_tool(db, {"summary": "x", "sessions": []}) is None
    assert chat._proposal_from_tool(db, {"summary": "x"}) is None
    assert proposals.list_pending(db) == []


def test_a_second_chat_proposal_supersedes_the_first(db):
    chat._proposal_from_tool(db, {"summary": "first", "sessions": _future_sessions()})
    chat._proposal_from_tool(db, {"summary": "second", "sessions": _future_sessions()})
    pending = proposals.list_pending(db)
    assert len(pending) == 1 and pending[0]["summary"] == "second"


# ---- response parsing (fake Anthropic client; no network) --------------------

def _fake_block(**kw):
    return types.SimpleNamespace(**kw)


class _FakeStream:  # mimics client.messages.stream(...) context manager
    def __init__(self, resp):
        self._resp = resp
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def get_final_message(self):
        return self._resp


def _install_fake_client(monkeypatch, blocks):
    resp = types.SimpleNamespace(content=blocks)
    client = types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **kw: _FakeStream(resp))
    )
    import app.llm as llm
    monkeypatch.setattr(llm, "get_client", lambda: client)


def test_parses_text_and_tool_call(db, monkeypatch):
    _install_fake_client(monkeypatch, [
        _fake_block(type="text", text="Moving your long run — approve the card."),
        _fake_block(type="tool_use", name="propose_plan_change",
                    input={"summary": "s", "sessions": _future_sessions()}),
    ])
    text, tool_input = chat.call_text_conversation([{"role": "user", "content": "x"}], "sys",
                                                   tools=[chat._propose_tool()])
    assert "long run" in text.lower()
    assert tool_input["sessions"][0]["type"] == "long_run"


def test_text_only_turn_has_no_tool_input(db, monkeypatch):
    _install_fake_client(monkeypatch, [_fake_block(type="text", text="Just coaching, no change.")])
    text, tool_input = chat.call_text_conversation([{"role": "user", "content": "x"}], "sys", tools=None)
    assert tool_input is None
    assert text == "Just coaching, no change."
