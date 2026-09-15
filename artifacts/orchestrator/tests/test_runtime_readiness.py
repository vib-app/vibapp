"""Synthetic gate contracts; no guest business-function acceptance is claimed."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import runtime_readiness as gate


class RuntimeReadinessTests(unittest.TestCase):
    def candidate(self, root, *, count=1, kind="ui"):
        candidate = root / "candidates" / ("a" * 64) / "candidate.json"
        package = candidate.parent / "package"
        package.mkdir(parents=True)
        manifest = {"app": {"id": "ai.vibapp.synthetic", "kind": kind},
                    "runtime": {"world": "ui-only-reference"},
                    "entrypoints": [{"id": f"entry-{index}", "kind": "launcher-ui", "routes": {"initial": "/"}}
                                    for index in range(count)]}
        (package / "manifest.json").write_text(json.dumps(manifest))
        (package / "component.wasm").write_bytes(b"explicit synthetic component; not executable")
        record = {"schema_version": "vibapp.builder-candidate.experimental-v1", "document_type": "verifier-promoted-candidate",
                  "state": "candidate-ready", "package_digest_sha256": "a" * 64,
                  "manifest": {"path": "manifest.json", "sha256": gate._hash(package / "manifest.json", gate.MAX_JSON)},
                  "component": {"path": "component.wasm", "sha256": gate._hash(package / "component.wasm", gate.MAX_JSON)}}
        candidate.write_text(json.dumps(record))
        binary = root / "synthetic-runtime-binary"
        binary.write_bytes(b"synthetic runtime identity; not executable")
        return candidate, binary

    def fake_daemon(self, binding, log, *, reject=None, mismatch=False, disable_error=False):
        class SyntheticDaemon:
            def __init__(self, root, promotion, **kwargs):
                self.root = root
                log.append((root.name, "construct", promotion, kwargs))
            def execute(self, envelope, **kwargs):
                command = envelope["command"]
                tag, value = command["tag"], command["value"]
                log.append((self.root.name, tag, value, kwargs))
                if tag == "launch" and reject:
                    return {"error": {"code": "malformed-output", "message": reject, "retryable": False}}
                if tag == "disable" and disable_error:
                    raise RuntimeError("synthetic disable failure")
                if tag == "launch":
                    return {"outcome": {"value": {**value, "package_digest_sha256": binding["package_digest_sha256"],
                        "component_sha256": "f" * 64 if mismatch else binding["component_sha256"],
                        "semantic_surface": {"synthetic": True}}}}
                return {"outcome": {"value": {}}}
            def shutdown(self):
                log.append((self.root.name, "shutdown", None, None))
        return SyntheticDaemon

    def test_each_launcher_is_cold_and_initial_once_then_disabled_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, binary = self.candidate(root, count=2)
            manifest, binding = gate.candidate_binding(candidate)
            log = []
            result = gate._exercise(candidate, binary, root, self.fake_daemon(binding, log), [])
            self.assertEqual(result["status"], "PASS")
            self.assertFalse(result["business_function_acceptance"])
            self.assertTrue(result["cleanup_confirmed"])
            expected = ["construct", "install", "enable", "launch", "disable", "shutdown"]
            self.assertEqual([row[1] for row in log], expected * 2)
            self.assertEqual({row[0] for row in log}, {"entry-0", "entry-1"})
            self.assertEqual(result["binding"]["runtime_binary_sha256"], gate._hash(binary, gate.MAX_BINARY))
            for row in log:
                if row[1] == "install":
                    self.assertFalse(row[2]["enable_after_install"])
                    self.assertEqual(row[3]["promotion_record"], candidate)

    def test_launch_rejection_keeps_original_cause_and_cleans_up(self):
        for error in ("UI semantic tree has an invalid or duplicate action", "UI semantic tree has an invalid or duplicate confirmation action"):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidate, binary = self.candidate(root)
                _, binding = gate.candidate_binding(candidate)
                log = []
                with self.assertRaisesRegex(gate.RuntimeReadinessError, error):
                    gate._exercise(candidate, binary, root, self.fake_daemon(binding, log, reject=error), [])
                self.assertEqual([row[1] for row in log][-2:], ["disable", "shutdown"])

    def test_disable_failure_still_shutdowns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, binary = self.candidate(root)
            _, binding = gate.candidate_binding(candidate)
            log = []
            with self.assertRaisesRegex(RuntimeError, "disable failure"):
                gate._exercise(candidate, binary, root, self.fake_daemon(binding, log, disable_error=True), [])
            self.assertEqual(log[-1][1], "shutdown")

    def test_wrong_surface_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, binary = self.candidate(root)
            _, binding = gate.candidate_binding(candidate)
            with self.assertRaisesRegex(gate.RuntimeReadinessError, "not bound"):
                gate._exercise(candidate, binary, root, self.fake_daemon(binding, [], mismatch=True), [])

    def test_ui_service_mixing_and_entrypoint_ceiling_rejected(self):
        for count in (0, 9):
            with tempfile.TemporaryDirectory() as directory:
                candidate, _ = self.candidate(Path(directory), count=count)
                with self.assertRaises(gate.RuntimeReadinessError):
                    gate.check_runtime_readiness(candidate)
        with tempfile.TemporaryDirectory() as directory:
            candidate, _ = self.candidate(Path(directory))
            manifest, _ = gate.candidate_binding(candidate)
            manifest["entrypoints"][0]["kind"] = "service"
            with self.assertRaises(gate.RuntimeReadinessError):
                gate._entries(manifest)

    def test_non_ui_is_not_applicable_without_runtime_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate, _ = self.candidate(Path(directory), kind="service")
            with mock.patch.object(gate, "_run_child") as child:
                result = gate.check_runtime_readiness(candidate)
            self.assertEqual(result["status"], "not-applicable")
            self.assertFalse(result["business_function_acceptance"])
            child.assert_not_called()

    def test_capability_host_change_stops_before_any_execution(self):
        from vibapp_daemon import core
        with mock.patch.dict(core.LOCAL_HOST_CAPABILITY_AVAILABILITY, {gate.FORBIDDEN_EFFECTS[0]: "brokered"}):
            with self.assertRaisesRegex(gate.RuntimeReadinessError, "unavailable HTTP"):
                gate._worker(Path("unused"), Path("unused"), Path("unused"))

    def test_gate_child_owns_constructor_failure_and_uses_single_pid_termination(self):
        from vibapp_daemon import core, service_executor
        class SyntheticProcess:
            stdin = stdout = None
            returncode = None
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
            def kill(self):
                self.returncode = -9
            def wait(self, timeout):
                return self.returncode
        process = SyntheticProcess()
        class FailingSyntheticWorker:
            def __init__(self):
                self.process = service_executor.subprocess.Popen(["synthetic"], start_new_session=True)
                self._context_directory = None
                raise RuntimeError("synthetic constructor failure after spawn")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, binary = self.candidate(root)
            _, binding = gate.candidate_binding(candidate)
            log = []
            parent = self.fake_daemon(binding, log)
            class SyntheticDaemon(parent):
                def execute(self, envelope, **kwargs):
                    if envelope["command"]["tag"] == "launch":
                        core.ServiceWorker()
                    return super().execute(envelope, **kwargs)
            with mock.patch.object(core, "RuntimeDaemon", SyntheticDaemon), \
                 mock.patch.object(core, "ServiceWorker"), \
                 mock.patch.object(service_executor, "ServiceWorker", FailingSyntheticWorker), \
                 mock.patch.object(service_executor, "subprocess", gate.subprocess), \
                 mock.patch.object(gate.subprocess, "Popen", return_value=process) as spawn:
                with self.assertRaisesRegex(RuntimeError, "constructor failure"):
                    gate._worker(candidate, binary, root)
                self.assertFalse(spawn.call_args.kwargs["start_new_session"])
            self.assertEqual(process.returncode, -15)
            self.assertEqual(log[-1][1], "shutdown")

    def test_binary_or_candidate_drift_cannot_reuse_success_and_every_call_executes(self):
        from vibapp_daemon import service_executor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, binary = self.candidate(root)
            calls = []
            def child(command, scratch, cancellation):
                del command, cancellation
                calls.append(scratch)
                _, binding = gate.candidate_binding(candidate)
                binding.update(runtime_binary_sha256=gate._hash(binary, gate.MAX_BINARY),
                               entrypoints=gate._entries(gate._json(candidate.parent / "package/manifest.json")))
                (scratch / "result.json").write_text(json.dumps({"status": "PASS", "binding": binding,
                    "checked_initial_surfaces": binding["entrypoints"],
                    "cleanup_confirmed": True, "business_function_acceptance": False}))
            with mock.patch.object(service_executor, "resolve_runtime_binary", return_value=binary), mock.patch.object(gate, "_run_child", side_effect=child):
                gate.check_runtime_readiness(candidate)
                gate.check_runtime_readiness(candidate)
                self.assertEqual(len(calls), 2)
                self.assertNotEqual(*calls)
                original = child
                def mutate(command, scratch, cancellation):
                    original(command, scratch, cancellation)
                    binary.write_bytes(b"changed after execution")
                with mock.patch.object(gate, "_run_child", side_effect=mutate):
                    with self.assertRaisesRegex(gate.RuntimeReadinessError, "binding changed"):
                        gate.check_runtime_readiness(candidate)
            self.assertTrue(all(not path.exists() for path in calls))

    def test_actual_wrapper_success_and_timeout_are_bounded_and_quiescent(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            gate._run_child([sys.executable, "-c", "pass"], scratch, None)
            with mock.patch.dict(gate.POLICY, {"wall_seconds": 0.15}):
                with self.assertRaisesRegex(gate.RuntimeReadinessError, "wall timeout"):
                    gate._run_child([sys.executable, "-c", "import time; time.sleep(5)"], scratch, None)

    def test_cancellation_cleanup_and_unconfirmed_cleanup_never_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            event = threading.Event()
            event.set()
            with self.assertRaisesRegex(gate.RuntimeReadinessError, "cancelled"):
                gate._run_child([sys.executable, "-c", "import time; time.sleep(5)"], scratch, event)
            process = mock.Mock(pid=900000000, returncode=0)
            process.poll.return_value = 0
            with mock.patch.object(gate.subprocess, "Popen", return_value=process), \
                 mock.patch.object(gate, "_group_exists", return_value=True), \
                 mock.patch.object(gate.os, "killpg"), \
                 mock.patch.object(gate.time, "monotonic", side_effect=[0, 0, 3]):
                with self.assertRaisesRegex(gate.RuntimeReadinessError, "cleanup could not be confirmed"):
                    gate._run_child(["synthetic"], scratch, None)


if __name__ == "__main__":
    unittest.main()
