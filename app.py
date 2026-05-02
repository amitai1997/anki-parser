"""Streamlit UI: upload PDF → preview/edit cards → download .apkg.

Designed for personal use: minimalist, single-page, no auth, no persistence.
"""
from __future__ import annotations

import html
import io
import re
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from anki_export import build_apkg
from card import Card
from parser import parse_pdf

st.set_page_config(page_title="anki-parser", page_icon="🗂", layout="wide")


# RTL preview: expander summaries + radio option labels + text areas inside cards.
st.markdown(
    """
    <style>
    [data-testid="stExpander"] summary { direction: rtl; text-align: right; }
    [data-testid="stExpander"] summary p {
        direction: rtl; unicode-bidi: plaintext; text-align: right;
    }
    /* Radio labels inside card expanders */
    [data-testid="stExpander"] [data-testid="stRadio"] label {
        direction: rtl; text-align: right; width: 100%;
    }
    [data-testid="stExpander"] [data-testid="stRadio"] label p {
        direction: rtl; unicode-bidi: plaintext; text-align: right;
    }
    [data-testid="stExpander"] [data-testid="stRadio"] > label:first-child {
        text-align: right;
    }
    /* Text areas and inputs in edit mode */
    [data-testid="stExpander"] textarea {
        direction: rtl; text-align: right;
    }
    [data-testid="stExpander"] [data-testid="stTextInput"] input {
        direction: rtl; text-align: right;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _ensure_session_state():
    st.session_state.setdefault("cards", [])
    st.session_state.setdefault("warnings", [])
    st.session_state.setdefault("media_dir", None)
    st.session_state.setdefault("filename", None)
    st.session_state.setdefault("exam_tags", [])
    st.session_state.setdefault("page", 0)
    st.session_state.setdefault("last_filter_key", None)


_ensure_session_state()


st.title("anki-parser")
st.caption("Hebrew exam-recovery PDF → Anki .apkg deck")

# ------------- Sidebar: actions -------------

with st.sidebar:
    st.header("Settings")
    deck_name = st.text_input("Deck name", value="Hebrew Exam Recovery")
    include_appendices = st.checkbox(
        "Include reference appendices",
        value=False,
        help="Adds an 'appendix' note per reference section. Off by default.",
    )
    st.divider()
    st.markdown(
        "**Workflow**\n\n"
        "1. Upload a PDF.\n"
        "2. Review cards. Edit, reorder, or remove as needed.\n"
        "3. Download `.apkg` and import into Anki."
    )


# ------------- Step 1: upload -------------

uploaded = st.file_uploader("Upload a PDF", type=["pdf"], accept_multiple_files=False)

if uploaded is not None and (
    st.session_state.filename != uploaded.name
    or not st.session_state.cards
):
    with st.status("Parsing PDF…", expanded=True) as status:
        _prog = st.progress(0.0)
        _msg = st.empty()
        media_dir = Path(tempfile.mkdtemp(prefix="anki-parser-media-"))
        tmp_pdf = Path(tempfile.mkdtemp(prefix="anki-parser-pdf-")) / uploaded.name
        tmp_pdf.write_bytes(uploaded.read())

        def _parse_progress(frac: float, msg: str) -> None:
            _prog.progress(min(frac, 1.0))
            _msg.text(msg)

        result = parse_pdf(
            tmp_pdf, media_dir,
            include_appendices=include_appendices,
            on_progress=_parse_progress,
        )
        st.session_state.cards = result.cards
        st.session_state.warnings = result.warnings
        st.session_state.media_dir = str(media_dir)
        st.session_state.filename = uploaded.name
        st.session_state.exam_tags = result.exam_tags
        status.update(
            label=f"Parsed {len(result.cards)} cards "
            f"across {len(result.exam_tags)} sections "
            f"({len(result.warnings)} warnings)",
            state="complete",
        )

cards: list[Card] = st.session_state.cards
warnings: list[str] = st.session_state.warnings
media_dir: str | None = st.session_state.media_dir

if not cards:
    st.info("Upload a PDF to begin.")
    st.stop()


# ------------- Step 2: preview / edit -------------

# Filter bar
exam_tags = sorted({c.exam_tag for c in cards})
left, mid, right = st.columns([2, 2, 1])
with left:
    selected_tags = st.multiselect(
        "Exam sections",
        options=exam_tags,
        default=exam_tags,
    )
    if len(exam_tags) > 1:
        split_per_section = st.checkbox(
            "Split into separate decks per section (export as .zip)",
            value=False,
            help=(
                "When checked, the build produces one .apkg per exam section "
                "bundled into a single .zip download. Useful for big PDFs "
                "where you want each section as a standalone deck."
            ),
        )
    else:
        split_per_section = False
with mid:
    only_unresolved = st.checkbox(
        "Show only cards needing review",
        value=False,
        help="Cards without a detected correct answer or with non-5 option counts.",
    )
with right:
    included_count = sum(1 for c in cards if c.include and c.exam_tag in selected_tags)
    st.metric("Included", f"{included_count} / {len(cards)}")

if warnings:
    with st.expander(f"⚠ {len(warnings)} parser warnings", expanded=False):
        for w in warnings:
            st.markdown(f"- {w}")

st.divider()


# ---- Helper functions (defined before first use) ----

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _plain(s: str) -> str:
    """Strip HTML tags and decode HTML entities — for edit field defaults."""
    return html.unescape(_strip_html(s))


def _excerpt(html_str: str, n: int) -> str:
    plain = _strip_html(html_str)
    return plain[:n] + ("…" if len(plain) > n else "")


def _to_html(plain: str) -> str:
    """Convert user-edited plain text back to HTML (escape + line breaks)."""
    return html.escape(plain).replace("\n", "<br>")



def _render_card(card: Card):
    is_unresolved = card.correct is None or len(card.options) != 5
    icon = "🟡" if is_unresolved else ("✅" if card.include else "⚫")
    ans = f"ans:{card.correct}" if card.correct else "ans:?"
    title = (
        f"{icon} **{card.exam_tag}** · Q{card.number} · {ans} · "
        f"{_excerpt(card.question_html, 80)}"
    )
    with st.expander(title, expanded=False):
        ctrl_l, ctrl_r = st.columns([2, 1])
        with ctrl_l:
            card.include = st.checkbox(
                "Include in deck",
                value=card.include,
                key=f"include-{card.uid}",
            )
        with ctrl_r:
            edit_mode = st.toggle("✏️ Edit", key=f"edit-{card.uid}")

        st.markdown("---")

        if edit_mode:
            st.caption(
                "Editing replaces formatted text with plain text — "
                "bold/color from the source PDF will be lost."
            )
            new_q = st.text_area(
                "Question",
                value=_plain(card.question_html),
                key=f"q-{card.uid}",
                height=120,
            )
            card.question_html = _to_html(new_q)

            st.write("**Options**")
            opts = list(card.options) + [""] * max(0, 5 - len(card.options))
            opts = opts[:5]
            new_opts: list[str] = []
            for i, opt in enumerate(opts):
                new_opts.append(
                    st.text_input(
                        f"Option {i + 1}",
                        value=_plain(opt),
                        key=f"opt-{card.uid}-{i}",
                        label_visibility="collapsed",
                        placeholder=f"Option {i + 1}",
                    )
                )
            card.options = [html.escape(o) for o in new_opts if o.strip()]

            new_e = st.text_area(
                "Explanation",
                value=_plain(card.explanation_html),
                key=f"e-{card.uid}",
                height=120,
            )
            card.explanation_html = _to_html(new_e)
        else:
            st.markdown(
                f'<div dir="rtl" style="font-size:16px;text-align:right">'
                f"{card.question_html}</div>",
                unsafe_allow_html=True,
            )
            if card.question_image and media_dir:
                p = Path(media_dir) / card.question_image
                if p.exists():
                    st.image(str(p), width=400)

        if card.options:
            option_labels = [
                f"{i + 1}. {_plain(o)[:120]}"
                for i, o in enumerate(card.options)
            ]
            n_opts = len(card.options)
            current_index = (
                min(card.correct - 1, n_opts - 1)
                if card.correct and 1 <= card.correct <= n_opts
                else 0
            )
            radio_key = f"correct-{card.uid}"
            if st.session_state.get(radio_key, 0) >= n_opts:
                st.session_state[radio_key] = 0
            choice = st.radio(
                "Correct answer",
                options=list(range(n_opts)),
                format_func=lambda i: option_labels[i],
                index=current_index,
                key=radio_key,
                horizontal=False,
            )
            card.correct = choice + 1

        if not edit_mode and card.explanation_html:
            st.markdown(
                '<div dir="rtl" style="font-size:14px;color:#444;'
                'border-right:3px solid #137a4a;padding-right:8px;'
                'margin-top:8px;text-align:right">'
                f"{card.explanation_html}</div>",
                unsafe_allow_html=True,
            )
        if not edit_mode and card.explanation_image and media_dir:
            p = Path(media_dir) / card.explanation_image
            if p.exists():
                st.image(str(p), width=400)


CARDS_PER_PAGE = 25

filtered = [
    c for c in cards
    if c.exam_tag in selected_tags
    and (not only_unresolved or c.correct is None or len(c.options) != 5)
]

# Reset to page 0 whenever the filter changes.
filter_key = (tuple(selected_tags), only_unresolved)
if st.session_state["last_filter_key"] != filter_key:
    st.session_state["page"] = 0
    st.session_state["last_filter_key"] = filter_key

total_pages = max(1, (len(filtered) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE)
page = min(st.session_state["page"], total_pages - 1)
st.session_state["page"] = page

start = page * CARDS_PER_PAGE
end = min(start + CARDS_PER_PAGE, len(filtered))

# Pagination controls
pg_l, pg_c, pg_r = st.columns([1, 3, 1])
with pg_l:
    if st.button("← Prev", disabled=(page == 0), use_container_width=True):
        st.session_state["page"] -= 1
        st.rerun()
with pg_c:
    st.caption(
        f"Page {page + 1} of {total_pages} · "
        f"Cards {start + 1}–{end} of {len(filtered)}"
    )
with pg_r:
    if st.button("Next →", disabled=(page >= total_pages - 1), use_container_width=True):
        st.session_state["page"] += 1
        st.rerun()

for c in filtered[start:end]:
    _render_card(c)

# Bottom pagination (convenient when scrolled down)
if total_pages > 1:
    b_l, b_c, b_r = st.columns([1, 3, 1])
    with b_l:
        if st.button("← Prev", key="prev-bottom", disabled=(page == 0), use_container_width=True):
            st.session_state["page"] -= 1
            st.rerun()
    with b_c:
        st.caption(f"Page {page + 1} of {total_pages}")
    with b_r:
        if st.button("Next →", key="next-bottom", disabled=(page >= total_pages - 1), use_container_width=True):
            st.session_state["page"] += 1
            st.rerun()


# ------------- Step 3: build & download -------------

st.divider()

build_col_l, build_col_r = st.columns([2, 1])
with build_col_l:
    st.subheader("Build deck")
    st.caption(
        f"{included_count} cards will be exported. "
        "Use the Include checkboxes above to control inclusion."
    )
with build_col_r:
    btn_label = "Build .zip" if split_per_section else "Build .apkg"
    if st.button(btn_label, type="primary", use_container_width=True):
        cards_to_export = [
            c for c in cards if c.include and c.exam_tag in selected_tags
        ]
        tags_in_export = sorted({c.exam_tag for c in cards_to_export})
        do_split = split_per_section and len(tags_in_export) > 1

        with st.status(
            "Building decks…" if do_split else "Building deck…",
            expanded=True,
        ) as _build_status:
            _bprog = st.progress(0.0)
            _bmsg = st.empty()

            def _build_progress(frac: float, msg: str) -> None:
                _bprog.progress(min(frac, 1.0))
                _bmsg.text(msg)

            if do_split:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, tag in enumerate(tags_in_export):
                        section_cards = [c for c in cards_to_export if c.exam_tag == tag]
                        section_out = Path(tempfile.mkdtemp()) / f"{tag}.apkg"
                        section_idx = i  # capture for closure

                        def _section_progress(frac: float, msg: str, _i=section_idx, _t=tag) -> None:
                            overall = (_i + frac) / len(tags_in_export)
                            _bprog.progress(min(overall, 1.0))
                            _bmsg.text(f"[{_i + 1}/{len(tags_in_export)}] {_t}: {msg}")

                        apkg_bytes = build_apkg(
                            section_cards,
                            media_dir or "/tmp",
                            deck_name=f"{deck_name} - {tag}",
                            output_path=section_out,
                            on_progress=_section_progress,
                        )
                        safe_name = re.sub(r"[^A-Za-z0-9._\-]+", "_", tag).strip("_") or "section"
                        zf.writestr(f"{safe_name}.apkg", apkg_bytes)
                data = buf.getvalue()
                st.session_state["last_apkg"] = data
                st.session_state["last_apkg_size"] = len(data)
                st.session_state["last_apkg_count"] = len(cards_to_export)
                st.session_state["last_apkg_is_zip"] = True
                st.session_state["last_apkg_section_count"] = len(tags_in_export)
                _build_status.update(
                    label=(
                        f"Built {len(tags_in_export)} decks "
                        f"({len(cards_to_export)} cards) into a .zip"
                    ),
                    state="complete",
                )
            else:
                output = Path(tempfile.mkdtemp()) / "deck.apkg"
                data = build_apkg(
                    cards_to_export,
                    media_dir or "/tmp",
                    deck_name=deck_name,
                    output_path=output,
                    on_progress=_build_progress,
                )
                st.session_state["last_apkg"] = data
                st.session_state["last_apkg_size"] = len(data)
                st.session_state["last_apkg_count"] = len(cards_to_export)
                st.session_state["last_apkg_is_zip"] = False
                st.session_state.pop("last_apkg_section_count", None)
                _build_status.update(
                    label=f"Built {len(cards_to_export)} cards",
                    state="complete",
                )

if "last_apkg" in st.session_state:
    is_zip = st.session_state.get("last_apkg_is_zip", False)
    section_count = st.session_state.get("last_apkg_section_count")
    size_mb = st.session_state["last_apkg_size"] / 1024 / 1024
    if is_zip and section_count:
        st.success(
            f"Built {section_count} decks ({st.session_state['last_apkg_count']} cards, "
            f"{size_mb:.1f} MB)"
        )
    else:
        st.success(
            f"Built {st.session_state['last_apkg_count']} cards ({size_mb:.1f} MB)"
        )
    ext = "zip" if is_zip else "apkg"
    mime = "application/zip" if is_zip else "application/octet-stream"
    st.download_button(
        f"Download .{ext}",
        data=st.session_state["last_apkg"],
        file_name=f"{deck_name.replace(' ', '_')}.{ext}",
        mime=mime,
        type="primary",
    )
