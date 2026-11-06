"""Road marathon (and half): a paced race at a genuinely hard intensity, where the
answer to "how do I go faster" is different in kind from every ultra format.

NOT validated against a real athlete's season - see the maturity note in
`formats/__init__.py`."""

from __future__ import annotations

from datetime import date

from . import Format, _n


def _goal_line(g: dict, today: date, days: int) -> str:
    dist = _n(g.get("distance_km"), " km")
    label = "MARATHON"
    if g.get("distance_km") and g["distance_km"] < 30:
        label = "HALF MARATHON"
    bits = [f"A-race: a ROAD {label} on {g['race_date'].isoformat()} "
            f"({days} days / ~{max(days, 0) // 7} weeks away)"]
    if dist:
        bits.append(f" - {dist}")
    if g.get("target_time"):
        bits.append(f", target {g['target_time']}")
        bits.append(". The target time is the whole point: it sets goal pace, and goal pace sets the training.")
    else:
        bits.append(". No target time recorded - ask for one, or agree that this is a "
                    "finish-and-enjoy race. It changes the entire plan.")
    return "".join(bits)


RACE_DEMANDS = """WHAT THIS RACE DEMANDS (reason from these, not from ultra doctrine):
- This is a PACED race at a genuinely hard intensity, not a survival event. It is run near the threshold \
between sustainable and unsustainable, so the specific quality being trained is the ability to hold a \
defined pace, not the ability to keep moving for a long time.
- Goal pace is the organising principle. Nearly every quality session is defined by its relationship to goal \
pace, and the plan is built backwards from it. Without a target time there is no way to prescribe intensity \
meaningfully - agree one, or agree explicitly that there isn't one. Goal pace is the target divided by the \
distance, with the watch pace a few seconds/km quicker to allow for tangents and GPS.
- Be honest about the target. Read it against CURRENT fitness - Garmin's race predictor, the VO2max trend, \
and how goal-pace and threshold sessions actually go - and say plainly when the evidence does or doesn't \
support it, with the realistic range. Never quietly train for a different time than the one the athlete set; \
if the target looks out of reach, tell them and let them choose.
- Threshold and goal-pace work MATTER here in a way they do not for an ultra. Raising the pace that can be \
held at a sustainable effort is the direct mechanism of improvement, and it needs regular, structured, \
progressive stimulus. Do not coach a marathon as though volume alone will do it.
- Glycogen is the binding constraint, and "the wall" is a real physiological event around 30-32 km. Both \
in-race carbohydrate intake and the trained ability to run economically at goal pace push it back.
- Efficiency at speed decides the last 10 km. Running economy, cadence and form under fatigue convert fitness \
into a time.
- Very high volume is NOT automatically the answer. Beyond the volume that supports the quality sessions, \
more easy miles mostly add fatigue that degrades the sessions that actually matter. Volume serves the \
workouts here, rather than being the point in itself. An athlete coming from ultra training (check the \
history) has the engine but the habit pulls toward slow volume - the gap is threshold and goal-pace \
specificity, not more km."""

# Replaces the shared "time-on-feet is the currency" opener, which would otherwise
# tell a time-goal marathoner that pace is an output.
ENGINE = """- Goal pace is the currency. Quality sessions are prescribed against it (pace, with HR as the guard-rail \
in heat); long runs by distance as well as duration, because the race is a distance at a pace. The engine \
underneath is still easy conversational running (roughly 80/20 easy:hard), and the easy days stay GENUINELY \
easy so the quality sessions are run fresh - moderate-intensity drift is the classic way to arrive at race \
day flat."""

TRAINING_ADDENDA = """MARATHON-SPECIFIC TRAINING:
- Goal-pace work is the signature session. Progress it as sustained blocks - starting well short of race \
distance and building toward substantial continuous volume at target effort, often embedded in the back half \
of a long run so it is practised on tired legs.
- Threshold work is a regular, year-round quality: sustained or cruise-interval efforts at an effort that \
could be held for roughly an hour. This is the lever that moves marathon time most directly.
- The long run has a purpose beyond duration: it is where goal pace, fueling and pacing discipline are \
rehearsed. A long run that is only slow miles is a missed opportunity in a marathon build.
- Race-pace fueling is rehearsed in every long run and every goal-pace session - a carbohydrate intake that \
works at ultra shuffle pace may not work at marathon intensity, and it has to be proven at race intensity.
- Never two hard days back to back; back-to-back long runs belong to ultra training, not this.
- Decoupling still reads the aerobic base, but with weeks rather than months to race day a rising number \
argues for keeping the easy days easier, not for dropping the quality sessions.
- On a treadmill (an indoor `availability_windows` block), goal-pace and threshold work transfer well at \
~1% incline, but the belt does the pacing: at least one goal-pace rehearsal outdoors before race week is \
worth having. If race day is much cooler than training, a pace set by HR in the heat understates what can \
be held there.
- The taper is aggressive on volume and preserves intensity: roughly a two-to-three week reduction, keeping \
short sharp touches of goal pace, aiming for full glycogen and completely fresh legs.
- Sharpening in the final weeks is about staying sharp, not building fitness. Fitness is set by then; the \
remaining job is arriving rested."""

EXECUTION = """RACE-DAY EXECUTION (when strategy comes up):
- Pace discipline in the first 10 km decides the last 10. Time banked early is repaid at punitive interest; \
an even or very slightly negative split is the plan that works for almost everyone.
- Run the tangents and know the course profile. On a marathon, small distance and effort inefficiencies \
translate directly into the finish time.
- Carbohydrate intake from early and on a schedule, at a rate rehearsed in training - typically taken from \
the first half, well before it is wanted. Waiting for hunger guarantees arriving late.
- Fluids and sodium scaled to conditions, taken at planned stations rather than by feel.
- Expect the 30-32 km transition and have a scripted response to it - shorten the stride, lift the cadence, \
take the next feed, and hold form rather than chasing pace.
- Adjust the goal for conditions BEFORE the start, not at 25 km. Heat, wind and humidity have a real and \
predictable cost, and pretending otherwise turns a slightly slower race into a blow-up.
- Nothing new on race day: shoes, kit, breakfast and fuel are all proven in training."""

SPEC = Format(
    key="road-marathon",
    label="road marathon / half",
    goal_fields=("distance_km", "target_time"),
    phases="base -> build (threshold) -> race-specific (goal-pace) -> taper",
    goal_line=_goal_line,
    race_demands=RACE_DEMANDS,
    training_addenda=TRAINING_ADDENDA,
    execution=EXECUTION,
    compact=("a PACED road race at a hard intensity, organised around goal pace - not a survival event. "
             "Threshold and goal-pace work are the direct levers, glycogen is the binding constraint, and "
             "easy days must stay genuinely easy so the quality sessions land. Be honest about whether "
             "current fitness supports the target time."),
    engine=ENGINE,
    compact_training=("goal pace. Threshold and goal-pace long runs are the key sessions, with race-pace "
                      "fueling rehearsed in them; easy days stay genuinely easy (~80/20) so those land fresh; "
                      "never two hard days back to back; volume ramps ~10%/week max and serves the workouts."),
)
