# TODO

> The single backlog. Pending features, known bugs, technical debt, and
> in-flight items. The top section is **prioritized** — work down it in
> order. The lower sections are unordered piles tagged by category.

## 🔥 Priority queue (do these first, in order)

### 1. Legistar `View.ashx` returns 200/0-bytes for unposted Council agendas

Surfaced by the Somerville smoke test (2026-05-01). 4 of 8
LEGISTAR-classified meetings failed with `Legistar download for
ID=... returned 0 bytes` — all of them future Council/Committee
meetings (5/11–5/14) where the calendar listing has a Gateway URL
but no agenda PDF has been uploaded yet. Legistar returns HTTP 200
with an empty body in this case, not a 404, so our downloader's
"valid PDF or raise" check trips.

Suggested fix: in [scraper/legistar_download.py](scraper/legistar_download.py),
treat 200/0-bytes as a soft-miss equivalent to MISSING (raise a
specific `LegistarNotYetPostedError`); the orchestrator then
downgrades the meeting from LEGISTAR to MISSING in the run summary
and the dashboard shows "Agenda not yet posted" instead of "failed."

**Sub-bug:** the failed Legistar download still creates a 0-byte PDF
on disk (`agendas/{slug}/{stem}.pdf`) before the magic-byte check
raises, so subsequent runs see those stubs via `_already_have` and
mark the meeting `SKIPPED_EXISTING`. Fix: write to a tempfile and
atomically rename only after the magic-byte check passes; clean up
the tempfile on any failure path. Same pattern is worth applying to
`s3_download.py` and `civicclerk_download.py` for consistency.

### 2. Multi-municipality Phase 2 — final wrap-up

Phase 2 adapter, downloaders, branding JSON, and end-to-end smoke
test all shipped 2026-05-01. Bug 1 (Synthesizer Medford-coupling)
fixed 2026-05-01 — the orchestrator now builds a `meetings_index`
(source filename → MeetingRecord) and threads it into
`synthesize_directory`, which uses the adapter-resolved fields
verbatim. Existing `somerville/agendas.json` rebuilt via
`scraper.synthesizer --rebuild-meetings-from-summary` (no LLM cost);
17/17 meetings now have correct `agenda_type=S3|LEGISTAR`,
`detail_url`, `agenda_url`, and `has_zoom` flags.

Remaining bookkeeping:

1. Commit + push the smoke-test artifacts: `branding/somerville-ma.json`,
   `somerville/{index.html,branding.json,agendas.json,archived/*}`,
   `cities.json`, plus the Bug 1 source-code changes
   (`scraper/synthesizer.py`, `scraper/run_pipeline.py`) so GitHub
   Pages serves `/somerville/`.
2. Re-run the full pipeline (or just the rebuild flag) once Bug 1
   above is also fixed, to clean up the four "failed" Council
   meetings.

## ✅ Recently done (kept here briefly so future sessions can see what shipped)

- **Multi-municipality Phase 2 — SomervilleAdapter + host downloaders**
  (commits `4ee703c`, `a50f763`, `855c714`). Implementation shipped
  end-to-end: new modules `scraper/s3_download.py` (generic public-S3
  downloader), `scraper/legistar_download.py` (generic Legistar PDF
  downloader), and `scraper/adapters/somerville_ma.py` (SomervilleAdapter
  implementing the CityAdapter Protocol). Adapter walks Drupal
  `/calendar?page=N`, filters by "meeting" substring, fetches detail
  pages, classifies agenda host (Legistar first, else S3, else MISSING),
  handles time-zone conversion to America/New_York (DST-aware),
  dispatches download based on `agenda_type`. Live smoke test (May 1–15):
  26 meetings, split 14 S3 / 8 Legistar / 4 MISSING. Both download
  paths exercised; both pass `%PDF` magic validation. Deterministic
  split rule (City Council committees → Legistar; all others → S3) held
  perfectly.

- **Phase 1.5 — multi-city deploy layout** (prior session). Each city
  now lives under `/{site_path}/` (e.g. `/medford/`) instead of
  sharing the repo root. Root `index.html` is now a landing page
  that reads a pipeline-generated `cities.json`. Canonical city
  dashboard at `template/dashboard.html`; pipeline copies it to
  `{site_path}/index.html` on every run. Adapter Protocol gained a
  `site_path` attribute. `run_pipeline.py` reorganized: per-city
  working dir at `agendas/{slug}/` (gitignored), per-city published
  dir at `{site_path}/` (committed). New `--all` flag loops over
  every registered slug; the GH Actions cron now uses `--all`.
  Added helpers `_refresh_site_chrome()` and
  `_update_cities_registry()`. Existing Medford archive moved with
  `git mv` so history is preserved. Dashboard JS archived-link path
  is now relative to the city folder.

- **Multi-municipality refactor — Phase 1** (commit `3ee4461`). Introduced
  `scraper/adapters/` package with the `CityAdapter` Protocol,
  `MeetingRecord` dataclass, registry, and `MedfordAdapter` wrapping
  the existing Finalsite + CivicClerk + Google modules.
  `run_pipeline.py` is now city-agnostic with a `--municipality SLUG`
  flag (defaulting to `medford-ma`, also reads `MUNICIPALITY_SLUG`
  env var). Branding extracted into `branding/{slug}.json` files,
  loaded at runtime by the dashboard. `index.html` got a two-tier
  header — project banner ("Municipal Dashboards", charcoal + copper
  accent, system-sans) above a city-branded subject section, with
  an explicit "independent project" disclaimer in the footer. GH
  Actions workflow plumbs `MUNICIPALITY_SLUG` through to the
  pipeline. No behavior change for Medford; new schema enables
  Phase 2.

- **Wire the dashboard to render the new schema fields** (prior session).
  `agendas.json` now has a top-level `meetings[]` array
  alongside `items[]`, where each meeting record carries
  `committee_name`, `meeting_date`, `meeting_time`, `location`,
  `has_zoom`, `has_livestream`, `agenda_url`, `agenda_type`, and
  `detail_url`. Per the user's UX call: surface presence-of-Zoom and
  presence-of-livestream as boolean indicators only — never expose the
  Zoom URL itself (avoids sending people to potentially-stale links).
  The agenda link points to the live external URL when available
  (CivicClerk / Google Doc / Drive) or to our archived copy at
  `agendas/archived/{filename}` for legacy meetings; "Agenda not
  posted" badge shown when truly missing. Each card also gets a
  prominent "View on medfordma.org" link as the canonical fallback for
  attendance details. Synthesizer now builds these records inline; a
  one-time `--backfill-meetings` CLI flag was used to populate
  meetings[] for the existing 13 source files (7 enriched via detail-
  page re-fetch; 6 legacy got minimal records, marked
  agenda_type=ARCHIVED).
- **Document scheduling for forkers** (commit `905185a`). New
  [SCHEDULING.md](SCHEDULING.md) runbook covers where the code runs
  (ephemeral GitHub Actions Ubuntu runner), where files go (committed
  back to main → Pages rebuild), where to put the API key (repo
  secrets — never in the repo), the 5-step setup walkthrough for a
  new fork, how to adjust cadence, and a troubleshooting section.
  Wired into the SessionStart hook context, the README index, and
  MEMORY.md.
- **Schedule the pipeline** (commit `a2c5f28`). GitHub Actions workflow
  at `.github/workflows/refresh-agendas.yml` runs the full pipeline
  daily at 10 UTC (≈6 AM ET) plus on-demand via the Actions tab. Reads
  `ANTHROPIC_API_KEY` from repo secrets. Auto-commits regenerated
  `agendas.json` and newly-archived sources with a bot identity, only
  if anything changed. Uploads `agendas/.last_scraper_run.json` as an
  artifact every run for debugging. **One manual setup step is
  required**: add `ANTHROPIC_API_KEY` to the repo's Actions secrets
  (Settings → Secrets and variables → Actions → New repository
  secret).
- **Reconcile README.md with the new docs** (commit `774eab0`). README
  trimmed
  from 168 → 101 lines. Now public-facing only: project pitch, live
  site, operational run commands, dashboard description, "Project
  documentation" index linking to MEMORY/TODO/ARCHITECTURE/
  TARGET_SITES/AGENTS, known limitations. Architectural diagram, full
  data schema, agent team contract, and roadmap moved to their
  respective companion files.
- **Document the documentation system** (commit `05eb1c1`). The
  persistent Markdown docs are now reinforced by a SessionStart hook
  (`.claude/hooks/doc-context-hook.sh`) wired in
  `.claude/settings.json` (project-shared, committed). Hook injects
  file-purpose definitions and continuous-update rules at every session
  start. PreCompact hook reminds Claude to refresh `MEMORY.md` /
  `TODO.md` before compaction.
- **Finish the API-key handoff** (commit `bfcb6a2`). New key set as
  Windows user env var; smart-override `.env` loader now respects
  existing OS env vars and only fills from `.env` when the OS value is
  empty/whitespace. Synthesizer re-ran successfully producing 78 new
  items; `agendas.json` grew 80 → 165.

## 📋 Pending features

- **Multi-municipality refactor — original framing (now mostly
  superseded by the Phase 1/2 split above).** The user wants any
  contributor in a different municipality to fork the repo, swap
  in a city adapter, add their API key, and get their own running
  dashboard.

  **What's currently Medford-specific** (would need to become
  config-driven):
  - `scraper/calendar_scrape.py` — `MEDFORD_CALENDAR_ELEMENT = 6730`,
    `medfordma.org` host, AJAX URL template
  - `scraper/event_detail_scrape.py` — `medfordma.org` detail URL
    pattern (the `~occur-id/{N}` shape itself is Finalsite-generic)
  - `scraper/civicclerk_download.py` — `medfordma.api.civicclerk.com`
    and `medfordma.portal.civicclerk.com` subdomains
  - `index.html` — City of Medford branding (navy `#25347a`, official
    seal image URL, Merriweather + Lato, calendar-CTA link, page title)

  **Suggested approach:**
  1. Introduce a `MunicipalityConfig` dataclass (or `municipalities/{slug}.yaml`)
     with fields like `name`, `slug`, `timezone`, `events_calendar_url`,
     `finalsite_element_id` (nullable — not all cities use Finalsite),
     `civicclerk_subdomain` (nullable), `title_filter`, and a
     `branding` block (primary color, accent color, logo URL, font
     stack, page title, calendar-link URL).
  2. Refactor each scraper module to accept a config object instead of
     reading module-level constants. Same for the dashboard's branding
     variables.
  3. Add a `municipalities/` directory with one config per city. Start
     with `municipalities/medford-ma.yaml` extracted from the current
     constants so the existing pipeline keeps producing identical
     output.
  4. Introduce a thin platform-adapter interface for the calendar
     source. Today everything assumes Finalsite. Other cities might use
     **Granicus**, **Legistar**, or **BoardDocs** — design the calendar
     fetcher as an interface so a `GranicusAdapter` could slot in
     without touching the rest of the pipeline.
  5. Same adapter pattern for the agenda-host downloaders. CivicClerk
     and Google Doc/Drive cover Medford; other tenants might use
     Granicus PDFs, Legistar, etc. Keep dispatch keyed on the
     `agenda_type` enum and add new enum values as needed.
  6. Make the GitHub Actions workflow either auto-pick the config from
     a single committed `MUNICIPALITY_SLUG` env var, or allow the
     workflow to receive the slug as a `workflow_dispatch` input so a
     single repo could in principle handle multiple cities (though the
     simpler model is one fork per city).
  7. Write a new `MUNICIPALITY_SETUP.md` walking a new contributor
     through: identify which platforms the target city uses, derive
     element IDs / URL patterns (recipes for the recon process —
     similar shape to what's already in `TARGET_SITES.md` but
     instructional), swap dashboard branding, set up GitHub Pages,
     add the API key secret, run the first sync.

  **Candidate target cities to consider while designing** (so the
  abstraction is sized for real variation, not just a one-off):
  - Somerville, MA — explicitly mentioned by user; likely Finalsite +
    CivicClerk, similar to Medford
  - Cambridge, MA — Granicus stack (would force the adapter pattern)
  - Boston, MA — Granicus, much higher meeting volume
  - Other Massachusetts municipalities running Finalsite (a large set)

  **Out of scope for this item:** packaging as a `pip` CLI, hosting a
  multi-tenant deployment ourselves, or any kind of billing. The
  deliverable is *"a fork-friendly template that works for the next
  contributor's city without forking the actual scraping logic."*



- **Step 4 — Schedule the pipeline.** Wire the full
  `scrape → parse → synthesize → archive → commit → push` chain into
  a recurring job. Two viable hosts: GitHub Actions cron (free, simple,
  needs `ANTHROPIC_API_KEY` as a repo secret) or local Windows Task
  Scheduler (no cloud secret, but only runs when machine is on).
  Recommend GitHub Actions for reliability.

- **Dashboard schema upgrade.** `index.html` currently renders only
  `Committee_Name`, `Meeting_Date`, `Meeting_Time`, `Location`,
  `Item_Number`, `Item_Type`, `Agenda_Topic`, `Source_File`. The
  scraper now also collects `agenda_url`, `agenda_type`, `zoom_url`,
  `livestream_url` per meeting — but they aren't yet flowing into
  `agendas.json` (the Synthesizer doesn't see them) or shown in the
  dashboard. Decide on schema:
  - Option A: Add a `meetings` top-level array (one entry per meeting
    with these fields), and let `items` reference meetings by key.
  - Option B: Repeat the meeting-level fields on every item.

  Then update `index.html` to render an "Agenda" link per meeting card,
  show "Agenda not posted" when `agenda_type === "MISSING"`, and link
  the Zoom URL.

- **Google Doc → Markdown direct export.** Google Docs supports
  `?format=md` natively. For Google Doc agendas this would skip the
  PDF→Markdown Parser pass entirely — saving Haiku tokens and likely
  producing cleaner structured output (Google's own export preserves
  heading levels, bullet hierarchy, etc.). Worth exploring once
  scheduling is wired up.

- **Item de-duplication across meetings.** The same agenda item ID
  (e.g. `26-074`) can appear in both a Committee of the Whole agenda
  and the City Council Regular agenda. Today both rows live in
  `agendas.json` independently; users probably want to see them
  collapsed with both meeting references.

- **Calendar (month-grid) view toggle for the dashboard.** Some users
  prefer scanning by date rather than by committee.

- **Subscribe-by-keyword email digest.** Opt-in alerts for items
  matching a watchlist (e.g., "zoning", "Tufts Park"). Out of scope
  until we have reliable scheduled updates.

- **`.docx` parsing path.** Currently any `.docx` placed in `agendas/`
  fails the Parser (which only handles PDFs). Add either: (a) a
  `.docx` → PDF conversion step, or (b) direct `.docx` reading via the
  Anthropic SDK's `document` content block, which supports it.

## 🐛 Known bugs / edge cases

- **Image-only PDFs** (scanned, no OCR) trigger
  `ParserError("output is suspiciously short")`. We surface this clearly,
  but there's no automatic fallback to a different parser or OCR step.
  If the city ever switches to scanned agendas, we'd need to add OCR.

- **`agenda_type=OTHER`** is currently a dead-end — the orchestrator
  marks the meeting as `unsupported` and skips download. No alerting.
  Today this never triggers because all observed agenda hosts are
  CivicClerk / Google Doc / Google Drive, but if the city posts to
  some new system we'd silently lose that meeting until someone reads
  the run summary.

- **Synthesizer "Extra data" JSON parse error (mitigated, not root-caused).**
  On the first end-to-end Synthesizer run (2026-04-25), 6 of 7 meetings
  succeeded but the Zoning Board agenda (`23793`) failed with
  `json.JSONDecodeError: Extra data: line 1 column 2692 (char 2691)`.
  Sonnet emitted a complete valid JSON object and then kept generating
  trailing tokens past the schema's natural stop point — even with
  `output_config.format` enforcing the JSON Schema. **Mitigated** by
  switching from `json.loads()` to `json.JSONDecoder().raw_decode()`
  in `synthesizer.py`, which parses the first valid JSON value and
  ignores any trailing garbage. Follow-ups worth doing:
    - Log when `raw_decode` silently drops trailing content so we can
      tell if this is happening more than expected (might mask a real
      schema-violation regression).
    - Investigate whether this is a known issue with adaptive thinking
      + structured outputs on Sonnet 4.6, or specific to our prompt
      shape.
    - Consider migrating to the SDK's `client.messages.parse()` with a
      Pydantic model, which the `claude-api` skill flags as the
      recommended structured-output API.

- **CivicClerk plain-text variant occasionally returns 0 bytes.** We
  catch this in the downloader and only raise on PDF format; text is
  treated as best-effort.

- **System-reminder leak of `.env` contents.** Documented in
  `MEMORY.md`. Workaround: use OS env vars instead of `.env` while a
  Claude Code session is active in this directory.

- **Small system prompts don't actually cache.** Both agents pass
  `cache_control: {"type": "ephemeral"}` on the system block, but the
  system prompts are well under Haiku's 4096-token / Sonnet's 2048-token
  cache minimum. The marker is a no-op today; harmless but worth knowing
  so we don't over-rely on it for cost projections.

## 🧹 Technical debt / cleanups

- **Item-Type classification is heuristic.** The Synthesizer maps
  agenda items to a 10-value enum based on text patterns. There's no
  evaluation set; quality is "looks right on the half-dozen meetings
  we've inspected." If the dashboard becomes more important, build a
  small labeled eval set and measure.

- **Pipeline test coverage = 0.** All verification has been ad-hoc CLI
  runs. At minimum, add a smoke test that runs `calendar_scrape` against
  a saved HTML fixture and asserts a known meeting is found. Bigger
  picture: a fixtures-based test for each downloader.

- **No retry/backoff on the LLM calls.** The Anthropic SDK retries
  transient errors with exponential backoff by default — we rely on
  this. If we ever pin retry counts, document why.

- **`run_pipeline.py` is getting large.** 400+ lines and growing. Split
  out the per-meeting dispatch into a `process_meeting.py` if it grows
  another 50%.

- **No structured logs.** Currently we `print(...)` to stderr. For
  scheduled runs, this would be more useful as JSON lines.

- **`requirements.txt` lists minimum versions but isn't pinned.** For
  reproducible builds (especially on a scheduler), generate a
  `requirements.lock` and pin exact versions.

- **The `__pycache__` was once accidentally committed and reverted.**
  `.gitignore` now covers it; just a note in case it sneaks back.
