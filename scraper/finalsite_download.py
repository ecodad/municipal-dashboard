"""
Download an agenda PDF hosted on Finalsite's Resource Manager.

Medford posts a growing share of board/commission agendas to its
Finalsite CMS's Resource Manager rather than to CivicClerk or Google.
These links look like:

    https://medfordmaorg.finalsite.com/fs/resource-manager/view/{uuid}

That URL 302-redirects to a direct PDF on Finalsite's asset CDN
(`resources.finalsite.net/.../<name>.pdf`, `Content-Type: application/pdf`).
No auth is required.

This module is intentionally city-agnostic: it accepts any Finalsite
resource-manager or asset-CDN URL, follows redirects, and validates that
the response is a real PDF (`%PDF` magic). Callers (adapters) pick the
right URL out of a city's detail page.
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


class FinalsiteDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class FinalsiteDownloadResult:
    path: Path
    size_bytes: int
    source_url: str


def _is_finalsite_url(url: str) -> bool:
    """True if `url` points at a Finalsite resource-manager or asset CDN."""
    host = urlparse(url).hostname or ""
    if host.endswith(".finalsite.com"):
        return True
    if host.endswith(".finalsite.net"):
        return True
    return False


def download_finalsite_agenda(
    url: str,
    dest_dir: Path,
    filename_stem: str | None = None,
) -> FinalsiteDownloadResult:
    """Download a Finalsite-hosted PDF to dest_dir.

    Args:
        url: Finalsite resource-manager (`/fs/resource-manager/view/{uuid}`)
            or asset-CDN URL. Redirects are followed transparently.
        dest_dir: Destination directory; created if missing.
        filename_stem: Output file stem (`.pdf` is appended). Defaults to
            the basename of the final (post-redirect) URL with its
            extension stripped.

    Raises:
        FinalsiteDownloadError on non-Finalsite URLs, HTTP errors, empty
        responses, or non-PDF content.
    """
    if not _is_finalsite_url(url):
        raise FinalsiteDownloadError(f"Not a Finalsite URL: {url}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

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
                raise FinalsiteDownloadError(f"HTTP {resp.status_code} for {url}")
            # Resolve the filename from the final URL after redirects.
            if filename_stem is None:
                last = Path(urlparse(resp.url).path).stem or "finalsite_agenda"
                filename_stem = last
            out_path = dest_dir / f"{filename_stem}.pdf"
            written = 0
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
    except requests.RequestException as err:
        raise FinalsiteDownloadError(f"Network error: {err}") from err

    if written == 0:
        raise FinalsiteDownloadError(
            f"Finalsite download returned 0 bytes for {url}"
        )

    with out_path.open("rb") as f:
        magic = f.read(4)
    if magic != b"%PDF":
        with out_path.open("rb") as f:
            preview = f.read(160).decode("utf-8", errors="replace")
        raise FinalsiteDownloadError(
            f"Finalsite response for {url} isn't a PDF (got magic={magic!r}). "
            f"First bytes: {preview!r}"
        )

    return FinalsiteDownloadResult(
        path=out_path,
        size_bytes=written,
        source_url=url,
    )


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download a Finalsite-hosted agenda PDF."
    )
    parser.add_argument(
        "url",
        help="Finalsite resource-manager or asset-CDN URL.",
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
        result = download_finalsite_agenda(
            url=args.url,
            dest_dir=Path(args.dest),
            filename_stem=args.name,
        )
    except FinalsiteDownloadError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    print(
        f"Downloaded Finalsite agenda ({result.size_bytes:,} bytes) "
        f"-> {result.path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
