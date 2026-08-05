"""Trail / mountain ultra: a point-to-point or loop course over distance and
vertical, usually with cutoffs, aid stations and a finish line that exists.

NOT validated against a real athlete's season - see the maturity note in
`formats/__init__.py`."""

from __future__ import annotations

from datetime import date

from . import Format, _n


def _goal_line(g: dict, today: date, days: int) -> str:
    dist = _n(g.get("distance_km"), " km")
    vert = _n(g.get("elevation_gain_m"), " m")
    bits = [f"A-race: a TRAIL / MOUNTAIN ULTRA on {g['race_date'].isoformat()} "
            f"({days} days / ~{max(days, 0) // 7} weeks away)"]
    if dist:
        bits.append(f" - {dist}")
        if vert:
            bits.append(f" with about {vert} of climbing")
    elif vert:
        bits.append(f" - about {vert} of climbing")
    if g.get("target_time"):
        bits.append(f", target {g['target_time']}")
    bits.append(".")
    if not dist and not vert:
        bits.append(" The distance and vertical aren't recorded yet - ask for them, because "
                    "the climbing profile changes the training more than the distance does.")
    return "".join(bits)


RACE_DEMANDS = """WHAT THIS RACE DEMANDS (reason from these, not from road-race doctrine):
- Vertical is the defining variable, not distance. A 50 km race with 3,000 m of climbing is a harder day than \
a flat 80 km, and it is a different day: climbing is a strength-endurance and cardiac problem, descending is \
an eccentric-loading and skill problem, and the two fail differently.
- DESCENDING is what ends most trail ultras, and it is the most under-trained skill in the sport. Quads that \
are not conditioned eccentrically shred on the first long descent, and everything after that is damage \
management. Descending must be trained deliberately, on terrain, at effort - not just accumulated by accident.
- Time-on-feet, not distance, is the honest unit. Hiking uphill is a technique, not a failure: on steep \
grades a strong power-hike is more economical than a shuffle-run and must be practised and paced.
- Cutoffs make this a race against a clock at checkpoints, not just to a finish. Aid-station discipline - \
knowing what happens at each one before arriving - is worth minutes that pacing cannot recover.
- Fueling and stomach management over many hours, on terrain that makes eating awkward and at altitudes or \
temperatures that suppress appetite. Gut failure is a leading cause of DNF.
- Terrain specificity matters: technical footing, roots, rock, and night sections on trail are skills. \
Course-specific reconnaissance (or the closest available terrain match) pays disproportionately.
- Kit is mandatory and non-trivial: required-gear lists, poles, headlamps, water carriage and weather layers \
all have to be rehearsed, not discovered on race day."""

TRAINING_ADDENDA = """TRAIL-SPECIFIC TRAINING:
- Vertical gain is a trained quantity with its own weekly progression, tracked alongside volume. Ramp it as \
conservatively as distance, and never spike both in the same week.
- Deliberate DESCENT training: controlled downhill repeats and long descents at race effort, introduced early \
and progressed gradually, because eccentric loading has the longest adaptation and the worst acute cost. \
Never bolt a big descent volume on late in the build.
- Power-hiking is a session, not a fallback: steep sustained climbs practised at race effort, with poles if \
poles will be used on the day.
- Back-to-back long runs on terrain (a moderate long day on consecutive days) build fatigue-resistance at \
lower injury risk than one monster outing, and rehearse running technical ground on tired legs.
- Long runs carry full race kit and race fueling from the build phase onward - pack weight, poles and bottle \
handling are skills that cost time when they're novel.
- Where the race has a night section, rehearse running by headlamp on real terrain: pace, footing confidence \
and depth perception all change, and discovering that at 2am in a race is expensive.
- The taper reduces volume substantially over the final 2 weeks while keeping some vertical and some \
intensity, so the legs stay used to climbing and descending without accumulating damage."""

EXECUTION = """RACE-DAY EXECUTION (when strategy comes up):
- Pace by EFFORT and by climb, never by average pace. The first climb feeling easy is the most common way to \
ruin a trail ultra; the correct early effort feels conservative to the point of frustration.
- Run the runnable, hike the steep, and set the changeover threshold in advance rather than negotiating it \
while tired. Descend under control early - banked minutes on descent one are paid back with interest.
- Aid stations: know before arriving what is being taken on, what is being swapped, and how long it should \
take. A written plan per station beats improvisation, and the clock at a checkpoint is unforgiving.
- Fueling: target a rehearsed hourly carbohydrate intake with fluids and sodium scaled to conditions, eating \
on climbs where it's easiest. A missed feed is corrected at the next opportunity, not written off.
- Manage the low patches: they are near-universal and usually chemical (under-fuelled, over-heated, \
under-slept) rather than terminal. The response is to eat, drink, cool or warm, walk, and reassess - not to \
decide about the race while in the hole.
- Feet: proactive care at a planned station - dry socks, lube, tape on hotspots before they become blisters. \
Foot damage is cumulative and irreversible within a race.
- Weather and kit: mountain conditions change fast; carry the mandatory kit properly and use it EARLY, \
particularly warm layers at night and at exposed high points."""

SPEC = Format(
    key="trail-ultra",
    label="trail / mountain ultra",
    goal_fields=("distance_km", "elevation_gain_m", "target_time"),
    phases="base -> build (vertical + descent) -> race-specific (terrain simulation) -> taper",
    goal_line=_goal_line,
    race_demands=RACE_DEMANDS,
    training_addenda=TRAINING_ADDENDA,
    execution=EXECUTION,
    compact=("a trail/mountain ultra decided by vertical, descending durability and fueling over many hours - "
             "not by flat speed. Quad conditioning for descents, power-hiking, time-on-feet and cutoff "
             "discipline are the variables; train vertical as deliberately as distance."),
)
