"""Trusted GitHub Actions launcher and resumable, real cloud Builder adapter.

All workflow, source and publication operations use repository-scoped App tokens.
No owner-token fallback or build-time access to App credentials.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import tempfile
import uuid
import zipfile

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
sys.path.insert(0, str(REPO / "artifacts/app-builder"))
from app_builder import RunnerResult, build_handoff, validate_handoff
from common import ProcessLimits, PipelineError
from github_archive import (Archiver, CredentialStore, DEFAULT_STORE, SetupError, canonical,
                            credential_record, verify_installation, snapshot, sha256)
from ci_builder import bounded
from github_transport import ScopedAppApi
from github_archive import repository_name, source_repository_matches

LOCK = BASE / "fixtures/ci-policy/Cargo.lock"
WORKFLOW = '''name: VibApp WASI build
run-name: vibapp-${{ inputs.request_id }}
on:
  workflow_dispatch:
    inputs:
      source_sha:
        required: true
        type: string
      source_digest:
        required: true
        type: string
      request_id:
        required: true
        type: string
permissions:
  contents: read
concurrency:
  group: vibapp-${{ inputs.request_id }}
  cancel-in-progress: false
jobs:
  compile:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with:
          persist-credentials: false
          path: platform
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with:
          persist-credentials: false
          ref: ${{ inputs.source_sha }}
          path: source
      - name: Isolated offline WASI compile
        env:
          VIBAPP_SOURCE_SHA: ${{ inputs.source_sha }}
          VIBAPP_SOURCE_DIGEST: ${{ inputs.source_digest }}
          VIBAPP_REQUEST_ID: ${{ inputs.request_id }}
        run: python3 platform/.vibapp-ci/ci_builder.py --source source --policy platform/.vibapp-ci --output output
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02
        with:
          name: vibapp-${{ inputs.request_id }}
          path: output/
          if-no-files-found: error
          retention-days: 7
          compression-level: 0
'''


def build_api(state, write=False):
    source = state["source"]
    if not source_repository_matches(source["repository"], state["publisher_id"], state["app_id"]):
        raise SetupError("workflow_repository_binding")
    if source["repository"] == "sources" and state.get("public_source_approved") is not True:
        raise SetupError("public_source_approval_required")
    return ScopedAppApi(source["repository"], source["repository_id"],
        {"contents": "write", "actions": "write", "workflows": "write"} if write else
        {"contents": "read", "actions": "read"})


def save(path, value):
    descriptor, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(name)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def workflow_files():
    return {".github/workflows/vibapp-build.yml": WORKFLOW.encode(),
            ".vibapp-ci/ci_builder.py": (BASE / "ci_builder.py").read_bytes(),
            ".vibapp-ci/Cargo.lock": LOCK.read_bytes()}


def bootstrap(api):
    ref = api.request("GET", "/git/ref/heads/main")
    parent = ref["object"]["sha"]
    commit = api.request("GET", "/git/commits/" + parent)
    files = workflow_files()
    current = api.request("GET", "/git/trees/" + commit["tree"]["sha"] + "?recursive=1")
    if current.get("truncated"):
        raise SetupError("workflow_tree_truncated")
    from github_archive import blob_sha
    entries = {e["path"]: e for e in current["tree"]}
    if all(entries.get(p, {}).get("sha") == blob_sha(data) and entries[p]["mode"] == "100644" for p, data in files.items()):
        return parent
    tree = []
    for path, data in files.items():
        blob = api.request("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        if blob["sha"] != blob_sha(data):
            raise SetupError("workflow_blob_mismatch")
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = api.request("POST", "/git/trees", {"base_tree": commit["tree"]["sha"], "tree": tree})
    new_commit = api.request("POST", "/git/commits", {
        "message": "Install trusted VibApp isolated WASI build workflow", "tree": new_tree["sha"], "parents": [parent]})
    api.request("PATCH", "/git/refs/heads/main", {"sha": new_commit["sha"], "force": False})
    return new_commit["sha"]


def start(handoff_path, publisher, out, *, public_source_approved=False):
    if public_source_approved is not True:
        raise SetupError("public_source_approval_required")
    handoff = validate_handoff(handoff_path)
    state = {"schema_version": "vibapp.github-pipeline.v1", "state": "source-staging",
             "request_id": uuid.uuid4().hex, "handoff": str(handoff_path.resolve()), "publisher_id": publisher,
             "handoff_sha256": sha256(handoff_path.read_bytes()),
             "app_id": handoff.document["package_intent"]["app_id"],
             "public_source_approved": True,
             "source": None, "workflow_commit": None, "run_id": None}
    save(out / "state.json", state)
    return stage(state, out)


def stage(state, out):
    if state.get("public_source_approved") is not True:
        raise SetupError("public_source_approval_required")
    handoff_path = Path(state["handoff"])
    if sha256(handoff_path.read_bytes()) != state["handoff_sha256"]:
        raise SetupError("handoff_changed")
    handoff = validate_handoff(handoff_path)
    record = credential_record(CredentialStore(DEFAULT_STORE).read("credentials.json"))
    installation = verify_installation(record)
    source = Archiver(record, installation).stage_source(state["publisher_id"],
        handoff.document["package_intent"]["app_id"], handoff.document["source_tree_sha256"], handoff.source_root,
        public_source_approved=True)
    state.update(source=source, state="source-staged")
    save(out / "state.json", state)
    return dispatch(state, out)


def dispatch(state, out):
    api = build_api(state, write=True)
    workflow_sha = bootstrap(api)
    tag = "vibapp-build-" + workflow_sha
    ref = api.request("GET", "/git/ref/tags/" + tag, missing_ok=True)
    if ref is None:
        api.request("POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": workflow_sha})
    elif ref.get("object") != {"type": "commit", "sha": workflow_sha, "url": ref.get("object", {}).get("url")}:
        raise SetupError("workflow_tag_conflict")
    state.update(workflow_commit=workflow_sha, state="dispatch-pending")
    save(out / "state.json", state)  # No blind re-dispatch after an ambiguous POST.
    api.request("POST", "/actions/workflows/vibapp-build.yml/dispatches", {
        "ref": tag, "inputs": {"source_sha": state["source"]["commit_sha"],
         "source_digest": state["source"]["source_digest_sha256"], "request_id": state["request_id"]}})
    state["state"] = "dispatched"
    save(out / "state.json", state)
    return state


def get_run(state, api=None):
    api = api or build_api(state)
    if state["run_id"] is None:
        page = api.request("GET", "/actions/workflows/vibapp-build.yml/runs?event=workflow_dispatch&per_page=100")
        matches = [r for r in page["workflow_runs"] if r["display_title"] == "vibapp-" + state["request_id"]]
        if len(matches) > 1:
            raise SetupError("ambiguous_workflow_runs")
        if not matches:
            return None
        state["run_id"] = matches[0]["id"]
    run = api.request("GET", "/actions/runs/" + str(state["run_id"]))
    if (run["head_sha"] != state["workflow_commit"] or run["event"] != "workflow_dispatch" or
            run["display_title"] != "vibapp-" + state["request_id"] or
            run["path"] != ".github/workflows/vibapp-build.yml" or run.get("run_attempt") != 1):
        raise SetupError("workflow_identity_mismatch")
    state["run_url"] = run["html_url"]
    return run


def download(state, out, api=None):
    api = api or build_api(state)
    result = api.request("GET", "/actions/runs/" + str(state["run_id"]) + "/artifacts?per_page=100")
    artifacts = [a for a in result["artifacts"] if a["name"] == "vibapp-" + state["request_id"] and not a["expired"]]
    if len(artifacts) != 1 or artifacts[0]["size_in_bytes"] > 20*1048576:
        raise SetupError("workflow_artifact_invalid")
    data = api.request("GET", "/actions/artifacts/" + str(artifacts[0]["id"]) + "/zip", binary=True)
    expected = artifacts[0].get("digest")
    if expected != "sha256:" + sha256(data):
        raise SetupError("workflow_artifact_digest")
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) != 3 or {i.filename for i in infos} != {"component.wasm", "Cargo.lock", "build.json"}:
            raise SetupError("workflow_artifact_paths")
        for info in infos:
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or info.file_size > (16*1048576 if info.filename == "component.wasm" else 1048576):
                raise SetupError("workflow_artifact_limit")
            files[info.filename] = archive.read(info)
    report = json.loads(files["build.json"])
    if any(report.get(k) != v for k, v in {
        "source_commit": state["source"]["commit_sha"], "workflow_commit": state["workflow_commit"],
        "run_id": state["run_id"], "request_id": state["request_id"],
        "source_digest_sha256": state["source"]["source_digest_sha256"],
        "component_sha256": sha256(files["component.wasm"]), "cargo_lock_sha256": sha256(files["Cargo.lock"]),
        "network": "none", "source_execution_observed": True, "production_isolation": False,
    }.items()):
        raise SetupError("workflow_output_binding")
    output = out / "cloud-output"
    output.mkdir(mode=0o700, exist_ok=True)
    for name, payload in files.items():
        p = output / name
        if p.exists() and p.read_bytes() != payload:
            raise SetupError("workflow_output_conflict")
        if not p.exists():
            with p.open("xb") as stream:
                stream.write(payload)
            p.chmod(0o400)
    state["artifact_id"] = artifacts[0]["id"]
    state["artifact_sha256"] = sha256(data)
    state["state"] = "cloud-compiled-awaiting-verifier"
    save(out / "state.json", state)
    return files, report


class GitHubResultRunner:
    def __init__(self, state, files, report):
        self.state, self.files, self.report = state, files, report

    def preflight(self):
        return {"status": "ready", "compile_authority": "isolated-builder",
                "verify_authority": "independent-verifier", "production_isolation": False}

    def execute(self, source_root, workspace, limits):
        snapshot(source_root, self.state["source"]["source_digest_sha256"])
        return RunnerResult(
            component_bytes=self.files["component.wasm"], cargo_lock_bytes=self.files["Cargo.lock"],
            stdout=b"GitHub Actions compile completed; verification remains separate.\n", stderr=b"",
            execution_mode="github-actions-docker-experimental",
            runner_identity_sha256=sha256(canonical(self.report)),
            command=["github-actions", self.state["run_url"], "cargo", "build", "--release", "--target",
                     "wasm32-wasip2", "--locked", "--offline", "--jobs", "1"],
            tool_versions={"rust": "1.93.0", "cargo": "1.93.0", "target": "wasm32-wasip2"},
            isolation={"network": "none", "source_execution_observed": True, "production_isolation": False,
                       "builder_image": self.report["builder_image"], "source_commit": self.report["source_commit"]},
            resource_observations={"component_bytes": len(self.files["component.wasm"])},
            cache_acceptance_sha256=None)


def resume(out):
    state = json.loads((out / "state.json").read_text())
    if state["state"] == "source-staging":
        return stage(state, out)
    if state["state"] == "source-staged":
        return dispatch(state, out)
    api = build_api(state)
    run = get_run(state, api)
    save(out / "state.json", state)
    if run is None or run["status"] != "completed":
        return {"state": "waiting-for-actions", "run_id": state["run_id"], "run_url": state.get("run_url")}
    if run["conclusion"] != "success":
        state["state"] = "cloud-build-failed"
        state["conclusion"] = run["conclusion"]
        save(out / "state.json", state)
        raise SetupError("github_build_" + str(run["conclusion"]))
    files, report = download(state, out, api)
    handoff_path = Path(state["handoff"])
    if state.get("handoff_sha256") and sha256(handoff_path.read_bytes()) != state["handoff_sha256"]:
        raise SetupError("handoff_changed")
    handoff = validate_handoff(handoff_path)
    if (handoff.document["source_tree_sha256"] != state["source"]["source_digest_sha256"] or
            handoff.document["package_intent"]["app_id"] != state["app_id"]):
        raise SetupError("handoff_changed")
    pipeline = out / "pipeline"
    receipt = pipeline / "jobs" / handoff.document["job_id"] / "quarantine/quarantine-receipt.json"
    if not receipt.exists():
        receipt = build_handoff(handoff_path, pipeline, GitHubResultRunner(state, files, report),
                                limits=ProcessLimits(memory_bytes=4*1024**3), publisher_id=state["publisher_id"])
    # Separate process: this runs existing pinned wasm-tools + runtime descriptor
    # reconciliation; cloud/agent success reports cannot create a candidate.
    result = subprocess.run([sys.executable, str(REPO / "artifacts/app-builder/verifier.py"), "verify",
                             str(receipt), "--output-root", str(pipeline), "--recheck-existing"],
                            capture_output=True, timeout=120)
    if result.returncode:
        (out / "verifier-failure.txt").write_bytes(result.stdout[:65536] + result.stderr[:65536])
        raise SetupError("independent_verifier_failed")
    receipt_data = json.loads(receipt.read_text())
    candidate = pipeline / "candidates" / receipt_data["package_digest_sha256"] / "candidate.json"
    value = json.loads(candidate.read_text())
    if value.get("state") != "candidate-ready":
        raise SetupError("candidate_not_verified")
    state["candidate"] = str(candidate.resolve())
    state["state"] = "independently-verified"
    save(out / "state.json", state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "resume"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--publisher-id")
    parser.add_argument("--public-source", action="store_true",
                        help="approve uploading this app's source to public vib-app/sources before CI")
    args = parser.parse_args()
    try:
        if args.command == "start":
            if not args.public_source:
                raise SetupError("public_source_approval_required")
            if args.output.exists() and (args.output.is_symlink() or any(args.output.iterdir())):
                raise SetupError("output_exists_use_resume")
            args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
            state = start(args.handoff, args.publisher_id, args.output, public_source_approved=True)
            print(json.dumps({"state": state["state"], "request_id": state["request_id"], "source": state["source"]}))
        else:
            state = resume(args.output)
            print(json.dumps({k: state[k] for k in ("state", "run_id", "run_url", "candidate") if k in state}))
        return 0
    except (SetupError, PipelineError) as exc:
        print(json.dumps({"state": "failed", "error": exc.code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
