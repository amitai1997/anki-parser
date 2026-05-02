"""Headless converter: PDF in, .apkg out. No UI, no preview."""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from anki_export import build_apkg
from parser import parse_pdf


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert a Hebrew exam-recovery PDF to an Anki .apkg deck."
    )
    p.add_argument("pdf", type=Path, help="Path to the source PDF")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output.apkg"),
        help="Path to write the resulting .apkg (default: output.apkg)",
    )
    p.add_argument(
        "--deck-name",
        default="Hebrew Exam Recovery",
        help="Deck name shown in Anki",
    )
    p.add_argument(
        "--exam",
        action="append",
        default=[],
        help="Limit output to one or more exam tags (e.g. 2019-moed-a). Repeat for multiple.",
    )
    p.add_argument(
        "--include-appendices",
        action="store_true",
        help="Include reference appendices as plain notes",
    )
    p.add_argument(
        "--media-dir",
        type=Path,
        default=None,
        help="Where to extract images. Defaults to a temp dir.",
    )
    args = p.parse_args(argv)

    if not args.pdf.exists():
        print(f"error: file not found: {args.pdf}", file=sys.stderr)
        return 2

    media_ctx = (
        tempfile.TemporaryDirectory(prefix="anki-parser-")
        if args.media_dir is None
        else None
    )
    media_dir = Path(media_ctx.name) if media_ctx else args.media_dir
    try:
        result = parse_pdf(
            args.pdf,
            media_dir,
            include_appendices=args.include_appendices,
        )
        cards = result.cards
        if args.exam:
            wanted = set(args.exam)
            cards = [c for c in cards if c.exam_tag in wanted]
        if not cards:
            print("warning: no cards selected for export", file=sys.stderr)
        build_apkg(
            cards,
            media_dir,
            deck_name=args.deck_name,
            output_path=args.output,
        )
        print(f"Wrote {args.output} ({len(cards)} cards)")
        if result.warnings:
            print(f"Parse warnings ({len(result.warnings)}):", file=sys.stderr)
            for w in result.warnings[:50]:
                print(f"  {w}", file=sys.stderr)
            if len(result.warnings) > 50:
                print(f"  …and {len(result.warnings) - 50} more", file=sys.stderr)
    finally:
        if media_ctx:
            media_ctx.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
