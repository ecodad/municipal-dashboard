# Architecture

> The technical design — how the modules fit together, what each one is
> responsible for, where data flows, and where the seams are. The README
> is the elevator pitch; this is the component-by-component reference.

## High-level data flow

The pipeline is structured around a **CityAdapter** protocol so each
city's calendar/detail/agenda specifics are encapsulated in one place.
Everything downstream of the adapter is city-agnostic.

```
   ┌──────────────────────────────────────────────────────────────────┐
   │ scraper/run_pipeline.py — orchestrator (city-agnostic)           │
   │  --municipality SLUG  →  load_adapter(slug)  →  CityAdapter      │
   └─────────────────────────────────┬────────────────────────────────┘
                                     │
                                     ▼
                  ┌────────────────────────────────────┐
                  │  CityAdapter (Protocol)            │
                  │   list_meetings(today, lookahead)  │
                  │     → list[MeetingRecord]          │
                  │   download_agenda(record, dir,     │
                  │                   stem)            │
                  │     → AgendaDownloadResult         │
                  └─────┬──────────────────────────┬───┘
                        │ implements               │ implements
       ┌────────────────▼─────────────┐ ┌──────────▼────────────────────┐
       │ MedfordAdapter                │ │ SomervilleAdapter (Phase 2)   │
       │  Finalsite calendar           │ │  Drupal /calendar list        │
       │   + Finalsite detail extract  │ │   + Drupal detail (sparse)    │
       │  Dispatches to host downloader│ │  Dispatches to:               │
       │   based on agenda_type        │ │   legistar / granicus / etc.  │
       └───────────────┬───────────────┘ └─────────────┬─────────────────┘
                       │                               │
                       ▼ composes                      ▼ composes
       ┌──────────────────────────────────────────────────────────────────┐
       │ Host-level downloaders (city-agnostic, reusable)                 │
       │   civicclerk_download.py  · google_download.py                   │
       │   legistar_download.py (Phase 2)  · granicus (later, on demand)  │
       └──────────────────────────────────────────────────────────────────┘
                       │
                       │ PDFs in agendas/{date}__{occur_id}__{slug}.pdf
                       ▼
            ┌──────────────────────────────┐
            │ parser.py — Claude Haiku 4.5  │
            │ PDF (base64 doc block) → MD   │
            └──────────────┬────────────────┘
                           │ markdown in agendas/markdown/{stem}.md
                           ▼
            ┌──────────────────────────────┐
            │ synthesizer.py                │
            │ Claude Sonnet 4.6 (adaptive   │
            │ thinking, structured JSON)    │
            └──────────────┬────────────────┘
                           │ items appended to agendas.json;
                           │ PDFs + .md moved to agendas/archived/
                           ▼
            ┌──────────────────────────────┐
            │ index.html (browser, vanilla  │
            │ HTML/CSS/JS) fetches          │
            │ agendas.json + branding.json  │
            └──────────────────────────────┘
```

## City adapter layer

Each city is a Python module under `scraper/adapters/` that exposes a
class implementing the `CityAdapter` Protocol (defined in
`scraper/adapters/__init__.py`).

| Element | Purpose |
|---|---|
| `MeetingRecord` (dataclass) | Currency between adapter → orchestrator. Carries `occur_id`, `title`, `start`, `detail_url`, `agenda_url`, `agenda_type`, `location`, `zoom_url`, `livestream_url`, plus an opaque `adapter_payload: dict` the adapter can stash anything in (e.g. a Legistar GUID). |
| `AgendaDownloadResult` (dataclass) | What `download_agenda` returns on success: `path`, `size_bytes`, `source_url`. |
| `AdapterDownloadError` | Exception type adapters wrap host-specific exceptions in. Orchestrator catches it to record `status=failed` per meeting. |
| `AGENDA_TYPE_MISSING` / `AGENDA_TYPE_UNSUPPORTED` (`"OTHER"`) | Sentinel strings on `MeetingRecord.agenda_type` that tell the orchestrator to skip the download step. Anything else is "downloadable" → handed back to `adapter.download_agenda()`. |
| `_REGISTRY` (slug → "module:Class") | Lazy-imported lookup. Orchestrator calls `load_adapter(slug)`. To add a city: write the module, add a registry entry, add `branding/{slug}.json`. |

The orchestrator never imports city-specific code. A new city is purely
additive — no edits to `run_pipeline.py`, the LLM agents, or the
dashboard.

## Module responsibilities

### Adapter layer

| Module | Responsibility |
|---|---|
| `scraper/adapters/__init__.py` | Protocol + dataclasses + registry + `load_adapter(slug)`. |
| `scraper/adapters/medford_ma.py` | `MedfordAdapter` — wraps Finalsite calendar + detail + dispatches to CivicClerk / Google host downloaders. |
| `scraper/adapters/{slug}.py` | One per city. Phase 2 will add `somerville_ma.py`. |

### Host-level downloaders (city-agnostic)

| Module | Responsibility | Inputs | Outputs |
|---|---|---|---|
| `scraper/civicclerk_download.py` | Download agendas from any CivicClerk tenant's OData API | Portal URL | PDF (default) or plain text |
| `scraper/google_download.py` | Download Google Doc / Drive agendas | Share URL | PDF |
| `scraper/legistar_download.py` (Phase 2) | Download from any Legistar tenant via `View.ashx?M=A` | Legistar URL | PDF |

### Medford-specific deterministic helpers (called by `MedfordAdapter`)

| Module | Responsibility | Inputs | Outputs |
|---|---|---|---|
| `scraper/calendar_scrape.py` | List meetings from Medford's Finalsite calendar | `today`, `lookahead_days` | `list[Meeting]` with `occur_id`, `title`, `start` ISO datetime, `detail_url` |
| `scraper/event_detail_scrape.py` | Parse Medford detail pages for agenda URL + attendance | `detail_url` | `EventDetail` with `agenda_url`, `agenda_type` enum, `location`, `zoom_url`, `livestream_url` |

Each is also a CLI: `python -m scraper.<module> [args]`.

### LLM agent layer (Anthropic SDK)

| Module | Model | Responsibility | Inputs | Outputs |
|---|---|---|---|---|
| `scraper/parser.py` | Haiku 4.5 | Faithful PDF → Markdown transcription | PDFs in `agendas/` | Markdown files in `agendas/markdown/` |
| `scraper/synthesizer.py` | Sonnet 4.6 (adaptive thinking, medium effort) | Classify and extract per-item fields, write JSON, archive sources | Markdown in `agendas/markdown/` + existing `agendas.json` | Updated `agendas.json`; sources moved to `agendas/archived/` |

### Orchestration

| Module | Role |
|---|---|
| `scraper/run_pipeline.py` | End-to-end driver. Default: scrape only. With `--process`: scrape → Parser → Synthesizer. Writes `agendas/.last_scraper_run.json` summary. |

## Idempotency model

Every stage is safe to re-run; each has its own dedup key:

| Stage | Dedup key | Behavior |
|---|---|---|
| Calendar scrape | n/a (read-only) | Always returns current calendar state |
| Detail scrape | n/a (read-only) | Always returns current detail-page state |
| Download (any source) | Filename contains `__{occur_id}__` substring; checks `agendas/` and `agendas/archived/` | Skips if already present |
| Parser | Output `{stem}.md` exists in target dir | Skipped unless `--force` |
| Synthesizer | `Source_File` already appears in `agendas.json` | Skipped (still archives the source) |

## Filesystem layout

```
municipal_dashboard/
├── .env                 ← gitignored, real ANTHROPIC_API_KEY
├── .env.example         ← committed template
├── .gitignore
├── README.md            ← public project overview
├── MEMORY.md            ← session-state log
├── ARCHITECTURE.md      ← this file
├── TARGET_SITES.md      ← external data sources
├── AGENTS.md            ← agent/module roles + permissions
├── TODO.md              ← pending work
├── SCHEDULING.md        ← runbook for the GH Actions cron
├── index.html           ← static dashboard (project banner + city section)
├── branding.json        ← active city branding (copied from branding/{slug}.json)
├── branding/
│   ├── medford-ma.json  ← per-city chrome (logo, colors, name, …)
│   └── {slug}.json      ← one per city
├── agendas.json         ← canonical structured data
├── requirements.txt     ← Python deps: requests, bs4, anthropic, python-dotenv
├── scraper/
│   ├── __init__.py
│   ├── adapters/
│   │   ├── __init__.py        ← CityAdapter Protocol + registry
│   │   └── medford_ma.py      ← MedfordAdapter
│   ├── calendar_scrape.py     ← Medford Finalsite calendar (used by MedfordAdapter)
│   ├── event_detail_scrape.py ← Medford detail-page parser
│   ├── civicclerk_download.py ← host-level (any CivicClerk tenant)
│   ├── google_download.py     ← host-level (any Google Doc/Drive share)
│   ├── parser.py              ← Claude Haiku PDF→MD
│   ├── synthesizer.py         ← Claude Sonnet MD→agendas.json
│   └── run_pipeline.py        ← city-agnostic orchestrator
└── agendas/
    ├── *.pdf            ← freshly-downloaded, not yet processed
    ├── markdown/        ← Parser output, transient
    │   └── *.md
    ├── archived/        ← post-Synthesizer audit trail
    │   ├── *.pdf
    │   └── *.md
    └── .last_scraper_run.json  ← gitignored, regenerated each run
```

## Data shapes

### `Meeting` (from calendar_scrape)

```python
@dataclass(frozen=True)
class Meeting:
    occur_id: str       # numeric, unique per Finalsite event occurrence
    title: str          # filtered to those containing "meeting"
    start: str          # ISO 8601 with timezone, e.g. "2026-04-30T09:30:00-04:00"
    detail_url: str     # https://www.medfordma.org/.../~occur-id/{N}
```

### `EventDetail` (from event_detail_scrape)

```python
@dataclass(frozen=True)
class EventDetail:
    occur_id: str
    detail_url: str
    agenda_url: Optional[str]
    agenda_type: AgendaType  # CIVICCLERK | GOOGLE_DOC | GOOGLE_DRIVE_FILE | OTHER | MISSING
    location: Optional[str]
    zoom_url: Optional[str]
    livestream_url: Optional[str]
    description_text: str    # raw description for diagnostics
```

### `agendas.json` (canonical output)

```json
{
  "metadata": {
    "processed_date": "YYYY-MM-DD",
    "documents_processed": [
      { "filename": "...", "status": "parsed", "item_count": 0 }
    ]
  },
  "items": [
    {
      "Committee_Name": "...",
      "Meeting_Date": "YYYY-MM-DD",
      "Meeting_Time": "9:30 AM",        // or null
      "Location": "...",                // or null
      "Item_Number": "26-074",          // or "1", "2.1", "Case #ZON26-..."
      "Item_Type": "Resolution",        // 10-value enum
      "Agenda_Topic": "...",
      "Source_File": "..."
    }
  ]
}
```

`Item_Type` enum: `Resolution | Ordinance | Public Hearing | Vote | Discussion | Communication | Report | Approval/Minutes | Procedural | Other`.

## Cost profile per run (rough)

| Stage | Cost | Why |
|---|---|---|
| Calendar scrape | $0 | 1 HTTP GET, deterministic parse |
| Detail scrape | $0 | N HTTP GETs (one per meeting), deterministic parse |
| CivicClerk / Google download | $0 | Public APIs, no auth |
| Parser (Haiku 4.5) | ~$0.005–0.02 per run for 7-meeting week | ~10–40 KB input PDF per call, ~3–8 KB output Markdown |
| Synthesizer (Sonnet 4.6) | ~$0.05–0.15 per run for 7-meeting week | ~3–8 KB input MD per call, structured JSON output, adaptive thinking |
| **Total per run** | **~$0.10 typical** | Fully deterministic stages dominate; LLM stages are bounded by meeting count |

## Where the seams are (for future maintainers)

- **Per-source downloaders are pluggable.** Add a new `agenda_type` enum
  value, write a `download_<source>(url, dest_dir, ...)` function, wire
  it into `run_pipeline.process_meeting()`. The rest of the pipeline
  doesn't need to change.
- **Parser/Synthesizer are independently swappable.** Either could be
  replaced with a different model or even a non-LLM approach (e.g.,
  pdfplumber + regex) without touching the orchestrator.
- **Dashboard JSON schema.** New fields can be added to `items[]`
  without breaking the existing dashboard — `index.html` ignores
  unknown keys.
- **Idempotency boundaries are explicit.** Each stage's dedup key is
  documented above; if you change a filename pattern, update both the
  downloader and `_already_have()` in `run_pipeline.py`.
