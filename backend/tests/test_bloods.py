"""Blood-marker reference-range flagging (conservative, unit-matched, non-diagnostic)
and its surfacing in the context snapshot."""
from datetime import date, timedelta

from app.context import bloods, store
from app.models import BloodMarker, LifestyleLog
from app.util import utcnow


def test_flag_low_high_inrange():
    assert bloods.flag_marker("ferritin", 25, "ng/mL") == "low"
    assert bloods.flag_marker("ferritin", 500, "ng/mL") == "high"
    assert bloods.flag_marker("ferritin", 120, "ng/mL") is None


def test_unit_mismatch_does_not_flag():
    # Can't compare ng/mL ranges against a µg/L reading — refuse rather than guess.
    assert bloods.flag_marker("ferritin", 25, "ug/L") is None


def test_unknown_marker_does_not_flag():
    assert bloods.flag_marker("mystery-analyte", 5, "x") is None
    assert bloods.flag_marker("ferritin", None, "ng/mL") is None


def test_name_aliases_and_single_sided_bounds():
    assert bloods.flag_marker("Vitamin B12", 150, "pg/mL") == "low"
    assert bloods.flag_marker("25-OH Vitamin D", 20, "ng/mL") == "low"
    assert bloods.flag_marker("HDL cholesterol", 30, "mg/dL") == "low"    # low-only bound
    assert bloods.flag_marker("LDL", 200, "mg/dL") == "high"              # high-only bound
    assert bloods.flag_marker("HDL", 200, "mg/dL") is None                # no high bound


def test_reference_display_strings():
    assert bloods.marker_reference("ferritin", "ng/mL") == "30–400 ng/mL"
    assert bloods.marker_reference("ldl", "mg/dL") == "<130 mg/dL"
    assert bloods.marker_reference("hdl", "mg/dL") == ">40 mg/dL"
    assert bloods.marker_reference("mystery", "x") is None


def test_snapshot_carries_flags_and_recent_lifestyle(db):
    db.add(BloodMarker(name="ferritin", value=22, unit="ng/mL",
                       measured_on=date(2026, 7, 1), created_at=utcnow()))
    # Relative to today — the snapshot view only reaches back 14 days, so a pinned
    # date silently rots out of the window and fails the day it ages past it.
    db.add(LifestyleLog(date=utcnow().date() - timedelta(days=1), raw_text="2 beers",
                        data={"alcohol": "2 beers", "summary": "beers"},
                        created_at=utcnow(), updated_at=utcnow()))
    db.commit()

    snap = store.snapshot(db)
    marker = next(m for m in snap["blood_markers"] if m["name"] == "ferritin")
    assert marker["flag"] == "low" and marker["reference"] == "30–400 ng/mL"
    assert snap["recent_lifestyle"][0]["flags"]["alcohol"] == "2 beers"
