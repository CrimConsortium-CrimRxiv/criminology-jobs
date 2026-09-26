import unittest
from unittest import mock

from scraper import fetch


class FetchTests(unittest.TestCase):
    def test_blocked_recognizes_cloudflare_challenge(self):
        body = b"<title>Just a moment...</title>" + b"x" * 5000
        self.assertTrue(fetch._blocked(body))

    def test_html_to_text_keeps_links_and_skips_scripts(self):
        html = """
        <html><body>
          <a href="/job/1">Role</a>
          <script>ignored()</script>
        </body></html>
        """

        text = fetch.html_to_text(html, "https://example.org/jobs/")

        self.assertIn("Role", text)
        self.assertIn("https://example.org/job/1", text)
        self.assertNotIn("ignored()", text)


class LivenessTests(unittest.TestCase):
    def test_dead_markers_cover_the_reported_case(self):
        """HigherEdJobs serves "Position Deleted on 1/02/2026" with HTTP 200, so
        a status check alone would keep it on the board forever."""
        self.assertIn("position deleted", fetch.DEAD_MARKERS)

    def test_per_host_budget_limits_requests(self):
        urls = [f"https://a.example/{i}" for i in range(5)] + ["https://b.example/1"]

        with mock.patch.object(fetch, "listing_state", return_value="live") as probe:
            states = fetch.listing_states(urls, workers=2, max_per_host=2)

        self.assertEqual(probe.call_count, 3)  # 2 from a.example, 1 from b.example
        self.assertEqual(len(states), 3)

    def test_requests_to_one_host_are_serialized(self):
        urls = [f"https://a.example/{i}" for i in range(3)]

        with (
            mock.patch.object(fetch, "listing_state", return_value="live"),
            mock.patch.object(fetch.time, "sleep") as slept,
        ):
            fetch.listing_states(urls, workers=4)

        self.assertEqual(slept.call_count, 2)  # paused between, not before, each


if __name__ == "__main__":
    unittest.main()