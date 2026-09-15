"""Trusted source archival worker; never imported or invoked by generated code.

Uploads one frozen, digest-checked source tree to an organization-owned archive.
New public publications use the shared public sources repository; legacy private
archives remain readable and are never converted or deleted. Successful receipts
are emitted only after GitHub read-back. No App
Store publication, Git hooks, shell/git execution, workflow or deletion APIs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from github_app_setup import (CredentialStore, DEFAULT_STORE, NoRedirect, ORG, SetupError,
                              app_jwt, credential_record, unique_object, verify_installation)

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PATH = re.compile(r"[A-Za-z0-9._/-]{1,240}\Z")
MARKER = ".vibapp-source-archive.json"
BOOTSTRAP = ".vibapp-archive-bootstrap"
SOURCES_REPOSITORY = "sources"
MAX_SOURCE = 16 * 1024 * 1024
MAX_FILE = 1024 * 1024
FORBIDDEN = {".git", ".ssh", ".aws", ".codex", ".env", "node_modules", "target", "credentials.json"}
SECRET = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                    rb"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{24,}")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def repository_name(publisher_id, app_id):
    if not IDENTITY.fullmatch(publisher_id) or not IDENTITY.fullmatch(app_id):
        raise SetupError("invalid_source_identity")
    return "app-" + sha256((publisher_id + "\n" + app_id).encode())


def source_repository_matches(repository, publisher_id, app_id):
    """Only the fixed public archive or this app's exact legacy private archive."""
    legacy = repository_name(publisher_id, app_id)  # Validate identity even for sources.
    return repository in (SOURCES_REPOSITORY, legacy)


def source_digest(files):
    # Same contract as app-builder/common.py:source_tree_digest.
    result = hashlib.sha256(b"VIBAPP-CODEAGENT-SOURCE\0")
    for path, data in sorted(files.items(), key=lambda item: item[0].encode()):
        result.update(path.encode() + b"\0" + hashlib.sha256(data).digest() + len(data).to_bytes(8, "big"))
    return result.hexdigest()


def snapshot(root: Path, expected_digest: str):
    if not SHA256.fullmatch(expected_digest):
        raise SetupError("invalid_source_digest")
    if root.is_symlink() or not root.is_dir():
        raise SetupError("unsafe_source")
    files, total, directories = {}, 0, 0
    for parent, dirs, names, directory_fd in os.fwalk(root, follow_symlinks=False):
        directories += 1
        if directories > 512 or len(dirs) + len(names) > 512:
            raise SetupError("source_limit")
        for name in dirs + names:
            if name in FORBIDDEN or name.startswith(".env.") or name == MARKER:
                raise SetupError("forbidden_source_file")
            if stat.S_ISLNK(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                raise SetupError("unsafe_source")
        for name in names:
            relative = (Path(parent) / name).relative_to(root).as_posix()
            if not PATH.fullmatch(relative) or any(part in ("", ".", "..") for part in relative.split("/")):
                raise SetupError("unsafe_source_path")
            if relative.startswith(".github/workflows/") or name.endswith((".pem", ".key", ".p12")):
                raise SetupError("forbidden_source_file")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
                    raise SetupError("unsafe_source")
                data = stream.read(MAX_FILE + 1)
            total += len(data)
            if len(data) > MAX_FILE or total > MAX_SOURCE or len(files) >= 512:
                raise SetupError("source_limit")
            if SECRET.search(data):
                raise SetupError("source_secret_detected")
            files[relative] = data
    if not files or source_digest(files) != expected_digest:
        raise SetupError("source_digest_mismatch")
    return files


class GitHub:
    """Fixed-host, bounded API transport. Tokens never leave process memory."""

    def __init__(self):
        self.deadline = time.monotonic() + 300
        self.calls = 0

    def request(self, method, path, token, body=None, missing_ok=False):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        allowed = (
            method == "PUT" and re.fullmatch(r"/repos/vib-app/(?:app-[0-9a-f]{64}|sources)/contents/\.vibapp-archive-bootstrap", path)
            or method == "POST" and (path == f"/orgs/{ORG}/repos" or
                                  re.fullmatch(r"/app/installations/[0-9]+/access_tokens", path))
            or method in ("GET", "POST") and re.fullmatch(
                r"/repos/vib-app/(?:app-[0-9a-f]{64}|sources)(?:/git/(?:blobs|trees|commits|refs|ref/(?:heads/main|tags/vibapp-(?:source-)?[0-9a-f]{64}))(?:/[0-9a-f]{40})?(?:\?recursive=1)?)?", path)
            or method == "PATCH" and path == "/repos/vib-app/sources/git/refs/heads/main"
        )
        if not allowed or self.calls > 540 or remaining <= 0:
            raise SetupError("archive_api_policy")
        if method == "PATCH" and (not isinstance(body, dict) or body.get("force") is not False):
            raise SetupError("archive_force_push_denied")
        request = Request("https://api.github.com" + path, method=method,
                          data=canonical(body) if body is not None else None,
                          headers={"Authorization": f"Bearer {token}", "User-Agent": "VibApp-Source-Archive",
                                   "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2026-03-10"})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=min(15, remaining)) as response:
                if response.status != (200 if method in ("GET", "PATCH") else 201):
                    raise SetupError("archive_api_response")
                data = response.read(2 * MAX_SOURCE + 1)
            if len(data) > 2 * MAX_SOURCE:
                raise SetupError("archive_response_limit")
            return json.loads(data, object_pairs_hook=unique_object)
        except HTTPError as exc:
            status = exc.code
            # Classify only known error shapes; never expose GitHub response
            # bodies, request headers or credentials in logs/errors.
            try:
                error = json.loads(exc.read(65536), object_pairs_hook=unique_object)
            except (ValueError, OSError):
                error = {}
            finally:
                exc.close()
            if not isinstance(error, dict):
                error = {}
            errors = error.get("errors", [])
            if not isinstance(errors, list):
                errors = []
            if status == 404 and missing_ok and method == "GET":
                return None
            if status == 409 and error.get("message") == "Git Repository is empty.":
                raise SetupError("archive_repository_empty") from None
            if status == 422 and path.endswith("/access_tokens") and error.get("message") == (
                "There is at least one repository that does not exist or is not accessible to the parent installation."
            ):
                # Repository creation does not imply selected-installation access.
                # Keep the source private and require explicit repository selection;
                # never fall back to a wider or personal credential.
                raise SetupError("archive_repository_not_accessible_to_installation") from None
            if status == 422 and path == f"/orgs/{ORG}/repos" and any(
                isinstance(item, dict) and item.get("field") == "name"
                and "already exists" in str(item.get("message", "")).lower()
                for item in errors
            ):
                raise SetupError("archive_repository_exists_but_not_accessible") from None
            operation = ("token" if path.endswith("/access_tokens") else
                         "create-repository" if path == f"/orgs/{ORG}/repos" else
                         "git" if "/git/" in path else "repository")
            raise SetupError(f"archive_github_{operation}_{status}") from None
        except (URLError, OSError, TimeoutError, ValueError):
            raise SetupError("archive_network_or_response") from None


class Archiver:
    def __init__(self, credentials, installation, api=None):
        self.credentials, self.installation = credentials, installation
        self.api = api or GitHub()

    def token(self, permissions, repository_id=None):
        body = {"permissions": permissions}
        if repository_id is not None:
            body["repository_ids"] = [repository_id]
        result = self.api.request("POST", f"/app/installations/{self.installation['installation_id']}/access_tokens",
                                  app_jwt(self.credentials), body)
        if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
            raise SetupError("archive_token_failed")
        return result["token"]

    def upload(self, publisher_id, app_id, package_digest, expected_source_digest, source_root):
        if not SHA256.fullmatch(package_digest):
            raise SetupError("invalid_package_digest")
        return self._upload(publisher_id, app_id, package_digest, expected_source_digest, source_root)

    def upload_public(self, publisher_id, app_id, package_digest, expected_source_digest, source_root):
        """Trusted publication operation: caller must have public-source consent."""
        if not SHA256.fullmatch(package_digest):
            raise SetupError("invalid_package_digest")
        return self._upload(publisher_id, app_id, package_digest, expected_source_digest, source_root, public=True)

    def stage_source(self, publisher_id, app_id, expected_source_digest, source_root, *, public_source_approved=False):
        """Pre-build snapshot, NOT a package/publication receipt."""
        return self._upload(publisher_id, app_id, None, expected_source_digest, source_root,
                            public=public_source_approved is True)

    def _upload(self, publisher_id, app_id, package_digest, expected_source_digest, source_root, *, public=False):
        identity = repository_name(publisher_id, app_id)
        repo = SOURCES_REPOSITORY if public else identity
        files = snapshot(source_root, expected_source_digest)  # Before ANY network or token request.
        # Two apps can have identical source bytes. Shared-repo staging tags must
        # also bind publisher/app identity, not just the source digest.
        stage_digest = sha256((identity + "\n" + expected_source_digest).encode()) if public else expected_source_digest
        tag = "vibapp-" + package_digest if package_digest is not None else "vibapp-source-" + stage_digest
        binding = {"publisher_id": publisher_id, "app_id": app_id,
                   "source_digest_sha256": expected_source_digest}
        if package_digest is not None:
            binding["package_digest_sha256"] = package_digest
        else:
            binding["stage"] = "untrusted-source-awaiting-builder"
        files[MARKER] = canonical(binding)
        prefix = f"/repos/{ORG}/{repo}"
        # auto_init writes the initial README. Contents:read can leave a
        # partially created empty repository when initialization is rejected.
        admin = self.token({"administration": "write", "contents": "write"})
        remote = self.api.request("GET", prefix, admin, missing_ok=True)
        if remote is None:
            # An ambiguous POST is never blindly retried: the next job rechecks
            # this deterministic repository name and the immutable package tag.
            remote = self.api.request("POST", f"/orgs/{ORG}/repos", admin,
                                      {"name": repo, "private": not public, "auto_init": True,
                                       "description": "VibApp public application sources" if public else "VibApp private source archive", "has_issues": False,
                                       "has_projects": False, "has_wiki": False})
        del admin
        owner = remote.get("owner", {})
        if remote.get("full_name") != f"{ORG}/{repo}" or remote.get("private") is not (not public) or \
                owner.get("id") != self.credentials["owner"]["id"] or owner.get("type") != "Organization" or \
                type(remote.get("id")) is not int or not 0 < remote["id"] <= 9007199254740991 or \
                remote.get("archived") is True or remote.get("disabled") is True:
            raise SetupError("archive_repository_mismatch")
        token = self.token({"contents": "write"}, remote["id"])
        ref_path = prefix + "/git/ref/tags/" + tag
        initialized = False

        def recover_empty_repository():
            nonlocal initialized
            if initialized:
                raise SetupError("archive_repository_empty")
            initialized = True
            # One recovery for a partial create, only after GitHub reports an
            # empty Git repository. No `sha`: this can never overwrite a file.
            self.api.request("PUT", prefix + "/contents/" + BOOTSTRAP, token,
                             {"message": "Initialize VibApp source archive",
                              "content": base64.b64encode(b"VibApp source archive\n").decode()})

        try:
            ref = self.api.request("GET", ref_path, token, missing_ok=True)
        except SetupError as exc:
            if exc.code != "archive_repository_empty":
                raise
            recover_empty_repository()
            ref = None
        if ref is None:
            tree = []
            for path, data in sorted(files.items()):
                body = {"content": base64.b64encode(data).decode(), "encoding": "base64"}
                try:
                    blob = self.api.request("POST", prefix + "/git/blobs", token, body)
                except SetupError as exc:
                    if exc.code != "archive_repository_empty":
                        raise
                    recover_empty_repository()
                    blob = self.api.request("POST", prefix + "/git/blobs", token, body)
                if blob.get("sha") != blob_sha(data):
                    raise SetupError("archive_blob_mismatch")
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            tree_result = self.api.request("POST", prefix + "/git/trees", token, {"tree": tree})
            tree_sha = tree_result.get("sha", "")
            if not SHA1.fullmatch(tree_sha):
                raise SetupError("archive_tree_mismatch")
            commit = self.api.request("POST", prefix + "/git/commits", token,
                                      {"message": "VibApp source archive " + tag,
                                       "tree": tree_sha, "parents": []})
            if not SHA1.fullmatch(commit.get("sha", "")):
                raise SetupError("archive_commit_mismatch")
            self.api.request("POST", prefix + "/git/refs", token,
                             {"ref": "refs/tags/" + tag, "sha": commit["sha"]})
            ref = self.api.request("GET", ref_path, token)
        obj = ref.get("object", {})
        if obj.get("type") != "commit" or not SHA1.fullmatch(obj.get("sha", "")):
            raise SetupError("archive_ref_mismatch")
        commit_sha = obj["sha"]
        commit = self.api.request("GET", prefix + "/git/commits/" + commit_sha, token)
        tree_sha = commit.get("tree", {}).get("sha", "")
        if not SHA1.fullmatch(tree_sha):
            raise SetupError("archive_commit_mismatch")
        readback = self.api.request("GET", prefix + "/git/trees/" + tree_sha + "?recursive=1", token)
        entries = readback.get("tree")
        if readback.get("truncated") is not False or not isinstance(entries, list) or len(entries) > 2048:
            raise SetupError("archive_tree_mismatch")
        observed = {}
        for entry in entries:
            if entry.get("type") == "tree" and entry.get("mode") == "040000":
                continue
            if entry.get("type") != "blob" or entry.get("mode") != "100644" or entry.get("path") in observed:
                raise SetupError("archive_tree_mismatch")
            observed[entry.get("path")] = entry.get("sha")
        if observed != {path: blob_sha(data) for path, data in files.items()}:
            raise SetupError("archive_readback_mismatch")
        if public:
            # A browsable per-app/per-source directory on main complements the
            # immutable root snapshot used by the existing isolated CI checkout.
            from source_catalog import publish_source_directory
            publish_source_directory(self.api, prefix, token, publisher_id, app_id,
                                     expected_source_digest, {p: b for p, b in files.items() if p != MARKER})
        result = {"organization": ORG, "repository": repo, "repository_id": remote["id"],
                  "commit_sha": commit_sha, "source_digest_sha256": expected_source_digest}
        if package_digest is not None:
            result["package_digest_sha256"] = package_digest
        else:
            result["stage"] = "untrusted-source-awaiting-builder"
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--publisher-id", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--package-digest", required=True)
    parser.add_argument("--source-digest", required=True)
    parser.add_argument("--public-source", action="store_true", help="publish source to public vib-app/sources")
    parser.add_argument("--legacy-private", action="store_true", help="explicit legacy per-app private archive operation")
    args = parser.parse_args()
    try:
        if args.public_source == args.legacy_private:
            raise SetupError("choose_public_source_or_legacy_private")
        record = credential_record(CredentialStore(DEFAULT_STORE).read("credentials.json"))
        installation = verify_installation(record)
        worker = Archiver(record, installation)
        upload = worker.upload_public if args.public_source else worker.upload
        result = upload(args.publisher_id, args.app_id,
                    args.package_digest, args.source_digest, args.source_root)
        print(json.dumps({"status": "uploaded-and-verified", "github_archive": result}))
        return 0
    except SetupError as exc:
        print(json.dumps({"status": "failed", "error": exc.code}))
        return 1
    except Exception:
        print('{"status":"failed","error":"archive_failed_no_credentials_logged"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
