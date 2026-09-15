"""Pure cross-process admission tests; no Docker, provider or credentials."""
import fcntl
import multiprocessing
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_budget as budget


def worker(root, kind, start, release, results, cancellation=None, wait_seconds=0):
    with mock.patch.object(budget, "_runtime_directory", return_value=Path(root)):
        start.wait(10)
        try:
            with budget.lease(kind, cancellation=cancellation, wait_seconds=wait_seconds) as slot:
                results.put(("acquired", os.getpid(), slot))
                release.wait(10)
        except budget.BudgetError as error:
            results.put((error.code, os.getpid(), traceback.format_exc() if error.code == "execution-lock-invalid" else None))


class HostBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-budget-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = mock.patch.object(budget, "_runtime_directory", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.context = multiprocessing.get_context("spawn")

    def process(self, kind, start, release, results, **options):
        process = self.context.Process(target=worker, args=(str(self.root), kind, start, release, results), kwargs=options)
        process.start()
        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(5)
            process.close()
        self.addCleanup(cleanup)
        return process

    def test_cross_process_race_admits_exactly_two_and_one_compiler(self):
        for kind, limit in (("codeagent", 2), ("pipeline", 2), ("compiler", 1), ("docker-admission", 1)):
            with self.subTest(kind=kind):
                start, release = self.context.Event(), self.context.Event()
                results = self.context.Queue()
                processes = [self.process(kind, start, release, results) for _ in range(6)]
                start.set()
                outcomes = [results.get(timeout=10) for _ in processes]
                self.assertEqual(sum(row[0] == "acquired" for row in outcomes), limit, outcomes)
                self.assertEqual(sum(row[0] == "local-capacity-busy" for row in outcomes), 6 - limit, outcomes)
                self.assertEqual({row[2] for row in outcomes if row[0] == "acquired"}, set(range(limit)))
                release.set()
                for process in processes:
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
                with budget.lease(kind):
                    pass

    def test_crashed_owner_releases_slot_without_replacing_inode(self):
        start, release, results = self.context.Event(), self.context.Event(), self.context.Queue()
        process = self.process("compiler", start, release, results)
        start.set()
        self.assertEqual(results.get(timeout=10)[0], "acquired")
        path = budget._root("compiler") / "execution.lock"
        inode = path.stat().st_ino
        with self.assertRaisesRegex(budget.BudgetError, "capacity"):
            with budget.lease("compiler"):
                pass
        process.kill()
        process.join(5)
        with budget.lease("compiler"):
            self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_waiter_cancel_is_prompt_and_does_not_release_another_job(self):
        start, release, results = self.context.Event(), self.context.Event(), self.context.Queue()
        cancellation = self.context.Event()
        with budget.lease("compiler"):
            process = self.process("compiler", start, release, results, cancellation=cancellation, wait_seconds=10)
            start.set()
            cancellation.set()
            self.assertEqual(results.get(timeout=5)[0], "provider-cancelled")
            process.join(5)
            with self.assertRaisesRegex(budget.BudgetError, "capacity"):
                with budget.lease("compiler"):
                    pass
        with budget.lease("compiler"):
            pass

    def test_wait_acquires_after_release_and_respects_deadline(self):
        with budget.lease("compiler"):
            before = time.monotonic()
            with self.assertRaises(budget.BudgetError) as caught:
                with budget.lease("compiler", wait_seconds=.12):
                    pass
            self.assertEqual(caught.exception.code, "local-capacity-busy")
            self.assertGreaterEqual(time.monotonic() - before, .1)
            start, release, results = self.context.Event(), self.context.Event(), self.context.Queue()
            process = self.process("compiler", start, release, results, wait_seconds=5)
            start.set()
        self.assertEqual(results.get(timeout=10)[0], "acquired")
        release.set()
        process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_legacy_first_inode_participates_and_body_exception_releases(self):
        root = budget._root("codeagent")
        root.mkdir(mode=0o700)
        descriptor = os.open(root / "execution.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with budget.lease("codeagent") as slot:
                self.assertEqual(slot, 1)
                with self.assertRaises(budget.BudgetError):
                    with budget.lease("codeagent"):
                        pass
        finally:
            os.close(descriptor)
        with self.assertRaisesRegex(RuntimeError, "body-failed"):
            with budget.lease("codeagent"):
                raise RuntimeError("body-failed")
        with budget.lease("codeagent") as slot:
            self.assertEqual(slot, 0)

    def test_private_nofollow_regular_single_link_files_required(self):
        for case in ("root-symlink", "root-mode", "slot-symlink", "slot-mode", "slot-hardlink", "slot-fifo"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=self.root) as directory:
                with mock.patch.object(budget, "_runtime_directory", return_value=Path(directory)):
                    root = budget._root("codeagent")
                    target = Path(directory) / "target"
                    if case == "root-symlink":
                        target.mkdir(mode=0o700)
                        root.symlink_to(target, target_is_directory=True)
                    else:
                        root.mkdir(mode=0o700)
                        slot = root / "execution-1.lock"
                        if case == "root-mode":
                            root.chmod(0o755)
                        elif case == "slot-fifo":
                            os.mkfifo(slot, mode=0o600)
                        elif case == "slot-symlink":
                            target.touch(mode=0o600)
                            slot.symlink_to(target)
                        else:
                            slot.touch(mode=0o600)
                            if case == "slot-mode":
                                slot.chmod(0o640)
                            else:
                                os.link(slot, target)
                    with self.assertRaises(budget.BudgetError) as caught:
                        with budget.lease("codeagent"):
                            pass
                    self.assertEqual(caught.exception.code, "execution-lock-invalid")

    def test_invalid_bounds_and_precancel_fail_closed(self):
        for kind, wait in (("other", 0), ("compiler", -1), ("compiler", float("nan")),
                           ("compiler", float("inf")), ("compiler", 1801), ("compiler", True)):
            with self.assertRaises(budget.BudgetError) as caught:
                with budget.lease(kind, wait_seconds=wait):
                    pass
            self.assertEqual(caught.exception.code, "execution-lock-invalid")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(budget.BudgetError) as caught:
            with budget.lease("compiler", cancellation=cancelled):
                pass
        self.assertEqual(caught.exception.code, "provider-cancelled")
        with budget.lease("compiler"):
            pass

    def test_cancel_after_lock_acquire_closes_descriptor(self):
        cancellation = mock.Mock()
        cancellation.is_set.side_effect = [False, True]
        with self.assertRaises(budget.BudgetError):
            with budget.lease("compiler", cancellation=cancellation):
                pass
        with budget.lease("compiler"):
            pass

    def test_temp_environment_cannot_split_real_subprocess_namespace(self):
        script = (
            "import sys;sys.path.insert(0,sys.argv[1]);import host_budget as b;"
            "lease=b.lease('compiler');"
            "\ntry:\n lease.__enter__();print('acquired',flush=True);input()"
            "\nexcept b.BudgetError as error:print(error.code,flush=True)"
            "\nfinally:\n lease.__exit__(None,None,None)"
        )
        module_root = str(Path(budget.__file__).parent)
        processes = []
        try:
            for index in range(2):
                distinct = self.root / str(index)
                distinct.mkdir(mode=0o700)
                environment = dict(os.environ)
                environment.update({name: str(distinct) for name in ("TMPDIR", "TEMP", "TMP")})
                process = subprocess.Popen([sys.executable, "-c", script, module_root], env=environment,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                self.assertEqual(process.stdout.readline().strip(), "acquired" if index == 0 else "local-capacity-busy")
            processes[1].wait(timeout=5)
            self.assertEqual(processes[1].returncode, 0, processes[1].stderr.read())
            # No locks were created beneath either caller-selected temp root.
            self.assertEqual(list((self.root / "0").iterdir()), [])
            self.assertEqual(list((self.root / "1").iterdir()), [])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.stdin.write("release\n")
                    process.stdin.flush()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main()
