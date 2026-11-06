"""Run the scraper: fetch -> extract -> merge -> write outputs.

Usage:  python -m scraper.run

Outputs: criminology_jobs.csv, data.js (site data), review.csv"""

import collections
import csv
import hashlib
import datetime
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import config, extract, fetch, proxy

ROOT = os.path.join(os.path.dirname(__file__), "..")
CSV_PATH = os.path.join(ROOT, "criminology_jobs.csv")
DATA_JS_PATH = os.path.join(ROOT, "data.js")
REVIEW_PATH = os.path.join(ROOT, "review.csv")
SUMMARY_PATH = os.path.join(ROOT, "refresh_summary.json")
INDEX_PATH = os.path.join(ROOT, "index.html")

COLUMNS = [
    "source_site", "job_title", "institution", "department_or_school",
    "country", "city_or_region", "rank_type", "area_specialization",
    "contract_type", "teaching_expectations", "research_expectations",
    "posted_date", "deadline_or_review_date", "salary_currency",
    "salary_range", "job_url", "combined_urls", "id", "consortium_member",
    "confidence", "last_probed",
]
# Named rather than sliced off COLUMNS: as a slice this silently gained an "id"
# column when COLUMNS grew.
_NOT_REVIEWED = ("id", "consortium_member", "confidence", "last_probed")
REVIEW_COLUMNS = ([c for c in COLUMNS if c not in _NOT_REVIEWED]
                  + ["confidence", "reason", "decision"])


@dataclass
class SourceResult:
    rows: list
    failure: str | None
    cost: float
    log: str


def require_api_key():
    """Load and validate the credential before doing any network or file work."""
    config.load_env()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it as a GitHub Actions "
            "repository secret or provide it in the local environment."
        )


def repair_urls(rows):
    """Drop stored URLs that never pointed anywhere.

    Rows written before job_url was validated can hold a URL the model composed
    rather than copied — id 874 carried
    "https://https//www.cech.uc.edu/..." — so a refresh repairs them in place
    instead of leaving a dead link on the site."""
    repaired = 0
    for row in rows:
        for field in ("job_url", "combined_urls"):
            kept = [u for u in (p.strip() for p in row.get(field, "").split(","))
                    if u and extract.valid_url(u)]
            original = row.get(field, "")
            row[field] = ", ".join(kept)
            if row[field] != original:
                repaired += 1
    return repaired


def prune_dead(rows, today):
    """Retire postings that are definitely gone, and unverifiable stale ones.

    The board used to be purely append-only, so it kept listings the boards had
    already taken down — a HigherEdJobs row still pointed at "Position Deleted
    on 1/02/2026". A row is only retired on a definite signal (404/410, or a
    dead marker on the page); anything we could not read is kept, so a board
    behind a bot wall never empties the board. Linkless rows are dropped before
    publish; the age-out below is only a backstop for a linkless row that is
    still in this list, retired after UNVERIFIABLE_MAX_AGE_DAYS.
    Returns (kept, retired) where each retired row carries a "retired_reason".
    """
    # Spend the per-host budget on the least recently probed postings, so a
    # board larger than the budget is worked through over successive runs
    # instead of re-checking the same head of the list every week.
    ordered = sorted(rows, key=lambda r: r.get("last_probed", ""))
    states = fetch.listing_states([r.get("job_url", "") for r in ordered],
                                  workers=config.LIVENESS_WORKERS,
                                  max_per_host=config.LIVENESS_MAX_PER_HOST)
    kept, retired = [], []
    for row in rows:
        url = row.get("job_url", "")
        state = states.get(url, "unprobed") if url else "no-url"
        if state != "unprobed":
            # Records the attempt, not the outcome: a host that refuses us
            # returns "unknown", and without marking those the rotation would
            # retry the same refused rows every run and never reach the rest.
            row["last_probed"] = today.isoformat()
        if state == "dead":
            retired.append({**row, "retired_reason": "listing gone"})
            continue
        age = _age_days(row.get("posted_date", ""), today)
        if state == "no-url":
            if age is not None and age > config.UNVERIFIABLE_MAX_AGE_DAYS:
                retired.append({**row, "retired_reason": "no url, too old",
                                "retired_detail": f"{age}d old"})
                continue
        elif (state != "live" and age is not None
                and age > config.MAX_AGE_DAYS):
            # Old and unconfirmed. Only a positive "live" keeps a posting this
            # old, because the hosts that will not answer a check are exactly
            # the ones whose stale rows would otherwise never leave the board.
            retired.append({**row, "retired_reason": "too old, not confirmed open",
                            "retired_detail": f"{age}d old"})
            continue
        kept.append(row)
    return kept, retired


def _age_days(posted_date, today):
    try:
        return (today - datetime.date.fromisoformat(posted_date)).days
    except ValueError:
        return None


def norm_key(row):
    """Match key: normalized title + institution, punctuation removed."""
    title = row.get("job_title", "")
    institution = row.get("institution", "")
    return re.sub(r"[^a-z0-9]", "", (title + institution).lower())


# Parenthetical / bracketed requisition ids, not specializations.
# "(AA28027)" and "(P10012987 / R0087910)" are codes; "(COURTS)" is not.
_CODE_GROUP = re.compile(r"[\(\[]([^()\[\]]*)[\)\]]")
_CODE_PART = re.compile(r"[A-Za-z]*\d[A-Za-z0-9-]*")
_TITLE_STOPWORDS = frozenset({
    "a", "an", "the", "of", "and", "or", "in", "for", "at", "to", "on",
})


def norm_institution(name):
    """Lowercase institution with punctuation collapsed to spaces.

    "Minnesota State University, Mankato" and "Minnesota State University
    Mankato" are the same campus.
    """
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _strip_job_codes(title):
    def repl(match):
        parts = [part.strip() for part in re.split(r"\s*/\s*|\s*,\s*", match.group(1)) if part.strip()]
        if parts and all(_CODE_PART.fullmatch(part) for part in parts):
            return " "
        return match.group(0)
    return _CODE_GROUP.sub(repl, title or "")


def title_tokens(title):
    """Order-independent title tokens, job codes removed.

    "(AA28027)" and a trailing requisition id do not make a second posting,
    and "Criminal Justice Assistant Professor" shares its tokens with
    "Assistant Professor, Criminal Justice".
    """
    text = re.sub(r"[^a-z0-9]+", " ", _strip_job_codes(title).lower())
    return frozenset(
        token for token in text.split()
        if token not in _TITLE_STOPWORDS and not _CODE_PART.fullmatch(token)
    )


def posting_key(row):
    """Fuzzy identity: same institution and the same title tokens.

    None when there is nothing to compare, so blank rows are not merged
    with each other.
    """
    institution = norm_institution(row.get("institution", ""))
    tokens = title_tokens(row.get("job_title", ""))
    if institution and tokens:
        return ("inst", institution, tokens)
    return None


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


def _search_with_retries(name, board_count):
    """Enumerate a board by search, retrying an empty result.

    One attempt is not evidence of an empty board: ACJS returned 25 listings on
    one attempt and 0 on the next with no code change. Attempts stop as soon as
    one returns any listings, so the usual case still costs a single call, and
    a short result is kept rather than measured against rows already stored.
    Returns (jobs, summed usage, attempts made)."""
    total = {"input": 0, "cached": 0, "output": 0, "searches": 0}
    best = []
    attempt = 0
    while attempt < config.SEARCH_ATTEMPTS:
        attempt += 1
        jobs, usage = extract.extract_jobs_via_search(
            name, config.SOURCES[name]["urls"][0], board_count,
            profile=config.SEARCH)
        for key in total:
            total[key] += usage.get(key, 0)
        if len(jobs) > len(best):
            best = jobs
        if best:
            break
    return best, total, attempt


def _scrape_source(name, board_count):
    """Fetch and extract one source without mutating shared scraper state."""
    started = time.monotonic()
    cost = 0.0
    usage_note = ""
    note = ""  # bound before the try so a failure log can still report the path
    try:
        try:
            profile = config.EXTRACT
            text, note = fetch.fetch_source(name)
            jobs, usage = extract.extract_jobs(name, text)
            if not jobs and config.SOURCES[name].get("kind") != "jmajax":
                # A page that yields no listings was not the listing page — a
                # bot-check interstitial that got past _blocked looks like this.
                # Treat it as a refused fetch so the proxy gets its turn.
                raise fetch.FetchError(
                    f"fetched {len(text):,} chars but found no listings")
        except fetch.FetchError as fetch_error:
            # A jmajax endpoint returns clean JSON; if that breaks we want the
            # failure, not a guess from somewhere else.
            if config.SOURCES[name].get("kind") == "jmajax":
                raise
            if proxy.available():
                # Perplexity's server-side fetch reaches pages our requests
                # cannot, so the page still goes through the normal extractor.
                hint = (f"The board is expected to list roughly {board_count} "
                        f"postings. ") if board_count else ""
                profile = config.PROXY
                text, usage = proxy.fetch_listing(
                    config.SOURCES[name]["urls"][0], hint)
                # Set before extraction so a later failure still names the path.
                note = (f"(fetch refused: {fetch_error}; "
                        f"proxy-fetched {len(text):,} chars)")
                jobs, extract_usage = extract.extract_jobs(name, text)
                for key in usage:
                    usage[key] += extract_usage.get(key, 0)
            else:
                profile = config.SEARCH
                note = f"(fetch refused: {fetch_error}; searched instead)"
                jobs, usage, attempts = _search_with_retries(name, board_count)
                if attempts > 1:
                    note = (f"(fetch refused: {fetch_error}; "
                            f"searched instead over {attempts} attempts)")
        cached = usage.get("cached", 0)  # discounted subset of usage["input"]
        cost = ((usage["input"] - cached) / 1e6 * profile["price_in"]
                + cached / 1e6 * profile["price_cached"]
                + usage["output"] / 1e6 * profile["price_out"]
                + usage["searches"] * (config.PROXY_SEARCH_COST
                                       if profile is config.PROXY
                                       else config.WEB_SEARCH_COST))
        searches = f", {usage['searches']} searches" if usage["searches"] else ""
        usage_note = (
            f" ({usage['input']:,} in / {usage['output']:,} out tokens"
            f"{searches}, ~${cost:.2f})"
        )
        for job in jobs:
            job["source_site"] = name
            job["confidence"] = f"{min(max(float(job['confidence']), 0.0), 0.99):.2f}"
        # After the empty-board check above: a page that yielded listings is
        # not a blocked fetch, but a listing with no http(s) URL is dropped
        # here so it never reaches publish or the review queue.
        jobs = extract.drop_linkless(jobs)
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
            f"  {name}: FAILED - {error}{usage_note} {note} [{elapsed:.1f}s]",
        )


def scrape(board_counts):
    """Fetch + extract sources concurrently. Returns (rows, failures, cost).

    board_counts: how many jobs the board currently lists per source. Passed to
    the proxy and search fallbacks as a thoroughness hint. A short or empty
    result is still returned; rows leave the board only through liveness and
    age checks.
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
    return rows, failures, total_cost


def _join_unique(values):
    return ", ".join(dict.fromkeys(v.strip() for v in values if v and str(v).strip()))


def _merge_group(group):
    """One posting seen on several boards: first row's fields, every source and URL."""
    merged = dict(group[0])
    merged["source_site"] = _join_unique(row.get("source_site", "") for row in group)
    urls = []
    for row in group:
        url = (row.get("job_url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    merged["combined_urls"] = ", ".join(urls)
    http_urls = [url for url in urls if extract.is_http_url(url)]
    merged["job_url"] = http_urls[0] if http_urls else (urls[0] if urls else "")
    merged["confidence"] = f"{max(float(row['confidence']) for row in group):.2f}"
    return merged


def dedup(rows):
    """Same job on several boards -> one row; merge sources + urls.

    Two rows are the same posting when the legacy punctuation-stripped
    title+institution key matches, or when the institutions match once
    commas and other punctuation are removed and the titles have the same
    tokens (job codes such as "(AA28027)" stripped, word order ignored).
    Different institutions never merge. A specialization left in parentheses,
    such as "(COURTS)" versus "(RACE & INEQUALITY)", keeps the titles apart.
    """
    if not rows:
        return []
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri == rj:
            return
        if ri < rj:
            parent[rj] = ri
        else:
            parent[ri] = rj

    buckets = {}
    for index, row in enumerate(rows):
        keys = []
        fuzzy = posting_key(row)
        if fuzzy:
            keys.append(fuzzy)
        legacy = norm_key(row)
        if legacy:
            keys.append(("legacy", legacy))
        for key in keys:
            if key in buckets:
                union(buckets[key], index)
            else:
                buckets[key] = index

    groups = {}
    order = []
    for index, row in enumerate(rows):
        root = find(index)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(row)
    return [_merge_group(groups[root]) for root in order]


def match_existing(row, by_url, by_posting, by_legacy):
    """Find the curated row this scrape is a duplicate of, if any."""
    for url in urls_of(row):
        if url in by_url:
            return by_url[url]
    fuzzy = posting_key(row)
    if fuzzy and fuzzy in by_posting:
        return by_posting[fuzzy]
    legacy = norm_key(row)
    if legacy and legacy in by_legacy:
        return by_legacy[legacy]
    return None


def main():
    require_api_key()
    today = datetime.date.today()

    existing = read_csv(CSV_PATH)
    repaired = repair_urls(existing)
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
    scraped, failures, total_cost = scrape(board_counts)
    scraped = dedup(scraped)
    # Merged rows can still be linkless when every copy lacked an http(s) URL.
    # Drop them before the publish/review decision so they never reach either.
    scraped = extract.drop_linkless(scraped)

    # Index existing rows for matching (by URL, then fuzzy title+institution,
    # then the legacy punctuation-stripped key).
    by_url = {url: row for row in existing for url in urls_of(row)}
    by_posting, by_legacy = {}, {}
    for row in existing:
        legacy = norm_key(row)
        if legacy:
            by_legacy[legacy] = row
        fuzzy = posting_key(row)
        if fuzzy:
            by_posting[fuzzy] = row
    max_id = max((int(row["id"]) for row in existing if row["id"].isdigit()), default=0)

    published, pending, seen_existing = [], [], set()
    new_jobs, unverified_jobs = [], []
    new_count = dropped_low = 0

    for row in scraped:
        match = match_existing(row, by_url, by_posting, by_legacy)
        if match is not None:
            # A curated row with no link must not shadow a scrape that has one,
            # or the write step would drop the only copy that can be published.
            if not extract.ensure_working_link(match) and extract.ensure_working_link(row):
                match["job_url"] = row["job_url"]
                match["combined_urls"] = row.get("combined_urls") or row["job_url"]
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
            new_jobs.append(row)
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
        unverified_jobs.append(row)

    # Retire postings the boards have taken down. Done after merging so a
    # posting re-seen in this run is checked too, and only on definite signals.
    retired = []
    if config.PRUNE_DEAD_LISTINGS:
        published, retired = prune_dead(published, today)

    published.sort(key=lambda r: (r.get("posted_date", ""), int(r["id"]) if r["id"].isdigit() else 0), reverse=True)

    published = write_outputs(published, today)
    write_csv(REVIEW_PATH, pending, REVIEW_COLUMNS)
    stamp_data_version(DATA_JS_PATH, INDEX_PATH)

    n_pending = sum(1 for p in pending if not p["decision"])
    write_summary({
        "run_date": today.isoformat(),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "published_total": len(published),
        "new_jobs": new_jobs,
        "verified_existing_count": len(seen_existing),
        "pending_review_count": n_pending,
        "dropped_low_confidence_count": dropped_low,
        "unverified_jobs": unverified_jobs,
        "retired_jobs": retired,
        "source_failures": failures,
        "estimated_api_cost_usd": total_cost,
    })

    for failure in failures:
        print(f"WARNING: {failure} (its existing jobs were kept)")
    if repaired:
        print(f"{repaired} stored URL field(s) repaired (unusable URLs cleared)")
    if retired:
        reasons = collections.Counter(r["retired_reason"] for r in retired)
        detail = ", ".join(f"{count} {reason}" for reason, count in reasons.most_common())
        print(f"{len(retired)} listings retired ({detail})")
    if dropped_low:
        print(f"{dropped_low} listings auto-dropped below CONFIDENCE_DROP={config.CONFIDENCE_DROP}")
    print(f"Refresh: {today.isoformat()} (+{new_count} new, {n_pending} pending review)")


def write_summary(summary):
    with open(SUMMARY_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def stamp_data_version(data_js_path, index_path):
    """Point index.html at data.js?v=<content hash>.

    index.html asked for a bare "data.js", which the CDN serves with
    max-age=600 and browsers hold onto for longer, so a refresh that removed a
    dead posting could still be showing it to a reader afterwards. Versioning
    the reference means a changed board is a changed URL. Returns the version,
    or None if nothing needed rewriting."""
    with open(data_js_path, "rb") as handle:
        version = hashlib.sha256(handle.read()).hexdigest()[:12]
    with open(index_path, encoding="utf-8") as handle:
        html = handle.read()
    updated = re.sub(r'<script src="data\.js(?:\?v=[0-9a-f]+)?"></script>',
                     f'<script src="data.js?v={version}"></script>', html)
    if updated == html:
        return None
    with open(index_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return version


def write_outputs(rows, today):
    """Write criminology_jobs.csv and data.js, never with a linkless row.

    This is the final gate. A row whose job_url and combined_urls are empty
    or not http(s) is dropped and logged here even if an earlier stage let
    it through.
    """
    rows = extract.drop_linkless(rows)
    write_csv(CSV_PATH, rows, COLUMNS)
    write_data_js(rows, today)
    return rows


def write_data_js(rows, today):
    rows = extract.drop_linkless(rows)
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
