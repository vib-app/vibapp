from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vibapp_daemon.core import DaemonError, RuntimeDaemon
from vibapp_daemon.service_executor import PROTOCOL_SCHEMA, ServiceExecutionError, ServiceWorker


def action_command() -> dict:
    return {"command": "ui-action", "session": "session-1", "surface": "surface-1",
            "route": "home", "action": "convert", "event_id": "event-1",
            "fields": [{"field": "input", "value": {"tag": "text", "value": "1" * 33}}]}


def rejection() -> dict:
    return {"schema_version": PROTOCOL_SCHEMA, "request_id": "rebound-by-test",
            "error": {"code": "invalid-argument", "message": "输入最多 32 个字符", "retryable": False},
            "ui_input_rejection": {"origin": "typed-guest-ui-action", "generation": "generation-1",
                                   "session": "session-1", "surface": "surface-1", "route": "home",
                                   "event_id": "event-1", "state_revision": 7}}


def worker_fixture(response: dict | None = None) -> ServiceWorker:
    # No process, filesystem, provider, guest, or notification is started.
    worker = object.__new__(ServiceWorker)
    worker.generation = "generation-1"
    worker._closed = False
    worker.process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), pid=-1, poll=lambda: None)
    worker.terminate = Mock(side_effect=lambda: setattr(worker, "_closed", True))

    def respond(_timeout: float) -> dict:
        result = copy.deepcopy(response if response is not None else rejection())
        result["request_id"] = json.loads(worker.process.stdin.getvalue().splitlines()[-1])["request_id"]
        return result

    worker._read_response = Mock(side_effect=respond)
    return worker


class ServiceWorkerInputRejectionTests(unittest.TestCase):
    def test_valid_host_marker_preserves_worker_and_public_typed_error(self) -> None:
        worker = worker_fixture()
        with self.assertRaises(ServiceExecutionError) as raised:
            worker.request(action_command(), timeout=1)
        self.assertEqual(raised.exception.ui_rejection_revision, 7)
        self.assertEqual(raised.exception.as_dict(), rejection()["error"])
        worker.terminate.assert_not_called()
        self.assertFalse(worker._closed)
        self.assertEqual(len(worker.process.stdin.getvalue().splitlines()), 1)

    def test_missing_or_forged_marker_and_non_input_errors_are_fatal(self) -> None:
        cases = []
        for key in rejection()["ui_input_rejection"]:
            document = rejection()
            document["ui_input_rejection"][key] = "different"
            cases.append(document)
        for revision in (True, False, 0, -1, 1.5, 2**64, None):
            document = rejection()
            document["ui_input_rejection"]["state_revision"] = revision
            cases.append(document)
        for code in ("internal", "resource-limit", "deadline-exceeded", "malformed-output", "stale-revision"):
            document = rejection()
            document["error"]["code"] = code
            cases.append(document)
        for marker in (None, False, "typed-guest-ui-action", {}):
            document = rejection()
            document["ui_input_rejection"] = marker
            cases.append(document)
        missing = rejection()
        del missing["ui_input_rejection"]
        missing["error"]["message"] = 'input rejected; ui_input_rejection={"state_revision":7}'
        cases.append(missing)
        for extra in ("outcome", "unknown"):
            document = rejection()
            document[extra] = {}
            cases.append(document)
        document = rejection()
        document["ui_input_rejection"]["unknown"] = True
        cases.append(document)
        document = rejection()
        document["error"]["retryable"] = "false"
        cases.append(document)
        document = rejection()
        document["error"]["unknown"] = True
        cases.append(document)
        for index, response in enumerate(cases):
            with self.subTest(index=index):
                worker = worker_fixture(response)
                with self.assertRaises(ServiceExecutionError) as raised:
                    worker.request(action_command(), timeout=1)
                self.assertIsNone(raised.exception.ui_rejection_revision)
                worker.terminate.assert_called_once()

    def test_marker_cannot_preserve_launch_refresh_or_service_operations(self) -> None:
        for command in ("ui-launch", "ui-refresh", "start", "trigger", "health", "migrate", "stop"):
            with self.subTest(command=command):
                worker = worker_fixture()
                value = action_command()
                value["command"] = command
                with self.assertRaises(ServiceExecutionError):
                    worker.request(value, timeout=1)
                worker.terminate.assert_called_once()

    def test_request_identity_mismatch_is_fatal_despite_marker(self) -> None:
        worker = worker_fixture()
        worker._read_response = Mock(return_value=rejection())
        with self.assertRaises(ServiceExecutionError) as raised:
            worker.request(action_command(), timeout=1)
        self.assertEqual(raised.exception.code, "forged-identifier")
        self.assertIsNone(raised.exception.ui_rejection_revision)
        worker.terminate.assert_called_once()

    def test_protocol_duplicate_nonfinite_or_malformed_json_is_fatal(self) -> None:
        encoded = json.dumps(rejection()).encode()
        lines = [b'{"schema_version":"wrong",' + encoded[1:] + b"\n",
                 encoded.replace(b'"state_revision": 7', b'"state_revision": 7, "state_revision": 7') + b"\n",
                 encoded.replace(b'"state_revision": 7', b'"state_revision": NaN') + b"\n",
                 b"{not json}\n"]
        for line in lines:
            with self.subTest(line=line):
                worker = worker_fixture()
                worker.process.stdout = io.BytesIO(line)
                with patch("vibapp_daemon.service_executor.selectors.DefaultSelector") as selector, \
                     patch("vibapp_daemon.service_executor._resident_bytes", return_value=None):
                    selector.return_value.select.return_value = [True]
                    with self.assertRaises(ServiceExecutionError) as raised:
                        ServiceWorker._read_response(worker, 1)
                self.assertEqual(raised.exception.code, "malformed-output")
                worker.terminate.assert_called_once()

    def test_timeout_and_resource_exhaustion_remain_fatal(self) -> None:
        for resident, timeout, expected in ((None, 0, "deadline-exceeded"), (2**40, 1, "resource-limit")):
            with self.subTest(expected=expected):
                worker = worker_fixture()
                with patch("vibapp_daemon.service_executor.selectors.DefaultSelector"), \
                     patch("vibapp_daemon.service_executor._resident_bytes", return_value=resident):
                    with self.assertRaises(ServiceExecutionError) as raised:
                        ServiceWorker._read_response(worker, timeout)
                self.assertEqual(raised.exception.code, expected)
                self.assertIsNone(raised.exception.ui_rejection_revision)
                worker.terminate.assert_called_once()


class RuntimeDaemonInputRejectionTests(unittest.TestCase):
    def setup_action(self, response: dict | None = None) -> tuple:
        worker = worker_fixture(response)
        value = action_command()
        del value["command"]
        value.update(entrypoint="main-ui", package_digest_sha256="a" * 64,
                     component_sha256="b" * 64, generation=worker.generation)
        surface = {key: value[key] for key in ("entrypoint", "package_digest_sha256", "component_sha256",
                                                "generation", "session", "route")}
        surface.update(state="open", render_feedback={"principal": "test-user"}, trusted_surface={"old": True})
        app = {"enabled": True, "package_digest_sha256": "a" * 64, "component_sha256": "b" * 64,
               "state": {"revision": 7}, "surfaces": {"surface-1": surface}}
        state = {"apps": {"ai.vibapp.test": app}}
        daemon = object.__new__(RuntimeDaemon)
        daemon._ui_workers = {("ai.vibapp.test", "surface-1"): worker}
        daemon._ui_refresh_at = {("ai.vibapp.test", "surface-1"): 10.0}
        daemon._rollback_observation_update = Mock()
        daemon._append_audit = Mock()
        daemon._trusted_ui_surface = Mock(return_value={"valid": True})
        daemon.clock = lambda: "2026-09-09T00:00:00Z"
        return daemon, state, value, worker

    def test_rejected_input_then_valid_input_reuses_exact_generation_surface_and_worker(self) -> None:
        daemon, state, value, worker = self.setup_action()
        before = copy.deepcopy(state)
        with self.assertRaises(DaemonError) as raised:
            daemon._ui_action(state, "ai.vibapp.test", value, "test-user")
        self.assertEqual(raised.exception.as_dict(), rejection()["error"])
        self.assertEqual(state, before)
        self.assertIs(daemon._ui_workers[("ai.vibapp.test", "surface-1")], worker)
        self.assertEqual(daemon._ui_refresh_at[("ai.vibapp.test", "surface-1")], 10.0)
        daemon._rollback_observation_update.assert_not_called()
        daemon._append_audit.assert_not_called()
        worker.terminate.assert_not_called()

        def valid_response(_timeout: float) -> dict:
            request_id = json.loads(worker.process.stdin.getvalue().splitlines()[-1])["request_id"]
            return {"schema_version": PROTOCOL_SCHEMA, "request_id": request_id,
                    "outcome": {"tag": "ui-updated", "state_revision": 8}}

        worker._read_response.side_effect = valid_response
        value["event_id"] = "event-2"
        value["fields"][0]["value"]["value"] = "1"
        result = daemon._ui_action(state, "ai.vibapp.test", value, "test-user")
        self.assertEqual(result["tag"], "ui-updated")
        self.assertEqual(result["value"]["generation"], "generation-1")
        self.assertEqual(result["value"]["surface"], "surface-1")
        self.assertEqual(state["apps"]["ai.vibapp.test"]["state"]["revision"], 8)
        self.assertIs(daemon._ui_workers[("ai.vibapp.test", "surface-1")], worker)
        worker.terminate.assert_not_called()
        self.assertEqual(len(worker.process.stdin.getvalue().splitlines()), 2, "no automatic replay")
        daemon._append_audit.assert_called_once()

    def test_unattested_error_or_changed_revision_closes_surface_and_rolls_back(self) -> None:
        cases = []
        for revision in (6, 8):
            document = rejection()
            document["ui_input_rejection"]["state_revision"] = revision
            cases.append(document)
        document = rejection()
        del document["ui_input_rejection"]
        cases.append(document)
        document = rejection()
        document["error"]["code"] = "internal"
        cases.append(document)
        for response in cases:
            with self.subTest(response=response):
                daemon, state, value, worker = self.setup_action(response)
                with self.assertRaises(DaemonError):
                    daemon._ui_action(state, "ai.vibapp.test", value, "test-user")
                self.assertTrue(worker._closed)
                self.assertNotIn(("ai.vibapp.test", "surface-1"), daemon._ui_workers)
                self.assertNotIn(("ai.vibapp.test", "surface-1"), daemon._ui_refresh_at)
                self.assertEqual(state["apps"]["ai.vibapp.test"]["surfaces"]["surface-1"]["state"], "closed")
                self.assertEqual(state["apps"]["ai.vibapp.test"]["state"]["revision"], 7)
                daemon._rollback_observation_update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
