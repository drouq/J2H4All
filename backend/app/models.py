from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB on Postgres, plain JSON elsewhere (local SQLite smoke tests)
JsonCol = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Heartbeat(Base):
    """SPA → API → Postgres round-trip check (Phase 0 origin, still LIVE): backs
    `POST /api/heartbeat` and the web's "✓ API + database connected" line."""

    __tablename__ = "heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )


class Activity(Base):
    """One Garmin activity. Typed columns for what the coach queries;
    the full payloads live in `raw` so nothing is lost."""

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Garmin activityId
    start_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_time_local: Mapped[datetime | None] = mapped_column(DateTime())  # wall clock at source
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255))

    distance_m: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[float | None] = mapped_column(Float)
    elevation_gain_m: Mapped[float | None] = mapped_column(Float)
    avg_hr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    avg_speed_mps: Mapped[float | None] = mapped_column(Float)
    avg_run_cadence: Mapped[float | None] = mapped_column(Float)
    calories: Mapped[float | None] = mapped_column(Float)
    aerobic_te: Mapped[float | None] = mapped_column(Float)
    anaerobic_te: Mapped[float | None] = mapped_column(Float)
    vo2max: Mapped[float | None] = mapped_column(Float)
    # Self-evaluation the athlete logs on the activity (from the detail endpoint).
    feel: Mapped[int | None] = mapped_column(Integer)  # 0/25/50/75/100 = very weak..very strong
    rpe: Mapped[int | None] = mapped_column(Integer)   # 0-100; displayed /10 (60 -> 6/10)
    # Weather at the run's start (Open-Meteo, by GPS + time) — context for the read.
    weather_temp_c: Mapped[float | None] = mapped_column(Float)
    weather_humidity: Mapped[int | None] = mapped_column(Integer)
    weather_feels_c: Mapped[float | None] = mapped_column(Float)

    hr_zones: Mapped[dict | list | None] = mapped_column(JsonCol)  # time-in-zone detail
    laps: Mapped[dict | list | None] = mapped_column(JsonCol)  # splits detail
    # Durability rollup from the per-second stream (decoupling / HR drift / pace CV).
    stream_metrics: Mapped[dict | None] = mapped_column(JsonCol)
    raw: Mapped[dict] = mapped_column(JsonCol, nullable=False)  # summary payload as pulled
    detail_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    streams_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WellnessDaily(Base):
    """Per-day recovery picture: RHR, HRV, sleep, Body Battery, stress,
    weight. `raw` holds each source payload keyed by source name."""

    __tablename__ = "wellness_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)

    resting_hr: Mapped[int | None] = mapped_column(Integer)
    hrv_last_night_avg: Mapped[int | None] = mapped_column(Integer)
    hrv_status: Mapped[str | None] = mapped_column(String(32))
    sleep_seconds: Mapped[int | None] = mapped_column(Integer)
    sleep_score: Mapped[int | None] = mapped_column(Integer)
    sleep_stages: Mapped[dict | None] = mapped_column(JsonCol)
    body_battery_high: Mapped[int | None] = mapped_column(Integer)
    body_battery_low: Mapped[int | None] = mapped_column(Integer)
    stress_avg: Mapped[int | None] = mapped_column(Integer)
    steps: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    body_fat_pct: Mapped[float | None] = mapped_column(Float)

    raw: Mapped[dict] = mapped_column(JsonCol, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FitnessMarker(Base):
    """Periodic fitness signals: VO2max trend, race predictor,
    training status/load. Generic kind+value so new markers slot in."""

    __tablename__ = "fitness_marker"
    __table_args__ = (UniqueConstraint("date", "kind", name="uq_fitness_marker_date_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # vo2max_running, race_prediction, training_status, ...
    value_num: Mapped[float | None] = mapped_column(Float)
    value: Mapped[dict | list | None] = mapped_column(JsonCol)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncRun(Base):
    """Sync audit trail + staleness source: what `sync_status` reads, and what lets
    the app degrade loudly instead of coaching off stale data."""

    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # full | incremental
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # running | success | failure
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text)  # failure summary
    stats: Mapped[dict | None] = mapped_column(JsonCol)  # counts pulled per entity
    alerted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


# ---------------------------------------------------------------- Phase 2: context

class AthleteProfile(Base):
    """WHO the athlete is. Single row, id pinned to 1 (same shape as UserState).

    This table exists so that no fact about a particular person is ever hardcoded
    into a prompt again. `coach/doctrine.py` renders from here; if you find
    yourself about to write someone's name, age or physiology into prompt text,
    it belongs in this table (structured) or in a coaching Note (free text).

    Every field is nullable or defaulted: a fresh install has no profile, and the
    coach must still work — more carefully, saying what it doesn't know — until
    the athlete fills one in.
    """

    __tablename__ = "athlete_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    # What the coach calls them. None -> it addresses them directly instead.
    name: Mapped[str | None] = mapped_column(String(64))
    # Free-form rather than an enum: "she/her", "he/him", "they/them", or whatever
    # the athlete tells us. Defaults to they/them — correct for an unknown person,
    # rather than a guess about one.
    pronouns: Mapped[str] = mapped_column(String(32), nullable=False,
                                          server_default=text("'they/them'"))
    birthdate: Mapped[date | None] = mapped_column(Date)
    # Preferred coaching language as an IETF tag ("en", "fr"). None = mirror
    # whatever language they wrote in, which is the existing default behaviour.
    language: Mapped[str | None] = mapped_column(String(16))
    # Free text: anything that changes HOW THEIR DATA SHOULD BE READ, as opposed to
    # how they should be trained. Restless legs or a newborn wrecking sleep scores
    # while recovery is genuinely fine; a medication that caps heart rate. The
    # coach is told to weigh these above the raw metric.
    data_caveats: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DietaryProfile(Base):
    """Diet pattern + free-text notes the coach references for fueling.

    `diet` is a free-text label the athlete gives ("omnivore", "vegetarian",
    "vegan", "coeliac") rather than an enum — it feeds a prompt, not a branch. It
    used to default to one athlete's diet; it now defaults to 'unspecified' so the
    coach knows it hasn't been told, instead of assuming."""

    __tablename__ = "dietary_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    diet: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'unspecified'"))
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BloodMarker(Base):
    """One marker reading, trended over time. name+date is the natural key."""

    __tablename__ = "blood_marker"
    __table_args__ = (UniqueConstraint("name", "measured_on", name="uq_blood_marker_name_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # ferritin, hemoglobin, ...
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    measured_on: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'chat'"))  # chat | pdf
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AvailabilityWindow(Base):
    """Dated training-constraint window. Treadmill is v1's first-class type; modeled
    generically so 'track only' / 'limited time' slot in later without a rebuild."""

    __tablename__ = "availability_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # treadmill, ...
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)  # open-ended if null
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InjuryLog(Base):
    """Body part, status, dates, notes."""

    __tablename__ = "injury_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body_part: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))  # active | resolved
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Preference(Base):
    """Structured constraints: long-run day, no-sessions-before time, equipment access."""

    __tablename__ = "preference"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Note(Base):
    """Free-text coaching memory that doesn't fit a field."""

    __tablename__ = "note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserState(Base):
    """Single-row app state. Current timezone is set by the user via chat;
    everything stored UTC, rendered in this zone. id is pinned to 1."""

    __tablename__ = "user_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'UTC'"))
    # The dedicated "J2H4All Training" Google calendar we create and exclusively own.
    training_calendar_id: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OAuthCredential(Base):
    """Server-side OAuth refresh token for a provider (Phase 4: Google Calendar).

    Obtained once via an interactive offline-access consent flow and persisted so
    the coach can push to the calendar even when no browser session is open. Never
    sent to the client. In production GOOGLE_REFRESH_TOKEN (env) takes
    precedence; this row is the runtime-obtained fallback."""

    __tablename__ = "oauth_credential"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. google_calendar
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------- Phase 3: goal & plan

class Goal(Base):
    """The primary A-race, structured so the coach knows this is a metronomic-
    durability + sleep-deprivation + hourly-fueling problem. One active."""

    __tablename__ = "goal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Selects the race-format doctrine (coach/formats/): backyard-ultra, trail-ultra,
    # road-ultra, road-marathon, or generic. Unknown values resolve to generic rather
    # than raising — a typo must not take down every prompt.
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    # Backyard-only: the loop and how many laps are being chased.
    loop_km: Mapped[float | None] = mapped_column(Float)
    target_laps: Mapped[int | None] = mapped_column(Integer)
    # Every other format: distance, and (trail) how much climbing. Nullable because
    # which of these carries meaning depends entirely on the format.
    distance_km: Mapped[float | None] = mapped_column(Float)
    elevation_gain_m: Mapped[float | None] = mapped_column(Float)
    # Free text, not seconds: it only ever feeds a prompt, and "sub-3:30" or "under
    # 24h" is a more honest statement of a goal than a rounded integer.
    target_time: Mapped[str | None] = mapped_column(String(32))
    race_date: Mapped[date] = mapped_column(Date, nullable=False)
    race_timezone: Mapped[str | None] = mapped_column(String(64))  # anchor for taper countdown
    floor_note: Mapped[str | None] = mapped_column(Text)
    stretch_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecondaryRace(Base):
    """Supporting races the coach reasons about for interplay/taper depth."""

    __tablename__ = "secondary_race"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    distance_km: Mapped[float | None] = mapped_column(Float)
    type: Mapped[str | None] = mapped_column(String(32))  # trail, road, ...
    priority: Mapped[str] = mapped_column(String(4), nullable=False, server_default=text("'B'"))  # A|B|C
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MacroPlan(Base):
    """Layer 1: dated phases + weekly targets to race day. Stable; a new
    plan supersedes the prior one rather than mutating it."""

    __tablename__ = "macro_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))  # active|superseded
    rationale: Mapped[str | None] = mapped_column(Text)
    b_race_approach: Mapped[str | None] = mapped_column(Text)
    phases: Mapped[list] = mapped_column(JsonCol, nullable=False)  # [{name,start_date,end_date,focus,weekly_km_low,weekly_km_high,intensity_note}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Session(Base):
    """Layer 2: a detailed planned session in the rolling window. Every
    session carries its 'why' (purpose). Superseded, never silently swapped."""

    __tablename__ = "session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    macro_plan_id: Mapped[int | None] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # long_run, easy, intervals, rest, ...
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    distance_km: Mapped[float | None] = mapped_column(Float)
    target_zone: Mapped[str | None] = mapped_column(String(32))
    target_pace: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(Text, nullable=False)  # the 'why'
    fueling_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'planned'"))  # planned|superseded
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))  # set in Phase 4
    structure: Mapped[dict | None] = mapped_column(JsonCol)  # coach-prescribed steps (plan/structure.py); null = plain run
    garmin_workout_id: Mapped[str | None] = mapped_column(String(64))  # pushed Garmin structured workout
    garmin_schedule_id: Mapped[str | None] = mapped_column(String(64))  # its calendar-date pin in Garmin
    # When the coach flagged that this planned run never happened (coach/missed.py).
    # Set once and never cleared — it is what stops a session being raised twice.
    missed_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionResult(Base):
    """Layer 3: what Garmin says actually happened, linked to a planned
    session. The coaching 'read' (HR drift, over/under-cooked) lands in Phase 5."""

    __tablename__ = "session_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int | None] = mapped_column(Integer, index=True)  # null = unplanned activity
    activity_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # Garmin activity
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    actual_distance_km: Mapped[float | None] = mapped_column(Float)
    actual_duration_min: Mapped[float | None] = mapped_column(Float)
    actual_avg_hr: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    read_summary: Mapped[str | None] = mapped_column(Text)  # Phase 5: coach's planned-vs-actual read
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))  # acute concern
    # A session >20% off its planned duration/distance is a QUESTION, not a diagnosis:
    # the coach asks what happened (stamping `deviation_asked_at`, once) and stores their
    # answer here rather than inferring a physical cause. See coach/completion.py.
    deviation_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deviation_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Proposal(Base):
    """Pending/approved/rejected side-effectful changes. Persisted so an
    approval survives restarts and can't be double-applied (idempotency)."""

    __tablename__ = "proposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # onboarding_draft | macro_plan | sessions | session_revision
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"), index=True)
    origin: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'web'"))  # web | weekly_review | red_flag
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonCol, nullable=False)  # the proposed changes
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- Phase 5: adaptation & proactivity

class Checkin(Base):
    """Daily subjective check-in — perceived effort, soreness, motivation, life
    stress. Garmin can't see these; a real coach asks. One row/day."""

    __tablename__ = "checkin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    energy: Mapped[int | None] = mapped_column(Integer)       # 1 (spent) .. 5 (fresh)
    soreness: Mapped[int | None] = mapped_column(Integer)     # 1 (none) .. 5 (very sore)
    motivation: Mapped[int | None] = mapped_column(Integer)   # 1 .. 5
    life_stress: Mapped[int | None] = mapped_column(Integer)  # 1 (calm) .. 5 (high)
    note: Mapped[str | None] = mapped_column(Text)            # free text ("knee cranky")
    raw: Mapped[dict | None] = mapped_column(JsonCol)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LifestyleLog(Base):
    """Daily end-of-day log of life factors Garmin can't see — alcohol, feeling ill,
    sleep disruptors (RLS/late night/stress), nutrition, extra workouts, travel.
    Prompted at 22:00 local over Telegram; feeds the coach's recovery/load read so a
    poor overnight reading can be attributed (a beer + late night, not fitness). One
    row/day. `raw_text` is their words; `data` is the LLM-parsed structured fields."""

    __tablename__ = "lifestyle_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    raw_text: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JsonCol)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Message(Base):
    """Coaching chat history, shared by both surfaces. Flat log — one
    implicit conversation for the single user."""

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    surface: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'web'"))  # web | telegram
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ScheduledJobRun(Base):
    """One row per (job, local date) it fired — lets a fixed-interval cron dispatch
    on the user's LOCAL clock exactly once per day, idempotently."""

    __tablename__ = "scheduled_job_run"
    __table_args__ = (UniqueConstraint("job", "ran_on", name="uq_scheduled_job_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(32), nullable=False)   # morning_brief | daily_checkin | weekly_review
    ran_on: Mapped[date] = mapped_column(Date, nullable=False)     # user-local date
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
