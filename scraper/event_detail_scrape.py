"""
Step 2b: extract structured info from a Medford event-details page.

Given a detail URL like:

    https://www.medfordma.org/about/events-calendar/event-details/~occur-id/23816

return an EventDetail containing the agenda URL, agenda source type,
location text, Zoom URL, and livestream URL — whichever of those the
city has published. The data lives in a `<div class="fsDescription">`
block on the page; we extract it deterministically with BeautifulSoup,
no LLM in the loop.

Agenda sources fall into a small fixed set on this calendar:

  - CIVICCLERK         — medfordma.portal.civicclerk.com (City Council
                         and Committee of the Whole)
  - GOOGLE_DOC         — docs.google.com/document/...
  - GOOGLE_DRIVE_FILE  — drive.google.com/file/...
  - OTHER              — none of the above; URL is still preserved
  - MISSING            — no agenda link on the page; ~5% of meetings.
                         Surfaced as a first-class status so the
                         dashboard can show a visible "Agenda not
                         posted" badge.
"""

from __future__ import annotations

import argparse
import enum
import json
import re
import sys
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup, Tag

REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; MedfordAgendaScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)

OCCUR_ID_RE = re.compile(r"~occur-id/(\d+)")


class AgendaType(str, enum.Enum):
    CIVICCLERK = "CIVICCLERK"
    GOOGLE_DOC = "GOOGLE_DOC"
    GOOGLE_DRIVE_FILE = "GOOGLE_DRIVE_FILE"
    OTHER = "OTHER"
    MISSING = "MISSING"


@dataclass(frozen=True)
class EventDetail:
    occur_id: str
    detail_url: str
    agenda_url: Optional[str]
    agenda_type: AgendaType
    location: Optional[str]
    zoom_url: Optional[str]
    livestream_url: Optional[str]
    description_text: str  # full description as extracted, for diagnostics


class EventDetailScrapeError(RuntimeError):
    pass


def _classify_agenda_url(url: Optional[str]) -> AgendaType:
    if not url:
        return AgendaType.MISSING
    if "civicclerk.com" in url:
        return AgendaType.CIVICCLERK
    if "docs.google.com/document" in url:
        return AgendaType.GOOGLE_DOC
    if "drive.google.com/file" in url or "docs.google.com/file" in url:
        return AgendaType.GOOGLE_DRIVE_FILE
    return AgendaType.OTHER


def _extract_location(desc: Tag) -> Optional[str]:
    """Find a paragraph that begins with 'Location:' and return the rest."""
    for p in desc.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text.lower().startswith("location:"):
            return text[len("Location:"):].strip() or None
    return None


def _is_livestream(url: str, label: str) -> bool:
    if "youtube.com" in url or "youtu.be" in url:
        return True
    if "livestream" in url.lower():
        return True
    if "live" in label and "agenda" not in label and "zoom" not in label:
        return True
    return False


def _extract_links(desc: Tag) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (agenda_url, zoom_url, livestream_url) inferred from <a> tags.

    Detection is anchored on the *paragraph text containing* the link,
    because the city's pattern is consistently:

        <p>Agenda: <a href="...">...</a></p>
        <p>Zoom: <a href="...">...</a></p>

    We fall back to URL-host hints (zoom.us, youtube.com) when there's
    no labeling text in the surrounding paragraph.
    """
    agenda_url: Optional[str] = None
    zoom_url: Optional[str] = None
    livestream_url: Optional[str] = None

    for a in desc.find_all("a", href=True):
        href = a["href"].strip()
        parent_text = a.parent.get_text(" ", strip=True).lower() if a.parent else ""

        if not agenda_url and "agenda" in parent_text:
            agenda_url = href
            continue
        if not zoom_url and ("zoom" in parent_text or "zoom.us" in href):
            zoom_url = href
            continue
        if not livestream_url and _is_livestream(href, parent_text):
            livestream_url = href

    return agenda_url, zoom_url, livestream_url


def _occur_id_from_url(url: str) -> str:
    m = OCCUR_ID_RE.search(url)
    if not m:
        raise EventDetailScrapeError(
            f"Detail URL doesn't contain '~occur-id/<id>': {url}"
        )
    return m.group(1)


def parse_event_detail(html: str, detail_url: str) -> EventDetail:
    """Parse already-fetched detail HTML into an EventDetail."""
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.find("div", class_="fsDescription")
    occur_id = _occur_id_from_url(detail_url)

    if desc is None:
        # Page rendered without a description block — treat agenda as missing
        # but don't raise. Log it via description_text so the caller has a hint.
        return EventDetail(
            occur_id=occur_id,
            detail_url=detail_url,
            agenda_url=None,
            agenda_type=AgendaType.MISSING,
            location=None,
            zoom_url=None,
            livestream_url=None,
            description_text="",
        )

    description_text = desc.get_text(" ", strip=True)
    agenda_url, zoom_url, livestream_url = _extract_links(desc)
    location = _extract_location(desc)

    return EventDetail(
        occur_id=occur_id,
        detail_url=detail_url,
        agenda_url=agenda_url,
        agenda_type=_classify_agenda_url(agenda_url),
        location=location,
        zoom_url=zoom_url,
        livestream_url=livestream_url,
        description_text=description_text,
    )


def fetch_event_detail(detail_url: str) -> EventDetail:
    """GET the detail page and parse it."""
    try:
        resp = requests.get(
            detail_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as err:
        raise EventDetailScrapeError(f"Network error for {detail_url}: {err}") from err
    return parse_event_detail(resp.text, detail_url)


def enrich_meetings(meeting_dicts: Iterable[dict]) -> list[dict]:
    """Take Step 1 meeting tickets and add Step 2b detail fields.

    Each input dict must contain at least `detail_url`. The output dicts
    keep all original fields and add: `agenda_url`, `agenda_type`,
    `location`, `zoom_url`, `livestream_url`.
    """
    enriched: list[dict] = []
    for m in meeting_dicts:
        detail_url = m.get("detail_url")
        if not detail_url:
            raise EventDetailScrapeError(
                f"Meeting record missing detail_url: {m}"
            )
        d = fetch_event_detail(detail_url)
        merged = dict(m)
        merged["agenda_url"] = d.agenda_url
        merged["agenda_type"] = d.agenda_type.value
        merged["location"] = d.location
        merged["zoom_url"] = d.zoom_url
        merged["livestream_url"] = d.livestream_url
        enriched.append(merged)
    return enriched


# ---- CLI -----------------------------------------------------------------


def _print_human(d: EventDetail) -> None:
    print(f"#{d.occur_id}  ({d.agenda_type.value})")
    if d.agenda_url:
        print(f"  agenda     : {d.agenda_url}")
    else:
        print(f"  agenda     : (not posted)")
    print(f"  location   : {d.location or '(not given)'}")
    print(f"  zoom       : {d.zoom_url or '(none)'}")
    print(f"  livestream : {d.livestream_url or '(none)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract agenda URL + attendance details from one or more "
            "Medford event-details pages."
        )
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("detail_url", nargs="?", help="A single event-details URL.")
    src.add_argument(
        "--from-stdin",
        action="store_true",
        help=(
            "Read Step 1 JSON output from stdin (object with a 'meetings' "
            "list), enrich each meeting, and emit the combined JSON."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="When given a single URL, emit JSON instead of human text.",
    )
    args = parser.parse_args(argv)

    try:
        if args.from_stdin:
            payload = json.load(sys.stdin)
            meetings = payload.get("meetings", [])
            enriched = enrich_meetings(meetings)
            payload["meetings"] = enriched
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            d = fetch_event_detail(args.detail_url)
            if args.json:
                json.dump(asdict(d), sys.stdout, indent=2, default=str)
                sys.stdout.write("\n")
            else:
                _print_human(d)
    except EventDetailScrapeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
