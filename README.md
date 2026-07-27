# Criminology Jobs Explorer

A searchable dashboard of current academic, practitioner, and trust & safety job postings in criminology, criminal justice, and closely related fields.

**Live site:** https://crimconsortium.github.io/criminology-jobs

## Sources

Postings are aggregated from publicly visible listings only — anything behind a login is excluded.

- [Academy of Criminal Justice Sciences — Careers](https://careers.acjs.org/jobs/)
- [American Society of Criminology — Career Center](https://asc41.org/career-center/position-postings/)
- [HigherEdJobs — Criminal Justice & Criminology faculty](https://www.higheredjobs.com/faculty/search.cfm?JobCat=156)
- [jobs.ac.uk — criminology search](https://www.jobs.ac.uk/search/?keywords=criminology)
- [Trust & Safety Professional Association — Job Board](https://www.tspa.org/explore/job-board/)

## Coverage

- **Roles:** faculty + research positions (tenure-track, postdocs, lecturers, professors). Adjunct, part-time pool, community-college, and pure forensic-science / homeland-security listings are filtered out.
- **Topics:** clearly criminology / criminal-justice work; generic law or sociology positions without a clear criminology component are excluded.
- **Dedup:** the same position appearing on more than one site is consolidated into a single row, with all source URLs listed.

## Data files

- `criminology_jobs.csv` — the master dataset (downloadable from the site too).
- `data.js` — same data embedded for the in-browser dashboard, with consortium-member tags applied.

## CrimConsortium

Postings from [CrimConsortium](https://crimconsortium.com) member institutions are highlighted with an orange rail and "Consortium" pill.

## Automation

The refresh is automated by a Python pipeline in `scraper/`:

1. **Fetch** — `scraper/fetch.py` pulls each board's listing pages (TSPA via
   its WordPress AJAX endpoint).
2. **Extract** — `scraper/extract.py` sends the page text to the Claude API,
   which returns structured rows plus a **confidence score (0–0.99)** that
   each job fits the coverage rules above. HigherEdJobs blocks direct
   fetching, so Claude's server-side web search enumerates it instead, with
   a sanity check against the board's current count so an incomplete search
   can never silently thin the site.
3. **Merge** — `scraper/run.py` dedups across boards, keeps stable ids for
   jobs already on the board, and drops jobs no longer listed (jobs from a
   source that failed to fetch are kept, never silently dropped).
4. **Review** — jobs scoring below the publish threshold land in
   `review.csv` instead of the site. Type `include` or `exclude` in its
   `decision` column; the next run applies the decision. Thresholds, model,
   and filter criteria are all in `scraper/config.py`.

Run locally with `python -m scraper.run` (needs `pip install -r
requirements.txt` and an `ANTHROPIC_API_KEY` — env var or a `.env` file at
the repo root). On GitHub, the **Refresh job board** workflow runs it and
commits the result; trigger it from the Actions tab (a cron schedule is
stubbed in `.github/workflows/refresh.yml`, commented out until the cadence
is settled).

## Local preview

```bash
python3 -m http.server 5000
# then open http://localhost:5000
```

## Credits

Built and maintained by [Scott Jacques](https://scottjacques.pubpub.org/) (Georgia State University / [CrimRxiv](https://crimrxiv.com)). Visual design mirrors the [Criminology PhD Faculty Explorer](https://crimconsortium.github.io/criminology-faculty-explorer).
