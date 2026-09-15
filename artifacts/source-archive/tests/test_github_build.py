import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import ci_builder as ci
import github_build as build
import github_smoke
from github_archive import SetupError, source_digest


class CloudBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.handoff = github_smoke.create(self.root / "input")
        self.source = self.handoff.parent / "source"
        self.digest = json.loads(self.handoff.read_text())["source_tree_sha256"]

    def tearDown(self):
        self.temp.cleanup()

    def test_real_smoke_source_obeys_both_policies(self):
        _, package = ci.validated_source(self.source, self.digest)
        self.assertEqual(package["name"], "ai_vibapp_archive-check")

    def test_source_changes_rejected(self):
        (self.source / "src/lib.rs").write_text("bad")
        with self.assertRaisesRegex(ValueError, "digest"):
            ci.validated_source(self.source, self.digest)

    def test_build_script_rejected(self):
        (self.source / "build.rs").write_text("fn main() {}")
        with self.assertRaisesRegex(ValueError, "path"):
            ci.validated_source(self.source, self.digest)

    def test_link_rejected(self):
        (self.source / "src/alias.rs").symlink_to(self.source / "src/lib.rs")
        with self.assertRaisesRegex(ValueError, "link"):
            ci.validated_source(self.source, self.digest)

    def test_untrusted_workflow_rejected(self):
        directory = self.source / ".github/workflows"
        directory.mkdir(parents=True)
        (directory / "run.yml").write_text("run: arbitrary")
        with self.assertRaisesRegex(ValueError, "path"):
            ci.validated_source(self.source, self.digest)

    def test_ambient_cargo_config_rejected(self):
        (self.source / ".cargo").mkdir()
        (self.source / ".cargo/config.toml").write_text('[build]\nrustc="malicious"')
        with self.assertRaisesRegex(ValueError, "path"):
            ci.validated_source(self.source, self.digest)

    def test_operator_api_scope(self):
        with self.assertRaises(SetupError):
            build.build_api({"source": {"repository": "packages"}, "publisher_id": "publisher.test", "app_id": "ai.vibapp.test"})

    def test_workflow_no_publish_secret(self):
        self.assertNotIn("secrets.", build.WORKFLOW)
        self.assertNotIn("contents: write", build.WORKFLOW)
        self.assertEqual(build.WORKFLOW.count("persist-credentials: false"), 2)
        self.assertIn("timeout-minutes: 20", build.WORKFLOW)

    def test_output_cap(self):
        with self.assertRaisesRegex(ValueError, "output-limit"):
            ci.bounded([sys.executable, "-c", "print('x'*10000)"], 100, 5)

    def test_workflow_wrong_commit_rejected(self):
        state = {"source": {"repository": "app-" + "a"*64}, "run_id": 1,
                 "workflow_commit": "a"*40, "request_id": "request"}
        from unittest.mock import MagicMock
        api = MagicMock()
        api.request.return_value = {"head_sha": "b"*40}
        with patch.object(build, "build_api", return_value=api):
            with self.assertRaisesRegex(SetupError, "workflow_identity"):
                build.get_run(state)


if __name__ == "__main__":
    unittest.main()
