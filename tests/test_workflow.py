import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "refresh.yml"
).read_text(encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def test_pages_build_is_requested_only_after_a_generated_commit(self):
        self.assertIn("pages: write", WORKFLOW)
        self.assertIn('echo "changed=true" >> "$GITHUB_OUTPUT"', WORKFLOW)
        self.assertIn("if: steps.publish.outputs.changed == 'true'", WORKFLOW)
        self.assertIn('repos/${GITHUB_REPOSITORY}/pages/builds', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
