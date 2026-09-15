"""Trusted connector tests; every credential/network/publication seam is mocked."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import release_pipeline as pipeline
import test_package_release as fixtures


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PackageReleaseTests()
        self.fixture.setUp()
        self.out = self.fixture.root / "job"
        self.out.mkdir()
        digest = self.fixture.record["package_digest_sha256"]
        destination = self.out / "pipeline/candidates" / digest
        destination.parent.mkdir(parents=True)
        self.fixture.candidate_dir.rename(destination)
        self.candidate = destination / "candidate.json"
        handoff = self.fixture.root / "input/handoff.json"
        self.state = {"schema_version": "vibapp.github-pipeline.v1", "state": "independently-verified",
            "request_id": "synthetic-request", "handoff": str(handoff), "handoff_sha256": pipeline.sha256(handoff.read_bytes()),
            "publisher_id": self.fixture.manifest["app"]["publisher"]["id"], "app_id": self.fixture.manifest["app"]["id"],
            "source": {"organization": "vib-app", "repository": self.fixture.source["repository"],
                "repository_id": self.fixture.source["repository_id"], "commit_sha": self.fixture.run["source_commit"],
                "source_digest_sha256": self.fixture.record["source_tree_sha256"], "stage": "untrusted-source-awaiting-builder"},
            "workflow_commit": self.fixture.run["workflow_commit"], "run_id": self.fixture.run["run_id"],
            "run_url": self.fixture.run["run_url"], "candidate": str(self.candidate)}
        self.save_state()
        self.receipt = {"state": "public-downloads-verified", "package_digest_sha256": digest,
            "release_url": "https://github.com/vib-app/packages/releases/tag/vibapp-package-" + digest,
            "asset_url": "https://github.com/vib-app/packages/releases/download/test/" + digest + ".zip",
            "torrent_url": "https://raw.githubusercontent.com/vib-app/packages/" + "c" * 40 + "/metadata/" + digest + ".torrent",
            "appstore_publication": "not-performed"}
        self.archive = MagicMock()
        self.archive.upload.return_value = copy.deepcopy(self.fixture.source)
        self.patches = [patch.object(pipeline.github_build, "resume", side_effect=lambda _: copy.deepcopy(self.state)),
                        patch.object(pipeline, "archiver", return_value=self.archive),
                        patch.object(pipeline.package_release, "publish_candidate", return_value=self.receipt)]
        self.resume, self.create_archiver, self.publish = [entry.start() for entry in self.patches]

    def tearDown(self):
        for entry in reversed(self.patches):
            entry.stop()
        self.fixture.tearDown()

    def save_state(self):
        (self.out / "state.json").write_bytes(pipeline.canonical(self.state))

    def test_actual_package_digest_postbuild_archive_and_bindings(self):
        self.assertEqual(pipeline.publish_job(self.out), self.receipt)
        self.resume.assert_called_once_with(self.out)
        self.archive.upload.assert_called_once_with(self.state["publisher_id"], self.state["app_id"],
            self.fixture.record["package_digest_sha256"], self.fixture.record["source_tree_sha256"],
            self.fixture.root / "input/source")
        self.publish.assert_called_once_with(self.candidate, self.fixture.source, self.fixture.run, self.out / "publication")
        self.assertEqual(pipeline.read_json(self.out / "source-archive-receipt.json"), self.fixture.source)
        self.assertEqual(pipeline.read_json(self.out / "release-receipt.json"), self.receipt)
        self.assertEqual(pipeline.read_json(self.out / "release-state.json")["state"], "public-downloads-verified")
        self.assertEqual(pipeline.read_json(self.out / "state.json"), self.state)

    def test_retry_always_reverifies_and_reads_back_archive(self):
        first = pipeline.publish_job(self.out)
        self.assertEqual(pipeline.publish_job(self.out), first)
        self.assertEqual(self.resume.call_count, 2)
        self.assertEqual(self.archive.upload.call_count, 2)
        self.assertEqual(self.publish.call_count, 2)

    def test_waiting_does_not_archive_or_publish(self):
        self.resume.side_effect = None
        self.resume.return_value = {"state": "waiting-for-actions"}
        self.assertEqual(pipeline.publish_job(self.out)["state"], "waiting-for-actions")
        self.create_archiver.assert_not_called()
        self.publish.assert_not_called()

    def test_resume_failure_no_publication(self):
        self.resume.side_effect = pipeline.SetupError("independent_verifier_failed")
        with self.assertRaisesRegex(pipeline.SetupError, "independent_verifier_failed"):
            pipeline.publish_job(self.out)
        self.create_archiver.assert_not_called()
        self.publish.assert_not_called()
        self.assertEqual(pipeline.read_json(self.out / "release-state.json")["state"], "failed")

    def test_changed_handoff_fails_before_resume(self):
        self.state["handoff_sha256"] = "0" * 64
        self.save_state()
        with self.assertRaisesRegex(pipeline.SetupError, "handoff_changed"):
            pipeline.publish_job(self.out)
        self.resume.assert_not_called()

    def test_candidate_publisher_mismatch_before_credentials(self):
        self.state["publisher_id"] = "publisher.other"
        self.state["source"]["repository"] = pipeline.repository_name(self.state["publisher_id"], self.state["app_id"])
        self.save_state()
        with self.assertRaisesRegex(pipeline.SetupError, "candidate_binding"):
            pipeline.publish_job(self.out)
        self.create_archiver.assert_not_called()

    def test_candidate_outside_ledger_path_rejected(self):
        outside = self.fixture.root / "outside-candidate"
        self.candidate.parent.rename(outside)
        self.state["candidate"] = str(outside / "candidate.json")
        self.save_state()
        with self.assertRaisesRegex(pipeline.SetupError, "candidate_path"):
            pipeline.publish_job(self.out)
        self.create_archiver.assert_not_called()

    def test_source_receipt_package_mismatch_never_publishes(self):
        self.archive.upload.return_value["package_digest_sha256"] = "0" * 64
        with self.assertRaisesRegex(pipeline.SetupError, "source_digest_mismatch"):
            pipeline.publish_job(self.out)
        self.publish.assert_not_called()
        self.assertFalse((self.out / "source-archive-receipt.json").exists())

    def test_archive_failure_no_publish_or_false_receipt(self):
        self.archive.upload.side_effect = pipeline.SetupError("archive_readback_mismatch")
        with self.assertRaisesRegex(pipeline.SetupError, "archive_readback_mismatch"):
            pipeline.publish_job(self.out)
        self.publish.assert_not_called()
        self.assertFalse((self.out / "release-receipt.json").exists())

    def test_failed_publication_retains_archive_but_no_release_receipt(self):
        self.publish.side_effect = pipeline.SetupError("package_raw_download_mismatch")
        with self.assertRaisesRegex(pipeline.SetupError, "package_raw_download_mismatch"):
            pipeline.publish_job(self.out)
        self.assertTrue((self.out / "source-archive-receipt.json").exists())
        self.assertFalse((self.out / "release-receipt.json").exists())

    def test_conflicting_archival_retry_is_not_overwritten(self):
        pipeline.publish_job(self.out)
        self.archive.upload.return_value["commit_sha"] = "d" * 40
        self.publish.reset_mock()
        with self.assertRaisesRegex(pipeline.SetupError, "receipt_conflict"):
            pipeline.publish_job(self.out)
        self.publish.assert_not_called()
        self.assertEqual(pipeline.read_json(self.out / "source-archive-receipt.json"), self.fixture.source)

    def test_cli_suppresses_sensitive_exception_text(self):
        self.resume.side_effect = RuntimeError("credential-canary-do-not-log")
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = pipeline.main(["publish", "--output", str(self.out)])
        self.assertEqual(code, 1)
        self.assertNotIn("credential-canary", output.getvalue())
        self.assertNotIn("credential-canary", (self.out / "release-state.json").read_text())

    def test_no_implicit_publish_subcommand(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                pipeline.main(["resume", "--output", str(self.out)])
        self.resume.assert_not_called()

    def test_unstarted_job_does_not_dispatch_as_publication(self):
        self.state["state"] = "source-staging"
        self.save_state()
        with self.assertRaisesRegex(pipeline.SetupError, "job_state"):
            pipeline.publish_job(self.out)
        self.resume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
