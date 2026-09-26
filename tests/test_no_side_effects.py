"""The suite must not modify tracked files.

run.main() stamps a data version into index.html. A main() test that patched
DATA_JS_PATH but not INDEX_PATH rewrote the repo's real index.html with a temp
file's hash, which then shipped a version query that matched nothing.
"""

import hashlib
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHED = ("index.html", "data.js", "criminology_jobs.csv", "review.csv")


def _digests():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in WATCHED if (ROOT / name).exists()}


class NoSideEffectTests(unittest.TestCase):
    def test_running_the_suite_leaves_tracked_files_untouched(self):
        before = _digests()
        subprocess.run(
            ["python3", "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_[a-m]*.py"],
            cwd=ROOT, capture_output=True, timeout=300, check=False,
        )
        self.assertEqual(before, _digests())

    def test_index_version_matches_the_data_it_names(self):
        """A stale version query defeats the cache-busting it exists for."""
        import re
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        found = re.findall(r"data\.js\?v=([0-9a-f]+)", html)
        self.assertEqual(len(found), 1, "expected exactly one versioned reference")
        digest = hashlib.sha256((ROOT / "data.js").read_bytes()).hexdigest()[:12]
        self.assertEqual(found[0], digest)


if __name__ == "__main__":
    unittest.main()
