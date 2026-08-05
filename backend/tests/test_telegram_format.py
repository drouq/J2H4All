"""Markdown -> Telegram HTML conversion for coach messages. Pure function."""
from app.telegram import format_html


def test_bold_italic_code():
    assert format_html("**Is 100% possible?**") == "<b>Is 100% possible?</b>"
    assert format_html("aim for *high and stable*") == "aim for <i>high and stable</i>"
    assert format_html("`load_balance` signal") == "<code>load_balance</code> signal"


def test_headers_and_bullets():
    assert format_html("## Heat readiness") == "<b>Heat readiness</b>"
    assert format_html("- run at 5-6pm") == "• run at 5-6pm"
    # Numbered lists render fine as plain text — leave them.
    assert format_html("1. Consistency") == "1. Consistency"


def test_angle_brackets_and_amp_are_escaped():
    # Coach text like "HR <145" must not be read as an HTML tag.
    assert format_html("Keep HR <145 & steady") == "Keep HR &lt;145 &amp; steady"


def test_unbalanced_markdown_stays_literal():
    # A lone ** or * must never produce an unclosed tag (would 400 on Telegram).
    out = format_html("unbalanced **bold and a lone * star")
    assert "<b>" not in out and "<i>" not in out
    assert out == "unbalanced **bold and a lone * star"


def test_arithmetic_asterisk_not_italicised():
    assert format_html("3*4 = 12") == "3*4 = 12"
