import os

# Confidence thresholds
# Each entry gets a 0-0.99 confidence score
# >= CONFIDENCE_PUBLISH -> published
# < CONFIDENCE_DROP -> dropped silently
# in between -> written to review.csv for manual decision
# prelim runs with no lower bound + manual review helped define this lower bound
CONFIDENCE_PUBLISH = 0.80
CONFIDENCE_DROP = 0.30

EXTRACT = {"model": "claude-haiku-4-5", "effort": None, "price_in": 1.00, "price_out": 5.00}
SEARCH = {"model": "claude-sonnet-5", "effort": "high", "price_in": 3.00, "price_out": 15.00}
# Model parameters
MAX_OUTPUT_TOKENS = 64000
MAX_INPUT_CHARS = 300_000
SEARCH_MAX_SEARCHES = 20  # cap on web searches per claude_search

# Sanity check for claude_search sources: if results return fewer than X fraction of the jobs the
# board currently lists from that source, fetch is considered a failure
# this is only helpful for initial testing against current infrastructure, will be phased out later
SEARCH_COUNT_MIN_RATIO = 0.5

# --- Sources ----------------------------------------------------------------
# "urls": pages fetched and handed to the LLM (extra pages are cheap insurance
#         against pagination; duplicate listings are deduped downstream).
# "kind": "jmajax"        = WP Job Manager AJAX endpoint (TSPA).
#         "claude_search" = bot-walled site we can't fetch directly; Claude's
#                           server-side web search/fetch tools enumerate the
#                           listings instead (billed to the same Anthropic key).
SOURCES = {
    "ACJS": {
        "urls": ["https://careers.acjs.org/jobs/"],
        "kind": "claude_search",
    },
    "ASC": {
        "urls": ["https://asc41.org/career-center/position-postings/"],
    },
    "HigherEdJobs": {
        "urls": ["https://www.higheredjobs.com/faculty/search.cfm?JobCat=156"],
        "kind": "claude_search",
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
