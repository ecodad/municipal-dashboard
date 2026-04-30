
> Runbook for the daily automatic refresh that keeps `agendas.json` and
> the live dashboard up to date without anyone running anything locally.
> Written so a forker can set up the same flow for their own city.

## TL;DR

A GitHub Actions cron job runs once a day at **10:00 UTC**, executes
the scraper → parser → synthesizer pipeline on a fresh Ubuntu runner,
and commits any new data back to `main`. GitHub Pages then rebuilds the
dashboard automatically. The Anthropic API key lives in repo secrets,
never in the repo.

The workflow file is [`.github/workflows/refresh-agendas.yml`](.github/workflows/refresh-agendas.yml).

## The flow at a glance

```
┌──────────────────────────────────────────────────────────────────┐
│ GitHub repository: main branch                                   │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                cron: "0 10 * * *"  (daily at 10 UTC)
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ GitHub Actions: ephemeral Ubuntu runner                          │
│                                                                  │
│  1. Clone main                                                   │
│  2. Install Python 3.13 + pip deps from requirements.txt         │
│  3. Run `python -m scraper.run_pipeline --process --all`         │
│     (ANTHROPIC_API_KEY from repo secrets). For each registered   │
│     adapter:                                                     │
│       a. Scrape city calendar  →  list of meetings               │
│       b. Resolve detail / agenda URL per meeting                 │
│       c. Download PDFs into agendas/{slug}/                      │
│       d. Parse PDFs with Claude Haiku  →  Markdown               │
│       e. Synthesize Markdown with Sonnet → {site_path}/agendas.json│
│       f. Move sources to {site_path}/archived/                   │
│       g. Refresh {site_path}/index.html (from template) +        │
│          {site_path}/branding.json (from branding/{slug}.json)   │
│     Then once at the end: rewrite root cities.json registry.     │
│  4. If anything changed, commit + push back to main (bot user)   │
│  5. Upload per-city run summaries as artifact                    │
│                                                                  │
│  Runner is destroyed after the job finishes. Filesystem is gone. │
│  The only persistent output is the commit back to main.          │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               │ git push origin HEAD:main
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ Repository main branch — new commit                              │
│   • {site_path}/agendas.json   (regenerated, per city)           │
│   • {site_path}/archived/      (newly-archived PDFs + .md)       │
│   • {site_path}/index.html     (refreshed from template)         │
│   • {site_path}/branding.json  (refreshed from branding/{slug}.json)│
│   • cities.json                (root registry)                   │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               │ Pages rebuild (~60s)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ Live dashboard updates automatically                             │
│   /                       — landing page lists all cities        │
│   /{site_path}/           — each city's dashboard                │
└──────────────────────────────────────────────────────────────────┘
```

## Where things actually run

| Question | Answer |
|---|---|
| Where does the code execute? | An ephemeral Ubuntu VM provisioned by GitHub Actions. The runner has no persistent disk; nothing survives between runs except what gets committed. |
| Where are the agenda PDFs downloaded? | The runner's local filesystem under `agendas/`. They are then moved into `agendas/archived/` (still on the runner). The runner pushes those archived files back to the repo via git. |
| Where does the Anthropic API key live? | GitHub repo secrets (encrypted at rest, injected as an env var only during the pipeline-run step). Never in the repo, never logged. |
| Where does the regenerated `agendas.json` end up? | Committed to `main` by the workflow. GitHub Pages serves it from there. |
| What identity does the bot push as? | `medford-dashboard-bot` (user.name) using the GitHub Actions–provided `GITHUB_TOKEN`. Commits show up as authored by the bot, with `Co-Authored-By: Claude Opus 4.7` on the human-driven runs. |
| Does this cost anything? | GitHub Actions for public repos: free (2,000 minutes/month, runs use ~2 minutes). Anthropic API: ~$0 on idle days (no new meetings → Synthesizer is a no-op), ~$0.10 on busy days. Daily cron over a year is roughly $5–$15 of API. |

## Setting it up on your own fork

These steps assume you've already forked the repo and customized the
scraper for your own city. (When the multi-municipality refactor lands —
see TODO.md — there will be a separate `MUNICIPALITY_SETUP.md` for the
config side; this doc covers only the scheduling/CI side.)

### 1. Get an Anthropic API key

Go to https://console.anthropic.com/settings/keys and create a key. Use
a descriptive name like `agenda-pipeline-prod` so you can identify it
later. **Copy the key value somewhere safe for the next 30 seconds — the
console only shows it once.**

### 2. Add the key as a GitHub repo secret

Repo secrets are encrypted at rest and only readable by workflows
running in your repo. They are the right place for an API key.

1. Open your forked repo on GitHub.
2. Click **Settings** (top-right of the repo page).
3. In the left sidebar, click **Secrets and variables → Actions**.
4. Click **New repository secret**.
5. Fill in:
   - **Name:** `ANTHROPIC_API_KEY` (must match exactly — the workflow
     looks for this name)
   - **Secret:** paste the key you just copied
6. Click **Add secret**.

**Direct link template:** `https://github.com/<your-username>/<your-fork>/settings/secrets/actions/new`

The secret is now available to workflow runs in your repo. You can edit
or rotate it from the same page later.

### 3. Verify the workflow exists in your fork

`.github/workflows/refresh-agendas.yml` should already be there from the
fork. If it isn't, copy it over from the upstream repo. GitHub
auto-detects new workflow files on push.

### 4. Run it manually once to verify

Don't wait for the next 10:00 UTC to find out something is broken.

1. Go to **Actions** tab in your repo.
2. In the left sidebar, click **Refresh Agendas**.
3. Click **Run workflow** (top right of the runs list) → select `main`
   → **Run workflow**.
4. Wait ~1–2 minutes. The new run appears at the top of the list.
5. Click into it to see step-by-step logs. The "Run pipeline" step is
   where the scraper/parser/synthesizer actually execute; expand it to
   confirm meetings are being processed.
6. If the pipeline produced new data, the "Commit and push if anything
   changed" step will show a `git push`. If not, it'll log "No data
   changes — nothing to commit." Both are healthy outcomes.
7. The "Upload run summary" step always runs (even on failure) and
   attaches `agendas/.last_scraper_run.json` as a workflow artifact —
   useful for debugging.

### 5. Verify the live dashboard updated

If the workflow committed new data, the live dashboard should reflect
it within ~60 seconds. For Medford that's
https://ecodad.github.io/municipal-dashboard/ — for your fork it'll be
`https://<your-username>.github.io/<your-fork>/`.

After that one verification run, the cron takes over. You can ignore
this thing for as long as the city's calendar / agenda hosts don't
change shape.

## Adjusting the cadence

The workflow runs daily at 10:00 UTC by default. To change:

1. Edit `.github/workflows/refresh-agendas.yml`.
2. Change the cron line:
   ```yaml
   - cron: "0 10 * * *"   # m h dom mon dow
   ```
   - `0 10 * * *` — daily at 10:00 UTC
   - `0 10 * * 1`  — weekly on Mondays at 10:00 UTC
   - `0 8,16 * * *` — twice daily, at 08:00 and 16:00 UTC
   - `*/15 * * * *` — every 15 minutes (don't actually do this)
3. Commit and push. The new schedule takes effect immediately.

Use https://crontab.guru/ to sanity-check any expression. Also note:
GitHub may delay scheduled runs by up to ~15 minutes during periods of
high load — don't depend on minute-exact timing.

## Troubleshooting

### "Run pipeline" step fails with `ANTHROPIC_API_KEY is not set`

You skipped step 2, or the secret name doesn't match. The workflow
looks for the secret named exactly `ANTHROPIC_API_KEY`. Re-check the
spelling at *Settings → Secrets and variables → Actions*.

### "Commit and push" step fails with `Permission to ... denied`

The default `GITHUB_TOKEN` doesn't have write permissions. The workflow
already declares `permissions: contents: write`, but if you removed
that block it'll fail. Verify it's still in the YAML.

### Workflow runs successfully but the live site doesn't update

GitHub Pages can take 60–90 seconds to rebuild, sometimes longer if
their build queue is busy. Check **Settings → Pages** for the build
status. If it shows an error, click into the failed build for details.

### Synthesizer fails on one meeting but the rest succeed

That's expected behavior — per-meeting failures are logged, the
remaining meetings still get processed, and a non-zero exit code only
fires if a hard failure occurs (the entire stage erroring, not one
meeting). Check the run summary artifact for which meeting failed and
why. Most common cause: a new agenda URL host the pipeline doesn't yet
recognize (`agenda_type=OTHER`). See `TARGET_SITES.md` for the current
supported set.

### I rotated my Anthropic key — how do I update?

1. Create a new key in the Anthropic console.
2. Go to *Settings → Secrets and variables → Actions*, click
   `ANTHROPIC_API_KEY`, click **Update**, paste the new value, save.
3. Disable or delete the old key in the Anthropic console once you've
   verified one workflow run succeeds with the new key.

The next scheduled or manual run picks up the new value automatically.

## What this doc does NOT cover

- **Setting up the project from scratch in a new city.** That's the
  multi-municipality refactor (still pending — see TODO.md "Pending
  features → Multi-municipality support"). When it lands, look for
  `MUNICIPALITY_SETUP.md` for that workflow.
- **Running the pipeline locally.** See the "Running the pipeline"
  section of `README.md` for the local commands.
- **Designing your own scheduling cadence beyond cron.** GitHub Actions
  also supports issue-comment triggers, push triggers, etc. Out of
  scope here; consult GitHub's
  [workflow trigger docs](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows).
