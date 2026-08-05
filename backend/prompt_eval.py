"""Read-only prompt-eval harness: generate the coach's key outputs (morning brief,
weekly review, coaching-chat answers, post-run read) against the CURRENT database
state, WITHOUT persisting anything, creating proposals, or sending Telegram
messages. Used to compare prompt revisions side by side before deploying them.

Usage (CWD must be backend/ so the `app` package and .env resolve):

  $env:DATABASE_URL = <neon url>   # optional: eval against prod state
  .venv/Scripts/python.exe prompt_eval.py --label baseline --outdir eval_out

Writes markdown files to <outdir>/<label>/. Run once before a prompt change and
once after, then diff the two directories. LLM calls are non-deterministic, so
judge substance (what the coach chooses to say) rather than exact wording.

Works both before and after the doctrine refactor: each surface's system prompt
is taken from a `system_prompt(db)` builder when the module has one, else from
the legacy module constant.
"""

import argparse
import json
from datetime import date
from pathlib import Path

CHAT_QUESTIONS = [
    "Looking at where I am right now, what should my long runs look like over the "
    "next month, and when do we start backyard-specific work?",
    "How will we train my gut and fueling between now and October, and what does "
    "race-day fueling look like hour by hour?",
    "Give me your current race-day strategy: lap pacing, walk/run, fueling, and how "
    "we handle the night.",
]


def _system_of(mod, db, builder: str = "system_prompt", const: str = "_SYSTEM") -> str:
    fn = getattr(mod, builder, None)
    if callable(fn):
        return fn(db)
    return getattr(mod, const)


def _chat_answer(system: str, context_block: str, question: str) -> str:
    from app.config import get_settings
    from app.llm import get_client

    client = get_client()
    model = get_settings().model_for("coach_chat")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Current athlete state (JSON, for your reasoning):\n" + context_block},
            {"type": "text", "text": question},
        ],
    }]
    resp = client.messages.create(
        model=model, max_tokens=1500, thinking={"type": "disabled"},
        system=system, messages=messages,
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def eval_brief(db, today, outdir: Path):
    from app.coach import brief

    text = brief.build_brief(db, today)  # pure: no Telegram send (that's send_brief)
    (outdir / "brief.md").write_text(f"# Morning brief\n\n{text}\n", encoding="utf-8")
    print("  brief: done")


def eval_weekly(db, today, outdir: Path):
    """run_review's exact fact assembly (weekly.build_facts) + LLM call, but
    creates NO proposal."""
    from app.coach import weekly
    from app.llm import call_tool

    facts = weekly.build_facts(db, today)
    if facts is None:
        print("  weekly: SKIPPED (no active macro plan)")
        return
    out = call_tool(
        task="weekly_review", system=_system_of(weekly, db),
        content="Full signal bundle + current plan (JSON). Run the weekly review.\n\n"
                + json.dumps(facts, default=str),
        tool_name="record_weekly_review", tool_schema=weekly.REVIEW_SCHEMA,
        tool_description="Record the weekly review + proposed next-block sessions.",
        max_tokens=12000,
    )
    lines = [
        "# Weekly review (dry run — no proposal created)", "",
        "## Summary", "", out.get("summary", ""), "",
        "## Change note", "", out.get("change_note", ""), "",
        "## Sessions", "",
    ]
    for s in out.get("sessions", []):
        lines.append(
            f"- **{s.get('date')}** {s.get('type')} — {s.get('title')} "
            f"({s.get('duration_min')}min / {s.get('distance_km')}km, "
            f"{s.get('target_zone')} {s.get('target_pace') or ''})"
        )
        lines.append(f"  - why: {s.get('purpose')}")
        if s.get("fueling_note"):
            lines.append(f"  - fueling: {s.get('fueling_note')}")
    (outdir / "weekly_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  weekly: done ({len(out.get('sessions', []))} sessions)")


def eval_chat(db, today, outdir: Path):
    from app.coach import chat

    system = _system_of(chat, db, const="SYSTEM")
    ctx = chat._context_block(db, today)
    lines = ["# Coaching chat (nothing persisted)", ""]
    for i, q in enumerate(CHAT_QUESTIONS, 1):
        answer = _chat_answer(system, ctx, q)
        lines += [f"## Q{i}: {q}", "", answer, ""]
        print(f"  chat Q{i}: done")
    (outdir / "chat.md").write_text("\n".join(lines), encoding="utf-8")


def eval_postrun(db, today, outdir: Path):
    """Re-reads the most recent planned-vs-actual result without writing anything."""
    from sqlalchemy import select

    from app.coach import postrun
    from app.coach.signals import _feel_label, _rpe_label, recovery_baseline
    from app.llm import call_tool
    from app.models import Activity, Session, SessionResult

    r = db.scalar(
        select(SessionResult).where(SessionResult.session_id.isnot(None))
        .order_by(SessionResult.id.desc()).limit(1)
    )
    if r is None:
        print("  postrun: SKIPPED (no linked session result)")
        return
    session = db.get(Session, r.session_id)
    act = db.get(Activity, r.activity_id) if r.activity_id else None
    facts = {
        "self_eval": {
            "feel": _feel_label(act.feel) if act else None,
            "rpe": _rpe_label(act.rpe) if act else None,
        },
        "planned": {
            "date": session.date.isoformat(), "type": session.type, "title": session.title,
            "target_zone": session.target_zone, "target_pace": session.target_pace,
            "distance_km": session.distance_km, "duration_min": session.duration_min,
            "purpose": session.purpose,
        },
        "actual": {
            "distance_km": r.actual_distance_km, "duration_min": r.actual_duration_min,
            "avg_hr": r.actual_avg_hr,
        },
        "durability": act.stream_metrics if act else None,
        "recovery_context": recovery_baseline(db, today),
    }
    out = call_tool(
        task="post_run_read", system=_system_of(postrun, db),
        content="Read this session (JSON):\n" + json.dumps(facts, default=str),
        tool_name="record_read", tool_schema=postrun.READ_SCHEMA,
        tool_description="Record the planned-vs-actual read.",
    )
    body = (
        f"# Post-run read (dry run — session {session.date.isoformat()} \"{session.title}\")\n\n"
        f"{out.get('read', '')}\n\nflagged: {out.get('flagged')}"
        + (f" — {out.get('flag_reason')}" if out.get("flag_reason") else "") + "\n"
    )
    (outdir / "postrun.md").write_text(body, encoding="utf-8")
    print("  postrun: done")


def main():
    ap = argparse.ArgumentParser(description="Read-only coach prompt eval")
    ap.add_argument("--label", required=True, help="e.g. baseline / doctrine-v1")
    ap.add_argument("--outdir", default="eval_out")
    args = ap.parse_args()

    outdir = Path(args.outdir) / args.label
    outdir.mkdir(parents=True, exist_ok=True)

    from app.config import get_settings
    from app.db import SessionLocal

    settings = get_settings()
    meta = {
        "label": args.label,
        "date": date.today().isoformat(),
        "models": {t: settings.model_for(t) for t in
                   ("morning_brief", "weekly_review", "coach_chat", "post_run_read")},
    }
    print(f"prompt_eval → {outdir}  (models: {meta['models']})")

    today = date.today()
    db = SessionLocal()
    try:
        eval_brief(db, today, outdir)
        eval_weekly(db, today, outdir)
        eval_chat(db, today, outdir)
        eval_postrun(db, today, outdir)
    finally:
        db.rollback()  # belt-and-braces: this harness must never persist anything
        db.close()
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("all done")


if __name__ == "__main__":
    main()
