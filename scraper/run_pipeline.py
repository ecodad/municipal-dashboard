"""
Pipeline orchestrator — city-agnostic.

For a given municipality slug + lookahead window, this:

  1. Loads the right `CityAdapter` (Finalsite for Medford, eventually
     Drupal+Legistar for Somerville, etc.).
  2. Asks it to `list_meetings(today, lookahead_days)` — returns
     `MeetingRecord`s with agenda_url + agenda_type already resolved.
  3. For each meeting whose agenda is downloadable, calls
     `adapter.download_agenda(...)` and saves the PDF into the per-city
     working directory `agendas/{slug}/`.
  4. Skips meetings whose agenda is already on disk in the working
     directory or in the published `{site_path}/archived/` directory.
  5. Optionally (`--process`) runs the Parser (Haiku) → Synthesizer
     (Sonnet) chain. The Parser writes Markdown into
     `agendas/{slug}/markdown/`; the Synthesizer reads from there,
     writes structured items into `{site_path}/agendas.json`, and
     moves the consumed PDFs + Markdown into `{site_path}/archived/`.
  6. Refreshes `{site_path}/index.html` from `template/dashboard.html`
     and `{site_path}/branding.json` from `branding/{slug}.json`, so a
     fork that swaps slug or template gets its dashboard chrome
     updated automatically.
  7. Rewrites the root `cities.json` registry with the current set of
     deployed cities (used by the landing page).

In `--all` mode the steps above are run sequentially for every adapter
registered in `scraper.adapters._REGISTRY`.

The orchestrator never imports any city's modules directly — the
adapter registry does the lookup. To add a city, write an adapter,
register it, and add `branding/{slug}.json`. This file does not change.
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
    SynthesizerError,
    synthesize_directory,
)


# ---- Filesystem layout --------------------------------------------------
#
# Per-city working dir:    agendas/{slug}/         (gitignored)
#                          agendas/{slug}/markdown/
# Per-city published dir:  {site_path}/            (committed)
#                          {site_path}/index.html  (refreshed from template)
#                          {site_path}/agendas.json
#                          {site_path}/branding.json (synced from branding/{slug}.json)
#                          {site_path}/archived/   (post-synthesizer audit trail)
# Per-city run summary:    agendas/{slug}/.last_scraper_run.json (gitignored)
# Project-level files:     index.html              (landing page, hand-written)
#                          cities.json             (pipeline-generated)
#                          template/dashboard.html (canonical city dashboard)
#                          branding/{slug}.json    (per-city chrome source)

WORKING_DIR_ROOT = Path("agendas")
TEMPLATE_DASHBOARD_PATH = Path("template") / "dashboard.html"
BRANDING_DIR = Path("branding")
CITIES_JSON_PATH = Path("cities.json")
RUN_SUMMARY_FILENAME = ".last_scraper_run.json"

DEFAULT_MUNICIPALITY_SLUG = "medford-ma"
SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---- Result types --------------------------------------------------------


class Status:
    DOWNLOADED = "downloaded"
    SKIPPED_EXISTING = "skipped_existing"
    WOULD_DOWNLOAD = "would_download"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass
class MeetingRunResult:
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
    site_path: str
    as_of: str
    window_end: str
    lookahead_days: int
    working_dir: str
    site_dir: str
    dry_run: bool
    counts: dict = field(default_factory=dict)
    meetings: list[dict] = field(default_factory=list)


# ---- Path helpers --------------------------------------------------------


def working_dir_for(adapter: CityAdapter) -> Path:
    """In-flight downloads + parser markdown live here. Gitignored."""
    return WORKING_DIR_ROOT / adapter.slug


def site_dir_for(adapter: CityAdapter) -> Path:
    """Published per-city directory served by GitHub Pages."""
    return Path(adapter.site_path)


def archive_dir_for(adapter: CityAdapter) -> Path:
    return site_dir_for(adapter) / "archived"


def agendas_json_for(adapter: CityAdapter) -> Path:
    return site_dir_for(adapter) / "agendas.json"


# ---- Slugify + dedup -----------------------------------------------------


def _slugify(s: str, max_len: int = 60) -> str:
    s = SLUG_RE.sub("-", s.lower()).strip("-")
    return s[:max_len].rstrip("-") or "meeting"


def _filename_stem_for(meeting: MeetingRecord) -> str:
    date_part = meeting.start[:10]  # YYYY-MM-DD slice from ISO timestamp
    slug = _slugify(meeting.title)
    return f"{date_part}__{meeting.occur_id}__{slug}"


def _already_have(occur_id: str, *search_dirs: Path) -> Optional[Path]:
    """Look in each search dir (and its `archived/` subdir if present)
    for a file containing `__{occur_id}__` in its name. Returns the
    first match, or None.
    """
    needle = f"__{occur_id}__"
    seen: set[Path] = set()
    for base in search_dirs:
        for d in (base, base / "archived"):
            if d in seen or not d.is_dir():
                continue
            seen.add(d)
            for p in d.iterdir():
                if p.is_file() and needle in p.name:
                    return p
    return None


# ---- Per-city site refresh ----------------------------------------------


def _refresh_site_chrome(adapter: CityAdapter, *, verbose: bool = True) -> None:
    """Sync `template/dashboard.html` → `{site_path}/index.html` and
    `branding/{slug}.json` → `{site_path}/branding.json`.

    Idempotent: a forker who edits the template or the branding file
    sees those changes appear in the deployed dashboard on the next
    pipeline run.
    """
    site_dir = site_dir_for(adapter)
    site_dir.mkdir(parents=True, exist_ok=True)

    if TEMPLATE_DASHBOARD_PATH.is_file():
        dest_html = site_dir / "index.html"
        shutil.copyfile(TEMPLATE_DASHBOARD_PATH, dest_html)
        if verbose:
            print(
                f"[run_pipeline] {TEMPLATE_DASHBOARD_PATH} -> {dest_html}",
                file=sys.stderr,
            )
    elif verbose:
        print(
            f"[run_pipeline] note: {TEMPLATE_DASHBOARD_PATH} not found; "
            f"leaving {site_dir / 'index.html'} unchanged.",
            file=sys.stderr,
        )

    branding_src = BRANDING_DIR / f"{adapter.slug}.json"
    if branding_src.is_file():
        dest_branding = site_dir / "branding.json"
        shutil.copyfile(branding_src, dest_branding)
        if verbose:
            print(
                f"[run_pipeline] {branding_src} -> {dest_branding}",
                file=sys.stderr,
            )
    elif verbose:
        print(
            f"[run_pipeline] note: {branding_src} not found; "
            f"leaving {site_dir / 'branding.json'} unchanged.",
            file=sys.stderr,
        )


def _update_cities_registry(verbose: bool = True) -> None:
    """Rewrite the root `cities.json` based on the current set of
    registered adapters. The landing page consumes this file.

    Each entry pulls its display fields from the city's
    `branding/{slug}.json` (logo, colors, eyebrow, tagline) and its
    item/meeting counts from `{site_path}/agendas.json` if available.
    """
    entries: list[dict] = []
    for slug in registered_slugs():
        try:
            adapter = load_adapter(slug)
        except Exception as err:  # noqa: BLE001
            if verbose:
                print(
                    f"[run_pipeline] cities.json: skipping {slug!r}: {err}",
                    file=sys.stderr,
                )
            continue

        entry: dict = {
            "slug": adapter.slug,
            "name": adapter.name.split(",")[0].strip(),
            "path": adapter.site_path,
        }

        # Pull display chrome from branding/{slug}.json if present.
        branding_path = BRANDING_DIR / f"{slug}.json"
        if branding_path.is_file():
            try:
                b = json.loads(branding_path.read_text(encoding="utf-8"))
                for k in (
                    "city_name",
                    "city_state",
                    "eyebrow",
                    "tagline",
                    "logo_url",
                    "logo_alt",
                    "primary_color",
                ):
                    if k in b:
                        # Map city_name → name only if branding sets one
                        # explicitly; otherwise leave the adapter-derived
                        # default.
                        if k == "city_name":
                            entry["name"] = b["city_name"]
                        elif k == "city_state":
                            entry["state"] = b["city_state"]
                        else:
                            entry[k] = b[k]
            except (OSError, json.JSONDecodeError) as err:
                if verbose:
                    print(
                        f"[run_pipeline] cities.json: bad branding "
                        f"{branding_path}: {err}",
                        file=sys.stderr,
                    )

        # Pull counts from the city's agendas.json if it exists.
        agendas_path = agendas_json_for(adapter)
        if agendas_path.is_file():
            try:
                data = json.loads(agendas_path.read_text(encoding="utf-8"))
                items = data.get("items") or []
                meetings = data.get("meetings") or []
                metadata = data.get("metadata") or {}
                entry["item_count"] = len(items)
                entry["meeting_count"] = len(meetings)
                if metadata.get("processed_date"):
                    entry["last_updated"] = metadata["processed_date"]
            except (OSError, json.JSONDecodeError) as err:
                if verbose:
                    print(
                        f"[run_pipeline] cities.json: bad agendas "
                        f"{agendas_path}: {err}",
                        file=sys.stderr,
                    )

        entries.append(entry)

    entries.sort(key=lambda e: e.get("name", ""))
    CITIES_JSON_PATH.write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )
    if verbose:
        names = [e.get("name", e["slug"]) for e in entries]
        print(
            f"[run_pipeline] {CITIES_JSON_PATH} updated: "
            f"{len(entries)} cit{'y' if len(entries) == 1 else 'ies'} "
            f"({', '.join(names)})",
            file=sys.stderr,
        )


# ---- Per-meeting processing ---------------------------------------------


def process_meeting(
    adapter: CityAdapter,
    meeting: MeetingRecord,
    working_dir: Path,
    archive_dir: Path,
    *,
    dry_run: bool = False,
) -> MeetingRunResult:
    """Resolve one meeting's agenda end-to-end.

    Never raises for per-meeting issues; returns a MeetingRunResult.
    Idempotency check looks in BOTH the per-city working dir AND the
    published archive (a previous run may have already moved the file
    into archived/).
    """
    if meeting.agenda_type == "MISSING":
        return MeetingRunResult(record=meeting, status=Status.MISSING)

    if not meeting.is_downloadable:
        return MeetingRunResult(
            record=meeting,
            status=Status.UNSUPPORTED,
            error=(
                f"Adapter classified agenda_type={meeting.agenda_type!r} "
                f"as non-downloadable (url={meeting.agenda_url!r})."
            ),
        )

    existing = _already_have(meeting.occur_id, working_dir, archive_dir.parent)
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
            dest_dir=working_dir,
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


# ---- Per-city pipeline driver ------------------------------------------


def run_for_adapter(
    adapter: CityAdapter,
    *,
    today: Optional[date] = None,
    lookahead_days: int = 14,
    dry_run: bool = False,
    write_summary: bool = True,
    verbose: bool = True,
) -> RunSummary:
    """Run the scrape→download stage for a single city."""
    today = today or date.today()
    working_dir = working_dir_for(adapter)
    site_dir = site_dir_for(adapter)
    archive_dir = archive_dir_for(adapter)

    working_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(
            f"[run_pipeline] === {adapter.slug} ({adapter.name}) === "
            f"working={working_dir}, site={site_dir}",
            file=sys.stderr,
        )

    meetings = adapter.list_meetings(today=today, lookahead_days=lookahead_days)

    results: list[MeetingRunResult] = []
    for m in meetings:
        if verbose:
            print(f"[scraper] {m.start[:16]}  {m.title}", file=sys.stderr)
        results.append(
            process_meeting(
                adapter, m, working_dir, archive_dir, dry_run=dry_run
            )
        )

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    window_end = today.fromordinal(today.toordinal() + lookahead_days)

    summary = RunSummary(
        run_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        municipality_slug=adapter.slug,
        municipality_name=adapter.name,
        site_path=adapter.site_path,
        as_of=today.isoformat(),
        window_end=window_end.isoformat(),
        lookahead_days=lookahead_days,
        working_dir=str(working_dir),
        site_dir=str(site_dir),
        dry_run=dry_run,
        counts=counts,
        meetings=[r.to_dict() for r in results],
    )

    if write_summary:
        summary_path = working_dir / RUN_SUMMARY_FILENAME
        summary_path.write_text(
            json.dumps(asdict(summary), indent=2), encoding="utf-8"
        )

    return summary


def run_process_stage(adapter: CityAdapter, *, verbose: bool = True) -> int:
    """Run Parser → Synthesizer for a single city. Returns exit code."""
    working_dir = working_dir_for(adapter)
    markdown_dir = working_dir / "markdown"
    site_dir = site_dir_for(adapter)
    archive_dir = archive_dir_for(adapter)
    agendas_json = agendas_json_for(adapter)

    site_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(
            f"\n=== Stage: Parser (Haiku) — {adapter.slug} ===",
            file=sys.stderr,
        )
    try:
        parse_directory(
            pdf_dir=working_dir,
            output_dir=markdown_dir,
            skip_existing=True,
            verbose=verbose,
        )
    except ParserError as err:
        print(f"[run_pipeline] Parser stage halted: {err}", file=sys.stderr)
        return 2

    if verbose:
        print(
            f"\n=== Stage: Synthesizer (Sonnet) — {adapter.slug} ===",
            file=sys.stderr,
        )
    try:
        synthesize_directory(
            markdown_dir=markdown_dir,
            pdf_dir=working_dir,
            archive_dir=archive_dir,
            agendas_json=agendas_json,
            dry_run=False,
            verbose=verbose,
        )
    except SynthesizerError as err:
        print(
            f"[run_pipeline] Synthesizer stage halted: {err}",
            file=sys.stderr,
        )
        return 2

    return 0


# ---- Human-readable summary printing ------------------------------------


def _print_human(summary: RunSummary) -> None:
    print(
        f"\n{summary.municipality_name} agenda scraper — window "
        f"{summary.as_of} -> {summary.window_end} "
        f"({summary.lookahead_days}d, working={summary.working_dir}, "
        f"site={summary.site_dir})"
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


# ---- CLI -----------------------------------------------------------------


def _resolve_slug(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get("MUNICIPALITY_SLUG", "").strip()
    if env_value:
        return env_value
    return DEFAULT_MUNICIPALITY_SLUG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Municipal Dashboards pipeline for one city or for all "
            "registered cities. Lists upcoming meetings, downloads agenda "
            "PDFs, optionally parses + synthesizes them into the city's "
            "agendas.json, and refreshes the dashboard chrome from the "
            "shared template."
        )
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--municipality",
        default=None,
        help=(
            f"Single municipality slug. Defaults to MUNICIPALITY_SLUG env "
            f"var, falling back to {DEFAULT_MUNICIPALITY_SLUG!r}. "
            f"Registered: {', '.join(registered_slugs())}."
        ),
    )
    target.add_argument(
        "--all",
        action="store_true",
        help=(
            "Run the pipeline for every registered city, sequentially. "
            "Used by the GitHub Actions cron."
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
            "Don't write .last_scraper_run.json into the working directory."
        ),
    )
    parser.add_argument(
        "--no-chrome-refresh",
        action="store_true",
        help=(
            "Don't refresh {site_path}/index.html or branding.json from the "
            "template/branding sources. Useful if you're hand-editing the "
            "deployed files."
        ),
    )
    parser.add_argument(
        "--no-cities-update",
        action="store_true",
        help="Don't rewrite the root cities.json registry.",
    )
    parser.add_argument(
        "--process",
        action="store_true",
        help=(
            "After downloading, run the Parser (Haiku) -> Synthesizer (Sonnet) "
            "stages. Requires ANTHROPIC_API_KEY to be set."
        ),
    )
    args = parser.parse_args(argv)

    if args.all:
        slugs = registered_slugs()
        if not slugs:
            print("ERROR: no adapters registered.", file=sys.stderr)
            return 2
    else:
        slugs = [_resolve_slug(args.municipality)]

    overall_failed = 0
    summaries: list[RunSummary] = []

    for slug in slugs:
        try:
            adapter = load_adapter(slug)
        except KeyError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 2

        summary = run_for_adapter(
            adapter,
            today=args.as_of,
            lookahead_days=args.lookahead_days,
            dry_run=args.dry_run,
            write_summary=not args.no_summary_file,
            verbose=not args.json,
        )
        summaries.append(summary)

        if args.json:
            json.dump(asdict(summary), sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            _print_human(summary)

        if args.process and not args.dry_run:
            stage_rc = run_process_stage(adapter, verbose=not args.json)
            if stage_rc:
                overall_failed += 1

        if not args.no_chrome_refresh and not args.dry_run:
            _refresh_site_chrome(adapter, verbose=not args.json)

        if summary.counts.get(Status.FAILED, 0):
            overall_failed += 1

    if not args.no_cities_update and not args.dry_run:
        _update_cities_registry(verbose=not args.json)

    return 1 if overall_failed else 0


if __name__ == "__main__":
    sys.exit(main())
