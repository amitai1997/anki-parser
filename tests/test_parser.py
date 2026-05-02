"""Tests for the anki-parser: unit tests for bidi/HTML helpers + end-to-end
assertions on the example PDF (skipped when the PDF isn't available locally).
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anki_export import build_apkg
from parser import (
    Span,
    _fix_bidi_parens,
    _format_explanation_html,
    _normalize_html,
    _spans_to_html,
    parse_pdf,
)


# ---------------------------------------------------------------------------
# Unit tests: _fix_bidi_parens
# ---------------------------------------------------------------------------

class TestFixBidiParens:
    """_fix_bidi_parens wraps ASCII-only parenthesised runs so they render
    correctly inside RTL (Hebrew) paragraphs in Anki."""

    # ---- wrapping happens ----

    def test_pure_latin_parens(self):
        assert _fix_bidi_parens("(EPSP)") == '<span dir="ltr">(EPSP)</span>'

    def test_latin_with_plus(self):
        # Real-world: (Na+)
        assert _fix_bidi_parens("(Na+)") == '<span dir="ltr">(Na+)</span>'

    def test_latin_acronym(self):
        assert _fix_bidi_parens("(NMDA)") == '<span dir="ltr">(NMDA)</span>'

    def test_digit_only(self):
        assert _fix_bidi_parens("(123)") == '<span dir="ltr">(123)</span>'

    def test_mixed_latin_digits(self):
        assert _fix_bidi_parens("(mGluA1)") == '<span dir="ltr">(mGluA1)</span>'

    def test_multiple_parens_in_one_string(self):
        result = _fix_bidi_parens("מה ש(EPSP) ו(NMDA)")
        assert result == 'מה ש<span dir="ltr">(EPSP)</span> ו<span dir="ltr">(NMDA)</span>'

    def test_parens_adjacent_to_hebrew(self):
        # Exact pattern from the screenshot: (Na+) לנוירון
        result = _fix_bidi_parens("(Na+) לנוירון")
        assert result == '<span dir="ltr">(Na+)</span> לנוירון'

    def test_parens_with_spaces_inside(self):
        result = _fix_bidi_parens("(EPSPs and IPSPs)")
        assert result == '<span dir="ltr">(EPSPs and IPSPs)</span>'

    def test_comma_separated_acronyms(self):
        result = _fix_bidi_parens("(EPSP, IPSP)")
        assert result == '<span dir="ltr">(EPSP, IPSP)</span>'

    # ---- mixed Hebrew+Latin → isolate wrap ----

    def test_mixed_hebrew_latin_isolated(self):
        # Real-world case from the user's screenshot: bidi was reordering the
        # closing paren and period to the wrong side.
        result = _fix_bidi_parens("(NMDA כן יכול)")
        assert result == (
            '<span dir="rtl" style="unicode-bidi:isolate">'
            '(NMDA כן יכול)</span>'
        )

    def test_mixed_hebrew_latin_long(self):
        result = _fix_bidi_parens("(NMDA כן יכול להעביר סידן)")
        assert 'dir="rtl"' in result
        assert "unicode-bidi:isolate" in result
        assert "(NMDA כן יכול להעביר סידן)" in result

    # ---- pure Hebrew → unchanged ----

    def test_pure_hebrew_parens_untouched(self):
        s = "(קולטן גלוטמט)"
        assert _fix_bidi_parens(s) == s

    def test_empty_parens_untouched(self):
        # Walker requires non-empty stripped content to wrap.
        assert _fix_bidi_parens("()") == "()"

    # ---- HTML tags are not touched ----

    def test_parens_inside_html_tag_untouched(self):
        # CSS values like rgb() inside a style attribute must not be wrapped.
        s = '<span style="color:rgb(255,0,0)">text</span>'
        assert _fix_bidi_parens(s) == s

    def test_parens_inside_href_untouched(self):
        s = '<a href="foo(bar)">link</a>'
        assert _fix_bidi_parens(s) == s

    def test_text_parens_but_not_tag_parens(self):
        # Tag attr left alone; text paren wrapped.
        s = '<b style="x(y)">text (EPSP) end</b>'
        result = _fix_bidi_parens(s)
        assert 'x(y)' in result                          # attr unchanged
        assert '<span dir="ltr">(EPSP)</span>' in result  # text fixed

    def test_html_preserved_around_parens(self):
        s = "<b>מה שגורם</b> (EPSP) <b>לדה</b>"
        result = _fix_bidi_parens(s)
        assert result == '<b>מה שגורם</b> <span dir="ltr">(EPSP)</span> <b>לדה</b>'

    # ---- idempotency ----

    def test_already_wrapped_not_double_wrapped(self):
        # After wrapping, the span tag contains `<` so the inner run is inside
        # a tag — a second pass must not wrap again.
        once = _fix_bidi_parens("(EPSP)")
        twice = _fix_bidi_parens(once)
        assert once == twice


# ---------------------------------------------------------------------------
# Integration: _normalize_html applies the fix
# ---------------------------------------------------------------------------

class TestNormalizeHtmlBidi:
    def test_normalize_applies_bidi_fix(self):
        result = _normalize_html("לדה-פולריזציה (EPSP) בנוירון")
        assert '<span dir="ltr">(EPSP)</span>' in result

    def test_normalize_does_not_touch_hebrew_parens(self):
        s = "(קולטן)"
        assert _fix_bidi_parens(s) == s


# ---------------------------------------------------------------------------
# Integration: _format_explanation_html applies the fix
# ---------------------------------------------------------------------------

class TestFormatExplanationBidi:
    def test_latin_parens_wrapped_in_explanation(self):
        html = "לנוירון הפוסט-סינפטי (EPSP) והם"
        result = _format_explanation_html(html)
        assert '<span dir="ltr">(EPSP)</span>' in result

    def test_mixed_line_partial_wrap(self):
        # (EPSP) → LTR wrap; (NMDA כן יכול) → RTL isolation wrap
        html = "פתוחים (EPSP) או (NMDA כן יכול) לסידן"
        result = _format_explanation_html(html)
        assert '<span dir="ltr">(EPSP)</span>' in result
        assert '<span dir="rtl" style="unicode-bidi:isolate">(NMDA כן יכול)</span>' in result


# ---------------------------------------------------------------------------
# _fix_bidi_parens: handles HTML tags inside parens
# ---------------------------------------------------------------------------

class TestFixBidiParensWithInnerHtml:
    """Regex-based wrapping wouldn't cross <b>…</b> tags inside parens; the
    walker-based implementation must."""

    def test_bold_latin_inside_parens(self):
        result = _fix_bidi_parens("(<b>EPSP</b>)")
        assert result == '<span dir="ltr">(<b>EPSP</b>)</span>'

    def test_colored_latin_inside_parens(self):
        result = _fix_bidi_parens('(<span style="color:#ff0000">EPSP</span>)')
        assert 'dir="ltr"' in result
        assert '<span style="color:#ff0000">EPSP</span>' in result

    def test_bold_in_mixed_content(self):
        result = _fix_bidi_parens("(<b>NMDA</b> כן יכול)")
        assert 'dir="rtl"' in result
        assert "unicode-bidi:isolate" in result
        assert "<b>NMDA</b>" in result

    def test_attribute_with_parens_not_wrapped(self):
        # rgb(255,0,0) inside a style attribute must not get wrapped.
        s = '<span style="color:rgb(255,0,0)">text</span>'
        assert _fix_bidi_parens(s) == s


# ---------------------------------------------------------------------------
# _spans_to_html: tight-punctuation hug rule
# ---------------------------------------------------------------------------

def _mk_span(text: str, start: int, end: int, y: float = 100.0, page: int = 0) -> Span:
    """Build a Span at a fixed y-coordinate (same visual line)."""
    return Span(
        text=text,
        bold=False,
        color=0,
        page=page,
        bbox=(0.0, y, 0.0, y + 10),
        start=start,
        end=end,
    )


class TestSpansToHtmlTightPunctuation:
    """PyMuPDF emits each font-run as its own internal "line" with a `\\n`
    between offsets, so adjacent runs like ``(`` + ``Na+`` + ``)`` show up as
    three spans separated by gap=1.  The hug rule must suppress the otherwise-
    inserted space when one side is tight punctuation."""

    def test_no_space_after_open_paren(self):
        spans = [
            _mk_span("(", 0, 1),
            _mk_span("Na+", 2, 5),  # gap=1 (PyMuPDF line boundary), same y
        ]
        assert _spans_to_html(spans, line_breaks=True) == "(Na+"

    def test_no_space_before_close_paren(self):
        spans = [
            _mk_span("Na+", 0, 3),
            _mk_span(")", 4, 5),
        ]
        assert _spans_to_html(spans, line_breaks=True) == "Na+)"

    def test_no_space_before_comma(self):
        spans = [
            _mk_span("הפוסט-סינפטי", 0, 12),
            _mk_span(",", 13, 14),
            _mk_span("מה", 15, 17),
        ]
        # Comma hugs the preceding word; space between , and מה is preserved.
        assert _spans_to_html(spans, line_breaks=True) == "הפוסט-סינפטי, מה"

    def test_no_space_before_period(self):
        spans = [
            _mk_span("text", 0, 4),
            _mk_span(".", 5, 6),
        ]
        assert _spans_to_html(spans, line_breaks=True) == "text."

    def test_full_paren_group(self):
        # End-to-end: hug rule produces "(Na+)", then _normalize_html →
        # _fix_bidi_parens wraps the ASCII content in dir="ltr".
        spans = [
            _mk_span("(", 0, 1),
            _mk_span("Na+", 2, 5),
            _mk_span(")", 6, 7),
        ]
        result = _spans_to_html(spans, line_breaks=True)
        assert result == '<span dir="ltr">(Na+)</span>'

    def test_normal_space_between_words_preserved(self):
        # Plain Hebrew word + space + Hebrew word: hug rule must NOT fire.
        spans = [
            _mk_span("שלום", 0, 4),
            _mk_span("עולם", 5, 9),
        ]
        assert _spans_to_html(spans, line_breaks=True) == "שלום עולם"

    def test_visual_line_break_still_breaks(self):
        # Different y coordinates → must produce <br>, not be suppressed.
        spans = [
            _mk_span("text", 0, 4, y=100.0),
            _mk_span(",", 5, 6, y=130.0),  # large y change
        ]
        assert _spans_to_html(spans, line_breaks=True) == "text<br>,"


# ---------------------------------------------------------------------------
# End-to-end: the screenshot example
# ---------------------------------------------------------------------------

class TestRealWorldExplanation:
    """The exact text from the user's screenshot, which previously rendered
    incorrectly in Anki due to bidi issues + spurious spaces."""

    def test_full_answer_text(self):
        # As it would appear after _spans_to_html — i.e. the parser's logical
        # output before bidi wrapping.
        html = (
            "(Na+) לנוירון הפוסט-סינפטי, מה שגורם לדה-פולריזציה "
            "(EPSP), והם לא פתוחים לסידן או כלור "
            "(NMDA כן יכול להעביר סידן)."
        )
        result = _fix_bidi_parens(html)
        # Pure-ASCII parens get LTR wrap.
        assert '<span dir="ltr">(Na+)</span>' in result
        assert '<span dir="ltr">(EPSP)</span>' in result
        # Mixed-content parens get RTL isolation wrap.
        assert (
            '<span dir="rtl" style="unicode-bidi:isolate">'
            '(NMDA כן יכול להעביר סידן)</span>'
        ) in result
        # Trailing period is outside any wrap.
        assert result.endswith("</span>.")

EXAMPLE_PDF = Path(
    "/Users/amitaisalmon/Downloads/"
    "________קובץ שחזורי מערכת העצבים של החולה - עדי צ'יק לנקרי (1).pdf"
)


@pytest.fixture(scope="module")
def parsed():
    if not EXAMPLE_PDF.exists():
        pytest.skip("example PDF not found")
    media = Path(tempfile.mkdtemp(prefix="ankiparser-test-"))
    return parse_pdf(EXAMPLE_PDF, media), media


def test_section_count(parsed):
    result, _ = parsed
    assert len(result.exam_tags) == 11, f"expected 11 sections, got {result.exam_tags}"


def test_card_count_in_range(parsed):
    result, _ = parsed
    # 10 exam sessions × ~40 questions each = ~400-450 cards
    assert 400 <= len(result.cards) <= 500, len(result.cards)


def test_first_card_2019_a_q1(parsed):
    result, _ = parsed
    cards_2019_a = [c for c in result.cards if c.exam_tag == "2019-moed-a"]
    assert cards_2019_a, "2019-moed-a missing"
    q1 = next(c for c in cards_2019_a if c.number == 1)
    assert "אפרנטים" in q1.question_html
    assert q1.options == ["GABA-A", "mGluA1", "AMPA", "EAAT1", "KAI"]
    assert q1.correct == 1


def test_image_attachment(parsed):
    result, _ = parsed
    cards_2019_a = [c for c in result.cards if c.exam_tag == "2019-moed-a"]
    by_num = {c.number: c for c in cards_2019_a}
    assert by_num[19].question_image, "Q19 of 2019-א should have a question image"
    assert by_num[26].question_image, "Q26 of 2019-א should have a question image"
    assert by_num[23].explanation_image, "A23 of 2019-א should have an explanation image"


def test_apkg_round_trip(parsed):
    result, media = parsed
    out = Path(tempfile.mkdtemp()) / "deck.apkg"
    data = build_apkg(result.cards, media, output_path=out)
    assert len(data) > 1000  # non-trivial
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert "collection.anki2" in names
    assert "media" in names
    media_files = [n for n in names if n.isdigit()]
    assert len(media_files) > 50  # 121 expected for the example PDF
