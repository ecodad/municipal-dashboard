"""
Step 3a: Parser Agent (Claude Haiku 4.5).

Converts each PDF in `agendas/` into clean, structured Markdown stored
in `agendas/markdown/`. The Markdown intermediate is consumed by the
Synthesizer in step 3b.

Why split parse from synthesize? Two reasons:
  1. Cost — Haiku is ~5x cheaper than Sonnet on input tokens. PDFs are
     large; the verbatim transcription job doesn't need Sonnet's
     reasoning.
  2. Validation — having a human-readable Markdown intermediate makes
     the pipeline auditable. If the JSON looks wrong, we can inspect
     the Markdown to see whether the issue is in extraction or
     classification.

Usage:
  python -m scraper.parser                    # parse all PDFs in agendas/
  python -m scraper.parser --force            # re-parse even if .md exists
  python -m scraper.parser <path-to-pdf>      # parse one specific PDF
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic

PARSER_MODEL = "claude-haiku-4-5"
DEFAULT_PDF_DIR = Path("agendas")
DEFAULT_OUTPUT_DIR = Path("agendas/markdown")
MAX_OUTPUT_TOKENS = 8000  # Haiku output cap; agendas are short

# Minimum bytes of useful Markdown we expect from a real agenda.
# Below this is almost certainly an extraction failure ("This PDF
# appears to contain no text" placeholder).
MIN_MARKDOWN_BYTES = 200


PARSER_SYSTEM_PROMPT = """You are the Parser Agent in a 2-stage municipal-agenda processing pipeline. Your job is fast, faithful PDF -> Markdown conversion. You do NOT extract structured data — that is the Synthesizer's job.

Convert each input PDF to clean, well-structured Markdown:

- Use `# {Committee/Board Name}` as the H1 (e.g. "# Medford Retirement Board")
- Immediately under the H1, output a short bullet block with meeting metadata:
  - **Date:** as printed
  - **Time:** as printed
  - **Location:** as printed
- Then `## Agenda` (or a more specific subsection header if the PDF uses one)
- Each agenda item as a numbered list item; preserve sub-items via indentation
- Strip page artifacts (received stamps, page numbers, signature scrawls, headers/footers, watermark dates)
- Preserve the EXACT wording of agenda items; do NOT paraphrase

Output ONLY the Markdown content. No preamble, no commentary, no code fences, no closing remarks."""


class ParserError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParseResult:
    pdf_path: Path
    md_path: Path
    bytes_written: int
    skipped: bool  # true when --skip-existing kept an existing .md


def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ParserError(
            "ANTHROPIC_API_KEY is not set. "
            "Set it (e.g. `export ANTHROPIC_API_KEY=sk-ant-...`) before running the parser."
        )
    return anthropic.Anthropic()


def parse_pdf(
    pdf_path: Path,
    output_dir: Path,
    *,
    client: Optional[anthropic.Anthropic] = None,
) -> ParseResult:
    """Convert one PDF to Markdown and write it under output_dir/."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    pdf_bytes = pdf_path.read_bytes()
    if not pdf_bytes.startswith(b"%PDF"):
        raise ParserError(
            f"{pdf_path.name} doesn't look like a PDF (no %PDF magic bytes)."
        )
    encoded = base64.standard_b64encode(pdf_bytes).decode("ascii")

    if client is None:
        client = _client()

    response = client.messages.create(
        model=PARSER_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[
            {
                "type": "text",
                "text": PARSER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Convert this municipal agenda PDF to structured Markdown "
                            "per the rules in the system prompt. Output Markdown only."
                        ),
                    },
                ],
            }
        ],
    )

    markdown = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if len(markdown.encode("utf-8")) < MIN_MARKDOWN_BYTES:
        raise ParserError(
            f"Parser output for {pdf_path.name} is only {len(markdown)} chars — "
            "PDF likely failed extraction (image-only PDF, scanned scan, or empty)."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{pdf_path.stem}.md"
    md_path.write_text(markdown + "\n", encoding="utf-8")
    return ParseResult(
        pdf_path=pdf_path,
        md_path=md_path,
        bytes_written=md_path.stat().st_size,
        skipped=False,
    )


def parse_directory(
    pdf_dir: Path = DEFAULT_PDF_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    skip_existing: bool = True,
    verbose: bool = True,
) -> list[ParseResult]:
    """Parse every top-level PDF in pdf_dir; write to output_dir."""
    pdf_dir = Path(pdf_dir)
    output_dir = Path(output_dir)

    pdfs = sorted(p for p in pdf_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        if verbose:
            print(f"[parser] no PDFs found in {pdf_dir}", file=sys.stderr)
        return []

    client = _client()
    results: list[ParseResult] = []
    for pdf in pdfs:
        target_md = output_dir / f"{pdf.stem}.md"
        if skip_existing and target_md.exists():
            if verbose:
                print(f"[parser] = {pdf.name} (markdown already present)", file=sys.stderr)
            results.append(
                ParseResult(
                    pdf_path=pdf, md_path=target_md,
                    bytes_written=target_md.stat().st_size, skipped=True,
                )
            )
            continue
        try:
            if verbose:
                print(f"[parser] + {pdf.name}", file=sys.stderr)
            res = parse_pdf(pdf, output_dir, client=client)
            results.append(res)
        except (ParserError, anthropic.APIError) as err:
            print(f"[parser] X {pdf.name}: {err}", file=sys.stderr)
    return results


# ---- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert each PDF in agendas/ to clean Markdown using Claude Haiku 4.5. "
            "Output goes to agendas/markdown/."
        )
    )
    parser.add_argument(
        "pdf_path",
        nargs="?",
        type=Path,
        help="Optional path to a single PDF. If omitted, parse every PDF in --pdf-dir.",
    )
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=DEFAULT_PDF_DIR,
        help=f"Directory of PDFs to parse (default: {DEFAULT_PDF_DIR}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write Markdown files (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-parse even if a .md file already exists.",
    )
    args = parser.parse_args(argv)

    try:
        if args.pdf_path:
            res = parse_pdf(args.pdf_path, args.output_dir)
            print(f"OK: {res.pdf_path.name} -> {res.md_path} ({res.bytes_written:,} bytes)")
            return 0
        results = parse_directory(
            pdf_dir=args.pdf_dir,
            output_dir=args.output_dir,
            skip_existing=not args.force,
        )
    except ParserError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    parsed = sum(1 for r in results if not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    print(f"Parsed {parsed} PDFs; skipped {skipped} (already present).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
