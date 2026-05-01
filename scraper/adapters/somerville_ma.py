"""
Somerville, MA — city adapter.

Calendar source: Drupal-driven listing at
``https://www.somervillema.gov/calendar``. Events are listed
chronologically (future-only) with ~20 per page; pagination is
zero-indexed (`?page=0` is the default first page; what the UI labels
"Page 2" is `?page=1`).

Agenda hosting splits cleanly along one axis:

  - **City Council standing committees** (full Council, Finance, Land
    Use, Legislative Matters, Confirmation of Appointments, Public
    Health & Public Safety, etc.) post their agendas through Legistar.
    The Drupal detail page contains a link of the form
    ``somervillema.legistar.com/Gateway.aspx?M=MD&...&ID=X&GUID=Y``;
    the agenda PDF is reachable at
    ``somervillema.legistar.com/View.ashx?M=A&ID=X&GUID=Y``
    (same ID/GUID — no second fetch required to derive the PDF URL).

  - **Every other body** (School Committee, Planning Board, Historic
    Preservation, Conservation, Zoning Board, Redevelopment Authority,
    Council on Aging, Human Rights Commission, OSPCD civic advisory
    committees, etc.) posts its agenda directly to a public S3 bucket:
    ``s3.amazonaws.com/somervillema-live/s3fs-public/YYYY-MM/...pdf``.
    Filenames are not predictable; the link text or href contains the
    substring "agenda" (used to distinguish from "Meeting Notice" PDFs
    posted alongside).

The 14-day default lookahead is appropriate: MA Open Meeting Law
requires agendas to be posted ≥48 hours before a meeting, and in
practice Somerville bodies post 1–2 weeks ahead. Beyond that window,
agendas haven't been published yet.

See TARGET_SITES.md (sections "Somerville Drupal calendar" and
"Somerville agenda hosting") for the full recon.
"""

from __future__ import annotations

import enum
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from . import (
    AdapterDownloadError,
    AGENDA_TYPE_MISSING,
    AgendaDownloadResult,
    MeetingRecord,
)
from ..legistar_download import (
    LegistarDownloadError,
    download_legistar_agenda,
)
from ..s3_download import (
    S3DownloadError,
    download_s3_agenda,
)


SITE_BASE = "https://www.somervillema.gov"
CALENDAR_URL = f"{SITE_BASE}/calendar"

REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = (
    "Mozilla/5.0 (compatible; MunicipalDashboardScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)

# Hard cap on calendar pages walked. With ~20 events/page and a 14-day
# lookahead, 1–2 pages is typical; 5 is plenty of headroom and serves
# as a runaway-loop safety net.
MAX_CALENDAR_PAGES = 5

# Drupal's <time datetime="..."> values are UTC. We convert to
# America/New_York so the ISO strings carry a wall-clock-correct local
# offset (-04:00 EDT in summer, -05:00 EST in winter), matching the
# shape of the offset-aware timestamps the Medford adapter already
# emits.
CITY_TIMEZONE = ZoneInfo("America/New_York")


# Patterns for agenda-link classification on the detail page. Both are
# matched anywhere in the page (not scoped to a specific field) because
# Drupal puts the link in different places depending on the body type:
# Legistar links land in the body field, S3 links land in a
# "views-field-field-custom-document-type" table.
LEGISTAR_GATEWAY_RE = re.compile(
    r"https?://[a-z0-9-]+\.legistar\.com/Gateway\.aspx[^\"'\s]*",
    re.IGNORECASE,
)
S3_BUCKET_PREFIX = "https://s3.amazonaws.com/somervillema-live/"

# Detail-URL slug extraction:
# /events/YYYY/MM/DD/{slug} → "YYYY_MM_DD_{slug}"
DETAIL_PATH_RE = re.compile(
    r"^/events/(\d{4})/(\d{2})/(\d{2})/([a-z0-9-]+)/?$"
)


class SomervilleAgendaType(str, enum.Enum):
    LEGISTAR = "LEGISTAR"
    S3 = "S3"
    MISSING = "MISSING"


class SomervilleScrapeError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ListingEvent:
    """A single row from the calendar listing page."""

    occur_id: str
    title: str
    start: str          # ISO 8601 with offset, e.g. "2026-05-04T19:00:00-04:00"
    detail_url: str     # absolute URL


def _utc_iso_to_local(utc_iso: str) -> str:
    """Convert a Drupal `<time datetime="...Z">` string to local ISO."""
    # Accept both "2026-05-04T23:00:00Z" and "...+00:00" forms.
    s = utc_iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CITY_TIMEZONE).isoformat()


def _occur_id_from_path(path: str) -> str:
    """`/events/2026/05/04/school-committee-regular-meeting` → stable id."""
    m = DETAIL_PATH_RE.match(path)
    if not m:
        raise SomervilleScrapeError(
            f"Detail URL path doesn't match /events/YYYY/MM/DD/<slug>: {path!r}"
        )
    yyyy, mm, dd, slug = m.groups()
    return f"{yyyy}_{mm}_{dd}_{slug}"


def _is_meeting(title: str) -> bool:
    """Filter rule: only events whose title contains "meeting" (case-insensitive)."""
    return "meeting" in title.lower()


def _fetch(url: str) -> str:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as err:
        raise SomervilleScrapeError(f"Network error for {url}: {err}") from err
    return resp.text


def _extract_listing_events(html: str) -> list[_ListingEvent]:
    """Parse one calendar page (?page=N) and return all events on it.

    Yields every event row regardless of title — the meeting filter and
    window filter are applied by the caller.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("div.views-row")
    events: list[_ListingEvent] = []
    for row in rows:
        a = row.select_one("span.views-field-title a[href^='/events/']")
        if a is None:
            # Some rows are non-event content (paginators, banners, etc.); skip.
            continue
        href = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not (href and title):
            continue

        time_el = row.find("time", attrs={"datetime": True})
        if time_el is None:
            # Defensive: rows without a datetime shouldn't be considered
            # meetings (no way to window-filter them). Skip silently.
            continue
        start_local = _utc_iso_to_local(time_el["datetime"])

        try:
            occur_id = _occur_id_from_path(href)
        except SomervilleScrapeError:
            # Malformed event URL — skip rather than crash the run.
            continue

        events.append(
            _ListingEvent(
                occur_id=occur_id,
                title=title,
                start=start_local,
                detail_url=urljoin(SITE_BASE, href),
            )
        )
    return events


def _extract_location(soup: BeautifulSoup) -> Optional[str]:
    """Best-effort location text from a detail page."""
    addr = soup.select_one("div.field--name-field-address")
    if addr is not None:
        text = addr.get_text(" ", strip=True)
        if text:
            return text
    return None


def _extract_zoom_url(soup: BeautifulSoup) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "zoom.us" in href:
            return href
    return None


def _classify_agenda(soup: BeautifulSoup) -> tuple[Optional[str], SomervilleAgendaType]:
    """Walk the detail page once and return (agenda_url, agenda_type).

    Detection order:
      1. Legistar Gateway anywhere on the page — wins immediately.
      2. S3 link to the somervillema-live bucket whose href OR link
         text contains "agenda" (case-insensitive). This filter excludes
         "Meeting Notice", "Call of the Week", flyer, and image-asset
         PDFs that are sometimes posted alongside.
      3. Otherwise MISSING.
    """
    for a in soup.find_all("a", href=True):
        if LEGISTAR_GATEWAY_RE.search(a["href"]):
            return a["href"], SomervilleAgendaType.LEGISTAR

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(S3_BUCKET_PREFIX):
            continue
        link_text = a.get_text(" ", strip=True).lower()
        if "agenda" in href.lower() or "agenda" in link_text:
            return href, SomervilleAgendaType.S3

    return None, SomervilleAgendaType.MISSING


@dataclass(frozen=True)
class _DetailExtract:
    agenda_url: Optional[str]
    agenda_type: SomervilleAgendaType
    location: Optional[str]
    zoom_url: Optional[str]


def _parse_detail(html: str) -> _DetailExtract:
    soup = BeautifulSoup(html, "html.parser")
    agenda_url, agenda_type = _classify_agenda(soup)
    return _DetailExtract(
        agenda_url=agenda_url,
        agenda_type=agenda_type,
        location=_extract_location(soup),
        zoom_url=_extract_zoom_url(soup),
    )


class SomervilleAdapter:
    """Drupal calendar + S3 / Legistar agendas."""

    slug = "somerville-ma"
    name = "Somerville, MA"
    site_path = "somerville"

    def list_meetings(
        self,
        today: date,
        lookahead_days: int,
        *,
        debug_dir: Optional[Path] = None,
    ) -> list[MeetingRecord]:
        if lookahead_days < 0:
            raise ValueError("lookahead_days must be >= 0")
        last_day = today + timedelta(days=lookahead_days)

        if debug_dir is not None:
            debug_dir = Path(debug_dir)
            if debug_dir.exists():
                shutil.rmtree(debug_dir)
            debug_dir.mkdir(parents=True, exist_ok=True)

        # Walk paginated listing until either: no events, all events past
        # the window, or hard cap reached.
        listing: list[_ListingEvent] = []
        for page in range(MAX_CALENDAR_PAGES):
            url = CALENDAR_URL if page == 0 else f"{CALENDAR_URL}?page={page}"
            html = _fetch(url)
            if debug_dir is not None:
                (debug_dir / f"calendar_page-{page}.html").write_text(
                    html, encoding="utf-8",
                )
            page_events = _extract_listing_events(html)
            if not page_events:
                break
            listing.extend(page_events)
            # Sorted chronologically by Drupal — once the first event on a
            # page is past last_day, no later page can be in window.
            first_date = datetime.fromisoformat(page_events[0].start).date()
            if first_date > last_day:
                break

        # Apply filters with explicit counters for run-log forensics.
        filtered_title: list[_ListingEvent] = []
        filtered_window: list[_ListingEvent] = []
        in_window: list[_ListingEvent] = []
        for ev in listing:
            if not _is_meeting(ev.title):
                filtered_title.append(ev)
                continue
            ev_date = datetime.fromisoformat(ev.start).date()
            if not (today <= ev_date <= last_day):
                filtered_window.append(ev)
                continue
            in_window.append(ev)

        # Dedup by occur_id, preserving first occurrence (chronological).
        seen: set[str] = set()
        deduped: list[_ListingEvent] = []
        for ev in in_window:
            if ev.occur_id in seen:
                continue
            seen.add(ev.occur_id)
            deduped.append(ev)

        print(
            f"[somerville-ma] listing: {len(listing)} events scanned, "
            f"{len(filtered_title)} dropped (no 'meeting' in title), "
            f"{len(filtered_window)} dropped (outside "
            f"{today.isoformat()}..{last_day.isoformat()} window), "
            f"{len(deduped)} passed",
            file=sys.stderr,
        )

        # Fetch each detail page and classify the agenda host.
        records: list[MeetingRecord] = []
        for ev in deduped:
            try:
                detail_html = _fetch(ev.detail_url)
            except SomervilleScrapeError as err:
                print(
                    f"[somerville-ma] detail fetch failed for {ev.occur_id}: {err}",
                    file=sys.stderr,
                )
                records.append(
                    MeetingRecord(
                        occur_id=ev.occur_id,
                        title=ev.title,
                        start=ev.start,
                        detail_url=ev.detail_url,
                        agenda_url=None,
                        agenda_type=AGENDA_TYPE_MISSING,
                        location=None,
                        zoom_url=None,
                        livestream_url=None,
                        adapter_payload={"detail_fetch_error": str(err)},
                    )
                )
                continue

            if debug_dir is not None:
                (debug_dir / f"detail_{ev.occur_id}.html").write_text(
                    detail_html, encoding="utf-8",
                )

            extract = _parse_detail(detail_html)
            records.append(
                MeetingRecord(
                    occur_id=ev.occur_id,
                    title=ev.title,
                    start=ev.start,
                    detail_url=ev.detail_url,
                    agenda_url=extract.agenda_url,
                    agenda_type=extract.agenda_type.value,
                    location=extract.location,
                    zoom_url=extract.zoom_url,
                    livestream_url=None,
                )
            )

        return records

    def download_agenda(
        self,
        meeting: MeetingRecord,
        dest_dir: Path,
        filename_stem: str,
    ) -> AgendaDownloadResult:
        if meeting.agenda_url is None:
            raise AdapterDownloadError(
                f"Meeting {meeting.occur_id} has no agenda_url to download."
            )

        if meeting.agenda_type == SomervilleAgendaType.LEGISTAR.value:
            try:
                res = download_legistar_agenda(
                    url=meeting.agenda_url,
                    dest_dir=dest_dir,
                    filename_stem=filename_stem,
                )
            except LegistarDownloadError as err:
                raise AdapterDownloadError(str(err)) from err
            return AgendaDownloadResult(
                path=res.path,
                size_bytes=res.size_bytes,
                source_url=res.download_url,
            )

        if meeting.agenda_type == SomervilleAgendaType.S3.value:
            try:
                res = download_s3_agenda(
                    url=meeting.agenda_url,
                    dest_dir=dest_dir,
                    filename_stem=filename_stem,
                )
            except S3DownloadError as err:
                raise AdapterDownloadError(str(err)) from err
            return AgendaDownloadResult(
                path=res.path,
                size_bytes=res.size_bytes,
                source_url=res.source_url,
            )

        raise AdapterDownloadError(
            f"Somerville adapter doesn't know how to download agenda_type="
            f"{meeting.agenda_type!r} (url={meeting.agenda_url!r})."
        )
