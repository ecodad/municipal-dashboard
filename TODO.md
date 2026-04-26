# TODO

> The single backlog. Pending features, known bugs, technical debt, and
> in-flight items. The top section is **prioritized** — work down it in
> order. The lower sections are unordered piles tagged by category.

## 🔥 Priority queue (do these first, in order)

### 1. Wire the dashboard to render the new schema fields

The scraper now collects per-meeting `agenda_url`, `agenda_type`,
`location`, `zoom_url`, and `livestream_url` — but they don't yet flow
into `agendas.json` (the Synthesizer doesn't see them) and aren't shown
in `index.html`. See "Pending features → Dashboard schema upgrade"
below for the design choice (Option A: meetings array; Option B: repeat
fields per item).

### 2. Schedule the pipeline

Step 4 of the original plan. Wire the
`scrape → parse → synthesize → archive → commit → push` chain into a
recurring job. Two viable hosts:
  - **GitHub Actions cron** — free, reliable, needs `ANTHROPIC_API_KEY`
    as a repo secret.
  - **Local Windows Task Scheduler** — no cloud secret, but only runs
    when the machine is on.

Recommend GitHub Actions for reliability.

## ✅ Recently done (kept here briefly so future sessions can see what shipped)

- **Reconcile README.md with the new docs** (this commit). README trimmed
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
