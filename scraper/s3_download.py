"""
Download an agenda PDF hosted on a public AWS S3 bucket.

Used today by the Somerville adapter for non-City-Council bodies (School
Committee, Planning Board, Historic Preservation Commission, etc.) which
post agendas directly to `s3.amazonaws.com/somervillema-live/`. No auth
is required — the bucket is public and `Content-Type: application/pdf`
is served directly.

This module is intentionally city-agnostic: it accepts any S3 HTTPS URL
and validates that the response is a real PDF (`%PDF` magic). Callers
(adapters) are responsible for picking the right URL out of a city's
detail page — for example, by filtering hrefs containing the substring
"agenda" so meeting-notice and flyer PDFs aren't mistaken for agendas.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = (
    "Mozilla/5.0 (compatible; MunicipalDashboardScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)


class S3DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class S3DownloadResult:
    path: Path
    size_bytes: int
    source_url: str


def _is_s3_url(url: str) -> bool:
    """True if `url` points at AWS S3 (any of the standard URL forms)."""
    host = urlparse(url).hostname or ""
    if host == "s3.amazonaws.com":
        return True
    if host.endswith(".s3.amazonaws.com"):
        return True
    # Region-scoped virtual-hosted style: <bucket>.s3.<region>.amazonaws.com
    if ".s3." in host and host.endswith(".amazonaws.com"):
        return True
    return False


def download_s3_agenda(
    url: str,
    dest_dir: Path,
    filename_stem: str | None = None,
) -> S3DownloadResult:
    """Download a public S3-hosted PDF to dest_dir.

    Args:
        url: HTTPS S3 URL pointing directly at a PDF object.
        dest_dir: Destination directory; created if missing.
        filename_stem: Output file stem (`.pdf` is appended). Defaults
            to the basename of the S3 object key with its extension
            stripped.

    Raises:
        S3DownloadError on non-S3 URLs, HTTP errors, empty responses,
        or non-PDF content.
    """
    if not _is_s3_url(url):
        raise S3DownloadError(f"Not an S3 URL: {url}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if filename_stem is None:
        # Last path segment minus its extension. Falls back to a fixed
        # stem if the URL has no path (shouldn't happen for real
        # objects).
        last = Path(urlparse(url).path).stem or "s3_agenda"
        filename_stem = last
    out_path = dest_dir / f"{filename_stem}.pdf"

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
                raise S3DownloadError(f"HTTP {resp.status_code} for {url}")
            written = 0
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
    except requests.RequestException as err:
        raise S3DownloadError(f"Network error: {err}") from err

    if written == 0:
        raise S3DownloadError(f"S3 download returned 0 bytes for {url}")

    with out_path.open("rb") as f:
        magic = f.read(4)
    if magic != b"%PDF":
        with out_path.open("rb") as f:
            preview = f.read(160).decode("utf-8", errors="replace")
        raise S3DownloadError(
            f"S3 response for {url} isn't a PDF (got magic={magic!r}). "
            f"First bytes: {preview!r}"
        )

    return S3DownloadResult(
        path=out_path,
        size_bytes=written,
        source_url=url,
    )


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download a public S3-hosted agenda PDF."
    )
    parser.add_argument(
        "url",
        help="HTTPS S3 URL pointing at a PDF object.",
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
        result = download_s3_agenda(
            url=args.url,
            dest_dir=Path(args.dest),
            filename_stem=args.name,
        )
    except S3DownloadError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    print(
        f"Downloaded S3 agenda ({result.size_bytes:,} bytes) -> {result.path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
