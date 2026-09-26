import unittest

from scraper import config, extract


class ExtractTests(unittest.TestCase):
    def test_search_tool_asks_for_an_unlimited_token_budget(self):
        """Under the default budget the model runs out of room mid-enumeration
        and returns no jobs at all for the bot-walled boards."""
        [tool] = extract._SEARCH_TOOLS
        self.assertEqual(tool["type"], "web_search")
        self.assertEqual(tool["return_token_budget"], "unlimited")
        self.assertEqual(tool["search_context_size"], "high")

    def test_model_composed_url_is_rejected(self):
        """The real regression: a posting whose text held the typo
        "https//www.cech.uc.edu/..." came back as "https://https//www.cech..." —
        a link to a host literally named "https"."""
        jobs = [{"job_url": "https://https//www.cech.uc.edu/Academics/school.html"}]
        self.assertEqual(extract._clean_urls(jobs)[0]["job_url"], "")

    def test_malformed_urls_are_blanked(self):
        for url in ("", "   ", "notaurl", "ftp://example.com/x", "https://nodot/x",
                    "https://example.com/a b"):
            with self.subTest(url=url):
                self.assertEqual(extract._clean_urls([{"job_url": url}])[0]["job_url"], "")

    def test_good_urls_survive(self):
        for url in ("https://www.jobs.ac.uk/job/DSX170/senior-lecturer",
                    "http://example.edu/jobs/1",
                    "https://careers.acjs.org/job/x/86038559/",
                    "https://httpsolutions.example.com/jobs/1"):
            with self.subTest(url=url):
                self.assertEqual(extract._clean_urls([{"job_url": url}])[0]["job_url"], url)

    def test_fetched_page_urls_must_appear_in_the_page(self):
        """On the fetched-page path the only URLs we can vouch for are the ones
        the page actually contained."""
        page = "Lecturer post [https://example.edu/jobs/real] apply now"
        jobs = [{"job_url": "https://example.edu/jobs/real"},
                {"job_url": "https://example.edu/jobs/invented"}]
        urls = [j["job_url"] for j in extract._clean_urls(jobs, page_text=page)]
        self.assertEqual(urls, ["https://example.edu/jobs/real", ""])

    def test_no_source_is_hardcoded_to_search(self):
        """Search is a fallback for a refused fetch, not a per-source setting."""
        for name, source in config.SOURCES.items():
            self.assertIn(source.get("kind"), (None, "jmajax"), name)

if __name__ == "__main__":
    unittest.main()
