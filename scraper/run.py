"""Run the scraper: fetch -> extract -> merge -> write outputs.

Usage:  python -m scraper.run

Outputs: criminology_jobs.csv, data.js (site data), review.csv"""

import csv
import datetime
import json
import os
import re
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


def scrape(board_counts):
    """Fetch + extract every source. Returns (rows, ok_sources, failures).

    board_counts: how many jobs the board currently lists per source, used for
    preliminary sanity checks
    """
    rows, ok, failures = [], [], []
    total_cost = 0.0
    for name in config.SOURCES:
        try:
            note = ""
            if config.SOURCES[name].get("kind") == "claude_search":
                jobs, usage = extract.extract_jobs_via_search(
                    name, config.SOURCES[name]["urls"][0], board_counts.get(name, 0))
                floor = board_counts.get(name, 0) * config.SEARCH_COUNT_MIN_RATIO
                if len(jobs) < floor:
                    raise RuntimeError(
                        f"sanity check: search found {len(jobs)} listings but the "
                        f"board currently has {board_counts[name]} from {name} "
                        f"(floor {floor:.0f})")
            else:
                text, note = fetch.fetch_source(name)
                jobs, usage = extract.extract_jobs(name, text)
            for job in jobs:
                job["source_site"] = name
                job["confidence"] = f"{min(max(float(job['confidence']), 0.0), 0.99):.2f}"
            rows.extend(jobs)
            ok.append(name)
            cost = (usage["input"] / 1e6 * config.PRICE_IN_PER_MTOK
                    + usage["output"] / 1e6 * config.PRICE_OUT_PER_MTOK
                    + usage["searches"] * 0.01)
            total_cost += cost
            searches = f", {usage['searches']} searches" if usage["searches"] else ""
            print(f"  {name}: {len(jobs)} listings extracted "
                  f"({usage['input']:,} in / {usage['output']:,} out tokens{searches}, "
                  f"~${cost:.2f}) {note}")
        except Exception as e:
            failures.append(f"{name}: {e}")
            print(f"  {name}: FAILED - {e}")
    print(f"API cost this run: ~${total_cost:.2f}")
    return rows, ok, failures


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
    config.load_env()
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

    print("Fetching sources...")
    scraped, ok_sources, failures = scrape(board_counts)
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

    # Existing rows not seen this run: drop them but only if their sources were actually fetched
    dropped = 0
    for row in existing:
        if id(row) in seen_existing:
            continue
        sources = [s.strip() for s in row["source_site"].split(",")]
        if all(s in ok_sources for s in sources):
            dropped += 1
        else:
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
    print(f"Refresh: {today.isoformat()} (+{new_count} new, -{dropped} dropped, {n_pending} pending review)")


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
