"""Fallback: an endurance race the app has no specific doctrine for.

Reached when `Goal.format` is unset, misspelt beyond the alias table, or names a
format nobody has written doctrine for yet. The right behaviour is to coach sound
general endurance and be honest about the gap - NOT to guess at the demands of a
race we know nothing about, and not to quietly apply backyard doctrine to a
track 10k because backyard happens to be the default that shipped."""

from __future__ import annotations

from datetime import date

from . import Format, _n


def _goal_line(g: dict, today: date, days: int) -> str:
    dist = _n(g.get("distance_km"), " km")
    bits = [f"A-race: on {g['race_date'].isoformat()} "
            f"({days} days / ~{max(days, 0) // 7} weeks away)"]
    if dist:
        bits.append(f" - {dist}")
    if g.get("elevation_gain_m"):
        bits.append(f", about {_n(g['elevation_gain_m'], ' m')} of climbing")
    if g.get("target_time"):
        bits.append(f", target {g['target_time']}")
    bits.append(". The race FORMAT has not been set, so you do not have format-specific doctrine for it.")
    return "".join(bits)


RACE_DEMANDS = """WHAT THIS RACE DEMANDS:
- You have not been told what kind of race this is, so do not assume one. Do not import backyard, trail, \
marathon or track doctrine by default - the demands of those formats contradict each other, and applying the \
wrong one confidently is worse than admitting the gap.
- ASK the athlete what the race actually is: distance, terrain, climbing, expected duration, conditions, \
cutoffs, whether it is paced to a target time or run to survive. Ask once, plainly, and then coach from the \
answer.
- Until then, reason from what the DATA supports: their current aerobic fitness, chronic load, durability \
markers and stated constraints. General endurance principle is sound; invented race specifics are not.
- Say plainly what you do not know rather than papering over it. An athlete told "I need to know the terrain \
before I can plan your long runs properly" trusts the answer more than one given a confident plan for the \
wrong race."""

TRAINING_ADDENDA = """WITHOUT A KNOWN FORMAT:
- Build the aerobic base, keep easy days easy, ramp conservatively, and protect consistency. Those hold for \
every endurance race and are safe to prescribe now.
- Hold off on the format-defining sessions - race-pace blocks, vertical progression, loop simulations, \
back-to-backs - until the format is known. Those are where the formats genuinely diverge, and prescribing the \
wrong one wastes a block.
- Taper on general principle: reduce volume over the final 1-2 weeks, keep frequency and a light touch of \
intensity, arrive fresh."""

EXECUTION = """RACE-DAY EXECUTION:
- You do not know the format, so give only what is universally true: start conservatively, fuel and drink on \
a rehearsed schedule rather than by feel, adjust expectations for conditions before the start rather than \
mid-race, and use nothing on race day that has not been used in training.
- For anything more specific - pacing structure, walk breaks, aid strategy, night plans - ask what the race \
is first."""

SPEC = Format(
    key="generic",
    label="endurance race (format not set)",
    goal_fields=("distance_km", "elevation_gain_m", "target_time"),
    phases="base -> build -> race-specific -> taper",
    goal_line=_goal_line,
    race_demands=RACE_DEMANDS,
    training_addenda=TRAINING_ADDENDA,
    execution=EXECUTION,
    compact=("the race format has NOT been set. Coach sound general endurance, ask what the race actually is "
             "(distance, terrain, climbing, duration, target), and don't assume a format - applying the wrong "
             "one confidently is worse than naming the gap."),
)
