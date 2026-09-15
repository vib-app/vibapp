"""Synthetic audit-tool tests, never application acceptance or user-state writes."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import audit_existing_apps as audit


class ExistingAppAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-audit-unit-")
        self.root = Path(self.temporary.name).resolve()
        self.store = self.root / "local-appstore"
        self.store.mkdir(mode=0o700)
        (self.store / ".local-appstore.lock").touch(mode=0o600)
        self.digest = "a" * 64
        self.app_id = "ai.vibapp.synthetic-audit"
        self.package = self.store / "candidates" / self.digest / "package"
        self.package.mkdir(parents=True)
        self.candidate = self.package.parent / "candidate.json"
        self.candidate.write_text(json.dumps({"package_digest_sha256": self.digest}))
        (self.package / "manifest.json").write_text(json.dumps({
            "app": {"id": self.app_id, "display_name": "Synthetic", "kind": "ui"},
            "entrypoints": [{"id": "main", "kind": "launcher-ui", "profiles": ["desktop"],
                             "routes": {"initial": "home"}}], "capabilities": [],
        }))
        connection = sqlite3.connect(self.store / "registry.sqlite3")
        connection.execute("CREATE TABLE releases (app_id TEXT, app_version TEXT, package_digest TEXT, state TEXT, record_json TEXT)")
        connection.execute("INSERT INTO releases VALUES (?,?,?,?,?)", (
            self.app_id, "0.1.0", self.digest, "private", json.dumps({"app": {"id": self.app_id}})))
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.stat().st_mode, path.stat().st_mtime_ns,
                                                  path.read_bytes() if path.is_file() else None)
                for path in [self.root, *self.root.rglob("*")]}

    def test_inventory_preserves_app_id_and_all_source_bytes_modes_and_mtimes(self):
        before = self.snapshot()
        result = audit.freeze_catalog("native", self.root)
        self.assertEqual(self.snapshot(), before)
        row = result["apps"][0]
        self.assertEqual(row["app_id"], self.app_id)
        self.assertNotIn("id", row)
        self.assertEqual(row["source_candidate"], str(self.candidate))
        self.assertEqual(row["candidate_sha256"], hashlib.sha256(self.candidate.read_bytes()).hexdigest())
        self.assertIsNone(row["inventory_error"])

    def test_withdrawn_negative_control_remains_separate_and_unmodified(self):
        connection = sqlite3.connect(self.store / "registry.sqlite3")
        connection.execute("UPDATE releases SET state='withdrawn'")
        connection.commit()
        connection.close()
        before = self.snapshot()
        self.assertEqual(audit.freeze_catalog("web", self.root)["apps"][0]["catalog_state"], "withdrawn")
        self.assertEqual(self.snapshot(), before)

    def test_journal_cannot_be_ignored_for_an_immutable_inventory(self):
        journal = self.store / "registry.sqlite3-wal"
        journal.write_bytes(b"synthetic-uncommitted-evidence")
        with self.assertRaisesRegex(ValueError, "unproven journal"):
            audit.freeze_catalog("native", self.root)

    def test_package_symlink_is_an_inventory_error_not_followed(self):
        manifest = self.package / "manifest.json"
        replacement = self.root / "outside-manifest.json"
        manifest.rename(replacement)
        manifest.symlink_to(replacement)
        result = audit.freeze_catalog("native", self.root)
        self.assertIsNotNone(result["apps"][0]["inventory_error"])

    def test_oversized_or_duplicate_documents_fail_closed(self):
        self.candidate.write_text('{"package_digest_sha256":"' + self.digest + '","package_digest_sha256":"' + self.digest + '"}')
        result = audit.freeze_catalog("native", self.root)
        self.assertIn("Duplicate", result["apps"][0]["inventory_error"]["message"])
        with self.assertRaises(ValueError):
            audit.bounded_bytes(self.candidate, 2)

    def test_identity_mismatch_is_preserved_as_an_exact_inventory_failure(self):
        self.candidate.write_text(json.dumps({"package_digest_sha256": "b" * 64}))
        row = audit.freeze_catalog("native", self.root)["apps"][0]
        self.assertEqual(row["app_id"], self.app_id)
        self.assertIn("identity differs", row["inventory_error"]["message"])

    def test_changed_frozen_candidate_is_not_installed_and_cleanup_is_recorded(self):
        row = audit.freeze_catalog("native", self.root)["apps"][0]
        directory = self.root / "child-output"
        directory.mkdir(mode=0o700)
        self.candidate.write_bytes(self.candidate.read_bytes() + b" ")
        result = audit.audit_child(row, directory, Path("/bin/sleep"))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("record changed", result["exception"]["message"])
        self.assertEqual(result["cleanup"], {"worker_count": 0, "all_reaped": True, "errors": []})
        self.assertTrue((directory / "result.json").is_file())

    def test_partial_daemon_constructor_cleans_an_already_spawned_child(self):
        # A harmless bounded sleep stands in for a native worker process solely
        # to test ownership/cleanup; no guest package is executed.
        sys.path.insert(0, str(audit.ARTIFACTS / "registry-store"))
        sys.path.insert(0, str(audit.ARTIFACTS / "runtime-daemon"))
        import local_appstore
        from vibapp_daemon import core
        row = audit.freeze_catalog("native", self.root)["apps"][0]
        directory = self.root / "constructor-failure"
        directory.mkdir(mode=0o700)
        workers = []
        def partial_constructor(*_args, **_kwargs):
            workers.append(subprocess.Popen(["/bin/sleep", "5"]))
            raise RuntimeError("synthetic constructor failed after worker spawn")
        store = Mock()
        store.candidates = directory / "product/local-appstore/candidates"
        with patch.object(local_appstore, "LocalAppStore", return_value=store), \
             patch.object(core, "RuntimeDaemon", side_effect=partial_constructor):
            result = audit.audit_child(row, directory, Path("/bin/sleep"))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("constructor failed", result["exception"]["message"])
        self.assertEqual(result["cleanup"]["worker_count"], 1)
        self.assertTrue(result["cleanup"]["all_reaped"])
        self.assertIsNotNone(workers[0].poll())
        self.assertEqual(len(json.loads((directory / "worker-handles.json").read_bytes())), 1)

    def test_outer_cleanup_refuses_changed_or_unbound_process_identity(self):
        directory = self.root / "outer-cleanup"
        directory.mkdir()
        audit.write_evidence(directory / "worker-handles.json", [{"pid": 12345, "identity": "old identity"}])
        with patch.object(audit, "process_identity", return_value="different identity"), patch.object(os, "kill") as kill:
            self.assertEqual(audit.cleanup_orphans(directory)[0]["status"], "unproven-not-killed")
        kill.assert_not_called()

    def test_outer_cleanup_checks_exact_output_root_before_kill(self):
        directory = self.root / "outer-cleanup"
        directory.mkdir()
        identity = "start /runtime --state-directory /another-audit/product/runtime-daemon"
        audit.write_evidence(directory / "worker-handles.json", [{"pid": 12345, "identity": identity}])
        with patch.object(audit, "process_identity", return_value=identity), patch.object(os, "kill") as kill:
            self.assertEqual(audit.cleanup_orphans(directory)[0]["status"], "unproven-not-killed")
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
