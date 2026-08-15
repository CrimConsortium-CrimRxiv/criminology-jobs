"""Run the scraper: fetch -> extract -> merge -> write outputs.

Usage:  python -m scraper.run

Outputs: criminology_jobs.csv, data.js (site data), review.csv"""

import csv
import datetime
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pandas as pd

from . import config, extract, fetch

ROOT = os.path.join(os.path.dirname(__file__), "..")
CSV_PATH = os.path.join(ROOT, "criminology_jobs.csv")
DATA_JS_PATH = os.path.join(ROOT, "data.js")
REVIEW_PATH = os.path.join(ROOT, "review.csv")

COLUMNS = [
    "source_site", "job_title", "institution", "department_or_school",
    "country", "city_or_region", "rank_type", "area_specialization",
    "contract_type", "teaching_expectations", "research_expectations",
    "posted_date", "deadline_or_review_date", "salary_currency",
    "salary_range", "job_url", "combined_urls", "id", "consortium_member",
    "confidence",
]
REVIEW_COLUMNS = COLUMNS[:-3] + ["confidence", "reason", "decision"]


@dataclass
class SourceResult:
    rows: list
    failure: str | None
    cost: float
    log: str


def require_api_key():
    """Load and validate the credential before doing any network or file work."""
    config.load_env()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it as a GitHub Actions "
            "repository secret or provide it in the local environment."
        )


def norm_key(row):
    """Match key: normalized title + institution"""
    return re.sub(r"[^a-z0-9]", "", (row["job_title"] + row["institution"]).lower())


def urls_of(row):
    return [u.strip() for u in (row.get("combined_urls") or row.get("job_url", "")).split(",") if u.strip()]


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, columns):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({c: row.get(c, "") for c in columns} for row in rows)


def _scrape_source(name, board_count):
    """Fetch and extract one source without mutating shared scraper state."""
    started = time.monotonic()
    cost = 0.0
    usage_note = ""
    try:
        note = ""
        if config.SOURCES[name].get("kind") == "claude_search":
            profile = (config.SEARCH_LARGE if config.SOURCES[name].get("large_context")
                       else config.SEARCH)
            jobs, usage = extract.extract_jobs_via_search(
                name, config.SOURCES[name]["urls"][0], board_count, profile=profile)
        else:
            profile = config.EXTRACT
            text, note = fetch.fetch_source(name)
            jobs, usage = extract.extract_jobs(name, text)
        cost = (usage["input"] / 1e6 * profile["price_in"]
                + usage["output"] / 1e6 * profile["price_out"]
                + usage["searches"] * 0.01)
        searches = f", {usage['searches']} searches" if usage["searches"] else ""
        usage_note = (
            f" ({usage['input']:,} in / {usage['output']:,} out tokens"
            f"{searches}, ~${cost:.2f})"
        )
        if config.SOURCES[name].get("kind") == "claude_search":
            floor = board_count * config.SEARCH_COUNT_MIN_RATIO
            if len(jobs) < floor:
                raise RuntimeError(
                    f"sanity check: search found {len(jobs)} listings but the "
                    f"board currently has {board_count} from {name} "
                    f"(floor {floor:.0f})")
        for job in jobs:
            job["source_site"] = name
            job["confidence"] = f"{min(max(float(job['confidence']), 0.0), 0.99):.2f}"
        elapsed = time.monotonic() - started
        return SourceResult(
            jobs,
            None,
            cost,
            f"  {name}: {len(jobs)} listings extracted{usage_note} {note} [{elapsed:.1f}s]",
        )
    except Exception as error:
        elapsed = time.monotonic() - started
        failure = f"{name}: {error}"
        return SourceResult(
            [], failure, cost,
            f"  {name}: FAILED - {error}{usage_note} [{elapsed:.1f}s]",
        )


def scrape(board_counts):
    """Fetch + extract sources concurrently. Returns (rows, failures).

    board_counts: how many jobs the board currently lists per source, used for
    preliminary sanity checks
    """
    rows, failures = [], []
    total_cost = 0.0
    source_names = list(config.SOURCES)
    worker_count = min(config.SCRAPE_WORKERS, len(source_names))
    print(f"Running {len(source_names)} sources with {worker_count} workers...", flush=True)
    results = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_scrape_source, name, board_counts.get(name, 0)): name
            for name in source_names
        }
        for future in as_completed(futures):
            name = futures[future]
            result = future.result()
            results[name] = result
            print(result.log, flush=True)

    # Aggregate in configuration order so IDs remain deterministic even though
    # sources finish in a different order from run to run.
    for name in source_names:
        result = results[name]
        rows.extend(result.rows)
        total_cost += result.cost
        if result.failure:
            failures.append(result.failure)
    print(f"API cost this run: ~${total_cost:.2f}")
    return rows, failures


def dedup(rows):
    """Same job on several boards -> one row; merge sources + urls"""
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["combined_urls"] = df["job_url"]
    df["confidence"] = df["confidence"].astype(float)
    df["_key"] = (df["job_title"] + df["institution"]).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
    join_unique = lambda values: ", ".join(dict.fromkeys(i for i in values if i))
    agg = {col: "first" for col in df.columns if col not in ("_key", "source_site", "combined_urls", "confidence")}
    agg["source_site"] = join_unique
    agg["combined_urls"] = join_unique
    agg["confidence"] = "max"
    merged = df.groupby("_key", sort=False).agg(agg).reset_index(drop=True)
    merged["confidence"] = merged["confidence"].map(lambda c: f"{c:.2f}")
    return merged.to_dict("records")


def main():
    require_api_key()
    today = datetime.date.today()

    existing = read_csv(CSV_PATH)
    review = read_csv(REVIEW_PATH)
    decisions = {}  # url -> "include" | "exclude"
    for row in review:
        for url in urls_of(row):
            if row.get("decision", "").strip().lower() in ("include", "exclude"):
                decisions[url] = row["decision"].strip().lower()

    board_counts = {}
    for row in existing:
        for source in row["source_site"].split(","):
            source = source.strip()
            board_counts[source] = board_counts.get(source, 0) + 1

    print("Fetching sources...", flush=True)
    scraped, failures = scrape(board_counts)
    scraped = dedup(scraped)

    # Index existing rows for matching (by URL, then by normalized title+institution).
    by_url = {url: row for row in existing for url in urls_of(row)}
    by_key = {norm_key(row): row for row in existing}
    max_id = max((int(row["id"]) for row in existing if row["id"].isdigit()), default=0)

    published, pending, seen_existing = [], [], set()
    new_count = dropped_low = 0

    for row in scraped:
        match = next((by_url[u] for u in urls_of(row) if u in by_url), None) or by_key.get(norm_key(row))
        if match is not None:
            # Already on the board: keep the curated row (stable id, posted_date, edits).
            if id(match) not in seen_existing:
                seen_existing.add(id(match))
                published.append(match)
            continue
        decision = next((decisions[u] for u in urls_of(row) if u in decisions), None)
        confidence = float(row["confidence"])
        row.setdefault("posted_date", "")
        row["posted_date"] = row["posted_date"] or today.isoformat()
        row["consortium_member"] = ""
        if decision == "exclude":
            pending.append({**row, "decision": "exclude"})
        elif decision == "include" or confidence >= config.CONFIDENCE_PUBLISH:
            max_id += 1
            row["id"] = str(max_id)
            published.append(row)
            new_count += 1
        elif confidence < config.CONFIDENCE_DROP:
            dropped_low += 1
        else:
            reason = row.get("reason", "")
            prior = next((p for p in review for u in urls_of(p) if u in urls_of(row)), None)
            if prior is not None:  # keep original score/reason to avoid churn
                row["confidence"], reason = prior["confidence"], prior.get("reason", reason)
            pending.append({**row, "reason": reason, "decision": ""})

    # The published board is append-only. A missing result can mean a source
    # changed markup, blocked the fetch, or the model overlooked a listing, so
    # absence from one refresh is never sufficient evidence to remove a job.
    for row in existing:
        if id(row) in seen_existing:
            continue
        published.append(row)

    published.sort(key=lambda r: (r.get("posted_date", ""), int(r["id"]) if r["id"].isdigit() else 0), reverse=True)

    write_csv(CSV_PATH, published, COLUMNS)
    write_csv(REVIEW_PATH, pending, REVIEW_COLUMNS)
    write_data_js(published, today)

    for failure in failures:
        print(f"WARNING: {failure} (its existing jobs were kept)")
    if dropped_low:
        print(f"{dropped_low} listings auto-dropped below CONFIDENCE_DROP={config.CONFIDENCE_DROP}")
    n_pending = sum(1 for p in pending if not p["decision"])
    print(f"Refresh: {today.isoformat()} (+{new_count} new, {n_pending} pending review)")


def write_data_js(rows, today):
    payload = {
        "compiled": f"{today:%B} {today.day}, {today.year}",
        "consortium_url": "https://crimconsortium.com",
        "jobs": [{c: row.get(c, "") for c in COLUMNS} for row in rows],
    }
    with open(DATA_JS_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"/* Auto-generated by scraper/run.py {today.isoformat()}. */\n")
        f.write("window.JOBS_DATA = ")
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))
        f.write(";\n")


if __name__ == "__main__":
    main()
