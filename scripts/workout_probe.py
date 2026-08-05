"""Step-0 probe for Garmin workout push (docs/garmin-workout-push.md).

Creates ONE throwaway structured workout ("J2H4All: probe (delete me)") exercising
everything v1 needs — warmup w/ HR-zone target, a repeat block with pace-target
work + recovery, cooldown — schedules it for tomorrow, reads both back, then
deletes the schedule and the workout. Leaves the account exactly as found.

Run from home (residential IP) with CWD=backend, like home_sync:
    $env:DATABASE_URL = <NEON_DATABASE_URL>; .venv\\Scripts\\python.exe ..\\scripts\\workout_probe.py
It uses the Neon-persisted rotating OAuth2 token (same as prod sync), so token
rotation stays on the one chain prod reads.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import SessionLocal  # noqa: E402
from app.garmin.client import GarminClient  # noqa: E402

SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery"},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat"},
}
END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
END_DIST = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
END_ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
TARGET_HR_ZONE = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}


def exe_step(order, kind, end, end_value, target=None, one=None, two=None, zone=None, child=None):
    s = {
        "type": "ExecutableStepDTO", "stepId": None, "stepOrder": order,
        "stepType": STEP_TYPES[kind], "childStepId": child,
        "endCondition": end, "endConditionValue": end_value,
        "targetType": target or {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
        "targetValueOne": one, "targetValueTwo": two, "zoneNumber": zone,
    }
    return s


PAYLOAD = {
    "workoutName": "J2H4All: probe (delete me)",
    "description": "Throwaway probe from J2H4All workout-push step 0. Safe to delete.",
    "sportType": SPORT,
    "workoutSegments": [{
        "segmentOrder": 1,
        "sportType": SPORT,
        "workoutSteps": [
            # 10 min warmup @ HR zone 1
            exe_step(1, "warmup", END_TIME, 600.0, TARGET_HR_ZONE, zone=1),
            # 2 × (800 m @ 4:30–4:50/km + 2 min recovery)
            {
                "type": "RepeatGroupDTO", "stepId": None, "stepOrder": 2,
                "stepType": STEP_TYPES["repeat"], "childStepId": 1,
                "numberOfIterations": 2, "smartRepeat": False,
                "endCondition": END_ITER, "endConditionValue": 2.0,
                "workoutSteps": [
                    # pace targets are speeds in m/s: 4:50/km → 3.448, 4:30/km → 3.704
                    exe_step(3, "interval", END_DIST, 800.0, TARGET_PACE,
                             one=round(1000 / 290, 3), two=round(1000 / 270, 3), child=1),
                    exe_step(4, "recovery", END_TIME, 120.0, child=1),
                ],
            },
            # 10 min cooldown, no target
            exe_step(5, "cooldown", END_TIME, 600.0),
        ],
    }],
}


def main() -> int:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    db = SessionLocal()
    try:
        gc = GarminClient(db=db)
        api = gc._client.connectapi  # probe only; the app will get a proper write method

        print("1) create workout ...")
        created = api("/workout-service/workout", method="POST", json=PAYLOAD)
        wid = created["workoutId"]
        print(f"   workoutId={wid}")

        print("2) read back ...")
        got = api(f"/workout-service/workout/{wid}")
        steps = got["workoutSegments"][0]["workoutSteps"]
        print(f"   name={got['workoutName']!r} steps={len(steps)} "
              f"(types: {[s.get('stepType', {}).get('stepTypeKey') for s in steps]})")
        rep = next(s for s in steps if s["stepType"]["stepTypeKey"] == "repeat")
        work = rep["workoutSteps"][0]
        print(f"   repeat x{rep.get('numberOfIterations')} — work target "
              f"{work.get('targetType', {}).get('workoutTargetTypeKey')} "
              f"{work.get('targetValueOne')}–{work.get('targetValueTwo')} m/s")

        print(f"3) schedule for {tomorrow} ...")
        sched = api(f"/workout-service/schedule/{wid}", method="POST", json={"date": tomorrow})
        sched_id = sched.get("workoutScheduleId") or sched.get("id")
        print(f"   scheduleId={sched_id} raw={json.dumps(sched)[:200]}")

        print("4) delete schedule ...")
        api(f"/workout-service/schedule/{sched_id}", method="DELETE")
        print("   deleted")

        print("5) delete workout ...")
        api(f"/workout-service/workout/{wid}", method="DELETE")
        print("   deleted")

        print("PROBE PASS — payload shape, write scope, schedule + delete all confirmed")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
