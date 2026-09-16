"""Native Windows storage tests; synthetic fixtures never execute a guest."""
from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "runtime-daemon"))


@unittest.skipUnless(os.name == "nt", "requires real Windows ownership and DACL APIs")
class WindowsStoreStorageTests(unittest.TestCase):
    def make_publicly_readable(self, path: Path) -> None:
        from vibapp_daemon import windows_security as security
        descriptor = security.P()
        sid = security.process_sid()
        security.checked(security._from_sddl(f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FR;;;WD)",
                                            1, security.ctypes.byref(descriptor), None))
        try:
            security.checked(security._set_file(str(path), 4 | 0x80000000, descriptor))
        finally:
            security._free(descriptor)

    def test_store_snapshot_and_daemon_stage_keep_exact_private_owner(self):
        from local_appstore import LocalAppStore
        from test_local_appstore import write_candidate
        from vibapp_daemon import windows_security as security
        from vibapp_daemon.core import RuntimeDaemon
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            security.protect(root, directory=True)
            candidate, digest = write_candidate(root / "input", component=b"\x00asm\r\n\x1a\x00storage-test-only")
            store = LocalAppStore(root / "store")
            result = store.ingest(candidate)
            self.assertFalse(store.ingest(candidate)["created"])
            self.assertEqual(len(store.list()["items"]), 1)
            for path in (store.root, store.candidates, store.database, store.lock_path, *store.candidates.rglob("*")):
                security.verify(path, directory=path.is_dir())
                if path.is_file():
                    self.assertFalse(path.stat().st_file_attributes & 1, "Store copies must permit bounded staging cleanup")
            record = result["record"]
            promotion = store.root / record["paths"]["candidate"]
            daemon = RuntimeDaemon(root / "runtime", store.candidates)
            try:
                outcome = daemon.execute({"request_id": "storage-install", "idempotency_key": "storage-install",
                                          "client": "cli", "subject": None, "command": {"tag": "install", "value": {
                                              "package_digest_sha256": digest, "enable_after_install": False}}},
                                         principal="sid:" + security.process_sid(), allowed_apps={"*"}, promotion_record=promotion)
                self.assertNotIn("error", outcome, outcome)
                for path in daemon.packages_root.rglob("*"):
                    security.verify(path, directory=path.is_dir())
                state = json.loads(daemon.state_path.read_bytes())
                app_id = record["app"]["id"]
                self.assertEqual(state["apps"][app_id]["package_path"], f"packages/{app_id}/{digest}")
                security.verify(daemon.data_root / app_id, directory=True)
                status = daemon.execute({"request_id": "storage-status", "idempotency_key": "storage-status",
                                         "client": "cli", "subject": app_id, "command": {"tag": "status", "value": None}},
                                        principal="sid:" + security.process_sid(), allowed_apps={"*"})
                self.assertNotIn("error", status, status)
                self.assertFalse(status["outcome"]["value"]["enabled"])
            finally:
                daemon.shutdown()
            restored = RuntimeDaemon(root / "runtime", store.candidates)
            try:
                status = restored.execute({"request_id": "storage-reopened", "idempotency_key": "storage-reopened",
                                           "client": "cli", "subject": app_id, "command": {"tag": "status", "value": None}},
                                          principal="sid:" + security.process_sid(), allowed_apps={"*"})
                self.assertNotIn("error", status, status)
                self.assertFalse(status["outcome"]["value"]["enabled"])
            finally:
                restored.shutdown()

    def test_existing_public_root_is_rejected_without_permission_repair(self):
        from local_appstore import LocalAppStore, StoreError
        from vibapp_daemon import windows_security as security
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            security.protect(root, directory=True)
            self.make_publicly_readable(root)
            try:
                with self.assertRaises(StoreError):
                    LocalAppStore(root)
                with self.assertRaises(PermissionError):
                    security.verify(root, directory=True)
                self.assertEqual(list(root.iterdir()), [])
            finally:
                security.protect(root, directory=True)

    def test_existing_public_database_is_rejected_without_permission_repair(self):
        from local_appstore import LocalAppStore, StoreError
        from vibapp_daemon import windows_security as security
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            security.protect(root, directory=True)
            store = LocalAppStore(root / "store")
            self.make_publicly_readable(store.database)
            try:
                with self.assertRaises(StoreError):
                    LocalAppStore(store.root)
                with self.assertRaises(PermissionError):
                    security.verify(store.database, directory=False)
            finally:
                security.protect(store.database)


if __name__ == "__main__":
    unittest.main()
