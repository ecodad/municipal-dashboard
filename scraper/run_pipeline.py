"""
Step 2e: orchestrate the full scraper pipeline.

For a given lookahead window, this:

  1. Calls Step 1 (calendar_scrape) to list upcoming meetings.
  2. For each meeting, calls Step 2b (event_detail_scrape) to discover
     the agenda URL, source type, location, and Zoom info.
  3. Dispatches to Step 2a (civicclerk_download) or 2c/2d
     (google_download) by source type and saves the agenda PDF into the
     destination directory.
  4. Skips meetings whose agenda is already present in `dest_dir/` or
     `dest_dir/archived/` (idempotency).
  5. Writes a JSON run summary at `dest_dir/.last_scraper_run.json`.

Designed so the existing Parser → Synthesizer pipeline can pick up the
freshly-downloaded PDFs from `agendas/` on its next run without any
code changes. The hidden `.last_scraper_run.json` file is ignored by
the Parser (which only reads `*.pdf`/`*.docx`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .calendar_scrape import Meeting, fetch_meetings
from .civicclerk_download import (
    CivicClerkDownloadError,
    download_agenda as download_civicclerk,
)
from .event_detail_scrape import (
    AgendaType,
    EventDetailScrapeError,
    fetch_event_detail,
)
from .google_download import (
    GoogleDownloadError,
    download_google_agenda,
)
from .parser import (
    DEFAULT_OUTPUT_DIR as DEFAULT_MARKDOWN_DIR,
    ParserError,
    parse_directory,
)
from .synthesizer import (
    DEFAULT_AGENDAS_JSON,
    DEFAULT_ARCHIVE_DIR,
    SynthesizerError,
    synthesize_directory,
)


DEFAULT_DEST = Path("agendas")
RUN_SUMMARY_FILENAME = ".last_scraper_run.json"
SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---- Result types --------------------------------------------------------


class Status:
    DOWNLOADED = "downloaded"
    SKIPPED_EXISTING = "skipped_existing"
    WOULD_DOWNLOAD = "would_download"  # dry-run only
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass
class MeetingResult:
    occur_id: str
    title: str
    start: str
    detail_url: str
    agenda_url: Optional[str]
    agenda_type: str
    location: Optional[str]
    zoom_url: Optional[str]
    livestream_url: Optional[str]
    status: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    error: Optional[str] = None


@dataclass
class RunSummary:
    run_at: str
    as_of: str
    window_end: str
    lookahead_days: int
    dest_dir: str
    dry_run: bool
    counts: dict = field(default_factory=dict)
    meetings: list[dict] = field(default_factory=list)


# ---- Helpers -------------------------------------------------------------


def _slugify(s: str, max_len: int = 60) -> str:
    s = SLUG_RE.sub("-", s.lower()).strip("-")
    return s[:max_len].rstrip("-") or "meeting"


def _filename_stem_for(meeting: Meeting) -> str:
    """Stable, sortable, human-readable name."""
    date_part = meeting.start[:10]  # YYYY-MM-DD slice from ISO timestamp
    slug = _slugify(meeting.title)
    return f"{date_part}__{meeting.occur_id}__{slug}"


def _already_have(occur_id: str, dest_dir: Path) -> Optional[Path]:
    """Look for any file in dest_dir or dest_dir/archived whose name
    contains this occur_id. Returns the first match, or None.
    """
    needle = f"__{occur_id}__"
    for d in (dest_dir, dest_dir / "archived"):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and needle in p.name:
                return p
    return None


def _empty_result(meeting: Meeting, status: str, *, error: str | None = None) -> MeetingResult:
    return MeetingResult(
        occur_id=meeting.occur_id,
        title=meeting.title,
        start=meeting.start,
        detail_url=meeting.detail_url,
        agenda_url=None,
        agenda_type=AgendaType.MISSING.value,
        location=None,
        zoom_url=None,
        livestream_url=None,
        status=status,
        error=error,
    )


# ---- Per-meeting processing ---------------------------------------------


def process_meeting(
    meeting: Meeting,
    dest_dir: Path,
    *,
    dry_run: bool = False,
) -> MeetingResult:
    """Resolve one meeting's agenda end-to-end.

    Never raises for per-meeting issues; returns a MeetingResult whose
    `status`/`error` fields describe what happened.
    """
    try:
        detail = fetch_event_detail(meeting.detail_url)
    except EventDetailScrapeError as err:
        return _empty_result(meeting, Status.FAILED, error=str(err))

    base = MeetingResult(
        occur_id=meeting.occur_id,
        title=meeting.title,
        start=meeting.start,
        detail_url=meeting.detail_url,
        agenda_url=detail.agenda_url,
        agenda_type=detail.agenda_type.value,
        location=detail.location,
        zoom_url=detail.zoom_url,
        livestream_url=detail.livestream_url,
        status=Status.MISSING,
    )

    if detail.agenda_type is AgendaType.MISSING:
        return base

    if detail.agenda_type is AgendaType.OTHER:
        base.status = Status.UNSUPPORTED
        base.error = f"Unrecognized agenda URL host: {detail.agenda_url}"
        return base

    # Idempotency: don't re-download already-captured agendas.
    existing = _already_have(meeting.occur_id, dest_dir)
    if existing is not None:
        base.status = Status.SKIPPED_EXISTING
        base.file_path = str(existing)
        base.file_size = existing.stat().st_size
        return base

    if dry_run:
        base.status = Status.WOULD_DOWNLOAD
        return base

    stem = _filename_stem_for(meeting)
    try:
        if detail.agenda_type is AgendaType.CIVICCLERK:
            res = download_civicclerk(
                portal_url=detail.agenda_url,
                dest_dir=dest_dir,
                fmt="pdf",
                filename_stem=stem,
            )
            base.file_path = str(res.path)
            base.file_size = res.size_bytes
            base.status = Status.DOWNLOADED
        elif detail.agenda_type in (
            AgendaType.GOOGLE_DOC,
            AgendaType.GOOGLE_DRIVE_FILE,
        ):
            res = download_google_agenda(
                url=detail.agenda_url,
                dest_dir=dest_dir,
                filename_stem=stem,
            )
            base.file_path = str(res.path)
            base.file_size = res.size_bytes
            base.status = Status.DOWNLOADED
        else:
            base.status = Status.UNSUPPORTED
    except (CivicClerkDownloadError, GoogleDownloadError) as err:
        base.status = Status.FAILED
        base.error = str(err)

    return base


# ---- Pipeline driver -----------------------------------------------------


def run(
    *,
    today: Optional[date] = None,
    lookahead_days: int = 14,
    dest_dir: Path = DEFAULT_DEST,
    dry_run: bool = False,
    write_summary: bool = True,
    verbose: bool = True,
) -> RunSummary:
    """Run the full pipeline. Returns a RunSummary."""
    today = today or date.today()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    meetings = fetch_meetings(today=today, lookahead_days=lookahead_days)

    results: list[MeetingResult] = []
    for m in meetings:
        if verbose:
            print(f"[scraper] {m.start[:16]}  {m.title}", file=sys.stderr)
        results.append(process_meeting(m, dest_dir, dry_run=dry_run))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    window_end = today.fromordinal(today.toordinal() + lookahead_days)

    summary = RunSummary(
        run_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        as_of=today.isoformat(),
        window_end=window_end.isoformat(),
        lookahead_days=lookahead_days,
        dest_dir=str(dest_dir),
        dry_run=dry_run,
        counts=counts,
        meetings=[asdict(r) for r in results],
    )

    if write_summary:
        summary_path = dest_dir / RUN_SUMMARY_FILENAME
        summary_path.write_text(json.dumps(asdict(summary), indent=2))

    return summary


# ---- CLI -----------------------------------------------------------------


def _print_human(summary: RunSummary) -> None:
    print(
        f"\nMedford agenda scraper — window {summary.as_of} -> {summary.window_end} "
        f"({summary.lookahead_days}d, dest={summary.dest_dir})"
    )
    label_order = [
        Status.DOWNLOADED,
        Status.SKIPPED_EXISTING,
        Status.WOULD_DOWNLOAD,
        Status.MISSING,
        Status.UNSUPPORTED,
        Status.FAILED,
    ]
    for label in label_order:
        n = summary.counts.get(label, 0)
        if n:
            print(f"  {label:18s} {n}")
    print()
    for m in summary.meetings:
        marker = {
            Status.DOWNLOADED: " + ",
            Status.SKIPPED_EXISTING: " = ",
            Status.WOULD_DOWNLOAD: " ? ",
            Status.MISSING: " ! ",
            Status.UNSUPPORTED: " * ",
            Status.FAILED: " X ",
        }.get(m["status"], "   ")
        line = (
            f"{marker}{m['start'][:16]}  {m['agenda_type']:18s}  {m['title']}"
        )
        print(line)
        if m["status"] == Status.DOWNLOADED:
            print(f"      -> {m['file_path']}  ({m['file_size']:,} b)")
        elif m["status"] == Status.SKIPPED_EXISTING:
            print(f"      already at {m['file_path']}")
        elif m["status"] == Status.FAILED:
            print(f"      ERROR: {m['error']}")
        elif m["status"] == Status.UNSUPPORTED:
            print(f"      {m['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the full Medford agenda scraper pipeline: list upcoming "
            "meetings, resolve each agenda URL, and download the PDFs."
        )
    )
    parser.add_argument(
        "--as-of",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Treat this YYYY-MM-DD as 'today' (default: actual today).",
    )
    parser.add_argument(
        "--lookahead-days",
        type=int,
        default=14,
        help="How many days past 'today' to include (default: 14).",
    )
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_DEST),
        help=f"Destination directory for downloaded PDFs (default: {DEFAULT_DEST}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve agendas and report what would happen, but don't download.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--no-summary-file",
        action="store_true",
        help=(
            "Don't write .last_scraper_run.json into the destination directory."
        ),
    )
    parser.add_argument(
        "--process",
        action="store_true",
        help=(
            "After downloading, run the Parser (Haiku) -> Synthesizer (Sonnet) "
            "stages to update agendas.json and archive the PDFs. Requires "
            "ANTHROPIC_API_KEY to be set."
        ),
    )
    args = parser.parse_args(argv)

    summary = run(
        today=args.as_of,
        lookahead_days=args.lookahead_days,
        dest_dir=Path(args.dest),
        dry_run=args.dry_run,
        write_summary=not args.no_summary_file,
        verbose=not args.json,
    )

    if args.json:
        json.dump(asdict(summary), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(summary)

    process_failed = 0
    if args.process and not args.dry_run:
        process_failed = _run_process_stage(
            dest_dir=Path(args.dest), verbose=not args.json
        )

    # Exit non-zero if anything actually failed (not for MISSING/UNSUPPORTED,
    # which are normal outcomes), or if Parser/Synthesizer hit an error.
    failed = summary.counts.get(Status.FAILED, 0)
    return 1 if (failed or process_failed) else 0


def _run_process_stage(*, dest_dir: Path, verbose: bool = True) -> int:
    """Run Parser -> Synthesizer over freshly-downloaded PDFs.

    Returns 0 on success, non-zero if anything failed unrecoverably.
    """
    if verbose:
        print("\n=== Stage: Parser (Haiku) ===", file=sys.stderr)
    try:
        parse_directory(
            pdf_dir=dest_dir,
            output_dir=dest_dir / "markdown",
            skip_existing=True,
            verbose=verbose,
        )
    except ParserError as err:
        print(f"[run_pipeline] Parser stage halted: {err}", file=sys.stderr)
        return 2

    if verbose:
        print("\n=== Stage: Synthesizer (Sonnet) ===", file=sys.stderr)
    try:
        synthesize_directory(
            markdown_dir=dest_dir / "markdown",
            pdf_dir=dest_dir,
            archive_dir=dest_dir / "archived",
            agendas_json=DEFAULT_AGENDAS_JSON,
            dry_run=False,
            verbose=verbose,
        )
    except SynthesizerError as err:
        print(f"[run_pipeline] Synthesizer stage halted: {err}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
