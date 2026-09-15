"""Explicit trusted-host publication of an existing GitHub Builder job.

Running `publish` is the operator's public-sharing action, not an implicit build
side effect. This connector never runs inside the untrusted cloud compiler.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import github_build
import package_release
from github_archive import (Archiver, CredentialStore, DEFAULT_STORE, SHA1, SHA256,
    SetupError, canonical, credential_record, repository_name, source_repository_matches, sha256, verify_installation)

MAX_JSON = 1048576


def require(condition, code):
    if not condition:
        raise SetupError(code)


def read_json(path):
    return package_release.parse_json(package_release._read_state(path))


def save_json(path, value):
    """Atomic write of a connector-owned leaf; never follow a destination link."""
    payload = canonical(value)
    require(len(payload) <= MAX_JSON, "release_state_limit")
    if path.exists() or path.is_symlink():
        package_release._read_state(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".release-state-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_exact(path, value):
    if path.exists() or path.is_symlink():
        require(read_json(path) == value, "release_receipt_conflict")
    else:
        save_json(path, value)


def archiver():
    record = credential_record(CredentialStore(DEFAULT_STORE).read("credentials.json"))
    return Archiver(record, verify_installation(record))


def ledger_input(out):
    """Check local source/app identity before resume can contact GitHub."""
    state = read_json(out / "state.json")
    require(state.get("schema_version") == "vibapp.github-pipeline.v1" and
            state.get("state") in {"dispatch-pending", "dispatched", "cloud-compiled-awaiting-verifier",
                "independently-verified", "cloud-build-failed"}, "release_job_state")
    require(isinstance(state.get("handoff"), str) and isinstance(state.get("handoff_sha256"), str) and
            SHA256.fullmatch(state["handoff_sha256"]), "release_handoff_binding")
    path = Path(state["handoff"])
    require(path.is_absolute() and path.resolve(strict=True) == path, "release_handoff_path")
    require(sha256(package_release._read_state(path)) == state["handoff_sha256"], "release_handoff_changed")
    handoff = github_build.validate_handoff(path)
    app_id = handoff.document["package_intent"]["app_id"]
    require(state.get("app_id") == app_id and isinstance(state.get("publisher_id"), str), "release_app_binding")
    source = state.get("source")
    package_release.object_keys(source, {"organization", "repository", "repository_id", "commit_sha",
        "source_digest_sha256", "stage"}, "release_source_binding")
    require(source["organization"] == "vib-app" and
            source_repository_matches(source["repository"], state["publisher_id"], app_id) and
            (source["repository"] != "sources" or state.get("public_source_approved") is True) and
            type(source["repository_id"]) is int and 0 < source["repository_id"] <= 9007199254740991 and
            isinstance(source["commit_sha"], str) and SHA1.fullmatch(source["commit_sha"]) and
            source["source_digest_sha256"] == handoff.document["source_tree_sha256"] and
            source["stage"] == "untrusted-source-awaiting-builder", "release_source_binding")
    require(isinstance(state.get("workflow_commit"), str) and SHA1.fullmatch(state["workflow_commit"]), "release_run_binding")
    return state, handoff


def verified_candidate(out, state, handoff):
    require(state.get("state") == "independently-verified" and isinstance(state.get("candidate"), str),
            "release_candidate_unverified")
    path = Path(state["candidate"])
    files = package_release.snapshot(path)
    record = package_release.parse_json(files["candidate.json"])
    package_release.validate_record(record)
    require(path == out / "pipeline/candidates" / record["package_digest_sha256"] / "candidate.json", "release_candidate_path")
    manifest = package_release.parse_json(files["package/manifest.json"])
    from common import package_digest
    from verifier import _validate_manifest_schema
    _validate_manifest_schema(manifest)
    require(record["job_id"] == handoff.document["job_id"] and
            record["source_tree_sha256"] == handoff.document["source_tree_sha256"] and
            record["package_digest_sha256"] == package_digest(manifest) and
            manifest["app"]["id"] == state["app_id"] and
            manifest["app"]["publisher"]["id"] == state["publisher_id"] and
            manifest["app"]["version"] == handoff.document["package_intent"]["version"], "release_candidate_binding")
    for key, name in (("component", "component.wasm"), ("manifest", "manifest.json")):
        payload = files["package/" + name]
        require(record[key] == {"path": name, "sha256": sha256(payload), "size_bytes": len(payload)},
                "release_candidate_digest")
    for key in ("canonical_component", "provenance", "sbom"):
        descriptor = manifest["artifacts"][key]
        payload = files["package/" + descriptor["path"]]
        require(descriptor["sha256"] == sha256(payload) and descriptor["size_bytes"] == len(payload), "release_candidate_digest")
    require(type(state.get("run_id")) is int and 0 < state["run_id"] <= 9007199254740991 and
            state.get("run_url") == f"https://github.com/vib-app/{state['source']['repository']}/actions/runs/{state['run_id']}",
            "release_run_binding")
    run_binding = {"run_id": state["run_id"], "run_url": state["run_url"], "workflow_commit": state["workflow_commit"],
                   "source_commit": state["source"]["commit_sha"]}
    return path, files, record, run_binding


def safe_code(error):
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", code) else "release_failed"


def publish_job(output):
    """Public-sharing authority is supplied by the explicit trusted CLI caller."""
    out = Path(output).absolute()
    require(out.is_dir() and out.resolve(strict=True) == out, "release_output_path")
    # Preflight precedes lock/state writes and resume/network operations.
    before, _ = ledger_input(out)
    lock_fd = os.open(out / ".release.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        metadata = os.fstat(lock.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1, "release_lock_path")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("release_busy") from None
        state_path = out / "release-state.json"
        progress = {"schema_version": "vibapp.release-pipeline.experimental-v1", "state": "reverifying",
                    "appstore_publication": "not-performed"}
        try:
            save_json(state_path, progress)
            resumed = github_build.resume(out)
            if resumed.get("state") == "waiting-for-actions":
                progress["state"] = "waiting-for-actions"
                save_json(state_path, progress)
                return progress
            state, handoff = ledger_input(out)
            require(resumed == state, "release_resume_ledger_mismatch")
            require(all(state.get(key) == before.get(key) for key in
                ("handoff", "handoff_sha256", "publisher_id", "app_id", "source", "workflow_commit", "request_id")),
                "release_job_changed")
            path, files, record, run_binding = verified_candidate(out, state, handoff)
            progress.update(state="source-archiving", package_digest_sha256=record["package_digest_sha256"],
                            candidate_sha256=sha256(files["candidate.json"]), run_id=state["run_id"])
            save_json(state_path, progress)
            # Actual canonical package identity, never a source digest or placeholder.
            worker = archiver()
            upload = worker.upload_public if state["source"]["repository"] == "sources" else worker.upload
            source_receipt = upload(state["publisher_id"], state["app_id"], record["package_digest_sha256"],
                                              record["source_tree_sha256"], handoff.source_root)
            package_release.validate_bindings(files, source_receipt, run_binding)
            require(source_receipt["repository_id"] == state["source"]["repository_id"], "release_source_repository_changed")
            save_exact(out / "source-archive-receipt.json", source_receipt)
            progress["state"] = "publishing"
            save_json(state_path, progress)
            receipt = package_release.publish_candidate(path, source_receipt, run_binding, out / "publication")
            require(isinstance(receipt, dict) and receipt.get("state") == "public-downloads-verified" and
                    receipt.get("package_digest_sha256") == record["package_digest_sha256"] and
                    receipt.get("appstore_publication") == "not-performed", "release_publication_receipt_invalid")
            save_exact(out / "release-receipt.json", receipt)
            progress.update(state="public-downloads-verified", release_url=receipt["release_url"],
                            torrent_url=receipt["torrent_url"], asset_url=receipt["asset_url"])
            save_json(state_path, progress)
            return receipt
        except Exception as error:
            progress.update(state="failed", error={"code": safe_code(error)})
            try:
                save_json(state_path, progress)
            except Exception:
                pass
            raise SetupError(safe_code(error)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish"], help="explicitly authorize public package distribution")
    parser.add_argument("--output", type=Path, required=True, help="existing trusted github_build job directory")
    parser.add_argument("--list-in-store", action="store_true", help="also publish the released app in the public Store catalog")
    args = parser.parse_args(argv)
    try:
        result = publish_job(args.output)
        if args.list_in_store and result["state"] == "public-downloads-verified":
            import store_catalog
            store_catalog.publish(args.output)
            result = {**result, "appstore_publication": "listed"}
        print(json.dumps({key: result[key] for key in ("state", "package_digest_sha256", "release_url", "asset_url",
                                                     "torrent_url", "appstore_publication") if key in result}, sort_keys=True))
        return 0 if result["state"] == "public-downloads-verified" else 2
    except Exception as error:
        print(json.dumps({"state": "failed", "error": safe_code(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
