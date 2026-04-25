"""
Step 2c + 2d: download agenda PDFs hosted on Google Docs or Google Drive.

Both sources are public, no auth required. The two URL patterns we see
on Medford's events calendar are:

    https://docs.google.com/document/d/{DOC_ID}/edit?usp=sharing&...
    https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing

Translated to download URLs they become:

    https://docs.google.com/document/d/{DOC_ID}/export?format=pdf
    https://drive.google.com/uc?export=download&id={FILE_ID}

For Google Drive, the response redirects to drive.usercontent.google.com
(handled transparently by `requests.get(..., allow_redirects=True)`).

Both downloads are validated to ensure the response is actually a PDF
(`%PDF` magic) — otherwise we usually got an HTML interstitial (rare for
publicly-shared agenda-sized files, but worth catching).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import requests

REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = (
    "Mozilla/5.0 (compatible; MedfordAgendaScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)

GOOGLE_DOC_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")
GOOGLE_DRIVE_RE = re.compile(
    r"(?:drive|docs)\.google\.com/(?:file|uc)/(?:d/)?([A-Za-z0-9_-]+)"
)


class GoogleSource(str, Enum):
    DOC = "GOOGLE_DOC"
    DRIVE = "GOOGLE_DRIVE_FILE"


class GoogleDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleDownloadResult:
    google_id: str
    source: GoogleSource
    path: Path
    size_bytes: int
    download_url: str


def _detect(url: str) -> tuple[GoogleSource, str]:
    """Detect whether `url` is a Google Doc or Drive file, and return the ID."""
    m = GOOGLE_DOC_RE.search(url)
    if m:
        return GoogleSource.DOC, m.group(1)
    m = GOOGLE_DRIVE_RE.search(url)
    if m:
        return GoogleSource.DRIVE, m.group(1)
    raise GoogleDownloadError(
        f"URL doesn't look like a Google Doc or Drive file: {url}"
    )


def _download_url_for(source: GoogleSource, google_id: str) -> str:
    if source is GoogleSource.DOC:
        return (
            f"https://docs.google.com/document/d/{google_id}/export?format=pdf"
        )
    return f"https://drive.google.com/uc?export=download&id={google_id}"


def _stream_to_file(url: str, dest: Path) -> int:
    """Stream a URL to `dest`, return bytes written."""
    try:
        with requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,*/*",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                raise GoogleDownloadError(
                    f"HTTP {resp.status_code} for {url}"
                )
            written = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            return written
    except requests.RequestException as err:
        raise GoogleDownloadError(f"Network error: {err}") from err


def _validate_pdf(path: Path, source: GoogleSource, google_id: str) -> None:
    """Sanity-check that what we just downloaded is actually a PDF."""
    size = path.stat().st_size
    if size == 0:
        raise GoogleDownloadError(
            f"{source.value} download for id={google_id} returned 0 bytes."
        )
    with path.open("rb") as f:
        magic = f.read(4)
    if magic != b"%PDF":
        # Read a slice of the body for diagnostics; HTML interstitials
        # tend to start with '<!DOC' or '<htm' or whitespace.
        with path.open("rb") as f:
            preview = f.read(160).decode("utf-8", errors="replace")
        raise GoogleDownloadError(
            f"{source.value} download for id={google_id} isn't a PDF "
            f"(got magic={magic!r}). First bytes: {preview!r}"
        )


def download_google_agenda(
    url: str,
    dest_dir: Path,
    filename_stem: str | None = None,
) -> GoogleDownloadResult:
    """Download a Google Doc or Drive PDF agenda to `dest_dir`."""
    source, google_id = _detect(url)
    download_url = _download_url_for(source, google_id)

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = filename_stem or (
        f"gdoc_{google_id}" if source is GoogleSource.DOC else f"gdrive_{google_id}"
    )
    out_path = dest_dir / f"{stem}.pdf"

    written = _stream_to_file(download_url, out_path)
    _validate_pdf(out_path, source, google_id)

    return GoogleDownloadResult(
        google_id=google_id,
        source=source,
        path=out_path,
        size_bytes=written,
        download_url=download_url,
    )


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download a Google Doc or Google Drive agenda PDF."
    )
    parser.add_argument(
        "url",
        help=(
            "A docs.google.com/document/... or drive.google.com/file/... "
            "URL (the share URL the city posts)."
        ),
    )
    parser.add_argument(
        "--dest", default=".",
        help="Destination directory (default: current dir).",
    )
    parser.add_argument(
        "--name", default=None,
        help="Override the output filename stem; '.pdf' is appended.",
    )
    args = parser.parse_args(argv)

    try:
        result = download_google_agenda(
            url=args.url,
            dest_dir=Path(args.dest),
            filename_stem=args.name,
        )
    except GoogleDownloadError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    print(
        f"Downloaded {result.source.value} id={result.google_id} "
        f"({result.size_bytes:,} bytes) -> {result.path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
