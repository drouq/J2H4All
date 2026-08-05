"""PDF blood-report parsing: PDF upload → Sonnet parses markers via a
document content block → returns proposed blood_marker items for the same
confirm flow as chat capture. Manual chat entry is the fallback.

The medical line is enforced in the prompt: extract values only, never
interpret or advise here — coaching interpretation happens elsewhere, and always
defers medical judgment to a clinician.
"""

import base64

from ..llm import call_tool

PARSE_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "report_date": {"type": ["string", "null"], "description": "ISO date of the blood draw if present on the report"},
        "markers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "description": "canonical lowercase name, e.g. ferritin, hemoglobin, vitamin d, b12"},
                    "value": {"type": "number"},
                    "unit": {"type": ["string", "null"]},
                },
                "required": ["name", "value", "unit"],
            },
        },
    },
    "required": ["report_date", "markers"],
}

_SYSTEM = (
    "You extract blood-test marker values from a lab report PDF for a running-coach app. "
    "Return every quantitative marker you can read: name (canonical lowercase — ferritin, hemoglobin, "
    "vitamin d, b12, transferrin saturation, etc.), numeric value, and unit. Use the report's own "
    "collection/draw date as report_date if present. Do NOT interpret results, flag ranges, or give "
    "any medical advice — extract the raw values only. If the PDF is not a lab report or is unreadable, "
    "return an empty markers list."
)


def parse_blood_pdf(pdf_bytes: bytes) -> dict:
    """Returns {report_date, markers:[{name,value,unit}]}."""
    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
        {"type": "text", "text": "Extract all blood-test markers from this report."},
    ]
    return call_tool(
        task="pdf_blood_parse",
        system=_SYSTEM,
        content=content,
        tool_name="record_blood_markers",
        tool_schema=PARSE_TOOL_SCHEMA,
    )


def markers_to_items(parsed: dict) -> list[dict]:
    """Convert parsed markers into the shared context-item shape for confirm."""
    report_date = parsed.get("report_date")
    items = []
    for m in parsed.get("markers", []):
        unit = f" {m['unit']}" if m.get("unit") else ""
        date_txt = f" ({report_date})" if report_date else ""
        items.append({
            "kind": "blood_marker",
            "summary": f"{m['name']}: {m['value']}{unit}{date_txt}",
            "marker_name": m.get("name"),
            "value": m.get("value"),
            "unit": m.get("unit"),
            "measured_on": report_date,
            "window_type": None, "start_date": None, "end_date": None,
            "body_part": None, "status": None, "key": None, "text": None, "timezone": None,
        })
    return items
