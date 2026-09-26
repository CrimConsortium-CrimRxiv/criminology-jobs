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

    def test_browse_pages_are_not_postings(self):
        """Search offered "/jobs/state/Massachusetts/" as a professorship's URL."""
        for url in ("https://careers.acjs.org/jobs/state/Massachusetts/",
                    "https://careers.acjs.org/jobs/",
                    "https://careers.acjs.org/jobs",
                    "https://www.higheredjobs.com/search",
                    "https://hrs.wsu.edu/careers",
                    "https://asc41.org/",
                    "https://example.org"):
            with self.subTest(url=url):
                self.assertEqual(extract._clean_urls([{"job_url": url}])[0]["job_url"], "")

    def test_detail_pages_that_merely_sit_under_search_survive(self):
        """HigherEdJobs serves real postings at /search/details.cfm, and TikTok
        at /search/<id>; a blunt "/search/" rule wrongly dropped 5 live rows."""
        for url in ("https://www.higheredjobs.com/search/details.cfm?JobCode=176574066",
                    "https://lifeattiktok.com/search/7556619364734208264"):
            with self.subTest(url=url):
                self.assertEqual(extract._clean_urls([{"job_url": url}])[0]["job_url"], url)

    def test_ats_links_that_name_the_job_in_the_query_survive(self):
        """A bare /jobs/ is a landing page, but /jobs/?ashby_jid=... is not."""
        for url in ("https://www.classdojo.com/jobs/?ashby_jid=050fee06",
                    "https://cantina.com/careers?ashby_jid=fb9a1183",
                    "https://boards.eu/careers?gh_jid=12345"):
            with self.subTest(url=url):
                self.assertEqual(extract._clean_urls([{"job_url": url}])[0]["job_url"], url)

    def test_good_urls_survive(self):
        for url in ("https://www.jobs.ac.uk/job/DSX170/senior-lecturer",
                    "http://example.edu/jobs/1",
                    "https://careers.acjs.org/job/x/86038559/",
                    "https://careers.acjs.org/jobs/view/assistant-professor-of-cj/",
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
