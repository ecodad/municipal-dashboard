# Working Memory — Medford Municipal Agendas Dashboard

> Persistent session notes. Update at the end of every meaningful work session
> so the next Claude can pick up without re-deriving context. Source of truth
> for "where we are right now"; the README is the public-facing project
> overview.

**Last updated:** 2026-04-26 (after dashboard schema upgrade — meetings[] surfaced)

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
**API key management:** ✅ Resolved — Windows user env var route, smart-override `.env` loader landed in `bfcb6a2`. User runs the pipeline from PowerShell outside Claude Code.
**Persistent doc system:** ✅ Wired — `MEMORY.md`, `ARCHITECTURE.md`, `TARGET_SITES.md`, `AGENTS.md`, `TODO.md` shipped in `bfcb6a2`. SessionStart + PreCompact hooks in `.claude/settings.json` (project-shared) reinforce reading + updating these docs every session.
**Step 4 (scheduling):** ⏳ Not started.
**Dashboard refresh** (incorporate `agenda_url`/`agenda_type`/`location`/`zoom_url` from new schema): ⏳ Not started.

## Active workstream

Nothing in flight. Both scheduling and dashboard schema upgrade are
shipped.

Confirmed working: the cron ran successfully on 2026-04-26 morning at
10 UTC (user reported: "the hook ran this morning at the prescribed
time and executed to success").

Next on the TODO priority queue is the multi-municipality refactor —
making the scraper config-driven so other cities can fork. That's
deliberately not started yet; it's substantial design work and the
project should bake for a few more cron cycles before it gets pulled
apart.

## Resolved this session — first production run + doc system + README

- 7 meetings (4/27–4/30) scraped, parsed, synthesized, and archived.
  `agendas.json` grew 80 → 165 items.
- Synthesizer hit a `json.JSONDecodeError: Extra data` on the Zoning
  Board agenda (Sonnet emitted valid JSON then kept generating).
  Mitigated by switching from `json.loads()` to `raw_decode()`. Re-run
  succeeded; that meeting added 7 items.
- API key rotation: old key leaked into the transcript via Claude
  Code's file-watch system-reminder when `.env` was edited. Rotated
  in the Console; new key now lives only in a Windows user env var
  (no `.env` file on disk). Code now uses smart-override `load_dotenv`
  so an OS-provided non-empty key wins, and Claude Code's empty-string
  sandbox default is treated as "unset".
- Persistent doc system shipped (commit `05eb1c1`): hook script
  `.claude/hooks/doc-context-hook.sh` + `.claude/settings.json`
  (project-shared) inject doc-purpose definitions and continuous-update
  rules at SessionStart, plus a PreCompact reminder.
- README reconciliation (this commit): README trimmed from 168 → 101
  lines. Public-facing project overview only — what it is, the live
  site, the operational run commands, and a "Project documentation"
  index pointing at the five companion docs. Architectural detail,
  agent contracts, target-site quirks, full data schema, and the
  roadmap all delegated to their respective companion files.

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

- `905185a` — Add SCHEDULING.md runbook for the cron + workflow
- `1cdbb5f` — TODO: capture multi-municipality fork-friendly refactor
- `a2c5f28` — Add scheduled pipeline workflow (GitHub Actions, daily cron)
- `774eab0` — Reconcile README.md with the persistent doc system (-67 net lines; README now public-facing only, links to companion docs)
- `05eb1c1` — Make project doc system available to all contributors (promoted hooks + script from gitignored local config to committed project-shared config)
- `bfcb6a2` — First production pipeline run + persistent docs + bug fixes (78 new items, 5 doc files, smart-override .env, raw_decode JSON, .env.example)
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
