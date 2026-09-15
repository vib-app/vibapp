from __future__ import annotations
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_app_download as download
from test_local_appstore import write_candidate


class PublicDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        candidate, digest = write_candidate(self.root / "original")
        metadata = json.loads(candidate.read_bytes())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.write(candidate, "candidate.json")
            for path in sorted((candidate.parent / "package").iterdir()):
                archive.write(path, f"package/{path.name}")
        self.data = buf.getvalue()
        self.item = {
            "app_id": "ai.vibapp.local-store-test", "version": "0.1.0",
            "publication_state": "published", "verification_state": "package-verified",
            "package_digest_sha256": digest, "component_sha256": metadata["component"]["sha256"],
            "source": {"organization": "vib-app", "repository": "sources", "repository_id": 1371257005,
                       "package_digest_sha256": digest, "source_digest_sha256": metadata["source_tree_sha256"], "commit_sha": "a" * 40},
            "source_url": "https://github.com/vib-app/sources/tree/" + "a" * 40,
            "download": {"url": f"https://github.com/vib-app/packages/releases/download/vibapp-package-{digest}/{digest}.zip",
                         "sha256": hashlib.sha256(self.data).hexdigest(), "size_bytes": len(self.data)},
        }

    def select(self, items):
        return download.select_app(json.dumps({"schema_version": "vibapp.store-catalog.v1", "apps": items}).encode(), self.item["app_id"])

    def test_real_shape_stages_as_private_candidate_without_install(self):
        self.assertEqual(self.select([self.item]), self.item)
        destination = self.root / "staged"
        destination.mkdir()
        candidate = download.stage_archive(self.data, self.item, destination)
        result = download.LocalAppStore(self.root / "store").ingest(candidate)
        self.assertEqual(result["record"]["authority"], {"install": "daemon", "publish": "none"})
        self.assertFalse((self.root / "runtime-daemon").exists())

    def test_missing_duplicate_revoked_and_external_download_are_rejected(self):
        cases = [[], [self.item, self.item]]
        for path, value in [("publication_state", "revoked"), ("verification_state", "pending")]:
            item = copy.deepcopy(self.item)
            item[path] = value
            cases.append([item])
        item = copy.deepcopy(self.item)
        item["download"]["url"] = "http://127.0.0.1/private"
        cases.append([item])
        for items in cases:
            with self.assertRaises(download.StoreError):
                self.select(items)

    def test_archive_hash_and_identity_must_match(self):
        destination = self.root / "staged"
        destination.mkdir()
        with self.assertRaises(download.StoreError):
            download.stage_archive(self.data + b"x", self.item, destination)
        item = copy.deepcopy(self.item)
        item["app_id"] = "ai.vibapp.wrong"
        with self.assertRaises(download.StoreError):
            download.stage_archive(self.data, item, destination)

    def test_zip_traversal_links_and_duplicates_fail_before_extraction(self):
        for name, link in [("../escape", False), ("package/../../escape", False), ("package/link", True), ("candidate.json", False)]:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr("candidate.json", b"{}")
                entry = zipfile.ZipInfo(name)
                if link:
                    entry.create_system = 3
                    entry.external_attr = 0o120777 << 16
                archive.writestr(entry, b"target")
            data = buf.getvalue()
            item = copy.deepcopy(self.item)
            item["download"].update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
            with self.assertRaises(download.StoreError):
                download.stage_archive(data, item, self.root / "never-created")
            self.assertFalse((self.root / "never-created").exists())


if __name__ == "__main__":
    unittest.main()
