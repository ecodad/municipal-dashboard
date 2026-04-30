"""
Medford, MA — city adapter.

Wraps the existing Medford-specific scraper modules
(`calendar_scrape`, `event_detail_scrape`, `civicclerk_download`,
`google_download`) behind the city-agnostic CityAdapter Protocol.

This is intentionally a thin facade. The host-level downloaders
(CivicClerk, Google Doc/Drive) and the Finalsite calendar/detail
modules are unchanged from the pre-adapter pipeline; this file just
composes them, translates exceptions, and exposes the unified shape.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Optional

from . import (
    AdapterDownloadError,
    AgendaDownloadResult,
    MeetingRecord,
)
from ..calendar_scrape import fetch_meetings
from ..civicclerk_download import (
    CivicClerkDownloadError,
    download_agenda as download_civicclerk,
)
from ..event_detail_scrape import (
    AgendaType as MedfordAgendaType,
    EventDetailScrapeError,
    fetch_event_detail,
)
from ..google_download import (
    GoogleDownloadError,
    download_google_agenda,
)


class MedfordAdapter:
    """Finalsite calendar + Finalsite detail + CivicClerk/Google agendas."""

    slug = "medford-ma"
    name = "Medford, MA"
    site_path = "medford"

    def list_meetings(
        self,
        today: date,
        lookahead_days: int,
        *,
        debug_dir: Optional[Path] = None,
    ) -> list[MeetingRecord]:
        meetings = fetch_meetings(
            today=today,
            lookahead_days=lookahead_days,
            debug_dir=debug_dir,
        )
        records: list[MeetingRecord] = []
        for m in meetings:
            try:
                detail = fetch_event_detail(m.detail_url)
            except EventDetailScrapeError as err:
                # Don't crash the run — surface the meeting as MISSING
                # with a payload note so it shows up in the run summary.
                print(
                    f"[medford-ma] event detail fetch failed for "
                    f"#{m.occur_id}: {err}",
                    file=sys.stderr,
                )
                records.append(
                    MeetingRecord(
                        occur_id=m.occur_id,
                        title=m.title,
                        start=m.start,
                        detail_url=m.detail_url,
                        agenda_url=None,
                        agenda_type=MedfordAgendaType.MISSING.value,
                        location=None,
                        zoom_url=None,
                        livestream_url=None,
                        adapter_payload={"detail_fetch_error": str(err)},
                    )
                )
                continue

            records.append(
                MeetingRecord(
                    occur_id=m.occur_id,
                    title=m.title,
                    start=m.start,
                    detail_url=m.detail_url,
                    agenda_url=detail.agenda_url,
                    agenda_type=detail.agenda_type.value,
                    location=detail.location,
                    zoom_url=detail.zoom_url,
                    livestream_url=detail.livestream_url,
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

        if meeting.agenda_type == MedfordAgendaType.CIVICCLERK.value:
            try:
                res = download_civicclerk(
                    portal_url=meeting.agenda_url,
                    dest_dir=dest_dir,
                    fmt="pdf",
                    filename_stem=filename_stem,
                )
            except CivicClerkDownloadError as err:
                raise AdapterDownloadError(str(err)) from err
            return AgendaDownloadResult(
                path=res.path,
                size_bytes=res.size_bytes,
                source_url=res.url,
            )

        if meeting.agenda_type in (
            MedfordAgendaType.GOOGLE_DOC.value,
            MedfordAgendaType.GOOGLE_DRIVE_FILE.value,
        ):
            try:
                res = download_google_agenda(
                    url=meeting.agenda_url,
                    dest_dir=dest_dir,
                    filename_stem=filename_stem,
                )
            except GoogleDownloadError as err:
                raise AdapterDownloadError(str(err)) from err
            return AgendaDownloadResult(
                path=res.path,
                size_bytes=res.size_bytes,
                source_url=res.download_url,
            )

        raise AdapterDownloadError(
            f"Medford adapter doesn't know how to download agenda_type="
            f"{meeting.agenda_type!r} (url={meeting.agenda_url!r})."
        )
