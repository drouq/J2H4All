"""First-run setup: the goal, the readiness report, and the plan-draft guard.

The behaviour worth protecting is the guard. A plan drafted against the
placeholder goal looks completely normal — correct-looking phases, sensible
volumes, a confident rationale — and is periodized backwards from a race date
nobody chose. The athlete has no way to tell it apart by reading it, which is
exactly why refusing beats producing it.
"""
from datetime import date, timedelta

import pytest

from app import setup as app_setup
from app.coach import doctrine
from app.context import store as ctx_store
from app.models import Goal
from app.plan import store as plan_store


# ------------------------------------------------------------------ setting a goal

def test_a_fresh_install_is_a_placeholder(db):
    plan_store.ensure_seed(db)
    goal = db.query(Goal).one()
    assert plan_store.is_placeholder(goal)
    assert goal.race_date > date.today()          # relative, so it can never rot


def test_setting_a_real_race_clears_the_placeholder(db):
    """Otherwise an athlete who fills in their race is still told it isn't set."""
    plan_store.set_goal(db, format="road-marathon", race_date="2027-04-18",
                        distance_km=42.195, target_time="sub-3:30")
    goal = db.query(Goal).one()
    assert not plan_store.is_placeholder(goal)
    assert goal.format == "road-marathon" and goal.target_time == "sub-3:30"


def test_format_is_normalized_through_the_registry(db):
    """'marathon' must land on the marathon doctrine, not on generic."""
    plan_store.set_goal(db, format="marathon", race_date="2027-04-18")
    assert db.query(Goal).one().format == "road-marathon"


def test_an_unknown_format_lands_on_generic_rather_than_the_wrong_doctrine(db):
    plan_store.set_goal(db, format="ironman", race_date="2027-04-18")
    assert db.query(Goal).one().format == "generic"


def test_partial_edit_does_not_blank_the_rest(db):
    """Same skip-nulls contract as the profile: changing the date must not wipe the
    distance the athlete entered last week."""
    plan_store.set_goal(db, format="trail-ultra", race_date="2027-06-01",
                        distance_km=100, elevation_gain_m=5200)
    plan_store.set_goal(db, race_date="2027-06-08")
    goal = db.query(Goal).one()
    assert str(goal.race_date) == "2027-06-08"
    assert goal.distance_km == 100 and goal.elevation_gain_m == 5200


def test_bad_input_is_rejected_not_stored(db):
    with pytest.raises(ValueError):
        plan_store.set_goal(db, race_date="next spring")
    with pytest.raises(ValueError):
        plan_store.set_goal(db, favourite_hill="Box Hill")


def test_the_goal_reaches_the_coach(db):
    """Setting a race must change what the coach is told, not just a table."""
    plan_store.set_goal(db, format="road-marathon", race_date=str(date.today() + timedelta(days=100)),
                        distance_km=42.195, target_time="sub-3:30")
    text = doctrine.full_doctrine(db)
    assert "MARATHON" in text and "sub-3:30" in text
    assert "Goal pace" in text          # the marathon doctrine, not the backyard one


# ------------------------------------------------------------------ the readiness report

def test_setup_view_never_raises_on_an_empty_install(db):
    """This is what someone opens when the app is misbehaving."""
    view = app_setup.view(db)
    assert view["steps"] and view["next"]
    assert set(view) == {"steps", "complete", "blockers", "next"}


def test_placeholder_goal_and_missing_key_are_the_only_blockers(db):
    """Deliberately narrow. Garmin, calendar, Telegram and the profile all degrade
    the plan without falsifying it — refusing there would be paternalistic."""
    keys = {s.key for s in app_setup.blockers(db)}
    assert keys == {"anthropic", "goal"}


def test_setting_the_race_clears_its_blocker(db):
    plan_store.set_goal(db, format="road-marathon", race_date="2027-04-18", distance_km=42.195)
    assert "goal" not in {s.key for s in app_setup.blockers(db)}


def test_profile_step_tracks_the_profile(db):
    def step(key):
        return next(s for s in app_setup.steps(db) if s.key == key)

    assert not step("profile").done
    ctx_store.set_profile(db, name="Sam")
    assert step("profile").done
    assert "Sam" in step("profile").detail


def test_every_incomplete_step_says_what_to_do(db):
    """A status list that reports a gap without an action has moved the problem."""
    for s in app_setup.steps(db):
        if not s.done:
            assert s.action, f"{s.key} has no action"


# ------------------------------------------------------------------ the draft guard

@pytest.fixture
def client(db):
    """The API with this test's database and a signed-in athlete. Overrides are
    cleared afterwards so one test can't leak its session into the next."""
    from fastapi.testclient import TestClient
    from app import main
    from app.auth import current_user
    from app.db import get_db

    main.app.dependency_overrides[current_user] = lambda: "athlete@example.com"
    main.app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.clear()


def test_drafting_against_a_placeholder_is_refused(client):
    """THE guard. The refusal must name what's missing — a bare 409 would leave the
    athlete clicking the button again."""
    res = client.post("/api/plan/draft")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["error"] == "setup_incomplete"
    assert "goal" in detail["blockers"]
    assert "Your race" in detail["message"]


def test_setup_endpoint_reports_the_same_blockers(client):
    body = client.get("/api/setup").json()
    assert "goal" in body["blockers"]
    assert body["complete"] is False


def test_goal_endpoint_sets_the_race(client):
    res = client.post("/api/setup/goal", json={
        "format": "trail-ultra", "race_date": "2027-06-01",
        "distance_km": 100, "elevation_gain_m": 5200,
    })
    assert res.status_code == 200
    assert res.json()["goal"]["format"] == "trail-ultra"
    assert "goal" not in client.get("/api/setup").json()["blockers"]


def test_a_garbage_garmin_token_is_rejected_at_paste_time(client):
    """Validate by loading it, not by trusting it — a truncated paste should fail
    here with a clear message rather than at 01:00 in a cron a day later."""
    res = client.post("/api/setup/garmin-token", json={"token": "obviously-not-a-token"})
    assert res.status_code == 422
    assert "valid Garmin token" in res.json()["detail"]
    assert client.post("/api/setup/garmin-token", json={"token": "   "}).status_code == 422


def test_the_garmin_bootstrap_token_is_an_internal_preference(db):
    """It is a credential. It must never reach the context panel or a prompt — the
    same rule that keeps the rotating OAuth token out of them."""
    from app.context.store import _is_internal_pref

    assert _is_internal_pref("garmin_bootstrap_token")
    ctx_store.stamp_meta(db, "garmin_bootstrap_token", "blob")
    snap = ctx_store.snapshot(db)
    assert all(p["key"] != "garmin_bootstrap_token" for p in snap["preferences"])


def test_an_unmigrated_database_says_so_rather_than_no_race_set(db, monkeypatch):
    """A failed read is not the same as an unset goal. Reporting it as one sends
    someone to fill in a form that was never the problem — and the usual cause is a
    database that hasn't been migrated."""
    def boom(*a, **k):
        raise RuntimeError("no such column: goal.distance_km")
    monkeypatch.setattr(db, "scalar", boom)

    step = next(s for s in app_setup.steps(db) if s.key == "goal")
    assert step.done is False and step.blocking is True
    assert "out of date" in step.detail
    assert "alembic upgrade head" in step.action


def test_changing_format_clears_the_previous_format_s_fields(db):
    """Skip-nulls is right for editing WITHIN a format and wrong ACROSS one. A
    backyard's target_laps survived a switch to marathon, and the plan panel duly
    announced "your marathon - 24 laps"."""
    plan_store.set_goal(db, format="backyard-ultra", race_date="2027-04-18",
                        loop_km=6.706, target_laps=24)
    plan_store.set_goal(db, format="road-marathon", distance_km=42.195, target_time="sub-3:30")
    goal = db.query(Goal).one()
    assert goal.target_laps is None and goal.loop_km is None
    assert goal.distance_km == 42.195 and goal.target_time == "sub-3:30"


def test_editing_within_a_format_keeps_its_fields(db):
    """The mirror: re-sending the same format must NOT wipe anything."""
    plan_store.set_goal(db, format="trail-ultra", race_date="2027-06-01",
                        distance_km=100, elevation_gain_m=5200)
    plan_store.set_goal(db, format="trail-ultra", race_date="2027-06-08")
    goal = db.query(Goal).one()
    assert goal.distance_km == 100 and goal.elevation_gain_m == 5200
