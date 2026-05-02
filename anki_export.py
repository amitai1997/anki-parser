"""Package a list of Card objects into an Anki ``.apkg`` archive.

The note type uses interactive multiple-choice buttons: clicking an option turns
it green if correct, red if wrong, and reveals the correct answer in green. On
the back side, if the user didn't pick anything, the correct option is auto-
highlighted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import genanki

from card import Card

# New model ID for the interactive MCQ layout. Once a deck is published with
# this ID, do NOT change it — re-importing would otherwise duplicate notes.
MODEL_ID = 1735192401
DECK_ID = 1735192315


CARD_CSS = """
.card {
  font-family: "Arial Hebrew", "David", "Heebo", Arial, sans-serif;
  font-size: 18px;
  color: #222;
  background: #fafafa;
  padding: 16px;
}
.rtl, .question, .explanation, .correct-banner, .source {
  direction: rtl;
  text-align: right;
  unicode-bidi: plaintext;
}
.source { color: #888; font-size: 13px; margin-bottom: 8px; }
.question { font-size: 19px; line-height: 1.5; margin-bottom: 12px; }

/* Interactive MCQ options */
.mcq { display: flex; flex-direction: column; gap: 8px; margin: 14px 0; }
.mcq .opt {
  display: flex;
  align-items: center;
  width: 100%;
  text-align: right;
  direction: rtl;
  unicode-bidi: plaintext;
  padding: 12px 16px;
  border: 1px solid #d0d0d0;
  border-radius: 8px;
  background: #fff;
  font-family: inherit;
  font-size: 16px;
  color: #222;
  cursor: pointer;
  transition: background-color 0.12s, border-color 0.12s;
  line-height: 1.4;
}
.mcq .opt:hover:not([disabled]) { background: #f0f0f0; }
.mcq .opt[disabled] { cursor: default; }
.mcq .opt .num { font-weight: 600; color: #888; margin-left: 8px; min-width: 1.4em; }
.mcq .opt.correct {
  background: #d4edda;
  border-color: #28a745;
  color: #155724;
}
.mcq .opt.incorrect {
  background: #f8d7da;
  border-color: #dc3545;
  color: #721c24;
}
.mcq .opt.correct .num,
.mcq .opt.incorrect .num { color: inherit; }

.correct-banner {
  color: #137a4a;
  font-weight: bold;
  margin-top: 8px;
  font-size: 17px;
}
.explanation { line-height: 1.6; margin-top: 8px; font-size: 16px; }
img { max-width: 100%; height: auto; margin: 8px 0; border-radius: 4px; }
hr { border: 0; border-top: 1px solid #ccc; margin: 14px 0; }
"""


# Front: question + clickable options. The correct answer index lives in
# data-correct on the .mcq container; the click handler reads it and applies
# .correct / .incorrect classes.
FRONT_TEMPLATE = """
<div class="rtl source">{{Source}}</div>
<div class="rtl question">{{Question}}</div>
{{QuestionImage}}
<div class="mcq" data-correct="{{Correct}}">
{{#Option1}}<button type="button" class="opt" data-idx="1" onclick="ankiPick(this)"><span class="num">1.</span><span class="text">{{Option1}}</span></button>{{/Option1}}
{{#Option2}}<button type="button" class="opt" data-idx="2" onclick="ankiPick(this)"><span class="num">2.</span><span class="text">{{Option2}}</span></button>{{/Option2}}
{{#Option3}}<button type="button" class="opt" data-idx="3" onclick="ankiPick(this)"><span class="num">3.</span><span class="text">{{Option3}}</span></button>{{/Option3}}
{{#Option4}}<button type="button" class="opt" data-idx="4" onclick="ankiPick(this)"><span class="num">4.</span><span class="text">{{Option4}}</span></button>{{/Option4}}
{{#Option5}}<button type="button" class="opt" data-idx="5" onclick="ankiPick(this)"><span class="num">5.</span><span class="text">{{Option5}}</span></button>{{/Option5}}
</div>
<script>
function ankiPick(btn) {
  var box = btn.parentElement;
  var correct = parseInt(box.getAttribute('data-correct'), 10);
  var clicked = parseInt(btn.getAttribute('data-idx'), 10);
  var opts = box.querySelectorAll('.opt');
  for (var i = 0; i < opts.length; i++) {
    var b = opts[i];
    b.setAttribute('disabled', 'disabled');
    var idx = parseInt(b.getAttribute('data-idx'), 10);
    if (idx === correct) b.classList.add('correct');
    else if (idx === clicked) b.classList.add('incorrect');
  }
}
</script>
"""


# Back: re-renders Front + reveal. If the user didn't click anything, auto-mark
# the correct option in green so the answer is always visible on the back side.
BACK_TEMPLATE = """
{{FrontSide}}
<hr>
{{#Correct}}<div class="rtl correct-banner">תשובה נכונה: {{Correct}} — {{CorrectText}}</div>{{/Correct}}
{{#Explanation}}<div class="rtl explanation">{{Explanation}}</div>{{/Explanation}}
{{ExplanationImage}}
<script>
(function() {
  var box = document.querySelector('.mcq');
  if (!box) return;
  if (box.querySelector('.correct, .incorrect')) return;  // user already picked
  var correct = parseInt(box.getAttribute('data-correct'), 10);
  var opts = box.querySelectorAll('.opt');
  for (var i = 0; i < opts.length; i++) {
    var b = opts[i];
    b.setAttribute('disabled', 'disabled');
    if (parseInt(b.getAttribute('data-idx'), 10) === correct) {
      b.classList.add('correct');
    }
  }
})();
</script>
"""


MODEL = genanki.Model(
    model_id=MODEL_ID,
    name="Hebrew Exam Recovery (MCQ)",
    fields=[
        {"name": "Source"},
        {"name": "Question"},
        {"name": "QuestionImage"},
        {"name": "Option1"},
        {"name": "Option2"},
        {"name": "Option3"},
        {"name": "Option4"},
        {"name": "Option5"},
        {"name": "Correct"},
        {"name": "CorrectText"},
        {"name": "Explanation"},
        {"name": "ExplanationImage"},
    ],
    templates=[
        {
            "name": "Q→A",
            "qfmt": FRONT_TEMPLATE.strip(),
            "afmt": BACK_TEMPLATE.strip(),
        }
    ],
    css=CARD_CSS,
)


def _pad(opts: list[str], n: int = 5) -> list[str]:
    """Return exactly `n` slots — empty slots are rendered as nothing thanks to
    the `{{#OptionN}}…{{/OptionN}}` mustache conditional."""
    return list(opts[:n]) + [""] * max(0, n - len(opts))


def build_apkg(
    cards: list[Card],
    media_dir: str | Path,
    deck_name: str = "Hebrew Exam Recovery",
    output_path: Optional[str | Path] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> bytes:
    """Package included cards into a .apkg. Returns bytes; also writes to
    ``output_path`` if provided."""
    deck = genanki.Deck(deck_id=DECK_ID, name=deck_name)
    media_dir = Path(media_dir)
    used_media: set[str] = set()
    n = max(len(cards), 1)
    for idx, c in enumerate(cards):
        if not c.include:
            continue
        correct_str = str(c.correct) if c.correct else ""
        correct_text = c.correct_text
        # Anki scans field content for <img src="..."> to discover which media
        # files to import. A bare filename in the field is NOT recognized.
        q_img_field = f'<img src="{c.question_image}">' if c.question_image else ""
        e_img_field = f'<img src="{c.explanation_image}">' if c.explanation_image else ""
        if c.question_image:
            used_media.add(c.question_image)
        if c.explanation_image:
            used_media.add(c.explanation_image)
        opt1, opt2, opt3, opt4, opt5 = _pad(c.options, 5)
        note = genanki.Note(
            model=MODEL,
            fields=[
                c.source or c.exam_tag,
                c.question_html,
                q_img_field,
                opt1,
                opt2,
                opt3,
                opt4,
                opt5,
                correct_str,
                correct_text,
                c.explanation_html,
                e_img_field,
            ],
            tags=[_sanitize_tag(c.exam_tag)],
        )
        deck.add_note(note)
        if on_progress:
            on_progress(0.8 * (idx + 1) / n, f"Preparing card {idx + 1}/{n}…")

    pkg = genanki.Package(deck)
    pkg.media_files = [str(media_dir / f) for f in used_media if (media_dir / f).exists()]

    out = Path(output_path) if output_path else Path("/tmp/anki-parser-deck.apkg")
    if on_progress:
        on_progress(0.85, "Writing .apkg file…")
    pkg.write_to_file(str(out))
    if on_progress:
        on_progress(1.0, "Done")
    return out.read_bytes()


def _sanitize_tag(tag: str) -> str:
    """Anki tags can't contain spaces; collapse to underscores."""
    return tag.replace(" ", "_")
