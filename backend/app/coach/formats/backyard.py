"""Backyard ultra: a fixed loop started on the hour, every hour, until one runner
is left. This is the format the app was originally built for, and the only one
that has been exercised against a real athlete's season and real prompt evals."""

from __future__ import annotations

from datetime import date

from . import Format, _n


def _goal_line(g: dict, today: date, days: int) -> str:
    loop = _n(g.get("loop_km"), " km")
    laps = g.get("target_laps")
    bits = [f"A-race: a BACKYARD ULTRA on {g['race_date'].isoformat()} "
            f"({days} days / ~{max(days, 0) // 7} weeks away)"]
    if loop:
        bits.append(f" - a {loop} loop started on the hour, every hour")
    if laps:
        bits.append(f", target {laps} laps (~{laps}h)")
    bits.append(". Every lap must be finished inside the hour; whatever time is left "
                "over is the only rest there is.")
    return "".join(bits)


RACE_DEMANDS = """WHAT THIS RACE DEMANDS (reason from these, not from generic ultra doctrine):
- A backyard is won by whoever degrades slowest. The outcome is decided by durability - holding an easy, \
repeatable effort hour after hour - not by speed. Pace and VO2max matter only insofar as they make the loop \
pace a smaller fraction of their capacity.
- The hourly reset IS the format: consistency beats banked time. Running a lap faster buys rest but spends \
the legs; the target is the slowest comfortable lap that still leaves a workable reset window, repeated \
identically, including the walk/run split inside the loop.
- Hourly fueling is the make-or-break variable: one forced feeding opportunity per lap, and gut failure - not \
the legs - ends most backyard races. Fueling tolerance is trainable and must be trained.
- A long attempt runs through the night on a sleep-deprived brain. Night running, light, caffeine timing and \
morale routines are part of the race, not an afterthought.
- The loop is runnable and repetitive: the mechanical challenge is an unchanged gait for many hours, so the \
risks are repetitive-strain and soft-tissue breakdown; cadence economy and tissue durability matter far more \
than climbing strength. This is NOT a mountain race - vertical gain is not a training priority.
- The finish is social, not distance-based: it ends when everyone else stops. Plan for the possibility of \
being alone out front, and for the possibility of a duel that runs hours past the target."""

TRAINING_ADDENDA = """BACKYARD-SPECIFIC TRAINING:
- Backyard simulation is the signature session: repeated loops run ON THE CLOCK - e.g. 3-6 x ~40-50 min \
"laps" started each hour - rehearsing the full hourly routine of pace discipline, stop, fuel, sit, restart. \
These are dress rehearsals for pacing, fueling, kit and mind, not extra volume.
- Back-to-back long runs (a moderate long run on consecutive days) build fatigue-resistance at lower injury \
risk than one monster run - the second run teaches running on tired legs, this format's core skill.
- Walking is a race skill: deliberate brisk walk segments inside long runs. The race-day walk/run split is \
rehearsed in training, never improvised.
- Sparing sleep-adjacent exposure late in the build (a pre-dawn start, or a late-evening long run with a \
headlamp) to rehearse running tired - never at the cost of systematic recovery.
- The taper trims volume hard (roughly halving over the final 2 weeks) while keeping frequency and small \
touches of intensity. The goals are full glycogen, a rebuilt sleep bank, and zero staleness."""

EXECUTION = """RACE-DAY EXECUTION (when strategy comes up):
- Lap pacing: settle immediately into the slowest lap that leaves a comfortable reset, identical effort every \
lap; going faster early is spending, not banking. The walk/run split repeats from lap 1.
- The hourly routine is scripted and sacred: finish -> fuel FIRST -> then sit (feet up; don't lie down in the \
early hours), kit for the next lap sorted before resting; everything laid out in the pit, decisions pre-made.
- Fueling: whatever training showed they tolerate (typically progressing toward 60-90 g carbs/hour, with \
fluids and sodium scaled to the conditions); more real food in the early hours, simpler sugars as the race \
accumulates; a missed feed is a red flag to correct at the very next reset, not later.
- Night plan: hold caffeine in reserve until genuinely needed (typically from evening), then dose to effect; \
warm layer and headlamp staged before dark; expect pace-for-effort to drift at night and protect the reset \
window rather than chase earlier lap times.
- Sleep: on a target beyond ~18 hours the default is pushing through on caffeine, light and pit routine; \
micro-naps only if lap margin genuinely allows them.
- Feet & chafe: hotspots pre-taped, lube renewed at resets, dry socks and a shoe change staged mid-race.
- Crew: if the athlete races crewed, script the crew like kit - feeds staged per reset, next-lap kit laid \
out, explicit per-lap instructions, and crew coverage through the ENTIRE night confirmed well in advance. \
Crew going home mid-race is a classic and entirely preventable cause of a backyard ending. If they race \
uncrewed, the pit has to be self-service and pre-staged to the same standard, and that changes the reset \
budget - plan for it explicitly.
- The evening transition (roughly 20:00-midnight) is the classic danger window: accumulated fatigue, \
under-eating, the pull of bed, and co-runners quitting on the same lap all land together. Pre-commit the \
rules ("quitting decisions were made at the start line, not at 9pm"), rehearse night laps in training, and \
brief the crew to carry them through that window specifically. If the athlete's notes record where a PREVIOUS \
attempt ended and why, treat that as the highest-value fact you have and train directly against it.
- Psychology: the race is only ever "one more lap". Anchor the athlete to the process and the floor goal, \
never to the total distance remaining. Social quitting is contagious - they run THEIR race when others drop."""

SPEC = Format(
    key="backyard-ultra",
    label="backyard ultra",
    goal_fields=("loop_km", "target_laps"),
    phases="base -> build -> backyard-specific (loop simulation) -> taper",
    goal_line=_goal_line,
    race_demands=RACE_DEMANDS,
    training_addenda=TRAINING_ADDENDA,
    execution=EXECUTION,
    compact=("a fixed loop on the hour, every hour, until one runner is left - won by degrading slowest. "
             "Durability (easy repeatable effort, low HR:pace drift, metronomic pace), the hourly fueling "
             "reset, and sleep-deprived night laps decide it; speed does not."),
)
