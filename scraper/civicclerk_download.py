"""
Download CivicClerk-hosted agenda files.

CivicClerk's public portal (medfordma.portal.civicclerk.com) shows the
agenda inside a React SPA, but the underlying API at
medfordma.api.civicclerk.com is fully public — no Bearer token, no
session cookies, no auth headers required.

The download endpoint is OData-style:

    GET /v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)

`plainText=true` returns the OCR/text export instead of the PDF. Both are
generated server-side; the text export occasionally returns 0 bytes when
CivicClerk hasn't run extraction yet.

Inputs to this module are CivicClerk *portal* URLs of the shape:

    https://medfordma.portal.civicclerk.com/event/{event_id}/files/agenda/{file_id}

We extract `file_id` (the only piece the API actually needs) and stream
the response to disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import requests

CIVICCLERK_API_BASE = "https://medfordma.api.civicclerk.com/v1"
CIVICCLERK_PORTAL_HOST = "medfordma.portal.civicclerk.com"

PORTAL_AGENDA_RE = re.compile(
    r"/event/(?P<event_id>\d+)/files/agenda/(?P<file_id>\d+)"
)

REQUEST_TIMEOUT_SECONDS = 60  # PDFs can be hundreds of KB; allow time
USER_AGENT = (
    "Mozilla/5.0 (compatible; MedfordAgendaScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)


Format = Literal["pdf", "text"]


class CivicClerkDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadResult:
    file_id: str
    path: Path
    format: Format
    size_bytes: int
    url: str


def parse_file_id(portal_url: str) -> str:
    """Extract the agenda file_id from a CivicClerk portal URL."""
    if CIVICCLERK_PORTAL_HOST not in portal_url:
        raise CivicClerkDownloadError(
            f"Not a CivicClerk portal URL (expected host "
            f"'{CIVICCLERK_PORTAL_HOST}'): {portal_url}"
        )
    m = PORTAL_AGENDA_RE.search(portal_url)
    if not m:
        raise CivicClerkDownloadError(
            f"Couldn't find /event/<id>/files/agenda/<id> in: {portal_url}"
        )
    return m.group("file_id")


def _stream_url(file_id: str, fmt: Format) -> str:
    plain_text = "true" if fmt == "text" else "false"
    return (
        f"{CIVICCLERK_API_BASE}/Meetings/"
        f"GetMeetingFileStream(fileId={file_id},plainText={plain_text})"
    )


def download_agenda(
    portal_url: str,
    dest_dir: Path,
    fmt: Format = "pdf",
    filename_stem: str | None = None,
) -> DownloadResult:
    """Download a CivicClerk agenda to dest_dir.

    Args:
        portal_url: The CivicClerk portal URL exposed by the city's
            events-calendar detail page.
        dest_dir: Destination directory; created if it doesn't exist.
        fmt: 'pdf' (default) or 'text'.
        filename_stem: Override the file stem; defaults to
            ``civicclerk_<file_id>``. The extension is appended based on fmt.

    Returns:
        DownloadResult with the saved path and basic metadata.

    Raises:
        CivicClerkDownloadError on HTTP errors, empty responses, or
        wrong content type.
    """
    file_id = parse_file_id(portal_url)
    api_url = _stream_url(file_id, fmt)

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = filename_stem or f"civicclerk_{file_id}"
    extension = "pdf" if fmt == "pdf" else "txt"
    out_path = dest_dir / f"{stem}.{extension}"

    try:
        with requests.get(
            api_url,
            headers={
                "User-Agent": USER_AGENT,
                "Origin": f"https://{CIVICCLERK_PORTAL_HOST}",
                "Accept": "application/pdf,text/plain,*/*",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                raise CivicClerkDownloadError(
                    f"HTTP {resp.status_code} for {api_url}"
                )
            content_type = resp.headers.get("content-type", "")
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as err:
        raise CivicClerkDownloadError(f"Network error: {err}") from err

    size = out_path.stat().st_size
    if size == 0:
        # Still useful to know but not always fatal; let caller decide.
        # We surface the empty file plus its (zero) size; for text format,
        # CivicClerk sometimes hasn't run extraction yet.
        if fmt == "pdf":
            raise CivicClerkDownloadError(
                f"PDF download for file_id={file_id} returned 0 bytes."
            )

    if fmt == "pdf" and size > 0:
        with out_path.open("rb") as f:
            magic = f.read(4)
        if magic != b"%PDF":
            raise CivicClerkDownloadError(
                f"Downloaded bytes for file_id={file_id} don't start with "
                f"%PDF (got {magic!r}). Wrong content-type {content_type!r}?"
            )

    return DownloadResult(
        file_id=file_id, path=out_path, format=fmt,
        size_bytes=size, url=api_url,
    )


# ---- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Download a CivicClerk agenda by portal URL."
    )
    parser.add_argument(
        "portal_url",
        help="CivicClerk portal URL, e.g. "
             "https://medfordma.portal.civicclerk.com/event/440/files/agenda/855",
    )
    parser.add_argument(
        "--dest",
        default=".",
        help="Destination directory (default: current dir).",
    )
    parser.add_argument(
        "--format",
        choices=["pdf", "text"],
        default="pdf",
        help="Download format (default: pdf).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Override the output filename stem (extension is appended).",
    )
    args = parser.parse_args(argv)

    try:
        result = download_agenda(
            portal_url=args.portal_url,
            dest_dir=Path(args.dest),
            fmt=args.format,
            filename_stem=args.name,
        )
    except CivicClerkDownloadError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    print(
        f"Downloaded file_id={result.file_id} ({result.format}, "
        f"{result.size_bytes:,} bytes) -> {result.path}"
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
