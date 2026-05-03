# TODO

> The single backlog. Pending features, known bugs, technical debt, and
> in-flight items. The top section is **prioritized** — work down it in
> order. The lower sections are unordered piles tagged by category.

## 🔥 Priority queue (do these first, in order)

### 1. Get feedback and feature requests

Phase 2 is complete and both Medford and Somerville are live. Now is
the time to share the dashboard with users, gather feedback, and
identify the next highest-value features or cities to add.

Suggested channels: share the live URL with the people most likely
to use it (neighbors, local civic groups, city staff, local press),
and note any pain points or feature gaps that come up.

## 📋 Pending features

- **Google Doc → Markdown direct export.** Google Docs supports
  `?format=md` natively. For Google Doc agendas this would skip the
  PDF→Markdown Parser pass entirely — saving Haiku tokens and likely
  producing cleaner structured output (Google's own export preserves
  heading levels, bullet hierarchy, etc.).

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

- **Add a third city.** The adapter pattern is validated by Somerville.
  Strong candidates: Cambridge, MA (Granicus stack — would force a new
  calendar adapter) or another MA city on Finalsite (lowest lift).

## 🐛 Known bugs / edge cases

- **Image-only PDFs** (scanned, no OCR) trigger
  `ParserError("output is suspiciously short")`. We surface this clearly,
  but there's no automatic fallback to a different parser or OCR step.
  If the city ever switches to scanned agendas, we'd need to add OCR.

- **`agenda_type=OTHER`** is currently a dead-end — the orchestrator
  marks the meeting as `unsupported` and skips download. No alerting.
  Today this never triggers because all observed agenda hosts are
  CivicClerk / Google Doc / Google Drive / S3 / Legistar, but if a city
  posts to some new system we'd silently lose that meeting until someone
  reads the run summary.

- **Synthesizer "Extra data" JSON parse error (mitigated, not root-caused).**
  On the first end-to-end Synthesizer run (2026-04-25), Sonnet emitted a
  complete valid JSON object and then kept generating trailing tokens past
  the schema's natural stop point. **Mitigated** by switching from
  `json.loads()` to `json.JSONDecoder().raw_decode()`. Follow-ups:
    - Log when `raw_decode` silently drops trailing content.
    - Investigate whether this is a known issue with adaptive thinking
      + structured outputs on Sonnet 4.6, or specific to our prompt shape.

- **CivicClerk plain-text variant occasionally returns 0 bytes.** We
  catch this in the downloader and only raise on PDF format; text is
  treated as best-effort.

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

- **No structured logs.** Currently we `print(...)` to stderr. For
  scheduled runs, this would be more useful as JSON lines.

- **`requirements.txt` lists minimum versions but isn't pinned.** For
  reproducible builds (especially on a scheduler), generate a
  `requirements.lock` and pin exact versions.

---

## ✅ Completed

### Phase 2 — Somerville live (completed 2026-05-03)

Full multi-municipality Phase 2 shipped and live at
`https://ecodad.github.io/municipal-dashboard/somerville/`.

- **SomervilleAdapter + host downloaders** (commits `4ee703c`, `a50f763`,
  `855c714`, `9a380d4`). New modules: `scraper/s3_download.py` (generic
  public-S3 downloader), `scraper/legistar_download.py` (generic Legistar
  PDF downloader), `scraper/adapters/somerville_ma.py` (SomervilleAdapter).
  Walks Drupal `/calendar?page=N`, classifies agenda host (Legistar for
  City Council committees; S3 for all other bodies), handles DST-aware
  time-zone conversion. Live smoke test: 26 meetings, split 14 S3 /
  8 Legistar / 4 MISSING. Deterministic split rule held perfectly.
- **Synthesizer Medford-coupling bug fixed** (2026-05-01). Orchestrator
  now builds a `meetings_index` (source filename → MeetingRecord) and
  threads it into `synthesize_directory`; Somerville meetings get correct
  `agenda_type`, `detail_url`, `agenda_url`, and `has_zoom` flags.
- **Legistar 0-byte → MISSING** (commit `fdba7ce`, 2026-05-02). Legistar
  returns HTTP 200 + empty body for unposted agendas; now caught as
  `LegistarAgendaNotPosted` / `AdapterAgendaNotPosted` and mapped to
  `Status.MISSING` so the run exits 0 and the commit step proceeds.
- **Somerville logo self-hosted** (commit `9d1f7ae`, 2026-05-03). Official
  city seal SVG committed to `branding/assets/somerville-seal.svg`;
  replaces the Wikimedia Commons raster thumbnail.
- **Landing page logo path fix** (commit `6b006a4`, 2026-05-03).
  `_update_cities_registry` now resolves relative `logo_url` values
  against the city's `site_path` so `cities.json` always contains
  root-relative paths that work from the landing page.
- **Agenda items numeric sort** (commit `3589c18`, 2026-05-03).
  `_item_sort_key()` in `synthesizer.py` extracts digit runs as int
  tuples; "1, 2, 3, 10, 11" instead of "1, 10, 11, 2, 3".

### Phase 1.5 — per-city subdirectory layout (completed prior session)

Each city lives under `/{site_path}/`. Root `index.html` is a landing
page reading a pipeline-generated `cities.json`. Canonical city dashboard
at `template/dashboard.html`; pipeline copies it on every run. Medford
archive moved with `git mv` to preserve history. New `--all` flag; GH
Actions cron uses `--all`.

### Phase 1 — multi-municipality adapter layer (commit `3ee4461`)

`scraper/adapters/` package with `CityAdapter` Protocol, `MeetingRecord`
dataclass, registry, and `MedfordAdapter`. `run_pipeline.py` is now
city-agnostic. Branding extracted into `branding/{slug}.json`. Two-tier
header (project banner + city section) with "independent project"
disclaimer in the footer.

### Earlier milestones

- Calendar cache-buster fix — CDN strips `cal_date` param; fixed by
  sending `_=<timestamp>` + `X-Requested-With` header (commit `0eee098`).
- Dashboard schema upgrade — `meetings[]` array alongside `items[]`;
  dashboard renders Zoom indicator, livestream indicator, Agenda link,
  and "View on official site" link per meeting card.
- GitHub Actions cron — daily at 10 UTC; auto-commits changed files;
  uploads run summary as artifact (commit `a2c5f28`).
- SCHEDULING.md runbook for forkers (commit `905185a`).
- Persistent doc system (SessionStart + PreCompact hooks, commit `05eb1c1`).
- First production run — 78 new items, `agendas.json` grew 80 → 165
  (commit `bfcb6a2`).
