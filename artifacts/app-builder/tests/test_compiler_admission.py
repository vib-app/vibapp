"""Offline compiler-lane tests: no Cargo, Docker, provider or credential access."""
from contextlib import contextmanager
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import app_builder as builder


CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import app_builder as builder
builder.host_budget._root = lambda kind: Path(sys.argv[2]) / kind
class Probe(builder.MacSandboxCargoRunner):
    def __init__(self): pass
    def _execute_in_workspace(self, source, workspace, limits, *, cancellation=None):
        print(json.dumps({"entered": True, "wall_seconds": limits.wall_seconds}), flush=True)
        sys.stdin.readline()
        return builder.SafeFixtureRunner(b"inert").execute(source, workspace, limits)
Probe().execute(Path(sys.argv[2]), Path(sys.argv[2]), builder.ProcessLimits(wall_seconds=5))
'''


class CompilerAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vibapp-compiler-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(builder.host_budget, "_root", side_effect=lambda kind: self.root / kind)
        patch.start()
        self.addCleanup(patch.stop)
        self.runner = object.__new__(builder.MacSandboxCargoRunner)

    def child(self):
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(BASE), str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        return process

    def entered(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 3)[0], "compiler child did not enter")
        return json.loads(process.stdout.readline())

    def release(self, process):
        process.stdin.write("release\n")
        process.stdin.flush()
        process.wait(timeout=3)
        self.assertEqual(process.returncode, 0, process.stderr.read())

    def test_compiler_serializes_distinct_processes_and_deducts_queue_time(self):
        first = self.child()
        self.assertTrue(self.entered(first)["entered"])
        second = self.child()
        self.assertFalse(select.select([second.stdout], [], [], .25)[0], "second compiler entered concurrently")
        self.release(first)
        observed = self.entered(second)
        self.assertLess(observed["wall_seconds"], 4.9)
        self.release(second)

    def test_compiler_contention_is_bounded_and_starts_no_workspace(self):
        first = self.child()
        self.entered(first)
        started = time.monotonic()
        with mock.patch.object(self.runner, "_execute_in_workspace") as execute:
            with self.assertRaises(builder.PipelineError) as caught:
                self.runner.execute(self.root, self.root, builder.ProcessLimits(wall_seconds=.1))
        self.assertEqual(caught.exception.code, "local-capacity-busy")
        self.assertLess(time.monotonic() - started, .5)
        execute.assert_not_called()
        self.release(first)

    def test_waiting_compiler_cancels_without_interrupting_other_process(self):
        first = self.child()
        self.entered(first)
        cancelled = threading.Event()
        timer = threading.Timer(.1, cancelled.set)
        timer.start()
        try:
            with mock.patch.object(self.runner, "_execute_in_workspace") as execute:
                with self.assertRaises(builder.PipelineError) as caught:
                    self.runner.execute(self.root, self.root, builder.ProcessLimits(wall_seconds=3), cancellation=cancelled)
            self.assertEqual(caught.exception.code, "cancelled")
            execute.assert_not_called()
            self.assertIsNone(first.poll())
        finally:
            timer.join(timeout=1)
        self.release(first)

    def test_compiler_crash_releases_lane(self):
        first = self.child()
        self.entered(first)
        first.kill()
        first.wait(timeout=3)
        second = self.child()
        self.entered(second)
        self.release(second)

    def source_provider(self):
        sys.path.insert(0, str(BASE.parent / "codeagent-adapter"))
        from docker_provider import DockerCodexProvider
        api = types.SimpleNamespace(require_model=lambda value: value, AdapterError=builder.PipelineError,
                                    derive_provider_control_record=mock.Mock(return_value={}))
        provider = DockerCodexProvider(api, mock.Mock(), model="synthetic")
        provider._execution_root = self.root
        provider._task = {"limits": {}}
        provider.import_source = mock.Mock()
        provider.configure_builder(self.runner, builder.ProcessLimits)
        return provider

    def test_direct_source_probe_uses_same_compiler_and_contention_is_not_repair(self):
        first = self.child()
        self.entered(first)
        provider = self.source_provider()
        with mock.patch.object(self.runner, "_execute_in_workspace") as execute:
            feedback = provider.check_source({"id": 0}, threading.Event(), .1)
        self.assertEqual(feedback["code"], "local-capacity-busy")
        self.assertFalse(feedback["repairable"])
        self.assertEqual(provider._terminal_failure_code("source-check-failed"), "local-capacity-busy")
        self.assertEqual(provider._terminal_failure_code("provider-timeout"), "provider-timeout")
        execute.assert_not_called()
        self.assertIsNone(first.poll())
        self.release(first)

    def test_source_probe_wait_cancellation_is_not_model_repair(self):
        first = self.child()
        self.entered(first)
        provider = self.source_provider()
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(self.runner, "_execute_in_workspace") as execute:
            feedback = provider.check_source({"id": 0}, cancelled, 3)
        self.assertEqual(feedback["code"], "cancelled")
        self.assertFalse(feedback["repairable"])
        self.assertEqual(provider._terminal_failure_code("source-check-failed"), "provider-cancelled")
        execute.assert_not_called()
        self.assertIsNone(first.poll())
        self.release(first)

    def test_source_probe_waits_for_final_build_then_succeeds_with_remaining_budget(self):
        first = self.child()
        self.entered(first)
        provider = self.source_provider()
        provider._last_source_error = "old failed probe"
        provider._last_source_error_code = "process-failed"
        def release():
            first.stdin.write("release\n")
            first.stdin.flush()
        timer = threading.Timer(.15, release)
        timer.start()
        seen = []
        def probe(source, workspace, limits, **kwargs):
            seen.append(limits.wall_seconds)
            return builder.SafeFixtureRunner(b"inert").execute(source, workspace, limits)
        try:
            with mock.patch.object(self.runner, "_execute_in_workspace", side_effect=probe):
                feedback = provider.check_source({"id": 0}, threading.Event(), 3)
        finally:
            timer.join(timeout=1)
        first.wait(timeout=3)
        self.assertEqual(first.returncode, 0, first.stderr.read())
        self.assertEqual(feedback, {"ok": True})
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], 2.9)
        self.assertIsNone(provider._last_source_error)

    def test_expired_source_probe_deadline_never_gets_a_new_minimum_second(self):
        provider = self.source_provider()
        with mock.patch.object(self.runner, "execute") as execute:
            feedback = provider.check_source({"id": 0}, threading.Event(), 0)
        self.assertEqual(feedback["code"], "provider-timeout")
        self.assertFalse(feedback["repairable"])
        execute.assert_not_called()

    def test_queue_wait_is_charged_and_cleanup_releases_after_failure(self):
        clock = [100.0]
        @contextmanager
        def delayed_lease(*args, **kwargs):
            self.assertEqual(args, ("compiler",))
            self.assertEqual(kwargs["wait_seconds"], 20)
            clock[0] += 7
            yield
        def probe(source, workspace, limits, *, cancellation):
            self.assertEqual(limits.wall_seconds, 13)
            self.assertTrue(workspace.is_dir())
            raise builder.PipelineError("process-failed", "synthetic failure")
        with mock.patch.object(builder.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(builder.host_budget, "lease", side_effect=delayed_lease), \
                mock.patch.object(self.runner, "_execute_in_workspace", side_effect=probe):
            with self.assertRaises(builder.PipelineError) as caught:
                self.runner.execute(self.root, self.root, builder.ProcessLimits(wall_seconds=20))
        self.assertEqual(caught.exception.code, "process-failed")

    def test_preflight_and_generate_lockfile_do_not_restart_wall_budget(self):
        self.runner.tool_layer = self.root / "tools"
        self.runner.cargo_home = self.root / "cache"
        source, workspace = self.root / "source", self.root / "workspace"
        source.mkdir()
        workspace.mkdir()
        clock, seen = [100.0], []
        def preflight():
            clock[0] += 1
            return {"cache_acceptance_sha256": "a" * 64}
        def run(*args, **kwargs):
            seen.append(kwargs["limits"].wall_seconds)
            if len(seen) == 2:
                raise builder.PipelineError("synthetic-stop", "no actual build")
            clock[0] += 4
            return types.SimpleNamespace(stdout=b"", stderr=b"")
        with mock.patch.object(builder.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(self.runner, "preflight", side_effect=preflight), \
                mock.patch.object(builder, "run_bounded", side_effect=run):
            with self.assertRaises(builder.PipelineError) as caught:
                self.runner._execute_in_workspace(source, workspace, builder.ProcessLimits(wall_seconds=10))
        self.assertEqual(caught.exception.code, "synthetic-stop")
        self.assertEqual(seen, [9, 5])


if __name__ == "__main__":
    unittest.main()
