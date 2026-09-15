#!/usr/bin/env python3
"""Independent-process verifier for the local Builder."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import zipfile

from build_product import (
    BASE,
    BUILDER_IMAGE,
    DOCKER,
    MAX_LOG_BYTES,
    REPO,
    REQUIRED_IMPORTS,
    BuildFailure,
    canonical_json,
    package_digest,
    run_capped,
    sha256_file,
    strict_object,
    write_json,
)


VERIFY_TIMEOUT_SECONDS = 120
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
EXPECTED_PACKAGE_FILES = {
    "manifest.json",
    "component.wasm",
    "provenance.json",
    "sbom.cdx.json",
}


def strict_json(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > 1_048_576:
        raise BuildFailure(f"unbounded or non-regular JSON: {path.name}")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildFailure(f"invalid JSON {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise BuildFailure(f"JSON root is not an object: {path.name}")
    return value


def require_bounded_tree(candidate_dir: Path, archive_name: str) -> Path:
    expected_top = {"handoff.json", archive_name, "package"}
    actual_top = {entry.name for entry in os.scandir(candidate_dir)}
    if actual_top != expected_top:
        raise BuildFailure(f"candidate boundary mismatch: {sorted(actual_top)}")
    package_dir = candidate_dir / "package"
    if not package_dir.is_dir() or package_dir.is_symlink():
        raise BuildFailure("package must be a real directory")
    actual_package = {entry.name for entry in os.scandir(package_dir)}
    if actual_package != EXPECTED_PACKAGE_FILES:
        raise BuildFailure(f"package boundary mismatch: {sorted(actual_package)}")
    total = 0
    for path in [candidate_dir / "handoff.json", candidate_dir / archive_name] + [
        package_dir / name for name in sorted(EXPECTED_PACKAGE_FILES)
    ]:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise BuildFailure(f"non-regular candidate path: {path.name}")
        total += metadata.st_size
    if total > MAX_PACKAGE_BYTES:
        raise BuildFailure("candidate exceeds 32 MiB verifier budget")
    return package_dir


def verify_archive(archive_path: Path, package_dir: Path) -> None:
    if archive_path.stat().st_size > MAX_PACKAGE_BYTES:
        raise BuildFailure("archive exceeds verifier budget")
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if names != ["manifest.json", "component.wasm", "provenance.json", "sbom.cdx.json"]:
            raise BuildFailure(f"archive entries/order mismatch: {names}")
        total = 0
        for info in archive.infolist():
            if info.is_dir() or "/" in info.filename or "\\" in info.filename:
                raise BuildFailure("archive contains a non-flat or directory entry")
            mode = (info.external_attr >> 16) & 0o170000
            if mode not in (0, stat.S_IFREG):
                raise BuildFailure("archive contains a non-regular entry")
            total += info.file_size
            if total > MAX_PACKAGE_BYTES:
                raise BuildFailure("archive expanded size exceeds verifier budget")
            if archive.read(info.filename) != (package_dir / info.filename).read_bytes():
                raise BuildFailure(f"archive/package byte mismatch: {info.filename}")


def descriptor_files(manifest: dict[str, object]) -> list[dict[str, object]]:
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    rows = [
        artifacts["canonical_component"],
        *artifacts["assets"],
        artifacts["provenance"],
        artifacts["sbom"],
    ]
    for derivation in artifacts["browser_derivations"]:
        rows.extend(derivation["files"])
        rows.append(derivation["derivation_attestation"])
    return rows


def verify_descriptors(manifest: dict[str, object], package_dir: Path) -> None:
    described: set[str] = set()
    for descriptor in descriptor_files(manifest):
        path = descriptor["path"]
        if not isinstance(path, str) or path in described or path not in EXPECTED_PACKAGE_FILES:
            raise BuildFailure(f"invalid/duplicate artifact path: {path}")
        described.add(path)
        file_path = package_dir / path
        if file_path.stat().st_size != descriptor["size_bytes"]:
            raise BuildFailure(f"artifact size mismatch: {path}")
        if sha256_file(file_path) != descriptor["sha256"]:
            raise BuildFailure(f"artifact digest mismatch: {path}")
    if described != EXPECTED_PACKAGE_FILES - {"manifest.json"}:
        raise BuildFailure(f"artifact coverage mismatch: {sorted(described)}")


def docker_verify(package_dir: Path) -> tuple[list[str], str]:
    command = [
        str(DOCKER),
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--cpus",
        "2",
        "--pids-limit",
        "64",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=536870912,mode=1777",
        "--tmpfs",
        "/exec:rw,exec,nosuid,nodev,size=134217728,mode=1777",
        "-e",
        "HOME=/tmp/home",
        "-e",
        "CARGO_HOME=/tmp/cargo-home",
        "-e",
        "CARGO_INCREMENTAL=0",
        "-e",
        "CARGO_NET_OFFLINE=true",
        "-e",
        "RUSTUP_AUTO_INSTALL=0",
        "-e",
        "TZ=UTC",
        "-e",
        "LANG=C.UTF-8",
        "-e",
        "LC_ALL=C.UTF-8",
        "--mount",
        f"type=bind,source={package_dir},target=/candidate,readonly",
        "--mount",
        f"type=bind,source={BASE},target=/workspace/builder,readonly",
        "--mount",
        f"type=bind,source={REPO},target=/workspace/repo,readonly",
        "--entrypoint",
        "/bin/sh",
        BUILDER_IMAGE,
        "/workspace/builder/verify_inside.sh",
    ]
    return command, run_capped(command, VERIFY_TIMEOUT_SECONDS, MAX_LOG_BYTES)


def extract_section(output: str, begin: str, end: str) -> str:
    pattern = re.compile(re.escape(begin) + r"\n(.*?)\n" + re.escape(end), re.DOTALL)
    match = pattern.search(output)
    if not match:
        raise BuildFailure(f"missing verifier output section {begin}")
    return match.group(1)


def verify(candidate_dir: Path) -> dict[str, object]:
    resolved_base = BASE.resolve()
    candidate_dir = candidate_dir.resolve(strict=True)
    if candidate_dir != resolved_base and resolved_base not in candidate_dir.parents:
        raise BuildFailure("candidate must stay inside the Builder root")
    handoff = strict_json(candidate_dir / "handoff.json")
    if handoff.get("status") != "builder-output-untrusted":
        raise BuildFailure("builder handoff status must remain untrusted")
    archive_name = handoff.get("archive")
    if not isinstance(archive_name, str) or not re.fullmatch(r"[a-z0-9.-]+\.vibapp", archive_name):
        raise BuildFailure("invalid handoff archive name")
    package_dir = require_bounded_tree(candidate_dir, archive_name)
    manifest = strict_json(package_dir / "manifest.json")
    if manifest.get("verification") != {"status": "unverified", "revocation": "not-revoked"}:
        raise BuildFailure("builder manifest must remain unverified and not-revoked")
    if manifest.get("package_format") != "vibapp.package.experimental-v0":
        raise BuildFailure("wrong package format")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("world") != "ui-only-reference":
        raise BuildFailure("candidate is not the ui-only-reference world")
    if runtime.get("required_imports") != REQUIRED_IMPORTS:
        raise BuildFailure("manifest required imports differ from frozen UI-only world")
    verify_descriptors(manifest, package_dir)
    digest = package_digest(manifest)
    component_digest = sha256_file(package_dir / "component.wasm")
    archive_path = candidate_dir / archive_name
    verify_archive(archive_path, package_dir)
    comparisons = {
        "package_digest_sha256": digest,
        "component_sha256": component_digest,
        "manifest_sha256": sha256_file(package_dir / "manifest.json"),
        "archive_sha256": sha256_file(archive_path),
        "builder_image_digest": BUILDER_IMAGE,
    }
    for key, actual in comparisons.items():
        if handoff.get(key) != actual:
            raise BuildFailure(f"handoff mismatch for {key}")

    command, tool_output = docker_verify(package_dir)
    for marker in ("WASM_VALIDATE_PASS", "MANIFEST_PROTOCOL_PASS"):
        if marker not in tool_output:
            raise BuildFailure(f"missing verifier marker: {marker}")
    wit = extract_section(tool_output, "WIT_BEGIN", "WIT_END")
    for interface in REQUIRED_IMPORTS:
        if interface not in wit:
            raise BuildFailure(f"component WIT is missing import {interface}")
    actual_imports = set(re.findall(r"^\s*import\s+([^;]+);\s*$", wit, re.MULTILINE))
    allowed_type_imports = {
        "vibapp:experimental-v0/common@0.0.1",
        "vibapp:experimental-v0/ui@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
    }
    expected_actual_imports = set(REQUIRED_IMPORTS) | allowed_type_imports
    if actual_imports != expected_actual_imports:
        raise BuildFailure(
            "component import reconciliation failed "
            f"missing={sorted(expected_actual_imports - actual_imports)} "
            f"extra={sorted(actual_imports - expected_actual_imports)}"
        )
    if "vibapp:experimental-v0/guest@0.0.1" not in wit:
        raise BuildFailure("component WIT is missing the guest export")
    prohibited = [
        "wasi:filesystem",
        "wasi:sockets",
        "wasi:cli/environment",
        "wasi:cli/run",
        "wasi:http",
    ]
    leaked = [name for name in prohibited if name in wit]
    if leaked:
        raise BuildFailure(f"component exposes prohibited ambient WASI imports: {leaked}")
    if "wit-bindgen-rust [0.60.0]" not in tool_output:
        raise BuildFailure("component metadata is not bound to wit-bindgen-rust 0.60.0")

    quarantine_dir = BASE / "quarantine" / digest
    quarantine_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    quarantined_archive = quarantine_dir / archive_name
    if quarantined_archive.exists() and sha256_file(quarantined_archive) != comparisons["archive_sha256"]:
        raise BuildFailure("quarantine already contains conflicting bytes")
    if not quarantined_archive.exists():
        shutil.copyfile(archive_path, quarantined_archive)
        quarantined_archive.chmod(0o600)
    record = {
        "schema_version": "vibapp.verifier-record.product-platform.v1",
        "status": "quarantined",
        "decision": "candidate-verifier-pass",
        "scope": "product-platform-local-hold",
        "accepted": False,
        "published": False,
        "installed": False,
        "package_digest_sha256": digest,
        "component_sha256": component_digest,
        "archive_sha256": comparisons["archive_sha256"],
        "archive": str(quarantined_archive),
        "verified_at": dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "checks": [
            "bounded-file-tree",
            "duplicate-key-rejection",
            "protocol-manifest-parse-and-semantics",
            "artifact-hash-size-coverage",
            "package-digest",
            "transport-archive-byte-equality",
            "wasm-tools-validate",
            "component-wit-import-export-reconciliation",
            "ambient-wasi-denial",
        ],
        "verifier_command": command,
        "limits": {
            "memory": "1g",
            "memory_swap": "1g",
            "cpus": 2,
            "pids": 64,
            "wall_seconds": VERIFY_TIMEOUT_SECONDS,
            "log_bytes": MAX_LOG_BYTES,
            "tmpfs_bytes": 536870912,
            "executable_verifier_tmpfs_bytes": 134217728,
        },
    }
    write_json(quarantine_dir / "verifier-record.json", record)
    (quarantine_dir / "verifier.log").write_text(tool_output, encoding="utf-8")
    (quarantine_dir / "verifier.log").chmod(0o600)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        record = verify(args.candidate)
    except (BuildFailure, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"VERIFY_REJECTED: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    else:
        print(f"QUARANTINED {record['archive']}")
        print("accepted=false published=false installed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
