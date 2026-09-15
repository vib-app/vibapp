#!/usr/bin/env python3
"""Assemble deterministic, explicitly unverified Linux/Windows package archives.

The assembler accepts only a strict JSON inventory rooted at one canonical directory.
It never discovers files, follows links, runs a binary, signs output, or changes the
native-support evidence state. Output is a quarantine-format definition artifact.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


INPUT_SCHEMA = "vibapp.native-package-input.experimental-v1"
MANIFEST_SCHEMA = "vibapp.native-package-manifest.experimental-v1"
RECEIPT_SCHEMA = "vibapp.native-package-receipt.experimental-v1"
MAX_FILES = 512
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MIN_EPOCH = 315532800  # 1980-01-01, required by the ZIP timestamp format.
MAX_EPOCH = 0xFFFFFFFF
TARGETS = {
    "linux-x86_64": {
        "extension": "tar.gz",
        "root": "vibapp",
        "roles": {
            "launcher": "vibapp/bin/vibapp-launcher",
            "runtime": "vibapp/bin/vibapp-runtime",
            "service-runtime": "vibapp/libexec/vibapp-service-runtime",
        },
    },
    "windows-x86_64": {
        "extension": "zip",
        "root": "VibApp",
        "roles": {
            "launcher": "VibApp/vibapp-launcher.exe",
            "runtime": "VibApp/vibapp-runtime.exe",
            "service-runtime": "VibApp/libexec/vibapp-service-runtime.exe",
        },
    },
}


class PackageError(ValueError):
    pass


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object_pairs_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, PackageError) as error:
        raise PackageError(f"cannot parse package input: {error}") from error
    if not isinstance(value, dict):
        raise PackageError("package input must be one JSON object")
    return value


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise PackageError(f"{label} keys differ: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _is_link_like(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _canonical_directory(path_value: str, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value or not Path(path_value).is_absolute():
        raise PackageError(f"{label} must be an explicit absolute path")
    path = Path(path_value)
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise PackageError(f"cannot resolve {label}: {error}") from error
    if resolved != path or _is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PackageError(f"{label} must be an existing canonical non-link directory")
    return path


def _safe_relative(value: Any, label: str, maximum_bytes: int = 240) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PackageError(f"invalid {label}")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise PackageError(f"{label} must be ASCII") from error
    path = PurePosixPath(value)
    if len(encoded) > maximum_bytes or path.is_absolute() or str(path) != value:
        raise PackageError(f"invalid {label}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PackageError(f"invalid {label}")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise PackageError(f"invalid {label}")
    return value


def _assert_no_link_ancestors(root: Path, relative: str) -> Path:
    candidate = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise PackageError(f"source path is missing: {relative}: {error}") from error
        if _is_link_like(metadata):
            raise PackageError(f"source path contains a symlink or reparse point: {relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise PackageError(f"source ancestor is not a directory: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise PackageError(f"source path escapes source_root: {relative}") from error
    if resolved != candidate:
        raise PackageError(f"source path is not canonical: {relative}")
    return candidate


@dataclass(frozen=True)
class InputFile:
    role: str
    archive_path: str
    executable: bool
    data: bytes
    sha256: str


def _read_input_file(root: Path, entry: dict[str, Any]) -> InputFile:
    _keys(entry, {"role", "source_path", "archive_path", "executable", "size_bytes", "sha256"}, "file")
    role = entry["role"]
    if role not in {"launcher", "runtime", "service-runtime", "resource"}:
        raise PackageError(f"unknown file role: {role}")
    source_path = _safe_relative(entry["source_path"], "source_path")
    archive_path = _safe_relative(entry["archive_path"], "archive_path")
    executable = entry["executable"]
    size_bytes = entry["size_bytes"]
    digest = entry["sha256"]
    if not isinstance(executable, bool):
        raise PackageError(f"executable must be boolean: {source_path}")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or not 0 <= size_bytes <= MAX_FILE_BYTES:
        raise PackageError(f"invalid size_bytes: {source_path}")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PackageError(f"invalid sha256: {source_path}")
    candidate = _assert_no_link_ancestors(root, source_path)
    before = candidate.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size_bytes:
        raise PackageError(f"source must be one ordinary, non-hardlinked file with the declared size: {source_path}")
    try:
        with candidate.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise PackageError(f"source changed while opening: {source_path}")
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise PackageError(f"cannot read source: {source_path}: {error}") from error
    after = candidate.lstat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(data) != size_bytes:
        raise PackageError(f"source changed while reading: {source_path}")
    actual_digest = sha256_bytes(data)
    if actual_digest != digest:
        raise PackageError(f"source digest mismatch: {source_path}")
    return InputFile(role=role, archive_path=archive_path, executable=executable, data=data, sha256=digest)


def validate_input(value: dict[str, Any]) -> tuple[dict[str, Any], list[InputFile], Path]:
    _keys(value, {"schema_version", "target", "package_version", "source_revision", "source_date_epoch", "source_root", "files"}, "package input")
    if value["schema_version"] != INPUT_SCHEMA:
        raise PackageError("unsupported package input schema_version")
    target = value["target"]
    if target not in TARGETS:
        raise PackageError(f"unsupported target: {target}")
    version = value["package_version"]
    revision = value["source_revision"]
    epoch = value["source_date_epoch"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?", version):
        raise PackageError("package_version must be a bounded SemVer-like value")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise PackageError("source_revision must be an exact lowercase Git commit")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not MIN_EPOCH <= epoch <= MAX_EPOCH or epoch % 2:
        raise PackageError("source_date_epoch must be an even UTC second representable by tar.gz and ZIP")
    root = _canonical_directory(value["source_root"], "source_root")
    entries = value["files"]
    if not isinstance(entries, list) or not 3 <= len(entries) <= MAX_FILES:
        raise PackageError("files must contain 3..512 explicit entries")
    declared_total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise PackageError("file entry must be an object")
        declared_size = entry.get("size_bytes")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or not 0 <= declared_size <= MAX_FILE_BYTES:
            raise PackageError("file entry has an invalid declared size")
        declared_total += declared_size
        if declared_total > MAX_TOTAL_BYTES:
            raise PackageError("total input bytes exceed the 128 MiB bound")
    files: list[InputFile] = []
    for entry in entries:
        files.append(_read_input_file(root, entry))
    archive_paths = [item.archive_path for item in files]
    if len(archive_paths) != len(set(archive_paths)):
        raise PackageError("archive paths must be unique")
    role_counts = {role: sum(item.role == role for item in files) for role in ("launcher", "runtime", "service-runtime")}
    if any(count != 1 for count in role_counts.values()):
        raise PackageError("launcher, runtime, and service-runtime roles must each occur exactly once")
    spec = TARGETS[target]
    for item in files:
        required = spec["roles"].get(item.role)
        if required is not None:
            if item.archive_path != required or not item.executable:
                raise PackageError(f"{item.role} must be executable at {required}")
        else:
            resource_prefix = f"{spec['root']}/resources/"
            if not item.archive_path.startswith(resource_prefix) or item.executable:
                raise PackageError(f"resources must be non-executable below {resource_prefix}")
    return value, files, root


class DeterministicGzipStoredWriter:
    """A gzip writer using only DEFLATE stored blocks, independent of zlib output."""

    def __init__(self, output: BinaryIO, epoch: int) -> None:
        self.output = output
        self.buffer = bytearray()
        self.crc = 0
        self.size = 0
        self.closed = False
        output.write(b"\x1f\x8b\x08\x00" + struct.pack("<I", epoch) + b"\x00\xff")

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        if self.closed:
            raise ValueError("write to closed gzip stream")
        raw = bytes(data)
        self.crc = binascii.crc32(raw, self.crc)
        self.size = (self.size + len(raw)) & 0xFFFFFFFF
        self.buffer.extend(raw)
        while len(self.buffer) > 65535:
            self._block(bytes(self.buffer[:65535]), final=False)
            del self.buffer[:65535]
        return len(raw)

    def _block(self, data: bytes, final: bool) -> None:
        length = len(data)
        self.output.write(bytes([1 if final else 0]))
        self.output.write(struct.pack("<HH", length, 0xFFFF ^ length))
        self.output.write(data)

    def close(self) -> None:
        if self.closed:
            return
        self._block(bytes(self.buffer), final=True)
        self.output.write(struct.pack("<II", self.crc & 0xFFFFFFFF, self.size))
        self.closed = True


def _manifest(input_value: dict[str, Any], files: list[InputFile]) -> bytes:
    return canonical_json(
        {
            "schema_version": MANIFEST_SCHEMA,
            "target": input_value["target"],
            "package_version": input_value["package_version"],
            "source_revision": input_value["source_revision"],
            "source_date_epoch": input_value["source_date_epoch"],
            "artifact_state": "unverified-package-definition-output",
            "support_claim": "not-verified",
            "signing": "not-applied",
            "files": [
                {
                    "path": item.archive_path,
                    "role": item.role,
                    "executable": item.executable,
                    "size_bytes": len(item.data),
                    "sha256": item.sha256,
                }
                for item in sorted(files, key=lambda item: item.archive_path.encode("ascii"))
            ],
        }
    )


def _archive_members(input_value: dict[str, Any], files: list[InputFile], manifest: bytes) -> list[tuple[str, bytes, bool]]:
    root = TARGETS[input_value["target"]]["root"]
    members = [(item.archive_path, item.data, item.executable) for item in files]
    members.append((f"{root}/PACKAGE-MANIFEST.json", manifest, False))
    return sorted(members, key=lambda item: item[0].encode("ascii"))


def _write_tar_gz(output: BinaryIO, epoch: int, members: list[tuple[str, bytes, bool]]) -> None:
    gzip_stream = DeterministicGzipStoredWriter(output, epoch)
    try:
        with tarfile.open(fileobj=gzip_stream, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
            for archive_path, data, executable in members:
                info = tarfile.TarInfo(archive_path)
                info.size = len(data)
                info.mode = 0o755 if executable else 0o644
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                info.mtime = epoch
                info.type = tarfile.REGTYPE
                archive.addfile(info, io.BytesIO(data))
    finally:
        gzip_stream.close()


def _write_zip(output: BinaryIO, epoch: int, members: list[tuple[str, bytes, bool]]) -> None:
    timestamp = time.gmtime(epoch)[:6]
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for archive_path, data, executable in members:
            info = zipfile.ZipInfo(archive_path, date_time=timestamp)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = ((0o100755 if executable else 0o100644) & 0xFFFF) << 16
            info.flag_bits = 0
            archive.writestr(info, data)


def assemble(input_path: Path, output_directory_value: str) -> dict[str, Any]:
    input_value, files, _root = validate_input(load_json(input_path))
    output_directory = _canonical_directory(output_directory_value, "output_directory")
    target = input_value["target"]
    extension = TARGETS[target]["extension"]
    base = f"vibapp-{input_value['package_version']}-{target}.{extension}"
    archive_path = output_directory / base
    manifest_path = output_directory / f"{base}.manifest.json"
    receipt_path = output_directory / f"{base}.receipt.json"
    for destination in (archive_path, manifest_path, receipt_path):
        if destination.exists() or destination.is_symlink():
            raise PackageError(f"refusing to overwrite output: {destination.name}")
    manifest = _manifest(input_value, files)
    members = _archive_members(input_value, files, manifest)
    created: list[Path] = []
    try:
        with archive_path.open("xb") as output:
            created.append(archive_path)
            if target == "linux-x86_64":
                _write_tar_gz(output, input_value["source_date_epoch"], members)
            else:
                _write_zip(output, input_value["source_date_epoch"], members)
            output.flush()
            os.fsync(output.fileno())
        with manifest_path.open("xb") as output:
            created.append(manifest_path)
            output.write(manifest)
            output.flush()
            os.fsync(output.fileno())
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "target": target,
            "package_version": input_value["package_version"],
            "source_revision": input_value["source_revision"],
            "source_date_epoch": input_value["source_date_epoch"],
            "archive": archive_path.name,
            "archive_size_bytes": archive_path.stat().st_size,
            "archive_sha256": sha256_file(archive_path),
            "manifest": manifest_path.name,
            "manifest_sha256": sha256_bytes(manifest),
            "member_count": len(members),
            "artifact_state": "unverified-package-definition-output",
            "evidence_label": "definition-output-only",
            "reproducibility": "not-established-by-single-assembly",
            "native_execution": "not-attested",
            "native_smoke": "not-run",
            "signing": "not-applied",
            "release_eligible": False,
            "support_claim": "not-verified",
        }
        receipt_bytes = canonical_json(receipt)
        with receipt_path.open("xb") as output:
            created.append(receipt_path)
            output.write(receipt_bytes)
            output.flush()
            os.fsync(output.fileno())
        return receipt
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-directory", required=True)
    arguments = parser.parse_args(argv)
    try:
        receipt = assemble(arguments.input, arguments.output_directory)
    except PackageError as error:
        print(f"native package definition rejected: {error}", file=sys.stderr)
        return 65
    print(canonical_json(receipt).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
