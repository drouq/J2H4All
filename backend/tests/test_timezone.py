"""Local-clock rendering (store UTC, render LOCAL).

Regression: the morning brief told the athlete "your watch synced at 2am" — our 01:00-UTC
cron's pull timestamp, quoted as a raw UTC hour (8h off in Singapore) AND mislabelled
as a watch upload. These cover the rendering half; the mislabel is fixed by naming
(`last_backend_pull_local`) + prompt guidance.
"""
from datetime import UTC, datetime

from app.coach import doctrine, schedule
from app.context.store import get_or_create_state


def _set_tz(db, tz: str):
    state = get_or_create_state(db)
    state.timezone = tz
    db.commit()


def test_to_local_converts_utc_to_singapore(db):
    _set_tz(db, "Asia/Singapore")
    # The exact bug: 02:00 UTC is 10:00 in Singapore, not 2am.
    utc_2am = datetime(2026, 7, 16, 2, 0, tzinfo=UTC)
    assert schedule.to_local(db, utc_2am).hour == 10


def test_to_local_assumes_utc_for_naive_input(db):
    """SQLite round-trips DateTime(timezone=True) as naive — must not be read as local."""
    _set_tz(db, "Asia/Singapore")
    naive_2am = datetime(2026, 7, 16, 2, 0)  # no tzinfo
    assert schedule.to_local(db, naive_2am).hour == 10


def test_fmt_local_renders_offset(db):
    _set_tz(db, "Asia/Singapore")
    out = schedule.fmt_local(db, datetime(2026, 7, 16, 2, 0, tzinfo=UTC))
    assert "09:58" not in out          # sanity: not a stray fixed string
    assert out.startswith("2026-07-16 10:00")
    assert "+08" in out


def test_local_tz_follows_travel_not_hardcoded(db):
    """The zone is set by chat and must follow them — never a hardcoded offset."""
    _set_tz(db, "Europe/London")
    # 02:00 UTC in July = 03:00 BST.
    assert schedule.to_local(db, datetime(2026, 7, 16, 2, 0, tzinfo=UTC)).hour == 3


def test_unknown_timezone_falls_back_to_utc(db):
    _set_tz(db, "Not/AZone")
    assert schedule.to_local(db, datetime(2026, 7, 16, 2, 0, tzinfo=UTC)).hour == 2


def test_doctrine_states_the_configured_zone(db):
    _set_tz(db, "Asia/Singapore")
    line = doctrine.timezone_line(db)
    assert "Asia/Singapore" in line
    assert "never UTC" in line.lower() or "never quote a raw utc" in line.lower()


def test_doctrine_timezone_line_survives_no_db():
    # doctrine must always render, even without a session (prompt_eval / fallbacks).
    assert "UTC" in doctrine.timezone_line(None)


def test_doctrine_defaults_to_his_local_day_not_the_server_day(db):
    """A caller that omits `today` must still get THEIR day. The default was
    `date.today()` (UTC on Render) until 2026-08-03 — latent, since every caller
    passes `today`, but one that forgot would render the days-to-race countdown a
    day short through their whole 00:00-08:00 window. Proven with two zones 26h
    apart: their local dates can never coincide, so a default that ignored
    user_state would hand back the same date for both.
    """
    _set_tz(db, "Pacific/Kiritimati")            # +14
    ahead = doctrine._default_today(db)
    _set_tz(db, "Etc/GMT+12")                    # -12
    behind = doctrine._default_today(db)
    assert ahead > behind

    # And it reaches the prompt, not just the helper: the countdown moves with the zone.
    _set_tz(db, "Pacific/Kiritimati")
    block_ahead = doctrine.athlete_block(db)
    _set_tz(db, "Etc/GMT+12")
    block_behind = doctrine.athlete_block(db)
    assert block_ahead != block_behind


def test_doctrine_still_renders_without_a_db():
    """`_facts` accepts db=None (static/no-store rendering); the local-day default
    must fall back to the server day there rather than raising."""
    block = doctrine.athlete_block(None)
    assert "THE ATHLETE & THE GOAL:" in block
    assert "BACKYARD ULTRA" in block
    assert doctrine.compact_doctrine(None)


def test_doctrine_carries_no_hardcoded_athlete():
    """The rule that makes this app reusable: doctrine renders the GOAL, and points
    at the context store for everything personal. No real person's name, race
    location, physiology or history may be baked into the prompt text — if it is,
    every install inherits one athlete's biography. See doctrine.py's module docstring."""
    text = doctrine.full_doctrine(None, execution=True) + doctrine.compact_doctrine(None)
    for leaked in ("Daniel", "Pasir Ris", "Singapore", "Forest Force", "Kosciuszko", "Kim"):
        assert leaked not in text, f"{leaked!r} is hardcoded in the doctrine"


def test_placeholder_goal_is_never_in_the_past():
    """The pre-onboarding placeholder is relative, not a fixed date, so a fresh
    install years from now still renders a future race rather than a negative
    countdown. A hardcoded date would rot silently."""
    block = doctrine.athlete_block(None)
    assert "-" not in block.split("days /")[0].split("(")[-1]


def test_data_freshness_reports_the_pull_in_local_time(db):
    """The bug site: a 02:00-UTC cron pull must reach the coach as 10:00 (+08),
    under a key that can't be misread as the watch's upload time."""
    from datetime import date as _date

    from app.coach import signals
    from app.models import WellnessDaily

    _set_tz(db, "Asia/Singapore")
    today = _date(2026, 7, 16)
    db.add(WellnessDaily(date=today, raw={}, resting_hr=47,
                         synced_at=datetime(2026, 7, 16, 2, 0, tzinfo=UTC)))
    db.commit()

    out = signals.data_freshness(db, today)
    assert "last_garmin_sync_utc" not in out        # the misleading key is gone
    assert out["last_backend_pull_local"].startswith("2026-07-16 10:00")
    assert "+08" in out["last_backend_pull_local"]
    assert "2026-07-16 02:00" not in out["last_backend_pull_local"]  # never the raw UTC hour
