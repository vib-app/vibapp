"""Trusted, bounded GitHub package distribution; not Registry publication.

The public payload is the unchanged candidate directory. Source archives, logs,
credentials and builder workspaces are never enumerated by this module.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

from github_archive import (SECRET, SHA1, SHA256, SetupError, blob_sha, canonical,
                            repository_name, source_repository_matches, sha256, unique_object)
from github_transport import ScopedAppApi, public_download

BASE = Path(__file__).resolve().parent
BUILDER = BASE.parent / "app-builder"
PACKAGES_REPOSITORY_ID = 1359065065
MAX_JSON = 1048576
MAX_FILE = 16 * 1048576
MAX_TOTAL = 20 * 1048576
PIECE_LENGTH = 262144
TRACKER = "wss://tracker.webtorrent.dev"
PACKAGE_FILES = {"manifest.json", "component.wasm", "provenance.json", "sbom.cdx.json"}
PAYLOAD_PATHS = {"candidate.json"} | {"package/" + name for name in PACKAGE_FILES}
CHECK_IDS = {"bounded-package-tree", "manifest-schema-and-semantics", "artifact-and-package-digests",
             "builder-authority-separation", "wasm-tools-validate", "component-import-reconciliation",
             "component-tool-metadata", "guest-descriptor-reconciliation"}


def require(condition, code):
    if not condition:
        raise SetupError(code)


def object_keys(value, keys, code="package_schema"):
    require(isinstance(value, dict) and set(value) == set(keys), code)
    return value


def parse_json(data):
    require(isinstance(data, bytes) and len(data) <= MAX_JSON, "package_json_limit")
    try:
        value = json.loads(data, object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise SetupError("package_json_invalid") from None
    require(isinstance(value, dict), "package_json_object")
    return value


def snapshot(candidate_path):
    """Read exactly five no-follow regular files into an immutable byte snapshot."""
    candidate_path = Path(candidate_path).absolute()
    require(candidate_path.name == "candidate.json", "package_candidate_path")
    require(candidate_path.resolve(strict=True) == candidate_path, "package_symlink")
    root = candidate_path.parent
    files = {}
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        require(set(os.listdir(root_fd)) == {"candidate.json", "package"}, "package_tree_boundary")
        package_fd = os.open("package", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            require(set(os.listdir(package_fd)) == PACKAGE_FILES, "package_tree_boundary")
            for relative in sorted(PAYLOAD_PATHS):
                parent_fd = package_fd if relative.startswith("package/") else root_fd
                fd = os.open(relative.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as stream:
                    metadata = os.fstat(fd)
                    maximum = MAX_FILE if relative.endswith(".wasm") else MAX_JSON
                    require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and
                            0 < metadata.st_size <= maximum, "package_unsafe_file")
                    data = stream.read(maximum + 1)
                    require(len(data) == metadata.st_size, "package_file_changed")
                require(not SECRET.search(data), "package_secret_detected")
                files[relative] = data
        finally:
            os.close(package_fd)
    finally:
        os.close(root_fd)
    require(sum(map(len, files.values())) <= MAX_TOTAL, "package_size_limit")
    return files


def validate_record(record):
    fields = {"schema_version", "document_type", "state", "job_id", "source_tree_sha256",
              "package_digest_sha256", "package_directory", "component", "manifest",
              "quarantine_receipt_sha256", "verification", "authority"}
    require(fields <= record.keys() and not record.keys() - fields - {"presentation"}, "package_candidate_schema")
    require(record["schema_version"] == "vibapp.builder-candidate.experimental-v1" and
            record["document_type"] == "verifier-promoted-candidate" and
            record["state"] == "candidate-ready" and record["package_directory"] == "package" and
            record["authority"] == {"install": "daemon", "publish": "none"}, "package_candidate_authority")
    require(isinstance(record["job_id"], str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", record["job_id"]),
            "package_job_id")
    for field in ("source_tree_sha256", "package_digest_sha256", "quarantine_receipt_sha256"):
        require(isinstance(record[field], str) and SHA256.fullmatch(record[field]), "package_digest_invalid")
    verification = object_keys(record["verification"], {"authority", "verifier_version", "verified_at_utc", "checks"})
    require(verification["authority"] == "independent-verifier" and
            verification["verifier_version"] == "app-verifier.experimental-v1", "package_verifier_identity")
    try:
        value = dt.datetime.strptime(verification["verified_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
        require(value.strftime("%Y-%m-%dT%H:%M:%SZ") == verification["verified_at_utc"], "package_verifier_time")
    except (ValueError, TypeError):
        raise SetupError("package_verifier_time") from None
    checks = verification["checks"]
    require(isinstance(checks, list) and 1 <= len(checks) <= 64, "package_verifier_checks")
    ids = set()
    for check in checks:
        object_keys(check, {"id", "outcome", "tool", "detail"})
        for field, maximum in (("id", 128), ("tool", 256), ("detail", 1000)):
            require(isinstance(check[field], str) and 1 <= len(check[field]) <= maximum, "package_verifier_checks")
        require(check["id"] not in ids and check["outcome"] == "pass", "package_verifier_checks")
        ids.add(check["id"])
    require(CHECK_IDS <= ids, "package_verifier_checks")


def validate_bindings(files, source_receipt, run_binding):
    """Check schemas, actual bytes and the trusted post-build archive/run bindings."""
    sys.path.insert(0, str(BUILDER)) if str(BUILDER) not in sys.path else None
    from common import package_digest, derive_host_presentation
    from verifier import _validate_manifest_schema

    candidate = parse_json(files["candidate.json"])
    validate_record(candidate)
    require(files["package/component.wasm"].startswith(b"\0asm\r\0\1\0"), "package_component_header")
    manifest = parse_json(files["package/manifest.json"])
    _validate_manifest_schema(manifest)
    for key, path in (("component", "component.wasm"), ("manifest", "manifest.json")):
        descriptor = object_keys(candidate[key], {"path", "sha256", "size_bytes"})
        data = files["package/" + path]
        require(descriptor == {"path": path, "sha256": sha256(data), "size_bytes": len(data)},
                "package_candidate_descriptor")
    for key in ("canonical_component", "provenance", "sbom"):
        descriptor = manifest["artifacts"][key]
        data = files["package/" + descriptor["path"]]
        require(descriptor["sha256"] == sha256(data) and descriptor["size_bytes"] == len(data),
                "package_artifact_digest")
    require(package_digest(manifest) == candidate["package_digest_sha256"], "package_digest_mismatch")
    if "presentation" in candidate:
        require(candidate["presentation"] == derive_host_presentation(manifest["app"]["kind"],
                manifest["app"]["display_name"], manifest["app"]["description"]), "package_presentation")
    object_keys(source_receipt, {"organization", "repository", "repository_id", "commit_sha",
                               "source_digest_sha256", "package_digest_sha256"}, "package_source_receipt")
    require(source_receipt["organization"] == "vib-app" and
            source_repository_matches(source_receipt["repository"], manifest["app"]["publisher"]["id"], manifest["app"]["id"]) and
            type(source_receipt["repository_id"]) is int and 0 < source_receipt["repository_id"] <= 9007199254740991 and
            isinstance(source_receipt["commit_sha"], str) and SHA1.fullmatch(source_receipt["commit_sha"]),
            "package_source_receipt")
    require(source_receipt["source_digest_sha256"] == candidate["source_tree_sha256"] and
            source_receipt["package_digest_sha256"] == candidate["package_digest_sha256"], "package_source_digest_mismatch")
    object_keys(run_binding, {"run_id", "run_url", "workflow_commit", "source_commit"}, "package_run_binding")
    require(type(run_binding["run_id"]) is int and 0 < run_binding["run_id"] <= 9007199254740991 and
            run_binding["run_url"] == f"https://github.com/vib-app/{source_receipt['repository']}/actions/runs/{run_binding['run_id']}" and
            all(isinstance(run_binding[key], str) and SHA1.fullmatch(run_binding[key]) for key in ("workflow_commit", "source_commit")),
            "package_run_binding")
    provenance = parse_json(files["package/provenance.json"])
    object_keys(provenance, {"schema_version", "scope", "job_id", "need_spec_digest_sha256", "source_tree_sha256",
        "cargo_lock_sha256", "contract_sha256", "component_wasm_sha256", "builder", "runner_identity_sha256",
        "execution_mode", "tool_versions", "command", "isolation", "limits", "resource_observations",
        "cache_acceptance_sha256", "network_phase", "finished_at_utc"}, "package_provenance_schema")
    require(provenance["source_tree_sha256"] == candidate["source_tree_sha256"] and
            provenance["job_id"] == candidate["job_id"] and provenance["component_wasm_sha256"] == candidate["component"]["sha256"] and
            provenance["cargo_lock_sha256"] == manifest["source"]["cargo_lock_sha256"] and
            provenance["execution_mode"] == "github-actions-docker-experimental" and
            provenance["network_phase"] == "none" and
            provenance["command"] == ["github-actions", run_binding["run_url"], "cargo", "build", "--release",
                "--target", "wasm32-wasip2", "--locked", "--offline", "--jobs", "1"], "package_provenance_binding")
    isolation = object_keys(provenance["isolation"], {"network", "source_execution_observed", "production_isolation",
                                                     "builder_image", "source_commit"}, "package_isolation_schema")
    require(isolation["network"] == "none" and isolation["source_execution_observed"] is True and
            isolation["production_isolation"] is False and isolation["source_commit"] == run_binding["source_commit"],
            "package_provenance_binding")
    from app_builder import BUILDER_VERSION
    require(provenance["schema_version"] == "vibapp.builder-provenance.experimental-v1" and
            provenance["scope"] == "local-product-prototype" and provenance["builder"] == BUILDER_VERSION and
            provenance["tool_versions"] == {"rust": "1.93.0", "cargo": "1.93.0", "target": "wasm32-wasip2"} and
            isinstance(isolation["builder_image"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", isolation["builder_image"]) and
            provenance["cache_acceptance_sha256"] is None and
            provenance["resource_observations"] == {"component_bytes": len(files["package/component.wasm"])},
            "package_provenance_schema")
    limits = object_keys(provenance["limits"], {"wall_seconds", "cpu_seconds", "memory_bytes", "pids", "disk_bytes",
        "stdout_bytes", "stderr_bytes", "open_files", "concurrent_jobs"}, "package_provenance_schema")
    require(all(type(value) is int and 0 < value <= 16 * 1024 ** 3 for value in limits.values()), "package_provenance_schema")
    for key in ("need_spec_digest_sha256", "cargo_lock_sha256", "contract_sha256", "runner_identity_sha256"):
        require(isinstance(provenance[key], str) and SHA256.fullmatch(provenance[key]), "package_provenance_schema")
    require(manifest["source"]["builder_image_digest"] == "sha256:" + provenance["runner_identity_sha256"] and
            manifest["source"]["revision"] == "codeagent-" + candidate["source_tree_sha256"][:24], "package_provenance_binding")
    # Current Builder's closed minimal SBOM contains no source attachment field.
    sbom = parse_json(files["package/sbom.cdx.json"])
    require(sbom == {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {"type": "application", "name": manifest["app"]["id"], "version": manifest["app"]["version"]}},
        "components": [{"type": "library", "name": "wit-bindgen", "version": "0.60.0"}],
        "properties": [{"name": "ai.vibapp.scope", "value": "local-product-prototype"},
                       {"name": "ai.vibapp.network-phase", "value": "none"}]}, "package_sbom_schema")
    return candidate


def _verify_snapshot(path):
    sys.path.insert(0, str(BUILDER))
    from verifier import _run_wasm_checks, DEFAULT_WASM_TOOLS
    from descriptor_reconciliation import run_guest_descriptor_reconciliation, DEFAULT_COMPONENT_INSPECTOR
    files = snapshot(path)
    manifest = parse_json(files["package/manifest.json"])
    component = Path(path).parent / "package/component.wasm"
    _run_wasm_checks(component, manifest, DEFAULT_WASM_TOOLS)
    run_guest_descriptor_reconciliation(component, manifest, DEFAULT_COMPONENT_INSPECTOR)


def _run_independent_checks(path):
    # Fixed trusted program, separate process, no inherited credentials/config.
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_verify-snapshot", str(path)],
                            cwd=BASE, env={"PATH": "/usr/bin:/bin", "TZ": "UTC", "LANG": "C", "LC_ALL": "C"},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=180, check=False)
    require(result.returncode == 0, "package_independent_verification_failed")


def bencode(value):
    if type(value) is int:
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    raise SetupError("package_torrent_type")


def torrent(files, digest, commit):
    joined = b"".join(files[path] for path in sorted(files))
    info = {"name": digest + ".vibapp-candidate", "piece length": PIECE_LENGTH,
            "pieces": b"".join(hashlib.sha1(joined[start:start + PIECE_LENGTH]).digest()
                               for start in range(0, len(joined), PIECE_LENGTH)),
            "files": [{"length": len(files[path]), "path": path.split("/")} for path in sorted(files)]}
    return bencode({"announce": TRACKER, "announce-list": [[TRACKER]], "info": info,
                    "url-list": [f"https://raw.githubusercontent.com/vib-app/packages/{commit}/packages/"]}), hashlib.sha1(bencode(info)).hexdigest()


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as stream:
        for name in sorted(files):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o444) << 16
            stream.writestr(entry, files[name])
    return output.getvalue()


def git_sha(value):
    require(isinstance(value, str) and SHA1.fullmatch(value), "package_git_identity")
    return value


def read_tree(api, tree_sha):
    result = api.request("GET", f"/git/trees/{git_sha(tree_sha)}?recursive=1")
    require(isinstance(result, dict) and result.get("truncated") is False and
            isinstance(result.get("tree"), list) and len(result["tree"]) <= 4096, "package_git_tree_limit")
    entries = {}
    for entry in result["tree"]:
        require(isinstance(entry, dict) and isinstance(entry.get("path"), str) and
                entry["path"] not in entries, "package_git_tree_invalid")
        entries[entry["path"]] = (entry.get("type"), entry.get("mode"), git_sha(entry.get("sha")))
    return entries


def candidate_commit(api, files, digest, binding):
    tag = "vibapp-package-" + digest
    ref_path = "/git/ref/tags/" + tag
    prefix = "packages/" + digest + ".vibapp-candidate/"
    expected = {prefix + path: ("blob", "100644", blob_sha(data)) for path, data in files.items()}
    message = "VibApp package distribution\n" + canonical(binding).decode()
    try:
        ref = api.request("GET", ref_path, missing_ok=True)
    except SetupError as error:
        if error.code != "github_role_http_409":
            raise
        ref = None
    if ref is None:
        try:
            head = api.request("GET", "/git/ref/heads/main", missing_ok=True)
        except SetupError as error:
            if error.code != "github_role_http_409":
                raise
            head = None
        if head is None:
            # Create-only Contents request, never supplies a previous blob sha.
            api.request("PUT", "/contents/.vibapp-packages-bootstrap", {
                "message": "Initialize VibApp package distribution",
                "content": base64.b64encode(b"VibApp experimental package distribution\n").decode()})
            head = api.request("GET", "/git/ref/heads/main")
        require(head.get("object", {}).get("type") == "commit", "package_git_head")
        parent = git_sha(head["object"]["sha"])
        parent_commit = api.request("GET", "/git/commits/" + parent)
        base_tree = git_sha(parent_commit.get("tree", {}).get("sha"))
        old = read_tree(api, base_tree)
        require(not any(path == prefix[:-1] or path.startswith(prefix) for path in old) and
                ("packages" not in old or old["packages"][:2] == ("tree", "040000")), "package_git_namespace_conflict")
        rows = []
        for path, data in sorted(files.items()):
            blob = api.request("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            require(blob.get("sha") == blob_sha(data), "package_git_blob_mismatch")
            rows.append({"path": prefix + path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = api.request("POST", "/git/trees", {"base_tree": base_tree, "tree": rows})
        actual = read_tree(api, tree.get("sha"))
        require({p: v for p, v in actual.items() if v[0] != "tree"} ==
                ({p: v for p, v in old.items() if v[0] != "tree"} | expected), "package_git_preservation_failed")
        commit = api.request("POST", "/git/commits", {"message": message, "tree": git_sha(tree["sha"]), "parents": [parent]})
        commit_sha = git_sha(commit.get("sha"))
        api.request("POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": commit_sha})
        ref = api.request("GET", ref_path)
    require(ref.get("ref") == "refs/tags/" + tag and ref.get("object", {}).get("type") == "commit", "package_git_ref_mismatch")
    commit_sha = git_sha(ref["object"]["sha"])
    commit = api.request("GET", "/git/commits/" + commit_sha)
    require(commit.get("message") == message, "package_retry_conflict")
    actual = read_tree(api, commit.get("tree", {}).get("sha"))
    require({p: v for p, v in actual.items() if p.startswith(prefix) and v[0] != "tree"} == expected,
            "package_git_readback_mismatch")
    return commit_sha, tag


def release_assets(api, tag, commit, binding, payloads):
    body = "Experimental package distribution; no AppStore publication.\n" + canonical(binding).decode()
    release = api.request("GET", "/releases/tags/" + tag, missing_ok=True)
    if release is None:
        release = api.request("POST", "/releases", {"tag_name": tag, "target_commitish": commit,
            "name": tag, "body": body, "draft": False, "prerelease": True, "make_latest": "false"})
    require(isinstance(release, dict) and type(release.get("id")) is int and release["id"] > 0 and
            release.get("tag_name") == tag and release.get("body") == body and
            release.get("draft") is False and release.get("prerelease") is True and
            release.get("html_url") == "https://github.com/vib-app/packages/releases/tag/" + tag,
            "package_release_conflict")
    assets = api.request("GET", f"/releases/{release['id']}/assets?per_page=100")
    require(isinstance(assets, list) and len(assets) < 100, "package_release_asset_limit")
    indexed = {}
    for asset in assets:
        require(isinstance(asset, dict) and isinstance(asset.get("name"), str) and
                asset["name"] not in indexed, "package_release_asset_conflict")
        indexed[asset["name"]] = asset
    require(set(indexed) <= set(payloads), "package_release_asset_conflict")
    verified = []
    for name, (payload, content_type) in sorted(payloads.items()):
        asset = indexed.get(name)
        if asset is None:
            asset = api.upload_asset(release["id"], name, payload, content_type)
        url = "https://github.com/vib-app/packages/releases/download/" + tag + "/" + name
        require(isinstance(asset, dict) and type(asset.get("id")) is int and asset["id"] > 0 and
                asset.get("name") == name and asset.get("size") == len(payload) and
                asset.get("state") == "uploaded" and asset.get("browser_download_url") == url,
                "package_release_asset_conflict")
        require(public_download(url, limit=len(payload)) == payload, "package_release_download_mismatch")
        verified.append({"name": name, "url": url, "sha256": sha256(payload), "size_bytes": len(payload)})
    return release["html_url"], verified


def torrent_commit(api, payload, digest, data_commit):
    """A second immutable commit avoids a self-referential Raw webseed URL."""
    path = "metadata/" + digest + ".torrent"
    expected = ("blob", "100644", blob_sha(payload))
    tag = "vibapp-torrent-" + digest
    ref_path = "/git/ref/tags/" + tag
    message = "VibApp torrent metadata\n" + canonical({"data_commit": data_commit,
        "package_digest_sha256": digest, "torrent_sha256": sha256(payload)}).decode()
    ref = api.request("GET", ref_path, missing_ok=True)
    if ref is None:
        parent = api.request("GET", "/git/commits/" + data_commit)
        base_tree = git_sha(parent.get("tree", {}).get("sha"))
        old = read_tree(api, base_tree)
        require(path not in old and ("metadata" not in old or old["metadata"][:2] == ("tree", "040000")),
                "package_torrent_namespace_conflict")
        blob = api.request("POST", "/git/blobs", {"content": base64.b64encode(payload).decode(), "encoding": "base64"})
        require(blob.get("sha") == expected[2], "package_git_blob_mismatch")
        tree = api.request("POST", "/git/trees", {"base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": expected[2]}]})
        actual = read_tree(api, tree.get("sha"))
        require({p: v for p, v in actual.items() if v[0] != "tree"} ==
                ({p: v for p, v in old.items() if v[0] != "tree"} | {path: expected}), "package_git_preservation_failed")
        commit = api.request("POST", "/git/commits", {"message": message, "tree": git_sha(tree["sha"]), "parents": [data_commit]})
        api.request("POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": git_sha(commit.get("sha"))})
        ref = api.request("GET", ref_path)
    require(ref.get("ref") == "refs/tags/" + tag and ref.get("object", {}).get("type") == "commit", "package_git_ref_mismatch")
    commit_sha = git_sha(ref["object"]["sha"])
    commit = api.request("GET", "/git/commits/" + commit_sha)
    require(commit.get("message") == message, "package_retry_conflict")
    actual = read_tree(api, commit.get("tree", {}).get("sha"))
    require(actual.get(path) == expected, "package_git_readback_mismatch")
    return commit_sha, f"https://raw.githubusercontent.com/vib-app/packages/{commit_sha}/{path}"


def _write_new(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _read_state(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(fd)
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_size <= MAX_JSON,
                "package_state_path")
        data = stream.read(MAX_JSON + 1)
        require(len(data) <= MAX_JSON, "package_state_path")
        return data


def publish_candidate(candidate_path, source_receipt, run_binding, output_dir, api=None):
    """Authorized trusted-host operation. Never called by generated source/CI."""
    files = snapshot(candidate_path)
    candidate = validate_bindings(files, source_receipt, run_binding)
    digest = candidate["package_digest_sha256"]
    output_dir = Path(output_dir).absolute()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(output_dir.resolve(strict=True) == output_dir and not output_dir.is_symlink(), "package_state_path")
    state_dir = output_dir / digest
    state_dir.mkdir(mode=0o700, exist_ok=True)
    require(not state_dir.is_symlink() and state_dir.is_dir(), "package_state_path")
    lock_fd = os.open(state_dir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        require(stat.S_ISREG(os.fstat(lock.fileno()).st_mode) and os.fstat(lock.fileno()).st_nlink == 1, "package_state_path")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("package_publisher_busy") from None
        binding = {"schema_version": "vibapp.package-release-binding.experimental-v1", "package_digest_sha256": digest,
                   "candidate_sha256": sha256(files["candidate.json"]), "source_receipt_sha256": sha256(canonical(source_receipt)),
                   "run_binding_sha256": sha256(canonical(run_binding))}
        binding_path = state_dir / "binding.json"
        if binding_path.exists() or binding_path.is_symlink():
            require(_read_state(binding_path) == canonical(binding), "package_retry_conflict")
        else:
            _write_new(binding_path, canonical(binding))
        with tempfile.TemporaryDirectory(prefix="verify-", dir=state_dir) as temporary:
            frozen = Path(temporary)
            (frozen / "package").mkdir(mode=0o700)
            for relative, payload in files.items():
                _write_new(frozen / relative, payload)
            _run_independent_checks(frozen / "candidate.json")
            require(snapshot(frozen / "candidate.json") == files, "package_verifier_changed_bytes")
        api = api if api is not None else ScopedAppApi("packages", PACKAGES_REPOSITORY_ID, {"contents": "write"})
        remote = api.request("GET", "")
        require(remote.get("id") == PACKAGES_REPOSITORY_ID and remote.get("full_name") == "vib-app/packages" and
                remote.get("private") is False and remote.get("default_branch") == "main" and
                not remote.get("archived") and not remote.get("disabled"), "package_repository_mismatch")
        commit, tag = candidate_commit(api, files, digest, binding)
        raw_base = f"https://raw.githubusercontent.com/vib-app/packages/{commit}/packages/{digest}.vibapp-candidate/"
        downloads = []
        for path, payload in sorted(files.items()):
            url = raw_base + path
            require(public_download(url, limit=len(payload)) == payload, "package_raw_download_mismatch")
            downloads.append({"path": path, "url": url, "sha256": sha256(payload), "size_bytes": len(payload)})
        torrent_bytes, info_hash = torrent(files, digest, commit)
        metadata_commit, torrent_url = torrent_commit(api, torrent_bytes, digest, commit)
        require(public_download(torrent_url, limit=len(torrent_bytes)) == torrent_bytes, "package_torrent_download_mismatch")
        downloads.append({"path": "metadata/" + digest + ".torrent", "url": torrent_url,
                          "sha256": sha256(torrent_bytes), "size_bytes": len(torrent_bytes)})
        zip_name, torrent_name = digest + ".zip", digest + ".torrent"
        payloads = {zip_name: (archive(files), "application/zip"), torrent_name: (torrent_bytes, "application/x-bittorrent")}
        release_url, assets = release_assets(api, tag, commit, binding, payloads)
        indexed = {item["name"]: item for item in assets}
        result = {"schema_version": "vibapp.package-release-receipt.experimental-v1", "state": "public-downloads-verified",
            "package_digest_sha256": digest, "release_url": release_url, "asset_url": indexed[zip_name]["url"],
            "raw_files_commit": commit, "raw_candidate_url": raw_base + "candidate.json", "torrent_url": torrent_url,
            "torrent_metadata_commit": metadata_commit, "torrent_release_url": indexed[torrent_name]["url"],
            "info_hash": info_hash, "webseed_url": f"https://raw.githubusercontent.com/vib-app/packages/{commit}/packages/",
            "magnet_uri": f"magnet:?xt=urn:btih:{info_hash}&dn={digest}.vibapp-candidate&tr=wss%3A%2F%2Ftracker.webtorrent.dev",
            "tracker_urls": [TRACKER],
            "verified_downloads": downloads + assets, "binding": binding, "appstore_publication": "not-performed"}
        receipt_path = state_dir / "receipt.json"
        if receipt_path.exists() or receipt_path.is_symlink():
            require(_read_state(receipt_path) == canonical(result), "package_retry_conflict")
        else:
            _write_new(receipt_path, canonical(result))
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["_verify-snapshot"])
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        _verify_snapshot(args.candidate)
    except Exception:
        raise SystemExit(1) from None
