# Target Sites & External Data Sources

> Every external system the pipeline touches: how we authenticate (or
> don't), what we send, what we get back, and any constraints we've
> discovered the hard way.

## 1. City of Medford Events Calendar (primary scrape target)

| | |
|---|---|
| **URL** | https://www.medfordma.org/about/events-calendar |
| **Owner** | City of Medford, MA |
| **Stack** | Finalsite CMS (themed `default_22`) |
| **Used by** | `scraper/calendar_scrape.py` |
| **Auth** | None (fully public) |
| **Format** | HTML, server-rendered |
| **Rate limit** | None observed; 1 HTTP GET per pipeline run |

### Behavior we discovered

- The page renders a **5-week month grid** (~35 days from the Sunday before the 1st to the Saturday after the last day of the current month). The default page already includes more than our 14-day lookahead, so **no pagination needed in the typical case**.
- Query params `?cal_date=YYYY-MM-DD` and `?view=week` are **client-side only** — the server returns the same HTML regardless. Don't waste time on them.
- The Finalsite calendar element on the page has `data-calendar-ids=351` (Medford's main calendar).
- Underneath, the page makes an AJAX call to `/fs/elements/{element_id}?cal_date=...`. ⚠️ **As of 2026-04-30, `cal_date` is silently ignored** by the CDN cache key — see "CDN cache gotcha" below. We still send the parameter (it's still a valid URL) and we still send a "today + 30 days" probe in addition to "today" so we get cache-shard variance, but the real defense is the no-cache request header.

### CDN cache gotcha (2026-04-30 incident)

The endpoint is fronted by a CDN that returns:

```
Cache-Control: public, s-maxage=3600, max-age=300, stale-if-error=21600,
               stale-while-revalidate=15
```

The cache key **strips the `cal_date` query parameter**. So
`?cal_date=2026-03-15`, `?cal_date=2026-09-15`, `?cal_date=anything`
all return the same cached response — whatever happened to be in the
shard last. The server-side state of "what month to show" rolls
forward through the day, and downstream consumers (us) get whichever
month was cached most recently.

**Symptom we saw:** the daily 10 UTC cron consistently missed early-May
meetings on 2026-04-30. The cron's `cal_date=2026-04-30` and
`cal_date=2026-05-30` probes returned an "April-flavored" cached
response with only 2 events in the 14-day window. Hours later, the
same URL began returning a "May-flavored" response with 16+ events.

**Fix:** the calendar fetcher in `scraper/calendar_scrape.py` now
sends `Cache-Control: no-cache` + `Pragma: no-cache` request headers,
which forces the CDN to revalidate against origin. Confirmed locally
that this restores per-call freshness even when other consumers are
poisoning the cache shard with stale content.

**Forensic capture:** the orchestrator now writes raw HTTP responses
to `agendas/{slug}/.last_calendar_responses/probe_*.html` on every
run, and the GitHub Actions workflow uploads that directory as an
artifact. So the next time something silently breaks, we have actual
evidence to debug from instead of guessing.

### Markup contract we depend on

Each meeting renders as:

```html
<a class="fsCalendarEventTitle fsCalendarEventLink"
   title="Retirement Board Meeting"
   data-occur-id="22908"
   href="https://www.medfordma.org/about/events-calendar/event-details/~occur-id/22908">
  Retirement Board Meeting
</a>
<div class="fsTimeRange">
  <time datetime="2026-03-31T09:30:00-04:00" class="fsStartTime">…</time>
</div>
```

We extract: `data-occur-id`, `title`, `href`, `<time datetime>`.

### Filter rule

Empirically, every governmental meeting on the calendar has the word
**"Meeting"** in its title; non-meeting community events do not. We
filter on this — case-insensitive substring match on the title.

---

## 2. City of Medford event detail pages

| | |
|---|---|
| **URL pattern** | `https://www.medfordma.org/about/events-calendar/event-details/~occur-id/{N}` |
| **Used by** | `scraper/event_detail_scrape.py` |
| **Auth** | None |
| **Rate limit** | ~1 HTTP GET per meeting per pipeline run; no observed throttling |

### What's on the page

A `<div class="fsDescription">` block contains a few `<p>` lines, each
labeled. We parse:

```
<p>Agenda: <a href="...">{some link}</a></p>
<p>Location: City Hall - Room 201</p>
<p>Zoom: <a href="https://us06web.zoom.us/...">link</a></p>
```

### Agenda URL types we handle

The agenda link is always one of these (or absent):

| Type | URL pattern | Coverage in sample |
|---|---|---|
| `CIVICCLERK` | `https://medfordma.portal.civicclerk.com/event/{event_id}/files/agenda/{file_id}` | City Council, Committee of the Whole |
| `GOOGLE_DOC` | `https://docs.google.com/document/d/{ID}/edit?...` | Conservation Commission, Zoning Board (some) |
| `GOOGLE_DRIVE_FILE` | `https://drive.google.com/file/d/{ID}/view?...` | MCHSBC, Retirement Board |
| `OTHER` | Anything else | Rare; surfaced but not auto-downloaded |
| `MISSING` | No agenda link in description block | ~5% of meetings |

---

## 3. CivicClerk (Medford City Council & Committee agendas)

| | |
|---|---|
| **Public portal** | https://medfordma.portal.civicclerk.com (React SPA, OIDC PKCE) |
| **API** | https://medfordma.api.civicclerk.com/v1 |
| **Used by** | `scraper/civicclerk_download.py` |
| **Auth** | **None — fully public**. Confirmed via Playwright network capture: the SPA's API calls carry no `Authorization` header. |
| **Rate limit** | Not documented, none observed at our cadence (a few calls per week) |

### Download endpoint

```
GET https://medfordma.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId={id},plainText=false)
```

OData-style — note the parens with `fileId=` parameter, not a path.

- `plainText=false` → returns the PDF (~100 KB typical)
- `plainText=true` → returns extracted text (~2 KB typical; sometimes empty if extraction hasn't run yet)

### Important wire-format details we learned

- The `event_id` in the portal URL (`/event/{N}`) is **not** what the
  API takes; the API uses the `file_id` (the second number in the URL).
- All response bytes start with `%PDF-1.7` for PDF requests — we
  validate this magic number.
- We pass `Origin: https://medfordma.portal.civicclerk.com` as a defensive
  measure; not strictly required but matches what a browser would send.

### Why we ditched the Playwright plan

Initial recon suggested CivicClerk's API required a bearer token from
its OIDC PKCE flow. Headless Chromium debug logging revealed the API
calls actually have no `Authorization` header — the API is genuinely
public. The early 405/404 responses were because we'd guessed the wrong
endpoint path (`/Events/GetEventFileStream` instead of `/Meetings/GetMeetingFileStream`).

---

## 4. Google Docs / Google Drive (board agendas)

| | |
|---|---|
| **Used by** | `scraper/google_download.py` |
| **Auth** | None — relies on the share-link making each file publicly accessible |
| **Rate limit** | Not documented; per-IP throttling possible at very high cadence (we are nowhere near it) |

### Endpoints

```
# Google Doc → PDF export
GET https://docs.google.com/document/d/{DOC_ID}/export?format=pdf

# Google Drive file (any type, here used for PDFs)
GET https://drive.google.com/uc?export=download&id={FILE_ID}
# Redirects to drive.usercontent.google.com — `requests` follows transparently
```

### Constraints

- Only works for files the city has shared **publicly** ("Anyone with link"). Any link with `?usp=sharing` typically qualifies.
- Drive shows a "virus scan warning" interstitial for files >100 MB; we never see this with agenda-sized PDFs but the module would fail-loud if we did (the response body wouldn't have `%PDF` magic).
- Google Doc exports support multiple formats: `pdf`, `txt`, `md`, `html`, `docx`. We currently use `pdf` for parity with the Parser pipeline; `md` could be a future optimization (in roadmap).

---

## 5. Anthropic API (LLM agents)

| | |
|---|---|
| **URL** | https://api.anthropic.com/v1/messages (via official `anthropic` Python SDK) |
| **Used by** | `scraper/parser.py`, `scraper/synthesizer.py` |
| **Auth** | `ANTHROPIC_API_KEY` env var, loaded via `python-dotenv` from `.env` (gitignored) or inherited from OS env |
| **Models** | `claude-haiku-4-5` (Parser), `claude-sonnet-4-6` (Synthesizer) |
| **Cost** | ~$0.10 per typical pipeline run (7 meetings) |

### Parser request shape

```python
client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=8000,
    system=[{"type": "text", "text": PARSER_SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": [
        {"type": "document",
         "source": {"type": "base64", "media_type": "application/pdf",
                    "data": <base64 of PDF bytes>}},
        {"type": "text", "text": "Convert this PDF to structured Markdown..."},
    ]}],
)
```

### Synthesizer request shape

```python
client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SYNTHESIZER_OUTPUT_SCHEMA},
    },
    system=[{"type": "text", "text": SYNTHESIZER_SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": "<source filename + agenda markdown>"}],
)
```

The schema enforces the 10-value `Item_Type` enum.

---

## 6. GitHub (hosting + Pages)

| | |
|---|---|
| **Repo** | https://github.com/ecodad/municipal-dashboard |
| **Owner** | `ecodad` |
| **Auth** | `gh` CLI authenticated as `ecodad` (locally) |
| **Pages** | Source: `main` branch root; URL: https://ecodad.github.io/municipal-dashboard/ |
| **Used by** | All commits (`git push`); `gh repo create`/`gh api .../pages` initial setup |

Pages typically rebuilds within 60–90 seconds of a push.

---

## Hard constraints / known limitations across all sources

- **`.doc` (Word 97-2003) files unsupported** — neither Parser nor any current downloader handles them. Convert to `.pdf` or `.docx` first.
- **Image-only PDFs** (scanned with no OCR) cause Haiku to emit a "no text" placeholder. The Parser detects sub-200-byte output and raises `ParserError`; caller decides whether to retry.
- **Calendar markup is stable but not contractual.** Finalsite is a CMS; if Medford rebuilds the site we may need to re-derive the `data-calendar-ids` and the `fsCalendarEventTitle` selector.
- **OData URL with parens.** The CivicClerk endpoint URL contains `(` and `)`. `requests` doesn't urlencode these by default, which is what we want — the server expects them literal.
