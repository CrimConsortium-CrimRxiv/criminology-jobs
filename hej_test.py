"""Quick isolated test of the HigherEdJobs search extraction.

Run from the repo root:  python hej_test.py

Prints every listing found (with confidence), the search count, and the cost.
18+ listings means the sanity check will pass in a full run.
"""

from scraper import extract, config

jobs, u = extract.extract_jobs_via_search(
    "HigherEdJobs", config.SOURCES["HigherEdJobs"]["urls"][0], 36)

cost = u["input"] / 1e6 * config.PRICE_IN_PER_MTOK \
    + u["output"] / 1e6 * config.PRICE_OUT_PER_MTOK \
    + u["searches"] * 0.01

print(f"\n{len(jobs)} listings, {u['searches']} searches, ~${cost:.2f}\n")
for j in sorted(jobs, key=lambda x: float(x["confidence"]), reverse=True):
    print(f"  {float(j['confidence']):.2f}  {j['job_title'][:52]:52} {j['institution'][:32]}")
