"""
Download an agenda PDF from a Legistar legislative-management portal.

Legistar (acquired by Granicus) is the system of record for City Council
legislative items in many municipalities. In Somerville, every City
Council standing committee (Finance, Land Use, Legislative Matters,
Confirmation of Appointments, Public Health & Safety, etc.) routes its
agenda through `somervillema.legistar.com` while non-Council bodies
post directly to S3.

Two URL forms can land here:

  Gateway (what the city's Drupal calendar links to):
      https://{host}.legistar.com/Gateway.aspx?M=MD&From=RSS&ID={ID}&GUID={GUID}

  View.ashx (the actual PDF endpoint):
      https://{host}.legistar.com/View.ashx?M=A&ID={ID}&GUID={GUID}

The two share `ID` and `GUID` — a Gateway URL can be transformed to its
View.ashx equivalent without a second HTTP fetch by swapping `M=MD` →
`M=A`. This module accepts either form and dispatches accordingly.

Both endpoints are publicly served (no auth, no cookies, no CSRF token);
`Content-Type: application/pdf` is returned directly.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = (
    "Mozilla/5.0 (compatible; MunicipalDashboardScraper/0.1; "
    "+https://github.com/ecodad/municipal-dashboard)"
)


class LegistarDownloadError(RuntimeError):
    pass


class LegistarAgendaNotPosted(LegistarDownloadError):
    """Legistar responded HTTP 200 with an empty body.

    This is Legistar's way of saying the agenda PDF hasn't been uploaded
    yet (rather than returning 404). Semantically MISSING, not a failure.
    """


@dataclass(frozen=True)
class LegistarDownloadResult:
    path: Path
    size_bytes: int
    source_url: str    # the original input URL (Gateway or View.ashx)
    download_url: str  # the resolved View.ashx URL we actually fetched
    legistar_id: str
    legistar_guid: str


def _parse_legistar_url(url: str) -> tuple[str, str, str]:
    """Extract (host, ID, GUID) from a Legistar Gateway or View.ashx URL.

    Raises LegistarDownloadError if the URL isn't a recognized Legistar
    URL, or if the required ID/GUID query parameters are missing.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host.endswith(".legistar.com"):
        raise LegistarDownloadError(
            f"Not a Legistar URL (expected *.legistar.com host): {url}"
        )

    path = parsed.path.lower()
    if not (path.endswith("/gateway.aspx") or path.endswith("/view.ashx")):
        raise LegistarDownloadError(
            f"Unrecognized Legistar path (expected Gateway.aspx or "
            f"View.ashx): {url}"
        )

    qs = parse_qs(parsed.query)
    try:
        legistar_id = qs["ID"][0]
        legistar_guid = qs["GUID"][0]
    except (KeyError, IndexError) as err:
        raise LegistarDownloadError(
            f"Legistar URL is missing ID/GUID query params: {url}"
        ) from err

    return host, legistar_id, legistar_guid


def build_view_ashx_url(gateway_or_view_url: str) -> str:
    """Return the View.ashx?M=A&ID=...&GUID=... PDF URL for a Legistar URL.

    Works whether the input is a Gateway link (M=MD) or already a
    View.ashx link — same ID and GUID either way.
    """
    host, legistar_id, legistar_guid = _parse_legistar_url(gateway_or_view_url)
    return (
        f"https://{host}/View.ashx?M=A"
        f"&ID={legistar_id}&GUID={legistar_guid}"
    )


def download_legistar_agenda(
    url: str,
    dest_dir: Path,
    filename_stem: str | None = None,
) -> LegistarDownloadResult:
    """Download a Legistar agenda PDF.

    Args:
        url: Either a Gateway URL (`Gateway.aspx?M=MD&...`) as found on
            the city's calendar detail page, or a direct View.ashx URL.
        dest_dir: Destination directory; created if missing.
        filename_stem: Output file stem (`.pdf` is appended). Defaults
            to ``legistar_{ID}``.

    Raises:
        LegistarDownloadError on parse failure, HTTP errors, empty
        responses, or non-PDF content.
    """
    host, legistar_id, legistar_guid = _parse_legistar_url(url)
    download_url = (
        f"https://{host}/View.ashx?M=A"
        f"&ID={legistar_id}&GUID={legistar_guid}"
    )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = filename_stem or f"legistar_{legistar_id}"
    out_path = dest_dir / f"{stem}.pdf"

    try:
        with requests.get(
            download_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,*/*",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                raise LegistarDownloadError(
                    f"HTTP {resp.status_code} for {download_url}"
                )
            written = 0
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
    except requests.RequestException as err:
        raise LegistarDownloadError(f"Network error: {err}") from err

    if written == 0:
        out_path.unlink(missing_ok=True)
        raise LegistarAgendaNotPosted(
            f"Legistar download for ID={legistar_id} returned 0 bytes "
            f"(agenda not yet posted)."
        )

    with out_path.open("rb") as f:
        magic = f.read(4)
    if magic != b"%PDF":
        with out_path.open("rb") as f:
            preview = f.read(160).decode("utf-8", errors="replace")
        raise LegistarDownloadError(
            f"Legistar response for ID={legistar_id} isn't a PDF "
            f"(got magic={magic!r}). First bytes: {preview!r}"
        )

    return LegistarDownloadResult(
        path=out_path,
        size_bytes=written,
        source_url=url,
        download_url=download_url,
        legistar_id=legistar_id,
        legistar_guid=legistar_guid,
    )


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download a Legistar agenda PDF by Gateway or View.ashx URL."
        ),
    )
    parser.add_argument(
        "url",
        help=(
            "Legistar Gateway URL (Gateway.aspx?M=MD&...) or direct "
            "View.ashx URL."
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
        result = download_legistar_agenda(
            url=args.url,
            dest_dir=Path(args.dest),
            filename_stem=args.name,
        )
    except LegistarDownloadError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    print(
        f"Downloaded Legistar ID={result.legistar_id} "
        f"({result.size_bytes:,} bytes) -> {result.path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
