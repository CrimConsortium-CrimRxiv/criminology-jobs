import unittest

from scraper import extract


class ExtractTests(unittest.TestCase):
    def test_search_tools_use_haiku_compatible_direct_invocation(self):
        self.assertTrue(extract._SEARCH_TOOLS)
        for tool in extract._SEARCH_TOOLS:
            self.assertEqual(tool["allowed_callers"], ["direct"])


if __name__ == "__main__":
    unittest.main()
