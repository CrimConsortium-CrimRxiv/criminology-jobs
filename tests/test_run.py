import csv
import datetime
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scraper import run


def job(**overrides):
    row = {column: "" for column in run.COLUMNS}
    row.update(
        source_site="ACJS",
        job_title="Assistant Professor of Criminology",
        institution="Existing University",
        job_url="https://example.edu/existing",
        combined_urls="https://example.edu/existing",
        id="1",
        confidence="0.95",
    )
    row.update(overrides)
    return row


class RunTests(unittest.TestCase):
    def test_sources_are_scraped_concurrently_with_bounded_workers(self):
        sources = {
            "one": {"urls": ["https://example.edu/one"]},
            "two": {"urls": ["https://example.edu/two"]},
            "three": {"urls": ["https://example.edu/three"]},
        }
        barrier = threading.Barrier(3)

        def fake_scrape_source(name, board_count):
            barrier.wait(timeout=1)
            return run.SourceResult([], None, 0.0, f"  {name}: done")

        with (
            patch.object(run.config, "SOURCES", sources),
            patch.object(run.config, "SCRAPE_WORKERS", 3),
            patch.object(run, "_scrape_source", side_effect=fake_scrape_source),
            redirect_stdout(StringIO()),
        ):
            rows, failures, cost = run.scrape({})

        self.assertEqual(rows, [])
        self.assertEqual(failures, [])
        self.assertEqual(cost, 0.0)

    def test_a_refused_fetch_falls_back_to_search(self):
        usage = {"input": 0, "cached": 0, "output": 0, "searches": 0}
        blocked = run.fetch.FetchError("blocked/empty page")

        with (
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source", side_effect=blocked),
            patch.object(run.extract, "extract_jobs_via_search",
                         return_value=([], usage)) as searched,
        ):
            result = run._scrape_source("ASC", 0)

        self.assertIsNone(result.failure)
        self.assertEqual(searched.call_args.kwargs["profile"], run.config.SEARCH)
        self.assertIn("fetch refused", result.log)

    def test_a_working_fetch_never_pays_for_search(self):
        usage = {"input": 10, "cached": 0, "output": 5, "searches": 0}

        with (
            patch.object(run.fetch, "fetch_source", return_value=("page text", "")),
            patch.object(run.extract, "extract_jobs", return_value=([], usage)),
            patch.object(run.extract, "extract_jobs_via_search") as searched,
        ):
            result = run._scrape_source("ASC", 0)

        self.assertIsNone(result.failure)
        searched.assert_not_called()

    def test_jmajax_failure_is_not_papered_over_by_search(self):
        """TSPA returns clean JSON; if that breaks we want the failure."""
        with (
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("endpoint moved")),
            patch.object(run.extract, "extract_jobs_via_search") as searched,
        ):
            result = run._scrape_source("TSPA", 0)

        self.assertIn("endpoint moved", result.failure)
        searched.assert_not_called()

    def test_search_fallback_still_enforces_the_count_floor(self):
        usage = {"input": 0, "cached": 0, "output": 0, "searches": 3}

        with (
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.extract, "extract_jobs_via_search",
                         return_value=([], usage)),
        ):
            result = run._scrape_source("ACJS", 40)

        self.assertIn("sanity check", result.failure)

    def test_all_profiles_use_the_cheap_luna_model(self):
        for profile in (run.config.EXTRACT, run.config.SEARCH):
            self.assertEqual(profile["model"], "gpt-6-luna")
            self.assertLess(profile["price_cached"], profile["price_in"])
            self.assertLess(profile["price_in"], profile["price_out"])

    def test_search_runs_at_high_effort(self):
        """At medium effort the model abandons the sweep and returns no jobs."""
        self.assertEqual(run.config.SEARCH["effort"], "high")

    def test_refresh_fails_before_scraping_when_api_key_is_missing(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(run.config, "load_env"),
            patch.object(run, "scrape") as scrape,
        ):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY is not set"):
                run.main()

        scrape.assert_not_called()

    def test_only_definite_signals_retire_a_posting(self):
        """A board we cannot read must never empty the board."""
        rows = [
            {"id": "1", "job_url": "https://b.example/live", "posted_date": "2026-09-01"},
            {"id": "2", "job_url": "https://b.example/gone", "posted_date": "2026-09-01"},
            {"id": "3", "job_url": "https://b.example/walled", "posted_date": "2026-09-01"},
            {"id": "4", "job_url": "https://b.example/skipped", "posted_date": "2026-09-01"},
        ]
        states = {"https://b.example/live": "live", "https://b.example/gone": "dead",
                  "https://b.example/walled": "unknown"}  # /skipped past the budget

        with patch.object(run.fetch, "listing_states", return_value=states):
            kept, retired = run.prune_dead(rows, datetime.date(2026, 9, 26))

        self.assertEqual([r["id"] for r in retired], ["2"])
        self.assertEqual([r["id"] for r in kept], ["1", "3", "4"])
        self.assertEqual(retired[0]["retired_reason"], "listing gone")

    def test_checked_rows_record_the_date_so_budget_rotates(self):
        rows = [{"id": "1", "job_url": "https://b.example/live", "posted_date": "2026-09-01"},
                {"id": "2", "job_url": "https://b.example/unchecked", "posted_date": "2026-09-01"}]

        with patch.object(run.fetch, "listing_states",
                          return_value={"https://b.example/live": "live"}):
            kept, _ = run.prune_dead(rows, datetime.date(2026, 9, 26))

        self.assertEqual(kept[0]["last_checked"], "2026-09-26")
        self.assertNotIn("last_checked", kept[1])  # untouched, so checked first next run

    def test_least_recently_checked_are_checked_first(self):
        rows = [{"id": "new", "job_url": "https://b.example/1", "last_checked": "2026-09-25"},
                {"id": "old", "job_url": "https://b.example/2", "last_checked": "2026-01-01"},
                {"id": "never", "job_url": "https://b.example/3"}]

        with patch.object(run.fetch, "listing_states", return_value={}) as probe:
            run.prune_dead(rows, datetime.date(2026, 9, 26))

        self.assertEqual(probe.call_args.args[0],
                         ["https://b.example/3", "https://b.example/2", "https://b.example/1"])
        self.assertEqual(probe.call_args.kwargs["max_per_host"],
                         run.config.LIVENESS_MAX_PER_HOST)

    def test_unverifiable_rows_age_out(self):
        rows = [{"id": "fresh", "job_url": "", "posted_date": "2026-09-01"},
                {"id": "stale", "job_url": "", "posted_date": "2025-01-01"}]

        with patch.object(run.fetch, "listing_states", return_value={}):
            kept, retired = run.prune_dead(rows, datetime.date(2026, 9, 26))

        self.assertEqual([r["id"] for r in kept], ["fresh"])
        self.assertIn("no url", retired[0]["retired_reason"])

    def test_review_columns_do_not_leak_internal_fields(self):
        for field in ("id", "consortium_member", "last_checked"):
            self.assertNotIn(field, run.REVIEW_COLUMNS)

    def test_stored_unusable_urls_are_repaired(self):
        """The live board carried id 874 with a URL the model composed:
        "https://https//www.cech.uc.edu/..." — a host literally named "https"."""
        rows = [{
            "job_url": "https://https//www.cech.uc.edu/Academics/school.html",
            "combined_urls": ("https://https//www.cech.uc.edu/Academics/school.html, "
                              "https://asc41.org/real-posting/"),
        }]

        repaired = run.repair_urls(rows)

        self.assertEqual(rows[0]["job_url"], "")
        self.assertEqual(rows[0]["combined_urls"], "https://asc41.org/real-posting/")
        self.assertEqual(repaired, 2)

    def test_repair_leaves_good_rows_alone(self):
        rows = [{"job_url": "https://asc41.org/x/", "combined_urls": "https://asc41.org/x/"}]
        self.assertEqual(run.repair_urls(rows), 0)
        self.assertEqual(rows[0]["job_url"], "https://asc41.org/x/")

    def test_dedup_combines_sources_urls_and_uses_highest_confidence(self):
        first = job()
        second = job(
            source_site="ASC",
            job_url="https://example.edu/duplicate",
            combined_urls="",
            confidence="0.80",
        )

        [merged] = run.dedup([first, second])

        self.assertEqual(merged["source_site"], "ACJS, ASC")
        self.assertEqual(
            merged["combined_urls"],
            "https://example.edu/existing, https://example.edu/duplicate",
        )
        self.assertEqual(merged["confidence"], "0.95")

    def test_refresh_is_append_only_when_existing_job_is_not_scraped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "criminology_jobs.csv"
            review_path = root / "review.csv"
            data_js_path = root / "data.js"
            summary_path = root / "refresh_summary.json"
            run.write_csv(csv_path, [job()], run.COLUMNS)

            new_job = job(
                source_site="ASC",
                job_title="New Research Fellow",
                institution="New University",
                job_url="https://example.edu/new",
                combined_urls="",
                id="",
            )

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                patch.object(run, "CSV_PATH", csv_path),
                patch.object(run, "REVIEW_PATH", review_path),
                patch.object(run, "DATA_JS_PATH", data_js_path),
                patch.object(run, "SUMMARY_PATH", summary_path),
                patch.object(run.config, "load_env"),
                patch.object(run.fetch, "listing_states", return_value={}),
                patch.object(run, "scrape", return_value=([new_job], [], 0.12)),
                redirect_stdout(StringIO()) as output,
            ):
                run.main()

            with csv_path.open(encoding="utf-8", newline="") as handle:
                refreshed = list(csv.DictReader(handle))

            self.assertEqual(len(refreshed), 2)
            self.assertEqual(
                {row["job_url"] for row in refreshed},
                {"https://example.edu/existing", "https://example.edu/new"},
            )
            self.assertIn("+1 new, 0 pending review", output.getvalue())
            self.assertNotIn("dropped", output.getvalue())
            with summary_path.open(encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual([row["job_url"] for row in summary["new_jobs"]], ["https://example.edu/new"])
            self.assertEqual(
                [row["job_url"] for row in summary["unverified_jobs"]],
                ["https://example.edu/existing"],
            )
            self.assertEqual(summary["estimated_api_cost_usd"], 0.12)

    def test_a_refused_fetch_prefers_the_proxy_over_search(self):
        """Proxy fetch returns the real page, so it beats stochastic search."""
        usage = {"input": 10, "cached": 0, "output": 5, "searches": 1}

        with (
            patch.object(run.proxy, "available", return_value=True),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.proxy, "fetch_listing",
                         return_value=("Lecturer [https://acjs.org/job/1]", usage)),
            patch.object(run.extract, "extract_jobs",
                         return_value=([{"job_url": "", "confidence": "0.9"}], usage)),
            patch.object(run.extract, "extract_jobs_via_search") as searched,
        ):
            result = run._scrape_source("ACJS", 0)

        self.assertIsNone(result.failure)
        searched.assert_not_called()
        self.assertIn("proxy-fetched", result.log)

    def test_a_thin_proxy_fetch_is_sanity_checked_too(self):
        """A proxy fetch is still a model reading a page we could not get."""
        usage = {"input": 0, "cached": 0, "output": 0, "searches": 0}

        with (
            patch.object(run.proxy, "available", return_value=True),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.proxy, "fetch_listing", return_value=("t", dict(usage))),
            patch.object(run.extract, "extract_jobs",
                         return_value=([{"job_url": "", "confidence": "0.9"}], usage)),
        ):
            result = run._scrape_source("ACJS", 60)

        self.assertIn("sanity check", result.failure)

    def test_proxy_billing_uses_the_perplexity_search_rate(self):
        """Perplexity bills search separately from OpenAI's $10/1k."""
        usage = {"input": 0, "cached": 0, "output": 0, "searches": 10}

        with (
            patch.object(run.proxy, "available", return_value=True),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.proxy, "fetch_listing", return_value=("text", dict(usage))),
            patch.object(run.extract, "extract_jobs",
                         return_value=([{"job_url": "", "confidence": "0.9"}],
                                       {"input": 0, "cached": 0, "output": 0, "searches": 0})),
        ):
            result = run._scrape_source("ACJS", 0)

        self.assertAlmostEqual(result.cost, 10 * run.config.PROXY_SEARCH_COST, places=6)

    def test_a_thin_search_is_retried_and_the_best_attempt_kept(self):
        """One attempt is not evidence of an empty board."""
        thin = ([{"job_url": "", "confidence": "0.9"}], {"input": 1, "cached": 0,
                                                        "output": 1, "searches": 1})
        full = ([{"job_url": "", "confidence": "0.9"} for _ in range(9)],
                {"input": 1, "cached": 0, "output": 1, "searches": 1})

        with (
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.extract, "extract_jobs_via_search",
                         side_effect=[thin, full]) as searched,
        ):
            result = run._scrape_source("ACJS", 10)

        self.assertIsNone(result.failure)          # floor is 5, second attempt clears it
        self.assertEqual(len(result.rows), 9)      # best attempt kept
        self.assertEqual(searched.call_count, 2)   # stopped as soon as it cleared
        self.assertIn("2 attempts", result.log)

    def test_a_search_that_clears_the_floor_is_not_retried(self):
        good = ([{"job_url": "", "confidence": "0.9"} for _ in range(9)],
                {"input": 1, "cached": 0, "output": 1, "searches": 1})

        with (
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.extract, "extract_jobs_via_search",
                         return_value=good) as searched,
        ):
            result = run._scrape_source("ACJS", 10)

        self.assertEqual(searched.call_count, 1)
        self.assertNotIn("attempts", result.log)

    def test_retries_stop_at_the_cap_and_sum_their_cost(self):
        thin = ([], {"input": 1_000, "cached": 0, "output": 100, "searches": 1})

        with (
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(run.extract, "extract_jobs_via_search",
                         return_value=thin) as searched,
        ):
            result = run._scrape_source("ACJS", 10)

        self.assertEqual(searched.call_count, run.config.SEARCH_ATTEMPTS)
        self.assertIn("sanity check", result.failure)
        # every attempt is billed, so every attempt is reported
        self.assertGreater(result.cost, run.config.SEARCH_ATTEMPTS * 0.009)

    def test_failed_search_is_included_in_reported_api_cost(self):
        usage = {"input": 1_000, "cached": 400, "output": 100, "searches": 1}
        sources = {
            "ProtectedBoard": {
                "urls": ["https://example.edu/jobs"],
            }
        }

        with (
            patch.object(run.config, "SOURCES", sources),
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.proxy, "available", return_value=False),
            patch.object(run.fetch, "fetch_source",
                         side_effect=run.fetch.FetchError("blocked")),
            patch.object(
                run.extract,
                "extract_jobs_via_search",
                return_value=([], usage),
            ),
            redirect_stdout(StringIO()) as output,
        ):
            rows, failures, cost = run.scrape({"ProtectedBoard": 8})

        self.assertEqual(rows, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("sanity check", failures[0])
        # one search per attempt, all attempts billed
        self.assertIn(f"{run.config.SEARCH_ATTEMPTS} searches", output.getvalue())
        self.assertIn(f"API cost this run: ~${0.01 * run.config.SEARCH_ATTEMPTS:.2f}",
                      output.getvalue())
        self.assertGreater(cost, 0.01)


if __name__ == "__main__":
    unittest.main()
