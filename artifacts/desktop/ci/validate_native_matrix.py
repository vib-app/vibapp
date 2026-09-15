#!/usr/bin/env python3
"""Validate and enforce the honest native-build evidence ceiling.

This tool deliberately has no third-party dependency. It validates the stricter
cross-field rules that JSON Schema cannot express and refuses a blocked phase before
any packaging command can run. It does not perform or certify a native build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "vibapp.desktop-native-platform-matrix.experimental-v1"
EVIDENCE_LABELS = [
    "definition-validated",
    "package-definition-fixture-reproduced",
    "native-source-compiled",
    "native-tests-passed",
    "native-binaries-reproduced",
    "package-candidate-produced",
    "package-bytes-reproduced",
    "native-package-smoke-passed",
    "independently-accepted",
]
TARGETS = {
    "linux-x86_64": ("linux", "x86_64-unknown-linux-gnu", "tar.gz", "LINUX-"),
    "windows-x86_64": ("windows", "x86_64-pc-windows-msvc", "zip", "WINDOWS-"),
}
PHASES = ("compile", "package", "native_smoke", "independent_acceptance")
BLOCKED_PHASES = ("package", "native_smoke", "independent_acceptance")
EXPECTED_CURRENT_EVIDENCE = {
    "definition": "authored-not-independently-accepted",
    "package_definition": "synthetic-fixture-reproduced-not-native",
    "native_compile": "not-run",
    "native_tests": "not-run",
    "package": "not-produced",
    "native_smoke": "not-run",
    "independent_acceptance": "not-run",
    "support_claim": "not-verified",
}
SOURCE_MARKERS = {
    "LINUX-RESOURCE-002": ("artifacts/desktop/src-tauri/src/native_platform.rs", 'filename(parent) == Some("bin")'),
    "LINUX-NATIVE-003": ("artifacts/desktop/evidence/platform-matrix-20260827.md", "Linux arm64 native GUI | NOT VERIFIED"),
    "LINUX-DAEMON-004": ("artifacts/runtime-daemon/tests/run-validation.sh", "/Users/zhuzhe/.rustup/toolchains"),
    "LINUX-SIGNING-005": ("artifacts/desktop/scripts/package-macos.sh", "codesign"),
    "WINDOWS-IPC-001": ("artifacts/desktop/src-tauri/src/native_platform.rs", "WindowsOwnerNamedPipeRequired"),
    "WINDOWS-PYTHON-002": ("artifacts/desktop/src-tauri/src/native_platform.rs", "Windows Python 3.11+"),
    "WINDOWS-RESOURCE-003": ("artifacts/desktop/src-tauri/src/native_platform.rs", 'filename(parent) == Some("VibApp")'),
    "WINDOWS-SECRETS-004": ("artifacts/desktop/src-tauri/src/native_platform.rs", "WindowsOwnerAclRequired"),
    "WINDOWS-NATIVE-006": ("artifacts/desktop/evidence/platform-matrix-20260827.md", "Windows x86_64 native GUI | DEFINITION VALIDATED; NOT VERIFIED"),
    "WINDOWS-SIGNING-007": ("artifacts/desktop/scripts/package-macos.sh", "codesign"),
}
ARCHIVE_LAYOUTS = {
    "linux-x86_64": {
        "root": "vibapp",
        "embedded_manifest": "vibapp/PACKAGE-MANIFEST.json",
        "launcher": "vibapp/bin/vibapp-launcher",
        "runtime": "vibapp/bin/vibapp-runtime",
        "service_runtime": "vibapp/libexec/vibapp-service-runtime",
        "resources_prefix": "vibapp/resources/",
        "timestamp": "source_date_epoch-even-utc-second",
        "ordinary_files_only": True,
        "sorted_utf8_paths": True,
    },
    "windows-x86_64": {
        "root": "VibApp",
        "embedded_manifest": "VibApp/PACKAGE-MANIFEST.json",
        "launcher": "VibApp/vibapp-launcher.exe",
        "runtime": "VibApp/vibapp-runtime.exe",
        "service_runtime": "VibApp/libexec/vibapp-service-runtime.exe",
        "resources_prefix": "VibApp/resources/",
        "timestamp": "source_date_epoch-even-utc-second",
        "ordinary_files_only": True,
        "sorted_utf8_paths": True,
    },
}


class MatrixError(ValueError):
    pass


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MatrixError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object_pairs_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, MatrixError) as error:
        raise MatrixError(f"cannot parse {path}: {error}") from error
    if not isinstance(value, dict):
        raise MatrixError(f"{path} must contain one JSON object")
    return value


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise MatrixError(f"{label} keys differ: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _regular_file(root: Path, relative: str) -> Path:
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise MatrixError(f"unsafe repository-relative path: {relative}")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise MatrixError(f"required ordinary file is absent: {relative}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(matrix_path: Path, repository_root: Path) -> dict[str, Any]:
    matrix = load_json(matrix_path)
    _keys(matrix, {"$schema", "schema_version", "evidence_labels", "toolchain", "targets"}, "matrix")
    if matrix["$schema"] != "./native-platform-matrix.schema.json":
        raise MatrixError("matrix must reference the repository-local schema")
    if matrix["schema_version"] != SCHEMA_VERSION:
        raise MatrixError("unsupported matrix schema_version")
    if matrix["evidence_labels"] != EVIDENCE_LABELS:
        raise MatrixError("evidence labels must preserve the closed ordered claim ladder")

    toolchain = matrix["toolchain"]
    if not isinstance(toolchain, dict):
        raise MatrixError("toolchain must be an object")
    _keys(toolchain, {"rust_release", "network_during_build", "cargo_flags", "required_environment", "launcher_platform_policy", "lockfiles", "package_assembler"}, "toolchain")
    if toolchain["rust_release"] != "1.93.0" or toolchain["network_during_build"] != "denied":
        raise MatrixError("native build must stay pinned to Rust 1.93.0 with network denied")
    if toolchain["cargo_flags"] != ["--locked", "--offline", "--jobs", "1"]:
        raise MatrixError("native Cargo flags must remain locked, offline, and single-job")
    if toolchain["required_environment"] != [
        "VIBAPP_NATIVE_CARGO",
        "VIBAPP_NATIVE_RUSTC",
        "VIBAPP_NATIVE_RUSTDOC",
        "VIBAPP_NATIVE_CARGO_HOME",
    ]:
        raise MatrixError("native tool paths must be explicit caller-owned inputs")
    platform_policy = toolchain["launcher_platform_policy"]
    if not isinstance(platform_policy, dict):
        raise MatrixError("launcher_platform_policy must be an object")
    _keys(platform_policy, {"path", "sha256", "validation_scope", "native_execution"}, "launcher_platform_policy")
    if platform_policy != {
        "path": "artifacts/desktop/src-tauri/src/native_platform.rs",
        "sha256": platform_policy["sha256"],
        "validation_scope": "macos-hosted-policy-tests-only",
        "native_execution": "not-run",
    }:
        raise MatrixError("launcher platform policy overstates its validation scope")
    if not isinstance(platform_policy["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", platform_policy["sha256"]):
        raise MatrixError("invalid launcher platform policy digest")
    if _sha256(_regular_file(repository_root, platform_policy["path"])) != platform_policy["sha256"]:
        raise MatrixError("launcher platform policy digest drift")
    lockfiles = toolchain["lockfiles"]
    if not isinstance(lockfiles, list) or len(lockfiles) != 2:
        raise MatrixError("exactly two native lockfiles must be pinned")
    seen_lockfiles: set[str] = set()
    for entry in lockfiles:
        if not isinstance(entry, dict):
            raise MatrixError("lockfile entries must be objects")
        _keys(entry, {"path", "sha256"}, "lockfile")
        path, digest = entry["path"], entry["sha256"]
        if not isinstance(path, str) or path in seen_lockfiles:
            raise MatrixError("lockfile paths must be unique strings")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MatrixError(f"invalid lockfile digest for {path}")
        seen_lockfiles.add(path)
        if _sha256(_regular_file(repository_root, path)) != digest:
            raise MatrixError(f"lockfile digest drift: {path}")
    assembler = toolchain["package_assembler"]
    if not isinstance(assembler, dict):
        raise MatrixError("package_assembler must be an object")
    _keys(assembler, {"path", "sha256", "input_schema_version", "maximum_files", "maximum_file_bytes", "maximum_total_bytes"}, "package_assembler")
    if assembler != {
        "path": "artifacts/desktop/ci/assemble_native_package.py",
        "sha256": assembler["sha256"],
        "input_schema_version": "vibapp.native-package-input.experimental-v1",
        "maximum_files": 512,
        "maximum_file_bytes": 64 * 1024 * 1024,
        "maximum_total_bytes": 128 * 1024 * 1024,
    }:
        raise MatrixError("package assembler contract differs from the bounded accepted definition")
    if not isinstance(assembler["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", assembler["sha256"]):
        raise MatrixError("invalid package assembler digest")
    if _sha256(_regular_file(repository_root, assembler["path"])) != assembler["sha256"]:
        raise MatrixError("package assembler digest drift")

    targets = matrix["targets"]
    if not isinstance(targets, list) or len(targets) != len(TARGETS):
        raise MatrixError("matrix must contain exactly the Linux and Windows targets")
    seen_targets: set[str] = set()
    seen_blockers: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise MatrixError("target entries must be objects")
        _keys(
            target,
            {"id", "host_os", "rust_target", "runner_labels", "package_format", "release_eligible", "current_evidence", "archive_layout", "phases", "blockers"},
            "target",
        )
        target_id = target["id"]
        if target_id not in TARGETS or target_id in seen_targets:
            raise MatrixError(f"unknown or duplicate target: {target_id}")
        seen_targets.add(target_id)
        host_os, rust_target, package_format, blocker_prefix = TARGETS[target_id]
        if (target["host_os"], target["rust_target"], target["package_format"]) != (host_os, rust_target, package_format):
            raise MatrixError(f"target tuple mismatch for {target_id}")
        labels = target["runner_labels"]
        if not isinstance(labels, list) or "self-hosted" not in labels or host_os not in labels or "vibapp-rust-1.93-offline" not in labels:
            raise MatrixError(f"{target_id} must require the matching pre-provisioned self-hosted runner")
        if target["release_eligible"] is not False:
            raise MatrixError(f"{target_id} cannot be release eligible")
        if target["current_evidence"] != EXPECTED_CURRENT_EVIDENCE:
            raise MatrixError(f"{target_id} overstates current evidence")
        if target["archive_layout"] != ARCHIVE_LAYOUTS[target_id]:
            raise MatrixError(f"{target_id} archive layout differs from the exact package definition")

        blockers = target["blockers"]
        if not isinstance(blockers, list) or not blockers:
            raise MatrixError(f"{target_id} needs explicit blockers")
        target_blockers: set[str] = set()
        for blocker in blockers:
            if not isinstance(blocker, dict):
                raise MatrixError("blockers must be objects")
            _keys(blocker, {"id", "severity", "observed_in", "condition", "closes_when"}, "blocker")
            blocker_id = blocker["id"]
            if not isinstance(blocker_id, str) or not blocker_id.startswith(blocker_prefix):
                raise MatrixError(f"blocker {blocker_id!r} has the wrong target prefix")
            if blocker_id in seen_blockers:
                raise MatrixError(f"duplicate blocker: {blocker_id}")
            seen_blockers.add(blocker_id)
            target_blockers.add(blocker_id)
            if blocker["severity"] not in {"release-blocking", "security-blocking"}:
                raise MatrixError(f"invalid severity for {blocker_id}")
            observed = blocker["observed_in"]
            if not isinstance(observed, list) or not observed or len(observed) != len(set(observed)):
                raise MatrixError(f"{blocker_id} needs unique observed_in paths")
            for relative in observed:
                if not isinstance(relative, str) or not relative.startswith("artifacts/"):
                    raise MatrixError(f"{blocker_id} has an invalid observed path")
                _regular_file(repository_root, relative)
            for field in ("condition", "closes_when"):
                if not isinstance(blocker[field], str) or not blocker[field].strip():
                    raise MatrixError(f"{blocker_id} has an empty {field}")

        phases = target["phases"]
        if not isinstance(phases, dict):
            raise MatrixError(f"{target_id} phases must be an object")
        _keys(phases, set(PHASES), f"{target_id} phases")
        for phase_name, phase in phases.items():
            if not isinstance(phase, dict):
                raise MatrixError(f"{target_id}/{phase_name} must be an object")
            _keys(phase, {"state", "emits_at_most", "blockers"}, f"{target_id}/{phase_name}")
            if phase["emits_at_most"] not in EVIDENCE_LABELS:
                raise MatrixError(f"{target_id}/{phase_name} has an unknown evidence ceiling")
            refs = phase["blockers"]
            if not isinstance(refs, list) or len(refs) != len(set(refs)) or not set(refs).issubset(target_blockers):
                raise MatrixError(f"{target_id}/{phase_name} has invalid blocker references")
            if phase_name == "compile":
                if phase["state"] != "runnable-on-matching-native-runner" or refs:
                    raise MatrixError(f"{target_id} compile may run only on its matching native runner")
            elif phase["state"] != "blocked" or not refs:
                raise MatrixError(f"{target_id}/{phase_name} must fail closed while blockers remain")

    if seen_targets != set(TARGETS) or seen_blockers != set(SOURCE_MARKERS):
        raise MatrixError("target or blocker inventory differs from the closed expected set")
    for blocker_id, (relative, marker) in SOURCE_MARKERS.items():
        contents = _regular_file(repository_root, relative).read_text(encoding="utf-8")
        if marker not in contents:
            raise MatrixError(f"{blocker_id} source marker disappeared; repair the code and update the matrix together")
    return matrix


def _target(matrix: dict[str, Any], target_id: str) -> dict[str, Any]:
    return next(target for target in matrix["targets"] if target["id"] == target_id)


def preflight(matrix_path: Path, repository_root: Path, target_id: str, phase: str) -> int:
    matrix = validate(matrix_path, repository_root)
    target = _target(matrix, target_id)
    phase_definition = target["phases"][phase]
    actual_os = platform.system().lower()
    if actual_os != target["host_os"]:
        raise MatrixError(f"{target_id}/{phase} requires native {target['host_os']}; current host is {actual_os}")
    if phase_definition["state"] == "blocked":
        print(
            json.dumps(
                {
                    "result": "blocked",
                    "target": target_id,
                    "phase": phase,
                    "blockers": phase_definition["blockers"],
                    "support_claim": "not-verified",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78
    print(
        json.dumps(
            {
                "result": "ready-for-native-execution",
                "target": target_id,
                "phase": phase,
                "evidence_ceiling": phase_definition["emits_at_most"],
                "support_claim": "not-verified-until-command-evidence-exists",
            },
            sort_keys=True,
        )
    )
    return 0


def repository_root_from_script() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=Path(__file__).with_name("native-platform-matrix.json"))
    parser.add_argument("--repository-root", type=Path, default=repository_root_from_script())
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    preflight_parser.add_argument("--phase", choices=PHASES, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate":
            validate(arguments.matrix, arguments.repository_root.resolve())
            print(json.dumps({"result": "pass", "evidence_label": "definition-validated", "native_execution": "not-run"}, sort_keys=True))
            return 0
        return preflight(arguments.matrix, arguments.repository_root.resolve(), arguments.target, arguments.phase)
    except MatrixError as error:
        print(f"native matrix rejected: {error}", file=sys.stderr)
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
