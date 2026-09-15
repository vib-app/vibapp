"""Offline controller wiring of host budgets and compiler cancellation."""
import fcntl
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

import test_delivery_controller as fixtures

delivery = fixtures.delivery


class ParallelDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.DeliveryControllerTests.setUpClass()
        self.helper = fixtures.DeliveryControllerTests()
        temporary = tempfile.TemporaryDirectory(prefix="vibapp-parallel-delivery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.helper.make_stage()
        self.stage.provider.parallel_authoring = True
        patch = mock.patch.object(self.stage.adapter.host_budget, "_root", side_effect=lambda kind: self.root / kind)
        patch.start()
        self.addCleanup(patch.stop)
        self.controller = delivery.DeliveryController(
            self.root / "product", self.stage, delivery.SafeFixtureRunner(self.helper.component.read_bytes()),
            wasm_tools=self.helper.wasm_tools)

    def submit(self):
        task = self.helper.fresh_task(self.stage, 1)
        return self.controller.submit(task, self.helper.registry(task, "parallel admission"), explicit_user_submit=True)

    def test_controller_allows_second_pipeline_but_leaves_third_attempt_queued(self):
        admitted = self.submit()
        with self.stage.adapter.one_job_lease(self.root, budget_kind="pipeline", parallel_authoring=True):
            with mock.patch.object(self.controller, "_run_attempt_under_budget", return_value={"entered": True}) as run:
                self.assertEqual(self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"]), {"entered": True})
                run.assert_called_once()
            with self.stage.adapter.one_job_lease(self.root, budget_kind="pipeline", parallel_authoring=True):
                with self.assertRaises(delivery.DeliveryError) as caught:
                    self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(caught.exception.code, "local-capacity-busy")
        attempt = self.controller._task_dir(admitted["task_id"]) / "attempts" / admitted["attempt_id"]
        self.assertEqual(delivery._load_json(attempt / "attempt.json", "attempt")["status"], "queued")
        self.assertFalse((attempt / "codeagent/status.json").exists())

    def test_codeagent_budget_busy_is_retryable_without_terminal_attempt(self):
        admitted = self.submit()
        with self.stage.adapter.one_job_lease(self.root, parallel_authoring=True), \
                self.stage.adapter.one_job_lease(self.root, parallel_authoring=True):
            with self.assertRaises(delivery.DeliveryError) as caught:
                self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(caught.exception.code, "local-capacity-busy")
        attempt = self.controller._task_dir(admitted["task_id"]) / "attempts" / admitted["attempt_id"]
        self.assertEqual(delivery._load_json(attempt / "attempt.json", "attempt")["status"], "queued")
        self.assertFalse((attempt / "codeagent/status.json").exists())
        # Same consent is still fresh and unconsumed; ordinary retry can finish.
        completed = self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(completed["status"], "private-appstore-ready")

    def test_same_attempt_still_exclusive_with_second_pipeline_available(self):
        admitted = self.submit()
        attempt = self.controller._task_dir(admitted["task_id"]) / "attempts" / admitted["attempt_id"]
        with (attempt / ".run.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(delivery.DeliveryError) as caught:
                self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(caught.exception.code, "attempt-active")

    def test_whole_attempt_signal_cancels_final_compiler_token_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        def body(*args, **kwargs):
            self.assertFalse(kwargs["cancellation"].is_set())
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertTrue(kwargs["cancellation"].is_set())
            return {"cancelled": True}
        with mock.patch.object(self.controller, "_run_attempt_under_budget", side_effect=body):
            self.assertEqual(self.controller.run_attempt("synthetic"), {"cancelled": True})
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_cancellation_at_compiler_boundary_does_not_start_verifier_or_ingest(self):
        admitted = self.submit()
        original_build = delivery.build_handoff
        def build(*args, **kwargs):
            self.assertIn("cancellation", kwargs)
            result = original_build(*args, **kwargs)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return result
        with mock.patch.object(delivery, "build_handoff", side_effect=build), \
                mock.patch.object(delivery, "verify_and_promote") as verify:
            result = self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "provider-cancelled")
        verify.assert_not_called()

    def test_author_cancellation_racing_success_reaches_enclosing_attempt(self):
        admitted = self.submit()
        original_execute = self.stage.provider.execute
        self.stage.provider.request_cancel = mock.Mock()
        def execute(**kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return original_execute(**kwargs)
        with mock.patch.object(self.stage.provider, "execute", side_effect=execute), \
                mock.patch.object(delivery, "build_handoff") as build:
            result = self.controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "provider-cancelled")
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
