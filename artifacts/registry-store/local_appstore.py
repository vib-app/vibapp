#!/usr/bin/env python3
"""Content-addressed local VibApp AppStore intake for verifier candidates.

This product-prototype adapter intentionally has no publish operation.  It accepts
only the frozen verifier-promoted candidate v1 handoff and preserves the adjacent
``candidate.json`` + ``package/`` shape required by the daemon install boundary.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import unicodedata
from typing import Any, Callable, Iterator

try:  # POSIX advisory lock.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by the Windows build matrix.
    fcntl = None  # type: ignore[assignment]

try:  # Windows byte-range lock.
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - unavailable on POSIX.
    msvcrt = None  # type: ignore[assignment]


CANDIDATE_SCHEMA = "vibapp.builder-candidate.experimental-v1"
RECORD_SCHEMA = "vibapp.local-appstore-record.experimental-v1"
RESULT_SCHEMA = "vibapp.local-appstore-result.experimental-v1"
MANIFEST_SCHEMA = "vibapp.manifest.experimental-v0.0.1"
PACKAGE_FORMAT = "vibapp.package.experimental-v0"
PRESENTATION_SCHEMA = "vibapp.host-presentation.experimental-v1"

MAX_CANDIDATE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PACKAGE_FILES = 256
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_REASON_BYTES = 1024
MAX_LIST_LIMIT = 100

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ARTIFACT_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class StoreError(Exception):
    """A closed, machine-readable local AppStore failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _windows_private(path: Path, *, directory: bool, created: bool = False) -> None:
    """Only newly created Store objects may receive ownership/private ACLs."""
    if os.name != "nt":
        return
    daemon_modules = str(Path(__file__).resolve().parent.parent / "runtime-daemon")
    if daemon_modules not in sys.path:
        sys.path.insert(0, daemon_modules)
    from vibapp_daemon import windows_security
    try:
        if created:
            windows_security.protect(path, directory=directory)
        else:
            windows_security.verify(path, directory=directory)
    except (OSError, ValueError) as error:
        raise StoreError("permission-denied", "Store storage must already be owner-private and not a Windows reparse point") from error


def _mkdir_private(path: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
    if os.name != "nt":
        path.mkdir(parents=parents, exist_ok=exist_ok, mode=0o700)
        return
    if parents and not path.parent.exists():
        _mkdir_private(path.parent, parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not exist_ok:
            raise
        _windows_private(path, directory=True)
    else:
        _windows_private(path, directory=True, created=True)


def _open_private_file(path: Path, flags: int) -> int:
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        return os.open(path, flags | os.O_CREAT, 0o600)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _windows_private(path, directory=False)
        return os.open(path, flags & ~(os.O_CREAT | os.O_EXCL), 0o600)
    try:
        _windows_private(path, directory=False, created=True)
        return fd
    except Exception:
        os.close(fd)
        raise


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> bytes:
    """JCS-compatible encoding for the manifest's integer-only JSON domain."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StoreError("malformed-output", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(data: bytes, *, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except StoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise StoreError("malformed-output", f"invalid {label} JSON") from error


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise StoreError("integrity-failure", f"{label} is not a lowercase SHA-256")
    return value


def _text(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise StoreError("malformed-output", f"{label} is not a string")
    encoded = value.encode("utf-8")
    if (not allow_empty and not encoded) or len(encoded) > maximum or "\x00" in value:
        raise StoreError("resource-limit", f"{label} is empty, contains NUL, or exceeds its bound")
    return value


def _derive_presentation(app_kind: str, display_name: str, description: str) -> dict[str, Any]:
    if app_kind not in ("ui", "service", "hybrid"):
        raise StoreError("incompatible-contract", "presentation app kind is unsupported")
    if not isinstance(display_name, str) or not isinstance(description, str):
        raise StoreError("malformed-output", "presentation identity text is malformed")
    normalized = unicodedata.normalize("NFKC", f"{display_name}\n{description}").casefold()
    if app_kind == "service":
        dimensions = (720, 480, 480, 320, False)
    elif any(marker in normalized for marker in (
        "clock", "timer", "stopwatch", "counter", "calculator", "reminder",
        "时钟", "计时", "秒表", "倒计时", "计数器", "计算器", "提醒",
    )):
        dimensions = (520, 300, 420, 240, True)
    elif any(marker in normalized for marker in (
        "dashboard", "workspace", "editor", "studio", "inventory", "kanban",
        "看板", "工作台", "编辑器", "库存", "管理台",
    )):
        dimensions = (1120, 760, 720, 480, True)
    elif app_kind == "hybrid":
        dimensions = (1040, 720, 640, 420, True)
    else:
        dimensions = (900, 640, 480, 360, True)
    preferred_width, preferred_height, minimum_width, minimum_height, resizable = dimensions
    return {
        "schema_version": PRESENTATION_SCHEMA,
        "preferred_width": preferred_width,
        "preferred_height": preferred_height,
        "minimum_width": minimum_width,
        "minimum_height": minimum_height,
        "resizable": resizable,
    }


def _presentation(
    value: Any, app_kind: str, display_name: str, description: str
) -> dict[str, Any]:
    if value is None:
        return _derive_presentation(app_kind, display_name, description)
    presentation = _exact_object(
        value,
        {"schema_version", "preferred_width", "preferred_height", "minimum_width", "minimum_height", "resizable"},
        "presentation",
    )
    if presentation.get("schema_version") != PRESENTATION_SCHEMA:
        raise StoreError("incompatible-contract", "presentation schema is unsupported")
    bounds = {
        "preferred_width": (320, 1920),
        "preferred_height": (240, 1200),
        "minimum_width": (320, 1280),
        "minimum_height": (240, 960),
    }
    normalized: dict[str, Any] = {"schema_version": PRESENTATION_SCHEMA}
    for field, (minimum, maximum) in bounds.items():
        item = presentation.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise StoreError("resource-limit", f"presentation.{field} is outside the host bound")
        normalized[field] = item
    if normalized["preferred_width"] < normalized["minimum_width"] or normalized["preferred_height"] < normalized["minimum_height"]:
        raise StoreError("incompatible-contract", "presentation preferred size is below its minimum")
    if not isinstance(presentation.get("resizable"), bool):
        raise StoreError("malformed-output", "presentation.resizable is not boolean")
    normalized["resizable"] = presentation["resizable"]
    return normalized


def _utc(value: Any, label: str) -> str:
    value = _text(value, label, 20)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise StoreError("malformed-output", f"{label} is not canonical whole-second UTC") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise StoreError("malformed-output", f"{label} is not canonical whole-second UTC")
    return value


def _exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StoreError("integrity-failure", f"{label} fields do not match the frozen contract")
    return value


def _artifact_path(value: Any, label: str) -> str:
    path = _text(value, label, 240)
    if not ARTIFACT_PATH_RE.fullmatch(path) or "\\" in path or "//" in path:
        raise StoreError("integrity-failure", f"{label} is not a safe artifact path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise StoreError("integrity-failure", f"{label} escapes the package")
    return path


def _read_fd(fd: int, maximum: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise StoreError("resource-limit", f"{label} exceeds {maximum} bytes")
    return b"".join(chunks)


def _copy_regular(source: Path, destination: Path, maximum: int, label: str) -> None:
    try:
        before = os.lstat(source)
    except OSError as error:
        raise StoreError("integrity-failure", f"cannot inspect {label}") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise StoreError("permission-denied", f"{label} is not a unique regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise StoreError("permission-denied", f"{label} is a symbolic link") from error
        raise
    try:
        info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_dev != before.st_dev
            or info.st_ino != before.st_ino
        ):
            raise StoreError("permission-denied", f"{label} is not a unique regular file")
        if info.st_size > maximum:
            raise StoreError("resource-limit", f"{label} exceeds {maximum} bytes")
        _mkdir_private(destination.parent, parents=True, exist_ok=True)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600 if os.name == "nt" else 0o400,
        )
        try:
            remaining = maximum
            while True:
                chunk = os.read(source_fd, min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    raise StoreError("resource-limit", f"{label} grew beyond its bound")
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            after = os.fstat(source_fd)
            if (
                after.st_dev != info.st_dev
                or after.st_ino != info.st_ino
                or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns
            ):
                raise StoreError("integrity-failure", f"{label} changed while being copied")
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        _windows_private(destination, directory=False, created=True)
    finally:
        os.close(source_fd)


def _hash_regular(path: Path, maximum: int, label: str) -> tuple[str, int]:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise StoreError("integrity-failure", f"missing {label}") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise StoreError("permission-denied", f"{label} is not a unique regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise StoreError("permission-denied", f"{label} is a symbolic link") from error
        raise StoreError("integrity-failure", f"missing {label}") from error
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_dev != before.st_dev
            or info.st_ino != before.st_ino
        ):
            raise StoreError("permission-denied", f"{label} is not a unique regular file")
        data = _read_fd(fd, maximum, label)
        after = os.fstat(fd)
        if after.st_size != len(data) or after.st_mtime_ns != info.st_mtime_ns:
            raise StoreError("integrity-failure", f"{label} changed while being read")
        return hashlib.sha256(data).hexdigest(), len(data)
    finally:
        os.close(fd)


def _read_regular(path: Path, maximum: int, label: str) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise StoreError("integrity-failure", f"cannot inspect {label}") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise StoreError("permission-denied", f"{label} is not a unique regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise StoreError("integrity-failure", f"cannot open {label}") from error
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_dev != before.st_dev
            or info.st_ino != before.st_ino
        ):
            raise StoreError("permission-denied", f"{label} is not a unique regular file")
        return _read_fd(fd, maximum, label)
    finally:
        os.close(fd)


def _safe_remove_tree(path: Path) -> None:
    """Remove only a staging tree created by this module, never following links."""

    if not path.exists():
        return
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            (root_path / name).unlink()
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                child.unlink()
            else:
                child.rmdir()
    path.rmdir()


def _snapshot_candidate(candidate: Path, staging: Path) -> None:
    if candidate.name != "candidate.json":
        raise StoreError("invalid-argument", "input must be the frozen candidate.json")
    _copy_regular(candidate, staging / "candidate.json", MAX_CANDIDATE_BYTES, "candidate.json")
    package = candidate.parent / "package"
    try:
        package_info = os.lstat(package)
    except OSError as error:
        raise StoreError("integrity-failure", "candidate has no adjacent package directory") from error
    if not stat.S_ISDIR(package_info.st_mode) or stat.S_ISLNK(package_info.st_mode):
        raise StoreError("permission-denied", "candidate package is not a real directory")
    _mkdir_private(staging / "package")
    file_count = 0
    total_bytes = 0
    for root, directories, files in os.walk(package, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(package)
        for name in list(directories):
            source_dir = root_path / name
            info = os.lstat(source_dir)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise StoreError("permission-denied", "package contains a linked or special directory")
            _mkdir_private(staging / "package" / relative_root / name)
        for name in files:
            source = root_path / name
            info = os.lstat(source)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise StoreError("permission-denied", "package contains a linked or special file")
            file_count += 1
            total_bytes += info.st_size
            if file_count > MAX_PACKAGE_FILES or total_bytes > MAX_PACKAGE_BYTES:
                raise StoreError("resource-limit", "package exceeds file or byte limits")
            relative = (relative_root / name).as_posix()
            _artifact_path(relative, "package path")
            _copy_regular(
                source,
                staging / "package" / relative_root / name,
                MAX_ARTIFACT_BYTES,
                relative,
            )


def _descriptor(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "media_type", "sha256", "size_bytes"}:
        raise StoreError("integrity-failure", f"{label} is not an exact artifact descriptor")
    path = _artifact_path(value["path"], f"{label}.path")
    digest = _digest(value["sha256"], f"{label}.sha256")
    size = value["size_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > MAX_ARTIFACT_BYTES:
        raise StoreError("resource-limit", f"{label}.size_bytes is outside the artifact bound")
    _text(value["media_type"], f"{label}.media_type", 128)
    return {"path": path, "sha256": digest, "size_bytes": size, "media_type": value["media_type"]}


def _manifest_descriptors(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise StoreError("incompatible-contract", "manifest artifacts are missing")
    result = [_descriptor(artifacts.get("canonical_component"), "canonical_component")]
    assets = artifacts.get("assets")
    derivations = artifacts.get("browser_derivations")
    if not isinstance(assets, list) or len(assets) > 128:
        raise StoreError("incompatible-contract", "manifest assets are malformed")
    if not isinstance(derivations, list) or len(derivations) > 2:
        raise StoreError("incompatible-contract", "manifest browser derivations are malformed")
    result.extend(_descriptor(item, "asset") for item in assets)
    for derivation in derivations:
        if not isinstance(derivation, dict):
            raise StoreError("incompatible-contract", "browser derivation is malformed")
        files = derivation.get("files")
        if not isinstance(files, list) or not files or len(files) > 32:
            raise StoreError("incompatible-contract", "browser derivation files are malformed")
        normalized_files = [_descriptor(item, "browser derivation file") for item in files]
        entry = _descriptor(derivation.get("entry"), "browser derivation entry")
        if sum(entry == item for item in normalized_files) != 1:
            raise StoreError("integrity-failure", "browser derivation entry is not one exact files item")
        canonical_digest = result[0]["sha256"]
        if derivation.get("derived_from_sha256") != canonical_digest:
            raise StoreError("integrity-failure", "browser derivation is not bound to the canonical component")
        result.extend(normalized_files)
        result.append(_descriptor(derivation.get("derivation_attestation"), "derivation attestation"))
    result.append(_descriptor(artifacts.get("provenance"), "provenance"))
    result.append(_descriptor(artifacts.get("sbom"), "sbom"))
    paths = [item["path"] for item in result]
    if len(paths) != len(set(paths)) or "manifest.json" in paths or "signature.json" in paths:
        raise StoreError("integrity-failure", "manifest artifact paths collide")
    return result


def _package_digest(manifest: dict[str, Any], descriptors: list[dict[str, Any]]) -> str:
    manifest_bytes = canonical_json(manifest)
    preimage = bytearray(b"VIBAPP-PACKAGE\x00experimental-v0\x00")
    preimage.extend(struct.pack(">Q", len(manifest_bytes)))
    preimage.extend(manifest_bytes)
    for descriptor in sorted(descriptors, key=lambda item: item["path"].encode("utf-8")):
        path_bytes = descriptor["path"].encode("utf-8")
        preimage.extend(struct.pack(">H", len(path_bytes)))
        preimage.extend(path_bytes)
        preimage.extend(bytes.fromhex(descriptor["sha256"]))
        preimage.extend(struct.pack(">Q", descriptor["size_bytes"]))
    return hashlib.sha256(preimage).hexdigest()


@dataclass(frozen=True)
class ValidatedCandidate:
    record: dict[str, Any]
    payload_sha256: str
    package_digest: str
    app_id: str
    app_version: str


def _validate_snapshot(candidate_path: Path, *, ingested_at: str) -> ValidatedCandidate:
    candidate_bytes = _read_regular(candidate_path, MAX_CANDIDATE_BYTES, "candidate.json")
    candidate = strict_json(candidate_bytes, label="candidate")
    candidate_keys = {
        "schema_version",
        "document_type",
        "state",
        "job_id",
        "source_tree_sha256",
        "package_digest_sha256",
        "package_directory",
        "component",
        "manifest",
        "quarantine_receipt_sha256",
        "verification",
        "authority",
    }
    # Candidate v1 is a strict compatibility union: legacy exact keys or the
    # same exact record plus one verifier-owned presentation sidecar.
    if set(candidate) not in (candidate_keys, candidate_keys | {"presentation"}):
        candidate = _exact_object(candidate, candidate_keys, "candidate")
    if (
        candidate["schema_version"] != CANDIDATE_SCHEMA
        or candidate["document_type"] != "verifier-promoted-candidate"
        or candidate["state"] != "candidate-ready"
        or candidate["package_directory"] != "package"
    ):
        raise StoreError("integrity-failure", "candidate is not a verifier-promoted candidate-ready v1")
    _text(candidate["job_id"], "job_id", 128)
    _digest(candidate["source_tree_sha256"], "source_tree_sha256")
    package_digest = _digest(candidate["package_digest_sha256"], "package_digest_sha256")
    quarantine_digest = _digest(candidate["quarantine_receipt_sha256"], "quarantine_receipt_sha256")
    if candidate["authority"] != {"install": "daemon", "publish": "none"}:
        raise StoreError("permission-denied", "candidate authority must remain daemon-install/private-only")
    verification = _exact_object(
        candidate["verification"],
        {"authority", "verifier_version", "verified_at_utc", "checks"},
        "candidate.verification",
    )
    if (
        verification.get("authority") != "independent-verifier"
        or verification.get("verifier_version") != "app-verifier.experimental-v1"
    ):
        raise StoreError("integrity-failure", "candidate lacks independent verifier authority")
    _utc(verification.get("verified_at_utc"), "verification.verified_at_utc")
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks or len(checks) > 64:
        raise StoreError("integrity-failure", "candidate has no independent verifier checks")
    for check in checks:
        check = _exact_object(check, {"id", "outcome", "tool", "detail"}, "verification check")
        if check.get("outcome") != "pass":
            raise StoreError("integrity-failure", "candidate contains a non-passing verifier check")
        _text(check.get("id"), "verification check id", 128)
        _text(check.get("tool"), "verification check tool", 256)
        _text(check.get("detail"), "verification check detail", 1000)

    package_dir = candidate_path.parent / "package"
    manifest_bytes = _read_regular(package_dir / "manifest.json", MAX_MANIFEST_BYTES, "manifest.json")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_ref = _exact_object(candidate["manifest"], {"path", "sha256", "size_bytes"}, "candidate.manifest")
    if (
        not isinstance(manifest_ref["size_bytes"], int)
        or isinstance(manifest_ref["size_bytes"], bool)
        or not 1 <= manifest_ref["size_bytes"] <= MAX_MANIFEST_BYTES
    ):
        raise StoreError("resource-limit", "candidate manifest size is outside the frozen bound")
    if (
        manifest_ref["path"] != "manifest.json"
        or manifest_ref["sha256"] != manifest_sha
        or manifest_ref["size_bytes"] != len(manifest_bytes)
    ):
        raise StoreError("integrity-failure", "candidate manifest digest or size does not match bytes")
    manifest = strict_json(manifest_bytes, label="manifest")
    if not isinstance(manifest, dict):
        raise StoreError("malformed-output", "manifest is not an object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("package_format") != PACKAGE_FORMAT:
        raise StoreError("incompatible-contract", "manifest contract is unsupported")
    if not isinstance(manifest.get("verification"), dict) or manifest["verification"].get("revocation") != "not-revoked":
        raise StoreError("permission-denied", "revoked or ambiguously revoked package cannot enter the Store")
    descriptors = _manifest_descriptors(manifest)
    descriptor_by_path = {item["path"]: item for item in descriptors}

    observed: list[dict[str, Any]] = []
    observed_paths: set[str] = set()
    total_size = 0
    for root, directories, files in os.walk(package_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(package_dir)
        for name in directories:
            info = os.lstat(root_path / name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise StoreError("permission-denied", "stored package contains a linked directory")
        for name in files:
            relative = (relative_root / name).as_posix()
            _artifact_path(relative, "stored package path")
            digest, size = _hash_regular(root_path / name, MAX_ARTIFACT_BYTES, relative)
            observed_paths.add(relative)
            observed.append({"path": relative, "sha256": digest, "size_bytes": size})
            total_size += size
            if len(observed) > MAX_PACKAGE_FILES or total_size > MAX_PACKAGE_BYTES:
                raise StoreError("resource-limit", "stored package exceeds file or byte limits")
    expected_paths = set(descriptor_by_path) | {"manifest.json"}
    if "signature.json" in observed_paths:
        expected_paths.add("signature.json")
    if observed_paths != expected_paths:
        raise StoreError("integrity-failure", "package has missing or extra files")
    for path, descriptor in descriptor_by_path.items():
        observed_item = next(item for item in observed if item["path"] == path)
        if observed_item["sha256"] != descriptor["sha256"] or observed_item["size_bytes"] != descriptor["size_bytes"]:
            raise StoreError("integrity-failure", f"artifact differs from manifest: {path}")

    component_ref = _exact_object(candidate["component"], {"path", "sha256", "size_bytes"}, "candidate.component")
    if (
        not isinstance(component_ref["size_bytes"], int)
        or isinstance(component_ref["size_bytes"], bool)
        or not 1 <= component_ref["size_bytes"] <= 16 * 1024 * 1024
    ):
        raise StoreError("resource-limit", "candidate component size is outside the frozen bound")
    canonical_component = descriptors[0]
    if component_ref != {
        "path": canonical_component["path"],
        "sha256": canonical_component["sha256"],
        "size_bytes": canonical_component["size_bytes"],
    }:
        raise StoreError("integrity-failure", "candidate component descriptor differs from the manifest")
    if _package_digest(manifest, descriptors) != package_digest:
        raise StoreError("integrity-failure", "candidate package digest does not match the package bytes")

    app = manifest.get("app")
    if not isinstance(app, dict):
        raise StoreError("incompatible-contract", "manifest app identity is missing")
    app_id = _text(app.get("id"), "app.id", 128)
    app_version = _text(app.get("version"), "app.version", 128)
    if not IDENTIFIER_RE.fullmatch(app_id) or not SEMVER_RE.fullmatch(app_version):
        raise StoreError("incompatible-contract", "manifest app identity is malformed")
    app_kind = app.get("kind")
    if app_kind not in ("ui", "service", "hybrid"):
        raise StoreError("incompatible-contract", "manifest app kind is unsupported")
    display_name = _text(app.get("display_name"), "app.display_name", 80)
    description = _text(app.get("description"), "app.description", 1000)
    presentation = _presentation(
        candidate.get("presentation"), app_kind, display_name, description
    )
    publisher = app.get("publisher")
    if not isinstance(publisher, dict):
        raise StoreError("incompatible-contract", "manifest publisher is missing")
    publisher_id = _text(publisher.get("id"), "publisher.id", 128)
    publisher_name = _text(publisher.get("display_name"), "publisher.display_name", 80)
    if not IDENTIFIER_RE.fullmatch(publisher_id):
        raise StoreError("incompatible-contract", "manifest publisher ID is malformed")
    runtime = manifest.get("runtime")
    capabilities = manifest.get("capabilities")
    entrypoints = manifest.get("entrypoints")
    if not isinstance(runtime, dict) or not isinstance(capabilities, list) or not isinstance(entrypoints, list):
        raise StoreError("incompatible-contract", "manifest runtime metadata is incomplete")
    if (
        runtime.get("contract") != "vibapp:experimental-v0@0.0.1"
        or runtime.get("wasi") != "0.2"
        or runtime.get("world")
        not in ("ui-only-reference", "service-only-reference", "hybrid-reference", "web-preview-reference")
    ):
        raise StoreError("incompatible-contract", "manifest runtime contract is unsupported")
    profiles = runtime.get("profiles")
    platforms = runtime.get("platforms")
    if not isinstance(profiles, list) or not isinstance(platforms, list):
        raise StoreError("incompatible-contract", "manifest profiles or platforms are malformed")
    app_state = _exact_object(
        manifest.get("state"),
        {"schema", "migratable_from_min", "migratable_from_max"},
        "manifest.state",
    )
    state_values = (
        app_state.get("schema"),
        app_state.get("migratable_from_min"),
        app_state.get("migratable_from_max"),
    )
    if any(
        isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 2**32 - 1
        for item in state_values
    ) or app_state["migratable_from_min"] > app_state["migratable_from_max"]:
        raise StoreError("incompatible-contract", "manifest state migration range is invalid")

    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    verifier_sha = hashlib.sha256(canonical_json(verification)).hexdigest()
    inventory = sorted(observed, key=lambda item: item["path"].encode("utf-8"))
    payload_sha = hashlib.sha256(
        canonical_json(
            {
                "candidate_sha256": candidate_sha,
                "package_digest_sha256": package_digest,
                "files": inventory,
            }
        )
    ).hexdigest()
    relative_root = f"candidates/{package_digest}"
    record = {
        "schema_version": RECORD_SCHEMA,
        "state": "private",
        "visibility": "private",
        "publication": {"state": "not-published", "authority": "none"},
        "app": {
            "id": app_id,
            "version": app_version,
            "kind": app_kind,
            "display_name": display_name,
            "description": description,
            "publisher": {"id": publisher_id, "display_name": publisher_name},
        },
        "runtime": {
            "contract": runtime.get("contract"),
            "wasi": runtime.get("wasi"),
            "world": runtime.get("world"),
            "profiles": profiles,
            "platforms": platforms,
            "entrypoints": entrypoints,
            "capabilities": capabilities,
        },
        "app_state": app_state,
        "presentation": presentation,
        "digests": {
            "package_sha256": package_digest,
            "component_sha256": canonical_component["sha256"],
            "manifest_sha256": manifest_sha,
            "source_tree_sha256": candidate["source_tree_sha256"],
            "quarantine_receipt_sha256": quarantine_digest,
            "candidate_record_sha256": candidate_sha,
            "verification_block_sha256": verifier_sha,
            "payload_binding_sha256": payload_sha,
        },
        "verification": verification,
        "authority": {"install": "daemon", "publish": "none"},
        "paths": {
            "candidate": f"{relative_root}/candidate.json",
            "package": f"{relative_root}/package",
        },
        "package_files": inventory,
        "ingested_at_utc": ingested_at,
        "withdrawn_at_utc": None,
        "withdraw_reason": None,
    }
    return ValidatedCandidate(record, payload_sha, package_digest, app_id, app_version)


class LocalAppStore:
    """Private-by-default, process-safe local candidate catalog."""

    def __init__(self, root: Path | str, *, clock: Callable[[], str] = _now_utc):
        self.root = Path(root).absolute()
        self.clock = clock
        if self.root.exists() and self.root.is_symlink():
            raise StoreError("permission-denied", "Store root cannot be a symbolic link")
        _mkdir_private(self.root, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise StoreError("invalid-argument", "Store root is not a directory")
        if os.name != "nt":
            os.chmod(self.root, 0o700)
        self.candidates = self.root / "candidates"
        _mkdir_private(self.candidates, exist_ok=True)
        if self.candidates.is_symlink():
            raise StoreError("permission-denied", "Store candidates path cannot be a symbolic link")
        self.database = self.root / "registry.sqlite3"
        self.lock_path = self.root / ".local-appstore.lock"
        self._initialize()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = _open_private_file(self.lock_path, flags)
        locked = False
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows only.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\x00")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - no supported stdlib locking primitive.
                raise StoreError("unsupported-version", "platform has no supported local file lock")
            locked = True
            yield
        finally:
            if locked and fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif locked and msvcrt is not None:  # pragma: no cover - Windows only.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        if os.name == "nt":
            # Pre-create before SQLite opens it: existing ownership is checked,
            # while only this exclusive creation is allowed to set owner/ACL.
            fd = _open_private_file(self.database, os.O_RDWR)
            os.close(fd)
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._lock():
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS releases (
                        package_digest TEXT PRIMARY KEY,
                        app_id TEXT NOT NULL,
                        app_version TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('private', 'withdrawn')),
                        record_json BLOB NOT NULL,
                        UNIQUE (app_id, app_version)
                    ) STRICT
                    """
                )
            finally:
                connection.close()
            if os.name != "nt":
                os.chmod(self.database, 0o600)

    @staticmethod
    def _row_record(row: sqlite3.Row) -> dict[str, Any]:
        encoded = row["record_json"]
        if isinstance(encoded, str):
            encoded = encoded.encode("utf-8")
        if not isinstance(encoded, bytes):
            raise StoreError("integrity-failure", "stored record encoding is malformed")
        value = strict_json(encoded, label="stored record")
        if not isinstance(value, dict):
            raise StoreError("integrity-failure", "stored record is malformed")
        digest = row["package_digest"]
        expected_root = f"candidates/{digest}"
        if (
            value.get("schema_version") != RECORD_SCHEMA
            or value.get("state") != row["state"]
            or value.get("state") not in ("private", "withdrawn")
            or value.get("visibility") != "private"
            or value.get("publication") != {"state": "not-published", "authority": "none"}
            or value.get("authority") != {"install": "daemon", "publish": "none"}
            or not isinstance(value.get("app"), dict)
            or value["app"].get("id") != row["app_id"]
            or value["app"].get("version") != row["app_version"]
            or not isinstance(value.get("digests"), dict)
            or value["digests"].get("package_sha256") != digest
            or value["digests"].get("payload_binding_sha256") != row["payload_sha256"]
            or value.get("paths")
            != {
                "candidate": f"{expected_root}/candidate.json",
                "package": f"{expected_root}/package",
            }
        ):
            raise StoreError("integrity-failure", "stored record authority or digest binding is malformed")
        if value["state"] == "private" and (
            value.get("withdrawn_at_utc") is not None or value.get("withdraw_reason") is not None
        ):
            raise StoreError("integrity-failure", "private record carries forged withdrawal metadata")
        if value["state"] == "withdrawn":
            _utc(value.get("withdrawn_at_utc"), "withdrawn_at_utc")
            _text(value.get("withdraw_reason"), "withdraw_reason", MAX_REASON_BYTES)
        app = value["app"]
        value["presentation"] = _presentation(
            value.get("presentation"),
            app.get("kind"),
            app.get("display_name"),
            app.get("description"),
        )
        return value

    def ingest(self, candidate: Path | str) -> dict[str, Any]:
        source = Path(candidate).absolute()
        with self._lock():
            staging = Path(tempfile.mkdtemp(prefix=".ingest-", dir=self.candidates))
            created_path: Path | None = None
            try:
                _windows_private(staging, directory=True, created=True)
                _snapshot_candidate(source, staging)
                validated = _validate_snapshot(staging / "candidate.json", ingested_at=self.clock())
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT * FROM releases WHERE package_digest = ?",
                        (validated.package_digest,),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_sha256"] != validated.payload_sha256:
                            raise StoreError("conflict", "package digest already exists with a different payload")
                        final = self.candidates / validated.package_digest
                        if not final.is_dir() or final.is_symlink():
                            raise StoreError("integrity-failure", "catalog bytes are missing or linked")
                        stored = _validate_snapshot(
                            final / "candidate.json",
                            ingested_at=self._row_record(existing)["ingested_at_utc"],
                        )
                        if stored.payload_sha256 != existing["payload_sha256"]:
                            raise StoreError("integrity-failure", "stored candidate bytes were modified")
                        connection.execute("COMMIT")
                        return {
                            "schema_version": RESULT_SCHEMA,
                            "operation": "ingest",
                            "created": False,
                            "record": self._row_record(existing),
                        }
                    version_conflict = connection.execute(
                        "SELECT package_digest FROM releases WHERE app_id = ? AND app_version = ?",
                        (validated.app_id, validated.app_version),
                    ).fetchone()
                    if version_conflict is not None:
                        raise StoreError("conflict", "app version already exists with another package digest")
                    final = self.candidates / validated.package_digest
                    if final.exists() or final.is_symlink():
                        raise StoreError("integrity-failure", "orphan candidate path collides with the package digest")
                    os.rename(staging, final)
                    created_path = final
                    record_bytes = canonical_json(validated.record)
                    connection.execute(
                        "INSERT INTO releases(package_digest, app_id, app_version, payload_sha256, state, record_json) VALUES (?, ?, ?, ?, 'private', ?)",
                        (
                            validated.package_digest,
                            validated.app_id,
                            validated.app_version,
                            validated.payload_sha256,
                            record_bytes,
                        ),
                    )
                    connection.execute("COMMIT")
                    created_path = None
                    return {
                        "schema_version": RESULT_SCHEMA,
                        "operation": "ingest",
                        "created": True,
                        "record": validated.record,
                    }
                except Exception:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    connection.close()
            finally:
                if staging.exists():
                    _safe_remove_tree(staging)
                if created_path is not None and created_path.exists():
                    _safe_remove_tree(created_path)

    def list(
        self,
        *,
        include_withdrawn: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise StoreError("resource-limit", f"list limit must be between 1 and {MAX_LIST_LIMIT}")
        with self._lock():
            connection = self._connect()
            try:
                clauses: list[str] = []
                parameters: list[Any] = []
                if not include_withdrawn:
                    clauses.append("state = 'private'")
                if cursor is not None:
                    cursor = _digest(cursor, "cursor")
                    cursor_row = connection.execute(
                        "SELECT app_id, app_version, package_digest FROM releases WHERE package_digest = ?",
                        (cursor,),
                    ).fetchone()
                    if cursor_row is None:
                        raise StoreError("not-found", "list cursor is not in the local AppStore")
                    clauses.append(
                        "(app_id > ? OR (app_id = ? AND (app_version > ? OR (app_version = ? AND package_digest > ?))))"
                    )
                    parameters.extend(
                        [
                            cursor_row["app_id"],
                            cursor_row["app_id"],
                            cursor_row["app_version"],
                            cursor_row["app_version"],
                            cursor_row["package_digest"],
                        ]
                    )
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                parameters.append(limit + 1)
                rows = connection.execute(
                    f"SELECT * FROM releases{where} ORDER BY app_id, app_version, package_digest LIMIT ?",
                    parameters,
                ).fetchall()
                has_more = len(rows) > limit
                rows = rows[:limit]
                items = [self._row_record(row) for row in rows]
            finally:
                connection.close()
        return {
            "schema_version": RESULT_SCHEMA,
            "operation": "list",
            "include_withdrawn": include_withdrawn,
            "count": len(items),
            "items": items,
            "next_cursor": items[-1]["digests"]["package_sha256"] if has_more and items else None,
        }

    def detail(self, package_digest: str) -> dict[str, Any]:
        package_digest = _digest(package_digest, "package_digest")
        with self._lock():
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM releases WHERE package_digest = ?", (package_digest,)
                ).fetchone()
                if row is None:
                    raise StoreError("not-found", "package digest is not in the local AppStore")
                record = self._row_record(row)
                stored = _validate_snapshot(
                    self.root / record["paths"]["candidate"],
                    ingested_at=record["ingested_at_utc"],
                )
                if stored.payload_sha256 != row["payload_sha256"]:
                    raise StoreError("integrity-failure", "stored candidate bytes were modified")
            finally:
                connection.close()
        return {"schema_version": RESULT_SCHEMA, "operation": "detail", "record": record}

    def withdraw(self, package_digest: str, reason: str) -> dict[str, Any]:
        package_digest = _digest(package_digest, "package_digest")
        reason = _text(reason, "withdraw reason", MAX_REASON_BYTES)
        with self._lock():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM releases WHERE package_digest = ?", (package_digest,)
                ).fetchone()
                if row is None:
                    raise StoreError("not-found", "package digest is not in the local AppStore")
                record = self._row_record(row)
                if row["state"] == "withdrawn":
                    if record.get("withdraw_reason") != reason:
                        raise StoreError("conflict", "withdrawal is immutable and already has another reason")
                    connection.execute("COMMIT")
                    return {
                        "schema_version": RESULT_SCHEMA,
                        "operation": "withdraw",
                        "changed": False,
                        "record": record,
                    }
                record["state"] = "withdrawn"
                record["withdrawn_at_utc"] = self.clock()
                record["withdraw_reason"] = reason
                connection.execute(
                    "UPDATE releases SET state = 'withdrawn', record_json = ? WHERE package_digest = ?",
                    (canonical_json(record), package_digest),
                )
                connection.execute("COMMIT")
                return {
                    "schema_version": RESULT_SCHEMA,
                    "operation": "withdraw",
                    "changed": True,
                    "record": record,
                }
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()


def _emit(value: Any, *, stream: Any) -> None:
    stream.write(canonical_json(value).decode("utf-8") + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private local VibApp AppStore candidate catalog")
    parser.add_argument("--store-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("candidate", type=Path)
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--include-withdrawn", action="store_true")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--cursor")
    detail_parser = commands.add_parser("detail")
    detail_parser.add_argument("package_digest")
    withdraw_parser = commands.add_parser("withdraw")
    withdraw_parser.add_argument("package_digest")
    withdraw_parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        store = LocalAppStore(args.store_root)
        if args.command == "ingest":
            result = store.ingest(args.candidate)
        elif args.command == "list":
            result = store.list(
                include_withdrawn=args.include_withdrawn,
                limit=args.limit,
                cursor=args.cursor,
            )
        elif args.command == "detail":
            result = store.detail(args.package_digest)
        else:
            result = store.withdraw(args.package_digest, args.reason)
    except (StoreError, OSError, sqlite3.Error) as error:
        code = error.code if isinstance(error, StoreError) else "internal"
        _emit(
            {"schema_version": RESULT_SCHEMA, "operation": "error", "error": {"code": code, "message": str(error)[:4096]}},
            stream=os.sys.stderr,
        )
        return 1
    _emit(result, stream=os.sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
