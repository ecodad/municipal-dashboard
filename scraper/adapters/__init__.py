"""
City adapter framework for the Municipal Dashboards pipeline.

A `CityAdapter` encapsulates everything city-specific in the pipeline:
how to list upcoming meetings, what their attendance details look like,
and how to download each agenda PDF. Everything downstream of the
adapter (PDF parser, JSON synthesizer, dashboard UI) is city-agnostic.

To add a new city:

  1. Write `scraper/adapters/{slug}.py` that defines a class
     implementing the CityAdapter Protocol.
  2. Register it in `_REGISTRY` below.
  3. Add `branding/{slug}.json` for the dashboard's city-section
     branding (logo, colors, name, etc.).
  4. Run the pipeline with `--municipality {slug}` (or set the
     `MUNICIPALITY_SLUG` env var, which is what the GitHub Actions
     workflow uses).

The orchestrator (`scraper/run_pipeline.py`) does not import any
specific city's modules — it only ever asks `load_adapter(slug)` for
the right one. A forker who only cares about their own city should not
need to touch the orchestrator at all.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


# --- Sentinel agenda_type values understood by the orchestrator ---------
#
# Adapters can emit any string they want for `agenda_type` (e.g.
# "CIVICCLERK", "GOOGLE_DOC", "LEGISTAR"). The orchestrator only
# special-cases these two: meetings whose agenda_type is in
# NON_DOWNLOADABLE_AGENDA_TYPES are skipped without invoking
# `download_agenda`. Anything else is handed back to the adapter.

AGENDA_TYPE_MISSING = "MISSING"          # no agenda link on the city page
AGENDA_TYPE_UNSUPPORTED = "OTHER"        # link exists but adapter can't fetch it
NON_DOWNLOADABLE_AGENDA_TYPES = frozenset(
    {AGENDA_TYPE_MISSING, AGENDA_TYPE_UNSUPPORTED}
)


@dataclass(frozen=True)
class MeetingRecord:
    """A single meeting plus enough metadata to download its agenda.

    Fields below mirror the pre-adapter `EventDetail` shape so the
    pipeline's downstream JSON output (`.last_scraper_run.json`,
    eventually `agendas.json` via the synthesizer) stays the same.

    `adapter_payload` is opaque to the orchestrator — the adapter that
    produced this record is the only thing that reads it back.
    Use it to stash whatever you'll need later in `download_agenda`
    (e.g. a Legistar GUID, a CivicClerk file_id).
    """

    occur_id: str
    title: str
    start: str  # ISO 8601 with timezone, e.g. "2026-04-30T09:30:00-04:00"
    detail_url: str
    agenda_url: Optional[str]
    agenda_type: str
    location: Optional[str]
    zoom_url: Optional[str]
    livestream_url: Optional[str]
    adapter_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_downloadable(self) -> bool:
        return self.agenda_type not in NON_DOWNLOADABLE_AGENDA_TYPES


@dataclass(frozen=True)
class AgendaDownloadResult:
    """What an adapter returns from `download_agenda` on success."""

    path: Path
    size_bytes: int
    source_url: str


class AdapterDownloadError(RuntimeError):
    """Raised by `CityAdapter.download_agenda` on any download failure.

    Adapters should wrap host-specific exceptions (e.g.
    `CivicClerkDownloadError`, `GoogleDownloadError`) in this so the
    orchestrator can record a uniform `status=failed` outcome.
    """


@runtime_checkable
class CityAdapter(Protocol):
    """Per-city scraper. The only city-specific surface in the pipeline."""

    slug: str       # registry key, e.g. "medford-ma" — also used for branding/{slug}.json
    name: str       # human-readable, e.g. "Medford, MA"
    site_path: str  # URL/dir slug under repo root, e.g. "medford" → /medford/

    def list_meetings(
        self,
        today: date,
        lookahead_days: int,
        *,
        debug_dir: Optional[Path] = None,
    ) -> list[MeetingRecord]:
        """Return meetings starting in [today, today + lookahead_days].

        Adapters that hit external HTTP APIs SHOULD honor `debug_dir` by
        writing raw responses to it for forensic debugging when given.
        Adapters that don't need this can ignore the kwarg. The
        orchestrator passes ``working_dir / ".last_calendar_responses"``;
        the directory is wiped at the start of each run.
        """

    def download_agenda(
        self,
        meeting: MeetingRecord,
        dest_dir: Path,
        filename_stem: str,
    ) -> AgendaDownloadResult:
        """Download `meeting`'s agenda into dest_dir/{filename_stem}.{ext}."""


# --- Registry -----------------------------------------------------------
#
# Maps slug -> "module.path:ClassName". Lazy-imported by `load_adapter`
# so unrelated cities don't pay import cost (and so a fork can add a
# new entry without touching anything else).

_REGISTRY: dict[str, str] = {
    "medford-ma": "scraper.adapters.medford_ma:MedfordAdapter",
    "somerville-ma": "scraper.adapters.somerville_ma:SomervilleAdapter",
}


def registered_slugs() -> list[str]:
    return sorted(_REGISTRY.keys())


def load_adapter(slug: str) -> CityAdapter:
    """Look up and instantiate a CityAdapter by slug."""
    if slug not in _REGISTRY:
        known = ", ".join(registered_slugs()) or "(none registered)"
        raise KeyError(
            f"Unknown municipality slug: {slug!r}. Registered: {known}"
        )
    module_path, class_name = _REGISTRY[slug].split(":")
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    instance = cls()
    if not isinstance(instance, CityAdapter):  # runtime_checkable Protocol
        raise TypeError(
            f"{module_path}:{class_name} does not satisfy the CityAdapter "
            f"Protocol (missing slug/name/list_meetings/download_agenda?)"
        )
    return instance


__all__ = [
    "AGENDA_TYPE_MISSING",
    "AGENDA_TYPE_UNSUPPORTED",
    "NON_DOWNLOADABLE_AGENDA_TYPES",
    "AdapterDownloadError",
    "AgendaDownloadResult",
    "CityAdapter",
    "MeetingRecord",
    "load_adapter",
    "registered_slugs",
]
