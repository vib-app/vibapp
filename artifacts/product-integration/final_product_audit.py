#!/usr/bin/env python3
"""Fail-closed audit for the bounded local VibApp product mission.

This command performs no provider/model request and no publication.  It only
revalidates retained local evidence, current package bytes, source derivation,
loopback health and an independently recorded Butler goal verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import urllib.request
import re
from typing import Any, Callable


REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO / "artifacts"
RUN_DIR = ARTIFACTS / ".butler/runs/20260827-035953-vibapp-launcher-gui-web-regist"
LIVE_ROOT = ARTIFACTS / "product-integration/output/live-codeagent-20260827t041629z"
TASK_ID = "development-d29ded7d7ca56fefb2ec7158832aac26"
ATTEMPT_ID = "attempt-0006-59f9082a7af8ce06"
PACKAGE_DIGEST = "39e80bdf6dc254ae643c3d86ec1ae6adfd0c6db3c8b978c529aa251c87b7e86b"
GOAL_GATE_ID = "20260827-135749-round006-final-bounded-local-product-goal"
GOAL_GATE_DIR = RUN_DIR / "acceptance/gates" / GOAL_GATE_ID
GOAL_GATE_GOAL = (
    "Independently accept or reject the complete bounded local VibApp mission: the same Launcher GUI on "
    "packaged macOS and local Web; NeedSpec refinement and Registry-first routing; retained real Codex to "
    "isolated Builder, independent Verifier and private AppStore; durable task/lifecycle UX; daemon UI/service "
    "execution; trusted foreground browser derivation and stable app URL; i18n, four CodeAgent adapters and "
    "non-CodeAgent BYOM; current signed package, health and regression. Do not claim public deployment, formal "
    "Stage 0 Web activation, or native Windows/Linux certification."
)
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
NODE = shutil.which("node")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REGRESSION_MARKERS = (
    "[vibapp regression] javascript: regenerate Web from the sole Desktop GUI source",
    "[vibapp regression] python: artifacts/cloud-agent/tests",
    "[vibapp regression] python: artifacts/codeagent-adapter/tests",
    "[vibapp regression] python: artifacts/app-builder/tests",
    "[vibapp regression] python: artifacts/orchestrator/tests",
    "[vibapp regression] python: artifacts/provider-runner/tests",
    "[vibapp regression] python: artifacts/local-codeagent/tests",
    "[vibapp regression] python: artifacts/product-integration",
    "[vibapp regression] no-request CodeAgent/Builder/Verifier/shared-GUI readiness",
    "[vibapp regression] python: artifacts/need-analyzer",
    "[vibapp regression] python: artifacts/registry/tests",
    "[vibapp regression] python: artifacts/registry-store/tests",
    "[vibapp regression] runtime: isolated rebuilt daemon, fixtures and CLI smoke",
    "[vibapp regression] python: artifacts/website/tests",
    "[vibapp regression] python: artifacts/product-platform/client/tests",
    "[vibapp regression] python: artifacts/product-platform/registry/tests",
    "[vibapp regression] python: artifacts/desktop/ci",
    "[vibapp regression] javascript: shared Desktop/Web GUI oracles",
    "[vibapp regression] rust: desktop",
    "[vibapp regression] rust: web core",
    "[vibapp regression] javascript: web contracts",
    "[vibapp regression] javascript: independent browser derivation verifier",
    "[vibapp regression] javascript: two-origin preview host",
    "[vibapp regression] javascript: trusted Website product backend",
    "[vibapp regression] javascript: Website local/production start policy",
    "[vibapp regression] synthetic complete vertical",
    "[vibapp regression] PASS",
)


class AuditFailure(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def read_bytes(path: Path, maximum: int = MAX_EVIDENCE_BYTES) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not 1 <= metadata.st_size <= maximum
    ):
        raise AuditFailure(f"unsafe or missing bounded evidence: {path}")
    return path.read_bytes()


def read_json(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise AuditFailure(f"duplicate JSON key in evidence: {path}: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(read_bytes(path), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditFailure(f"invalid JSON evidence: {path}") from error
    if not isinstance(value, dict):
        raise AuditFailure(f"JSON evidence is not an object: {path}")
    return value


def sha256(path: Path, maximum: int = MAX_EVIDENCE_BYTES) -> str:
    return hashlib.sha256(read_bytes(path, maximum)).hexdigest()


def require_tokens(path: Path, tokens: tuple[str, ...]) -> dict[str, Any]:
    text = read_bytes(path, 4 * 1024 * 1024).decode("utf-8")
    missing = [token for token in tokens if token not in text]
    if missing:
        raise AuditFailure(f"required current-source behavior is missing from {path}: {missing}")
    return {"path": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest(), "tokens": list(tokens)}


def require_regression(path: Path) -> dict[str, Any]:
    raw = read_bytes(path)
    text = raw.decode("utf-8", errors="strict")
    positions: list[int] = []
    for marker in REGRESSION_MARKERS:
        if text.count(marker) != 1:
            raise AuditFailure(f"complete regression marker is absent or duplicated: {marker}")
        positions.append(text.index(marker))
    if positions != sorted(positions) or not raw.rstrip().endswith(REGRESSION_MARKERS[-1].encode()):
        raise AuditFailure("complete regression does not end in its exact PASS marker")
    python_suites = [int(value) for value in re.findall(r"^Ran (\d+) tests? in ", text, re.MULTILINE)]
    rust_suites = [
        tuple(map(int, match))
        for match in re.findall(
            r"^test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored; (\d+) measured; (\d+) filtered out;",
            text,
            re.MULTILINE,
        )
    ]
    tap_passes = [int(value) for value in re.findall(r"^# pass (\d+)$", text, re.MULTILINE)]
    required_receipts = (
        '"provider_process_started":false',
        '"provider_calls":0',
        '"daemon_lifecycle_smoke": "PASS"',
        '"guest_execution_performed_by_daemon": true',
        '"private_appstore_ingest": true',
    )
    if (
        len(python_suites) < 13
        or sum(python_suites) < 200
        or len(rust_suites) < 5
        or sum(item[0] for item in rust_suites) < 190
        or any(item[1] != 0 for item in rust_suites)
        or len(tap_passes) < 5
        or sum(tap_passes) < 35
        or any(receipt not in text for receipt in required_receipts)
    ):
        raise AuditFailure("complete regression lacks required suite outcomes or bounded-chain receipts")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "ordered_markers": len(REGRESSION_MARKERS),
        "python_tests": sum(python_suites),
        "rust_tests": sum(item[0] for item in rust_suites),
        "tap_tests": sum(tap_passes),
        "pass_marker": True,
    }


def require_goal_gate(path: Path) -> dict[str, Any]:
    if path.resolve(strict=True) != (GOAL_GATE_DIR / "result.json").resolve(strict=True):
        raise AuditFailure("acceptance result is not the exact Round 006 goal gate")
    result = read_json(path)
    if (
        result.get("status") != "passed"
        or result.get("kind") != "goal"
        or result.get("gate_id") != GOAL_GATE_ID
        or result.get("goal") != GOAL_GATE_GOAL
        or result.get("linked_round") != "round-006"
        or result.get("findings") != []
        or result.get("reviewer_identity") != "round006_goal_acceptance"
        or result.get("owner_identity") != "root"
        or result.get("reviewer_identity") == result.get("owner_identity")
        or result.get("run_dir") != str(RUN_DIR)
        or result.get("project_root") != str(ARTIFACTS)
        or result.get("request_path") != str(GOAL_GATE_DIR / "request.md")
        or result.get("progress_path") != str(GOAL_GATE_DIR / "progress.md")
        or result.get("verdict_path") != str(GOAL_GATE_DIR / "verdict.md")
        or sha256(GOAL_GATE_DIR / "request.md") != "ff430cb43d58047e973d22ef6b75c4a4a3a5717be4eb795553fa81f396c330c9"
    ):
        raise AuditFailure("exact fresh independent Round 006 goal gate is non-passing, mutated, or has findings")
    return {
        "path": str(path),
        "sha256": sha256(path),
        "gate_id": result.get("gate_id"),
        "reviewer_identity": result.get("reviewer_identity"),
    }


def require_current_process(expected_name: str, expected_command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,state=,command="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
        raise AuditFailure(f"current process snapshot is unavailable: {expected_name}")
    exact_command = " ".join(expected_command)
    matches: list[dict[str, Any]] = []
    for raw_line in completed.stdout.decode("utf-8", errors="strict").splitlines():
        parts = raw_line.strip().split(None, 4)
        if len(parts) != 5 or parts[4] != exact_command:
            continue
        try:
            pid, parent_pid, process_group = (int(parts[index]) for index in range(3))
        except ValueError as error:
            raise AuditFailure(f"current process snapshot is malformed: {expected_name}") from error
        if pid < 2 or parent_pid < 0 or process_group != pid or parts[3].startswith("Z"):
            raise AuditFailure(f"current process identity is unsafe or not live: {expected_name}")
        matches.append({"pid": pid, "parent_pid": parent_pid, "pgid": process_group, "state": parts[3]})
    if len(matches) != 1:
        raise AuditFailure(f"expected one exact current process, observed {len(matches)}: {expected_name}")
    return {
        "name": expected_name,
        "observed_by": "current-ps-snapshot",
        "command": expected_command,
        **matches[0],
    }


def require_shared_gui() -> dict[str, Any]:
    desktop = ARTIFACTS / "desktop/ui"
    web = ARTIFACTS / "product-platform/website/public/launcher"
    shared: dict[str, Any] = {}
    for name in ("app.js", "styles.css", "favicon.svg"):
        desktop_digest = sha256(desktop / name)
        web_digest = sha256(web / name)
        if desktop_digest != web_digest:
            raise AuditFailure(f"Desktop/Website shared GUI drift: {name}")
        shared[name] = desktop_digest
    bridge_source = ARTIFACTS / "web-client-core/web-bridge.js"
    bridge_copy = web / "web-bridge.js"
    if sha256(bridge_source) != sha256(bridge_copy):
        raise AuditFailure("generated Website bridge differs from its source")
    return {"shared_sha256": shared, "web_bridge_sha256": sha256(bridge_source)}


def macho_uuid(path: Path) -> str:
    completed = subprocess.run(
        ["/usr/bin/dwarfdump", "--uuid", str(path)],
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    match = re.fullmatch(rb"UUID: ([0-9A-F-]{36}) \(arm64\) .+\n?", completed.stdout)
    if completed.returncode != 0 or not match:
        raise AuditFailure(f"cannot read one exact arm64 Mach-O UUID: {path}")
    return match.group(1).decode("ascii")


def run_json_command(command: list[str], timeout: int = 20) -> dict[str, Any]:
    tool_path = f"{Path(NODE).parent}:" if NODE else ""
    completed = subprocess.run(
        command,
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env={
            "PATH": tool_path + "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C",
            "HOME": os.environ.get("HOME", str(Path.home())),
        },
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4096:]
        raise AuditFailure(f"JSON check failed ({command[0]}): {detail}")
    if not 2 <= len(completed.stdout) <= 64 * 1024:
        raise AuditFailure(f"JSON check returned an unsafe response size: {command[0]}")
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditFailure(f"JSON check returned malformed output: {command[0]}") from error
    if not isinstance(result, dict):
        raise AuditFailure(f"JSON check did not return an object: {command[0]}")
    return {
        "command": command,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "result": result,
    }


def require_desktop_build_provenance(path: Path, current_inputs: dict[str, Any]) -> dict[str, Any]:
    expected_current_keys = {"schema_version", "sha256", "file_count"}
    expected_provenance_keys = {
        "schema_version",
        "sha256",
        "public_registry_source_sha256",
        "public_registry_snapshot_sha256",
        "public_registry_locator_index_sha256",
    }
    provenance = read_json(path)
    if (
        set(current_inputs) != expected_current_keys
        or current_inputs.get("schema_version") != "vibapp.desktop-build-inputs.v1"
        or not SHA256_RE.fullmatch(str(current_inputs.get("sha256", "")))
        or not isinstance(current_inputs.get("file_count"), int)
        or current_inputs["file_count"] < 1
        or set(provenance) != expected_provenance_keys
        or provenance.get("schema_version") != "vibapp.desktop-build-inputs.v1"
        or provenance.get("sha256") != current_inputs.get("sha256")
        or any(
            not SHA256_RE.fullmatch(str(provenance.get(key, "")))
            for key in expected_provenance_keys - {"schema_version"}
        )
    ):
        raise AuditFailure("packaged Desktop provenance is malformed or does not bind the current input digest")
    return provenance


def require_binary_marker(path: Path, marker: str) -> None:
    binary = read_bytes(path, 128 * 1024 * 1024)
    if binary.count(marker.encode("ascii")) != 1:
        raise AuditFailure(f"packaged binary lacks one exact build receipt: {path.name}")


def require_package_source_parity() -> dict[str, Any]:
    if not NODE:
        raise AuditFailure("node is unavailable for the Desktop input/projection checks")
    desktop = ARTIFACTS / "desktop"
    contents = desktop / "dist/VibApp.app/Contents"
    bundle = desktop / "dist/VibApp.app"
    current_inputs_evidence = run_json_command([NODE, str(desktop / "scripts/desktop-build-inputs.mjs")])
    current_inputs = current_inputs_evidence["result"]
    build_provenance_path = contents / "Resources/build-provenance/desktop-inputs.json"
    build_provenance = require_desktop_build_provenance(build_provenance_path, current_inputs)
    codesign = run_checked([
        "/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle),
    ])
    packaged_binaries = {
        "launcher": contents / "MacOS/vibapp-launcher",
        "runtime": contents / "MacOS/vibapp-runtime",
        "service-runtime": contents / "Resources/runtime-daemon/service-runtime/vibapp-service-runtime",
    }
    service_source_binary = ARTIFACTS / "runtime-daemon/target-service-1_98/release/vibapp-service-runtime"
    binary_evidence: dict[str, Any] = {}
    desktop_input_marker = f"VIBAPP_DESKTOP_BUILD_INPUTS_SHA256={build_provenance['sha256']}"
    registry_source_marker = (
        f"VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256={build_provenance['public_registry_source_sha256']}"
    )
    for name in ("launcher", "runtime"):
        packaged = packaged_binaries[name]
        require_binary_marker(packaged, desktop_input_marker)
        require_binary_marker(packaged, registry_source_marker)
        binary_evidence[name] = {
            "uuid": macho_uuid(packaged),
            "packaged_sha256": sha256(packaged, 128 * 1024 * 1024),
            "desktop_inputs_sha256": build_provenance["sha256"],
            "public_registry_source_sha256": build_provenance["public_registry_source_sha256"],
        }
    source_service_uuid = macho_uuid(service_source_binary)
    packaged_service_uuid = macho_uuid(packaged_binaries["service-runtime"])
    if source_service_uuid != packaged_service_uuid:
        raise AuditFailure("packaged service-runtime is not the current signed form of its release binary")
    binary_evidence["service-runtime"] = {
        "uuid": source_service_uuid,
        "release_sha256": sha256(service_source_binary, 128 * 1024 * 1024),
        "packaged_sha256": sha256(packaged_binaries["service-runtime"], 128 * 1024 * 1024),
    }

    packaged_public_registry = contents / "Resources/public-registry/data"
    public_registry_evidence = run_json_command([
        NODE,
        str(ARTIFACTS / "web-client-core/sync-public-package-locators.mjs"),
        "--verify-production-data-root",
        str(packaged_public_registry.resolve(strict=True)),
    ])
    public_registry_result = public_registry_evidence["result"]
    if (
        set(public_registry_result) != {"registry_snapshot_sha256", "locator_index_sha256", "locator_count"}
        or public_registry_result.get("registry_snapshot_sha256")
        != build_provenance["public_registry_snapshot_sha256"]
        or public_registry_result.get("locator_index_sha256")
        != build_provenance["public_registry_locator_index_sha256"]
        or not isinstance(public_registry_result.get("locator_count"), int)
        or public_registry_result["locator_count"] < 0
    ):
        raise AuditFailure("packaged public Registry projection does not match Desktop provenance")

    resources = (
        (desktop / "packaging/macos/Info.plist", contents / "Info.plist"),
        (ARTIFACTS / "registry/registry_service.py", contents / "Resources/registry/registry_service.py"),
        (ARTIFACTS / "registry/fixtures/catalog.json", contents / "Resources/registry/fixtures/catalog.json"),
        (ARTIFACTS / "orchestrator/orchestrator.py", contents / "Resources/orchestrator/orchestrator.py"),
        (ARTIFACTS / "orchestrator/dry_run_adapter.py", contents / "Resources/orchestrator/dry_run_adapter.py"),
        (ARTIFACTS / "orchestrator/delivery_controller.py", contents / "Resources/orchestrator/delivery_controller.py"),
        (ARTIFACTS / "cloud-agent/cloud_agent.py", contents / "Resources/cloud-agent/cloud_agent.py"),
        (ARTIFACTS / "codeagent-adapter/codeagent_adapter.py", contents / "Resources/codeagent-adapter/codeagent_adapter.py"),
        (ARTIFACTS / "app-builder/app_builder.py", contents / "Resources/app-builder/app_builder.py"),
        (ARTIFACTS / "app-builder/verifier.py", contents / "Resources/app-builder/verifier.py"),
        (ARTIFACTS / "app-builder/common.py", contents / "Resources/app-builder/common.py"),
        (ARTIFACTS / "app-builder/descriptor_reconciliation.py", contents / "Resources/app-builder/descriptor_reconciliation.py"),
        (ARTIFACTS / "app-builder/schemas/candidate.schema.json", contents / "Resources/app-builder/schemas/candidate.schema.json"),
        (ARTIFACTS / "app-builder/schemas/quarantine-receipt.schema.json", contents / "Resources/app-builder/schemas/quarantine-receipt.schema.json"),
        (ARTIFACTS / "app-builder/schemas/verifier-decision.schema.json", contents / "Resources/app-builder/schemas/verifier-decision.schema.json"),
        (ARTIFACTS / "runtime-daemon/vibapp_daemon/__init__.py", contents / "Resources/runtime-daemon/vibapp_daemon/__init__.py"),
        (ARTIFACTS / "runtime-daemon/vibapp_daemon/__main__.py", contents / "Resources/runtime-daemon/vibapp_daemon/__main__.py"),
        (ARTIFACTS / "runtime-daemon/vibapp_daemon/cli.py", contents / "Resources/runtime-daemon/vibapp_daemon/cli.py"),
        (ARTIFACTS / "runtime-daemon/vibapp_daemon/core.py", contents / "Resources/runtime-daemon/vibapp_daemon/core.py"),
        (ARTIFACTS / "runtime-daemon/vibapp_daemon/service_executor.py", contents / "Resources/runtime-daemon/vibapp_daemon/service_executor.py"),
        (ARTIFACTS / "registry-store/local_appstore.py", contents / "Resources/registry-store/local_appstore.py"),
        (ARTIFACTS / "need-analyzer/need_analyzer.py", contents / "Resources/need-analyzer/need_analyzer.py"),
        (ARTIFACTS / "product-config/llm.env.example", contents / "Resources/product-config/llm.env.example"),
        (ARTIFACTS / "cloud-agent/schemas/cloud-codeagent-task.schema.json", contents / "Resources/cloud-agent/schemas/cloud-codeagent-task.schema.json"),
        (ARTIFACTS / "cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json", contents / "Resources/cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json"),
        (ARTIFACTS / "cloud-agent/schemas/provider-result.schema.json", contents / "Resources/cloud-agent/schemas/provider-result.schema.json"),
        (ARTIFACTS / "cloud-agent/schemas/source-handoff.schema.json", contents / "Resources/cloud-agent/schemas/source-handoff.schema.json"),
        (ARTIFACTS / "cloud-agent/fixtures/dry-run/provider-result.json", contents / "Resources/cloud-agent/fixtures/dry-run/provider-result.json"),
        (ARTIFACTS / "cloud-agent/fixtures/dry-run/source/Cargo.toml", contents / "Resources/cloud-agent/fixtures/dry-run/source/Cargo.toml"),
        (ARTIFACTS / "cloud-agent/fixtures/dry-run/source/src/lib.rs", contents / "Resources/cloud-agent/fixtures/dry-run/source/src/lib.rs"),
        (REPO / "wit/experimental-v0/contract.wit", contents / "wit/experimental-v0/contract.wit"),
    )
    resource_evidence: dict[str, str] = {}
    for source, packaged in resources:
        source_digest = sha256(source, 128 * 1024 * 1024)
        if source_digest != sha256(packaged, 128 * 1024 * 1024):
            raise AuditFailure(f"packaged authored resource drift: {packaged.relative_to(contents)}")
        resource_evidence[str(packaged.relative_to(contents))] = source_digest

    service_inputs = [
        ARTIFACTS / "runtime-daemon/service-runtime/Cargo.toml",
        ARTIFACTS / "runtime-daemon/service-runtime/Cargo.lock",
        *ARTIFACTS.glob("runtime-daemon/service-runtime/src/**/*.rs"),
    ]
    service_inputs = [path for path in service_inputs if path.is_file() and not path.is_symlink()]
    newest_service_input = max(service_inputs, key=lambda path: path.stat().st_mtime_ns)
    if service_source_binary.stat().st_mtime_ns < newest_service_input.stat().st_mtime_ns:
        raise AuditFailure(f"service runtime predates its current source: {newest_service_input}")
    return {
        "binary_build_identity": binary_evidence,
        "current_desktop_inputs": current_inputs,
        "current_desktop_inputs_check": current_inputs_evidence,
        "packaged_build_provenance": build_provenance,
        "packaged_build_provenance_sha256": sha256(build_provenance_path),
        "packaged_public_registry_check": public_registry_evidence,
        "codesign": codesign,
        "authored_resource_count": len(resource_evidence),
        "authored_resources": resource_evidence,
        "newest_service_runtime_input": str(newest_service_input),
        "service_runtime_not_older_than_inputs": True,
    }


def run_checked(command: list[str], timeout: int = 45) -> dict[str, Any]:
    tool_path = f"{Path(NODE).parent}:" if NODE else ""
    completed = subprocess.run(
        command,
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env={
            "PATH": tool_path + "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C",
            "HOME": os.environ.get("HOME", str(Path.home())),
        },
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4096:]
        raise AuditFailure(f"check failed ({command[0]}): {detail}")
    return {
        "command": command,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "returncode": completed.returncode,
    }


def require_live_delivery() -> dict[str, Any]:
    task_path = LIVE_ROOT / "authorized-inputs/revision-006/task.json"
    registry_path = LIVE_ROOT / "authorized-inputs/revision-006/registry-no-match.json"
    attempt_path = LIVE_ROOT / f"tasks/{TASK_ID}/attempts/{ATTEMPT_ID}/attempt.json"
    task = read_json(task_path)
    registry = read_json(registry_path)
    attempt = read_json(attempt_path)
    registry_digest = hashlib.sha256(canonical(registry)).hexdigest()
    outputs = attempt.get("outputs", {})
    if (
        task.get("provider") != "openai-codex"
        or task.get("model") != "gpt-5.6-sol"
        or registry.get("route") != "refinement"
        or registry.get("recommendations") != []
        or registry.get("codeagent_handoff") != {"created": False, "permitted": False}
        or attempt.get("task_id") != TASK_ID
        or attempt.get("attempt_id") != ATTEMPT_ID
        or attempt.get("status") != "private-appstore-ready"
        or attempt.get("stage") != "private-appstore-ready"
        or attempt.get("progress_percent") != 100
        or attempt.get("registry_evidence_sha256") != registry_digest
        or outputs.get("package_digest_sha256") != PACKAGE_DIGEST
        or outputs.get("external_request_attempted") is not True
        or outputs.get("digest_equality_proven") is not True
        or outputs.get("publication_performed") is not False
        or outputs.get("appstore_created") is not True
    ):
        raise AuditFailure("retained real CodeAgent delivery binding is incomplete")
    status = read_json(LIVE_ROOT / f"tasks/{TASK_ID}/attempts/{ATTEMPT_ID}/codeagent/status.json")
    handoff = read_json(LIVE_ROOT / outputs["source_handoff_path"])
    candidate = read_json(LIVE_ROOT / "appstore/candidates" / PACKAGE_DIGEST / "candidate.json")
    package = LIVE_ROOT / "appstore/candidates" / PACKAGE_DIGEST / "package"
    if (
        status.get("provider_process_started") is not True
        or status.get("external_request_attempted") is not True
        or status.get("builder_invoked") is not False
        or handoff.get("provider_execution", {}).get("mode") != "direct-local-explicit-opt-in"
        or candidate.get("package_digest_sha256") != PACKAGE_DIGEST
        or candidate.get("verification", {}).get("authority") != "independent-verifier"
        or any(row.get("outcome") != "pass" for row in candidate.get("verification", {}).get("checks", []))
        or sha256(package / "component.wasm", 128 * 1024 * 1024) != outputs.get("component_sha256")
    ):
        raise AuditFailure("real provider/Builder/Verifier/private-AppStore evidence failed revalidation")
    controller = run_checked([
        sys.executable, "-B", str(ARTIFACTS / "orchestrator/delivery_controller.py"),
        "--root", str(LIVE_ROOT), "--provider", "codex", "--model", "gpt-5.6-sol",
        "--appstore-root", str(LIVE_ROOT / "appstore"), "status", TASK_ID,
    ])
    return {
        "task_id": TASK_ID,
        "attempt_id": ATTEMPT_ID,
        "task_sha256": sha256(task_path),
        "registry_canonical_sha256": registry_digest,
        "attempt_sha256": sha256(attempt_path),
        "package_digest_sha256": PACKAGE_DIGEST,
        "controller_read_only_revalidation": controller,
        "publication_performed": False,
    }


def require_http(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "vibapp-final-local-audit/1"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if response.status != 200 or len(body) > 2 * 1024 * 1024:
                raise AuditFailure(f"unhealthy or oversized local response: {url}")
    except Exception as error:
        if isinstance(error, AuditFailure):
            raise
        raise AuditFailure(f"local health check failed: {url}") from error
    return {"url": url, "status": 200, "body_sha256": hashlib.sha256(body).hexdigest()}


def row(row_id: str, requirement: str, checks: list[Callable[[], Any]]) -> dict[str, Any]:
    evidence = [check() for check in checks]
    return {"id": row_id, "requirement": requirement, "status": "pass", "evidence": evidence}


def audit(arguments: argparse.Namespace) -> dict[str, Any]:
    if not NODE:
        raise AuditFailure("node is unavailable for the independent browser derivation check")
    regression = lambda: require_regression(arguments.full_regression_log)
    ui = ARTIFACTS / "desktop/ui/app.js"
    screenshot = ARTIFACTS / "product-platform/website/output/playwright/round006-authoritative-task-preview.png"
    delivery: dict[str, Any] | None = None

    def live_once() -> dict[str, Any]:
        nonlocal delivery
        if delivery is None:
            delivery = require_live_delivery()
        return delivery

    rows = [
        row("need-confirmation", "Natural-language intake refines missing fields and confirms NeedSpec.", [
            lambda: require_tokens(ui, ("submit_need", "complete_need", "needspec-form", "remote_processing_consent")),
            lambda: {"browser_acceptance_screenshot": str(screenshot), "sha256": sha256(screenshot, 32 * 1024 * 1024)},
            regression,
        ]),
        row("registry-first", "Registry produces a compatible recommendation or deterministic no-match before development.", [
            live_once,
            lambda: require_tokens(ARTIFACTS / "desktop/src-tauri/src/registry_integration.rs", ("recommendations", "route", "refinement")),
            regression,
        ]),
        row("real-delivery", "Confirmed no-match traverses real CodeAgent, isolated Builder, independent Verifier and private AppStore.", [live_once, regression]),
        row("durable-history", "Restart-safe task history exposes chat, progress, failure, edit/retry, detail and run actions.", [
            lambda: require_tokens(ui, ("data-open-job-chat", "data-edit-job", "data-job-app", "data-job-run", "attempts", "failure")),
            live_once,
            regression,
        ]),
        row("desktop-runtime", "Desktop preserves install/enable/launch/service/close/disable/uninstall semantics.", [
            lambda: require_tokens(ui, ("submit_install_intent", "surface-close", "service-start", "service-stop", 'data-app-action="disable"', 'data-app-action="uninstall"')),
            lambda: run_checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(ARTIFACTS / "desktop/dist/VibApp.app")]),
            regression,
        ]),
        row("shared-web-runtime", "Website is the same GUI source and uses trusted, foreground Worker derivation with stable app URLs.", [
            require_shared_gui,
            lambda: run_checked([NODE, str(ARTIFACTS / "web-client-core/verify-browser-derivation.mjs")]),
            lambda: require_tokens(ARTIFACTS / "web-client-core/web-bridge.js", ("product-invoke", "new Worker", "web-worker-foreground", "foreground-only")),
            lambda: require_http(arguments.share_url),
            regression,
        ]),
        row("settings", "GUI i18n, selectable CodeAgent and non-CodeAgent BYOM Chat/Responses settings are present.", [
            lambda: require_tokens(ui, ("settings_gui_locale", "settings_codeagent_provider", "settings_protocol_chat", "settings_protocol_responses", "settings_codeagent_excluded")),
            lambda: require_tokens(ARTIFACTS / "desktop/src-tauri/src/codeagent_settings.rs", ("openai-codex", "anthropic-claude-code", "opencode", "google-gemini-cli")),
            regression,
        ]),
        row("packaged-and-accepted", "Signed macOS package and local Website pass complete regression and a fresh independent goal gate.", [
            require_package_source_parity,
            lambda: require_current_process(
                "current-packaged-desktop",
                [str(ARTIFACTS / "desktop/dist/VibApp.app/Contents/MacOS/vibapp-launcher")],
            ),
            lambda: require_current_process(
                "current-local-website-product-backend",
                ["node", "scripts/start-local.mjs", "--port", "3000"],
            ),
            lambda: require_http(arguments.website_url),
            lambda: require_http(arguments.backend_health_url),
            lambda: require_http(arguments.preview_health_url),
            regression,
            lambda: require_goal_gate(arguments.acceptance_result),
        ]),
    ]
    return {
        "schema_version": "vibapp.final-local-product-audit.experimental-v1",
        "status": "pass",
        "scope": "bounded-local-macos-and-web-product",
        "public_deployment_claimed": False,
        "formal_stage0_web_activation_claimed": False,
        "native_windows_linux_certification_claimed": False,
        "provider_request_performed_by_audit": False,
        "rows": rows,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="VibApp bounded local final product audit")
    value.add_argument("--full-regression-log", type=Path, required=True)
    value.add_argument("--acceptance-result", type=Path, required=True)
    value.add_argument("--website-url", default="http://127.0.0.1:3000/")
    value.add_argument("--share-url", default="http://127.0.0.1:3000/apps/ai.vibapp.hello")
    value.add_argument("--backend-health-url", default="http://127.0.0.1:3189/healthz")
    value.add_argument("--preview-health-url", default="http://127.0.0.1:4174/healthz")
    value.add_argument("--output", type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result = audit(arguments)
    except Exception as error:
        failure = {"schema_version": "vibapp.final-local-product-audit.experimental-v1", "status": "fail", "error": str(error)[:4096]}
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
