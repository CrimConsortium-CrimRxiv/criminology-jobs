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
    def test_challenge_pages_are_detected_despite_being_large(self):
        """A 92KB Incapsula interstitial passed the size check and cost 41k
        input tokens before returning zero listings."""
        for marker in (b"Incapsula", b"JavaScript is required",
                       b"Enable JavaScript and cookies to continue",
                       b"cf-browser-verification"):
            with self.subTest(marker=marker):
                page = b"<html>" + b"x" * 90_000 + marker + b"</html>"
                self.assertTrue(fetch._blocked(page))

    def test_a_real_page_is_not_flagged(self):
        self.assertFalse(fetch._blocked(b"<html>" + b"job listing " * 500 + b"</html>"))

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

    def test_a_refusing_host_is_abandoned_for_this_run(self):
        """higheredjobs.com answered 20 of 154 checks then bot-checked the rest;
        pressing on only deepens the block."""
        urls = [f"https://a.example/{i}" for i in range(40)]

        with (
            mock.patch.object(fetch, "listing_state", return_value="unknown") as probe,
            mock.patch.object(fetch.time, "sleep"),
        ):
            fetch.listing_states(urls, workers=1)

        self.assertEqual(probe.call_count, fetch.HOST_UNKNOWN_STREAK)

    def test_an_answering_host_is_checked_all_the_way(self):
        urls = [f"https://a.example/{i}" for i in range(12)]
        answers = ["live", "unknown", "dead", "unknown", "unknown", "live"] * 2

        with (
            mock.patch.object(fetch, "listing_state", side_effect=answers) as probe,
            mock.patch.object(fetch.time, "sleep"),
        ):
            states = fetch.listing_states(urls, workers=1)

        self.assertEqual(probe.call_count, 12)  # streak keeps resetting
        self.assertEqual(len(states), 12)

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