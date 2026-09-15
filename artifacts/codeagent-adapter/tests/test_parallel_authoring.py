"""Host-only synthetic admission tests; no live author or credentials."""
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_codeagent_adapter as fixtures

adapter = fixtures.adapter
BASE = Path(__file__).resolve().parents[1]

CHILD = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import codeagent_adapter as adapter
adapter.host_budget._root = lambda kind: Path(sys.argv[2]) / kind
with adapter.one_job_lease(Path(sys.argv[2]) / sys.argv[4], budget_kind=sys.argv[3], parallel_authoring=True):
    print("admitted", flush=True)
    sys.stdin.readline()
'''


class ParallelAuthoringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vibapp-parallel-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(adapter.host_budget, "_root", side_effect=lambda kind: self.root / kind)
        patch.start()
        self.addCleanup(patch.stop)

    def hold_child(self, kind, output):
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(BASE), str(self.root), kind, output],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
        self.assertEqual(process.stdout.readline().strip(), "admitted")
        return process

    def test_two_authors_and_pipelines_admitted_across_processes_third_busy(self):
        for kind in ("pipeline", "codeagent"):
            with self.subTest(kind=kind):
                first = self.hold_child(kind, "first")
                second = self.hold_child(kind, "second")
                with self.assertRaises(adapter.AdapterError) as caught:
                    with adapter.one_job_lease(self.root / "third", budget_kind=kind, parallel_authoring=True):
                        self.fail("third author admitted")
                self.assertEqual(caught.exception.code, "local-capacity-busy")
                first.kill()
                first.wait(timeout=3)
                with adapter.one_job_lease(self.root / "third", budget_kind=kind, parallel_authoring=True):
                    self.assertIsNone(second.poll())
                second.stdin.write("release\n")
                second.stdin.flush()
                second.wait(timeout=3)
                self.assertEqual(second.returncode, 0, second.stderr.read())

    def test_legacy_remains_serial_and_mixed_callers_share_total_limit(self):
        for kind in ("pipeline", "codeagent"):
            with adapter.one_job_lease(self.root, budget_kind=kind):
                with self.assertRaises(adapter.AdapterError):
                    with adapter.one_job_lease(self.root / "legacy", budget_kind=kind):
                        self.fail("legacy concurrency widened")
                with adapter.one_job_lease(self.root / "docker", budget_kind=kind, parallel_authoring=True):
                    with self.assertRaises(adapter.AdapterError):
                        with adapter.one_job_lease(self.root / "third", budget_kind=kind, parallel_authoring=True):
                            self.fail("mixed capacity exceeded")

    def test_same_output_stays_exclusive_with_two_host_slots(self):
        with adapter.one_job_lease(self.root, parallel_authoring=True), adapter._attempt_lease(self.root):
            with adapter.one_job_lease(self.root, parallel_authoring=True):
                with self.assertRaises(adapter.AdapterError) as caught:
                    with adapter._attempt_lease(self.root):
                        self.fail("same output admitted twice")
        self.assertEqual(caught.exception.code, "job-already-running")

    def test_busy_direct_adapter_writes_no_status_and_consumes_no_consent(self):
        provider = fixtures.FixtureProvider()
        provider.parallel_authoring = True
        helper = fixtures.CodeAgentAdapterTests()
        task = helper.fresh_task()
        task_path, status_path = self.root / "task.json", self.root / "status.json"
        task_path.write_text(json.dumps(task))
        with adapter.one_job_lease(self.root, parallel_authoring=True), \
                adapter.one_job_lease(self.root, parallel_authoring=True), \
                mock.patch.object(fixtures.cloud_agent, "claim_consent") as claim:
            with self.assertRaises(adapter.AdapterError) as caught:
                adapter.execute_task(fixtures.cloud_agent, provider, task_path, self.root / "output", status_path,
                                     confirm_job=task["job_id"], confirm_consent=task["consent"]["consent_id"],
                                     acknowledge_external_cost=True)
        self.assertEqual(caught.exception.code, "local-capacity-busy")
        self.assertFalse(status_path.exists())
        self.assertFalse(provider.execute_called)
        claim.assert_not_called()

    def test_docker_preflight_reports_capacity_and_binds_budget_policy(self):
        sys.path.insert(0, str(BASE))
        from docker_provider import DockerCodexProvider
        provider = DockerCodexProvider(adapter, fixtures.cloud_agent, model="synthetic")
        image = {"version": "1.0.0", "image_id": "sha256:" + "a" * 64,
                 "bundle_sha256": "b" * 64, "bridge_sha256": "c" * 64, "policy_sha256": "d" * 64}
        connection = {"auth_kind": "api-key", "kind": "https", "endpoint": "https://synthetic.invalid/v1"}
        with mock.patch.object(provider, "configuration", return_value=(image, connection)):
            observed = provider.preflight()
            self.assertFalse(observed["one_job_per_host_user"])
            self.assertEqual(observed["maximum_jobs_per_host_user"], 2)
            self.assertEqual(observed["maximum_compilers_per_host_user"], 1)
            self.assertEqual(observed["docker_policy"]["host_budget_sha256"],
                             fixtures.cloud_agent.sha256_file(Path(adapter.host_budget.__file__)))
            with mock.patch.object(adapter.host_budget, "AUTHOR_LIMIT", 1):
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider.assert_execution_identity(observed["provider_execution_identity"])
        self.assertEqual(caught.exception.code, "provider-execution-identity-mismatch")


if __name__ == "__main__":
    unittest.main()
