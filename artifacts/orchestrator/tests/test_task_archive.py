from __future__ import annotations

from contextlib import ExitStack
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_archive_test_module", BASE / "task_archive.py")
archive = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = archive
spec.loader.exec_module(archive)


class TaskArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-task-archive-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve()
        self.data.chmod(0o700)
        self.root = self.data / "delivery-controller"
        self.mkdir(self.root / "tasks")
        self.write(self.root / ".delivery-controller.lock", b"")
        self.archive = archive.TaskArchive(self.data, allowed_roots=(self.data,))

    def mkdir(self, path):
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)

    def write(self, path, value, mode=0o600):
        self.mkdir(path.parent)
        path.write_bytes(value if isinstance(value, bytes) else archive._bytes(value))
        path.chmod(mode)

    def task(self, number=1, states=("failed",)):
        task_id = f"development-{number:032x}"
        directory = self.root / "tasks" / task_id
        ids = [f"attempt-{index + 1:04d}-{index + number:016x}" for index in range(len(states))]
        task = {
            "schema_version": "vibapp.delivery-task.experimental-v2", "document_type": "delivery-task",
            "task_id": task_id, "need_id": f"need-{number}", "need_spec_revision": 1,
            "job_id": f"job-{number}", "immutable_task_digest_sha256": "a" * 64,
            "provider_execution_identity_sha256": "b" * 64, "supersedes_task_id": None,
            "attempt_count": len(ids), "current_attempt_id": ids[-1], "attempt_ids": ids,
            "created_at_utc": "2026-09-09T00:00:00Z", "updated_at_utc": "2026-09-09T00:00:00Z",
        }
        self.write(directory / "task.json", task)
        for index, (attempt_id, state) in enumerate(zip(ids, states)):
            attempt_dir = directory / "attempts" / attempt_id
            attempt = {
                "schema_version": "vibapp.delivery-attempt.experimental-v2", "document_type": "delivery-attempt",
                "task_id": task_id, "attempt_id": attempt_id, "attempt_number": index + 1,
                "retry_of_attempt_id": ids[index - 1] if index else None, "need_id": task["need_id"],
                "title": "Synthetic private title", "need_spec_revision": 1, "job_id": task["job_id"],
                "consent_id": f"consent-{index + 1}", "provider_execution_identity_sha256": "b" * 64,
                "registry_request_id": "registry-1", "registry_evidence_sha256": "c" * 64,
                "canonical_task_sha256": "d" * 64, "immutable_task_digest_sha256": "a" * 64,
                "status": state, "stage": "codeagent-failed" if state == "failed" else state,
                "progress_percent": 10, "error": {"code": "provider-failed"} if state == "failed" else None,
                "outputs": {}, "event_count": 2, "created_at_utc": task["created_at_utc"],
                "updated_at_utc": task["updated_at_utc"],
            }
            self.write(attempt_dir / "attempt.json", attempt)
            self.write(attempt_dir / ".run.lock", b"")
            self.write(attempt_dir / "codeagent/output/.execution.lock", b"")
            gateway = f"vibapp-{number + index:064x}"
            status = {"schema_version": "vibapp.codeagent-adapter-status.experimental-v2",
                      "status": "codeagent-failed", "gateway_request_id": gateway,
                      **{key: attempt[key] for key in ("job_id", "need_id", "immutable_task_digest_sha256",
                          "provider_execution_identity_sha256", "consent_id")}}
            self.write(attempt_dir / "codeagent/status.json", status)
            self.write(attempt_dir / "codeagent/output/executions" / attempt_id / "docker-terminal.json",
                       {"backend_execution_id": gateway, "cleanup_confirmed": True, "state": "failed"}, 0o644)
            self.write(attempt_dir / "codeagent/output/consents/consumed.json", {"consumed": True})
        return task_id, directory

    def contents(self):
        return {str(path.relative_to(self.data)): path.read_bytes()
                for path in self.data.rglob("*") if path.is_file() and not path.is_symlink()}

    def current(self, directory):
        task = json.loads((directory / "task.json").read_bytes())
        return directory / "attempts" / task["current_attempt_id"]

    def test_dry_run_has_no_filesystem_mutation_and_identifies_only_failed(self):
        failed, _ = self.task(1)
        self.task(2, ("private-appstore-ready",))
        before = self.contents()
        inventory = self.archive.inventory()
        self.assertEqual(inventory["eligible_count"], 1)
        self.assertEqual([row["task_id"] for row in inventory["tasks"] if row["eligible"]], [failed])
        self.assertEqual(before, self.contents())

    def test_archive_keeps_all_originals_and_idempotency_with_private_snapshot(self):
        task_id, directory = self.task()
        self.write(self.data / "history/need.json", {"chat": "retained"})
        self.write(self.data / "local-appstore/catalog.json", {"apps": ["successful-other-app"]})
        before = self.contents()
        result = self.archive.archive([task_id])
        after = self.contents()
        for path, data in before.items():
            self.assertEqual(after[path], data, path)
        self.assertTrue(self.archive.is_archived(task_id))
        self.assertEqual(self.archive.archived_tasks()["archived"], [{"task_id": task_id, "job_id": "job-1",
            "immutable_task_digest_sha256": "a" * 64, "provider_execution_identity_sha256": "b" * 64,
            "consent_id": "consent-1"}])
        snapshot = self.root / "task-archives" / result["archived"][0]["archive_id"] / "snapshot.json"
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / ".archived.json").stat().st_mode & 0o777, 0o600)
        # The controller still resolves precisely the original task/consent lineage.
        from delivery_controller import DeliveryController
        self.assertEqual(DeliveryController._task_record(directory)["task_id"], task_id)
        second = self.archive.archive([task_id])
        self.assertEqual(second["archived"], [])
        (directory / ".archived.json").rename(snapshot.parent / "restored-marker.json")
        self.assertFalse(self.archive.is_archived(task_id))

    def test_success_running_queued_and_any_successful_earlier_attempt_refused(self):
        for index, states in enumerate((("private-appstore-ready",), ("running",), ("queued",),
                                        ("private-appstore-ready", "failed")), 1):
            with self.subTest(states=states):
                task_id, _ = self.task(index, states)
                before = self.contents()
                with self.assertRaises(archive.ArchiveError):
                    self.archive.archive([task_id])
                self.assertEqual(before, self.contents())

    def test_success_outputs_on_failed_record_are_never_hidden(self):
        task_id, directory = self.task()
        attempt_path = self.current(directory) / "attempt.json"
        value = json.loads(attempt_path.read_bytes())
        value["outputs"]["app_id"] = "ai.vibapp.existing"
        self.write(attempt_path, value)
        with self.assertRaisesRegex(archive.ArchiveError, "task-has-appstore-output"):
            self.archive.archive([task_id])

    def test_changed_attempt_or_new_attempt_invalidates_marker(self):
        task_id, directory = self.task()
        self.archive.archive([task_id])
        path = self.current(directory) / "attempt.json"
        original = path.read_bytes()
        value = json.loads(original)
        value["event_count"] += 1
        self.write(path, value)
        self.assertFalse(self.archive.is_archived(task_id))
        self.write(path, original)
        self.assertTrue(self.archive.is_archived(task_id))
        self.mkdir(directory / "attempts/attempt-0002-0000000000000002")
        self.assertFalse(self.archive.is_archived(task_id))

    def test_archived_task_becomes_visible_during_worker_start_before_attempt_changes(self):
        task_id, directory = self.task()
        self.archive.archive([task_id])
        current = self.current(directory)
        attempt_bytes = (current / "attempt.json").read_bytes()
        self.write(self.root / "desktop-workers" / f"{task_id}-{current.name}.json", {
            "schema_version": "vibapp.desktop-delivery-worker.experimental-v2", "status": "starting",
            "terminal": False, "task_id": task_id, "attempt_id": current.name,
            "immutable_task_digest_sha256": "a" * 64,
        })
        self.assertEqual((current / "attempt.json").read_bytes(), attempt_bytes)
        self.assertFalse(self.archive.is_archived(task_id))
        self.assertEqual(self.archive.archived_tasks()["archived"], [])

    def test_archived_task_becomes_visible_while_run_or_adapter_lock_is_held(self):
        task_id, directory = self.task()
        self.archive.archive([task_id])
        current = self.current(directory)
        for path in (self.root / ".delivery-controller.lock", current / ".run.lock", current / "codeagent/output/.execution.lock"):
            descriptor = os.open(path, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(self.archive.is_archived(task_id))
                self.assertEqual(self.archive.archived_tasks()["archived"], [])
            finally:
                os.close(descriptor)
        self.assertTrue(self.archive.is_archived(task_id))

    def test_missing_or_contradictory_cleanup_refused(self):
        task_id, directory = self.task()
        current = self.current(directory)
        terminal = current / "codeagent/output/executions" / current.name / "docker-terminal.json"
        value = json.loads(terminal.read_bytes())
        for change in ({"cleanup_confirmed": False}, {"backend_execution_id": "vibapp-" + "f" * 64},
                       {"state": "running"}):
            self.write(terminal, {**value, **change})
            with self.assertRaisesRegex(archive.ArchiveError, "docker-cleanup-unproven"):
                self.archive.archive([task_id])

    def test_uncertain_desktop_worker_overrides_docker_cleanup(self):
        task_id, directory = self.task()
        current = self.current(directory)
        worker_path = self.root / "desktop-workers" / f"{task_id}-{current.name}.json"
        worker = {"schema_version": "vibapp.desktop-delivery-worker.experimental-v2",
                  "task_id": task_id, "attempt_id": current.name, "immutable_task_digest_sha256": "a" * 64,
                  "status": "starting"}
        self.write(worker_path, worker)
        with self.assertRaisesRegex(archive.ArchiveError, "worker-cleanup-unproven"):
            self.archive.archive([task_id])
        self.write(worker_path, {**worker, "status": "finished", "terminal": True, "cleanup_confirmed": True})
        self.archive.archive([task_id])
        self.assertTrue(self.archive.is_archived(task_id))

    def test_terminal_failed_list_archive_does_not_claim_missing_pipeline_cleanup(self):
        task_id, directory = self.task()
        path = self.current(directory) / "attempt.json"
        value = json.loads(path.read_bytes())
        value["stage"] = "builder-failed"
        self.write(path, value)
        self.archive.archive([task_id])
        self.assertTrue(self.archive.is_archived(task_id))

    def test_pre_cleanup_running_snapshot_does_not_override_final_cleanup(self):
        task_id, directory = self.task()
        current = self.current(directory)
        terminal_path = current / "codeagent/output/executions" / current.name / "docker-terminal.json"
        terminal = json.loads(terminal_path.read_bytes())
        self.write(terminal_path, {**terminal, "failure_diagnostic": {"container_running": True}})
        status_path = current / "codeagent/status.json"
        status = json.loads(status_path.read_bytes())
        self.write(status_path, {**status, "gateway_request_id": None})
        self.archive.archive([task_id])
        self.assertTrue(self.archive.is_archived(task_id))

    def make_legacy(self, directory):
        task_path = directory / "task.json"
        task = json.loads(task_path.read_bytes())
        task = {key: value for key, value in task.items() if key in archive.LEGACY_TASK_KEYS}
        task["schema_version"] = "vibapp.delivery-task.experimental-v1"
        self.write(task_path, task)
        for attempt_id in task["attempt_ids"]:
            path = directory / "attempts" / attempt_id / "attempt.json"
            value = json.loads(path.read_bytes())
            value["schema_version"] = "vibapp.delivery-attempt.experimental-v1"
            del value["consent_id"], value["provider_execution_identity_sha256"]
            self.write(path, value)

    def test_legacy_metadata_is_validated_without_migration_and_restores(self):
        task_id, directory = self.task()
        self.make_legacy(directory)
        current = self.current(directory)
        self.write(self.root / "desktop-workers" / f"{task_id}-{current.name}.json", {
            "schema_version": "vibapp.desktop-delivery-worker.experimental-v2", "status": "finished",
            "task_id": task_id, "attempt_id": current.name, "immutable_task_digest_sha256": "a" * 64,
        })
        before = self.contents()
        result = self.archive.archive([task_id])
        self.assertTrue(self.archive.is_archived(task_id))
        self.assertEqual(self.archive.archived_tasks()["archived"], [{"task_id": task_id, "job_id": "job-1",
            "immutable_task_digest_sha256": "a" * 64, "provider_execution_identity_sha256": None,
            "consent_id": None}])
        for key, data in before.items():
            self.assertEqual(self.contents()[key], data)
        snapshot = self.root / "task-archives" / result["archived"][0]["archive_id"] / "snapshot.json"
        value = json.loads(snapshot.read_bytes())
        self.assertEqual(value["records"]["task.json"]["document"]["schema_version"], "vibapp.delivery-task.experimental-v1")
        self.assertNotIn("provider_execution_identity_sha256", value["records"]["task.json"]["document"])

    def test_legacy_changed_revision_retry_and_success_preservation(self):
        task_id, directory = self.task(1, ("failed", "failed"))
        self.make_legacy(directory)
        current = self.current(directory)
        path = current / "attempt.json"
        value = json.loads(path.read_bytes())
        value.update(immutable_task_digest_sha256="e" * 64, job_id="job-second", need_spec_revision=2)
        self.write(path, value)
        status_path = current / "codeagent/status.json"
        status = json.loads(status_path.read_bytes())
        self.write(status_path, {**status, "immutable_task_digest_sha256": "e" * 64, "job_id": "job-second"})
        self.archive.archive([task_id])
        self.assertTrue(self.archive.is_archived(task_id))
        self.assertEqual(self.archive.archived_tasks()["archived"][0]["job_id"], "job-second")
        value["status"] = "private-appstore-ready"
        self.write(path, value)
        self.assertFalse(self.archive.is_archived(task_id))

    def test_legacy_nonterminal_and_forged_fields_fail_closed(self):
        task_id, directory = self.task(1, ("running",))
        self.make_legacy(directory)
        with self.assertRaisesRegex(archive.ArchiveError, "task-not-terminal-failed"):
            self.archive.archive([task_id])
        path = directory / "task.json"
        value = json.loads(path.read_bytes())
        value["unknown"] = True
        self.write(path, value)
        with self.assertRaisesRegex(archive.ArchiveError, "legacy-task-binding-invalid"):
            self.archive.archive([task_id])

    def test_controller_run_and_adapter_locks_refuse_live_operations(self):
        task_id, directory = self.task()
        current = self.current(directory)
        for path in (self.root / ".delivery-controller.lock", current / ".run.lock", current / "codeagent/output/.execution.lock"):
            descriptor = os.open(path, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(archive.ArchiveError, "task-active"):
                    self.archive.archive([task_id])
            finally:
                os.close(descriptor)
        self.assertFalse((directory / ".archived.json").exists())

    def test_adapter_lock_before_status_file_is_still_checked(self):
        task_id, directory = self.task()
        current = self.current(directory)
        status = current / "codeagent/status.json"
        status.rename(current / "codeagent/prior-status.json")
        descriptor = os.open(current / "codeagent/output/.execution.lock", os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(archive.ArchiveError, "task-active"):
                self.archive.archive([task_id])
        finally:
            os.close(descriptor)

    def test_mixed_batch_is_preflighted_before_any_write(self):
        good, _ = self.task(1)
        bad, _ = self.task(2, ("running",))
        before = self.contents()
        with self.assertRaises(archive.ArchiveError):
            self.archive.archive([good, bad])
        self.assertEqual(before, self.contents())
        self.assertFalse((self.root / "task-archives").exists())

    def test_invalid_targets_and_unrecognized_root_refused(self):
        task_id, _ = self.task()
        for ids in ([], [task_id, task_id], ["../tasks"], ["development-" + "a" * 33], [True]):
            with self.assertRaises(archive.ArchiveError):
                self.archive.archive(ids)
        with self.assertRaisesRegex(archive.ArchiveError, "unrecognized-data-root"):
            archive.TaskArchive(self.data)

    def test_symlink_and_hardlink_documents_and_unsafe_modes_refused(self):
        task_id, directory = self.task()
        path = directory / "task.json"
        backup = self.data / "original-task.json"
        path.rename(backup)
        path.symlink_to(backup)
        with self.assertRaises(OSError):
            self.archive.archive([task_id])
        path.unlink()
        os.link(backup, path)
        with self.assertRaisesRegex(archive.ArchiveError, "unsafe-document"):
            self.archive.archive([task_id])
        path.unlink()
        backup.rename(path)
        path.chmod(0o666)
        with self.assertRaisesRegex(archive.ArchiveError, "unsafe-document"):
            self.archive.archive([task_id])

    def test_oversized_or_duplicate_json_never_archived(self):
        task_id, directory = self.task()
        path = directory / "task.json"
        original = path.read_bytes()
        self.write(path, original + b" " * archive.DOCUMENT_LIMIT)
        with self.assertRaisesRegex(archive.ArchiveError, "unsafe-document"):
            self.archive.archive([task_id])
        self.write(path, b'{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(archive.ArchiveError, "duplicate-json-key"):
            self.archive.archive([task_id])

    def test_invalid_missing_or_tampered_snapshot_never_hides(self):
        task_id, directory = self.task()
        result = self.archive.archive([task_id])
        path = directory / ".archived.json"
        original = path.read_bytes()
        marker = json.loads(original)
        for change in ({"unknown": "bad"}, {"archived_at_utc": "2026-02-30T00:00:00Z"},
                       {"archive_id": "../../elsewhere"}, {"task_record_sha256": "f" * 64}):
            self.write(path, {**marker, **change})
            self.assertFalse(self.archive.is_archived(task_id))
        self.write(path, original)
        snapshot = self.root / "task-archives" / result["archived"][0]["archive_id"] / "snapshot.json"
        self.write(snapshot, {"tampered": True})
        self.assertFalse(self.archive.is_archived(task_id))

    def test_existing_invalid_marker_is_never_overwritten(self):
        task_id, directory = self.task()
        marker = directory / ".archived.json"
        self.write(marker, {"invalid": True})
        before = self.contents()
        with self.assertRaisesRegex(archive.ArchiveError, "existing-marker-invalid"):
            self.archive.archive([task_id])
        self.assertEqual(before, self.contents())

    def test_batch_and_snapshot_memory_bounds_fail_before_writes(self):
        first, _ = self.task(1)
        second, _ = self.task(2)
        from unittest.mock import patch
        before = self.contents()
        with patch.object(archive, "BATCH_LIMIT", 1):
            with self.assertRaisesRegex(archive.ArchiveError, "archive-batch-limit"):
                self.archive.archive([first, second])
        self.assertEqual(before, self.contents())
        with patch.object(archive, "SNAPSHOT_LIMIT", 1):
            with self.assertRaisesRegex(archive.ArchiveError, "snapshot-limit"):
                self.archive.archive([first])


if __name__ == "__main__":
    unittest.main()
