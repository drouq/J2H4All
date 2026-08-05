"""The Garmin workout payload builder, and its target-free steps at both ends.

Why they exist (2026-08-03): a zone/pace target is wrong at both ends of a session,
because HR lags the effort. At the START the target armed at second one, so the watch
alerted ~10 s in — and a gentler target is NOT a fix, since HR starts below Z1 too. At
the END, coming off threshold work his HR takes minutes to fall through a Z1 cooldown
ceiling, so the cooldown step alerts him for being ABOVE target while his body is doing
exactly the right thing.

Why they're CARVED OUT rather than added on top (the coach's call, asked with full
doctrine + live prod state): the prescription is total duration, so carving changes the
alerting and not the training, whereas adding on top would run every session ~5 min
long by design — a 3h00 long run filing as 3h05, invisible extra volume that never
trips the >20% AND >15 min off-plan question. The total-preserved assertions below are
that decision; they should fail loudly if anyone reverses it.

The ease-out fires ONLY on a prescribed cooldown — a trailing free step on plain easy
and long runs was asked for and explicitly declined; see the test that guards it.

`build_workout` had no coverage at all before this file.
"""
from app.garmin import workouts

_TIME = workouts._END_TIME["conditionTypeKey"]
_DIST = workouts._END_DIST["conditionTypeKey"]


def _steps(payload):
    return payload["workoutSegments"][0]["workoutSteps"]


def _is_untargeted(step):
    return step["targetType"]["workoutTargetTypeKey"] == "no.target"


def _kind(step):
    return step["stepType"]["stepTypeKey"]


def _minutes(step):
    assert step["endCondition"]["conditionTypeKey"] == _TIME
    return step["endConditionValue"] / 60.0


def _km(step):
    assert step["endCondition"]["conditionTypeKey"] == _DIST
    return step["endConditionValue"] / 1000.0


# ------------------------------------------------------------------ plain runs

def test_plain_run_opens_target_free_and_keeps_its_total():
    steps = _steps(workouts.build_workout(
        {"title": "Easy Aerobic", "duration_min": 55, "target_zone": "Z2"}))
    assert len(steps) == 2
    assert _is_untargeted(steps[0])                       # the whole point
    assert _minutes(steps[0]) == workouts.EASE_MIN
    assert steps[1]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert steps[1]["zoneNumber"] == 2
    # Carved out, not added on top: 5 + 50 = the 55 the plan and calendar say.
    assert _minutes(steps[0]) + _minutes(steps[1]) == 55


def test_a_plain_run_gets_no_trailing_free_step():
    """Asked and DECLINED by the coach: there is no alert to fix at the end of an easy
    run (he's already in zone), so a cooldown step would be clutter on every run
    forever — and it costs most where it helps least. Guards the decision, not a bug."""
    steps = _steps(workouts.build_workout(
        {"title": "Easy Aerobic", "duration_min": 55, "target_zone": "Z2"}))
    assert len(steps) == 2
    assert not _is_untargeted(steps[-1])                  # ends IN zone


def test_a_recovery_jog_keeps_its_ceiling():
    """The coach's sharpest argument against a trailing free step: a 30 min Z1 jog
    would become 5 free + 20 Z1 + 5 free, stripping the ceiling out of the one session
    whose entire purpose is holding it. Only the opening 5 may go."""
    steps = _steps(workouts.build_workout(
        {"title": "Recovery Jog", "duration_min": 30, "target_zone": "Z1"}))
    assert len(steps) == 2
    assert _minutes(steps[1]) == 25 and steps[1]["zoneNumber"] == 1


def test_a_pace_target_gets_the_same_lead_in():
    """Not an HR-only problem — a pace band alerts from second one just the same."""
    steps = _steps(workouts.build_workout(
        {"title": "Tempo", "duration_min": 40, "target_pace": "4:30-4:50/km"}))
    assert _is_untargeted(steps[0])
    assert steps[1]["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert _minutes(steps[0]) + _minutes(steps[1]) == 40


def test_an_untargeted_session_is_left_alone():
    """No target = nothing to alert on; don't split a run for no reason."""
    steps = _steps(workouts.build_workout({"title": "Free run", "duration_min": 45}))
    assert len(steps) == 1
    assert _minutes(steps[0]) == 45


def test_a_zone_string_with_no_number_counts_as_no_target():
    """`_has_target` must agree with `_target_fields`, which needs a digit."""
    steps = _steps(workouts.build_workout(
        {"title": "Easy", "duration_min": 45, "target_zone": "easy"}))
    assert len(steps) == 1


def test_distance_run_carves_distance_not_time():
    """A time lead-in in front of a distance step would silently lengthen the run."""
    steps = _steps(workouts.build_workout(
        {"title": "10 km", "distance_km": 10.0, "target_zone": "Z2"}))
    assert _is_untargeted(steps[0])
    assert _km(steps[0]) == workouts.EASE_KM
    assert round(_km(steps[0]) + _km(steps[1]), 3) == 10.0


def test_distance_wins_when_both_are_set():
    """Mirrors `_end_fields`' precedence — the lead-in must carve the same unit."""
    steps = _steps(workouts.build_workout(
        {"title": "Both", "duration_min": 60, "distance_km": 10.0, "target_zone": "Z2"}))
    assert round(_km(steps[0]) + _km(steps[1]), 3) == 10.0


# ------------------------------------------------------------------ short sessions

def test_a_short_session_gets_a_shorter_lead_in_never_a_dead_step():
    """Half the step is the ceiling, so an 8-min opener can't become 5 + 3 — and a
    5-min one can't become 5 + 0, which would be a zero-length step on the watch."""
    steps = _steps(workouts.build_workout(
        {"title": "Shakeout", "duration_min": 8, "target_zone": "Z1"}))
    assert _minutes(steps[0]) == 4 and _minutes(steps[1]) == 4
    for s in steps:
        assert s["endConditionValue"] > 0


def test_a_lap_button_step_is_prefixed_without_carving():
    """No end value = open-ended, so there is no total to preserve."""
    steps = _steps(workouts.build_workout({"title": "Open", "target_zone": "Z2"}))
    assert len(steps) == 2
    assert _minutes(steps[0]) == workouts.EASE_MIN
    assert steps[1]["endCondition"]["conditionTypeKey"] == "lap.button"


# ------------------------------------------------------------------ structured sessions

def _intervals(warmup_zone="Z2", cooldown_zone="Z1", cooldown_min=15):
    return {
        "title": "5x4min", "duration_min": 65,
        "structure": [
            {"kind": "warmup", "duration_min": 15, "target_zone": warmup_zone},
            {"kind": "repeat", "times": 5, "steps": [
                {"kind": "work", "duration_min": 4, "target_pace": "4:30-4:50/km"},
                {"kind": "recover", "duration_min": 2, "target_zone": "Z1"},
            ]},
            {"kind": "cooldown", "duration_min": cooldown_min, "target_zone": cooldown_zone},
        ],
    }


def test_lead_in_is_carved_from_inside_a_prescribed_warmup():
    """The coach's point: a warmup keeps its place, the free minutes come out of it —
    the opening of a warmup shouldn't be zone-gated either. 15 min becomes 5 + 10."""
    steps = _steps(workouts.build_workout(_intervals()))
    assert _is_untargeted(steps[0])
    assert _minutes(steps[0]) == workouts.EASE_MIN
    assert _minutes(steps[1]) == 10                      # the rest of the warmup
    assert steps[1]["zoneNumber"] == 2                   # still targeted
    assert steps[2]["type"] == "RepeatGroupDTO"          # work block untouched


def test_a_prescribed_cooldown_opens_target_free():
    """The mirrored bug: off 5x4min at threshold his HR is 165-175 and takes minutes to
    fall through the Z1 ceiling, so the cooldown alerted him for recovering correctly.
    15 min Z1 becomes 5 free + 10 Z1 — same 15 minutes, same session total."""
    steps = _steps(workouts.build_workout(_intervals()))
    cooldowns = [s for s in steps if _kind(s) == "cooldown"]
    assert len(cooldowns) == 2
    assert _is_untargeted(cooldowns[0])
    assert _minutes(cooldowns[0]) == workouts.EASE_MIN
    assert _minutes(cooldowns[1]) == 10 and cooldowns[1]["zoneNumber"] == 1
    assert _minutes(cooldowns[0]) + _minutes(cooldowns[1]) == 15


def test_both_ends_are_free_and_the_session_total_is_unchanged():
    """Neither carve may add time: warmup 15 + work 30 + cooldown 15 = 60, before and
    after, with only the alerting boundaries moved."""
    steps = _steps(workouts.build_workout(_intervals()))
    total = 0.0
    for s in steps:
        if s["type"] == "RepeatGroupDTO":
            for child in s["workoutSteps"]:
                total += _minutes(child) * s["numberOfIterations"]
        else:
            total += _minutes(s)
    assert total == 60                                   # 15 + 5*(4+2) + 15
    assert _is_untargeted(steps[0]) and _is_untargeted(steps[-2])
    assert not _is_untargeted(steps[-1])                 # ends on the Z1 block


def test_an_untargeted_prescribed_cooldown_is_not_split():
    steps = _steps(workouts.build_workout(_intervals(cooldown_zone=None)))
    cooldowns = [s for s in steps if _kind(s) == "cooldown"]
    assert len(cooldowns) == 1
    assert _minutes(cooldowns[0]) == 15


def test_a_short_cooldown_degrades_instead_of_collapsing():
    steps = _steps(workouts.build_workout(_intervals(cooldown_min=6)))
    cooldowns = [s for s in steps if _kind(s) == "cooldown"]
    assert _minutes(cooldowns[0]) == 3 and _minutes(cooldowns[1]) == 3


def test_an_untargeted_prescribed_warmup_is_not_split():
    plan = _intervals(warmup_zone=None)
    steps = _steps(workouts.build_workout(plan))
    assert _minutes(steps[0]) == 15
    assert steps[1]["type"] == "RepeatGroupDTO"


def test_step_order_stays_sequential_through_the_repeat_block():
    """Both carves shift stepOrder; Garmin rejects a broken sequence."""
    steps = _steps(workouts.build_workout(_intervals()))
    orders = []
    for s in steps:
        orders.append(s["stepOrder"])
        for child in s.get("workoutSteps", []):
            orders.append(child["stepOrder"])
            assert child["childStepId"] == 1
    assert orders == list(range(1, len(orders) + 1))


def test_structure_leading_with_a_repeat_is_prefixed_not_carved():
    """Can't carve out of a repeat block — prepend rather than leave him alerted."""
    steps = _steps(workouts.build_workout({
        "title": "Odd", "duration_min": 30,
        "structure": [{"kind": "repeat", "times": 3, "steps": [
            {"kind": "work", "duration_min": 5, "target_pace": "5:00/km"}]}],
    }))
    assert _is_untargeted(steps[0])
    assert _minutes(steps[0]) == workouts.EASE_MIN
    assert steps[1]["type"] == "RepeatGroupDTO"


def test_a_session_opening_on_a_cooldown_is_split_only_once():
    """Degenerate, but the two passes must not both carve the same step: the lead-in
    owns index 0, so the ease-out skips it."""
    steps = _steps(workouts.build_workout({
        "title": "Odd", "duration_min": 20,
        "structure": [{"kind": "cooldown", "duration_min": 20, "target_zone": "Z1"}],
    }))
    assert len(steps) == 2
    assert _minutes(steps[0]) + _minutes(steps[1]) == 20


def test_the_stored_structure_is_never_mutated():
    """Both carves are a watch-rendering detail: the plan, the calendar description and
    the completion check all read the store's structure and must not see them."""
    plan = _intervals()
    original = [dict(s) for s in plan["structure"]]
    workouts.build_workout(plan)
    assert plan["structure"] == original
    assert plan["structure"][0]["duration_min"] == 15
    assert plan["structure"][-1]["duration_min"] == 15
