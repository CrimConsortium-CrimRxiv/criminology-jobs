import os

# Confidence thresholds
# Each entry gets a 0-0.99 confidence score
# >= CONFIDENCE_PUBLISH -> published
# < CONFIDENCE_DROP -> dropped silently
# in between -> written to review.csv for manual decision
# prelim runs with no lower bound + manual review helped define this lower bound
CONFIDENCE_PUBLISH = 0.80
CONFIDENCE_DROP = 0.30

# Model profiles. gpt-6-luna: $0.10/M in, $0.01/M cached in, $0.50/M out,
# 1.05M context, 128k max output. Long-context rates (2x in, 1.5x out) start
# above 272k input tokens, which MAX_INPUT_CHARS keeps us well under.
EXTRACT = {"model": "gpt-6-luna", "effort": "medium",
           "price_in": 0.10, "price_cached": 0.01, "price_out": 0.50}
# High effort is what makes the search sources work: at medium the model gives
# up after ~9 searches and returns nothing, at high it keeps sweeping (~28
# searches) and enumerates the board.
SEARCH = {"model": "gpt-6-luna", "effort": "high",
          "price_in": 0.10, "price_cached": 0.01, "price_out": 0.50}
# Model parameters
MAX_OUTPUT_TOKENS = 64000
MAX_INPUT_CHARS = 300_000
SEARCH_MAX_SEARCHES = 40  # cap on server-side tool calls per search source
# (a full board enumeration observed ~28 calls; 20 cut it off mid-sweep)
WEB_SEARCH_COST = 0.01  # $10 per 1k web_search calls
REQUEST_TIMEOUT = 900  # seconds; search sources run a long server-side tool loop
MAX_RETRIES = 3  # SDK-level retries for transient API errors
SCRAPE_WORKERS = 3  # bound concurrent source fetch/extraction calls

# Sanity check for sources that fell back to search: if results return fewer than X fraction of
# the jobs the board currently lists from that source, the run is considered a failure
# this is only helpful for initial testing against current infrastructure, will be phased out later
SEARCH_COUNT_MIN_RATIO = 0.5

# --- Sources ----------------------------------------------------------------
# Every source is scraped directly first — that gives exact listings and real
# URLs off one cheap request. Search is only a fallback for when the fetch is
# refused, so a board behind a bot wall still refreshes, and goes back to being
# scraped the moment the wall comes down. ACJS, ASC and HigherEdJobs sit behind
# Cloudflare/Incapsula JS challenges as of 2026-09-26 and take the fallback;
# ASC was still scraping normally through 2026-09-22.
#
# "urls": pages fetched and handed to the LLM (extra pages are cheap insurance
#         against pagination; duplicate listings are deduped downstream).
# "kind": "jmajax" = WP Job Manager AJAX endpoint (TSPA), JSON rather than HTML.
#         A jmajax source has no search fallback — if its endpoint breaks we
#         want the failure, not a guess.
SOURCES = {
    "ACJS": {
        "urls": ["https://careers.acjs.org/jobs/"],
    },
    "ASC": {
        "urls": ["https://asc41.org/career-center/position-postings/"],
    },
    "HigherEdJobs": {
        "urls": ["https://www.higheredjobs.com/faculty/search.cfm?JobCat=156"],
    },
    "jobs.ac.uk": {
        "urls": ["https://www.jobs.ac.uk/search/?keywords=criminology"],
    },
    "TSPA": {
        "urls": ["https://www.tspa.org/jm-ajax/get_listings/"],
        "kind": "jmajax",
    },
}

# --- Relevance criteria (fed to the LLM verbatim; mirrors README Coverage) ---
CRITERIA = """\
Include a job only if it fits this board's coverage:
- Roles: faculty + research positions (tenure-track, postdocs, lecturers,
  professors) and, from TSPA, trust & safety practitioner roles. Everything
  listed on the TSPA board is in scope by definition.
- Exclude: adjunct, part-time pool, community-college, and pure
  forensic-science / homeland-security listings.
- Topics: clearly criminology / criminal-justice work; generic law or
  sociology positions without a clear criminology component are excluded.

Score each extracted job with a confidence (0 to 0.99) that it belongs:
- 0.90+: unambiguously in scope (e.g. "Assistant Professor of Criminology",
  or any trust & safety role on TSPA).
- 0.40-0.79: uncertain — plausibly in scope but the criteria could cut either
  way (e.g. a sociology post mentioning crime, a research analyst at a
  justice-adjacent agency, a lecturer post that might be part-time).
- below 0.40: clearly out of scope (adjunct pools, unrelated disciplines,
  paramedic/EMT, generic law school posts).
Include EVERY job listing you find in the output, even clearly out-of-scope
ones, with an honest confidence and a one-line reason. Do not pre-filter —
the pipeline applies the thresholds."""


def load_env():
    """Read KEY=VALUE lines from a repo-root .env into os.environ (no override)."""
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
