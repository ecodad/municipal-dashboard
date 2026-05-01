# Working Memory — Municipal Dashboards

> Persistent session notes. Update at the end of every meaningful work session
> so the next Claude can pick up without re-deriving context. Source of truth
> for "where we are right now"; the README is the public-facing project
> overview.

**Last updated:** 2026-05-01 (Somerville recon — S3 + Legistar split confirmed)

## What this project is

A renamed-and-refactored "Municipal Dashboards" project: a static dashboard
for municipal meeting agendas, hosted on GitHub Pages at
https://ecodad.github.io/municipal-dashboard/. Repo:
https://github.com/ecodad/municipal-dashboard. Originally Medford-only;
as of this session it's structured around a **CityAdapter** protocol so
other cities can be added with their own adapter module. The pipeline
fetches the active city's calendar, downloads agenda PDFs from whatever
hosting systems that city uses (Medford: CivicClerk + Google Docs/Drive;
future Somerville: Drupal + Legistar), parses them with Claude Haiku,
classifies items with Claude Sonnet, and writes a single `agendas.json`
consumed by `index.html`.

## Where we are right now

**Initial Medford pipeline (Steps 1–4):** ✅ All shipped (calendar scrape, CivicClerk + Google downloaders, orchestrator, Parser/Synthesizer LLM agents, scheduled GitHub Actions cron).

**Multi-municipality refactor — Phase 1:** ✅ Shipped this session.
- `scraper/adapters/` package with `CityAdapter` Protocol, `MeetingRecord` dataclass, `AgendaDownloadResult`, `AdapterDownloadError`, and a slug→class registry.
- `scraper/adapters/medford_ma.py` — `MedfordAdapter` wrapping the existing Finalsite calendar + CivicClerk/Google downloaders behind the protocol.
- `scraper/run_pipeline.py` — refactored to be city-agnostic: takes `--municipality SLUG` (or `MUNICIPALITY_SLUG` env var), defaults to `medford-ma`, loads the adapter via the registry, and never imports city-specific modules directly.
- Branding split into project chrome (Municipal Dashboards banner, fixed across cities) vs city section (logo, colors, eyebrow, tagline) loaded at runtime from `branding.json`. Per-city files live in `branding/{slug}.json`; the orchestrator copies the right one into the active `branding.json` on each run.
- `index.html` reorganized with two-tier header: project banner (charcoal + copper accent, system-sans, 2x2-square glyph) above a city-branded subject section. Footer now carries an explicit "independent project — for official documents follow the link to the Official City Calendar" disclaimer with a copper accent rule.
- HTML inline defaults match `branding/medford-ma.json` so the page renders correctly even over `file://` (i.e. when `fetch('branding.json')` can't run); JS still overrides at runtime.
- City title now reads "Medford Municipal Agendas" (city_name + tagline combined) rather than tagline alone.
- GitHub Actions workflow plumbs `MUNICIPALITY_SLUG` repo variable through to the pipeline; bot identity renamed `municipal-dashboard-bot`; `branding.json` added to the auto-commit set so a forker switching their slug variable sees the chrome update.

**Multi-municipality refactor — Phase 1.5 (multi-city deploy layout):** ✅ Shipped this session.
- Each city now lives under its own subdirectory: `/medford/index.html`, `/medford/agendas.json`, `/medford/branding.json`, `/medford/archived/`. Phase 2 will add `/somerville/`. Existing Medford archive (26 files) moved with `git mv` so history is preserved.
- Root `/index.html` is now a **landing page** that fetches `/cities.json` (pipeline-generated) and renders one card per registered city.
- Canonical city dashboard lives at `/template/dashboard.html`. The pipeline copies it to `{site_path}/index.html` on each run, so a forker who edits the template once gets it propagated to every city automatically.
- `CityAdapter` Protocol gained a `site_path` field (e.g. `medford`) separate from `slug` (e.g. `medford-ma`) so directory names stay short.
- `run_pipeline.py` reorganized: per-city working dir at `agendas/{slug}/` (gitignored), per-city published dir at `{site_path}/` (committed), per-city archive at `{site_path}/archived/`. Adds `--all` flag that loops over every registered slug; dropped the old `--municipality SLUG` requirement (still works) so cron just runs `--all`.
- New helper `_update_cities_registry()` rewrites `cities.json` from each `branding/{slug}.json` + `{site_path}/agendas.json` after every run. Idempotent.
- New helper `_refresh_site_chrome()` syncs `template/dashboard.html` → `{site_path}/index.html` and `branding/{slug}.json` → `{site_path}/branding.json` on every run.
- Dashboard JS archived-link path changed from `agendas/archived/{file}` to `archived/{file}` (relative to the city's own folder).
- Project banner "home" link now points at `..` (one level up to the landing page) instead of `.`.
- GH Actions workflow uses `--all`; auto-commit step iterates every top-level dir that has an `agendas.json` plus the root `cities.json`. Bot identity unchanged from Phase 1.
- `.gitignore` now ignores the entire `agendas/` working tree (was previously only `.last_scraper_run.json`); also adds `maai_raw.json` (local scratch).

**Multi-municipality refactor — Phase 2 (Somerville):** ✅ Recon complete. Calendar hosting confirmed as a deterministic mix: City Council standing committees use Legistar (`View.ashx?M=A`), all other bodies use public S3 (`somervillema-live` bucket). Both are public PDFs with no auth. Ready for adapter implementation.

## Calendar cache-buster fix (2026-04-30)

User reported the cron was silently missing meetings — Energy &
Environment Committee on 5/4, Oak Grove Cemetery Commission on 5/5,
and (it turned out) ~14 other May meetings entirely. Investigation
revealed:

- Medford's Finalsite calendar AJAX at `/fs/elements/6730?cal_date=...`
  is fronted by a CDN whose cache key strips the `cal_date` param.
  Every probe was returning whatever was last cached — usually a
  stale "April" view.
- The fix was hidden in plain sight in Finalsite's own JS bundle:
  `$.ajax({cache: false})` adds a unique `_=<ms-timestamp>` query
  parameter (which the cache key DOES include) plus an
  `X-Requested-With: XMLHttpRequest` header. Sending these makes
  `cal_date` honored end-to-end.
- Confirmed locally: `cal_date=2026-05-15` reliably returns the May
  grid; `cal_date=2026-06-15` returns the June grid.
- Patched `_fetch_calendar_page` accordingly. Added a third probe
  (`last_day` of the lookahead window) for resilience.
- Also added forensic capture (raw HTML responses written to
  `agendas/{slug}/.last_calendar_responses/probe_*.html` per run,
  uploaded as a workflow artifact) and verbose filter-stage logging
  so silent drops are visible in the run log next time something
  silently breaks.
- Workflow artifact upload was previously broken: `actions/upload-artifact@v4`
  defaults to `include-hidden-files: false`, so our dot-prefixed
  summary file was excluded. Fixed by setting `include-hidden-files: true`.

Local dry-run after the patch: 16 meetings in the 14-day window
(was 2 before the fix), including the user-reported missing ones.

## Somerville recon (2026-05-01)

User completed a full municipal calendar recon on Somerville, MA today, resolving the outstanding question about agenda hosting. The answer is **both S3 and Legistar, in a deterministic split**:

- City Council standing committees (Finance, Land Use, Legislative Matters, etc.) → **Legistar Gateway** (`somervillema.legistar.com/Gateway.aspx`, PDF at `/View.ashx?M=A&ID=...&GUID=...`). The PDF URL is derivable from the Drupal-page Gateway URL without a second fetch (just swap `M=MD` → `M=A`).
- Every other board/commission/authority (School Committee, Planning Board, HPC, etc.) → **S3** (`s3.amazonaws.com/somervillema-live/`, public, no auth). Filenames unpredictable; must be scraped from detail page and filtered by `"agenda"` substring to exclude meeting-notice PDFs.

Both hosts are fully public (no credentials needed), serve valid PDFs, and are documented in TARGET_SITES.md with concrete examples and the deterministic classification rule. Calendar recon also found: Drupal `/calendar` list view with zero-indexed pagination (`?page=0` is page 1), detail URLs at `/events/YYYY/MM/DD/{slug}`, 14-day lookahead cap justified by MA Open Meeting Law 48h posting requirement and empirical Somerville posting window of 1–2 weeks.


## Active workstream

Phase 1.5 was just pushed. Plan for the user's next interaction:

1. Verify https://ecodad.github.io/municipal-dashboard/ shows the new
   landing page with one Medford tile.
2. Verify https://ecodad.github.io/municipal-dashboard/medford/ shows
   the unchanged Medford dashboard.
3. Verify Pages serves `cities.json` and that the landing page's fetch
   succeeds.
4. Once green, begin Phase 2 — write `SomervilleAdapter`. Recon
   already in TARGET_SITES.md: Drupal calendar at
   `https://www.somervillema.gov/calendar`, detail pages at
   `/events/YYYY/MM/DD/{slug}`, agendas hosted in Legistar
   (`somervillema.legistar.com`) with PDFs at `View.ashx?M=A&ID=...&GUID=...`.

## Phase 1 design decisions (multi-municipality)

| Decision | Rationale |
|---|---|
| Adapters are Python modules, not YAML config | Somerville's stack proved cities differ enough that config-only can't capture the variation; any forker who can't write Python can't add a new city anyway. |
| One repo per city fork (one `MUNICIPALITY_SLUG` per repo) | Matches existing GH Pages + cron model; no multi-tenant URL routing. |
| Two-tier header (project banner above city section) | User wanted clear visual signal that this is *not* an official city site; project banner uses charcoal + copper + system-sans to be clearly distinct from any city's serif-and-color identity. |
| Copper `#b35a1f` as project accent | Chromatically far from both navy (Medford) and forest green (future Somerville); reads as civic/newspaper rather than tech-startup. |
| Branding loaded at runtime from `branding.json` (no build step) | Forking is "edit JSON → rerun pipeline → push." Static HTML stays static. |
| Inline HTML defaults match Medford branding | Page renders correctly over `file://` (where `fetch()` fails); a forker must edit four inline lines in `index.html` plus the JSON file. Documented inline. |
| Defer adapter abstraction for non-existent stacks (Granicus, BoardDocs, etc.) | YAGNI; the Somerville adapter will validate the protocol shape, and we'll only generalize once a third stack appears. |
| Title fronts city name ("Medford Municipal Agendas") | User feedback: makes the city the subject of the heading, not just an eyebrow above. |

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

- *(this commit)* — Document Somerville recon: deterministic S3 + Legistar split confirmed (both public, no auth)
- `0eee098` — Calendar cache-buster + forensic capture (fix for cron missing May meetings)
- `b6bb302` — Phase 1.5: per-city subdirectories + landing page + cities.json
- `3ee4461` — Multi-municipality refactor Phase 1: adapter layer, project banner, runtime branding
- `0ca4ad7` — Surface meeting-level attendance info in the dashboard
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
