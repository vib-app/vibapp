from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import test_task_archive as fixtures

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("readonly_delivery_history_test", BASE / "delivery_history.py")
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
import delivery_controller as delivery


class DeliveryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskArchiveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def legacy(self, number=1, states=("private-appstore-ready",)):
        task_id, directory = self.fixture.task(number, states)
        self.fixture.make_legacy(directory)
        return task_id, directory

    def controller(self):
        # Read-only history needs neither provider construction nor a writable
        # DeliveryController constructor. All other dependencies are absent.
        controller = object.__new__(delivery.DeliveryController)
        controller.root = self.root
        controller.tasks = self.root / "tasks"
        return controller

    def test_successful_failed_and_unproven_running_legacy_records_remain_original(self):
        for index, state in enumerate(("private-appstore-ready", "failed", "running", "queued"), 1):
            task_id, directory = self.legacy(index, (state,))
            expected_task = json.loads((directory / "task.json").read_bytes())
            expected_attempt = json.loads((self.fixture.current(directory) / "attempt.json").read_bytes())
            before = self.fixture.contents()
            result = self.controller().history(task_id)
            self.assertEqual(result, {"schema_version": delivery.SCHEMA_VERSION, "task": expected_task,
                                     "attempts": [expected_attempt], "legacy_read_only": True})
            self.assertEqual(result["attempts"][0]["status"], state)
            self.assertNotIn("consent_id", result["attempts"][0])
            self.assertNotIn("provider_execution_identity_sha256", result["attempts"][0])
            self.assertEqual(before, self.fixture.contents())

    def test_legacy_history_changes_no_permissions_or_timestamps_and_opens_readonly(self):
        task_id, directory = self.legacy()
        directory.chmod(0o755)
        contents_before = self.fixture.contents()
        before = {str(path): (path.stat().st_mode, path.stat().st_mtime_ns)
                  for path in self.fixture.data.rglob("*")}
        real_open = os.open

        def read_only_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(history.os, "open", side_effect=read_only_open), \
             mock.patch.object(history.os, "chmod", side_effect=AssertionError("history chmod")), \
             mock.patch.object(history.os, "rename", side_effect=AssertionError("history rename")), \
             mock.patch.object(history.os, "mkdir", side_effect=AssertionError("history mkdir")):
            history.read_legacy_history(self.root, task_id)
            result = self.controller().history(task_id)
            self.assertTrue(result["legacy_read_only"])
        after = {str(path): (path.stat().st_mode, path.stat().st_mtime_ns)
                 for path in self.fixture.data.rglob("*")}
        self.assertEqual(before, after)
        self.assertEqual(contents_before, self.fixture.contents())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_edited_revision_and_job_identity_in_legacy_attempts_are_not_rewritten(self):
        task_id, directory = self.legacy(1, ("failed", "private-appstore-ready"))
        path = self.fixture.current(directory) / "attempt.json"
        value = json.loads(path.read_bytes())
        value.update(need_spec_revision=2, job_id="job-revised", immutable_task_digest_sha256="e" * 64)
        self.fixture.write(path, value)
        result = self.controller().history(task_id)
        self.assertEqual([item["job_id"] for item in result["attempts"]], ["job-1", "job-revised"])
        self.assertEqual([item["immutable_task_digest_sha256"] for item in result["attempts"]], ["a" * 64, "e" * 64])
        self.assertEqual(result["task"]["schema_version"], history.TASK_VERSION)
        self.assertNotIn("job_id", result["task"])

    def test_modern_history_keeps_existing_shape_and_strict_execution_validation(self):
        task_id, _ = self.fixture.task()
        self.assertIsNone(history.read_legacy_history(self.root, task_id))
        with mock.patch.object(delivery.importlib.util, "spec_from_file_location",
                               side_effect=AssertionError("modern history must not load the legacy reader")):
            result = self.controller().history(task_id)
        self.assertNotIn("legacy_read_only", result)
        self.assertEqual(result["task"]["schema_version"], delivery.TASK_VERSION)

    def test_status_and_run_still_refuse_legacy_as_execution_authority(self):
        task_id, directory = self.legacy()
        controller = self.controller()
        with self.assertRaises(delivery.DeliveryError):
            controller.status(task_id)
        with self.assertRaises(delivery.DeliveryError):
            controller._run_attempt_under_budget(task_id)
        with self.assertRaises(delivery.DeliveryError):
            delivery._validate_task_record(json.loads((directory / "task.json").read_bytes()))

    def test_duplicate_keys_unknown_fields_and_fake_modern_authority_are_rejected(self):
        task_id, directory = self.legacy()
        path = directory / "task.json"
        original = path.read_bytes()
        self.fixture.write(path, b'{"schema_version":"vibapp.delivery-task.experimental-v1","schema_version":"other"}')
        with self.assertRaises(history.HistoryError):
            history.read_legacy_history(self.root, task_id)
        self.fixture.write(path, original)
        attempt_path = self.fixture.current(directory) / "attempt.json"
        value = json.loads(attempt_path.read_bytes())
        self.fixture.write(attempt_path, {**value, "consent_id": "invented-consent"})
        with self.assertRaisesRegex(history.HistoryError, "legacy-attempt-binding-invalid"):
            history.read_legacy_history(self.root, task_id)

    def test_cross_task_or_need_attempts_and_broken_lineage_are_rejected(self):
        task_id, directory = self.legacy()
        path = self.fixture.current(directory) / "attempt.json"
        value = json.loads(path.read_bytes())
        for change in ({"task_id": "development-" + "f" * 32}, {"need_id": "other-need"},
                       {"attempt_number": 2}, {"retry_of_attempt_id": "attempt-0001-" + "f" * 16},
                       {"created_at_utc": "2026-02-30T00:00:00Z"}):
            self.fixture.write(path, {**value, **change})
            with self.assertRaises(history.HistoryError):
                history.read_legacy_history(self.root, task_id)

    def test_unindexed_attempt_directory_is_not_silently_ignored_or_reconciled(self):
        task_id, directory = self.legacy()
        self.fixture.mkdir(directory / "attempts/attempt-0002-0000000000000002")
        before = self.fixture.contents()
        with self.assertRaisesRegex(history.HistoryError, "legacy-attempt-index-stale"):
            history.read_legacy_history(self.root, task_id)
        self.assertEqual(before, self.fixture.contents())

    def test_task_and_attempt_links_are_rejected(self):
        task_id, directory = self.legacy()
        path = self.fixture.current(directory) / "attempt.json"
        backup = self.fixture.data / "original-attempt.json"
        path.rename(backup)
        path.symlink_to(backup)
        with self.assertRaises(OSError):
            history.read_legacy_history(self.root, task_id)
        path.unlink()
        os.link(backup, path)
        with self.assertRaisesRegex(history.HistoryError, "legacy-history-unsafe-document"):
            history.read_legacy_history(self.root, task_id)

    def test_size_limits_and_wrong_root_task_ids_are_bounded(self):
        task_id, directory = self.legacy()
        path = directory / "task.json"
        self.fixture.write(path, path.read_bytes() + b" " * history.MAX_DOCUMENT_BYTES)
        with self.assertRaisesRegex(history.HistoryError, "legacy-history-unsafe-document"):
            history.read_legacy_history(self.root, task_id)
        for invalid in ("../tasks", "development-" + "a" * 33, True):
            with self.assertRaises(history.HistoryError):
                history.read_legacy_history(self.root, invalid)

    def test_history_total_limit_rejects_without_truncating_attempts(self):
        task_id, _ = self.legacy(1, ("failed", "failed"))
        with mock.patch.object(history, "MAX_HISTORY_BYTES", 1):
            with self.assertRaisesRegex(history.HistoryError, "legacy-history-size-limit"):
                history.read_legacy_history(self.root, task_id)


if __name__ == "__main__":
    unittest.main()
