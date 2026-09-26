# scraper/ — automated job-board refresh

Python pipeline that refreshes the criminology jobs board: it collects postings from the sources of interest, uses the OpenAI API to extract postings and score each one for relevance, merges the results against the current board, and refreshes the site. Independent sources run concurrently with a bounded worker pool.

## How it works

```
fetch  ->  extract  ->  merge/dedup/id  ->  write outputs
```

| File | Role |
|------|------|
| `config.py` | All settings — thresholds, model, sources, relevance criteria. Edit here, not in the logic files. |
| `fetch.py` | Downloads each board's listing pages and reduces them to plain text. |
| `extract.py` | One AI call per source yields structured job rows and a confidence score for each post |
| `run.py` | Orchestrates everything and writes the outputs. `python -m scraper.run` runs the pipeline. |

### Sources

Every board is scraped directly first — one cheap request, exact listings, real
URLs. If the fetch is refused (a bot wall, or a page that comes back empty), that
source falls back to server-side web search for this run only, so a board behind
Cloudflare still refreshes and goes back to being scraped as soon as the wall
comes down. Nothing is pinned to the search path in config.

| Source | Method | Status 2026-09-26 |
|--------|--------|-------------------|
| TSPA | WordPress AJAX endpoint (returns clean JSON) | scraped; no search fallback by design |
| jobs.ac.uk | Direct fetch | scraped |
| ASC | Direct fetch | fetch refused (Cloudflare) — was scraping fine through 2026-09-22 |
| ACJS | Direct fetch | fetch refused (Cloudflare) — search fallback |
| HigherEdJobs | Direct fetch | fetch refused (Incapsula) — search fallback |

TSPA is deliberately excluded from the fallback: it returns clean JSON, so if
that endpoint breaks we want the failure rather than a guess.

### job_url

A `job_url` is only kept if it parses as http(s) with a dotted host, and on the
scraped path only if it actually appears in the page we handed over. The model
will otherwise compose one: a posting whose text held the typo
`https//www.cech.uc.edu/...` was recorded as `https://https//www.cech.uc.edu/...`,
pointing at a host literally named `https`.

### Confidence + review

Every extracted job gets a confidence score (0–0.99) that it fits the board's coverage rules (`CRITERIA` in `config.py`):

- `>= CONFIDENCE_PUBLISH` (0.80): published to the site automatically
- `<  CONFIDENCE_DROP` (0.30): discarded silently (this threshold was set based on data from trial runs, subject to increase as updates continue to be accurate)
- in between: written to `review.csv` for a human decision

To act on a flagged job, put `include` or `exclude` in its `decision` column in `review.csv`; the next run applies it (and remembers it thereafter for that specific post).


## Running it

From the repository root:

```bash
pip install -r requirements.txt
```

Provide an OpenAI API key —  create a `.env` file at the repo root (gitignored) containing:

```
OPENAI_API_KEY=...
```

Then run:

```bash
python -m scraper.run
```

The run prints one line per source (listing count + token cost), a total
cost estimate, and a summary (`+N new, K pending review`). It rewrites
three files at the repo root:

- `criminology_jobs.csv` — the master dataset (existing columns + `confidence`)
- `data.js` — the data the site reads (`window.JOBS_DATA`)
- `review.csv` — jobs awaiting a manual include/exclude decision

It also writes a gitignored `refresh_summary.json`. The GitHub Actions workflow
uses that summary to email the run date, new jobs grouped by source, pending
review count, source failures, and existing jobs that were not verified in the
latest scrape. SMTP configuration uses `SENDER_EMAIL`, `SENDER_PASSWORD`,
`SENDER_SERVER`, and comma- or semicolon-separated `TO_EMAILS`; Titan defaults
to SMTP-over-SSL on port 465 (`SENDER_PORT` can override it).

Published jobs are append-only. A refresh adds new jobs but never removes an
existing published row merely because it disappeared from a source or was not
returned by an extraction run.

## Configuration

| Setting | Default | Purpose |
|---------|---------|---------|
| `CONFIDENCE_PUBLISH` | `0.80` | Auto-publish at/above this score |
| `CONFIDENCE_DROP` | `0.30` | Auto-discard below this score |
| `EXTRACT` | gpt-6-luna, medium effort | Model profile for direct-fetch sources |
| `SEARCH` | gpt-6-luna, high effort | Model profile for the search fallback. High effort is required — at medium the model abandons the sweep and returns nothing. |
| `SEARCH_MAX_SEARCHES` | `40` | Cap on server-side tool calls for a search-based source |
| `SCRAPE_WORKERS` | `3` | Maximum source fetch/extraction calls running concurrently |
| `SEARCH_COUNT_MIN_RATIO` | `0.5` | A source that *fell back to search* and returns fewer than this fraction of its current board count is treated as failed |
| `SOURCES` | — | The five boards and their URLs |
| `CRITERIA` | — | Relevance rules, fed to the model verbatim |
