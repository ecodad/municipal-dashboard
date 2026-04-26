# Medford Municipal Agendas Dashboard

A static dashboard that surfaces what's on the agenda across Medford, MA's
boards, commissions, and council meetings. Source agendas (PDFs) are
processed by a two-stage Claude agent pipeline into a single
`agendas.json` file, which the dashboard fetches and renders in the
browser.

**Live site:** https://ecodad.github.io/municipal-dashboard/

## How it works (in a paragraph)

A scraper pulls Medford's events calendar weekly, follows each meeting
to its detail page, extracts the agenda link (CivicClerk, Google Doc, or
Google Drive), and downloads the PDF. **Claude Haiku 4.5** transcribes
each PDF to clean, structured Markdown. **Claude Sonnet 4.6** then
classifies and extracts each agenda item — committee, date, item
number, type, topic — into the canonical `agendas.json` schema. A
single-file `index.html` dashboard fetches and renders that JSON, with
filters by committee, item type, time window, and free-text search. Full
component-level design lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## The dashboard

Vanilla HTML/CSS/JS with no build step. Uses City of Medford branding
(navy `#25347a`, Merriweather + Lato) and renders meeting cards grouped
by `(Committee, Date)`, sortable by a "Next 7 days / 30 days / All /
Past" time tab plus committee, type, and full-text filters. The default
view answers the dashboard's primary question: **"What's on the agenda
this week?"**

## Running the pipeline

```bash
pip install -r requirements.txt
```

**Set `ANTHROPIC_API_KEY` as an OS environment variable, not in a file
inside the repo.** On Windows: *Settings → Environment Variables → New*
under "User variables". On macOS/Linux: add to your shell rc. The Parser
and Synthesizer prefer the OS env var; a project-local `.env` is only
read as a fallback when the OS var is empty.

Then:

```bash
# End-to-end: scrape → parse → synthesize → archive
python -m scraper.run_pipeline --process

# Or each stage individually
python -m scraper.run_pipeline       # download new agenda PDFs
python -m scraper.parser             # PDFs → Markdown
python -m scraper.synthesizer        # Markdown → JSON, archive
```

Then commit and push to deploy:

```bash
git add agendas.json agendas/archived/
git commit -m "Update agendas $(date +%F)"
git push origin main
```

GitHub Pages serves the update within ~60 seconds.

The full pipeline is idempotent: meetings already in `agendas.json` are
skipped at the Synthesizer stage; PDFs already in `agendas/archived/`
are skipped at the Scraper stage. Re-runs are safe.

## Project documentation

This repository uses a set of persistent Markdown documents as long-term
project memory. They live at the project root and are kept current by a
Claude Code SessionStart hook (see `.claude/settings.json` and
`.claude/hooks/doc-context-hook.sh`).

| File | Purpose |
| --- | --- |
| [MEMORY.md](MEMORY.md) | Current project state, recent decisions, in-flight workstreams. **Read first** when picking up the project. |
| [TODO.md](TODO.md) | Prioritized backlog. Pending features, known bugs, technical debt. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component-by-component design: module responsibilities, data flow, idempotency keys, data shapes, cost profile. |
| [TARGET_SITES.md](TARGET_SITES.md) | Every external data source (events calendar, CivicClerk, Google Doc/Drive, Anthropic API, GitHub) with URL patterns, auth model, and known constraints. |
| [AGENTS.md](AGENTS.md) | Roles, tool access, and permission matrix for every module (LLM agents and deterministic scrapers). |
| [SCHEDULING.md](SCHEDULING.md) | Runbook for the GitHub Actions cron that auto-refreshes the dashboard. Where the code runs, where files go, how to set it up on your own fork (including the API key secret). |

## Known limitations

- **Legacy `.doc` (Word 97–2003) files are not supported.** Convert to
  `.pdf` or `.docx` before placing them in `agendas/`. (`.docx` works.)
- **Image-only PDFs** (scanned without OCR) cause the Parser to surface
  a clear error rather than emit garbage. The pipeline doesn't currently
  fall back to OCR.
- **Item-type classification is heuristic.** The Synthesizer maps each
  item to one of 10 types based on text patterns; review high-stakes
  items in context. Full enum and decision rules in
  [AGENTS.md](AGENTS.md).
- The dashboard fetches `agendas.json` from the same origin it's served
  from, so it always reflects whatever's been pushed to `main`.

## License

Public information; site code MIT.
