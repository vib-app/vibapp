"""Offline protocol tests. Synthetic records do not certify a real candidate."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent / "app-builder"))
sys.path.insert(0, str(BASE.parent / "app-builder/tests"))
import package_release as publish
from app_builder import build_handoff, SafeFixtureRunner
from common import package_digest, canonical_json
from test_app_builder import make_handoff, COMPONENT


class FakeApi:
    def __init__(self):
        self.calls, self.uploads, self.public, self.blobs = [], [], {}, {}
        self.base = "a" * 40
        self.base_tree = "b" * 40
        self.readme = ("blob", "100644", publish.blob_sha(b"keep me"))
        self.trees = {self.base_tree: {"README.md": self.readme}}
        self.commits = {self.base: {"sha": self.base, "tree": {"sha": self.base_tree}, "message": "existing"}}
        self.refs = {"refs/heads/main": {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.base}}}
        self.release, self.assets = None, []
        self.drop_existing = False
        self.private = False

    def request(self, method, suffix, body=None, missing_ok=False, binary=False):
        self.calls.append((method, suffix, copy.deepcopy(body)))
        if method == "GET" and suffix == "":
            return {"id": publish.PACKAGES_REPOSITORY_ID, "full_name": "vib-app/packages",
                    "private": self.private, "default_branch": "main"}
        if suffix.startswith("/git/ref/"):
            return copy.deepcopy(self.refs.get("refs/" + suffix.removeprefix("/git/ref/")))
        if method == "GET" and suffix.startswith("/git/commits/"):
            return copy.deepcopy(self.commits[suffix.rsplit("/", 1)[-1]])
        if method == "GET" and suffix.startswith("/git/trees/"):
            sha = suffix.split("/")[-1].split("?")[0]
            return {"sha": sha, "truncated": False, "tree": [
                {"path": path, "type": row[0], "mode": row[1], "sha": row[2]}
                for path, row in self.trees[sha].items()]}
        if method == "POST" and suffix == "/git/blobs":
            import base64
            data = base64.b64decode(body["content"])
            sha = publish.blob_sha(data)
            self.blobs[sha] = data
            return {"sha": sha}
        if method == "POST" and suffix == "/git/trees":
            tree = {} if self.drop_existing else copy.deepcopy(self.trees[body["base_tree"]])
            for row in body["tree"]:
                tree[row["path"]] = (row["type"], row["mode"], row["sha"])
            sha = hashlib.sha1(publish.canonical(tree)).hexdigest()
            self.trees[sha] = tree
            return {"sha": sha}
        if method == "POST" and suffix == "/git/commits":
            sha = hashlib.sha1(publish.canonical(body)).hexdigest()
            self.commits[sha] = {"sha": sha, "tree": {"sha": body["tree"]}, "message": body["message"]}
            for path, row in self.trees[body["tree"]].items():
                if row[2] in self.blobs:
                    self.public[f"https://raw.githubusercontent.com/vib-app/packages/{sha}/{path}"] = self.blobs[row[2]]
            return {"sha": sha}
        if method == "POST" and suffix == "/git/refs":
            if body["ref"] in self.refs:
                raise publish.SetupError("github_role_http_422")
            self.refs[body["ref"]] = {"ref": body["ref"], "object": {"type": "commit", "sha": body["sha"]}}
            return copy.deepcopy(self.refs[body["ref"]])
        if method == "GET" and suffix.startswith("/releases/tags/"):
            return copy.deepcopy(self.release)
        if method == "POST" and suffix == "/releases":
            self.release = dict(body, id=1, html_url="https://github.com/vib-app/packages/releases/tag/" + body["tag_name"])
            return copy.deepcopy(self.release)
        if method == "GET" and suffix == "/releases/1/assets?per_page=100":
            return copy.deepcopy(self.assets)
        raise AssertionError((method, suffix, body))

    def upload_asset(self, release_id, name, payload, content_type):
        self.uploads.append(name)
        url = "https://github.com/vib-app/packages/releases/download/" + self.release["tag_name"] + "/" + name
        self.public[url] = payload
        asset = {"id": len(self.assets) + 1, "name": name, "size": len(payload), "state": "uploaded", "browser_download_url": url}
        self.assets.append(asset)
        return copy.deepcopy(asset)

    def download(self, url, limit):
        data = self.public[url]
        if len(data) > limit:
            raise publish.SetupError("download_limit")
        return data


def decode(data):
    """Independent small decoder for asserting emitted BEP19 wire layout."""
    def read(offset):
        token = data[offset:offset + 1]
        if token == b"i":
            end = data.index(b"e", offset)
            return int(data[offset + 1:end]), end + 1
        if token in (b"d", b"l"):
            offset += 1
            result = {} if token == b"d" else []
            while data[offset:offset + 1] != b"e":
                item, offset = read(offset)
                if token == b"d":
                    value, offset = read(offset)
                    result[item] = value
                else:
                    result.append(item)
            return result, offset + 1
        end = data.index(b":", offset)
        length = int(data[offset:end])
        return data[end + 1:end + 1 + length], end + 1 + length
    value, end = read(0)
    assert end == len(data)
    return value


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        handoff = make_handoff(self.root / "input")
        receipt = build_handoff(handoff, self.root / "pipeline", SafeFixtureRunner(COMPONENT.read_bytes()))
        self.candidate_dir = self.root / "candidate"
        self.candidate_dir.mkdir()
        (self.candidate_dir / "package").mkdir()
        self.path = self.candidate_dir / "candidate.json"
        for name in publish.PACKAGE_FILES:
            (self.candidate_dir / "package" / name).write_bytes((receipt.parent / "package" / name).read_bytes())
        self.manifest = json.loads((self.candidate_dir / "package/manifest.json").read_bytes())
        repo = publish.repository_name(self.manifest["app"]["publisher"]["id"], self.manifest["app"]["id"])
        self.run = {"run_id": 123, "run_url": f"https://github.com/vib-app/{repo}/actions/runs/123",
                    "workflow_commit": "1" * 40, "source_commit": "2" * 40}
        provenance_path = self.candidate_dir / "package/provenance.json"
        provenance = json.loads(provenance_path.read_bytes())
        provenance.update(execution_mode="github-actions-docker-experimental",
            tool_versions={"rust": "1.93.0", "cargo": "1.93.0", "target": "wasm32-wasip2"},
            resource_observations={"component_bytes": COMPONENT.stat().st_size},
            command=["github-actions", self.run["run_url"], "cargo", "build", "--release", "--target",
                     "wasm32-wasip2", "--locked", "--offline", "--jobs", "1"],
            isolation={"network": "none", "source_execution_observed": True, "production_isolation": False,
                       "builder_image": "sha256:" + "3" * 64, "source_commit": self.run["source_commit"]})
        provenance_path.write_bytes(canonical_json(provenance))
        self.manifest["artifacts"]["provenance"].update(sha256=publish.sha256(provenance_path.read_bytes()), size_bytes=provenance_path.stat().st_size)
        (self.candidate_dir / "package/manifest.json").write_bytes(canonical_json(self.manifest))
        self.record = {"schema_version": "vibapp.builder-candidate.experimental-v1", "document_type": "verifier-promoted-candidate",
            "state": "candidate-ready", "job_id": provenance["job_id"], "source_tree_sha256": provenance["source_tree_sha256"],
            "package_digest_sha256": package_digest(self.manifest), "package_directory": "package",
            "quarantine_receipt_sha256": "4" * 64, "authority": {"install": "daemon", "publish": "none"},
            "verification": {"authority": "independent-verifier", "verifier_version": "app-verifier.experimental-v1",
                "verified_at_utc": "2026-09-06T00:00:00Z", "checks": [{"id": check, "outcome": "pass", "tool": "synthetic-test", "detail": "synthetic-test"}
                for check in sorted(publish.CHECK_IDS)]}}
        for key, name in (("manifest", "manifest.json"), ("component", "component.wasm")):
            data = (self.candidate_dir / "package" / name).read_bytes()
            self.record[key] = {"path": name, "sha256": publish.sha256(data), "size_bytes": len(data)}
        self.path.write_bytes(canonical_json(self.record))
        self.source = {"organization": "vib-app", "repository": repo, "repository_id": 321,
                       "commit_sha": "5" * 40, "source_digest_sha256": self.record["source_tree_sha256"],
                       "package_digest_sha256": self.record["package_digest_sha256"]}
        self.api = FakeApi()
        self.out = self.root / "publisher"
        self.verifier_patch = patch.object(publish, "_run_independent_checks")
        self.verifier = self.verifier_patch.start()
        self.download_patch = patch.object(publish, "public_download", side_effect=self.api.download)
        self.download = self.download_patch.start()

    def tearDown(self):
        self.download_patch.stop()
        self.verifier_patch.stop()
        self.temporary.cleanup()

    def call(self):
        return publish.publish_candidate(self.path, self.source, self.run, self.out, api=self.api)

    def save_record(self):
        self.path.write_bytes(canonical_json(self.record))

    def test_publish_zip_and_multifile_torrent_exact_bytes(self):
        result = self.call()
        self.assertEqual(result["state"], "public-downloads-verified")
        self.assertEqual(result["appstore_publication"], "not-performed")
        files = publish.snapshot(self.path)
        with zipfile.ZipFile(io.BytesIO(self.api.public[result["asset_url"]])) as zipped:
            self.assertEqual(set(zipped.namelist()), publish.PAYLOAD_PATHS)
            for name, data in files.items():
                self.assertEqual(zipped.read(name), data)
        meta = decode(self.api.public[result["torrent_url"]])
        info = meta[b"info"]
        self.assertEqual(info[b"name"].decode(), result["package_digest_sha256"] + ".vibapp-candidate")
        self.assertEqual(meta[b"announce"], b"wss://tracker.webtorrent.dev")
        self.assertEqual(result["magnet_uri"], f"magnet:?xt=urn:btih:{result['info_hash']}&dn={info[b'name'].decode()}&tr=wss%3A%2F%2Ftracker.webtorrent.dev")
        joined = b""
        for row in info[b"files"]:
            name = b"/".join(row[b"path"]).decode()
            self.assertEqual(row[b"length"], len(files[name]))
            self.assertEqual(self.api.public[meta[b"url-list"][0].decode() + info[b"name"].decode() + "/" + name], files[name])
            joined += files[name]
        hashes = b"".join(hashlib.sha1(joined[i:i + info[b"piece length"]]).digest() for i in range(0, len(joined), info[b"piece length"]))
        self.assertEqual(info[b"pieces"], hashes)
        self.assertEqual(result["info_hash"], hashlib.sha1(publish.bencode(info)).hexdigest())
        self.assertNotEqual(result["raw_files_commit"], result["torrent_metadata_commit"])
        self.assertEqual(self.api.public[result["torrent_url"]], self.api.public[result["torrent_release_url"]])
        self.assertEqual(len(result["verified_downloads"]), 8)

    def test_existing_paths_preserved_no_main_update_or_delete(self):
        result = self.call()
        tree = self.api.trees[self.api.commits[result["raw_files_commit"]]["tree"]["sha"]]
        self.assertEqual(tree["README.md"], self.api.readme)
        self.assertEqual(self.api.refs["refs/heads/main"]["object"]["sha"], self.api.base)
        self.assertFalse(any(method in ("DELETE", "PATCH") for method, _, _ in self.api.calls))

    def test_retry_rechecks_without_writes(self):
        first = self.call()
        self.api.calls.clear()
        self.api.uploads.clear()
        self.download.reset_mock()
        self.assertEqual(self.call(), first)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
        self.assertEqual(self.api.uploads, [])
        self.assertEqual(self.download.call_count, 8)

    def test_tampered_component_fails_before_api(self):
        (self.candidate_dir / "package/component.wasm").write_bytes(b"bad")
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_traversal_descriptor_fails(self):
        self.record["component"]["path"] = "../source.rs"
        self.save_record()
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_symlink_and_hardlink_files_fail(self):
        component = self.candidate_dir / "package/component.wasm"
        original = self.root / "original.wasm"
        component.rename(original)
        component.symlink_to(original)
        with self.assertRaises((publish.SetupError, OSError)):
            self.call()
        component.unlink()
        os.link(original, component)
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_symlink_directory_fails(self):
        (self.candidate_dir / "package").rename(self.root / "real-package")
        (self.candidate_dir / "package").symlink_to(self.root / "real-package", target_is_directory=True)
        with self.assertRaises((publish.SetupError, OSError)):
            self.call()

    def test_extra_private_source_and_empty_directory_fail(self):
        extra = self.candidate_dir / "source"
        extra.mkdir()
        with self.assertRaisesRegex(publish.SetupError, "tree_boundary"):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_embedded_source_field_rejected(self):
        self.record["source_code"] = "private source text"
        self.save_record()
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_secret_marker_rejected(self):
        self.record["verification"]["checks"][0]["detail"] = "-----BEGIN PRIVATE KEY-----"
        self.save_record()
        with self.assertRaisesRegex(publish.SetupError, "secret_detected"):
            self.call()

    def test_source_receipt_digest_mismatch(self):
        self.source["source_digest_sha256"] = "0" * 64
        with self.assertRaisesRegex(publish.SetupError, "source_digest_mismatch"):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_source_receipt_private_raw_attachment_fails(self):
        self.source["raw_source"] = "not for publication"
        with self.assertRaisesRegex(publish.SetupError, "source_receipt"):
            self.call()

    def test_run_binding_conflict(self):
        self.run["source_commit"] = "f" * 40
        with self.assertRaisesRegex(publish.SetupError, "provenance_binding"):
            self.call()

    def test_independent_verifier_failure_prevents_network(self):
        self.verifier.side_effect = publish.SetupError("package_independent_verification_failed")
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_state_string_does_not_bypass_verifier(self):
        self.call()
        self.verifier.assert_called_once()

    def test_missing_verifier_evidence_fails(self):
        self.record["verification"]["checks"] = self.record["verification"]["checks"][:1]
        self.save_record()
        with self.assertRaisesRegex(publish.SetupError, "verifier_checks"):
            self.call()

    def test_duplicate_json_keys_rejected(self):
        self.path.write_bytes(b'{"state":"candidate-ready","state":"candidate-ready"}')
        with self.assertRaises(publish.SetupError):
            self.call()

    def test_conflicting_local_retry_fails_before_network(self):
        self.call()
        self.source["commit_sha"] = "f" * 40
        self.api.calls.clear()
        with self.assertRaisesRegex(publish.SetupError, "retry_conflict"):
            self.call()
        self.assertEqual(self.api.calls, [])

    def test_conflicting_remote_retry_fails_even_without_local_state(self):
        self.call()
        self.out = self.root / "new-state"
        self.source["commit_sha"] = "f" * 40
        with self.assertRaisesRegex(publish.SetupError, "retry_conflict"):
            self.call()

    def test_failed_raw_download_never_success(self):
        self.download.side_effect = publish.SetupError("download_network")
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(list(self.out.rglob("receipt.json")), [])
        self.assertIsNone(self.api.release)

    def test_failed_asset_download_never_success_then_safe_retry(self):
        def fail_asset(url, limit):
            if "/releases/download/" in url:
                raise publish.SetupError("download_network")
            return self.api.download(url, limit)
        self.download.side_effect = fail_asset
        with self.assertRaises(publish.SetupError):
            self.call()
        self.assertEqual(list(self.out.rglob("receipt.json")), [])
        prior_uploads = list(self.api.uploads)
        self.download.side_effect = self.api.download
        self.call()
        for name in prior_uploads:
            self.assertEqual(self.api.uploads.count(name), 1)

    def test_failed_raw_torrent_download_never_creates_release(self):
        def fail_metadata(url, limit):
            if "/metadata/" in url:
                return b"corrupted torrent"
            return self.api.download(url, limit)
        self.download.side_effect = fail_metadata
        with self.assertRaisesRegex(publish.SetupError, "torrent_download_mismatch"):
            self.call()
        self.assertIsNone(self.api.release)
        self.assertEqual(list(self.out.rglob("receipt.json")), [])

    def test_metadata_commit_retry_conflict(self):
        result = self.call()
        self.api.commits[result["torrent_metadata_commit"]]["message"] = "wrong binding"
        self.api.uploads.clear()
        with self.assertRaisesRegex(publish.SetupError, "retry_conflict"):
            self.call()
        self.assertEqual(self.api.uploads, [])

    def test_existing_asset_tampering_fails_without_overwrite(self):
        first = self.call()
        self.api.public[first["asset_url"]] = b"bad"
        self.api.uploads.clear()
        with self.assertRaisesRegex(publish.SetupError, "download_mismatch"):
            self.call()
        self.assertEqual(self.api.uploads, [])

    def test_wrong_release_metadata_fails(self):
        self.call()
        self.api.release["body"] = "unbound"
        with self.assertRaisesRegex(publish.SetupError, "release_conflict"):
            self.call()

    def test_git_preservation_failure_stops_before_ref(self):
        self.api.drop_existing = True
        with self.assertRaisesRegex(publish.SetupError, "preservation_failed"):
            self.call()
        self.assertEqual(set(self.api.refs), {"refs/heads/main"})

    def test_private_packages_repo_rejected(self):
        self.api.private = True
        with self.assertRaisesRegex(publish.SetupError, "repository_mismatch"):
            self.call()
        self.assertFalse(any(method != "GET" for method, _, _ in self.api.calls))

    def test_oversize_candidate_rejected(self):
        self.path.write_bytes(b"x" * (publish.MAX_JSON + 1))
        with self.assertRaisesRegex(publish.SetupError, "unsafe_file"):
            self.call()

    def test_zip_determinism(self):
        files = publish.snapshot(self.path)
        self.assertEqual(publish.archive(files), publish.archive(dict(reversed(list(files.items())))))

    def test_fifo_state_is_rejected_without_blocking(self):
        state = self.out / self.record["package_digest_sha256"]
        state.mkdir(parents=True)
        os.mkfifo(state / "binding.json")
        with self.assertRaisesRegex(publish.SetupError, "state_path"):
            self.call()


if __name__ == "__main__":
    unittest.main()
