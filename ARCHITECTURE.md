# Architecture

> The technical design — how the modules fit together, what each one is
> responsible for, where data flows, and where the seams are. The README
> is the elevator pitch; this is the component-by-component reference.

## High-level data flow

```
                   ┌─────────────────────────────────────────────────┐
                   │  Medford events calendar (Finalsite, public)    │
                   └────────────────────────┬────────────────────────┘
                                            │ HTML (server-rendered)
                                            ▼
                          ┌─────────────────────────────┐
                          │ Step 1: calendar_scrape.py  │
                          │ (BeautifulSoup, no LLM)     │
                          └────────────┬────────────────┘
                                       │ list[Meeting] within window
                                       ▼
                          ┌─────────────────────────────┐
                          │ Step 2b: event_detail_scrape│
                          │ Per-meeting fetch + parse   │
                          └────────────┬────────────────┘
                                       │ EventDetail (agenda_url, type,
                                       │              location, zoom)
                                       ▼
                       ┌──── dispatch on agenda_type ────┐
                       ▼               ▼                 ▼
   ┌─────────────────────┐ ┌──────────────────────┐ ┌─────────────────────┐
   │ Step 2a:            │ │ Step 2c+2d:          │ │ MISSING / OTHER:    │
   │ civicclerk_download │ │ google_download.py   │ │ surface as "no      │
   │ OData fileId stream │ │ Doc export / Drive   │ │ agenda" in JSON;    │
   │ (no auth)           │ │ uc?export=download   │ │ no file written     │
   └──────────┬──────────┘ └──────────┬───────────┘ └─────────────────────┘
              └──────────┬────────────┘
                         │ PDFs in agendas/{date}__{occur_id}__{slug}.pdf
                         ▼
            ┌──────────────────────────────┐
            │ Step 3a: parser.py            │
            │ Claude Haiku 4.5              │
            │ PDF (base64 doc block) → MD   │
            └──────────────┬────────────────┘
                           │ markdown in agendas/markdown/{stem}.md
                           ▼
            ┌──────────────────────────────┐
            │ Step 3b: synthesizer.py       │
            │ Claude Sonnet 4.6 (adaptive   │
            │ thinking, structured JSON)    │
            └──────────────┬────────────────┘
                           │ items appended to agendas.json;
                           │ PDFs + .md moved to agendas/archived/
                           ▼
            ┌──────────────────────────────┐
            │ index.html (browser, vanilla  │
            │ HTML/CSS/JS) fetches          │
            │ agendas.json                  │
            └──────────────────────────────┘
```

## Module responsibilities

### Deterministic scraper layer (zero LLM tokens)

| Module | Responsibility | Inputs | Outputs |
|---|---|---|---|
| `scraper/calendar_scrape.py` | List meetings in a lookahead window | `today: date`, `lookahead_days: int` | `list[Meeting]` with `occur_id`, `title`, `start` ISO datetime, `detail_url` |
| `scraper/event_detail_scrape.py` | Pull agenda URL + attendance details from each meeting's detail page | `detail_url: str` | `EventDetail` with `agenda_url`, `agenda_type` enum, `location`, `zoom_url`, `livestream_url` |
| `scraper/civicclerk_download.py` | Download agendas from CivicClerk's OData API | CivicClerk portal URL | PDF (default) or plain text |
| `scraper/google_download.py` | Download Google Doc / Drive agendas | Share URL | PDF |

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
├── MEMORY.md            ← session-state log (this directory)
├── ARCHITECTURE.md      ← this file
├── TARGET_SITES.md      ← external data sources
├── AGENTS.md            ← agent/module roles + permissions
├── TODO.md              ← pending work
├── index.html           ← static dashboard (vanilla HTML/CSS/JS)
├── agendas.json         ← canonical structured data
├── requirements.txt     ← Python deps: requests, bs4, anthropic, python-dotenv
├── scraper/
│   ├── __init__.py
│   ├── calendar_scrape.py
│   ├── event_detail_scrape.py
│   ├── civicclerk_download.py
│   ├── google_download.py
│   ├── parser.py
│   ├── synthesizer.py
│   └── run_pipeline.py
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
