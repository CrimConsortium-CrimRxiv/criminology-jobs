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

    def test_off_domain_urls_are_blanked(self):
        """The same posting shows up on mirror and aggregator sites; a wrong
        link is worse than none."""
        jobs = [
            {"job_url": "https://www.higheredmilitary.com/jobs/details.cfm?JobCode=1"},
            {"job_url": "https://www.higheredjobs.com/faculty/details.cfm?JobCode=2"},
            {"job_url": "https://higheredjobs.com/x"},
            {"job_url": "https://nothigheredjobs.com/x"},
            {"job_url": ""},
        ]
        urls = [j["job_url"] for j in extract._keep_own_urls(jobs, "higheredjobs.com")]
        self.assertEqual(urls, [
            "",
            "https://www.higheredjobs.com/faculty/details.cfm?JobCode=2",
            "https://higheredjobs.com/x",
            "",
            "",
        ])

    def test_urls_are_untouched_when_no_domain_is_given(self):
        jobs = [{"job_url": "https://anywhere.example/x"}]
        self.assertEqual(extract._keep_own_urls(jobs, None)[0]["job_url"],
                         "https://anywhere.example/x")

    def test_every_search_source_declares_a_domain(self):
        for name, source in config.SOURCES.items():
            if source.get("kind") == "model_search":
                self.assertTrue(source.get("search_domain"), name)

    def test_schema_is_strict_compatible(self):
        """Strict json_schema requires every property listed in `required`
        and additionalProperties disabled at every level."""
        self.assertFalse(extract._SCHEMA["additionalProperties"])
        self.assertEqual(extract._SCHEMA["required"], ["jobs"])
        items = extract._SCHEMA["properties"]["jobs"]["items"]
        self.assertFalse(items["additionalProperties"])
        self.assertEqual(sorted(items["required"]), sorted(items["properties"]))


if __name__ == "__main__":
    unittest.main()
