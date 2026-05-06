"""Parse Hebrew exam-recovery PDFs into Anki cards.

Strategy:
- Page 1 is parsed as a table of contents to locate exam sections and
  appendices with reliable page boundaries.
- For each section we build a single ``(plain_text, spans)`` stream with each
  span's offsets in the plain text. Question/answer markers are located via
  regex on the plain text, then sliced back to spans for HTML rendering.
- Images are extracted with their bboxes; each is attached to the latest Q/A
  marker that began above/before it in document reading order.
"""
from __future__ import annotations

import html as html_lib
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pymupdf

from card import Card, ParseResult


# ---------- Regexes ----------

QUESTION_RE = re.compile(r"שאלה\s*(\d+)\s*:")
# Answer header — appears like "6.\n \n תשובה4 \n,נכונה" in plain text. The comma
# is sometimes before נכונ due to the BiDi visual ordering preserved by pdf.
ANSWER_RE = re.compile(
    r"(?P<num>\d+)\s*\.\s*ת\s*ש\s*ו\s*ב\s*ה\s*(?P<ans>\d+)\s*[,]?\s*(?:נכונה|נכון|נכונים)"
)
OPTION_RE = re.compile(r"(?P<lead>(?:^|\n))\s*(?P<n>[1-5])\s*\.\s*(?:\n|$)")
HEB_LETTER_TO_ASCII = {"א": "a", "ב": "b"}

# Regexes for alternate formats
BOOKLET_Q_RE = re.compile(r"שאלה\s+מספר\s+(\d+)\s*:")
BOOKLET_OPT_RE = re.compile(r"(?m)^\s*\.([1-5])\s*$")

BOLD_FLAG = 16


# ---------- Span model ----------

@dataclass
class Span:
    text: str
    bold: bool
    color: int
    page: int
    bbox: tuple
    start: int  # offset in joined plain text
    end: int


@dataclass
class ImageRef:
    page: int
    bbox: tuple
    path: str  # filename only


# ---------- Section detection (TOC-driven) ----------

@dataclass
class Section:
    tag: str
    title: str
    start_page: int
    end_page: int
    is_appendix: bool = False


def _parse_toc(toc_text: str) -> list[Section]:
    clean = re.sub(r"[.\s]+", " ", toc_text).strip()
    chunks = re.split(r"(?=(?:מועד|נספח))", clean)
    out: list[Section] = []
    for chunk in chunks:
        m_exam = re.match(r"מועד\s*([אב])'?\s*[–\-]\s*(\d{4})\s+(\d{1,3})\b", chunk)
        if m_exam:
            letter, year, page = m_exam.groups()
            tag = f"{year}-moed-{HEB_LETTER_TO_ASCII.get(letter, letter)}"
            out.append(
                Section(
                    tag=tag,
                    title=f"מועד {letter}' – {year}",
                    start_page=int(page) - 1,
                    end_page=-1,
                    is_appendix=False,
                )
            )
            continue
        m_app = re.match(r"נספח\s+(.+?)\s+(\d{1,3})\b", chunk)
        if m_app:
            name, page = m_app.groups()
            short = re.sub(r"[^֐-׿]+", "-", name.strip()).strip("-")[:25]
            out.append(
                Section(
                    tag=f"appendix-{short or 'x'}",
                    title=f"נספח {name.strip()}",
                    start_page=int(page) - 1,
                    end_page=-1,
                    is_appendix=True,
                )
            )
    out.sort(key=lambda s: s.start_page)
    return out


def _detect_sections(doc: pymupdf.Document) -> list[Section]:
    """Use page 1 (TOC) when present; otherwise scan every page for headers."""
    sections: list[Section] = []
    if doc.page_count > 0:
        sections = _parse_toc(doc[0].get_text())
    if not sections:
        # Fallback: scan each page for a top-of-page bold exam header.
        for p in range(doc.page_count):
            text = doc[p].get_text()
            m = re.search(r"מועד\s*([אב])'?\s*[–\-]\s*(\d{4})", text)
            if m:
                letter, year = m.groups()
                tag = f"{year}-moed-{HEB_LETTER_TO_ASCII.get(letter, letter)}"
                if not any(s.tag == tag for s in sections):
                    sections.append(
                        Section(
                            tag=tag,
                            title=f"מועד {letter}' – {year}",
                            start_page=p,
                            end_page=-1,
                        )
                    )
    sections.sort(key=lambda s: s.start_page)
    for i, sec in enumerate(sections):
        sec.end_page = (
            sections[i + 1].start_page if i + 1 < len(sections) else doc.page_count
        )
    return sections


# ---------- Text/span stream ----------


def _reorder_rtl_line(
    items: list[tuple[dict, str]],
) -> list[tuple[dict, str]]:
    """Reorder span items in a Hebrew (RTL) line into logical reading order.

    PyMuPDF emits spans in x-ascending (visual stream) order. For an RTL
    paragraph the reading order is the reverse — except that LTR runs (Latin
    words, digits, punctuation) read left-to-right within themselves, so they
    must keep their stream order even though the macro order flips.

    We group consecutive non-Hebrew items (with the whitespace between them)
    into a single LTR macro token, then reverse the macro-token order. Hebrew
    words and standalone whitespace each form their own macro token.
    """
    if not items:
        return []
    classified: list[tuple[str, dict, str]] = []
    for span, t in items:
        if not t.strip():
            kind = "ws"
        elif _HEB_RE.search(t):
            kind = "heb"
        else:
            kind = "ltr"
        classified.append((kind, span, t))

    macros: list[list[tuple[dict, str]]] = []
    i = 0
    n = len(classified)
    while i < n:
        kind, span, t = classified[i]
        if kind in ("heb", "ws"):
            macros.append([(span, t)])
            i += 1
            continue
        # kind == "ltr": absorb following ltr items and interior whitespace
        run: list[tuple[dict, str]] = [(span, t)]
        j = i + 1
        while j < n:
            k_kind = classified[j][0]
            if k_kind == "ltr":
                run.append((classified[j][1], classified[j][2]))
                j += 1
                continue
            if k_kind == "ws":
                k = j + 1
                while k < n and classified[k][0] == "ws":
                    k += 1
                if k < n and classified[k][0] == "ltr":
                    for kk in range(j, k + 1):
                        run.append((classified[kk][1], classified[kk][2]))
                    j = k + 1
                    continue
            break
        macros.append(run)
        i = j

    macros.reverse()
    out: list[tuple[dict, str]] = []
    for m in macros:
        out.extend(m)
    return out


def _build_text_stream(
    doc: pymupdf.Document,
    start_page: int,
    end_page: int,
    bidi_correct: bool = False,
) -> tuple[str, list[Span]]:
    """Walk pages, returning ``(plain_text, spans)`` where each span has
    ``start``/``end`` offsets into ``plain_text``."""
    spans: list[Span] = []
    parts: list[str] = []
    cursor = 0
    for p in range(start_page, end_page):
        page = doc[p]
        # Build both dict and rawdict from the same TextPage to avoid parsing
        # the page twice.  dict text is authoritative; rawdict chars supply
        # per-character x-positions for RTL+LTR reordering (PyMuPDF 1.27 leaves
        # rawdict span["text"] empty, so we cannot rely on it directly).
        tp = page.get_textpage()
        d = page.get_text("dict", textpage=tp)
        d_raw = page.get_text("rawdict", textpage=tp)
        del tp
        raw_blocks = d_raw.get("blocks", [])
        for b_idx, block in enumerate(d.get("blocks", [])):
            if block.get("type") != 0:
                continue
            raw_block = raw_blocks[b_idx] if b_idx < len(raw_blocks) else {}
            for l_idx, line in enumerate(block.get("lines", [])):
                raw_lines = raw_block.get("lines") or []
                raw_line = raw_lines[l_idx] if l_idx < len(raw_lines) else {}
                raw_spans = raw_line.get("spans") or []
                line_items: list[tuple[dict, str]] = []
                for s_idx, span in enumerate(line.get("spans", [])):
                    t = span.get("text", "")
                    if not t:
                        continue
                    # Apply x-position reordering only when the rawdict chars
                    # signal a genuine stream-order scramble (large x-reversal).
                    raw_chars = raw_spans[s_idx].get("chars", []) if s_idx < len(raw_spans) else []
                    if raw_chars and _span_has_rtl_disorder(raw_chars):
                        reordered = _chars_to_text(raw_chars)
                        if len(reordered) == len(t):
                            t = reordered
                    if t:
                        line_items.append((span, t))
                # PyMuPDF emits spans in x-ascending (visual stream) order. For RTL
                # lines containing Hebrew, the logical reading order is x-descending,
                # so a multi-span line like "Heb1 LTR Heb2" comes out reversed unless
                # we re-sort. Pure-LTR lines are left untouched.
                if any(_HEB_RE.search(t) for _, t in line_items):
                    if bidi_correct:
                        line_items = _reorder_rtl_line(line_items)
                    else:
                        line_items.sort(key=lambda it: -it[0].get("bbox", (0, 0, 0, 0))[0])
                for span, t in line_items:
                    spans.append(
                        Span(
                            text=t,
                            bold=bool(span.get("flags", 0) & BOLD_FLAG),
                            color=int(span.get("color", 0) or 0),
                            page=p,
                            bbox=tuple(span.get("bbox", (0, 0, 0, 0))),
                            start=cursor,
                            end=cursor + len(t),
                        )
                    )
                    parts.append(t)
                    cursor += len(t)
                parts.append("\n")
                cursor += 1
            parts.append("\n")
            cursor += 1
    return "".join(parts), spans


def _slice_spans(spans: list[Span], start: int, end: int) -> list[Span]:
    out: list[Span] = []
    for s in spans:
        if s.end <= start or s.start >= end:
            continue
        text = s.text
        s0, s1 = s.start, s.end
        if s0 < start:
            text = text[start - s0:]
            s0 = start
        if s1 > end:
            text = text[: len(text) - (s1 - end)]
            s1 = end
        if text:
            out.append(Span(text, s.bold, s.color, s.page, s.bbox, s0, s1))
    return out


def _is_hebrew(c: str) -> bool:
    return bool(c) and "֐" <= c <= "׿"


def _is_latin_or_digit(c: str) -> bool:
    return bool(c) and c.isascii() and (c.isalpha() or c.isdigit())


# Minimum x-jump (in PDF points) in the wrong direction that signals a
# genuinely scrambled span.  Font-kerning / space positioning can cause
# ~10-pt backwards bumps in otherwise well-ordered Hebrew text; a threshold
# of 20 pt filters those out while still catching NMDA-class scrambles (which
# produce jumps of 80-130 pt).
_RTL_DISORDER_THRESHOLD = 20.0
_LTR_CONT = frozenset("-.")

_MARKER_Q = "Q"
_MARKER_A = "A"


def _span_has_rtl_disorder(chars: list[dict]) -> bool:
    """True when the char stream has a suspicious rightward x-jump.

    In RTL Hebrew text chars go left (decreasing x).  A large *increase* in x
    between consecutive non-space chars means PyMuPDF stored them in visual
    order rather than logical order — the signature of the NMDA / .)כן bugs.
    Small bumps (<= _RTL_DISORDER_THRESHOLD) from font kerning or PDF space
    glyphs are ignored so well-ordered spans are left untouched.
    """
    prev_x: float | None = None
    for ch in chars:
        c = ch.get("c", "")
        if isinstance(c, int):
            c = chr(c)
        if not c or c.isspace():
            continue
        origin = ch.get("origin", (0.0, 0.0))
        x = float(origin[0]) if isinstance(origin, (list, tuple)) else 0.0
        if prev_x is not None and (x - prev_x) > _RTL_DISORDER_THRESHOLD:
            return True
        prev_x = x
    return False


def _is_ltr_char(c: str) -> bool:
    """True for characters that belong to a left-to-right run in Hebrew RTL text.

    Extends _is_latin_or_digit to cover Unicode superscripts, subscripts, and
    math symbols (e.g. ⁺ U+207A) so that Na⁺ is treated as one LTR group.
    """
    if not c:
        return False
    if c.isascii():
        return c.isalpha() or c.isdigit()
    if _is_hebrew(c):
        return False
    cat = unicodedata.category(c)
    # L* = Unicode letter, N* = number (includes superscript digits),
    # Sm = math symbol (⁺ ⁻ etc.), Sk = modifier symbol
    return cat[0] in ("L", "N") or cat in ("Sm", "Sk")


def _chars_to_text(chars: list[dict]) -> str:
    """Reconstruct span text from rawdict characters in correct logical order.

    PyMuPDF sometimes emits characters in visual/stream order for mixed RTL+LTR
    spans (e.g. Hebrew text interleaved with NMDA or Na⁺).  The correct logical
    reading order is: sort all chars descending by x (Hebrew RTL), then reverse
    each contiguous run of LTR chars (Latin letters, digits, math symbols) back
    to ascending-x order so they read left-to-right within the RTL line.
    """
    pairs: list[tuple[float, str]] = []
    for ch in chars:
        c = ch.get("c", "")
        if isinstance(c, int):
            c = chr(c)
        if not c:
            continue
        origin = ch.get("origin", (0.0, 0.0))
        x = float(origin[0]) if isinstance(origin, (list, tuple)) else 0.0
        pairs.append((x, c))

    if not pairs:
        return ""

    # Sort all characters descending x (right-to-left = Hebrew reading order).
    sorted_chars = [c for _, c in sorted(pairs, key=lambda p: -p[0])]

    # Reverse each contiguous run of LTR characters so they read left-to-right.
    # A run starts at an _is_ltr_char and continues through additional ltr chars
    # AND through word-internal punctuation (hyphen, period) that sits between
    # two ltr chars (e.g. "GABA-A", "Ca2+").  Trailing word-internal punctuation
    # is trimmed off the run end so it doesn't get reversed into the wrong place.
    result: list[str] = []
    i = 0
    n = len(sorted_chars)
    while i < n:
        if _is_ltr_char(sorted_chars[i]):
            j = i + 1
            while j < n and (
                _is_ltr_char(sorted_chars[j]) or sorted_chars[j] in _LTR_CONT
            ):
                j += 1
            # Trim trailing continuation chars — they belong after the run, not inside it.
            while j > i and sorted_chars[j - 1] in _LTR_CONT:
                j -= 1
            result.extend(sorted_chars[i:j][::-1])
            i = j
        else:
            result.append(sorted_chars[i])
            i += 1

    return "".join(result)


def _crosses_script_boundary(a: str, b: str) -> bool:
    """True when `a` and `b` are from different scripts (Hebrew vs Latin/digit).
    Used to insert a space when PyMuPDF emits adjacent spans across the
    Hebrew↔Latin boundary without one (e.g. "קולטןNMDA")."""
    if not a or not b:
        return False
    return (_is_hebrew(a) and _is_latin_or_digit(b)) or (
        _is_latin_or_digit(a) and _is_hebrew(b)
    )


def _spans_to_html(spans: list[Span], line_breaks: bool = False) -> str:
    """Render spans as HTML.

    Args:
        line_breaks: If True, use bbox y-coordinates to detect *visual* line
            breaks. PyMuPDF puts each formatting run into a separate internal
            "line" even when they share a baseline, so trusting gap alone splits
            "ס – mGluA1 קולטנים" across four ``<br>`` rows. Comparing y instead
            preserves the visual line structure.
            If False (default), only large gaps (> 3) produce ``<br>``; small
            gaps become spaces (better for question/option text where PDF line-
            wrap artefacts should not split a sentence).
    """
    # Tolerance (in PDF points) for treating two spans as the same visual row.
    # Bullet glyphs are often drawn slightly above/below the baseline.
    Y_TOL = 4.0

    parts: list[str] = []
    open_b = False
    open_color: Optional[int] = None
    prev_end: Optional[int] = None
    prev_last_char: str = ""
    prev_y: Optional[float] = None
    prev_page: Optional[int] = None
    for s in spans:
        if prev_end is not None and s.start > prev_end:
            gap = s.start - prev_end
            if line_breaks:
                cur_y = s.bbox[1] if s.bbox else None
                page_changed = prev_page is not None and s.page != prev_page
                y_changed = (
                    prev_y is not None
                    and cur_y is not None
                    and abs(cur_y - prev_y) > Y_TOL
                )
                # <br> only on a real visual line break: page change, y change,
                # or a clearly empty gap in the source text.
                if page_changed or y_changed or gap > 3:
                    sep = "<br>"
                else:
                    sep = " "
            else:
                # gap 1-2 = line wrap or block boundary inside a paragraph (very
                # common when PyMuPDF splits a wrapped sentence into two blocks).
                # Use a plain space so words don't fuse. Reserve <br> for clearly
                # empty visual lines (gap > 3).
                sep = "<br>" if gap > 3 else " "
            # Tight punctuation: suppress the inserted space when one side is
            # an opening bracket or the other side is a closing/sentence
            # punctuation that should hug its neighbour.  Without this, runs
            # like `(` + `Na+` + `)` (which PyMuPDF often splits across "lines"
            # at every font change) come out as `( Na+ )`.
            if sep == " ":
                next_first = s.text[0] if s.text else ""
                if prev_last_char in "([{" or next_first in ").,;:!?]}":
                    sep = ""
            parts.append(sep)
        elif (
            prev_end is not None
            and s.start == prev_end
            and s.text
            and _crosses_script_boundary(prev_last_char, s.text[0])
        ):
            # No gap between spans, but Hebrew↔Latin boundary → insert a space.
            parts.append(" ")
        target_b = s.bold
        target_color = s.color if s.color else None
        if open_b and not target_b:
            parts.append("</b>")
            open_b = False
        if open_color is not None and open_color != target_color:
            parts.append("</span>")
            open_color = None
        if target_color is not None and open_color is None:
            parts.append(f'<span style="color:#{target_color:06x}">')
            open_color = target_color
        if target_b and not open_b:
            parts.append("<b>")
            open_b = True
        text = html_lib.escape(s.text).replace("\n", "<br>")
        parts.append(text)
        prev_end = s.end
        if s.text:
            prev_last_char = s.text[-1]
        if s.bbox:
            prev_y = s.bbox[1]
        prev_page = s.page
    if open_b:
        parts.append("</b>")
    if open_color is not None:
        parts.append("</span>")
    return _normalize_html("".join(parts))


# Bullet markers used in Hebrew exam PDFs. PyMuPDF often extracts the visual
# "○" glyph as a different code point depending on the font's CMap — most
# commonly Latin "o" or Hebrew samekh "ס". Match all plausible variants.
_BULLET_CHARS = "oO○●◦•▪▫ס•○◦▪▫"
_BULLET_AT_START = re.compile(
    rf"^\s*([{_BULLET_CHARS}]|\*)(?:\s+[–\-—])?\s+",
    re.UNICODE,
)


def _format_explanation_html(html: str) -> str:
    """Post-process explanation HTML for nicer rendering on Anki cards.

    - Lines starting with a bullet glyph become ``<li>`` items inside ``<ul>``.
    - Non-bullet lines wrap in a ``unicode-bidi: isolate`` block so RTL/LTR
      mixed text wraps cleanly without punctuation jumping to line edges.

    Operates on the ``<br>``-separated output from ``_spans_to_html``.
    """
    if not html:
        return html

    lines = re.split(r"<br\s*/?>", html)
    out: list[str] = []
    in_list = False

    def _close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        plain = re.sub(r"<[^>]+>", "", line).strip()
        if _BULLET_AT_START.match(plain):
            # Strip the bullet from the HTML (preserving any leading tags).
            li = re.sub(
                rf"^(\s*(?:<[^>]+>\s*)*)(?:[{_BULLET_CHARS}]|\*)(?:\s+[–\-—])?\s+",
                r"\1",
                line,
                count=1,
            )
            if not in_list:
                out.append(
                    '<ul style="unicode-bidi:isolate;direction:rtl;'
                    'text-align:right;padding-inline-start:24px;'
                    'padding-inline-end:0;margin:6px 0">'
                )
                in_list = True
            out.append(
                '<li style="unicode-bidi:isolate;text-align:right;'
                f'margin:4px 0">{li}</li>'
            )
        else:
            _close_list()
            out.append(
                '<div style="unicode-bidi:isolate;text-align:right;'
                f'margin:4px 0">{line}</div>'
            )
    _close_list()
    return _fix_bidi_parens("".join(out))


_HEB_RE = re.compile(r"[א-ת]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIR_SPAN_OPEN = re.compile(r'<span\b[^>]*\bdir=["\'](?:ltr|rtl)["\']', re.I)


def _fix_bidi_parens(html: str) -> str:
    """Wrap parenthesised runs in bidi-isolated containers.

    In an RTL Hebrew paragraph the bidi algorithm attaches neutral chars (
    and ) to whichever strong-direction neighbour wins, which produces visible
    glitches around Latin/Hebrew transitions.  We fix this by wrapping
    parenthesised groups to pin their direction:

    - **Pure ASCII inside** (e.g. ``(EPSP)``, ``(Na+)``)
      → ``<span dir="ltr">(...)</span>``
    - **Mixed Hebrew+Latin inside** (e.g. ``(NMDA כן יכול)``)
      → ``<span dir="rtl" style="unicode-bidi:isolate">(...)</span>``
      Forces the parens to attach to an RTL context isolated from the outer
      paragraph, so they render around the content rather than being shoved
      to the wrong side by surrounding Latin runs.
    - **Pure Hebrew inside** → unchanged (renders fine in an RTL paragraph).

    The walker handles inline tags inside the parens (``(<b>EPSP</b>)``),
    leaves HTML attribute values alone (``style="rgb(255,0,0)"``), and is
    idempotent — content already inside a ``<span dir="…">`` is skipped.
    """
    def _strip(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s)

    n = len(html)
    out: list[str] = []
    i = 0
    iso_depth = 0  # depth inside an existing dir="ltr"/"rtl" span

    while i < n:
        c = html[i]
        if c == "<":
            j = html.find(">", i)
            if j == -1:
                out.append(html[i:])
                break
            tag = html[i : j + 1]
            if _DIR_SPAN_OPEN.match(tag):
                iso_depth += 1
            elif tag == "</span>" and iso_depth > 0:
                iso_depth -= 1
            out.append(tag)
            i = j + 1
            continue

        if c == "(" and iso_depth == 0:
            # Find the matching ')' in text context, skipping over nested
            # HTML tags and balanced inner parens.
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                cj = html[j]
                if cj == "<":
                    end = html.find(">", j)
                    if end == -1:
                        depth = -1
                        break
                    j = end + 1
                elif cj == "(":
                    depth += 1
                    j += 1
                elif cj == ")":
                    depth -= 1
                    if depth == 0:
                        break
                    j += 1
                else:
                    j += 1
            if depth == 0 and j < n:
                inner_html = html[i + 1 : j]
                inner_text = _strip(inner_html).strip()
                if inner_text:
                    has_heb = bool(_HEB_RE.search(inner_text))
                    has_latin = bool(_LATIN_RE.search(inner_text))
                    if has_heb and has_latin:
                        out.append(
                            f'<span dir="rtl" style="unicode-bidi:isolate">'
                            f"({inner_html})</span>"
                        )
                        i = j + 1
                        continue
                    if not has_heb:
                        out.append(f'<span dir="ltr">({inner_html})</span>')
                        i = j + 1
                        continue
                    # Pure Hebrew → leave alone.
            out.append(c)
            i += 1
            continue

        out.append(c)
        i += 1
    return "".join(out)


def _normalize_html(s: str) -> str:
    # Iterate to a fixed point so nested empty tags collapse fully.
    for _ in range(3):
        s = re.sub(r"<b>(\s|<br>)*</b>", "", s)
        s = re.sub(r'<span[^>]*>(\s|<br>)*</span>', "", s)
        s = re.sub(r"(<br>\s*){2,}", "<br>", s)
    s = re.sub(r"^(<br>|\s)+|(<br>|\s)+$", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    return _fix_bidi_parens(s)


# ---------- Markers, image association ----------

@dataclass
class _Marker:
    kind: str
    number: int
    answer: Optional[int]
    text_offset: int
    page: int
    y_top: float


def _offset_to_span(offset: int, spans: list[Span]) -> Optional[Span]:
    # Binary search would be nicer, but linear is fine here.
    for s in spans:
        if s.start <= offset < s.end:
            return s
    return None


def _find_markers(plain: str, spans: list[Span]) -> list[_Marker]:
    markers: list[_Marker] = []
    for m in QUESTION_RE.finditer(plain):
        s = _offset_to_span(m.start(), spans)
        if s is None:
            continue
        markers.append(
            _Marker(_MARKER_Q, int(m.group(1)), None, m.start(), s.page, s.bbox[1])
        )
    for m in ANSWER_RE.finditer(plain):
        s = _offset_to_span(m.start(), spans)
        if s is None:
            continue
        markers.append(
            _Marker(
                _MARKER_A,
                int(m.group("num")),
                int(m.group("ans")),
                m.start(),
                s.page,
                s.bbox[1],
            )
        )
    markers.sort(key=lambda m: m.text_offset)
    return markers


def _extract_images(
    doc: pymupdf.Document,
    pages: range,
    media_dir: Path,
    tag: str,
    on_progress=None,
) -> list[ImageRef]:
    out: list[ImageRef] = []
    seen_hashes: set[int] = set()
    total_pages = pages.stop - pages.start
    try:
        from PIL import Image as _PIL_Image
    except ImportError:
        _PIL_Image = None
    for i, p in enumerate(pages):
        if on_progress and i % 3 == 0:
            on_progress(i / max(total_pages, 1), f"Extracting images, page {p+1}…")
        page = doc[p]
        for img_idx, img_info in enumerate(page.get_images(full=True)):
            xref = img_info[0]
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.colorspace and pix.colorspace.n >= 4:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                # Skip tiny decorative images (< 40x40 px)
                if pix.width < 40 or pix.height < 40:
                    pix = None
                    continue
                key = hash((xref, pix.width, pix.height))
                if key in seen_hashes:
                    pix = None
                    continue
                seen_hashes.add(key)
                fname = f"{tag}-p{p+1}-img{img_idx+1}-x{xref}.png"
                fpath = media_dir / fname
                MAX_DIM = 1500
                if pix.width > MAX_DIM or pix.height > MAX_DIM:
                    img = _PIL_Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    img.thumbnail((MAX_DIM, MAX_DIM), _PIL_Image.LANCZOS)
                    img.save(str(fpath))
                    pix = None
                else:
                    pix.save(fpath)
                    pix = None
            except Exception:
                continue
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            if not rects:
                continue
            for r in rects:
                out.append(ImageRef(page=p, bbox=tuple(r), path=fname))
    return out


def _associate_images(
    markers: list[_Marker], images: list[ImageRef]
) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    if not markers:
        return out
    sorted_markers = sorted(markers, key=lambda m: (m.page, m.y_top))
    for img in images:
        best: Optional[_Marker] = None
        for m in sorted_markers:
            if (m.page, m.y_top) <= (img.page, img.bbox[1]):
                best = m
            else:
                break
        if best is None:
            continue
        key = (best.kind, best.number)
        if key not in out:
            out[key] = img.path
    return out


# ---------- Question / answer chunk parsing ----------

def _parse_question_chunk(
    plain: str, q_start: int, q_end: int, spans: list[Span]
) -> tuple[str, list[str]]:
    """Find option markers in the original plain text (where lines are
    separated by ``\\n``) and slice spans at their absolute offsets."""
    section_text = plain[q_start:q_end]
    # Skip the ``שאלה N:`` header
    header = re.match(r"\s*שאלה\s*\d+\s*:\s*", section_text)
    stem_start = q_start + (header.end() if header else 0)

    # Locate option markers as ".N" or "N." standalone tokens on their own line.
    option_starts: list[tuple[int, int, int]] = []  # (n, abs_marker_start, abs_marker_end)
    for m in OPTION_RE.finditer(section_text):
        n = int(m.group("n"))
        # Marker text starts after the leading newline (or string start)
        marker_lead = q_start + m.start("lead")
        marker_end = q_start + m.end()
        expected = len(option_starts) + 1
        if n == expected and 1 <= n <= 5:
            # Use the position right after the marker for the text start
            option_starts.append((n, marker_lead, marker_end))
        elif n == 1 and not option_starts:
            option_starts.append((n, marker_lead, marker_end))
        # If we already have option N and we see a duplicate or out-of-order,
        # skip — likely a "1." inside text like "(1-3 שבועות)"
    # Stem
    if option_starts:
        stem_end = option_starts[0][1]
    else:
        stem_end = q_end
    stem_spans = _slice_spans(spans, stem_start, stem_end)
    question_html = _spans_to_html(stem_spans)
    options: list[str] = []
    for i, (_n, _lead, marker_end) in enumerate(option_starts):
        opt_start = marker_end
        opt_end = (
            option_starts[i + 1][1] if i + 1 < len(option_starts) else q_end
        )
        opt_spans = _slice_spans(spans, opt_start, opt_end)
        options.append(_spans_to_html(opt_spans))
    return question_html, options


def _parse_answer_chunk(
    plain: str, a_start: int, a_end: int, spans: list[Span]
) -> str:
    section_text = plain[a_start:a_end]
    header = re.match(
        r"\s*\d+\s*\.\s*תשובה\s*\d+\s*[,]?\s*(?:נכונה|נכון|נכונים)\s*[,]?\s*",
        section_text,
    )
    body_start = a_start + (header.end() if header else 0)
    body_spans = _slice_spans(spans, body_start, a_end)
    return _format_explanation_html(_spans_to_html(body_spans, line_breaks=True))


# ---------- Alternate format detection and parsers ----------

def _detect_format(doc: pymupdf.Document) -> str:
    """Returns 'huji_review', 'google_form_exam', 'exam_booklet', or 'multi_exam'."""
    if doc.page_count == 0:
        return "multi_exam"
    page0 = doc[0].get_text()
    if "exam4.cs.huji.ac.il" in page0 or ":הנכונה התשובה" in page0:
        return "huji_review"
    if "docs.google.com/forms" in page0 or "Switch account" in page0:
        return "google_form_exam"
    if "שאלה מספר" in page0:
        return "exam_booklet"
    for p in range(1, min(3, doc.page_count)):
        if "שאלה מספר" in doc[p].get_text():
            return "exam_booklet"
    return "multi_exam"


def _is_huji_metadata(line: str) -> bool:
    l = line.strip()
    if not l:
        return True
    if re.match(r"^שאלה\s+\d+$", l):
        return True
    if l in ("תקין", "שגוי", "לענות", "מצב", "הסתיים", "/"):
        return True
    if "נקודות מתוך" in l:
        return True
    if re.match(r"^\d+$", l):
        return True
    if re.match(r"^\d+\.\d+$", l):
        return True
    if re.match(r"^\d+/\d+$", l):
        return True
    if "מספר קורס" in l:
        return True
    if l.startswith("http"):
        return True
    if re.search(r"AM\s+\d+:\d+", l):
        return True
    if l.startswith("התחיל ב") or l.startswith("הושלם ב"):
        return True
    if "ציונים" in l or "ציון מירבי" in l:
        return True
    if "הזמן שלקח" in l or "סקירת ניסיון" in l:
        return True
    if "הקורסים שלי" in l or "/יחידות" in l or re.match(r"^Topic \d+$", l):
        return True
    # Duration lines like "1 שעה 19 דקות"
    if re.match(r"^\d+\s+[א-ת]+\s+\d+\s+[א-ת]+$", l):
        return True
    return False


def _heb_letters_only(s: str) -> str:
    return re.sub(r"[^א-ת\s]", "", s).strip()


def _find_correct_option(answer_text: str, options: list[str]) -> Optional[int]:
    def norm(s: str) -> str:
        s = re.sub(r"\s+", " ", html_lib.unescape(s).replace("", "")).strip()
        return re.sub(r"\s*-\s*", "-", s)

    norm_ans = norm(answer_text)
    norm_opts = [norm(o) for o in options]

    for i, o in enumerate(norm_opts):
        if norm_ans == o:
            return i + 1
    for i, o in enumerate(norm_opts):
        if norm_ans in o or o in norm_ans:
            return i + 1
    h_ans = _heb_letters_only(norm_ans)
    if h_ans:
        for i, o in enumerate(norm_opts):
            h_o = _heb_letters_only(o)
            if h_o and (h_ans in h_o or h_o in h_ans):
                return i + 1
    return None


def _extract_exam_tag(page0_text: str) -> tuple[str, str]:
    """Return (tag, title) from the first-page text of any supported exam format."""
    letter_m = re.search(r"מועד\s*'?\s*([אב])", page0_text)
    year_m = re.search(r"EXAM\s+(\d{4})", page0_text) or re.search(r"\b(20\d{2})\b", page0_text)
    letter = letter_m.group(1) if letter_m else "a"
    year = year_m.group(1) if year_m else "unknown"
    tag = f"{year}-moed-{HEB_LETTER_TO_ASCII.get(letter, letter)}"
    title = f"מועד {letter}' – {year}"
    return tag, title


def _parse_huji_review(doc: pymupdf.Document, media_dir: Path) -> ParseResult:
    """Parse HUJI online exam review export (exam4.cs.huji.ac.il)."""
    media_dir.mkdir(parents=True, exist_ok=True)

    page0_text = doc[0].get_text() if doc.page_count else ""
    tag, title = _extract_exam_tag(page0_text)

    # Collect (page_num, line_text) for all pages. Use _build_text_stream so
    # Hebrew word order is reading-order (PyMuPDF's get_text("text") returns
    # spans in physical x-ascending order, which reverses RTL words).
    page_lines: list[tuple[int, str]] = []
    q_num_seq: list[int] = []
    for p in range(doc.page_count):
        page_text, _ = _build_text_stream(doc, p, p + 1, bidi_correct=True)
        for raw_line in page_text.split("\n"):
            stripped = raw_line.strip()
            m = re.match(r"^שאלה\s+(\d+)$", stripped)
            if m:
                q_num_seq.append(int(m.group(1)))
            page_lines.append((p, stripped))

    # Extract images (small decorative ones already filtered in _extract_images)
    images = _extract_images(doc, range(doc.page_count), media_dir, tag)

    # Build question blocks by scanning content lines
    blocks: list[dict] = []
    current_lines: list[tuple[int, str]] = []  # (page, text)
    just_answered = False

    for page_num, line in page_lines:
        if _is_huji_metadata(line):
            continue
        # Match answer marker in both word orders (bidi reversal can flip
        # "התשובה הנכונה" to "הנכונה התשובה") and at line start or end.
        _ans_m = re.match(
            r"^(?:התשובה הנכונה|התשובות הנכונות הן|הנכונה התשובה|הן הנכונות התשובות)\s*:?\s*",
            line,
        )
        _ans_rev = None
        if not _ans_m:
            _ans_rev = re.search(
                r":?\s*(?:הנכונה התשובה|הן הנכונות התשובות)(.*?)$", line
            )
        if _ans_m or _ans_rev:
            if _ans_m:
                # Bidi reorder may push the trailing colon to the end of the line
                # when the answer contains an LTR run. Strip a dangling colon.
                answer_text = re.sub(r"\s*:\s*$", "", line[_ans_m.end():]).strip()
                is_multi = "הנכונות" in _ans_m.group()
            else:
                # Marker at line end: "<heb answer>: הנכונה התשובה [ltr tail]"
                heb_before = line[: _ans_rev.start()].rstrip(": \t")
                ltr_after = _ans_rev.group(1).strip().rstrip(": ")
                answer_text = (heb_before + " " + ltr_after).strip() if ltr_after else heb_before
                is_multi = "הנכונות" in line
            pages_in_block = {pg for pg, _ in current_lines}
            blocks.append(
                {
                    "lines": [l for _, l in current_lines],
                    "answer": answer_text,
                    "is_multi": is_multi,
                    "extra": [],
                    "pages": pages_in_block | {page_num},
                }
            )
            current_lines = []
            just_answered = True
        elif just_answered:
            # Possible continuation of previous answer (e.g. ")Disorder")
            if blocks and (line.startswith(")") or not re.search(r"[א-ת]", line)):
                blocks[-1]["extra"].append(line)
            else:
                current_lines = [(page_num, line)]
                just_answered = False
        else:
            current_lines.append((page_num, line))
            just_answered = False

    page_image_map: dict[int, str] = {}
    for img in images:
        if img.page not in page_image_map:
            page_image_map[img.page] = img.path

    q_num_iter = iter(q_num_seq)
    cards: list[Card] = []
    warnings: list[str] = []

    for block in blocks:
        block_lines = block["lines"]

        # Skip blocks with no question (e.g. metadata accumulated before first answer)
        if not any("?" in l for l in block_lines):
            continue

        qnum = next(q_num_iter, len(cards) + 1)
        answer_text = block["answer"]
        if block["extra"]:
            answer_text = answer_text + " " + " ".join(block["extra"])

        # Question boundary: last line containing '?' (bidi-corrected text may
        # place sentence-final punctuation mid-line when LTR text follows).
        q_end_idx = -1
        for i, l in enumerate(block_lines):
            if "?" in l:
                q_end_idx = i
        if q_end_idx >= 0:
            q_lines = block_lines[: q_end_idx + 1]
            opt_lines_raw = block_lines[q_end_idx + 1 :]
        else:
            split = max(0, len(block_lines) - 5)
            q_lines = block_lines[:split]
            opt_lines_raw = block_lines[split:]


        # Remove lines that are only a checkmark (PDF layout artifact)
        opt_lines_raw = [l for l in opt_lines_raw if l.replace("", "").replace("", "").replace("✓", "").strip()]
        # Merge obvious wrap-artefact continuation lines into the preceding
        # option. Conservative: only the clearest cases, since options in
        # Hebrew exam PDFs rarely end with terminal punctuation and we don't
        # want to collapse valid options.
        merged_opts: list[str] = []
        for _ol in opt_lines_raw:
            _paren_cont = _ol.startswith(")") and (len(_ol) < 30 and not _HEB_RE.search(_ol) or len(_ol) < 15)
            _dot_cont = _ol.startswith(".") and len(_ol.replace(".", "").strip()) < 20
            if (_paren_cont or _dot_cont) and merged_opts:
                merged_opts[-1] = merged_opts[-1] + " " + _ol
            else:
                merged_opts.append(_ol)
        opt_lines_raw = merged_opts

        options = [
            html_lib.escape(l.replace("", "").strip())
            for l in opt_lines_raw
            if l.strip()
        ]
        q_clean = [
            html_lib.escape(l.replace("", "").strip())
            for l in q_lines
            if l.strip()
        ]
        question_html = "<br>".join(q_clean)

        is_multi = block.get("is_multi", False)
        if is_multi:
            correct = None
            explanation_html = (
                _format_explanation_html(
                    html_lib.escape(answer_text).replace("\n", "<br>")
                )
                if answer_text
                else ""
            )
        else:
            correct = _find_correct_option(answer_text, options)
            explanation_html = ""

        if len(options) != 5:
            warnings.append(f"{tag} Q{qnum}: detected {len(options)} options (expected 5)")

        # Associate image: pick first image on any page this question spans
        q_img = None
        for pg in sorted(block["pages"]):
            if pg in page_image_map:
                q_img = page_image_map.pop(pg)
                break

        cards.append(
            Card(
                exam_tag=tag,
                number=qnum,
                question_html=question_html,
                options=options,
                correct=correct,
                explanation_html=explanation_html,
                question_image=q_img,
                source=f"{title}, שאלה {qnum}",
            )
        )

    return ParseResult(cards=cards, warnings=warnings, media_dir=str(media_dir), exam_tags=[tag])


def _is_google_form_meta(line: str) -> bool:
    """Filter lines that are Google Forms UI chrome, not exam content."""
    l = line.strip()
    if not l:
        return True
    if l.startswith("http"):
        return True
    if re.match(r"^\d+/\d+$", l):  # page number like "1/21"
        return True
    if re.search(r"\d+:\d+\s*,\s*\d+\.\d+\.\d{4}", l):  # timestamp "10:33 ,4.5.2026"
        return True
    if "טופס ערעורים" in l:
        return True
    if l in ("Clear selection", "Submit", "Clear form", "Not shared", "Switch account", "Forms"):
        return True
    if "does this form look suspicious" in l.lower() or l.lower() == "report":
        return True
    if "this form was created inside" in l.lower():
        return True
    if "@" in l and re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", l):
        return True
    # Lone "ב'" line from split title on page 0
    if l in ("'ב", "ב'"):
        return True
    return False


def _parse_google_form_page(page_text: str) -> tuple[list[str], list[str]]:
    """Parse one page of a Google Forms exam PDF.

    PyMuPDF returns options (left column) before question text (right column).
    Options are separated by blank lines; question text blocks have NO blank
    lines between their constituent lines, and always contain '?'.

    Returns (options, questions) where each entry is a plain string.
    """
    lines = page_text.split("\n")
    # Build content blocks: runs of consecutive non-empty lines
    raw_blocks: list[list[str]] = []
    current: list[str] = []
    for raw in lines:
        s = raw.strip()
        if s:
            current.append(s)
        elif current:
            raw_blocks.append(current)
            current = []
    if current:
        raw_blocks.append(current)

    # Filter metadata lines and join each block to a single string
    text_blocks: list[str] = []
    for block in raw_blocks:
        content = [l for l in block if not _is_google_form_meta(l)]
        if content:
            text_blocks.append(" ".join(content))

    # Merge continuation blocks: a block that ends with '(' but contains no ')'
    # is an unclosed parenthetical — the closing text is on the next block.
    merged: list[str] = []
    for tb in text_blocks:
        if merged and merged[-1].endswith("(") and ")" not in merged[-1]:
            merged[-1] = merged[-1] + " " + tb
        else:
            merged.append(tb)

    # Classify: anything containing '?' is a question; everything else is an option.
    # Questions always contain '?'; options are declarative statements.
    options: list[str] = []
    questions: list[str] = []
    for block_text in merged:
        if "?" in block_text:
            questions.append(block_text)
        else:
            options.append(block_text)
    return options, questions


def _parse_google_form_exam(doc: pymupdf.Document, media_dir: Path) -> ParseResult:
    """Parse a HUJI Google Forms exam export (no answer key present)."""
    media_dir.mkdir(parents=True, exist_ok=True)
    # Use bidi-corrected page 0 so _extract_exam_tag finds year without reversal
    page0_bidi, _ = _build_text_stream(doc, 0, 1, bidi_correct=True)
    tag, title = _extract_exam_tag(page0_bidi)

    cards: list[Card] = []
    warnings: list[str] = []
    qnum = 0
    for p in range(doc.page_count):
        page_text, _ = _build_text_stream(doc, p, p + 1, bidi_correct=True)
        opts, qs = _parse_google_form_page(page_text)
        if not qs:
            continue
        expected = len(qs) * 5
        if len(opts) != expected:
            warnings.append(
                f"Page {p}: {len(opts)} options for {len(qs)} questions "
                f"(expected {expected}) — some options may be truncated"
            )
        for i, q_text in enumerate(qs):
            qnum += 1
            cards.append(
                Card(
                    exam_tag=tag,
                    number=qnum,
                    question_html=_normalize_html(q_text),
                    options=opts[i * 5 : (i + 1) * 5],
                    correct=None,
                    explanation_html="",
                    source=f"{title}, שאלה {qnum}",
                )
            )
    return ParseResult(cards=cards, warnings=warnings, media_dir=str(media_dir), exam_tags=[tag])


def _parse_booklet_question_chunk(
    plain: str, q_start: int, q_end: int, spans: list[Span]
) -> tuple[str, list[str]]:
    """Parse a question chunk from an exam booklet (`:N שאלה מספר` format).

    Option text precedes its `.N` marker (reversed vs. the reconstruction format).
    """
    section_text = plain[q_start:q_end]
    header_m = BOOKLET_Q_RE.search(section_text)
    if not header_m:
        return "", []
    content_start = q_start + header_m.end()

    # Question block ends at the first double-newline after the header
    first_sep = section_text.find("\n\n", header_m.end())
    if first_sep >= 0:
        q_block_end = q_start + first_sep + 1
        opts_start_in_chunk = first_sep + 2
    else:
        q_block_end = q_end
        opts_start_in_chunk = len(section_text)

    stem_spans = _slice_spans(spans, content_start, q_block_end)
    question_html = _spans_to_html(stem_spans)

    # Find .N markers (option number comes after option text in this format)
    option_markers: list[re.Match] = []
    for m in BOOKLET_OPT_RE.finditer(section_text, opts_start_in_chunk):
        n = int(m.group(1))
        if n == len(option_markers) + 1:
            option_markers.append(m)

    options: list[str] = []
    pos_in_chunk = opts_start_in_chunk
    for m in option_markers:
        opt_abs_start = q_start + pos_in_chunk
        opt_abs_end = q_start + m.start()
        opt_spans = _slice_spans(spans, opt_abs_start, opt_abs_end)
        options.append(_spans_to_html(opt_spans))
        # \s*$ in the RE already consumes trailing \n; skip one more for block boundary
        pos_in_chunk = m.end() + 1

    # Fallback: options in blank-line-separated blocks with digit prefix ("1text",
    # ". 1text") or dot-number suffix ("text .1") — booklets where the marker is
    # typeset inline. Also handles multi-line questions by rescanning from the
    # header to find where option 1 begins.
    if not options:
        content = section_text[header_m.end():]
        all_blks = [b.strip() for b in re.split(r"\n{2,}", content) if b.strip()]
        # Option-1 block: starts with ^\s*\.?\s*1<non-digit> OR ends with .1
        _OPT1 = re.compile(r"^\s*\.?\s*1(?!\d)|(?<!\d)\.1\s*$")
        opt1_idx = next((i for i, b in enumerate(all_blks) if _OPT1.search(b)), -1)
        # Secondary fallback: locate option-2 and treat the preceding block as option-1
        # (handles bidi-garbled option text where the 1 is embedded mid-string)
        if opt1_idx < 0:
            _OPT2 = re.compile(r"^\s*\.?\s*2(?!\d)|(?<!\d)\.2\s*$")
            opt2_idx = next((i for i, b in enumerate(all_blks) if _OPT2.search(b)), -1)
            if opt2_idx > 0:
                opt1_idx = opt2_idx - 1
        if opt1_idx >= 0:
            q_blks = all_blks[:opt1_idx]
            if q_blks:
                question_html = "<br>".join(html_lib.escape(b) for b in q_blks)
            for blk in all_blks[opt1_idx : opt1_idx + 6]:
                cleaned = re.sub(r"^\s*\.?\s*[1-5](?!\d)\s*", "", blk)
                cleaned = re.sub(r"(?<!\d)\.[1-5]\s*$", "", cleaned).strip()
                if cleaned:
                    options.append(html_lib.escape(cleaned))

    return question_html, options


def _shuffle_booklet_options(
    options: list[str], exam_tag: str, qnum: int
) -> tuple[list[str], Optional[int]]:
    """Shuffle booklet options whose source-index 0 is the correct answer by convention.

    Booklet PDFs (no answer key) list the correct answer first. Setting correct=1
    blindly would make every Anki card trivial, so we shuffle and return the new
    1-based position of the originally-first option. The shuffle is seeded by
    (exam_tag, qnum) so the same PDF re-parses identically across runs.
    """
    if not options:
        return [], None
    if len(options) == 1:
        return list(options), 1
    correct_text = options[0]
    shuffled = list(options)
    # str seed is stable across runs; tuple seeds raise TypeError, and a plain
    # hash() of a tuple is salted unless PYTHONHASHSEED is fixed.
    rng = random.Random(f"{exam_tag}:{qnum}")
    rng.shuffle(shuffled)
    # Guarantee the correct answer never stays at position 1 (the source position),
    # which would make the shuffle invisible to the learner for that card.
    if shuffled[0] == correct_text:
        swap_idx = rng.randint(1, len(shuffled) - 1)
        shuffled[0], shuffled[swap_idx] = shuffled[swap_idx], shuffled[0]
    return shuffled, shuffled.index(correct_text) + 1


def _parse_exam_booklet(doc: pymupdf.Document, media_dir: Path) -> ParseResult:
    """Parse a single-exam question booklet (`:N שאלה מספר`, no answer key)."""
    media_dir.mkdir(parents=True, exist_ok=True)

    page0_text = doc[0].get_text() if doc.page_count else ""
    tag, title = _extract_exam_tag(page0_text)

    plain, spans = _build_text_stream(doc, 0, doc.page_count)

    q_matches = list(BOOKLET_Q_RE.finditer(plain))
    if not q_matches:
        return ParseResult(cards=[], warnings=["No questions found (exam_booklet)"], exam_tags=[])

    images = _extract_images(doc, range(doc.page_count), media_dir, tag)
    q_image_markers: list[_Marker] = []
    for qm in q_matches:
        s = _offset_to_span(qm.start(), spans)
        if s:
            q_image_markers.append(
                _Marker(_MARKER_Q, int(qm.group(1)), None, qm.start(), s.page, s.bbox[1])
            )
    image_map = _associate_images(q_image_markers, images)

    cards: list[Card] = []
    warnings: list[str] = []
    for i, qm in enumerate(q_matches):
        q_start = qm.start()
        q_end = q_matches[i + 1].start() if i + 1 < len(q_matches) else len(plain)
        qnum = int(qm.group(1))
        q_html, options = _parse_booklet_question_chunk(plain, q_start, q_end, spans)
        if len(options) != 5:
            warnings.append(f"{tag} Q{qnum}: detected {len(options)} options (expected 5)")
        shuffled_options, correct_idx = _shuffle_booklet_options(options, tag, qnum)
        cards.append(
            Card(
                exam_tag=tag,
                number=qnum,
                question_html=q_html,
                options=shuffled_options,
                correct=correct_idx,
                explanation_html="",
                question_image=image_map.get((_MARKER_Q, qnum)),
                source=f"{title}, שאלה {qnum}",
            )
        )

    return ParseResult(cards=cards, warnings=warnings, media_dir=str(media_dir), exam_tags=[tag])


# ---------- Public entry point ----------

def parse_pdf(
    pdf_path: str | Path,
    media_dir: str | Path,
    include_appendices: bool = False,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> ParseResult:
    def _rpt(f: float, m: str) -> None:
        on_progress and on_progress(f, m)

    _rpt(0.0, "Reading PDF…")
    pdf_path = Path(pdf_path)
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    _rpt(0.05, "Detecting format…")

    fmt = _detect_format(doc)
    if fmt == "huji_review":
        _rpt(0.1, "Parsing HUJI format…")
        result = _parse_huji_review(doc, media_dir)
        _rpt(1.0, "Done")
        return result
    if fmt == "google_form_exam":
        _rpt(0.1, "Parsing Google Forms exam…")
        result = _parse_google_form_exam(doc, media_dir)
        _rpt(1.0, "Done")
        return result
    if fmt == "exam_booklet":
        _rpt(0.1, "Parsing exam booklet…")
        result = _parse_exam_booklet(doc, media_dir)
        _rpt(1.0, "Done")
        return result

    sections = _detect_sections(doc)
    cards: list[Card] = []
    warnings: list[str] = []
    exam_tags: list[str] = []

    for i, sec in enumerate(sections):
        if sec.is_appendix and not include_appendices:
            continue
        if sec.is_appendix:
            _, app_spans = _build_text_stream(doc, sec.start_page, sec.end_page)
            html = _format_explanation_html(_spans_to_html(app_spans, line_breaks=True))
            cards.append(
                Card(
                    exam_tag=sec.tag,
                    number=1,
                    question_html=sec.title,
                    options=[],
                    correct=None,
                    explanation_html=html,
                    source=sec.title,
                )
            )
            exam_tags.append(sec.tag)
            continue
        _rpt(
            0.1 + 0.8 * i / max(len(sections), 1),
            f"Parsing section {i+1}/{len(sections)}: {sec.tag}…",
        )
        section_cards, section_warnings = _parse_exam_section(doc, sec, media_dir)
        cards.extend(section_cards)
        warnings.extend(section_warnings)
        if section_cards:
            exam_tags.append(sec.tag)
    _rpt(1.0, "Done")
    return ParseResult(
        cards=cards,
        warnings=warnings,
        media_dir=str(media_dir),
        exam_tags=exam_tags,
    )


def _parse_exam_section(
    doc: pymupdf.Document, sec: Section, media_dir: Path
) -> tuple[list[Card], list[str]]:
    plain, spans = _build_text_stream(doc, sec.start_page, sec.end_page)
    markers = _find_markers(plain, spans)
    images = _extract_images(doc, range(sec.start_page, sec.end_page), media_dir, sec.tag)
    image_map = _associate_images(markers, images)

    questions: dict[int, tuple[int, int]] = {}
    answers: dict[int, tuple[int, int, Optional[int]]] = {}
    for i, m in enumerate(markers):
        next_offset = (
            markers[i + 1].text_offset if i + 1 < len(markers) else len(plain)
        )
        if m.kind == _MARKER_Q:
            questions[m.number] = (m.text_offset, next_offset)
        else:
            answers[m.number] = (m.text_offset, next_offset, m.answer)

    warnings: list[str] = []
    if questions and answers and max(questions) != max(answers):
        warnings.append(
            f"{sec.tag}: max question {max(questions)} vs max answer {max(answers)}"
        )
    elif questions and not answers:
        warnings.append(f"{sec.tag}: no answers detected")

    cards: list[Card] = []
    for qnum in sorted(questions):
        q_start, q_end = questions[qnum]
        q_html, options = _parse_question_chunk(plain, q_start, q_end, spans)

        explanation_html = ""
        correct = None
        if qnum in answers:
            a_start, a_end, correct = answers[qnum]
            explanation_html = _parse_answer_chunk(plain, a_start, a_end, spans)
        else:
            warnings.append(f"{sec.tag}: question {qnum} has no matching answer")

        if len(options) != 5:
            warnings.append(
                f"{sec.tag} Q{qnum}: detected {len(options)} options (expected 5)"
            )
        cards.append(
            Card(
                exam_tag=sec.tag,
                number=qnum,
                question_html=q_html,
                options=options,
                correct=correct,
                explanation_html=explanation_html,
                question_image=image_map.get((_MARKER_Q, qnum)),
                explanation_image=image_map.get((_MARKER_A, qnum)),
                source=f"{sec.title}, שאלה {qnum}",
            )
        )
    return cards, warnings
