"""
Pipeline orchestrator — city-agnostic.

For a given municipality slug + lookahead window, this:

  1. Loads the right `CityAdapter` (Finalsite for Medford, eventually
     Drupal+Legistar for Somerville, etc.).
  2. Asks it to `list_meetings(today, lookahead_days)` — returns a
     list of `MeetingRecord` with agenda_url + agenda_type already
     resolved.
  3. For each meeting whose agenda is downloadable, calls
     `adapter.download_agenda(...)` and saves the PDF.
  4. Skips meetings whose agenda is already on disk in `dest_dir/` or
     `dest_dir/archived/` (idempotency, keyed on occur_id substring).
  5. Optionally (`--process`) runs the Parser (Haiku) → Synthesizer
     (Sonnet) chain to update `agendas.json`.
  6. Optionally syncs `branding/{slug}.json` -> `branding.json` so
     the static dashboard picks up the right city chrome.
  7. Writes a JSON run summary at `dest_dir/.last_scraper_run.json`.

The orchestrator never imports any city's modules directly — the
adapter registry in `scraper.adapters` does the lookup. To add a city,
write an adapter; this file does not change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .adapters import (
    AdapterDownloadError,
    CityAdapter,
    MeetingRecord,
    load_adapter,
    registered_slugs,
)
from .parser import (
    ParserError,
    parse_directory,
)
from .synthesizer import (
    DEFAULT_AGENDAS_JSON,
    SynthesizerError,
    synthesize_directory,
)


DEFAULT_DEST = Path("agendas")
DEFAULT_MUNICIPALITY_SLUG = "medford-ma"
RUN_SUMMARY_FILENAME = ".last_scraper_run.json"
BRANDING_DIR = Path("branding")
ACTIVE_BRANDING_PATH = Path("branding.json")
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
class MeetingRunResult:
    """Per-meeting outcome of one pipeline run.

    Carries the full MeetingRecord forward so the run summary keeps
    the same shape it had before the adapter refactor (existing
    consumers — including the dashboard's earlier `legacy` fallback —
    can read it without changes).
    """

    record: MeetingRecord
    status: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self.record)
        d.update(
            {
                "status": self.status,
                "file_path": self.file_path,
                "file_size": self.file_size,
                "error": self.error,
            }
        )
        return d


@dataclass
class RunSummary:
    run_at: str
    municipality_slug: str
    municipality_name: str
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


def _filename_stem_for(meeting: MeetingRecord) -> str:
    """Stable, sortable, human-readable name; same shape as before."""
    date_part = meeting.start[:10]  # YYYY-MM-DD slice from ISO timestamp
    slug = _slugify(meeting.title)
    return f"{date_part}__{meeting.occur_id}__{slug}"


def _already_have(occur_id: str, dest_dir: Path) -> Optional[Path]:
    """Look in dest_dir + dest_dir/archived for any file containing
    this occur_id. Returns the first match, or None.
    """
    needle = f"__{occur_id}__"
    for d in (dest_dir, dest_dir / "archived"):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and needle in p.name:
                return p
    return None


def _sync_active_branding(slug: str, *, verbose: bool = True) -> None:
    """Copy `branding/{slug}.json` to `branding.json` so the static
    dashboard picks up the right city chrome on its next page load.

    No-op if the per-slug file is missing — the live `branding.json`
    is left as-is, which keeps the existing site rendering whatever
    branding was last committed.
    """
    src = BRANDING_DIR / f"{slug}.json"
    if not src.is_file():
        if verbose:
            print(
                f"[run_pipeline] note: {src} not found; "
                f"leaving {ACTIVE_BRANDING_PATH} unchanged.",
                file=sys.stderr,
            )
        return
    shutil.copyfile(src, ACTIVE_BRANDING_PATH)
    if verbose:
        print(
            f"[run_pipeline] branding: {src} -> {ACTIVE_BRANDING_PATH}",
            file=sys.stderr,
        )


# ---- Per-meeting processing ---------------------------------------------


def process_meeting(
    adapter: CityAdapter,
    meeting: MeetingRecord,
    dest_dir: Path,
    *,
    dry_run: bool = False,
) -> MeetingRunResult:
    """Resolve one meeting's agenda end-to-end.

    Never raises for per-meeting issues; returns a MeetingRunResult
    whose `status`/`error` describe what happened.
    """
    if meeting.agenda_type == "MISSING":
        return MeetingRunResult(record=meeting, status=Status.MISSING)

    if not meeting.is_downloadable:
        # OTHER / unrecognized — adapter said "won't fetch."
        return MeetingRunResult(
            record=meeting,
            status=Status.UNSUPPORTED,
            error=(
                f"Adapter classified agenda_type={meeting.agenda_type!r} "
                f"as non-downloadable (url={meeting.agenda_url!r})."
            ),
        )

    existing = _already_have(meeting.occur_id, dest_dir)
    if existing is not None:
        return MeetingRunResult(
            record=meeting,
            status=Status.SKIPPED_EXISTING,
            file_path=str(existing),
            file_size=existing.stat().st_size,
        )

    if dry_run:
        return MeetingRunResult(record=meeting, status=Status.WOULD_DOWNLOAD)

    stem = _filename_stem_for(meeting)
    try:
        result = adapter.download_agenda(
            meeting=meeting,
            dest_dir=dest_dir,
            filename_stem=stem,
        )
    except AdapterDownloadError as err:
        return MeetingRunResult(
            record=meeting, status=Status.FAILED, error=str(err)
        )

    return MeetingRunResult(
        record=meeting,
        status=Status.DOWNLOADED,
        file_path=str(result.path),
        file_size=result.size_bytes,
    )


# ---- Pipeline driver -----------------------------------------------------


def run(
    *,
    adapter: CityAdapter,
    today: Optional[date] = None,
    lookahead_days: int = 14,
    dest_dir: Path = DEFAULT_DEST,
    dry_run: bool = False,
    write_summary: bool = True,
    verbose: bool = True,
) -> RunSummary:
    today = today or date.today()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(
            f"[run_pipeline] municipality: {adapter.slug} ({adapter.name})",
            file=sys.stderr,
        )

    meetings = adapter.list_meetings(today=today, lookahead_days=lookahead_days)

    results: list[MeetingRunResult] = []
    for m in meetings:
        if verbose:
            print(f"[scraper] {m.start[:16]}  {m.title}", file=sys.stderr)
        results.append(process_meeting(adapter, m, dest_dir, dry_run=dry_run))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    window_end = today.fromordinal(today.toordinal() + lookahead_days)

    summary = RunSummary(
        run_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        municipality_slug=adapter.slug,
        municipality_name=adapter.name,
        as_of=today.isoformat(),
        window_end=window_end.isoformat(),
        lookahead_days=lookahead_days,
        dest_dir=str(dest_dir),
        dry_run=dry_run,
        counts=counts,
        meetings=[r.to_dict() for r in results],
    )

    if write_summary:
        summary_path = dest_dir / RUN_SUMMARY_FILENAME
        summary_path.write_text(json.dumps(asdict(summary), indent=2))

    return summary


# ---- CLI -----------------------------------------------------------------


def _print_human(summary: RunSummary) -> None:
    print(
        f"\n{summary.municipality_name} agenda scraper — window "
        f"{summary.as_of} -> {summary.window_end} "
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


def _resolve_slug(cli_value: Optional[str]) -> str:
    """Pick the municipality slug. Precedence:

      1. --municipality on the command line
      2. MUNICIPALITY_SLUG env var
      3. DEFAULT_MUNICIPALITY_SLUG
    """
    if cli_value:
        return cli_value
    env_value = os.environ.get("MUNICIPALITY_SLUG", "").strip()
    if env_value:
        return env_value
    return DEFAULT_MUNICIPALITY_SLUG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Municipal Dashboards pipeline: list upcoming meetings, "
            "resolve each agenda URL, and download the PDFs (and optionally "
            "parse + synthesize them into agendas.json)."
        )
    )
    parser.add_argument(
        "--municipality",
        default=None,
        help=(
            f"Municipality slug. Defaults to MUNICIPALITY_SLUG env var, "
            f"falling back to {DEFAULT_MUNICIPALITY_SLUG!r}. "
            f"Registered: {', '.join(registered_slugs())}."
        ),
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
        "--no-branding-sync",
        action="store_true",
        help=(
            "Don't copy branding/{slug}.json -> branding.json. Useful if "
            "you're hand-editing the active branding."
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

    slug = _resolve_slug(args.municipality)
    try:
        adapter = load_adapter(slug)
    except KeyError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    if not args.no_branding_sync and not args.dry_run:
        _sync_active_branding(slug, verbose=not args.json)

    summary = run(
        adapter=adapter,
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

    failed = summary.counts.get(Status.FAILED, 0)
    return 1 if (failed or process_failed) else 0


def _run_process_stage(*, dest_dir: Path, verbose: bool = True) -> int:
    """Run Parser -> Synthesizer over freshly-downloaded PDFs.

    City-agnostic; the adapter has done its work by this point.
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
