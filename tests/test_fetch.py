import unittest

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


if __name__ == "__main__":
    unittest.main()
