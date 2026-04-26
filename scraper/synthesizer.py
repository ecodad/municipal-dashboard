"""
Step 3b: Synthesizer Agent (Claude Sonnet 4.6).

Reads parsed Markdown agendas from `agendas/markdown/`, classifies and
extracts each agenda item into the canonical `agendas.json` schema, then
moves the source PDF + the Markdown intermediate into
`agendas/archived/`.

Idempotent: if a PDF's items are already present in `agendas.json`
(matched by `Source_File`), it's skipped. Each meeting is processed in
its own API call so a failure on one meeting doesn't lose the others.

Why Sonnet here? The Synthesizer does real reasoning — classifying each
item into one of 10 types based on context, deciding which sub-bullets
warrant their own row, normalizing item numbers. Adaptive thinking with
medium effort consistently produces better classifications than Haiku
in our testing.

Usage:
  python -m scraper.synthesizer
  python -m scraper.synthesizer --dry-run        # don't archive, don't write JSON
  python -m scraper.synthesizer --json-path path # use a custom JSON file
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic

from .event_detail_scrape import (
    EventDetailScrapeError,
    fetch_event_detail,
)

# Load secrets from a project-local .env file if one exists.
# Same precedence rule as parser.py: a real OS env var wins; .env only
# fills in when nothing useful is set. See parser.py for full rationale.
try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]

    _existing = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    load_dotenv(override=not _existing)
except ImportError:
    pass

SYNTHESIZER_MODEL = "claude-sonnet-4-6"
DEFAULT_AGENDAS_JSON = Path("agendas.json")
DEFAULT_PDF_DIR = Path("agendas")
DEFAULT_MARKDOWN_DIR = Path("agendas/markdown")
DEFAULT_ARCHIVE_DIR = Path("agendas/archived")
MAX_OUTPUT_TOKENS = 16000

# Source filenames produced by the cron-driven scraper follow the pattern
# {YYYY-MM-DD}__{occur_id}__{slug}.pdf. The occur_id lets us re-derive
# the original event detail URL on medfordma.org and re-fetch attendance
# info (location, zoom presence, agenda URL, etc.) — captured here as
# the meetings[] record. Manually-uploaded PDFs from the very first
# project setup don't have this pattern, so they get a minimal record
# built from the item fields alone.
_OCCUR_ID_FROM_FILENAME = re.compile(r"__(\d+)__")
_DETAIL_URL_TEMPLATE = (
    "https://www.medfordma.org/about/events-calendar/event-details/"
    "~occur-id/{occur_id}"
)


ALLOWED_ITEM_TYPES = [
    "Resolution",
    "Ordinance",
    "Public Hearing",
    "Vote",
    "Discussion",
    "Communication",
    "Report",
    "Approval/Minutes",
    "Procedural",
    "Other",
]


SYNTHESIZER_SYSTEM_PROMPT = """You are the Synthesizer Agent. Read a Markdown municipal meeting agenda and extract every actionable agenda item into a structured JSON object.

Schema constraints:
- Committee_Name: take from the H1 of the markdown. Strip trailing whitespace/punctuation.
- Meeting_Date: ISO YYYY-MM-DD. Convert any date format the markdown uses.
- Meeting_Time: normalize to "9:30 AM" / "6:30 PM" form (one space, "AM"/"PM" uppercase). Use null if not stated.
- Location: as printed, minus trailing junk. null if not stated.
- Item_Number: prefer formal IDs when shown (e.g. "26-074", "Case #ZON26-000004"). Otherwise use the markdown's numbered position ("1", "2", "2.1" for sub-items).
- Item_Type: pick exactly one from the enum the schema enforces. Heuristics:
    * Resolution           — topic begins with or contains "Resolution"
    * Ordinance            — topic mentions an ordinance amendment or new ordinance
    * Public Hearing       — explicitly listed as a public hearing
    * Approval/Minutes     — about approving minutes or records
    * Procedural           — call to order, roll call, salute to flag, adjournment
    * Communication        — Communications from the Mayor; RFP responses; correspondence
    * Report               — committee reports
    * Vote                 — items explicitly under a "Vote to consider" header (e.g. Retirement Board)
    * Discussion           — labeled as discussion
    * Other                — none of the above
- Agenda_Topic: the most descriptive line for the item (verbatim or lightly cleaned).

Emit one row per agenda item that has a clear identifier (formal ID or numbered position) or substantive content. For items that have a list of named sub-items (e.g. each Case #ZON... in a Zoning Board agenda), emit a row per named sub-item rather than the parent. Skip purely structural sub-bullets that just elaborate on a parent item.

Output strictly the JSON object the schema requires. Do NOT include Source_File — that's added by the caller."""


SYNTHESIZER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Committee_Name": {"type": "string"},
                    "Meeting_Date": {"type": "string"},
                    "Meeting_Time": {"type": ["string", "null"]},
                    "Location": {"type": ["string", "null"]},
                    "Item_Number": {"type": "string"},
                    "Item_Type": {"type": "string", "enum": ALLOWED_ITEM_TYPES},
                    "Agenda_Topic": {"type": "string"},
                },
                "required": [
                    "Committee_Name",
                    "Meeting_Date",
                    "Meeting_Time",
                    "Location",
                    "Item_Number",
                    "Item_Type",
                    "Agenda_Topic",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# ---- Result types --------------------------------------------------------


class SynthesizerError(RuntimeError):
    pass


@dataclass
class MeetingResult:
    source_file: str  # original PDF filename
    md_path: str
    status: str  # synthesized | skipped_existing | failed
    item_count: int = 0
    error: Optional[str] = None


@dataclass
class RunSummary:
    run_at: str
    items_added: int
    meetings_processed: list[dict] = field(default_factory=list)
    new_total_items: int = 0


# ---- Core flow -----------------------------------------------------------


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or key.startswith("sk-ant-REPLACE_ME"):
        raise SynthesizerError(
            "ANTHROPIC_API_KEY is not set (or still on the placeholder). "
            "Edit .env and replace the placeholder with your real key from "
            "https://console.anthropic.com/settings/keys"
        )
    return anthropic.Anthropic()


def _load_agendas_json(path: Path) -> dict:
    """Load existing agendas.json if present, else return a fresh skeleton."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise SynthesizerError(f"{path} is not valid JSON: {err}") from err
        data.setdefault("metadata", {"processed_date": None, "documents_processed": []})
        data.setdefault("items", [])
        data.setdefault("meetings", [])
        return data
    return {
        "metadata": {"processed_date": None, "documents_processed": []},
        "meetings": [],
        "items": [],
    }


def _occur_id_from_filename(name: str) -> Optional[str]:
    """Extract the numeric occur_id from a filename built by run_pipeline.

    Returns None for legacy filenames without the `__{N}__` pattern.
    """
    m = _OCCUR_ID_FROM_FILENAME.search(name)
    return m.group(1) if m else None


def _meeting_record_for_source(
    source_file: str,
    items: list[dict],
    *,
    verbose: bool = False,
) -> Optional[dict]:
    """Build the meetings[] record for one source file.

    Uses the first item for committee/date/time/location, then enriches
    with `event_detail_scrape.fetch_event_detail()` if the filename has
    an occur_id (cron-generated files do; the very first manual PDFs
    from project setup don't).

    The resulting record is what the dashboard consumes — exposing
    presence of Zoom and livestream as booleans rather than the URLs
    themselves, and pointing back to the city event page as the canonical
    "how to attend" anchor.

    Returns None if items is empty (defensive — should not happen in
    practice).
    """
    if not items:
        return None

    sample = items[0]
    occur_id = _occur_id_from_filename(source_file)

    record = {
        "source_file": source_file,
        "occur_id": occur_id,
        "committee_name": sample.get("Committee_Name"),
        "meeting_date": sample.get("Meeting_Date"),
        "meeting_time": sample.get("Meeting_Time"),
        "location": sample.get("Location"),
        "has_zoom": False,
        "has_livestream": False,
        "agenda_url": None,
        "agenda_type": "ARCHIVED",  # default — we have the archived PDF locally
        "detail_url": None,
    }

    if occur_id is None:
        # Legacy file — no detail-page link to enrich from.
        return record

    detail_url = _DETAIL_URL_TEMPLATE.format(occur_id=occur_id)
    record["detail_url"] = detail_url

    try:
        detail = fetch_event_detail(detail_url)
    except EventDetailScrapeError as err:
        # Detail page unreachable (e.g., city purged a past event). Keep
        # the minimal record + detail_url so the dashboard can still
        # link out, but don't enrich.
        if verbose:
            print(
                f"[synth] (warning) couldn't enrich {source_file}: {err}",
                file=sys.stderr,
            )
        return record

    # The detail page is the more authoritative source for attend-info
    # (the city updates it when things change); prefer it over the
    # agenda-extracted location.
    record["location"] = detail.location or record["location"]
    record["has_zoom"] = detail.zoom_url is not None
    record["has_livestream"] = detail.livestream_url is not None
    record["agenda_url"] = detail.agenda_url
    record["agenda_type"] = detail.agenda_type.value
    return record


def _ensure_meeting_record(
    data: dict,
    source_file: str,
    items_for_source: list[dict],
    *,
    verbose: bool = False,
) -> bool:
    """If `data['meetings']` doesn't yet have an entry for source_file,
    build one and append. Returns True if a record was added.
    """
    existing = {m.get("source_file") for m in data.get("meetings", [])}
    if source_file in existing:
        return False
    record = _meeting_record_for_source(
        source_file, items_for_source, verbose=verbose
    )
    if record is None:
        return False
    data.setdefault("meetings", []).append(record)
    return True


def synthesize_markdown(
    md_text: str,
    source_pdf_filename: str,
    *,
    client: anthropic.Anthropic,
) -> list[dict]:
    """Send one markdown agenda to Sonnet and return the structured items list.

    `Source_File` is added programmatically — the model is told not to emit it.
    """
    user_text = (
        f"Source PDF filename (for your reference, do NOT include it in the output): "
        f"{source_pdf_filename}\n\n"
        f"Agenda markdown:\n\n{md_text}"
    )

    response = client.messages.create(
        model=SYNTHESIZER_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SYNTHESIZER_OUTPUT_SCHEMA},
        },
        system=[
            {
                "type": "text",
                "text": SYNTHESIZER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_text}],
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise SynthesizerError(
            f"Synthesizer returned no text for {source_pdf_filename}; "
            "check stop_reason."
        )

    # Use raw_decode rather than json.loads: even with output_config.format
    # constraining the schema, Sonnet occasionally emits a complete JSON
    # object and then keeps generating (commentary, a repeated block, or
    # stray trailing tokens). raw_decode parses the FIRST valid JSON value
    # and returns the position where it ended; we ignore everything after.
    try:
        payload, _end_pos = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as err:
        raise SynthesizerError(
            f"Synthesizer output for {source_pdf_filename} isn't valid JSON: {err}"
        ) from err

    items = payload.get("items", [])
    for item in items:
        item["Source_File"] = source_pdf_filename
    return items


def _move_to_archive(pdf_path: Path, md_path: Path, archive_dir: Path) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        shutil.move(str(pdf_path), str(archive_dir / pdf_path.name))
    if md_path.exists():
        shutil.move(str(md_path), str(archive_dir / md_path.name))


def _resolve_pdf_for_md(md_path: Path, pdf_dir: Path) -> Optional[Path]:
    """Find the source PDF for a markdown file. Returns None if missing."""
    candidate = pdf_dir / f"{md_path.stem}.pdf"
    return candidate if candidate.exists() else None


def synthesize_directory(
    *,
    markdown_dir: Path = DEFAULT_MARKDOWN_DIR,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    agendas_json: Path = DEFAULT_AGENDAS_JSON,
    dry_run: bool = False,
    verbose: bool = True,
) -> RunSummary:
    """Process every .md in markdown_dir, merge into agendas_json, archive sources.
    """
    markdown_dir = Path(markdown_dir)
    pdf_dir = Path(pdf_dir)
    archive_dir = Path(archive_dir)
    agendas_json = Path(agendas_json)

    md_files = sorted(p for p in markdown_dir.glob("*.md") if p.is_file())
    if not md_files:
        if verbose:
            print(f"[synth] no markdown files in {markdown_dir}", file=sys.stderr)
        return RunSummary(run_at=date.today().isoformat(), items_added=0)

    data = _load_agendas_json(agendas_json)
    already_processed_files = {it.get("Source_File") for it in data["items"]}

    client = _client()
    meeting_results: list[MeetingResult] = []
    items_added = 0

    for md_path in md_files:
        pdf_path = _resolve_pdf_for_md(md_path, pdf_dir)
        # Source_File is the PDF filename if it exists alongside, otherwise
        # fall back to the md stem + .pdf so dedup still works.
        source_filename = (
            pdf_path.name if pdf_path else f"{md_path.stem}.pdf"
        )

        if source_filename in already_processed_files:
            if verbose:
                print(f"[synth] = {source_filename} (already in agendas.json)", file=sys.stderr)
            # Even for already-processed items, ensure there's a
            # meetings[] record (e.g. on the first run after schema
            # migration) so the dashboard renders consistently.
            if not dry_run:
                items_for_src = [
                    it for it in data["items"] if it.get("Source_File") == source_filename
                ]
                if _ensure_meeting_record(data, source_filename, items_for_src, verbose=verbose):
                    if verbose:
                        print(f"[synth]   + meetings[] record (backfill)", file=sys.stderr)
                if pdf_path is not None:
                    _move_to_archive(pdf_path, md_path, archive_dir)
            meeting_results.append(
                MeetingResult(
                    source_file=source_filename,
                    md_path=str(md_path),
                    status="skipped_existing",
                )
            )
            continue

        if verbose:
            print(f"[synth] + {source_filename}", file=sys.stderr)
        try:
            md_text = md_path.read_text(encoding="utf-8")
            new_items = synthesize_markdown(
                md_text, source_filename, client=client
            )
        except (SynthesizerError, anthropic.APIError) as err:
            print(f"[synth] X {source_filename}: {err}", file=sys.stderr)
            meeting_results.append(
                MeetingResult(
                    source_file=source_filename,
                    md_path=str(md_path),
                    status="failed",
                    error=str(err),
                )
            )
            continue

        if not dry_run:
            data["items"].extend(new_items)
            data["metadata"]["documents_processed"].append(
                {
                    "filename": source_filename,
                    "status": "parsed",
                    "item_count": len(new_items),
                }
            )
            # Build the meetings[] record (re-fetches the detail page
            # for occur_id-tagged files; minimal record for legacy).
            _ensure_meeting_record(data, source_filename, new_items, verbose=verbose)
            if pdf_path is not None:
                _move_to_archive(pdf_path, md_path, archive_dir)
        items_added += len(new_items)
        meeting_results.append(
            MeetingResult(
                source_file=source_filename,
                md_path=str(md_path),
                status="synthesized",
                item_count=len(new_items),
            )
        )

    if not dry_run:
        data["metadata"]["processed_date"] = date.today().isoformat()
        data["items"].sort(
            key=lambda i: (
                i.get("Meeting_Date", ""),
                i.get("Source_File", ""),
                i.get("Item_Number", ""),
            )
        )
        data["meetings"].sort(
            key=lambda m: (
                m.get("meeting_date", ""),
                m.get("committee_name", ""),
            )
        )
        agendas_json.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return RunSummary(
        run_at=date.today().isoformat(),
        items_added=items_added,
        meetings_processed=[asdict(r) for r in meeting_results],
        new_total_items=len(data["items"]),
    )


def backfill_meetings(
    agendas_json: Path = DEFAULT_AGENDAS_JSON,
    *,
    verbose: bool = True,
) -> int:
    """One-shot backfill: populate `meetings[]` for items already in agendas.json.

    Used after schema migration. For each unique `Source_File` in
    `items[]` that doesn't yet have a meetings[] entry, build one — for
    cron-generated files the EventDetail is re-fetched to enrich, for
    legacy files a minimal record is built from item fields alone.

    Returns the number of meeting records added.
    """
    data = _load_agendas_json(agendas_json)
    existing = {m.get("source_file") for m in data.get("meetings", [])}
    source_files = sorted({it.get("Source_File") for it in data.get("items", [])})

    added = 0
    for src in source_files:
        if not src or src in existing:
            continue
        items_for_src = [it for it in data["items"] if it.get("Source_File") == src]
        record = _meeting_record_for_source(src, items_for_src, verbose=verbose)
        if record is None:
            continue
        data.setdefault("meetings", []).append(record)
        existing.add(src)
        added += 1
        if verbose:
            mark = "rich" if record.get("occur_id") else "minimal"
            print(
                f"[backfill] + {src}  ({mark}; agenda_type={record.get('agenda_type')})",
                file=sys.stderr,
            )

    if added > 0:
        data["meetings"].sort(
            key=lambda m: (
                m.get("meeting_date", ""),
                m.get("committee_name", ""),
            )
        )
        agendas_json.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return added


# ---- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synthesize parsed agenda markdown into agendas.json using Claude Sonnet 4.6, "
            "then archive the source PDFs and Markdown."
        )
    )
    parser.add_argument(
        "--markdown-dir",
        type=Path,
        default=DEFAULT_MARKDOWN_DIR,
        help=f"Directory of .md files (default: {DEFAULT_MARKDOWN_DIR}).",
    )
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=DEFAULT_PDF_DIR,
        help=f"Directory of source PDFs (default: {DEFAULT_PDF_DIR}).",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=DEFAULT_ARCHIVE_DIR,
        help=f"Where to move processed PDFs + Markdown (default: {DEFAULT_ARCHIVE_DIR}).",
    )
    parser.add_argument(
        "--json-path",
        type=Path,
        default=DEFAULT_AGENDAS_JSON,
        help=f"Path to agendas.json (default: {DEFAULT_AGENDAS_JSON}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Synthesize and report, but don't write JSON or move files.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit run summary as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--backfill-meetings",
        action="store_true",
        help=(
            "One-time pass: don't synthesize anything new. Just populate "
            "the meetings[] array in agendas.json from existing items[] "
            "(re-fetches event detail pages for occur_id-tagged files). "
            "Safe to re-run; only adds missing meeting records."
        ),
    )
    args = parser.parse_args(argv)

    if args.backfill_meetings:
        added = backfill_meetings(args.json_path, verbose=not args.json)
        if args.json:
            json.dump({"meetings_added": added}, sys.stdout)
            sys.stdout.write("\n")
        else:
            print(f"\nBackfill complete: {added} meeting record(s) added.")
        return 0

    try:
        summary = synthesize_directory(
            markdown_dir=args.markdown_dir,
            pdf_dir=args.pdf_dir,
            archive_dir=args.archive_dir,
            agendas_json=args.json_path,
            dry_run=args.dry_run,
            verbose=not args.json,
        )
    except SynthesizerError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(asdict(summary), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(
            f"\nSynthesizer run @ {summary.run_at} — "
            f"items added: {summary.items_added}, "
            f"new total items in {args.json_path}: {summary.new_total_items}"
        )
        for m in summary.meetings_processed:
            marker = {
                "synthesized": " + ",
                "skipped_existing": " = ",
                "failed": " X ",
            }.get(m["status"], "   ")
            print(f"{marker}{m['source_file']}  ({m['item_count']} items)")
            if m["status"] == "failed":
                print(f"      ERROR: {m['error']}")

    failed = sum(
        1 for m in summary.meetings_processed if m["status"] == "failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
