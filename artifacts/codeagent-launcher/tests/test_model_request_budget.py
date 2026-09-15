"""Phase-aware request admission with synthetic HTTP only; no live calls."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_executor as docker
import test_gateway_recovery as recovery


class ModelRequestBudgetTests(unittest.TestCase):
    gateway = recovery.GatewayRecoveryTests.gateway
    message = recovery.GatewayRecoveryTests.message
    connection = recovery.GatewayRecoveryTests.connection
    cancellation = recovery.GatewayRecoveryTests.cancellation

    def test_authoring_cannot_consume_repair_reserve_or_increment_refused_request(self):
        gateway = self.gateway()
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=lambda *a, **k: self.connection(200)) as factory:
            for _ in range(40):
                gateway.relay(self.message(), lambda _: None, threading.Event())
            snapshot = gateway.budget_snapshot()
            self.assertEqual(snapshot, {
                "schema_version": "vibapp.model-request-budget-v1", "phase": "authoring",
                "total_limit": 48, "authoring_limit": 40, "repair_limit": 8,
                "total_used": 40, "authoring_used": 40, "repair_used": 0,
                "total_remaining": 8, "authoring_remaining": 0, "repair_remaining": 8,
            })
            with self.assertRaisesRegex(docker.DockerError, "provider-request-budget-exhausted"):
                gateway.relay(self.message(), lambda _: None, threading.Event())
        self.assertEqual(factory.call_count, 40)
        self.assertEqual((gateway.requests, gateway.logical_requests), (40, 40))
        self.assertEqual(gateway.budget_snapshot(), snapshot)
        for call in gateway.progress_callback.call_args_list:
            self.assertLessEqual(call.args[0]["model_requests"], 40)
        self.assertEqual(gateway.progress_callback.call_args.args[0]["model_request_budget"], snapshot)

    def test_repair_has_shared_hard_eight_call_cap_and_cannot_reset_phase(self):
        gateway = self.gateway()
        gateway.set_phase("repair")
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=lambda *a, **k: self.connection(200)) as factory:
            for index in range(8):
                if index == 4:
                    gateway.set_phase("repair")  # A second repair round does not replenish.
                gateway.relay(self.message(), lambda _: None, threading.Event())
            with self.assertRaisesRegex(docker.DockerError, "provider-request-budget-exhausted"):
                gateway.relay(self.message(), lambda _: None, threading.Event())
        self.assertEqual(factory.call_count, 8)
        snapshot = gateway.budget_snapshot()
        self.assertEqual(snapshot["repair_used"], 8)
        self.assertEqual(snapshot["repair_remaining"], 0)
        self.assertEqual(snapshot["total_remaining"], 40)
        with self.assertRaises(docker.DockerError):
            gateway.set_phase("authoring")
        with self.assertRaises(docker.DockerError):
            gateway.set_phase("unbounded")
        self.assertEqual(gateway.budget_snapshot(), snapshot)

    def test_retry_consumes_phase_and_total_capacity_without_spilling_into_reserve(self):
        gateway = self.gateway()
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=lambda *a, **k: self.connection(200)):
            for _ in range(39):
                gateway.relay(self.message(), lambda _: None, threading.Event())
        cancelled = self.cancellation()
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(502)) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                gateway.relay(self.message(), lambda _: None, cancelled)
        factory.assert_called_once()
        cancelled.wait.assert_not_called()
        self.assertEqual(gateway.budget_snapshot()["repair_used"], 0)
        gateway.set_phase("repair")
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=[self.connection(502), self.connection(200)]):
            gateway.relay(self.message(), lambda _: None, self.cancellation())
        self.assertEqual(gateway.budget_snapshot()["repair_used"], 2)
        self.assertEqual(gateway.budget_snapshot()["total_used"], 42)
        self.assertEqual(gateway.model_retries, 1)

    def test_invalid_and_cancelled_requests_do_not_claim_capacity_or_logical_calls(self):
        gateway = self.gateway()
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(docker.http.client, "HTTPSConnection") as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-cancelled"):
                gateway.relay(self.message(), lambda _: None, cancelled)
            with self.assertRaisesRegex(docker.DockerError, "provider-relay-request-invalid"):
                gateway.relay({"id": 1, "body": "!invalid"}, lambda _: None, threading.Event())
        factory.assert_not_called()
        self.assertEqual((gateway.requests, gateway.logical_requests), (0, 0))
        self.assertEqual(gateway.budget_snapshot()["total_used"], 0)
        self.assertNotIn("private", json.dumps(gateway.budget_snapshot()))

    def test_connect_failure_does_not_invent_model_post_but_uncertain_send_counts(self):
        gateway = self.gateway()
        connection = self.connection(200)
        connection.connect.side_effect = TimeoutError("PRIVATE_ERROR")
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(docker.DockerError, "provider-timeout"):
                gateway.relay(self.message(), lambda _: None, self.cancellation())
        connection.request.assert_not_called()
        self.assertEqual((gateway.requests, gateway.logical_requests), (0, 0))
        connection = self.connection(200)
        connection.request.side_effect = ConnectionResetError("PRIVATE_ERROR")
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(docker.DockerError, "provider-network-error"):
                gateway.relay(self.message(), lambda _: None, threading.Event())
        connection.request.assert_called_once()
        self.assertEqual((gateway.requests, gateway.logical_requests, gateway.model_retries), (1, 1, 0))
        self.assertNotIn("PRIVATE", json.dumps(gateway.request_history))

    def test_closed_budget_projection_rejects_inconsistent_counts_and_raw_fields(self):
        value = docker.budget_snapshot(authoring_used=3)
        self.assertEqual(docker.validate_model_request_budget(value, 3), value)
        for mutation in ({"total_used": 4}, {"authoring_remaining": 40}, {"repair_used": 1},
                         {"total_limit": 65}, {"total_used": True}, {"phase": "PRIVATE_ERROR"},
                         {"raw_prompt": "PRIVATE_PROMPT"}):
            with self.subTest(mutation=mutation), self.assertRaises(docker.DockerError):
                docker.validate_model_request_budget({**value, **mutation}, 3)
        with self.assertRaises(docker.DockerError):
            docker.validate_model_request_budget(value, 4)

    def test_optional_progress_disk_error_does_not_invent_a_call_or_network_failure(self):
        gateway = self.gateway()
        gateway.progress_callback = mock.Mock(side_effect=OSError("PRIVATE_DISK_ERROR"))
        connection = self.connection(200)
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            gateway.relay(self.message(), lambda _: None, threading.Event())
        connection.request.assert_called_once()
        self.assertEqual((gateway.requests, gateway.logical_requests), (1, 1))
        self.assertIsNone(gateway.last_error)
        self.assertNotIn("PRIVATE", json.dumps(gateway.request_history))


if __name__ == "__main__":
    unittest.main()
