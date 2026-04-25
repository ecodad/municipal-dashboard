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
and quality. Both are implemented as SDK-based Python modules so the pipeline
can run unattended (cron, GitHub Actions, etc.) — they don't depend on a
Claude Code session.

- **Parser Agent** ([scraper/parser.py](scraper/parser.py)) — Claude Haiku 4.5.
  Fast, faithful PDF→Markdown. Reads PDFs directly via the Anthropic SDK's
  `document` content block (no third-party PDF library needed). Validates that
  output is non-trivial; surfaces a clear error on extraction failure.
- **Synthesizer Agent** ([scraper/synthesizer.py](scraper/synthesizer.py)) —
  Claude Sonnet 4.6 with adaptive thinking. Reads the Parser's Markdown,
  classifies each item, and emits the canonical JSON. Uses
  `output_config.format` for guaranteed-valid JSON Schema output. Then
  archives sources to `agendas/archived/`. Idempotent — meetings already
  in `agendas.json` are skipped.

**Prerequisite:** set `ANTHROPIC_API_KEY` in the environment before running
the Parser or Synthesizer.

### Front-end

`index.html` is intentionally framework-free — one HTML file with embedded
CSS and a single vanilla-JS module. It uses City of Medford branding
(navy `#25347a`, Merriweather + Lato) and renders meeting cards grouped
by `(Committee, Date)`, sortable by a "Next 7 days / 30 days / All / Past"
time tab plus committee, type, and full-text filters. The default view
answers the dashboard's primary question: **"What's on the agenda this week?"**

## Running the pipeline

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# End-to-end: scrape → parse → synthesize → archive
python -m scraper.run_pipeline --process

# Or each stage individually:
python -m scraper.run_pipeline                  # Step 1+2: download new agenda PDFs
python -m scraper.parser                        # Step 3a: PDFs → Markdown
python -m scraper.synthesizer                   # Step 3b: Markdown → JSON, archive

# Then commit and push:
git add agendas.json agendas/archived/
git commit -m "Update agendas $(date +%F)"
git push origin main
```

The full pipeline produces a fresh `agendas.json` and moves the source PDFs
plus their Markdown intermediates into `agendas/archived/`. Re-runs are
idempotent: meetings already represented in `agendas.json` are skipped at
the Synthesizer stage, and PDFs already in `archived/` are skipped at the
Scraper stage.

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
