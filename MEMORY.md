# Working Memory — Medford Municipal Agendas Dashboard

> Persistent session notes. Update at the end of every meaningful work session
> so the next Claude can pick up without re-deriving context. Source of truth
> for "where we are right now"; the README is the public-facing project
> overview.

**Last updated:** 2026-04-25 (mid-session; pipeline mid-deployment)

## What this project is

A static dashboard for Medford, MA's municipal meeting agendas, hosted on
GitHub Pages at https://ecodad.github.io/municipal-dashboard/. Repo:
https://github.com/ecodad/municipal-dashboard. The data pipeline scrapes
the city's events calendar, downloads agenda PDFs from three different
hosting systems (CivicClerk, Google Docs, Google Drive), parses them with
Claude Haiku, classifies items with Claude Sonnet, and writes a single
`agendas.json` consumed by `index.html`.

## Where we are right now

**Phase 1–4 (initial setup):** ✅ Complete and shipped.
**Step 1 (calendar scrape):** ✅ Shipped — `scraper/calendar_scrape.py`.
**Step 2a (CivicClerk download):** ✅ Shipped — `scraper/civicclerk_download.py`. Discovered the `medfordma.api.civicclerk.com` API is fully public (no auth needed) — Playwright was unnecessary.
**Step 2b (event detail extractor):** ✅ Shipped — `scraper/event_detail_scrape.py`. Surfaces `agenda_type` enum: `CIVICCLERK | GOOGLE_DOC | GOOGLE_DRIVE_FILE | OTHER | MISSING`.
**Steps 2c + 2d (Google downloaders):** ✅ Shipped — `scraper/google_download.py`. Uses `docs.google.com/document/d/{id}/export?format=pdf` and `drive.google.com/uc?export=download&id={id}`.
**Step 2e (orchestrator):** ✅ Shipped — `scraper/run_pipeline.py`. Idempotent via `__{occur_id}__` filename pattern.
**Step 3a (Parser agent, SDK-based):** ✅ Shipped — `scraper/parser.py`. Claude Haiku 4.5 via Anthropic SDK; reads PDFs via `document` content block.
**Step 3b (Synthesizer agent, SDK-based):** ✅ Shipped — `scraper/synthesizer.py`. Claude Sonnet 4.6, adaptive thinking, `output_config.format` JSON Schema.
**API key management:** ⏳ In progress. See "Active workstream" below.
**Step 4 (scheduling):** ⏳ Not started.
**Dashboard refresh** (incorporate `agenda_url`/`agenda_type`/`location`/`zoom_url` from new schema): ⏳ Not started.

## Active workstream — `.env` / API key handling

User rotated their Anthropic API key after a key value leaked into the
session transcript via Claude Code's file-watch system-reminder when
`.env` was edited. We agreed to:

1. ✅ Move the real key to a **Windows user environment variable**
   (Settings → Environment Variables) instead of `.env`. The user has
   set the new key.
2. ⏳ User to fully quit + relaunch Claude Desktop (from the system tray,
   not just close the window) so the new env var is inherited by any
   shells spawned from this session.
3. ⏳ I need to make a small code change so OS env vars take precedence
   over `.env`: switch `load_dotenv(override=True)` to a "smart override"
   that only overrides when the existing value is empty/whitespace. This
   is needed in **both** `scraper/parser.py` and `scraper/synthesizer.py`.
4. ⏳ Verify the new key is visible (without printing it) via something
   like `python -c "import os; print(bool(os.environ.get('ANTHROPIC_API_KEY')))"`.
5. ⏳ Run `python -m scraper.synthesizer` to consume the 7 already-parsed
   markdown files and update `agendas.json`.
6. ⏳ Commit the updated `agendas.json` plus the archived PDFs/markdown,
   push to main.

## Outstanding artifacts on disk (not yet committed)

- **7 freshly-downloaded PDFs** in `agendas/` (root, not archived). These
  were dropped there by `run_pipeline` step 2e during a verification run.
  Filenames follow `{YYYY-MM-DD}__{occur_id}__{slug}.pdf`.
- **7 markdown files** in `agendas/markdown/` produced by the Parser when
  I accidentally triggered a real API call during `.env` setup. They
  match the 7 PDFs above and are ready for the Synthesizer.
- These artifacts represent meetings 2026-04-27 through 2026-04-30: MCHSBC,
  Committee of the Whole (×2), Conservation Commission, City Council,
  Climate Equity Council (MISSING agenda — surfaced as such), Retirement
  Board, Zoning Board.

## Recent key decisions

| Decision | Rationale |
|---|---|
| Use deterministic Python (BeautifulSoup) for the calendar scrape, not an LLM | Markup is stable; LLM is wasteful for ~25 KB of structured HTML |
| Deferred Playwright; CivicClerk API is public after all | Saved a 200 MB Chromium dependency; pure `requests` works |
| Three-source agenda detection (CivicClerk / Google Doc / Google Drive) + a `MISSING` enum value | Matches user's empirical observation that ~95% of meetings post agendas in one of three places, ~5% miss |
| Filename pattern `{YYYY-MM-DD}__{occur_id}__{slug}.pdf` | Sortable, dedup-friendly via stable `occur_id`, human-readable |
| Haiku 4.5 for Parser, Sonnet 4.6 for Synthesizer | Cost split: Haiku for verbatim transcription, Sonnet for classification reasoning |
| `output_config.format` with strict JSON Schema for Synthesizer | Guaranteed-valid output; 10-value `Item_Type` enum |
| `python-dotenv` with `load_dotenv(override=True)` initially, switching to smart-override next | Started with override=True to mask Claude Code's empty `ANTHROPIC_API_KEY=""`; user wants OS env var to win, so we need the smarter loader |
| `.env` gitignored; `.env.example` committed | Standard secrets-handling pattern |

## Recent commits (most recent first)

- `a1e8e58` — Add Step 3: SDK-based Parser and Synthesizer modules
- `a5f2c18` — Add scraper step 2e: end-to-end pipeline orchestrator
- `e4b5003` — Add scraper steps 2c + 2d: Google Doc and Drive downloaders
- `ded428c` — Add scraper step 2b: event detail page extraction
- `82c59b5` — Add scraper: CivicClerk agenda downloader
- `3b5b73d` — Add scraper step 1: calendar event extraction
- `059c456` — Link to City of Medford Events Calendar
- `8c9dd7f` — Initial commit: Medford municipal agendas dashboard

## Open items deferred mid-flight (not bugs, just things we said we'd do)

- Smart-override `.env` loader (described under Active workstream)
- Extend dashboard `index.html` to render `agenda_url`, `agenda_type` badge, `location`, `zoom_url`, and "Agenda not posted" callout when type is `MISSING`
- Schedule the full pipeline (Step 4)
- Revisit Google Doc native `?format=md` export to skip the PDF-to-Markdown step for Google Doc agendas (in roadmap)

## Things that bit us (so future-Claude doesn't repeat)

- `/tmp` on this Windows + bash setup maps to `C:\Users\jmhunt\AppData\Local\Temp` for bash but Python sees `/tmp` as literal. Use `cygpath -w` or write to project-relative `.recon/` instead.
- Claude Code's file-watch hook re-injects `.env` content into the session transcript whenever the file changes. **Don't put real keys in `.env` while a Claude Code session is live in the project.**
- Haiku occasionally returns "This PDF appears to contain no text" placeholder for valid PDFs on a first try. The Parser now validates a minimum 200-byte output and raises a clear `ParserError` on suspiciously short results — caller can retry.
