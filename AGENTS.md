# Agents & Modules — Roles, Permissions, and Tool Access

> This project mixes two kinds of "agents":
>
> 1. **LLM agents** (Anthropic API calls — Haiku Parser, Sonnet Synthesizer)
> 2. **Deterministic scraper modules** (Python; no LLM in the loop)
>
> Both are documented here. The deterministic modules aren't "agents" in the
> Claude sense, but they fill the same role in the pipeline: encapsulated
> components with a defined contract, inputs, outputs, and tool surface.

## Agent / module roster

| Role | Module | Type | Inputs | Outputs |
|---|---|---|---|---|
| **Scribe Agent** | `.claude/agents/scribe.md` | Sub-Agent (Haiku) | Task description | Doc updates + git commit/push |
| **Web Investigator** | `.claude/agents/web-investigator.md` | Sub-Agent (Sonnet) | Task description | Selectors, API endpoints, data shapes |
| **City Adapter (Protocol)** | `scraper/adapters/__init__.py` | Deterministic | Slug | `CityAdapter` instance (with `slug`, `name`, `site_path`) |
| Medford Adapter | `scraper/adapters/medford_ma.py` | Deterministic | `today`, `lookahead_days`; later `MeetingRecord` | `list[MeetingRecord]`; `AgendaDownloadResult` |
| Calendar Scraper (Medford-specific) | `scraper/calendar_scrape.py` | Deterministic | Today's date + lookahead window | `list[Meeting]` |
| Detail Scraper (Medford-specific) | `scraper/event_detail_scrape.py` | Deterministic | Meeting `detail_url` | `EventDetail` (incl. `agenda_url`, `agenda_type`) |
| CivicClerk Downloader (host-level) | `scraper/civicclerk_download.py` | Deterministic | CivicClerk portal URL | PDF (or plain text) on disk |
| Google Downloader (host-level) | `scraper/google_download.py` | Deterministic | Google Doc / Drive share URL | PDF on disk |
| **Parser Agent** | `scraper/parser.py` | LLM (Claude Haiku 4.5) | PDF | Markdown on disk |
| **Synthesizer Agent** | `scraper/synthesizer.py` | LLM (Claude Sonnet 4.6) | Markdown + existing `agendas.json` | Updated `agendas.json` + archived sources |
| Pipeline Orchestrator | `scraper/run_pipeline.py` | Deterministic glue (city-agnostic) | Slug + adapter + all of the above | Run summary, exit code |

The orchestrator only ever talks to a `CityAdapter`. Adapters compose
the host-level downloaders and (where applicable) Medford-specific
deterministic helpers. LLM agents are downstream of and independent
from the adapter layer.

## LLM Agents — full contract

### Parser Agent

| | |
|---|---|
| **Model** | `claude-haiku-4-5` |
| **Why this model** | Fastest, cheapest model that handles vision/document input well. PDF→Markdown is a transcription job that doesn't need reasoning. |
| **Thinking** | Off by default (Haiku doesn't support adaptive) |
| **Max tokens** | 8000 |
| **Structured output** | No — emits plain Markdown |
| **System prompt cache** | `cache_control: {"type": "ephemeral"}` set on the system block. Note: our system prompt is short (~80 tokens), well under Haiku's 4096-token cache minimum, so caching is a no-op in practice but harmless. |
| **Tool access** | None (no tool use) |
| **Validation** | Output < 200 bytes → `ParserError` (almost always indicates extraction failure) |
| **Permissions** | Read PDFs from `agendas/`. Write Markdown to `agendas/markdown/`. No deletion or modification of source PDFs. |
| **Failure mode** | Surfaces `ParserError` per-PDF; caller (orchestrator) logs and continues with the next PDF. |

### Synthesizer Agent

| | |
|---|---|
| **Model** | `claude-sonnet-4-6` |
| **Why this model** | Per-item classification into a 10-value enum requires real reasoning; Haiku's accuracy on this task is materially lower in our testing. |
| **Thinking** | `{"type": "adaptive"}` — Claude self-decides per request |
| **Effort** | `medium` (sweet spot of cost vs quality for this task) |
| **Max tokens** | 16000 |
| **Structured output** | Yes — `output_config.format` with strict JSON Schema enforcing the 10-value `Item_Type` enum |
| **Tool access** | None |
| **Validation** | JSON parse + schema is enforced server-side; we still wrap `json.loads()` in a try/except |
| **Permissions** | Read Markdown from `agendas/markdown/`. Read/write `agendas.json`. Move PDFs and Markdown from `agendas/` and `agendas/markdown/` into `agendas/archived/`. |
| **Idempotency** | A meeting whose `Source_File` already appears in `agendas.json` is skipped (no API call) but the source is still archived. |
| **Failure mode** | Per-meeting `SynthesizerError` is logged; remaining meetings still get processed. Run exits non-zero only if at least one meeting failed. |

### Why split Parser from Synthesizer at all?

Two reasons, both still hold:

1. **Cost.** Haiku is ~5× cheaper than Sonnet on input tokens. PDFs are
   the largest input the pipeline handles.
2. **Auditability.** Markdown is a human-readable intermediate. When the
   JSON looks wrong, we can inspect the Markdown to localize whether
   the issue is in extraction (Parser's job) or classification
   (Synthesizer's job).

## Authentication & secrets

| Secret | Where it lives | Loaded by |
|---|---|---|
| `ANTHROPIC_API_KEY` | Windows user environment variable (preferred) **or** `.env` at project root (gitignored) | `python-dotenv` `load_dotenv()` at module import time in `parser.py` and `synthesizer.py` |

Both modules check that the key is set and non-empty before constructing
the `anthropic.Anthropic()` client; placeholder strings starting with
`sk-ant-REPLACE_ME` also raise an explicit error.

> ⚠️ **Active workstream:** the `load_dotenv(override=True)` setting was
> introduced to mask Claude Code's empty `ANTHROPIC_API_KEY=""` sandbox
> default. With the user now using a Windows user env var for the real
> key, we need to switch to a smart-override pattern that only overrides
> when the existing OS value is empty/whitespace. See `MEMORY.md`
> "Active workstream" and `TODO.md`.

## Permissions matrix

| Module | Filesystem read | Filesystem write | Network | Anthropic API |
|---|---|---|---|---|
| `calendar_scrape` | (none) | (none) | medfordma.org HTTPS | (none) |
| `event_detail_scrape` | (none) | (none) | medfordma.org HTTPS | (none) |
| `civicclerk_download` | (none) | dest dir | medfordma.api.civicclerk.com HTTPS | (none) |
| `google_download` | (none) | dest dir | docs.google.com / drive.google.com HTTPS | (none) |
| `parser` | `agendas/*.pdf` | `agendas/markdown/*.md` | api.anthropic.com (via SDK) | ✅ via `ANTHROPIC_API_KEY` |
| `synthesizer` | `agendas/markdown/*.md`, `agendas.json`, `agendas/*.pdf` | `agendas.json`, `agendas/archived/` | api.anthropic.com (via SDK) | ✅ via `ANTHROPIC_API_KEY` |
| `run_pipeline` | All scraper inputs | All scraper outputs | All scraper destinations | ✅ when `--process` is used |

## Tool access (LLM agents)

Neither LLM agent uses tool calling. The Parser takes a PDF document
content block. The Synthesizer takes a text content block and uses
structured outputs (`output_config.format`) — this is *not* a tool, it's
a response-format constraint.

If we ever need to add tools (e.g., a vision-mode "look up the source
URL for this committee" tool), the natural place to add them would be
in the Synthesizer, since it does the only real reasoning.

## Future agents (not yet built)

- **Scheduler agent** (Step 4) — cron / GitHub Actions wrapper that runs the orchestrator on a schedule, commits the diff, opens a PR or auto-pushes. No LLM involved; just a scheduled CI job.
- **Subscriber digest agent** (roadmap) — would read `agendas.json`, match user keyword watchlists, and email digests. Out of scope for the current pipeline.
