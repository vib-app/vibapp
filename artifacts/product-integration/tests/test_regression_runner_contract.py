"""Prevent product-lesson regressions from silently falling out of the main suite."""
from pathlib import Path
import unittest


class ProductRegressionRunnerTests(unittest.TestCase):
    def test_main_runner_includes_launch_admission_and_workflow_regressions(self):
        script = Path(__file__).resolve().parents[1] / "run_full_regression.sh"
        lines = {line.strip() for line in script.read_text().splitlines()}
        for suite in ("artifacts/cloud-agent/tests", "artifacts/codeagent-launcher/tests",
                      "artifacts/codeagent-adapter/tests", "artifacts/orchestrator/tests",
                      "artifacts/product-integration/tests"):
            self.assertIn(f"run_python_suite {suite}", lines)
        self.assertIn('node --test "$repo_root/artifacts/desktop/ui/tests/app-ui.test.mjs"', lines)


if __name__ == "__main__":
    unittest.main()
