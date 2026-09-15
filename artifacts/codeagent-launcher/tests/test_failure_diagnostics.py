"""Offline regressions for closed diagnostics; never start Docker or a provider."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_executor as docker


def diagnostic(**updates):
    return {
        "schema_version": "vibapp.docker-failure-diagnostic-v1",
        "failure_origin": "provider-process",
        "provider_error_category": "unknown",
        "child_exit_code": None,
        "child_signal": None,
        "stdout_bytes": None,
        "stderr_bytes": None,
        "output_limit_exceeded": None,
        "frame_limit_exceeded": None,
        "bridge_stdout_bytes": None,
        "bridge_stderr_bytes": None,
        "container_exit_code": None,
        "container_oom_killed": None,
        "container_running": None,
        **updates,
    }


class FailureDiagnosticTests(unittest.TestCase):
    def test_closed_record_round_trips_without_aliasing_or_inventing_observations(self):
        value = diagnostic()
        result = docker.validate_failure_diagnostic(value)
        self.assertEqual(result, value)
        self.assertIsNot(result, value)
        self.assertIsNone(result["child_exit_code"])
        self.assertIsNone(result["container_oom_killed"])
        for origin in ("provider-process", "provider-spawn", "provider-output-limit", "bridge-process",
                       "host-relay", "host-control", "host-deadline", "host-cancellation", "host-protocol", "unknown"):
            self.assertEqual(docker.validate_failure_diagnostic(diagnostic(failure_origin=origin))["failure_origin"], origin)
        for category in ("stream-decode", "stream-disconnected", "authentication", "rate-limit",
                         "thread-resource", "process-failed", "unknown", "none"):
            self.assertEqual(docker.validate_failure_diagnostic(diagnostic(provider_error_category=category))["provider_error_category"], category)

    def test_closed_record_rejects_raw_fields_types_and_unbounded_values(self):
        invalid = [None, [], {}, {**diagnostic(), "stderr": "private-auth-or-prompt"}]
        for field, bad_values in {
            "schema_version": [None, "future"],
            "failure_origin": ["secret exception", None, 1],
            "provider_error_category": ["raw response", None, {}],
            "child_exit_code": [-1, 256, True, "1", 1.0],
            "child_signal": ["SIGSECRET", "secret", 9],
            "container_exit_code": [-1, 256, False, "1", 1.0],
            **{field: [-1, 8 * 1024 * 1024 + 1, True, "12", 1.5] for field in
               ("stdout_bytes", "stderr_bytes", "bridge_stdout_bytes", "bridge_stderr_bytes")},
            **{field: [0, 1, "true", {}] for field in
               ("output_limit_exceeded", "frame_limit_exceeded", "container_oom_killed", "container_running")},
        }.items():
            invalid.extend(diagnostic(**{field: value}) for value in bad_values)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(docker.DockerError) as caught:
                    docker.validate_failure_diagnostic(value)
                self.assertEqual(caught.exception.code, "docker-failure-diagnostic-invalid")
        for field in diagnostic():
            value = diagnostic()
            del value[field]
            with self.assertRaises(docker.DockerError):
                docker.validate_failure_diagnostic(value)

    def run_failure(self, frame, *, returncode=1, inspect_state=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = mock.Mock(requests=2, observed=True, last_status=200, last_error=None,
                                last_request=None, logical_requests=2, model_retries=0, request_history=[])
            limits = {"wall_time_seconds": 5, "cpu_seconds": 5, "memory_bytes": 256 * 1024**2,
                      "workspace_bytes": 1024**2, "pids": 64}
            executor = docker.DockerExecutor(image_id="sha256:" + "a" * 64, input_payload={},
                                            gateway=gateway, limits=limits, state_root=root)
            job = {"name": "unit-container", "cancelled": threading.Event()}
            container = {"Id": "exact-container-id", "Image": executor.image_id,
                         "Config": {"Labels": {"ai.vibapp.execution": "unit-execution",
                                                "ai.vibapp.owner": str(os.getuid()), "ai.vibapp.role": "codeagent"}},
                         "State": inspect_state or {"Running": False, "ExitCode": 1, "OOMKilled": False}}
            # A local static protocol producer replaces Docker. No model/config/auth discovery.
            launch = subprocess.Popen
            script = "import sys; sys.stdin.readline(); sys.stdout.write(" + repr(frame) + "); sys.stdout.flush(); sys.exit(" + str(returncode) + ")"
            captured_before_cleanup = []
            def cleanup(*_):
                captured_before_cleanup.append(getattr(executor, "failure_diagnostic", None))
                return True
            def inspect_before_cleanup(*_):
                self.assertTrue(job["cancelled"].is_set(), "diagnostic inspection must not delay cancellation")
                gateway.close.assert_called_once()
                self.assertEqual(captured_before_cleanup, [])
                return container
            with mock.patch.object(executor, "_create_reserved"), mock.patch.object(executor, "_inspect", side_effect=inspect_before_cleanup), \
                 mock.patch.object(executor, "_cleanup", side_effect=cleanup), mock.patch.object(docker, "docker_binary", return_value="unused"), \
                 mock.patch.object(docker.subprocess, "Popen", side_effect=lambda *_args, **kwargs: launch([sys.executable, "-c", script], **kwargs)):
                executor._run(mock.Mock(), mock.Mock(max_output_bytes=1024**2), "unit-execution", job)
            return json.loads((root / "docker-terminal.json").read_bytes()), captured_before_cleanup

    def test_failed_event_retains_closed_diagnostic_before_cleanup_and_keeps_failure_code(self):
        reported = diagnostic(provider_error_category="stream-decode", child_exit_code=1,
                              stdout_bytes=123, stderr_bytes=456, output_limit_exceeded=False,
                              frame_limit_exceeded=False, container_exit_code=0, container_oom_killed=True)
        terminal, before = self.run_failure(json.dumps({"type": "failed", "code": "provider-failed", "failure_diagnostic": reported}) + "\n")
        value = terminal["failure_diagnostic"]
        self.assertEqual(before, [value])
        self.assertEqual(value["provider_error_category"], "stream-decode")
        self.assertEqual(value["child_exit_code"], 1)
        self.assertEqual(value["stderr_bytes"], 456)
        self.assertEqual(value["container_exit_code"], 1)
        self.assertFalse(value["container_oom_killed"])
        self.assertFalse(value["container_running"])
        self.assertGreater(value["bridge_stdout_bytes"], 0)
        self.assertEqual(value["bridge_stderr_bytes"], 0)
        self.assertEqual(terminal["failure_code"], "provider-failed")
        self.assertTrue(terminal["cleanup_confirmed"])

    def test_bare_bridge_exit_is_distinguished_without_fabricating_child_cause(self):
        terminal, before = self.run_failure("", inspect_state={"Running": False, "ExitCode": 137, "OOMKilled": True})
        value = terminal["failure_diagnostic"]
        self.assertEqual(before, [value])
        self.assertEqual(value["failure_origin"], "bridge-process")
        self.assertEqual(value["provider_error_category"], "unknown")
        self.assertIsNone(value["child_exit_code"])
        self.assertIsNone(value["child_signal"])
        self.assertIsNone(value["stderr_bytes"])
        self.assertEqual(value["container_exit_code"], 137)
        self.assertTrue(value["container_oom_killed"])
        self.assertTrue(terminal["cleanup_confirmed"])

    def test_invalid_reported_diagnostic_cannot_persist_raw_text_or_mask_original_failure(self):
        invalid = {**diagnostic(), "raw_stderr": "PRIVATE_HEADER_AND_PROMPT"}
        terminal, _ = self.run_failure(json.dumps({"type": "failed", "code": "provider-failed", "failure_diagnostic": invalid}) + "\n")
        self.assertNotIn("PRIVATE", json.dumps(terminal))
        self.assertEqual(terminal["failure_code"], "provider-failed")
        self.assertEqual(terminal["failure_diagnostic"]["provider_error_category"], "unknown")
        docker.validate_failure_diagnostic(terminal["failure_diagnostic"])


@unittest.skipUnless(shutil.which("node"), "Node is needed for pure trusted-bridge diagnostic tests")
class BridgeFailureDiagnosticTests(unittest.TestCase):
    def bridge_diagnostic(self, **options):
        entry = (Path(docker.__file__).resolve().parent / "docker/entry.mjs").as_uri()
        script = "import {providerFailureDiagnostic} from " + json.dumps(entry) + "; "
        script += "process.stdout.write(JSON.stringify(providerFailureDiagnostic(" + json.dumps(options) + ")),()=>process.exit(0));"
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode()[:1000])
        return docker.validate_failure_diagnostic(json.loads(result.stdout))

    def test_known_provider_error_shapes_map_only_to_closed_categories(self):
        for text, category in [
            ("stream disconnected before completion: error decoding response body PRIVATE", "stream-decode"),
            ("stream disconnected before completion PRIVATE", "stream-disconnected"),
            ("pthread_create: Resource temporarily unavailable PRIVATE", "thread-resource"),
            ("unauthorized authentication 401 PRIVATE", "authentication"),
            ("rate limit 429 PRIVATE", "rate-limit"),
            ("some unrecognized PRIVATE failure", "unknown"),
        ]:
            with self.subTest(category=category):
                value = self.bridge_diagnostic(origin="provider-process", exitCode=1, diagnosticText=text,
                                               stdoutBytes=4, stderrBytes=5,
                                               outputLimitExceeded=False, frameLimitExceeded=False)
                self.assertEqual(value["provider_error_category"], category)
                self.assertEqual(value["child_exit_code"], 1)
                self.assertEqual(value["stdout_bytes"], 4)
                self.assertNotIn("PRIVATE", json.dumps(value))
                self.assertIsNone(value["container_exit_code"])

    def test_signalled_output_limit_preserves_null_exit_and_saturates_counts(self):
        value = self.bridge_diagnostic(origin="provider-output-limit", exitCode=None, signal="SIGKILL",
                                       stdoutBytes=100 * 1024**2, stderrBytes=1,
                                       outputLimitExceeded=True, frameLimitExceeded=True)
        self.assertIsNone(value["child_exit_code"])
        self.assertEqual(value["child_signal"], "SIGKILL")
        self.assertEqual(value["stdout_bytes"], 8 * 1024**2)
        self.assertTrue(value["output_limit_exceeded"])
        self.assertTrue(value["frame_limit_exceeded"])

    def test_unknown_signal_and_spawn_error_do_not_export_error_or_invent_exit(self):
        value = self.bridge_diagnostic(origin="provider-spawn", signal="private-signal",
                                       diagnosticText="PRIVATE_ARGUMENT_AND_HEADER")
        self.assertEqual(value["failure_origin"], "provider-spawn")
        self.assertEqual(value["child_signal"], "other")
        self.assertIsNone(value["child_exit_code"])
        self.assertIsNone(value["stdout_bytes"])
        self.assertNotIn("PRIVATE", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
