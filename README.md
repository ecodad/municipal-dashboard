# Municipal Dashboards

Static dashboards that surface what's on the agenda across municipal
boards, commissions, and councils. Source agendas (PDFs) are processed
by a two-stage Claude agent pipeline into per-city `agendas.json` files
that each city's dashboard fetches and renders in the browser.

**Live site:** https://ecodad.github.io/municipal-dashboard/

The root URL is a landing page listing every registered city. Each
city has its own dashboard at `/{site_path}/`, e.g.
`https://ecodad.github.io/municipal-dashboard/medford/` for Medford, MA.

> **Independent project, not a city site.** Municipal Dashboards is not
> run by, or affiliated with, any city government. Every dashboard
> links back to the city's official events calendar and original
> agenda PDFs for anything authoritative.

## How it works (in a paragraph)

For each registered city, a Python `CityAdapter` lists upcoming
meetings, follows each one to its detail page, extracts the agenda
link, and downloads the PDF. **Claude Haiku 4.5** transcribes each PDF
to clean, structured Markdown. **Claude Sonnet 4.6** then classifies
and extracts each agenda item — committee, date, item number, type,
topic — into the canonical `agendas.json` schema for that city. A
shared `template/dashboard.html` is copied into each city's folder
on every run; the dashboard fetches `agendas.json` and a per-city
`branding.json` and renders meeting cards grouped by
`(Committee, Date)`. Component-level design lives in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Cities

Currently registered:

- **Medford, MA** — Finalsite calendar + CivicClerk and Google Doc/Drive agendas.

Phase 2 in progress:

- **Somerville, MA** — Drupal calendar + Legistar agendas.

To add your own city, see "Forking for a new city" below.

## Running the pipeline

```bash
pip install -r requirements.txt
```

**Set `ANTHROPIC_API_KEY` as an OS environment variable, not in a file
inside the repo.** On Windows: *Settings → Environment Variables → New*
under "User variables". On macOS/Linux: add to your shell rc. The
Parser and Synthesizer prefer the OS env var; a project-local `.env` is
only read as a fallback when the OS var is empty.

Then:

```bash
# Run the full pipeline for every registered city (default for cron):
python -m scraper.run_pipeline --process --all

# Or just one city:
python -m scraper.run_pipeline --process --municipality medford-ma

# Stage individually if needed:
python -m scraper.run_pipeline --municipality medford-ma   # download only
python -m scraper.parser                                   # PDFs → Markdown
python -m scraper.synthesizer                              # Markdown → JSON, archive
```

Each pipeline run for a given slug:

1. Downloads new agenda PDFs into `agendas/{slug}/` (gitignored).
2. Parses them with Haiku into `agendas/{slug}/markdown/`.
3. Synthesizes them with Sonnet into `{site_path}/agendas.json`,
   archiving the consumed PDFs + Markdown into `{site_path}/archived/`.
4. Refreshes `{site_path}/index.html` from `template/dashboard.html`
   and `{site_path}/branding.json` from `branding/{slug}.json`.
5. Rewrites the root `cities.json` registry.

The pipeline is idempotent: meetings already in the city's
`agendas.json` are skipped at the Synthesizer stage; PDFs whose
`__{occur_id}__` filename pattern is already on disk (in either the
working dir or the published archive) are skipped at the download
stage. Re-runs are safe.

To deploy after a local run:

```bash
git add medford/ cities.json     # or whichever city/cities changed
git commit -m "Update agendas $(date +%F)"
git push origin main
```

GitHub Pages rebuilds within ~60 seconds. The
[`refresh-agendas` GitHub Actions workflow](.github/workflows/refresh-agendas.yml)
does this automatically every day at 10 UTC.

## Forking for a new city

Adding a new city is purely additive — no edits to existing city code:

1. Write `scraper/adapters/{your-slug}.py` exposing a class that
   implements the `CityAdapter` Protocol (see
   `scraper/adapters/__init__.py` for the contract and
   `scraper/adapters/medford_ma.py` as a reference).
2. Register it in `scraper/adapters/__init__.py`'s `_REGISTRY`.
3. Add `branding/{your-slug}.json` describing the city's chrome
   (logo, colors, eyebrow, tagline, official calendar URL).
4. The next pipeline run will create `{site_path}/` for you with
   the dashboard, the branding file, and any newly archived agendas.
   The landing page will pick it up via `cities.json`.

For the recurring cron, set `ANTHROPIC_API_KEY` in your fork's
*Settings → Secrets and variables → Actions* as documented in
[SCHEDULING.md](SCHEDULING.md).

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
| [TARGET_SITES.md](TARGET_SITES.md) | Every external data source (events calendars, CivicClerk, Legistar, Google Doc/Drive, Anthropic API, GitHub) with URL patterns, auth model, and known constraints. |
| [AGENTS.md](AGENTS.md) | Roles, tool access, and permission matrix for every module (LLM agents, adapters, deterministic scrapers). |
| [SCHEDULING.md](SCHEDULING.md) | Runbook for the GitHub Actions cron that auto-refreshes the dashboards. Where the code runs, where files go, how to set it up on your own fork (including the API key secret). |

## Known limitations

- **Legacy `.doc` (Word 97–2003) files are not supported.** Convert to
  `.pdf` or `.docx` before downstream processing. (`.docx` works.)
- **Image-only PDFs** (scanned without OCR) cause the Parser to surface
  a clear error rather than emit garbage. The pipeline doesn't currently
  fall back to OCR.
- **Item-type classification is heuristic.** The Synthesizer maps each
  item to one of 10 types based on text patterns; review high-stakes
  items in context. Full enum and decision rules in
  [AGENTS.md](AGENTS.md).
- Each dashboard fetches its `agendas.json` from the same origin it's
  served from, so it always reflects whatever's been pushed to `main`.

## License

Public information; site code MIT.
