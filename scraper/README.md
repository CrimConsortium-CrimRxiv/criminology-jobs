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
URLs. If the fetch is refused, the page is fetched through Perplexity's
`fetch_url` (it reaches pages our requests cannot) and then goes through the
same extractor. Search-based enumeration is the last resort, because it is
stochastic: the same board returned 33, 25 and 0 listings from identical code.
Nothing is pinned to a fallback in config, so a board goes back to being scraped
as soon as its wall comes down.

```
scrape  ->  (if refused) proxy-fetch  ->  (if no proxy key) search
```

| Source | Path | Listings 2026-09-26 |
|--------|------|---------------------|
| TSPA | WP AJAX endpoint (clean JSON) | 135 scraped |
| jobs.ac.uk | Direct fetch | 8 scraped |
| HigherEdJobs | Direct fetch (Incapsula; intermittent) | 25 scraped |
| ASC | Proxy fetch (Cloudflare since ~2026-09-22) | 62 |
| ACJS | Proxy fetch (Cloudflare) | 1 — see below |

TSPA has no fallback by design: it returns clean JSON, so a break there should
surface rather than be guessed at.

**ACJS is the unsolved one.** Its listing page is JS-rendered, so `fetch_url`
returns only one posting; its `robots.txt` refuses Perplexity's crawler on
detail pages; and our own requests get a Cloudflare challenge. Searching finds
real postings but only ~10 of 61 and sometimes none. It therefore trips the
count sanity check most runs, which keeps its existing rows and adds nothing —
the safe failure. Worth revisiting with a rendered fetch.

### Retiring dead listings

The board used to be purely append-only, so it accumulated postings that had
already been taken down — one row still linked to a page reading "Position
Deleted on 1/02/2026". Every refresh now re-checks stored postings:

- a posting is retired only on a **definite** signal — HTTP 404/410, or a dead
  marker in the page (`DEAD_MARKERS` in `fetch.py`). HigherEdJobs serves deleted
  postings with HTTP 200, so the page has to be read, not just pinged.
- anything unreadable (bot wall, timeout, 5xx) is **kept**. A board we cannot
  read is not a board without jobs.
- rows with no URL cannot be checked at all, so they age out after
  `UNVERIFIABLE_MAX_AGE_DAYS`.
- past `MAX_AGE_DAYS` (240) the rule inverts: a posting has to be confirmed
  **live** to stay. Chasing the bot wall cannot clear old cruft on its own — the
  posting reported as dead was 329 days old on a host that answers almost no
  checks, so a liveness-only rule would have kept it indefinitely. No academic
  posting is still open after eight months.

Volume is what gets us blocked: 8 concurrent requests made higheredjobs.com
report 144 of 154 postings unverifiable, and a few hundred checks in a day made
it refuse all 154. So checks run one-at-a-time per host with a pause, capped at
`LIVENESS_MAX_PER_HOST` per run, spending the budget on the least recently
probed rows (`last_probed`, which records the attempt rather than the
outcome so a refusing host does not soak up the budget every run) and working
through the rest over later runs. A host is abandoned for the run after five
unreadable replies in a row, since pressing on only deepens the block.
Perplexity cannot stand in here — HigherEdJobs tells it "JavaScript is required"
and ACJS's robots.txt refuses it.

### job_url

A `job_url` is kept only if it is plausibly *that posting's* page:

- it parses as http(s) with a dotted host. The model composes URLs otherwise: a
  posting whose text held the typo `https//www.cech.uc.edu/...` was stored as
  `https://https//www.cech.uc.edu/...`, a link to a host named `https`.
- it is not a browse page. Search offered `/jobs/state/Massachusetts/` as a
  professorship's URL, and rows held bare `https://www.berkeley.edu/`.
  ATS links that name the posting in the query (`/jobs/?ashby_jid=...`) are kept.
- on the scraped path it must appear in the page we handed over.

A refresh also repairs rows already stored with an unusable URL rather than
leaving a dead link on the site.

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
PERPLEXITY_API_KEY=...   # optional; reaches bot-walled listing pages
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
| `SEARCH_COUNT_MIN_RATIO` | `0.5` | A source enumerated indirectly (proxy or search) returning fewer than this fraction of its board count is treated as failed |
| `PROXY` | gpt-6-luna via Perplexity | Profile for proxy-fetching a walled page |
| `PRUNE_DEAD_LISTINGS` | `True` | Retire postings that are definitely gone |
| `LIVENESS_MAX_PER_HOST` | `200` | Liveness checks per host per run |
| `UNVERIFIABLE_MAX_AGE_DAYS` | `120` | Age-out for rows with no URL |
| `MAX_AGE_DAYS` | `240` | Past this age a posting must be confirmed live to stay |
| `SOURCES` | — | The five boards and their URLs |
| `CRITERIA` | — | Relevance rules, fed to the model verbatim |
