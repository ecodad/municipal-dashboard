"""
Calendar-scrape step of the Medford agenda pipeline.

Fetches the city's events calendar for a configurable lookahead window
(default: today + 14 days) and returns the list of "Meeting" events with
the URL to each event's detail page.

This module is deliberately deterministic: no LLM is involved. The
Finalsite calendar markup is consistent enough that BeautifulSoup
selectors do the job at zero token cost.

Run as a CLI for ad-hoc verification:

    python -m scraper.calendar_scrape
    python -m scraper.calendar_scrape --lookahead-days 21
    python -m scraper.calendar_scrape --as-of 2026-05-15 --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup

# The Finalsite element ID of the calendar widget on
# https://www.medfordma.org/about/events-calendar . If Medford ever rebuilds
# the page, re-derive it by curl'ing the public events-calendar URL and
# grepping for `id="fsEl_<NNNN>"` on the element with `data-calendar-ids=351`.
MEDFORD_CALENDAR_ELEMENT = 6730

CALENDAR_AJAX_URL = (
    "https://www.medfordma.org/fs/elements/{element_id}?cal_date={cal_date}"
)

USER_AGENT = (
    "Mozilla/5.0 (compatible; MedfordAgendaScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)

REQUEST_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Meeting:
    occur_id: str
    title: str
    start: str  # ISO 8601 with timezone, e.g. "2026-04-30T09:30:00-04:00"
    detail_url: str


class ScrapeError(RuntimeError):
    """Raised when the calendar response can't be parsed as expected."""


def _fetch_calendar_page(cal_date: date) -> str:
    """GET the calendar element for the month containing `cal_date`.

    IMPORTANT: must mimic the in-page XHR call exactly, otherwise the
    CDN serves stale "current month" content regardless of cal_date.

    Reverse-engineered from Medford's Finalsite calendar JS (the
    `loadElementData` function in `/assets/application-*.js`):
    when the user clicks the next/prev-month button, the JS does
    `$.ajax({cache: false, ...})` which adds an `X-Requested-With:
    XMLHttpRequest` header AND a unique `_=<timestamp>` query
    parameter. The CDN cache key strips ``cal_date`` but does NOT
    strip ``_``, so the timestamp param is what actually busts the
    cache and makes ``cal_date`` honored end-to-end.

    Without the cache-buster:
      - 2026-04-30 cron, cal_date=2026-04-30 + cal_date=2026-05-30
        both returned an "April-flavored" cached response with only
        2 events in the 14-day window. Origin had served April; CDN
        keyed cal_date out of the URL; so every probe got April.
    With the cache-buster:
      - cal_date=2026-05-30 now reliably returns the May grid
        (9 April + 36 May + 3 June events).
      - cal_date=2026-06-15 now reliably returns the June grid.

    See TARGET_SITES.md for the full discovery.
    """
    url = CALENDAR_AJAX_URL.format(
        element_id=MEDFORD_CALENDAR_ELEMENT,
        cal_date=cal_date.isoformat(),
    )
    # jQuery's $.ajax({cache: false}) appends `_=<ms-timestamp>`. The
    # value just needs to be unique per request; we don't care what
    # it is.
    cache_buster = int(time.time() * 1000)
    url_with_bust = f"{url}&_={cache_buster}"

    resp = requests.get(
        url_with_bust,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html, */*; q=0.01",
            # Mimic an in-page XHR; without this the CDN may classify
            # us as a regular page-load and serve a different cache
            # shard.
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.medfordma.org/about/events-calendar",
            # Defense in depth: also ask the CDN to revalidate.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    if "fsCalendar" not in resp.text:
        raise ScrapeError(
            f"Response from {url_with_bust} doesn't contain expected "
            "fsCalendar markup. The Finalsite element ID or markup may "
            "have changed."
        )
    return resp.text


def _extract_events(html: str) -> list[Meeting]:
    """Pull every event link + its start datetime out of one calendar page."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("a.fsCalendarEventTitle.fsCalendarEventLink")
    if not anchors:
        # Empty calendar pages exist (the calendar simply has no events that
        # month). That's not necessarily an error — caller decides.
        return []

    meetings: list[Meeting] = []
    for a in anchors:
        occur_id = a.get("data-occur-id")
        title = (a.get("title") or a.get_text(strip=True) or "").strip()
        href = a.get("href", "")

        if not (occur_id and occur_id.isdigit()):
            raise ScrapeError(
                f"Event anchor has missing or non-numeric data-occur-id: {a}"
            )
        if not title:
            raise ScrapeError(f"Event anchor has empty title: {a}")
        if not href:
            raise ScrapeError(f"Event anchor has empty href: {a}")

        start_iso = _find_event_start(a)
        if not start_iso:
            # Some all-day events render without a <time> sibling. Skip
            # silently — we filter to "Meeting" titles anyway and meetings
            # always have a start time.
            continue

        meetings.append(
            Meeting(
                occur_id=occur_id,
                title=title,
                start=start_iso,
                detail_url=href,
            )
        )
    return meetings


def _find_event_start(anchor) -> str | None:
    """Find the start datetime that belongs to this event link.

    Each event renders as:

        <a class="fsCalendarEventTitle ...">Meeting Name</a>
        <div class="fsTimeRange">
            <time datetime="2026-04-30T09:30:00-04:00" class="fsStartTime">...
        </div>

    We walk the anchor's parent and look for the next fsStartTime <time>.
    """
    container = anchor.find_parent(class_="fsCalendarInfo") or anchor.parent
    if container is None:
        return None
    time_el = container.find("time", class_="fsStartTime")
    if time_el and time_el.get("datetime"):
        return time_el["datetime"]
    return None


def _is_meeting(title: str) -> bool:
    """Filter rule: the user wants events whose title is for a *meeting*.

    Empirically every governmental meeting on Medford's calendar has the
    word 'Meeting' in the title; non-meeting community events do not.
    """
    return "meeting" in title.lower()


def _within_window(start_iso: str, today: date, last_day: date) -> bool:
    """Is the event's start date within [today, last_day] (both inclusive)?"""
    start_date = datetime.fromisoformat(start_iso).date()
    return today <= start_date <= last_day


def fetch_meetings(
    today: date | None = None,
    lookahead_days: int = 14,
    *,
    debug_dir: Optional[Path] = None,
    verbose: bool = True,
) -> list[Meeting]:
    """Return deduped Meeting events in [today, today + lookahead_days].

    Uses two calendar fetches (this month and ~30 days out) to guarantee
    coverage of any 14-day window regardless of what day of the month we
    run. (Note: the cal_date param has been observed to be ignored by
    Medford's CDN cache key — see _fetch_calendar_page docstring — but
    the second probe still helps because of cache-shard variance and
    forces revalidation.)

    Args:
        today: ISO date to treat as "today". Defaults to today.
        lookahead_days: how far past today to include.
        debug_dir: when given, the raw HTML of every probe is written
            to ``{debug_dir}/probe_{N}_calDate-{YYYY-MM-DD}.html`` for
            forensic debugging. Existing contents of debug_dir are
            wiped first so each run reflects only its own probes.
        verbose: when True (default), print filter-stage counts to
            stderr so silent drops are visible in the run log.
    """
    if today is None:
        today = date.today()
    if lookahead_days < 0:
        raise ValueError("lookahead_days must be >= 0")

    last_day = today + timedelta(days=lookahead_days)

    # Probe at today, +30 days, and the last day of the window. With the
    # cache-buster fix in `_fetch_calendar_page` cal_date is honored
    # end-to-end, so each probe reliably returns its target month's
    # grid. Three probes cover every reasonable lookahead (default 14
    # days, but also 30 / 45 / 60 with no extra effort) and dedup by
    # occur_id makes the redundancy free.
    probe_dates = sorted({
        today,
        today + timedelta(days=30),
        last_day,
    })

    if debug_dir is not None:
        debug_dir = Path(debug_dir)
        if debug_dir.exists():
            shutil.rmtree(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)

    by_id: dict[str, Meeting] = {}
    per_probe_counts: list[tuple[str, int]] = []
    for n, probe_date in enumerate(probe_dates, start=1):
        html = _fetch_calendar_page(probe_date)
        if debug_dir is not None:
            out = debug_dir / f"probe_{n}_calDate-{probe_date.isoformat()}.html"
            out.write_text(html, encoding="utf-8")
        events = _extract_events(html)
        per_probe_counts.append((probe_date.isoformat(), len(events)))
        for ev in events:
            by_id.setdefault(ev.occur_id, ev)

    extracted_total = len(by_id)

    # Apply filters with explicit counters so we can report what got
    # dropped at each stage.
    filtered_title: list[Meeting] = []
    filtered_window: list[Meeting] = []
    passed: list[Meeting] = []
    for m in by_id.values():
        if not _is_meeting(m.title):
            filtered_title.append(m)
            continue
        if not _within_window(m.start, today, last_day):
            filtered_window.append(m)
            continue
        passed.append(m)

    # Stable sort: by start datetime, then title.
    passed.sort(key=lambda m: (m.start, m.title))

    if verbose:
        probes_str = ", ".join(
            f"cal_date={d}:{n}" for d, n in per_probe_counts
        )
        print(
            f"[scraper] calendar probes: {probes_str}; "
            f"{extracted_total} unique events after dedup",
            file=sys.stderr,
        )
        print(
            f"[scraper] filter outcomes: "
            f"{len(filtered_title)} dropped (no 'meeting' in title), "
            f"{len(filtered_window)} dropped (outside "
            f"{today.isoformat()}..{last_day.isoformat()} window), "
            f"{len(passed)} passed",
            file=sys.stderr,
        )
        if filtered_window:
            window_titles = sorted(
                {(m.start[:10], m.title) for m in filtered_window}
            )
            preview = ", ".join(f"{d} {t}" for d, t in window_titles[:6])
            more = f" (+{len(window_titles) - 6} more)" if len(window_titles) > 6 else ""
            print(
                f"[scraper]   window-filtered samples: {preview}{more}",
                file=sys.stderr,
            )

    # Sanity guardrail: if the lookahead is at least two weeks and we see
    # zero meetings, that's suspicious enough to surface (Medford rarely
    # has zero government meetings in a 14-day window).
    if lookahead_days >= 14 and not passed:
        print(
            "WARNING: 0 meetings extracted across a >=14-day window. "
            "The calendar markup may have changed, or the upstream CDN "
            "cache may be serving an unexpected month. See "
            f"{debug_dir or '<debug_dir not set>'} for raw responses.",
            file=sys.stderr,
        )

    return passed


# ---- CLI ------------------------------------------------------------------


def _meetings_as_dicts(meetings: Iterable[Meeting]) -> list[dict]:
    return [asdict(m) for m in meetings]


def _format_human(meetings: list[Meeting]) -> str:
    if not meetings:
        return "(no meetings in window)"
    lines = []
    for m in meetings:
        when = datetime.fromisoformat(m.start)
        lines.append(
            f"  {when.strftime('%a %Y-%m-%d %H:%M')}  "
            f"#{m.occur_id}  {m.title}\n"
            f"      {m.detail_url}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape Medford municipal meeting events from the public calendar."
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
        "--json",
        action="store_true",
        help="Emit JSON to stdout instead of a human-readable list.",
    )
    args = parser.parse_args(argv)

    try:
        meetings = fetch_meetings(today=args.as_of, lookahead_days=args.lookahead_days)
    except (ScrapeError, requests.RequestException) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    today = args.as_of or date.today()
    last_day = today + timedelta(days=args.lookahead_days)

    if args.json:
        json.dump(
            {
                "as_of": today.isoformat(),
                "window_end": last_day.isoformat(),
                "count": len(meetings),
                "meetings": _meetings_as_dicts(meetings),
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        print(
            f"Medford meetings between {today.isoformat()} and {last_day.isoformat()} "
            f"(lookahead={args.lookahead_days}d): {len(meetings)} found"
        )
        print(_format_human(meetings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
