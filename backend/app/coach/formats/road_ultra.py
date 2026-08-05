"""Road / flat ultra: 50 km through 24-hour, on tarmac, track or towpath. Relentless
uniform gait, no terrain relief, and a pace that is genuinely runnable throughout.

NOT validated against a real athlete's season - see the maturity note in
`formats/__init__.py`."""

from __future__ import annotations

from datetime import date

from . import Format, _n


def _goal_line(g: dict, today: date, days: int) -> str:
    dist = _n(g.get("distance_km"), " km")
    bits = [f"A-race: a ROAD / FLAT ULTRA on {g['race_date'].isoformat()} "
            f"({days} days / ~{max(days, 0) // 7} weeks away)"]
    if dist:
        bits.append(f" - {dist}")
    if g.get("target_time"):
        bits.append(f", target {g['target_time']}")
    bits.append(". Flat and runnable throughout: there is no terrain to hide behind and no climb to walk.")
    return "".join(bits)


RACE_DEMANDS = """WHAT THIS RACE DEMANDS (reason from these, not from trail or marathon doctrine):
- It is relentless. Flat and runnable means no climbs forcing a walk and no descents changing the muscle \
recruitment - the same tissues take the same load for the entire race. Repetitive-strain and soft-tissue \
breakdown are the mechanical risks, not quad damage.
- Pace discipline is unusually punishing, because a flat course lets an athlete hold a too-fast pace for far \
longer than they can afford. There is no hill to impose honesty. The early pace must be set by plan and by \
effort, not by how good it feels.
- Fueling over many hours at a genuinely running (not hiking) intensity: blood flow to the gut is lower than \
on trail, so a rehearsed, tolerated intake matters more, not less. Gut failure is a leading cause of DNF.
- Efficiency and cadence economy decide the late race. Small inefficiencies compound over tens of thousands of \
identical strides; form under fatigue is a trainable outcome.
- Mental monotony is a real variable, especially on looped or out-and-back courses. Segmenting the race and \
having a plan for the psychological trough is part of preparation.
- Where the event is fixed-time (6/12/24 hour) rather than fixed-distance, the objective changes: it becomes \
an exercise in minimising time NOT moving, and a disciplined walk-break structure from the start usually \
beats running until forced to stop."""

TRAINING_ADDENDA = """ROAD-ULTRA-SPECIFIC TRAINING:
- Sustained race-pace blocks inside long runs: the key session is holding goal effort while already tired, \
because that is exactly the race. Progress the duration of the block, not its pace.
- Back-to-back long runs build fatigue-resistance at lower injury risk than a single monster run, and rehearse \
holding form on tired legs.
- Structured walk breaks, if they will be used on the day, are rehearsed from the build phase so the \
run-to-walk transition is practised rather than a capitulation.
- Surface specificity: train enough on the actual race surface to condition the tissues for it. Accumulating \
all long-run volume on soft trail before a tarmac ultra is a common and avoidable mistake.
- Cadence and running economy work - short strides, relaxed form drills, and attention to form late in long \
runs - matters more here than in any other ultra format.
- Shoe rotation and a proven race shoe: identical repetitive loading makes footwear choice consequential; the \
race shoe must have been worn for a long run at distance, never straight from the box.
- The taper trims volume substantially over the final 2 weeks while keeping frequency and short touches of \
race pace, aiming for full glycogen and fresh legs."""

EXECUTION = """RACE-DAY EXECUTION (when strategy comes up):
- Go out slower than feels right, and hold it. On a flat course the cost of an early over-pace is deferred, \
which is exactly what makes it dangerous - the first hour should feel almost too easy.
- Even effort over even pace as the race matures: expect and accept a small drift, and protect the ability to \
keep running rather than defending a split.
- Fueling: a rehearsed hourly carbohydrate target with fluids and sodium scaled to conditions, taken on a \
schedule rather than by feel - at running intensity, appetite is a poor guide and arrives too late.
- Where walk breaks are part of the plan, take them from the beginning, on schedule, before they are needed. \
A walk break taken early is cheap; one taken because running stopped being possible is not.
- Aid or crew stops: keep them short and pre-decided. On a fixed-time event, time not moving is the single \
biggest controllable loss.
- Feet and chafe: lube and tape proactively, and plan a sock change on a long race. Identical repeated \
loading makes hotspots almost inevitable without prevention.
- Break the race into segments and race only the current one. On looped courses, count segments rather than \
laps remaining."""

SPEC = Format(
    key="road-ultra",
    label="road / flat ultra",
    goal_fields=("distance_km", "target_time"),
    phases="base -> build -> race-specific (sustained race-effort blocks) -> taper",
    goal_line=_goal_line,
    race_demands=RACE_DEMANDS,
    training_addenda=TRAINING_ADDENDA,
    execution=EXECUTION,
    compact=("a flat, relentless ultra: no terrain relief, identical loading throughout. Pace discipline "
             "(a flat course hides an over-pace for hours), rehearsed hourly fueling at running intensity, "
             "and cadence economy under fatigue are the variables."),
)
