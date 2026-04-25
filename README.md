# Medford Municipal Agendas Dashboard

A static dashboard that surfaces what's on the agenda across Medford, MA's
boards, commissions, and council meetings. Source agendas (PDFs) are processed
by a two-stage Claude agent pipeline into a single `agendas.json` file, which
the dashboard fetches and renders in the browser.

**Live site:** https://ecodad.github.io/municipal-dashboard/

## Architecture

```
┌────────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ agendas/*.pdf      │ →  │ Parser Agent     │ →  │ agendas/markdown/*.md│
│ (raw source docs)  │    │ (Claude Haiku)   │    │ (clean structured md)│
└────────────────────┘    └──────────────────┘    └──────────┬───────────┘
                                                             │
                                                             ▼
┌────────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ index.html         │ ←  │ agendas.json     │ ←  │ Synthesizer Agent    │
│ (static dashboard) │    │ (canonical data) │    │ (Claude Sonnet)      │
└────────────────────┘    └──────────────────┘    └──────────────────────┘
                                                             │
                                                             ▼
                                                  ┌──────────────────────┐
                                                  │ agendas/archived/    │
                                                  │ (PDFs + .md moved    │
                                                  │  here after a run)   │
                                                  └──────────────────────┘
```

### Components

| Path | Purpose |
| --- | --- |
| `agendas/` | Drop new `.pdf` (or `.docx`) agenda documents here for the next pipeline run. |
| `agendas/markdown/` | Intermediate Markdown produced by the Parser. Cleared on every successful run. |
| `agendas/archived/` | Originals + Markdown after they've been folded into `agendas.json`. |
| `agendas.json` | Canonical structured data the dashboard reads. Includes a `metadata` block (run date + per-document item counts) and an `items` array. |
| `index.html` | Single-file static dashboard — vanilla HTML/CSS/JS, no build step, fetches `agendas.json` at load. |

### Data schema (`agendas.json`)

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
      "Meeting_Time": "6:30 PM",
      "Location": "...",
      "Item_Number": "1",
      "Item_Type": "Resolution",
      "Agenda_Topic": "...",
      "Source_File": "..."
    }
  ]
}
```

`Item_Type` is constrained to one of:
`Resolution`, `Ordinance`, `Public Hearing`, `Vote`, `Discussion`,
`Communication`, `Report`, `Approval/Minutes`, `Procedural`, `Other`.

### Agent team

The pipeline uses two specialized Claude agents, deliberately split for cost
and quality:

- **Parser Agent** — Claude Haiku 4.5. Fast, faithful PDF→Markdown.
  Skips legacy `.doc` files (Word 97-2003 binary) and logs them.
- **Synthesizer Agent** — Claude Sonnet 4.6. Reads the Parser's Markdown,
  classifies each item, and emits the canonical JSON. Then archives sources.

### Front-end

`index.html` is intentionally framework-free — one HTML file with embedded
CSS and a single vanilla-JS module. It uses City of Medford branding
(navy `#25347a`, Merriweather + Lato) and renders meeting cards grouped
by `(Committee, Date)`, sortable by a "Next 7 days / 30 days / All / Past"
time tab plus committee, type, and full-text filters. The default view
answers the dashboard's primary question: **"What's on the agenda this week?"**

## Running the pipeline

1. Drop new `.pdf` files into `agendas/` (root, not subfolders).
2. Run the Parser Agent (Claude Haiku) over `agendas/`.
3. Run the Synthesizer Agent (Claude Sonnet) over `agendas/markdown/`.
4. The Synthesizer writes a fresh `agendas.json` and moves originals +
   Markdown into `agendas/archived/`.
5. Commit `agendas.json` and push to `main`. GitHub Pages serves the update.

## Known limitations

- **Legacy `.doc` files are not supported.** Convert to `.pdf` or `.docx`
  before placing them in `agendas/`. The Parser currently skips and logs
  binary Word 97-2003 files. (`.docx` works.)
- The dashboard fetches `agendas.json` from the same origin as the page,
  so it always reflects whatever has been pushed to `main`.
- `Item_Type` classification is heuristic; review high-stakes items in
  context before acting on them.

## Future Roadmap

- [ ] **Web Scraper Agent** — automatically monitor the City of Medford
  web calendar (https://www.medfordma.org/about/events-calendar),
  download newly posted PDF agendas, and pipe them to the Parser Agent.
  When this lands, JSON items will gain a `Source_URL` field linking
  back to the official agenda document so dashboard rows can deep-link
  to the source.
- [ ] **Scheduled pipeline** — run the full Scraper → Parser → Synthesizer
  → commit chain on a cron (likely nightly) so the dashboard stays current
  without human intervention.
- [ ] **`.docx` parsing path** — formalize support so authors can drop
  Word files alongside PDFs.
- [ ] **Google Doc → Markdown direct export** — Google Docs can export
  natively to Markdown via `?format=md` (or `?format=txt` for plain
  text). For Google Doc agendas specifically this would skip the entire
  "PDF → Markdown" parser step, save tokens, and likely produce cleaner
  structured content. Currently the `google_download.py` module always
  fetches `format=pdf` for parity with the existing Parser pipeline.
  Worth exploring once the rest of the scraper is wired up end-to-end.
- [ ] **Item de-duplication across meetings** — the same `26-074` item
  may appear in both a Committee of the Whole agenda and a Council
  Regular agenda; dedupe with a stable per-item key.
- [ ] **Calendar (month grid) view toggle** — alternative to the card list
  for users who prefer a calendar-first scan.
- [ ] **Subscribe-by-keyword email digest** — opt-in alerts for items
  matching a watchlist (e.g., "zoning", "Tufts Park").

## License

Public information; site code MIT.
