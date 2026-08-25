import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.js").read_text(encoding="utf-8")


class CompiledDateTests(unittest.TestCase):
    def test_all_visible_update_dates_use_generated_compiled_date(self):
        self.assertEqual(INDEX.count("data-compiled-date"), 3)
        self.assertIn('querySelectorAll("[data-compiled-date]")', APP)
        self.assertNotIn("Last updated August", INDEX)
        self.assertNotIn("Aggregated August", INDEX)


if __name__ == "__main__":
    unittest.main()
