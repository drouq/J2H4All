"""Coach-proposed plan changes: a change discussed on Telegram becomes a
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
    text, tool_name, tool_input = chat.call_text_conversation(
        [{"role": "user", "content": "x"}], "sys", tools=[chat._propose_tool()])
    assert "long run" in text.lower()
    assert tool_name == "propose_plan_change"
    assert tool_input["sessions"][0]["type"] == "long_run"


def test_text_only_turn_has_no_tool_input(db, monkeypatch):
    _install_fake_client(monkeypatch, [_fake_block(type="text", text="Just coaching, no change.")])
    text, tool_name, tool_input = chat.call_text_conversation(
        [{"role": "user", "content": "x"}], "sys", tools=None)
    assert tool_name is None and tool_input is None
    assert text == "Just coaching, no change."


def test_each_tool_is_identified_by_name(db, monkeypatch):
    """Two tools now hang off the coach chat; the turn must route to the right one
    (a proposal card and a mark-done card do very different things)."""
    _install_fake_client(monkeypatch, [
        _fake_block(type="text", text="Marking Wednesday's gym."),
        _fake_block(type="tool_use", name="mark_session_done",
                    input={"session_date": "2027-08-05", "session_type": "strength",
                           "summary": "Wed gym, done Thu"}),
    ])
    _text, tool_name, tool_input = chat.call_text_conversation(
        [{"role": "user", "content": "x"}], "sys",
        tools=[chat._propose_tool(), chat._mark_done_tool()])
    assert tool_name == "mark_session_done"
    assert tool_input["session_type"] == "strength"


# ---- mark_session_done resolution -------------------------------------------


def _planned_gym(db, d):
    from app.models import Session as PlannedSession
    from app.util import utcnow
    s = PlannedSession(date=d, type="strength", title="Gym — Upper Body (Pull)", purpose="",
                       status="planned", created_at=utcnow(), updated_at=utcnow())
    db.add(s)
    db.flush()
    db.commit()
    return s


def test_mark_done_request_resolves_the_planned_session(db):
    wed = date.today() - timedelta(days=7)
    gym = _planned_gym(db, wed)
    req = chat._mark_done_request(db, {
        "session_date": wed.isoformat(), "session_type": "strength",
        "completed_on": (wed + timedelta(days=1)).isoformat(), "summary": "s",
    })
    assert req["session_id"] == gym.id
    assert "Gym — Upper Body (Pull)" in req["text"] and "done" in req["text"]


def test_mark_done_request_is_none_when_nothing_matches(db):
    """A hallucinated date/type must degrade to a plain coaching answer, not a card
    pointing at the wrong session."""
    wed = date.today() - timedelta(days=7)
    _planned_gym(db, wed)
    assert chat._mark_done_request(db, {"session_date": "not-a-date",
                                        "session_type": "strength", "summary": "s"}) is None
    assert chat._mark_done_request(db, {"session_date": wed.isoformat(),
                                        "session_type": "long_run", "summary": "s"}) is None
    assert chat._mark_done_request(db, {"session_date": (wed - timedelta(days=30)).isoformat(),
                                        "session_type": "strength", "summary": "s"}) is None


def test_a_failed_resolution_never_promises_a_card(db, monkeypatch):
    """The J2H4All-specific fix: a tool-only turn whose mark-done call resolves to
    nothing must fall back to the rephrase line, not to "Here's the card below."
    with no card under it."""
    _install_fake_client(monkeypatch, [
        _fake_block(type="tool_use", name="mark_session_done",
                    input={"session_date": "not-a-date", "session_type": "strength",
                           "summary": "s"}),
    ])
    answer, proposal, mark_done = chat.ask_with_proposal(db, "I did my gym", surface="telegram")
    assert proposal is None and mark_done is None
    assert "card below" not in answer
