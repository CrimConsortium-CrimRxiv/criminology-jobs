import csv
import os
import tempfile
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
    def test_refresh_fails_before_scraping_when_api_key_is_missing(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(run.config, "load_env"),
            patch.object(run, "scrape") as scrape,
        ):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY is not set"):
                run.main()

        scrape.assert_not_called()

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
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
                patch.object(run, "CSV_PATH", csv_path),
                patch.object(run, "REVIEW_PATH", review_path),
                patch.object(run, "DATA_JS_PATH", data_js_path),
                patch.object(run.config, "load_env"),
                patch.object(run, "scrape", return_value=([new_job], [])),
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

    def test_failed_search_is_included_in_reported_api_cost(self):
        usage = {"input": 1_000, "output": 100, "searches": 1}
        sources = {
            "ProtectedBoard": {
                "urls": ["https://example.edu/jobs"],
                "kind": "claude_search",
            }
        }

        with (
            patch.object(run.config, "SOURCES", sources),
            patch.object(
                run.extract,
                "extract_jobs_via_search",
                return_value=([], usage),
            ),
            redirect_stdout(StringIO()) as output,
        ):
            rows, failures = run.scrape({"ProtectedBoard": 8})

        self.assertEqual(rows, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("sanity check", failures[0])
        self.assertIn("1 searches", output.getvalue())
        self.assertIn("API cost this run: ~$0.01", output.getvalue())


if __name__ == "__main__":
    unittest.main()
