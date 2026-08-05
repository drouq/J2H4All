"""Blood-marker reference-range flagging (stays intact: this FLAGS, never
diagnoses). Typical adult-male population ranges — NOT lab-specific — used only to mark
a value outside the usual band so the coach can raise it and suggest a doctor. Curated
for the athlete's relevant panel (iron status, B12/D, a few common markers).

Deliberately conservative: an unknown marker, a missing value, OR a unit that doesn't
match the range's canonical unit → NO flag (a wrong flag is worse than none).
"""

import re

# canonical_name -> (canonical_unit, low_or_None, high_or_None). A None bound means only
# the other side is clinically meaningful (e.g. LDL flags high, HDL flags low).
_RANGES: dict[str, tuple[str, float | None, float | None]] = {
    "ferritin": ("ng/ml", 30, 400),
    "hemoglobin": ("g/dl", 13.5, 17.5),
    "hematocrit": ("%", 38.8, 50.0),
    "iron": ("ug/dl", 65, 175),
    "transferrin_saturation": ("%", 20, 50),
    "vitamin_b12": ("pg/ml", 200, 900),
    "vitamin_d": ("ng/ml", 30, 100),
    "folate": ("ng/ml", 3.0, None),
    "tsh": ("miu/l", 0.4, 4.0),
    "total_cholesterol": ("mg/dl", None, 200),
    "ldl": ("mg/dl", None, 130),
    "hdl": ("mg/dl", 40, None),
    "triglycerides": ("mg/dl", None, 150),
    "glucose": ("mg/dl", 70, 100),
    "hba1c": ("%", None, 5.7),
    "creatinine": ("mg/dl", 0.7, 1.3),
    "crp": ("mg/l", None, 3.0),
}

_NAME_ALIASES: dict[str, str] = {
    "haemoglobin": "hemoglobin", "hgb": "hemoglobin", "hb": "hemoglobin",
    "hct": "hematocrit",
    "serum iron": "iron",
    "transferrin saturation": "transferrin_saturation", "transferrin sat": "transferrin_saturation",
    "tsat": "transferrin_saturation", "tf sat": "transferrin_saturation",
    "b12": "vitamin_b12", "vitamin b12": "vitamin_b12", "cobalamin": "vitamin_b12",
    "vitamin d": "vitamin_d", "vitamin d3": "vitamin_d", "vit d": "vitamin_d",
    "25 oh vitamin d": "vitamin_d", "25-oh vitamin d": "vitamin_d", "25(oh)d": "vitamin_d",
    "folic acid": "folate",
    "cholesterol": "total_cholesterol", "total cholesterol": "total_cholesterol",
    "ldl cholesterol": "ldl", "ldl c": "ldl",
    "hdl cholesterol": "hdl", "hdl c": "hdl",
    "trigs": "triglycerides",
    "blood glucose": "glucose", "fasting glucose": "glucose",
    "a1c": "hba1c", "hemoglobin a1c": "hba1c",
    "c reactive protein": "crp", "hs crp": "crp", "hscrp": "crp",
}


def _norm_name(name: str | None) -> str:
    return re.sub(r"[\s\-()]+", " ", (name or "").strip().lower()).strip()


def _norm_unit(unit: str | None) -> str:
    return (unit or "").strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")


def _canonical(name: str | None) -> str | None:
    n = _norm_name(name)
    if n in _NAME_ALIASES:
        return _NAME_ALIASES[n]
    key = n.replace(" ", "_")
    return key if key in _RANGES else None


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def flag_marker(name: str | None, value: float | None, unit: str | None) -> str | None:
    """'low' | 'high' | None (in-range, or unknown marker / unit mismatch)."""
    key = _canonical(name)
    if key is None or value is None:
        return None
    cu, lo, hi = _RANGES[key]
    if _norm_unit(unit) != cu:  # can't compare across units without guessing — don't
        return None
    if lo is not None and value < lo:
        return "low"
    if hi is not None and value > hi:
        return "high"
    return None


def marker_reference(name: str | None, unit: str | None) -> str | None:
    """Human-readable reference band for display (using the reading's own unit), or None
    when the marker/unit isn't recognised."""
    key = _canonical(name)
    if key is None:
        return None
    cu, lo, hi = _RANGES[key]
    if _norm_unit(unit) != cu:
        return None
    u = f" {unit}" if unit else ""
    if lo is not None and hi is not None:
        return f"{_fmt(lo)}–{_fmt(hi)}{u}"
    if hi is not None:
        return f"<{_fmt(hi)}{u}"
    if lo is not None:
        return f">{_fmt(lo)}{u}"
    return None
