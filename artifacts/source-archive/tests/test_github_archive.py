"""Synthetic API responses; no real credentials/network/repository writes."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import github_archive as archive


class FakeGitHub:
    def __init__(self):
        self.calls, self.repo, self.refs, self.trees, self.commits = [], None, {}, {}, {}
        self.fail = None
        self.tamper = False

    def request(self, method, path, token, body=None, missing_ok=False):
        self.calls.append((method, path, body))
        if self.fail and self.fail in path:
            raise archive.SetupError("synthetic_upload_failure")
        if path.endswith("/access_tokens"):
            return {"token": "SYNTHETIC-TOKEN"}
        if path == "/orgs/vib-app/repos":
            self.repo = {"id": 12345, "full_name": "vib-app/" + body["name"], "private": body["private"],
                         "owner": {"id": 6789, "type": "Organization"}}
            return self.repo
        if "/git/" not in path:
            return self.repo
        if method == "GET" and "/git/ref/" in path:
            return self.refs.get(path.split("/git/ref/")[1])
        if path.endswith("/git/blobs"):
            return {"sha": archive.blob_sha(base64.b64decode(body["content"]))}
        if path.endswith("/git/trees"):
            sha = "b" * 40
            self.trees[sha] = {"sha": sha, "truncated": False, "tree": body["tree"]}
            return {"sha": sha}
        if path.endswith("/git/commits"):
            sha = "c" * 40
            self.commits[sha] = {"sha": sha, "tree": {"sha": body["tree"]}}
            return self.commits[sha]
        if path.endswith("/git/refs"):
            result = {"object": {"type": "commit", "sha": body["sha"]}}
            self.refs[body["ref"].removeprefix("refs/")] = result
            return result
        if "/git/commits/" in path:
            return self.commits[path.rsplit("/", 1)[1]]
        if "/git/trees/" in path:
            result = self.trees[path.rsplit("/", 1)[1].split("?")[0]]
            if self.tamper:
                return {**result, "tree": result["tree"][:-1]}
            return result
        raise AssertionError("unexpected test endpoint")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vibapp-source-upload-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src/lib.rs").write_text("pub fn answer() -> u32 { 42 }\n")
        self.files = {"src/lib.rs": (self.root / "src/lib.rs").read_bytes()}
        self.digest = archive.source_digest(self.files)
        self.api = FakeGitHub()
        self.worker = archive.Archiver({"id": 123, "owner": {"id": 6789}},
                                      {"installation_id": 456}, self.api)
        signer = patch.object(archive, "app_jwt", return_value="SYNTHETIC-JWT")
        signer.start()
        self.addCleanup(signer.stop)

    def upload(self):
        return self.worker.upload("publisher.test", "ai.vibapp.test", "a" * 64, self.digest, self.root)

    def test_upload_and_readback_are_bound_to_source_package_owner(self):
        receipt = self.upload()
        self.assertEqual(receipt["organization"], "vib-app")
        self.assertEqual(receipt["source_digest_sha256"], self.digest)
        self.assertEqual(receipt["package_digest_sha256"], "a" * 64)
        self.assertTrue(self.api.repo["private"])
        tokens = [body for _, path, body in self.api.calls if path.endswith("access_tokens")]
        self.assertEqual(tokens[0]["permissions"], {"administration": "write", "contents": "write"})
        self.assertEqual(tokens[-1], {"permissions": {"contents": "write"}, "repository_ids": [12345]})
        self.assertEqual(receipt["repository"], archive.repository_name("publisher.test", "ai.vibapp.test"))
        self.assertNotIn("TOKEN", str(receipt))

    def test_replay_rechecks_tree_without_duplicate_commits(self):
        first = self.upload()
        self.api.calls.clear()
        self.assertEqual(first, self.upload())
        self.assertFalse(any(method == "POST" and "/git/" in path for method, path, _ in self.api.calls))
        self.assertTrue(any("recursive=1" in path for _, path, _ in self.api.calls))

    def test_prebuild_staging_is_not_a_publication_receipt(self):
        receipt = self.worker.stage_source("publisher.test", "ai.vibapp.test", self.digest, self.root)
        self.assertEqual(receipt["stage"], "untrusted-source-awaiting-builder")
        self.assertNotIn("package_digest_sha256", receipt)
        self.assertIn("tags/vibapp-source-" + self.digest, self.api.refs)

    def test_prebuild_replay_is_read_only_and_digest_bound(self):
        first = self.worker.stage_source("publisher.test", "ai.vibapp.test", self.digest, self.root)
        self.api.calls.clear()
        replay = self.worker.stage_source("publisher.test", "ai.vibapp.test", self.digest, self.root)
        self.assertEqual(replay, first)
        self.assertFalse(any(method == "POST" and "/git/" in path for method, path, _ in self.api.calls))

    def test_partial_failure_is_retryable_without_a_success_receipt(self):
        self.api.fail = "/git/commits"
        with self.assertRaises(archive.SetupError):
            self.upload()
        self.assertEqual(self.api.refs, {})
        self.api.fail = None
        self.assertEqual(self.upload()["commit_sha"], "c" * 40)

    def test_partial_empty_repository_is_initialized_once_without_overwrite(self):
        original = self.api.request
        recovered = []
        def request(method, path, token, body=None, missing_ok=False):
            if method == "GET" and "/git/ref/" in path and not recovered:
                raise archive.SetupError("archive_repository_empty")
            if method == "PUT":
                self.assertTrue(path.endswith("/contents/" + archive.BOOTSTRAP))
                self.assertNotIn("sha", body)
                recovered.append(path)
                return {}
            return original(method, path, token, body, missing_ok)
        self.api.request = request
        self.assertEqual(self.upload()["commit_sha"], "c" * 40)
        self.assertEqual(len(recovered), 1)

    def test_remote_tampering_fails_even_after_prior_success(self):
        self.upload()
        self.api.tamper = True
        with self.assertRaisesRegex(archive.SetupError, "archive_readback_mismatch"):
            self.upload()

    def test_public_or_other_owner_repository_rejected(self):
        self.upload()
        for change in ({"private": False}, {"owner": {"id": 9999}}, {"archived": True}):
            previous = self.api.repo
            self.api.repo = {**previous, **change}
            with self.subTest(change=change), self.assertRaisesRegex(archive.SetupError, "repository_mismatch"):
                self.upload()
            self.api.repo = previous

    def test_bad_source_fails_before_network(self):
        (self.root / "src/lib.rs").write_text("changed source")
        with self.assertRaisesRegex(archive.SetupError, "source_digest_mismatch"):
            self.upload()
        self.assertEqual(self.api.calls, [])

    def test_symlinks_secret_paths_and_private_keys_are_blocked(self):
        for name, content in ((".env", b"secret"), ("credentials.json", b"{}"),
                              ("secret.rs", b"-----BEGIN RSA PRIVATE KEY-----\nSYNTHETIC")):
            path = self.root / name
            path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(archive.SetupError):
                self.upload()
            path.unlink()
        (self.root / "link").symlink_to(self.root / "src")
        with self.assertRaisesRegex(archive.SetupError, "unsafe_source"):
            self.upload()
        self.assertEqual(self.api.calls, [])

    def test_identity_mapping_is_tenant_scoped(self):
        self.assertNotEqual(archive.repository_name("publisher.one", "app.one"),
                            archive.repository_name("publisher.two", "app.one"))
        with self.assertRaises(archive.SetupError):
            archive.repository_name("../vib-app", "app")

    def test_transport_refuses_arbitrary_hosts_or_deletion(self):
        api = archive.GitHub()
        for method, path in (("DELETE", "/repos/vib-app/app-" + "a" * 64),
                             ("PUT", "/repos/vib-app/app-" + "a" * 64 + "/contents/src/lib.rs"),
                             ("POST", "https://evil.test/"),
                             ("POST", "/repos/another-org/app-" + "a" * 64 + "/git/blobs")):
            with self.assertRaisesRegex(archive.SetupError, "archive_api_policy"):
                api.request(method, path, "SYNTHETIC")

    def test_selected_installation_repository_token_rejection_is_actionable(self):
        payload = {"message": "There is at least one repository that does not exist or is not accessible to the parent installation."}
        opener = MagicMock()
        opener.open.side_effect = HTTPError("https://api.github.com", 422, "SENSITIVE-RESPONSE",
                                            {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(archive, "build_opener", return_value=opener):
            with self.assertRaises(archive.SetupError) as failure:
                archive.GitHub().request("POST", "/app/installations/123/access_tokens", "SENSITIVE-TOKEN", {})
            self.assertEqual(failure.exception.code, "archive_repository_not_accessible_to_installation")
            self.assertNotIn("SENSITIVE", str(failure.exception))

    def test_known_api_failures_are_classified_without_logging_response_or_token(self):
        for status, payload, expected in (
            (422, {"errors": [{"field": "name", "message": "name already exists on this account"}]},
             "archive_repository_exists_but_not_accessible"),
            (422, {"errors": None, "message": "SENSITIVE-RESPONSE"}, "archive_github_create-repository_422"),
            (409, {"message": "Git Repository is empty."}, "archive_repository_empty"),
        ):
            opener = MagicMock()
            opener.open.side_effect = HTTPError("https://api.github.com", status, "SENSITIVE-RESPONSE",
                                                {}, io.BytesIO(json.dumps(payload).encode()))
            with self.subTest(status=status, expected=expected), patch.object(archive, "build_opener", return_value=opener):
                with self.assertRaises(archive.SetupError) as failure:
                    archive.GitHub().request("POST", "/orgs/vib-app/repos", "SENSITIVE-TOKEN", {})
                self.assertEqual(failure.exception.code, expected)
                self.assertNotIn("SENSITIVE", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
