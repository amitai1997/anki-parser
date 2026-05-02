# anki-parser

Convert Hebrew medical-exam recovery PDFs (with mixed Hebrew/English text, embedded images, and an answer-key section) into Anki `.apkg` decks.

## Run locally

```bash
uv sync
streamlit run app.py
```

Then upload a PDF, review the parsed cards, optionally toggle/reorder, and download the `.apkg`.

## Headless / CLI

```bash
uv run python cli.py path/to/input.pdf -o output.apkg
# Optional flags:
#   --exam 2019-moed-a       only export one section
#   --include-appendices     include reference appendices as plain notes
```

## Deploy

**Streamlit Community Cloud** (free, "git push → live"):

1. Push this repo to GitHub.
2. Go to <https://share.streamlit.io>, "New app", select the repo, set entry point `app.py`.
3. Click Deploy.

HuggingFace Spaces is a drop-in alternative — same `app.py`.

## Optional: AI fallback

If a section's heuristic parse fails or a card lacks a detected correct answer, the app can call Gemini to suggest. Provide an API key in the sidebar; key lives only in the session.

```bash
uv sync --extra ai
```

## Card format

- **Front**: source tag · question stem (rich HTML, bold/color preserved) · embedded image (if any) · 5 numbered options.
- **Back**: correct option + its text · full explanation · explanation diagram (if any).
- RTL-aware CSS so Hebrew/English mixing renders correctly in Anki desktop, AnkiWeb, and AnkiMobile.

Each card is tagged with its exam section (e.g., `2019-moed-a`) so you can study by year.
