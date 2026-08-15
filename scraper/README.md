# scraper/ — automated job-board refresh

Python pipeline that refreshes the criminology jobs board: it collects postings from the sources of interest, uses Anthropic API to extract postings and score each one for relevance, merges the results against the current board, and refreshes the site.

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

| Source | Method |
|--------|--------|
| ACJS | Cloudflare-protected; Claude web search fetches the postings |
| ASC | Direct fetch |
| jobs.ac.uk | Direct fetch |
| TSPA | WordPress AJAX endpoint (returns clean JSON) |
| HigherEdJobs | Blocked for scraping; Claude web search fetches the postings |

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

Provide an Anthropic API key —  create a `.env` file at the repo root (gitignored) containing:

```
ANTHROPIC_API_KEY=...
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

Published jobs are append-only. A refresh adds new jobs but never removes an
existing published row merely because it disappeared from a source or was not
returned by an extraction run.

## Configuration

| Setting | Default | Purpose |
|---------|---------|---------|
| `CONFIDENCE_PUBLISH` | `0.80` | Auto-publish at/above this score |
| `CONFIDENCE_DROP` | `0.30` | Auto-discard below this score |
| `EXTRACT` | Claude Haiku 4.5, default effort | Model profile for direct-fetch sources |
| `SEARCH` | Claude Sonnet 5, high effort | Model profile for the HigherEdJobs search |
| `SEARCH_MAX_SEARCHES` | `20` | Cap on web searches for a search-based source |
| `SEARCH_COUNT_MIN_RATIO` | `0.5` | A source that returns fewer than this fraction of its current board count is treated as a failed fetch |
| `SOURCES` | — | The five boards, their URLs, and fetch method |
| `CRITERIA` | — | Relevance rules, fed to the model verbatim |
