#!/usr/bin/env python3
"""Fail-closed one-job provider runner boundary and local synthetic oracle.

The production path deliberately has no default backend, credential material, or
network implementation.  The synthetic path runs only the pinned inert fixture and
does not issue a ProviderRunnerReceipt v3 or claim provider egress.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Protocol
from urllib.parse import urlsplit


BASE = Path(__file__).resolve().parent
POLICY_PATH = BASE / "deployment/runner-policy.json"
DESTINATION_POLICY_PATH = BASE / "deployment/gateway-egress-policy.json"
PROFILE_TEMPLATE_PATH = BASE / "deployment/accepted-profile.template.json"
SYNTHETIC_EXECUTABLE = BASE / "fixtures/inert_worker.py"

POLICY_VERSION = "vibapp.provider-runner-isolation.experimental-v1"
REQUEST_VERSION = "vibapp.provider-runner-request.experimental-v2"
RECEIPT_VERSION = "vibapp.provider-runner-receipt.experimental-v3"
ACCEPTED_PROFILE_VERSION = "vibapp.provider-runner-accepted-profile.experimental-v1"
RECEIPT_ATTESTATION_TYPE = "detached-signature-placeholder-v1"
GATEWAY_RECEIPT_VERSION = "vibapp.provider-gateway-receipt.experimental-v1"
GATEWAY_ATTESTATION_TYPE = "detached-gateway-signature-v1"
SYNTHETIC_REPORT_VERSION = "vibapp.provider-runner-synthetic-report.experimental-v1"

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RUNNER_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{43,1024}={0,2}$")
RELATIVE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ATTEMPT_IDENTIFIER = re.compile(r"^attempt-[0-9]{4}-[0-9a-f]{16}$")
HTTPS_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
)
PROVIDER_RESULT_SCHEMA_VERSION = "vibapp.cloud-codeagent-provider-result.experimental-v1"
PROVIDER_RESULT_DOCUMENT_TYPE = "cloud-codeagent-provider-result"


class RunnerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DetachedSignatureVerifier(Protocol):
    """Production verifier interface; the runner ships no accepted key."""

    def verify(self, digest: bytes, signature: str, key_id: str) -> bool: ...


class DetachedReceiptSigner(Protocol):
    """External attestor interface; the runner ships no production signer."""

    def sign(self, digest: bytes, key_id: str) -> str: ...


class ProviderBackend(Protocol):
    """VM/container supervisor boundary, implemented outside the Python process."""

    def execute(self, job: "ProductionJob") -> "BackendResult": ...


def canonical_json(value: Any) -> bytes:
    """Canonical encoding for this integer/string/boolean-only receipt vocabulary."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def durable_claim_name(job_id: str, execution_attempt: dict[str, Any]) -> str:
    """Return the path-safe replay key for one mutable execution attempt."""
    bounded_identifier(job_id, "job_id")
    attempt = validate_execution_attempt(execution_attempt)
    return f"{sha256_bytes(canonical_json({'job_id': job_id, 'execution_attempt': attempt}))}.json"


def exact_object(value: Any, keys: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = set(value) if isinstance(value, dict) else set()
        raise RunnerError(
            "schema-invalid",
            f"{context} fields mismatch missing={sorted(keys - actual)} extra={sorted(actual - keys)}",
        )
    return value


def bounded_identifier(value: Any, context: str, *, runner: bool = False) -> str:
    pattern = RUNNER_IDENTIFIER if runner else IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RunnerError("schema-invalid", f"{context} is not a bounded identifier")
    return value


def sha256_value(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise RunnerError("schema-invalid", f"{context} is not lowercase SHA-256")
    return value


def int_value(value: Any, context: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RunnerError("schema-invalid", f"{context} must be integer {minimum}..{maximum}")
    return value


def bool_value(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise RunnerError("schema-invalid", f"{context} must be boolean")
    return value


def validate_execution_attempt(value: Any, context: str = "execution_attempt") -> dict[str, Any]:
    """Validate and detach the mutable retry identity from caller-owned storage."""
    attempt = exact_object(value, {"attempt_id", "ordinal"}, context)
    attempt_id = attempt["attempt_id"]
    if not isinstance(attempt_id, str) or not ATTEMPT_IDENTIFIER.fullmatch(attempt_id):
        raise RunnerError(
            "schema-invalid",
            f"{context}.attempt_id must match attempt-NNNN-<16 lowercase hex>",
        )
    ordinal = int_value(attempt["ordinal"], f"{context}.ordinal", 1, 9999)
    if attempt_id[8:12] != f"{ordinal:04}":
        raise RunnerError("schema-invalid", f"{context}.attempt_id prefix does not match ordinal")
    return {"attempt_id": attempt_id, "ordinal": ordinal}


def _bounded_identity_text(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value.strip() != value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RunnerError("schema-invalid", f"{context} is not bounded non-secret identity text")
    return value


def _canonical_https_endpoint(value: Any, context: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise RunnerError("schema-invalid", f"{context} must be a bounded HTTPS endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise RunnerError("schema-invalid", f"{context} has an invalid port") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RunnerError("schema-invalid", f"{context} is not canonical credential-free HTTPS")
    hostname = parsed.hostname
    if (
        not hostname.isascii()
        or hostname != hostname.casefold()
        or hostname.endswith(".")
        or not HTTPS_HOST.fullmatch(hostname)
    ):
        raise RunnerError("schema-invalid", f"{context} host is not canonical lowercase ASCII")
    if port is not None and not 1 <= port <= 65535:
        raise RunnerError("schema-invalid", f"{context} port is out of range")
    canonical_port = "" if port in {None, 443} else f":{port}"
    path = parsed.path
    if (
        "\\" in path
        or "//" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
        or not path.isascii()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
        or re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@/%\-]*", path) is None
    ):
        raise RunnerError("schema-invalid", f"{context} path is not canonical")
    for match in re.finditer("%", path):
        escape = path[match.start() + 1 : match.start() + 3]
        if len(escape) != 2 or not re.fullmatch(r"[0-9A-F]{2}", escape):
            raise RunnerError("schema-invalid", f"{context} percent escape is not canonical")
    canonical = f"https://{hostname}{canonical_port}{path.rstrip('/')}"
    if value != canonical:
        raise RunnerError("schema-invalid", f"{context} is not canonical; expected {canonical}")
    return canonical


def validate_provider_execution_identity(
    value: Any,
    provider: str,
    expected_digest: str,
    context: str = "provider_execution_identity",
) -> dict[str, Any]:
    """Validate the complete non-secret provider identity and both digest bindings."""
    expected_digest = sha256_value(expected_digest, f"{context} digest binding")
    identity = exact_object(
        value,
        {
            "schema_version",
            "endpoint",
            "runtime",
            "non_secret_config_sha256",
            "identity_sha256",
        },
        context,
    )
    if identity["schema_version"] != "vibapp.provider-execution-identity.experimental-v1":
        raise RunnerError("schema-invalid", f"{context}.schema_version is unsupported")
    endpoint = exact_object(
        identity["endpoint"],
        {"kind", "canonical_endpoint", "endpoint_sha256"},
        f"{context}.endpoint",
    )
    if endpoint["kind"] == "https":
        _canonical_https_endpoint(endpoint["canonical_endpoint"], f"{context}.endpoint")
    elif endpoint["kind"] == "provider-managed":
        if endpoint["canonical_endpoint"] != "provider-managed" or provider != "openai-codex":
            raise RunnerError("schema-invalid", f"{context}.endpoint is not valid for {provider}")
    else:
        raise RunnerError("schema-invalid", f"{context}.endpoint.kind is unsupported")
    endpoint_digest = sha256_value(
        endpoint["endpoint_sha256"], f"{context}.endpoint.endpoint_sha256"
    )
    endpoint_body = {
        "kind": endpoint["kind"],
        "canonical_endpoint": endpoint["canonical_endpoint"],
    }
    if endpoint_digest != sha256_bytes(canonical_json(endpoint_body)):
        raise RunnerError("provider-identity-mismatch", "provider endpoint digest is invalid")
    runtime = exact_object(
        identity["runtime"],
        {
            "adapter_id",
            "adapter_version",
            "adapter_sha256",
            "package_id",
            "package_version",
            "executable_sha256",
        },
        f"{context}.runtime",
    )
    for field in ("adapter_id", "adapter_version", "package_id", "package_version"):
        _bounded_identity_text(runtime[field], f"{context}.runtime.{field}")
    sha256_value(runtime["adapter_sha256"], f"{context}.runtime.adapter_sha256")
    sha256_value(runtime["executable_sha256"], f"{context}.runtime.executable_sha256")
    sha256_value(identity["non_secret_config_sha256"], f"{context}.non_secret_config_sha256")
    embedded_digest = sha256_value(identity["identity_sha256"], f"{context}.identity_sha256")
    body = {
        "schema_version": identity["schema_version"],
        "endpoint": endpoint,
        "runtime": runtime,
        "non_secret_config_sha256": identity["non_secret_config_sha256"],
    }
    actual_digest = sha256_bytes(canonical_json(body))
    if embedded_digest != actual_digest or expected_digest != actual_digest:
        raise RunnerError(
            "provider-identity-mismatch",
            "provider execution identity payload differs from its embedded/request digest",
        )
    # Canonical JSON round-trip detaches all nested objects from caller mutation.
    return json.loads(canonical_json(identity))


def load_strict_json(path: Path, maximum_bytes: int = 256 * 1024) -> Any:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RunnerError("schema-invalid", f"{path} must be a regular non-symlink file")
    if metadata.st_size > maximum_bytes:
        raise RunnerError("resource-limit", f"{path} exceeds {maximum_bytes} bytes")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise RunnerError("schema-invalid", f"duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        return json.loads(path.read_bytes(), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError("schema-invalid", f"{path} is not strict UTF-8 JSON") from error


def canonical_relative_path(value: str, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise RunnerError("path-invalid", f"{context} is empty or oversized")
    if value.startswith("/") or value.endswith("/") or "\\" in value or "//" in value:
        raise RunnerError("path-invalid", f"{context} is not a canonical relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or not RELATIVE_SEGMENT.fullmatch(part) for part in parts):
        raise RunnerError("path-invalid", f"{context} contains an invalid segment")
    return "/".join(parts)


def safe_tree_records(root: Path, maximum_files: int, maximum_bytes: int) -> list[dict[str, Any]]:
    root = root.resolve(strict=True)
    records: list[dict[str, Any]] = []
    total = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RunnerError("bundle-unsafe", "bundle contains a linked or non-directory entry")
            canonical_relative_path(path.relative_to(root).as_posix(), "bundle directory")
        for name in files:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RunnerError("bundle-unsafe", "bundle contains a symlink, hard link, or special file")
            relative = canonical_relative_path(path.relative_to(root).as_posix(), "bundle file")
            total += metadata.st_size
            records.append({"path": relative, "sha256": sha256_file(path), "size_bytes": metadata.st_size})
            if len(records) > maximum_files or total > maximum_bytes:
                raise RunnerError("bundle-limit", "bundle exceeds its file or byte ceiling")
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    return records


def bundle_digest(root: Path, maximum_files: int, maximum_bytes: int) -> str:
    records = safe_tree_records(root, maximum_files, maximum_bytes)
    digest = hashlib.sha256()
    digest.update(b"VIBAPP-PROVIDER-RUNNER-BUNDLE\0")
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(record["sha256"]))
        digest.update(record["size_bytes"].to_bytes(8, "big"))
    return digest.hexdigest()


def copy_bundle(source: Path, destination: Path, maximum_files: int, maximum_bytes: int, *, read_only: bool) -> None:
    records = safe_tree_records(source, maximum_files, maximum_bytes)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_root = source.resolve(strict=True)
    for record in records:
        relative = canonical_relative_path(record["path"], "copy path")
        source_path = source_root.joinpath(*relative.split("/")).resolve(strict=True)
        source_path.relative_to(source_root)
        destination_path = destination.joinpath(*relative.split("/"))
        destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with source_path.open("rb") as reader, destination_path.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=64 * 1024)
        destination_path.chmod(0o400 if read_only else 0o600)
    if read_only:
        for current, directories, _ in os.walk(destination, topdown=False):
            for name in directories:
                (Path(current) / name).chmod(0o500)
        destination.chmod(0o500)


def _copy_open_file_to_snapshot(
    source_fd: int,
    destination: Path,
    initial: os.stat_result,
    budget: dict[str, int],
) -> dict[str, Any]:
    """Copy one already-open nofollow file and reject in-place mutation."""
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            budget["bytes"] += len(chunk)
            if budget["bytes"] > budget["maximum_bytes"]:
                raise RunnerError("bundle-limit", "bundle exceeds its byte ceiling during snapshot")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    final = os.fstat(source_fd)
    stable_fields = (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns",
    )
    if any(getattr(initial, field) != getattr(final, field) for field in stable_fields) or size != initial.st_size:
        raise RunnerError("bundle-mutated", "source file changed while its immutable snapshot was copied")
    destination.chmod(0o400)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def snapshot_bundle_to_directory(
    source: Path,
    destination: Path,
    maximum_files: int,
    maximum_bytes: int,
    *,
    expected_digest: str | None = None,
) -> str:
    """Create and seal one descriptor-bound regular-file snapshot.

    The digest is computed from the exact destination bytes.  Directory membership
    and file metadata are rechecked while each source directory descriptor is held.
    """
    if destination.exists() or destination.is_symlink():
        raise RunnerError("output-conflict", "snapshot destination already exists")
    if expected_digest is not None:
        sha256_value(expected_digest, "expected bundle digest")
    source_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(source, source_flags)
    except OSError as error:
        raise RunnerError("bundle-unsafe", "bundle root is not a stable nofollow directory") from error
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    budget = {"bytes": 0, "maximum_bytes": maximum_bytes}

    def walk(directory_fd: int, relative_parts: tuple[str, ...], destination_directory: Path) -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(list(iterator), key=lambda entry: entry.name.encode("utf-8"))
        except OSError as error:
            raise RunnerError("bundle-mutated", "cannot enumerate a stable source directory") from error
        initial_names = [entry.name for entry in entries]
        for entry in entries:
            relative = canonical_relative_path("/".join((*relative_parts, entry.name)), "snapshot path")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise RunnerError("bundle-mutated", "source entry changed before snapshot open") from error
            if stat.S_ISDIR(metadata.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                except OSError as error:
                    raise RunnerError("bundle-mutated", "source directory changed before descriptor binding") from error
                child_metadata = os.fstat(child_fd)
                if child_metadata.st_dev != metadata.st_dev or child_metadata.st_ino != metadata.st_ino:
                    os.close(child_fd)
                    raise RunnerError("bundle-mutated", "source directory identity changed during binding")
                child_destination = destination_directory / entry.name
                child_destination.mkdir(mode=0o700)
                try:
                    walk(child_fd, (*relative_parts, entry.name), child_destination)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RunnerError("bundle-unsafe", "bundle contains a symlink, hard link, or special file")
            try:
                file_fd = os.open(entry.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            except OSError as error:
                raise RunnerError("bundle-mutated", "source file changed before descriptor binding") from error
            try:
                bound = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(bound.st_mode)
                    or bound.st_nlink != 1
                    or bound.st_dev != metadata.st_dev
                    or bound.st_ino != metadata.st_ino
                ):
                    raise RunnerError("bundle-mutated", "source file identity changed during binding")
                budget["files"] = budget.get("files", 0) + 1
                if budget["files"] > maximum_files:
                    raise RunnerError("bundle-limit", "bundle exceeds its file-count ceiling")
                copied = _copy_open_file_to_snapshot(file_fd, destination_directory / entry.name, bound, budget)
            finally:
                os.close(file_fd)
            records.append({"path": relative, **copied})
        try:
            with os.scandir(directory_fd) as iterator:
                final_names = sorted(entry.name for entry in iterator)
        except OSError as error:
            raise RunnerError("bundle-mutated", "cannot re-enumerate source directory after snapshot") from error
        if sorted(initial_names) != final_names:
            raise RunnerError("bundle-mutated", "source directory membership changed during snapshot")

    try:
        walk(root_fd, (), destination)
    finally:
        os.close(root_fd)
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    digest = hashlib.sha256()
    digest.update(b"VIBAPP-PROVIDER-RUNNER-BUNDLE\0")
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(record["sha256"]))
        digest.update(record["size_bytes"].to_bytes(8, "big"))
    snapshot_digest = digest.hexdigest()
    for current, directories, _ in os.walk(destination, topdown=False):
        for name in directories:
            (Path(current) / name).chmod(0o500)
    destination.chmod(0o500)
    rehashed = bundle_digest(destination, maximum_files, maximum_bytes)
    if rehashed != snapshot_digest:
        raise RunnerError("bundle-mutated", "sealed snapshot bytes differ from their descriptor-bound digest")
    if expected_digest is not None and snapshot_digest != expected_digest:
        raise RunnerError("input-bundle-mismatch", "immutable input snapshot differs from the request digest")
    return snapshot_digest


@dataclass(frozen=True)
class Limits:
    wall_time_seconds: int
    cpu_seconds: int
    rss_bytes: int
    pids: int
    disk_bytes: int
    stdout_bytes: int
    stderr_bytes: int
    output_files: int

    @classmethod
    def from_dict(cls, value: Any) -> "Limits":
        keys = {
            "wall_time_seconds", "cpu_seconds", "rss_bytes", "pids", "disk_bytes",
            "stdout_bytes", "stderr_bytes", "output_files",
        }
        item = exact_object(value, keys, "limits")
        result = cls(
            wall_time_seconds=int_value(item["wall_time_seconds"], "wall_time_seconds", 1, 900),
            cpu_seconds=int_value(item["cpu_seconds"], "cpu_seconds", 1, 900),
            rss_bytes=int_value(item["rss_bytes"], "rss_bytes", 256 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
            pids=int_value(item["pids"], "pids", 1, 64),
            disk_bytes=int_value(item["disk_bytes"], "disk_bytes", 1024, 64 * 1024 * 1024),
            stdout_bytes=int_value(item["stdout_bytes"], "stdout_bytes", 0, 1024 * 1024),
            stderr_bytes=int_value(item["stderr_bytes"], "stderr_bytes", 0, 1024 * 1024),
            output_files=int_value(item["output_files"], "output_files", 1, 512),
        )
        if result.cpu_seconds > result.wall_time_seconds:
            raise RunnerError("schema-invalid", "cpu_seconds cannot exceed wall_time_seconds")
        return result


def validate_requested_isolation_policy(required: Any, runner_policy: Any) -> dict[str, Any]:
    """Validate CloudAgent's exact per-job policy against the reviewed envelope."""
    required_keys = {
        "schema_version", "one_job_per_isolate", "non_root", "read_only_base",
        "environment_inheritance", "credential_delivery", "network_policy",
        "host_home_mounted", "host_repository_mounted", "host_ssh_mounted",
        "host_cloud_credentials_mounted", "host_container_socket_mounted",
        "whole_job_kill", "cpu_seconds", "rss_bytes", "pids", "disk_bytes",
    }
    request = exact_object(required, required_keys, "requested isolation policy")
    if request["schema_version"] != POLICY_VERSION:
        raise RunnerError("isolation-policy-mismatch", "requested isolation policy version is unsupported")
    constants = {
        "one_job_per_isolate": True,
        "non_root": True,
        "read_only_base": True,
        "environment_inheritance": "none",
        "credential_delivery": "broker-only",
        "network_policy": "provider-gateway-only",
        "host_home_mounted": False,
        "host_repository_mounted": False,
        "host_ssh_mounted": False,
        "host_cloud_credentials_mounted": False,
        "host_container_socket_mounted": False,
        "whole_job_kill": True,
    }
    if any(request[key] != value for key, value in constants.items()):
        raise RunnerError("isolation-policy-mismatch", "requested isolation policy weakens a mandatory control")
    policy_keys = {
        "schema_version", "document_type", "status", "runtime_boundary", "identity",
        "filesystem", "environment", "credential_broker", "network", "resource_ceilings",
        "whole_job_lifecycle", "request", "receipt", "gateway_observation",
        "immutable_runtime_inputs",
    }
    policy = exact_object(runner_policy, policy_keys, "runner policy")
    if policy["schema_version"] != "vibapp.external-one-job-runner-policy.experimental-v1":
        raise RunnerError("isolation-policy-mismatch", "runner policy version is unsupported")
    if policy["document_type"] != "external-one-job-runner-policy" or policy["status"] != "deployment-unprovisioned":
        raise RunnerError("isolation-policy-mismatch", "this artifact may only load the unprovisioned deployment policy")
    if policy["runtime_boundary"] != "one-job-rootless-oci-or-microvm":
        raise RunnerError("isolation-policy-mismatch", "runner runtime boundary is invalid")
    if policy["identity"].get("allocation") != "fresh-uid-gid-and-namespace-per-job":
        raise RunnerError("isolation-policy-mismatch", "runner identity policy is not per-job")
    if policy["filesystem"].get("rootfs") != "read-only" or policy["filesystem"].get("host_mounts") != []:
        raise RunnerError("isolation-policy-mismatch", "runner filesystem policy permits host/base mutation")
    if policy["environment"].get("inheritance") != "none":
        raise RunnerError("isolation-policy-mismatch", "runner environment policy inherits ambient variables")
    denied = set(policy["environment"].get("explicitly_denied", []))
    required_denied = {
        "HOME", "CODEX_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSH_AUTH_SOCK",
        "DOCKER_HOST", "CONTAINER_HOST",
    }
    if not required_denied <= denied:
        raise RunnerError("isolation-policy-mismatch", "runner environment denylist is incomplete")
    if policy["credential_broker"].get("delivery") != "opaque-single-use-handle-on-fd":
        raise RunnerError("isolation-policy-mismatch", "runner policy permits credential delivery outside the broker")
    if policy["network"].get("workload_egress") != "trusted-provider-gateway-only":
        raise RunnerError("isolation-policy-mismatch", "runner network policy is not gateway-only")
    if policy["network"].get("dns") != "denied" or policy["network"].get("metadata_routes") != "denied":
        raise RunnerError("isolation-policy-mismatch", "runner network policy permits DNS or metadata routes")
    ceilings = policy["resource_ceilings"]
    for key, request_key in (("cpu_seconds", "cpu_seconds"), ("rss_bytes", "rss_bytes"), ("pids", "pids"), ("disk_bytes", "disk_bytes")):
        ceiling = int_value(ceilings.get(key), f"runner policy ceiling {key}", 1, 2**63 - 1)
        if request[request_key] > ceiling:
            raise RunnerError("isolation-policy-mismatch", f"requested {request_key} exceeds the reviewed runner ceiling")
    if policy["whole_job_lifecycle"].get("kill_scope") != "whole-isolate" or policy["whole_job_lifecycle"].get("quiescence") != "supervisor-observed-no-tasks":
        raise RunnerError("isolation-policy-mismatch", "runner whole-job lifecycle policy is incomplete")
    request_contract = exact_object(
        policy["request"],
        {
            "version",
            "document_type",
            "execution_attempt_required",
            "provider_execution_identity_required",
            "provider_execution_identity_digest_required",
        },
        "runner request contract",
    )
    if request_contract != {
        "version": REQUEST_VERSION,
        "document_type": "provider-runner-request",
        "execution_attempt_required": True,
        "provider_execution_identity_required": True,
        "provider_execution_identity_digest_required": True,
    }:
        raise RunnerError("isolation-policy-mismatch", "runner request contract is stale or incomplete")
    receipt_contract = exact_object(
        policy["receipt"],
        {
            "version",
            "document_type",
            "canonical_body",
            "execution_attempt_bound",
            "provider_execution_identity_digest_bound",
            "detached_signature_required",
        },
        "runner receipt contract",
    )
    if (
        receipt_contract["version"] != RECEIPT_VERSION
        or receipt_contract["document_type"] != "provider-runner-receipt"
        or receipt_contract["canonical_body"]
        != "sorted-utf8-json-integer-string-boolean-null-vocabulary"
        or receipt_contract["execution_attempt_bound"] is not True
        or receipt_contract["provider_execution_identity_digest_bound"] is not True
        or receipt_contract["detached_signature_required"] is not True
    ):
        raise RunnerError("isolation-policy-mismatch", "runner receipt policy is incomplete")
    if policy["gateway_observation"].get("authority") != "trusted-gateway-signed-receipt-only":
        raise RunnerError("isolation-policy-mismatch", "gateway observation authority is invalid")
    runtime_inputs = policy["immutable_runtime_inputs"]
    expected_runtime_keys = {
        "provider_executable_in_image", "provider_result_schema_in_image",
        "provider_result_schema_sha256", "provider_result_schema_version",
        "provider_result_document_type", "job_work_directory", "provider_last_message",
    }
    exact_object(runtime_inputs, expected_runtime_keys, "immutable runtime inputs")
    if runtime_inputs["provider_executable_in_image"] != "/opt/vibapp/bin/codex":
        raise RunnerError("isolation-policy-mismatch", "in-isolate provider executable path is not fixed")
    if runtime_inputs["provider_result_schema_in_image"] != "/opt/vibapp/contracts/provider-result.schema.json":
        raise RunnerError("isolation-policy-mismatch", "in-isolate provider result schema path is not fixed")
    sha256_value(runtime_inputs["provider_result_schema_sha256"], "provider result schema pin")
    if (
        runtime_inputs["provider_result_schema_version"] != PROVIDER_RESULT_SCHEMA_VERSION
        or runtime_inputs["provider_result_document_type"] != PROVIDER_RESULT_DOCUMENT_TYPE
    ):
        raise RunnerError("isolation-policy-mismatch", "provider-result schema/document contract is stale")
    if runtime_inputs["job_work_directory"] != "/job/work" or runtime_inputs["provider_last_message"] != "/job/work/provider-last-message.json":
        raise RunnerError("isolation-policy-mismatch", "in-isolate workspace/output paths are not fixed")
    return request


def validate_provider_result_schema_contract(path: Path, runtime_inputs: dict[str, Any]) -> None:
    """Bind the exact schema bytes and its two document discriminator constants."""
    if sha256_file(path) != runtime_inputs["provider_result_schema_sha256"]:
        raise RunnerError("command-invalid", "provider output schema bytes differ from the reviewed pin")
    schema = load_strict_json(path)
    if not isinstance(schema, dict):
        raise RunnerError("command-invalid", "provider output schema is not an object")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RunnerError("command-invalid", "provider output schema has no properties object")
    schema_version = properties.get("schema_version")
    document_type = properties.get("document_type")
    if (
        not isinstance(schema_version, dict)
        or schema_version.get("const") != runtime_inputs["provider_result_schema_version"]
        or not isinstance(document_type, dict)
        or document_type.get("const") != runtime_inputs["provider_result_document_type"]
    ):
        raise RunnerError("command-invalid", "provider output schema discriminators differ from policy")


def validate_gateway_destination_policy(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "document_type", "status", "workload_network_namespace",
        "default_egress", "allowed_egress", "denied_routes",
        "provider_destinations_enforced_by", "gateway_receipt_required",
        "gateway_receipt_version",
    }
    policy = exact_object(value, keys, "gateway egress policy")
    if policy["schema_version"] != "vibapp.provider-gateway-egress-policy.experimental-v1":
        raise RunnerError("gateway-policy-invalid", "gateway egress policy version is unsupported")
    constants = {
        "document_type": "provider-gateway-egress-policy",
        "status": "deployment-unprovisioned",
        "workload_network_namespace": "per-job-private",
        "default_egress": "deny",
        "provider_destinations_enforced_by": "trusted-provider-gateway",
        "gateway_receipt_required": True,
        "gateway_receipt_version": GATEWAY_RECEIPT_VERSION,
    }
    if any(policy[key] != expected for key, expected in constants.items()):
        raise RunnerError("gateway-policy-invalid", "gateway egress policy weakens a mandatory control")
    allowed = policy["allowed_egress"]
    if not isinstance(allowed, list) or len(allowed) != 1:
        raise RunnerError("gateway-policy-invalid", "gateway egress policy must have exactly one destination role")
    row = exact_object(
        allowed[0],
        {"destination_role", "transport", "address_source", "ports", "dns_required"},
        "gateway allowed egress",
    )
    if row != {
        "destination_role": "trusted-provider-gateway",
        "transport": "tcp-mtls",
        "address_source": "independently-pinned-private-address",
        "ports": [443],
        "dns_required": False,
    }:
        raise RunnerError("gateway-policy-invalid", "gateway egress destination is not the exact reviewed role")
    routes = policy["denied_routes"]
    if not isinstance(routes, list) or len(routes) < 8 or any(not isinstance(route, str) for route in routes):
        raise RunnerError("gateway-policy-invalid", "gateway denied-route inventory is incomplete")
    return policy


def validate_provider_runner_request_envelope(request: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the CloudAgent ProviderRunnerRequest v2 discriminators and bindings."""
    if (
        getattr(request, "schema_version", None) != REQUEST_VERSION
        or getattr(request, "document_type", None) != "provider-runner-request"
    ):
        raise RunnerError("runner-request-invalid", "provider runner request schema/document type is unsupported")
    bounded_identifier(getattr(request, "job_id", None), "request.job_id")
    attempt = validate_execution_attempt(
        getattr(request, "execution_attempt", None), "request.execution_attempt"
    )
    sha256_value(
        getattr(request, "immutable_task_digest_sha256", None),
        "request.immutable_task_digest_sha256",
    )
    provider = getattr(request, "provider", None)
    if provider != "openai-codex":
        raise RunnerError("provider-mismatch", "external provider runner supports only openai-codex")
    identity_digest = sha256_value(
        getattr(request, "provider_execution_identity_sha256", None),
        "request.provider_execution_identity_sha256",
    )
    identity = validate_provider_execution_identity(
        getattr(request, "provider_execution_identity", None),
        provider,
        identity_digest,
        "request.provider_execution_identity",
    )
    executable_identity = getattr(request, "executable_identity", None)
    executable_identity = exact_object(
        executable_identity,
        {
            "configured_path",
            "resolved_path",
            "symlink_chain",
            "version",
            "sha256",
            "owner_uid",
            "mode",
        },
        "request.executable_identity",
    )
    executable_digest = sha256_value(
        executable_identity.get("sha256"), "request.executable_identity.sha256"
    )
    if executable_digest != identity["runtime"]["executable_sha256"]:
        raise RunnerError(
            "provider-identity-mismatch",
            "request executable identity differs from the provider execution identity",
        )
    resolved_path = executable_identity.get("resolved_path")
    if not isinstance(resolved_path, str) or not resolved_path.startswith("/"):
        raise RunnerError("runner-request-invalid", "request executable resolved_path is not absolute")
    configured_path = executable_identity.get("configured_path")
    if not isinstance(configured_path, str) or not configured_path.startswith("/"):
        raise RunnerError("runner-request-invalid", "request executable configured_path is not absolute")
    chain = executable_identity.get("symlink_chain")
    if (
        not isinstance(chain, list)
        or not 1 <= len(chain) <= 16
        or any(not isinstance(path, str) or not path.startswith("/") for path in chain)
    ):
        raise RunnerError("runner-request-invalid", "request executable symlink_chain is invalid")
    if executable_identity.get("version") != identity["runtime"]["package_version"]:
        raise RunnerError(
            "provider-identity-mismatch",
            "request executable version differs from the provider execution identity",
        )
    int_value(executable_identity.get("owner_uid"), "request.executable_identity.owner_uid", 0, 2**31 - 1)
    int_value(executable_identity.get("mode"), "request.executable_identity.mode", 0, 0o7777)
    sha256_value(getattr(request, "input_bundle_sha256", None), "request.input_bundle_sha256")
    isolation_policy = getattr(request, "isolation_policy", None)
    if not isinstance(isolation_policy, dict):
        raise RunnerError("runner-request-invalid", "request.isolation_policy is not an object")
    policy_digest = sha256_value(
        getattr(request, "isolation_policy_sha256", None),
        "request.isolation_policy_sha256",
    )
    if sha256_bytes(canonical_json(isolation_policy)) != policy_digest:
        raise RunnerError("isolation-policy-mismatch", "request isolation policy digest is invalid")
    prompt = getattr(request, "prompt", None)
    if type(prompt) is not bytes or not prompt or len(prompt) > 256 * 1024:
        raise RunnerError("prompt-limit", "provider prompt must be non-empty and at most 256 KiB")
    command = getattr(request, "command", None)
    if not isinstance(command, list):
        raise RunnerError("runner-request-invalid", "request.command is not an argument list")
    workspace = getattr(request, "workspace", None)
    if not isinstance(workspace, Path):
        raise RunnerError("runner-request-invalid", "request.workspace is not a typed Path")
    return attempt, identity


def normalize_cloud_agent_command(
    command: Any,
    *,
    executable_path: Path,
    workspace: Path,
    runner_policy: dict[str, Any],
) -> tuple[str, ...]:
    """Reject command drift and replace every host path with a pinned isolate path."""
    if not isinstance(command, list) or len(command) != 18 or any(not isinstance(item, str) or not item for item in command):
        raise RunnerError("command-invalid", "provider command does not have the exact reviewed argument shape")
    fixed = [
        "exec", "--model", None, "--ephemeral", "-s", "workspace-write", "-C", None,
        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
        "--output-schema", None, "--json", "-o", None, "-",
    ]
    for index, expected in enumerate(fixed, start=1):
        if expected is not None and command[index] != expected:
            raise RunnerError("command-invalid", f"provider command argument {index} drifted from the reviewed shape")
    resolved_executable = Path(command[0]).resolve(strict=True)
    if resolved_executable != executable_path.resolve(strict=True):
        raise RunnerError("command-invalid", "provider command executable path is not the pinned host input")
    resolved_workspace = workspace.resolve(strict=True)
    model = command[3]
    if (
        model.strip() != model
        or len(model) > 256
        or model.startswith("-")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in model)
    ):
        raise RunnerError("command-invalid", "provider command model is not explicit and bounded")
    if Path(command[8]).resolve(strict=True) != resolved_workspace:
        raise RunnerError("command-invalid", "provider command workspace path is not the digest-bound input")
    schema_path = Path(command[13]).resolve(strict=True)
    schema_metadata = schema_path.lstat()
    if stat.S_ISLNK(schema_metadata.st_mode) or not stat.S_ISREG(schema_metadata.st_mode):
        raise RunnerError("command-invalid", "provider output schema is not a regular file")
    runtime_inputs = runner_policy["immutable_runtime_inputs"]
    validate_provider_result_schema_contract(schema_path, runtime_inputs)
    expected_output = resolved_workspace / "provider-last-message.json"
    if Path(command[16]).resolve(strict=False) != expected_output:
        raise RunnerError("command-invalid", "provider last-message output escapes or differs from the workspace")
    return (
        runtime_inputs["provider_executable_in_image"],
        "exec", "--model", model, "--ephemeral", "-s", "workspace-write", "-C",
        runtime_inputs["job_work_directory"],
        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
        "--output-schema", runtime_inputs["provider_result_schema_in_image"],
        "--json", "-o", runtime_inputs["provider_last_message"], "-",
    )


@dataclass(frozen=True)
class GatewayReceipt:
    schema_version: str
    document_type: str
    job_id: str
    immutable_task_digest_sha256: str
    input_bundle_sha256: str
    executed_executable_sha256: str
    destination_policy_sha256: str
    credential_handle_sha256: str
    external_request_attempted: bool
    external_request_observed: bool
    gateway_request_id: str
    attestation_type: str
    attestation_key_id: str
    receipt_digest_sha256: str
    attestation_signature: str


def gateway_receipt_body(receipt: GatewayReceipt) -> dict[str, Any]:
    body = asdict(receipt)
    del body["receipt_digest_sha256"]
    del body["attestation_signature"]
    return body


def gateway_receipt_digest(receipt: GatewayReceipt) -> str:
    return sha256_bytes(canonical_json(gateway_receipt_body(receipt)))


def validate_gateway_receipt(
    receipt: Any,
    *,
    job_id: str,
    immutable_task_digest_sha256: str,
    input_bundle_sha256: str,
    executable_sha256: str,
    destination_policy_sha256: str,
    credential_handle_sha256: str,
    accepted_key_id: str,
    verifier: DetachedSignatureVerifier | None,
) -> GatewayReceipt:
    if type(receipt) is not GatewayReceipt:
        raise RunnerError("gateway-receipt-invalid", "gateway observation must be a typed trusted-gateway receipt")
    if verifier is None:
        raise RunnerError("gateway-attestation-unavailable", "no trusted gateway signature verifier is configured")
    if receipt.schema_version != GATEWAY_RECEIPT_VERSION or receipt.document_type != "provider-gateway-receipt":
        raise RunnerError("gateway-receipt-invalid", "unsupported gateway receipt version/type")
    for value, context in (
        (receipt.job_id, "gateway.job_id"),
        (receipt.gateway_request_id, "gateway.gateway_request_id"),
    ):
        bounded_identifier(value, context)
    bounded_identifier(receipt.attestation_key_id, "gateway.attestation_key_id", runner=True)
    for value, context in (
        (receipt.immutable_task_digest_sha256, "gateway.task"),
        (receipt.input_bundle_sha256, "gateway.input"),
        (receipt.executed_executable_sha256, "gateway.executable"),
        (receipt.destination_policy_sha256, "gateway.destination_policy"),
        (receipt.credential_handle_sha256, "gateway.credential_handle"),
        (receipt.receipt_digest_sha256, "gateway.receipt_digest"),
    ):
        sha256_value(value, context)
    bool_value(receipt.external_request_attempted, "gateway.external_request_attempted")
    bool_value(receipt.external_request_observed, "gateway.external_request_observed")
    if receipt.external_request_attempted is not True or receipt.external_request_observed is not True:
        raise RunnerError("gateway-receipt-invalid", "a live result requires gateway-observed attempted egress")
    if receipt.attestation_type != GATEWAY_ATTESTATION_TYPE:
        raise RunnerError("gateway-receipt-invalid", "unsupported gateway attestation type")
    if receipt.attestation_key_id != accepted_key_id:
        raise RunnerError("gateway-attestation-invalid", "gateway key is not independently accepted")
    expected_bindings = (
        receipt.job_id == job_id,
        receipt.immutable_task_digest_sha256 == immutable_task_digest_sha256,
        receipt.input_bundle_sha256 == input_bundle_sha256,
        receipt.executed_executable_sha256 == executable_sha256,
        receipt.destination_policy_sha256 == destination_policy_sha256,
        receipt.credential_handle_sha256 == credential_handle_sha256,
    )
    if not all(expected_bindings):
        raise RunnerError("gateway-receipt-invalid", "gateway receipt is bound to another request/policy/credential handle")
    digest = gateway_receipt_digest(receipt)
    if receipt.receipt_digest_sha256 != digest:
        raise RunnerError("gateway-attestation-invalid", "gateway receipt body digest is invalid")
    if not isinstance(receipt.attestation_signature, str) or not SIGNATURE.fullmatch(receipt.attestation_signature):
        raise RunnerError("gateway-attestation-invalid", "gateway signature encoding is invalid")
    try:
        valid = verifier.verify(bytes.fromhex(digest), receipt.attestation_signature, receipt.attestation_key_id)
    except Exception as error:
        raise RunnerError("gateway-attestation-invalid", "gateway verifier failed closed") from error
    if valid is not True:
        raise RunnerError("gateway-attestation-invalid", "gateway signature is not accepted")
    return receipt


@dataclass(frozen=True)
class ProviderReceiptV3:
    schema_version: str
    document_type: str
    job_id: str
    execution_attempt: dict[str, Any]
    immutable_task_digest_sha256: str
    provider_execution_identity_sha256: str
    input_bundle_sha256: str
    output_bundle_sha256: str | None
    runner_id: str
    runner_identity_sha256: str
    isolation_policy_sha256: str
    external_request_attempted: bool
    external_request_observed: bool
    gateway_request_id: str | None
    whole_job_quiescent: bool
    exit_code: int
    stdout_bytes: int
    stderr_bytes: int
    executed_executable_sha256: str | None
    attestation_type: str
    attestation_key_id: str
    receipt_digest_sha256: str
    attestation_signature: str


def provider_receipt_body(receipt: ProviderReceiptV3) -> dict[str, Any]:
    body = asdict(receipt)
    del body["receipt_digest_sha256"]
    del body["attestation_signature"]
    return body


def provider_receipt_digest(receipt: ProviderReceiptV3) -> str:
    return sha256_bytes(canonical_json(provider_receipt_body(receipt)))


@dataclass(frozen=True)
class AcceptedRunnerProfile:
    schema_version: str
    document_type: str
    runner_id: str
    runner_identity_sha256: str
    runner_policy_sha256: str
    executable_sha256: str
    receipt_key_id: str
    gateway_key_id: str
    destination_policy_sha256: str
    status: str

    @classmethod
    def from_dict(cls, value: Any) -> "AcceptedRunnerProfile":
        keys = {
            "schema_version", "document_type",
            "runner_id", "runner_identity_sha256", "runner_policy_sha256",
            "executable_sha256", "receipt_key_id", "gateway_key_id",
            "destination_policy_sha256", "status",
        }
        item = exact_object(value, keys, "accepted profile")
        result = cls(**item)
        return validate_accepted_runner_profile(result)


def validate_accepted_runner_profile(profile: Any) -> AcceptedRunnerProfile:
    if type(profile) is not AcceptedRunnerProfile:
        raise RunnerError("runner-profile-unaccepted", "accepted runner profile is untyped")
    if profile.schema_version != ACCEPTED_PROFILE_VERSION:
        raise RunnerError("runner-profile-unaccepted", "accepted runner profile version is unsupported")
    if profile.document_type != "provider-runner-accepted-profile":
        raise RunnerError("runner-profile-unaccepted", "accepted runner profile document type is unsupported")
    for value, context in (
        (profile.runner_id, "profile.runner_id"),
        (profile.receipt_key_id, "profile.receipt_key_id"),
        (profile.gateway_key_id, "profile.gateway_key_id"),
    ):
        bounded_identifier(value, context, runner=True)
        normalized = value.casefold()
        compact = "".join(character for character in normalized if character.isalnum())
        if (
            not compact
            or compact == "0" * len(compact)
            or any(marker in compact for marker in ("unprovisioned", "placeholder", "replaceme", "replacewithrealvalue"))
        ):
            raise RunnerError("runner-profile-unaccepted", f"{context} is a documented placeholder/sentinel")
    for field in (
        "runner_identity_sha256", "runner_policy_sha256", "executable_sha256",
        "destination_policy_sha256",
    ):
        digest = sha256_value(getattr(profile, field), f"profile.{field}")
        if digest == "0" * 64:
            raise RunnerError("runner-profile-unaccepted", f"profile.{field} is an all-zero placeholder digest")
    if profile.status != "independently-accepted":
        raise RunnerError("runner-profile-unaccepted", "runner profile is not independently accepted")
    return profile


def validate_provider_receipt_v3(
    receipt: Any,
    *,
    request_job_id: str,
    execution_attempt: dict[str, Any],
    immutable_task_digest_sha256: str,
    provider_execution_identity_sha256: str,
    input_bundle_sha256: str,
    output_bundle_sha256: str,
    isolation_policy_sha256: str,
    profile: AcceptedRunnerProfile,
    verifier: DetachedSignatureVerifier | None,
) -> ProviderReceiptV3:
    if type(receipt) is not ProviderReceiptV3:
        raise RunnerError("runner-receipt-invalid", "receipt is not a typed ProviderReceiptV3")
    validate_accepted_runner_profile(profile)
    if verifier is None:
        raise RunnerError("runner-attestation-unavailable", "no runner signature verifier is configured")
    if receipt.schema_version != RECEIPT_VERSION or receipt.document_type != "provider-runner-receipt":
        raise RunnerError("runner-receipt-invalid", "unsupported runner receipt version/type")
    bounded_identifier(receipt.job_id, "receipt.job_id")
    try:
        receipt_attempt = validate_execution_attempt(receipt.execution_attempt, "receipt.execution_attempt")
        request_attempt = validate_execution_attempt(execution_attempt, "request.execution_attempt")
    except RunnerError as error:
        raise RunnerError("runner-receipt-invalid", str(error)) from error
    sha256_value(immutable_task_digest_sha256, "request.immutable_task_digest_sha256")
    sha256_value(
        provider_execution_identity_sha256,
        "request.provider_execution_identity_sha256",
    )
    bounded_identifier(receipt.runner_id, "receipt.runner_id", runner=True)
    bounded_identifier(receipt.attestation_key_id, "receipt.attestation_key_id", runner=True)
    for value, context in (
        (receipt.immutable_task_digest_sha256, "receipt.task"),
        (receipt.provider_execution_identity_sha256, "receipt.provider_execution_identity"),
        (receipt.input_bundle_sha256, "receipt.input"),
        (receipt.output_bundle_sha256, "receipt.output"),
        (receipt.runner_identity_sha256, "receipt.runner_identity"),
        (receipt.isolation_policy_sha256, "receipt.policy"),
        (receipt.executed_executable_sha256, "receipt.executable"),
        (receipt.receipt_digest_sha256, "receipt.digest"),
    ):
        sha256_value(value, context)
    bool_value(receipt.external_request_attempted, "receipt.external_request_attempted")
    bool_value(receipt.external_request_observed, "receipt.external_request_observed")
    bool_value(receipt.whole_job_quiescent, "receipt.whole_job_quiescent")
    int_value(receipt.exit_code, "receipt.exit_code", 0, 255)
    int_value(receipt.stdout_bytes, "receipt.stdout_bytes", 0, 1024 * 1024)
    int_value(receipt.stderr_bytes, "receipt.stderr_bytes", 0, 1024 * 1024)
    if receipt.gateway_request_id is None:
        raise RunnerError("runner-receipt-invalid", "live receipt requires a trusted gateway request ID")
    bounded_identifier(receipt.gateway_request_id, "receipt.gateway_request_id", runner=True)
    if receipt.attestation_type != RECEIPT_ATTESTATION_TYPE:
        raise RunnerError("runner-receipt-invalid", "unsupported runner attestation type")
    if receipt.external_request_attempted is not True or receipt.external_request_observed is not True:
        raise RunnerError("runner-receipt-invalid", "accepted live receipt must carry gateway-observed true/true")
    if receipt.whole_job_quiescent is not True:
        raise RunnerError("process-tree-not-quiescent", "runner receipt does not prove whole-job quiescence")
    bindings = (
        receipt.job_id == request_job_id,
        receipt_attempt == request_attempt,
        receipt.immutable_task_digest_sha256 == immutable_task_digest_sha256,
        receipt.provider_execution_identity_sha256 == provider_execution_identity_sha256,
        receipt.input_bundle_sha256 == input_bundle_sha256,
        receipt.output_bundle_sha256 == output_bundle_sha256,
        receipt.runner_id == profile.runner_id,
        receipt.runner_identity_sha256 == profile.runner_identity_sha256,
        receipt.isolation_policy_sha256 == isolation_policy_sha256,
        receipt.executed_executable_sha256 == profile.executable_sha256,
        receipt.attestation_key_id == profile.receipt_key_id,
    )
    if not all(bindings):
        raise RunnerError("runner-receipt-invalid", "runner receipt binding or accepted pin mismatch")
    digest = provider_receipt_digest(receipt)
    if receipt.receipt_digest_sha256 != digest:
        raise RunnerError("runner-attestation-invalid", "runner receipt body digest is invalid")
    if not isinstance(receipt.attestation_signature, str) or not SIGNATURE.fullmatch(receipt.attestation_signature):
        raise RunnerError("runner-attestation-invalid", "runner signature encoding is invalid")
    try:
        valid = verifier.verify(bytes.fromhex(digest), receipt.attestation_signature, receipt.attestation_key_id)
    except Exception as error:
        raise RunnerError("runner-attestation-invalid", "runner verifier failed closed") from error
    if valid is not True:
        raise RunnerError("runner-attestation-invalid", "runner signature is not accepted")
    return receipt


@dataclass(frozen=True)
class IsolationIdentity:
    isolate_id: str
    uid: int
    gid: int
    enforcement: str


class IdentityAllocator:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def allocate(self, job_id: str, *, synthetic: bool) -> IsolationIdentity:
        bounded_identifier(job_id, "job_id")
        for _ in range(128):
            uid = 200000 + secrets.randbelow(400000)
            claim = self.root / f"{uid}.claim"
            try:
                descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            isolate_id = f"job-{secrets.token_hex(16)}"
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json({"job_id": job_id, "isolate_id": isolate_id, "uid": uid, "gid": uid}) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            identity_root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(identity_root_fd)
            finally:
                os.close(identity_root_fd)
            return IsolationIdentity(
                isolate_id=isolate_id,
                uid=uid,
                gid=uid,
                enforcement="synthetic-label-only" if synthetic else "external-supervisor-required",
            )
        raise RunnerError("identity-exhausted", "cannot allocate a fresh per-job identity")


@dataclass(frozen=True)
class ProductionJob:
    request_schema_version: str
    request_document_type: str
    job_id: str
    execution_attempt: dict[str, Any]
    immutable_task_digest_sha256: str
    provider_execution_identity: dict[str, Any]
    provider_execution_identity_sha256: str
    input_directory: Path
    output_directory: Path
    input_bundle_sha256: str
    executable_path: Path
    executable_sha256: str
    command: tuple[str, ...]
    prompt: bytes
    credential_handle: bytes
    credential_handle_sha256: str
    identity: IsolationIdentity
    limits: Limits
    isolation_policy: dict[str, Any]
    isolation_policy_sha256: str
    destination_policy_sha256: str


@dataclass(frozen=True)
class BackendResult:
    exit_code: int
    stdout_bytes: int
    stderr_bytes: int
    whole_job_quiescent: bool
    gateway_receipt: GatewayReceipt | None
    applied_identity: IsolationIdentity
    applied_policy_sha256: str
    executed_executable_sha256: str
    wall_time_milliseconds: int
    cpu_time_milliseconds: int
    maximum_rss_bytes: int
    maximum_pids: int
    maximum_disk_bytes: int


class UnavailableBackend:
    def execute(self, job: ProductionJob) -> BackendResult:
        raise RunnerError("external-isolation-unavailable", "no production VM/container backend is configured")


@dataclass(frozen=True)
class SyntheticRunReport:
    schema_version: str
    document_type: str
    job_id: str
    status: str
    input_bundle_sha256: str
    output_bundle_sha256: str | None
    executable_sha256: str
    isolate_id: str
    allocated_uid: int
    allocated_gid: int
    identity_enforcement: str
    system_uid: int
    minimal_environment_keys: list[str]
    external_request_attempted: bool
    external_request_observed: bool
    gateway_request_id: None
    whole_job_quiescent: bool
    exit_code: int | None
    stdout_bytes: int
    stderr_bytes: int
    rejected_code: str | None


def _tighten_child_limits(limits: Limits) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    current_no_file = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, current_no_file), current_no_file))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.disk_bytes, limits.disk_bytes))
    cpu_hard = min(limits.cpu_seconds + 1, resource.getrlimit(resource.RLIMIT_CPU)[1])
    resource.setrlimit(resource.RLIMIT_CPU, (min(limits.cpu_seconds, cpu_hard), cpu_hard))
    if sys.platform != "darwin":
        current_as = resource.getrlimit(resource.RLIMIT_AS)[1]
        target_as = min(limits.rss_bytes, current_as)
        resource.setrlimit(resource.RLIMIT_AS, (target_as, current_as))
        if hasattr(resource, "RLIMIT_NPROC"):
            current_nproc = resource.getrlimit(resource.RLIMIT_NPROC)[1]
            resource.setrlimit(resource.RLIMIT_NPROC, (min(limits.pids, current_nproc), current_nproc))


def _process_group_snapshot(pgid: int) -> dict[int, dict[str, int]]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,uid=,rss=,state="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerError("process-observation-failed", "cannot observe synthetic process group") from error
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise RunnerError("process-observation-failed", "synthetic process observation failed closed")
    members: dict[int, dict[str, int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, pgid_text, uid_text, rss_text, state = line.decode("ascii").split()
        except (UnicodeDecodeError, ValueError) as error:
            raise RunnerError("process-observation-failed", "synthetic process observation was malformed") from error
        if int(pgid_text) == pgid and not state.startswith("Z"):
            members[int(pid_text)] = {"uid": int(uid_text), "rss": int(rss_text) * 1024}
    return members


def _kill_group_and_prove_quiet(pgid: int) -> None:
    deadline = time.monotonic() + 3
    sent_kill = False
    quiet = 0
    while time.monotonic() < deadline:
        members = _process_group_snapshot(pgid)
        if not members:
            quiet += 1
            if quiet >= 2:
                return
            time.sleep(0.025)
            continue
        quiet = 0
        try:
            os.killpg(pgid, signal.SIGKILL if sent_kill else signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            if any(item["uid"] != os.getuid() for item in members.values()):
                raise RunnerError("process-tree-not-quiescent", "synthetic process group contains a foreign identity") from error
            for pid in members:
                try:
                    os.kill(pid, signal.SIGKILL if sent_kill else signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError as child_error:
                    raise RunnerError("process-tree-not-quiescent", "cannot terminate a synthetic process") from child_error
        sent_kill = True
        time.sleep(0.05)
    raise RunnerError("process-tree-not-quiescent", "synthetic process group did not become quiet")


class SyntheticLifecycleRunner:
    """Bounded local oracle. It is intentionally not a ProviderBackend."""

    MODES = {"success", "output-bomb", "disk-bomb", "fork-burst", "hang", "child-holds", "nonzero"}

    def __init__(self, state_root: Path):
        self.state_root = state_root
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.identities = IdentityAllocator(self.state_root / "identity-claims")
        self.executable_sha256 = sha256_file(SYNTHETIC_EXECUTABLE)

    def run(self, job_id: str, input_root: Path, limits: Limits, *, mode: str = "success") -> SyntheticRunReport:
        bounded_identifier(job_id, "job_id")
        if mode not in self.MODES:
            raise RunnerError("synthetic-command-denied", "synthetic mode is not allowlisted")
        claim_path = self.state_root / "job-claims" / f"{job_id}.json"
        claim_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            claim_fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RunnerError("replay-rejected", "job_id has already been consumed") from error
        identity = self.identities.allocate(job_id, synthetic=True)
        with os.fdopen(claim_fd, "wb") as handle:
            handle.write(canonical_json({"job_id": job_id, "isolate_id": identity.isolate_id}) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

        input_digest = bundle_digest(input_root, limits.output_files, limits.disk_bytes)
        job_root = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=self.state_root))
        input_copy = job_root / "input"
        output = job_root / "output"
        tmp = job_root / "tmp"
        copy_bundle(input_root, input_copy, limits.output_files, limits.disk_bytes, read_only=True)
        output.mkdir(mode=0o700)
        tmp.mkdir(mode=0o700)
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "TMPDIR": str(tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "VIBAPP_INPUT_DIR": str(input_copy),
            "VIBAPP_OUTPUT_DIR": str(output),
            "VIBAPP_SYNTHETIC_MODE": mode,
            "VIBAPP_ISOLATE_ID": identity.isolate_id,
        }
        command = [sys.executable, "-I", "-B", str(SYNTHETIC_EXECUTABLE)]
        process: subprocess.Popen[bytes] | None = None
        counts = {"stdout": 0, "stderr": 0}
        exit_code: int | None = None
        rejected: str | None = None
        output_digest: str | None = None
        quiescent = False
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job_root,
                env=environment,
                start_new_session=True,
                close_fds=True,
                preexec_fn=lambda: _tighten_child_limits(limits),
            )
            assert process.stdout is not None and process.stderr is not None
            selector = selectors.DefaultSelector()
            for stream, name, ceiling in (
                (process.stdout, "stdout", limits.stdout_bytes),
                (process.stderr, "stderr", limits.stderr_bytes),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, (name, ceiling))
            deadline = time.monotonic() + limits.wall_time_seconds
            while selector.get_map():
                if time.monotonic() >= deadline:
                    rejected = "deadline-exceeded"
                    break
                for key, _ in selector.select(timeout=0.025):
                    stream = key.fileobj
                    name, ceiling = key.data
                    chunk = os.read(stream.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    counts[name] += len(chunk)
                    if counts[name] > ceiling:
                        rejected = f"{name}-limit"
                        break
                if rejected:
                    break
                members = _process_group_snapshot(process.pid)
                if len(members) > limits.pids:
                    rejected = "pid-limit"
                    break
                if sum(item["rss"] for item in members.values()) > limits.rss_bytes:
                    rejected = "rss-limit"
                    break
                try:
                    safe_tree_records(job_root, limits.output_files + 32, limits.disk_bytes)
                except RunnerError:
                    rejected = "disk-limit"
                    break
                polled = process.poll()
                if polled is not None:
                    exit_code = polled
            selector.close()
            if exit_code is None and rejected is None:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    exit_code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    rejected = "deadline-exceeded"
            _kill_group_and_prove_quiet(process.pid)
            quiescent = True
            if rejected is None and exit_code == 0:
                output_digest = bundle_digest(output, limits.output_files, limits.disk_bytes)
            elif rejected is None and exit_code != 0:
                rejected = "synthetic-nonzero"
        finally:
            if process is not None:
                try:
                    _kill_group_and_prove_quiet(process.pid)
                    quiescent = True
                except RunnerError:
                    quiescent = False
                try:
                    observed_return = process.wait(timeout=0.5)
                    if exit_code is None:
                        exit_code = observed_return
                except subprocess.TimeoutExpired:
                    quiescent = False
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

        if exit_code is not None and exit_code < 0:
            exit_code = min(255, 128 + abs(exit_code))

        return SyntheticRunReport(
            schema_version=SYNTHETIC_REPORT_VERSION,
            document_type="provider-runner-synthetic-report",
            job_id=job_id,
            status="synthetic-pass" if rejected is None and quiescent else "synthetic-rejected",
            input_bundle_sha256=input_digest,
            output_bundle_sha256=output_digest,
            executable_sha256=self.executable_sha256,
            isolate_id=identity.isolate_id,
            allocated_uid=identity.uid,
            allocated_gid=identity.gid,
            identity_enforcement=identity.enforcement,
            system_uid=os.getuid(),
            minimal_environment_keys=sorted(environment),
            external_request_attempted=False,
            external_request_observed=False,
            gateway_request_id=None,
            whole_job_quiescent=quiescent,
            exit_code=exit_code,
            stdout_bytes=counts["stdout"],
            stderr_bytes=counts["stderr"],
            rejected_code=rejected,
        )


@dataclass(frozen=True)
class ClaimedProductionJob:
    claim_token: str
    request_schema_version: str
    request_document_type: str
    job_id: str
    execution_attempt: dict[str, Any]
    execution_attempt_sha256: str
    immutable_task_digest_sha256: str
    provider_execution_identity: dict[str, Any]
    provider_execution_identity_sha256: str
    input_directory: Path
    output_directory: Path
    input_bundle_sha256: str
    executable_path: Path
    executable_sha256: str
    command: tuple[str, ...]
    prompt: bytes
    identity: IsolationIdentity
    limits: Limits
    isolation_policy: dict[str, Any]
    isolation_policy_sha256: str
    runner_policy_sha256: str
    destination_policy_sha256: str
    runner_id: str
    runner_identity_sha256: str
    receipt_key_id: str
    gateway_key_id: str
    returned_output_root: Path


class ExternalOneJobProviderRunner:
    """Production-shape runner. Without every trust dependency it fails closed."""

    def __init__(
        self,
        *,
        state_root: Path,
        backend: ProviderBackend | None = None,
        accepted_profile: AcceptedRunnerProfile | None = None,
        receipt_signer: DetachedReceiptSigner | None = None,
        gateway_verifier: DetachedSignatureVerifier | None = None,
    ):
        self.state_root = state_root
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backend = backend or UnavailableBackend()
        self.accepted_profile = accepted_profile
        self.receipt_signer = receipt_signer
        self.gateway_verifier = gateway_verifier
        self.identities = IdentityAllocator(self.state_root / "identity-claims")
        self.policy = load_strict_json(POLICY_PATH)
        self.policy_digest = sha256_bytes(canonical_json(self.policy))
        self.destination_policy = validate_gateway_destination_policy(load_strict_json(DESTINATION_POLICY_PATH))
        self.destination_policy_digest = sha256_bytes(canonical_json(self.destination_policy))
        self._active_claims: dict[str, ClaimedProductionJob] = {}

    def preflight(self, required_policy: dict[str, Any]) -> dict[str, Any]:
        validate_requested_isolation_policy(required_policy, self.policy)
        if self.accepted_profile is None or self.receipt_signer is None or self.gateway_verifier is None:
            raise RunnerError("runner-attestation-unavailable", "accepted runner profile/signer/gateway verifier is not configured")
        validate_accepted_runner_profile(self.accepted_profile)
        if self.accepted_profile.runner_policy_sha256 != self.policy_digest:
            raise RunnerError("isolation-policy-mismatch", "accepted profile does not pin the current runner policy digest")
        if self.accepted_profile.destination_policy_sha256 != self.destination_policy_digest:
            raise RunnerError("gateway-policy-invalid", "accepted profile does not pin the current gateway policy digest")
        if type(self.backend) is UnavailableBackend:
            raise RunnerError("external-isolation-unavailable", "no production backend is configured")
        return dict(required_policy)

    def begin(
        self,
        *,
        request_schema_version: str,
        request_document_type: str,
        job_id: str,
        execution_attempt: dict[str, Any],
        immutable_task_digest_sha256: str,
        provider_execution_identity: dict[str, Any],
        provider_execution_identity_sha256: str,
        input_root: Path,
        expected_input_bundle_sha256: str,
        executable_path: Path,
        command: list[str],
        prompt: bytes,
        limits: Limits,
        isolation_policy: dict[str, Any],
        expected_isolation_policy_sha256: str,
        returned_output_root: Path,
    ) -> ClaimedProductionJob:
        if request_schema_version != REQUEST_VERSION or request_document_type != "provider-runner-request":
            raise RunnerError("runner-request-invalid", "provider runner request schema/document type is unsupported")
        bounded_identifier(job_id, "job_id")
        attempt = validate_execution_attempt(execution_attempt)
        attempt_digest = sha256_bytes(canonical_json(attempt))
        sha256_value(immutable_task_digest_sha256, "immutable_task_digest_sha256")
        provider_identity = validate_provider_execution_identity(
            provider_execution_identity,
            "openai-codex",
            provider_execution_identity_sha256,
        )
        sha256_value(expected_input_bundle_sha256, "expected_input_bundle_sha256")
        if self.accepted_profile is None:
            raise RunnerError("runner-profile-unaccepted", "no independently accepted runner profile is configured")
        self.preflight(isolation_policy)
        accepted_profile = validate_accepted_runner_profile(self.accepted_profile)
        requested_policy_digest = sha256_bytes(canonical_json(isolation_policy))
        if requested_policy_digest != expected_isolation_policy_sha256:
            raise RunnerError("isolation-policy-mismatch", "request isolation policy digest is invalid")
        if not isinstance(prompt, bytes) or not prompt or len(prompt) > 256 * 1024:
            raise RunnerError("prompt-limit", "provider prompt must be non-empty and at most 256 KiB")
        executable_path = executable_path.resolve(strict=True)
        executable_metadata = executable_path.lstat()
        if not stat.S_ISREG(executable_metadata.st_mode) or stat.S_ISLNK(executable_metadata.st_mode):
            raise RunnerError("executable-pin-mismatch", "reviewed executable is not a regular file")
        if executable_metadata.st_mode & 0o022 or not os.access(executable_path, os.X_OK):
            raise RunnerError("executable-pin-mismatch", "reviewed executable mode is unsafe or non-executable")
        actual_executable = sha256_file(executable_path)
        if actual_executable != self.accepted_profile.executable_sha256:
            raise RunnerError("executable-pin-mismatch", "executable bytes differ from the independently accepted pin")
        if actual_executable != provider_identity["runtime"]["executable_sha256"]:
            raise RunnerError(
                "provider-identity-mismatch",
                "executed provider bytes differ from the request provider execution identity",
            )
        inner_command = normalize_cloud_agent_command(
            command,
            executable_path=executable_path,
            workspace=input_root,
            runner_policy=self.policy,
        )
        if returned_output_root.exists() or returned_output_root.is_symlink():
            raise RunnerError("output-conflict", "returned output root must not already exist")

        claims = self.state_root / "job-claims"
        claims.mkdir(mode=0o700, parents=True, exist_ok=True)
        claim_path = claims / durable_claim_name(job_id, attempt)
        try:
            claim_fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RunnerError("replay-rejected", "job execution attempt was already consumed") from error
        identity = self.identities.allocate(job_id, synthetic=False)
        claim_token = secrets.token_hex(32)
        with os.fdopen(claim_fd, "wb") as handle:
            handle.write(canonical_json({
                "request_schema_version": request_schema_version,
                "request_document_type": request_document_type,
                "job_id": job_id,
                "execution_attempt": attempt,
                "execution_attempt_sha256": attempt_digest,
                "immutable_task_digest_sha256": immutable_task_digest_sha256,
                "provider_execution_identity_sha256": provider_execution_identity_sha256,
                "input_bundle_sha256": expected_input_bundle_sha256,
                "isolate_id": identity.isolate_id,
                "claim_token_sha256": sha256_bytes(claim_token.encode("ascii")),
                "state": "claimed-before-credential-handle",
            }) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        claims_fd = os.open(claims, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(claims_fd)
        finally:
            os.close(claims_fd)

        job_root = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=self.state_root))
        input_copy = job_root / "input"
        output = job_root / "output"
        input_digest = snapshot_bundle_to_directory(
            input_root,
            input_copy,
            limits.output_files,
            limits.disk_bytes,
            expected_digest=expected_input_bundle_sha256,
        )
        output.mkdir(mode=0o700)
        claimed = ClaimedProductionJob(
            claim_token=claim_token,
            request_schema_version=request_schema_version,
            request_document_type=request_document_type,
            job_id=job_id,
            execution_attempt=attempt,
            execution_attempt_sha256=attempt_digest,
            immutable_task_digest_sha256=immutable_task_digest_sha256,
            provider_execution_identity=provider_identity,
            provider_execution_identity_sha256=provider_execution_identity_sha256,
            input_directory=input_copy,
            output_directory=output,
            input_bundle_sha256=input_digest,
            executable_path=executable_path.resolve(strict=True),
            executable_sha256=actual_executable,
            command=inner_command,
            prompt=prompt,
            identity=identity,
            limits=limits,
            isolation_policy=dict(isolation_policy),
            isolation_policy_sha256=requested_policy_digest,
            runner_policy_sha256=accepted_profile.runner_policy_sha256,
            destination_policy_sha256=accepted_profile.destination_policy_sha256,
            runner_id=accepted_profile.runner_id,
            runner_identity_sha256=accepted_profile.runner_identity_sha256,
            receipt_key_id=accepted_profile.receipt_key_id,
            gateway_key_id=accepted_profile.gateway_key_id,
            returned_output_root=returned_output_root,
        )
        self._active_claims[claim_token] = claimed
        return claimed

    def abandon_claim(self, claimed: ClaimedProductionJob) -> None:
        active = self._active_claims.get(claimed.claim_token)
        if active == claimed:
            del self._active_claims[claimed.claim_token]

    def execute_claimed(self, claimed: ClaimedProductionJob, credential_handle: bytes) -> ProviderReceiptV3:
        active = self._active_claims.pop(claimed.claim_token, None)
        if active != claimed:
            raise RunnerError("claim-invalid", "claimed job is stale, forged, or already consumed")
        try:
            current_attempt = validate_execution_attempt(
                claimed.execution_attempt,
                "claimed.execution_attempt",
            )
            current_provider_identity = validate_provider_execution_identity(
                claimed.provider_execution_identity,
                "openai-codex",
                claimed.provider_execution_identity_sha256,
                "claimed.provider_execution_identity",
            )
        except RunnerError as error:
            raise RunnerError("claim-invalid", "claimed request binding changed after durable claim") from error
        if sha256_bytes(canonical_json(current_attempt)) != claimed.execution_attempt_sha256:
            raise RunnerError("claim-invalid", "execution attempt changed after durable claim")
        if sha256_bytes(canonical_json(claimed.isolation_policy)) != claimed.isolation_policy_sha256:
            raise RunnerError("claim-invalid", "isolation policy changed after durable claim")
        if self.accepted_profile is None or self.receipt_signer is None or self.gateway_verifier is None:
            raise RunnerError("runner-attestation-unavailable", "runner trust dependencies disappeared after claim")
        current_profile = validate_accepted_runner_profile(self.accepted_profile)
        if (
            current_profile.runner_id != claimed.runner_id
            or current_profile.runner_identity_sha256 != claimed.runner_identity_sha256
            or current_profile.receipt_key_id != claimed.receipt_key_id
            or current_profile.gateway_key_id != claimed.gateway_key_id
            or current_profile.runner_policy_sha256 != claimed.runner_policy_sha256
            or current_profile.executable_sha256 != claimed.executable_sha256
            or current_profile.destination_policy_sha256 != claimed.destination_policy_sha256
        ):
            raise RunnerError("runner-profile-unaccepted", "accepted runner profile changed after durable claim")
        if type(credential_handle) is not bytes or not credential_handle or len(credential_handle) > 4096:
            raise RunnerError("credential-handle-invalid", "credential handle must be opaque and bounded")
        if b"=" in credential_handle or b"{" in credential_handle or b"\n" in credential_handle:
            raise RunnerError("credential-handle-invalid", "credential handle resembles inline secret/config material")
        if bundle_digest(claimed.input_directory, claimed.limits.output_files, claimed.limits.disk_bytes) != claimed.input_bundle_sha256:
            raise RunnerError("input-bundle-mismatch", "sealed input snapshot changed before backend dispatch")
        credential_digest = sha256_bytes(credential_handle)
        production_job = ProductionJob(
            request_schema_version=claimed.request_schema_version,
            request_document_type=claimed.request_document_type,
            job_id=claimed.job_id,
            execution_attempt=current_attempt,
            immutable_task_digest_sha256=claimed.immutable_task_digest_sha256,
            provider_execution_identity=current_provider_identity,
            provider_execution_identity_sha256=claimed.provider_execution_identity_sha256,
            input_directory=claimed.input_directory,
            output_directory=claimed.output_directory,
            input_bundle_sha256=claimed.input_bundle_sha256,
            executable_path=claimed.executable_path,
            executable_sha256=claimed.executable_sha256,
            command=claimed.command,
            prompt=claimed.prompt,
            credential_handle=credential_handle,
            credential_handle_sha256=credential_digest,
            identity=claimed.identity,
            limits=claimed.limits,
            isolation_policy=claimed.isolation_policy,
            isolation_policy_sha256=claimed.isolation_policy_sha256,
            destination_policy_sha256=claimed.destination_policy_sha256,
        )
        result = self.backend.execute(production_job)
        if result.applied_identity != claimed.identity or result.applied_policy_sha256 != claimed.isolation_policy_sha256:
            raise RunnerError("runner-attestation-invalid", "backend did not apply the allocated identity and pinned policy")
        if result.executed_executable_sha256 != claimed.executable_sha256:
            raise RunnerError("executable-pin-mismatch", "backend executed different bytes")
        if result.whole_job_quiescent is not True:
            raise RunnerError("process-tree-not-quiescent", "backend did not prove whole-isolate quiescence")
        if not 0 <= result.exit_code <= 255:
            raise RunnerError("runner-result-invalid", "backend exit code is invalid")
        observations = (
            (result.wall_time_milliseconds, claimed.limits.wall_time_seconds * 1000, "wall time"),
            (result.cpu_time_milliseconds, claimed.limits.cpu_seconds * 1000, "CPU time"),
            (result.maximum_rss_bytes, claimed.limits.rss_bytes, "RSS"),
            (result.maximum_pids, claimed.limits.pids, "PID count"),
            (result.maximum_disk_bytes, claimed.limits.disk_bytes, "disk use"),
        )
        for observed, maximum, label in observations:
            if type(observed) is not int or observed < 0 or observed > maximum:
                raise RunnerError("resource-limit", f"backend {label} observation exceeds the request limit")
        if result.stdout_bytes > claimed.limits.stdout_bytes or result.stderr_bytes > claimed.limits.stderr_bytes:
            raise RunnerError("provider-output-limit", "backend stream counts exceed request limits")
        gateway = validate_gateway_receipt(
            result.gateway_receipt,
            job_id=claimed.job_id,
            immutable_task_digest_sha256=claimed.immutable_task_digest_sha256,
            input_bundle_sha256=claimed.input_bundle_sha256,
            executable_sha256=claimed.executable_sha256,
            destination_policy_sha256=claimed.destination_policy_sha256,
            credential_handle_sha256=credential_digest,
            accepted_key_id=claimed.gateway_key_id,
            verifier=self.gateway_verifier,
        )
        output_digest = snapshot_bundle_to_directory(
            claimed.output_directory,
            claimed.returned_output_root,
            claimed.limits.output_files,
            claimed.limits.disk_bytes,
        )
        if bundle_digest(claimed.returned_output_root, claimed.limits.output_files, claimed.limits.disk_bytes) != output_digest:
            raise RunnerError("bundle-mutated", "returned output changed before receipt construction")
        unsigned = ProviderReceiptV3(
            schema_version=RECEIPT_VERSION,
            document_type="provider-runner-receipt",
            job_id=claimed.job_id,
            execution_attempt=dict(claimed.execution_attempt),
            immutable_task_digest_sha256=claimed.immutable_task_digest_sha256,
            provider_execution_identity_sha256=claimed.provider_execution_identity_sha256,
            input_bundle_sha256=claimed.input_bundle_sha256,
            output_bundle_sha256=output_digest,
            runner_id=claimed.runner_id,
            runner_identity_sha256=claimed.runner_identity_sha256,
            isolation_policy_sha256=claimed.isolation_policy_sha256,
            external_request_attempted=gateway.external_request_attempted,
            external_request_observed=gateway.external_request_observed,
            gateway_request_id=gateway.gateway_request_id,
            whole_job_quiescent=True,
            exit_code=result.exit_code,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
            executed_executable_sha256=claimed.executable_sha256,
            attestation_type=RECEIPT_ATTESTATION_TYPE,
            attestation_key_id=claimed.receipt_key_id,
            receipt_digest_sha256="0" * 64,
            attestation_signature="A" * 43,
        )
        digest = provider_receipt_digest(unsigned)
        signature = self.receipt_signer.sign(bytes.fromhex(digest), claimed.receipt_key_id)
        receipt = ProviderReceiptV3(**{
            **asdict(unsigned),
            "receipt_digest_sha256": digest,
            "attestation_signature": signature,
        })
        if bundle_digest(claimed.returned_output_root, claimed.limits.output_files, claimed.limits.disk_bytes) != output_digest:
            raise RunnerError("bundle-mutated", "returned output changed after receipt signing")
        return receipt

    def execute(
        self,
        *,
        request_schema_version: str,
        request_document_type: str,
        job_id: str,
        execution_attempt: dict[str, Any],
        immutable_task_digest_sha256: str,
        provider_execution_identity: dict[str, Any],
        provider_execution_identity_sha256: str,
        input_root: Path,
        expected_input_bundle_sha256: str,
        executable_path: Path,
        command: list[str],
        prompt: bytes,
        credential_handle: bytes,
        limits: Limits,
        isolation_policy: dict[str, Any],
        expected_isolation_policy_sha256: str,
        returned_output_root: Path,
    ) -> ProviderReceiptV3:
        claimed = self.begin(
            request_schema_version=request_schema_version,
            request_document_type=request_document_type,
            job_id=job_id,
            execution_attempt=execution_attempt,
            immutable_task_digest_sha256=immutable_task_digest_sha256,
            provider_execution_identity=provider_execution_identity,
            provider_execution_identity_sha256=provider_execution_identity_sha256,
            input_root=input_root,
            expected_input_bundle_sha256=expected_input_bundle_sha256,
            executable_path=executable_path,
            command=command,
            prompt=prompt,
            limits=limits,
            isolation_policy=isolation_policy,
            expected_isolation_policy_sha256=expected_isolation_policy_sha256,
            returned_output_root=returned_output_root,
        )
        return self.execute_claimed(claimed, credential_handle)


def default_limits() -> Limits:
    return Limits(
        wall_time_seconds=5,
        cpu_seconds=3,
        rss_bytes=256 * 1024 * 1024,
        pids=8,
        disk_bytes=1024 * 1024,
        stdout_bytes=4096,
        stderr_bytes=4096,
        output_files=32,
    )


def default_requested_isolation_policy(limits: Limits | None = None) -> dict[str, Any]:
    limits = limits or default_limits()
    return {
        "schema_version": POLICY_VERSION,
        "one_job_per_isolate": True,
        "non_root": True,
        "read_only_base": True,
        "environment_inheritance": "none",
        "credential_delivery": "broker-only",
        "network_policy": "provider-gateway-only",
        "host_home_mounted": False,
        "host_repository_mounted": False,
        "host_ssh_mounted": False,
        "host_cloud_credentials_mounted": False,
        "host_container_socket_mounted": False,
        "whole_job_kill": True,
        "cpu_seconds": limits.cpu_seconds,
        "rss_bytes": limits.rss_bytes,
        "pids": limits.pids,
        "disk_bytes": limits.disk_bytes,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="VibApp one-job provider runner boundary")
    subcommands = parser.add_subparsers(dest="command", required=True)
    preflight = subcommands.add_parser("preflight")
    preflight.add_argument("--state-root", type=Path, required=True)
    synthetic = subcommands.add_parser("synthetic")
    synthetic.add_argument("job_id")
    synthetic.add_argument("input_root", type=Path)
    synthetic.add_argument("--state-root", type=Path, required=True)
    synthetic.add_argument("--mode", choices=sorted(SyntheticLifecycleRunner.MODES), default="success")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "preflight":
            runner = ExternalOneJobProviderRunner(state_root=arguments.state_root)
            runner.preflight(default_requested_isolation_policy())
            raise AssertionError("unreachable")
        report = SyntheticLifecycleRunner(arguments.state_root).run(
            arguments.job_id,
            arguments.input_root,
            default_limits(),
            mode=arguments.mode,
        )
        print(canonical_json(asdict(report)).decode("utf-8"))
        return 0 if report.status == "synthetic-pass" else 2
    except RunnerError as error:
        print(canonical_json({"status": "rejected", "code": error.code, "message": str(error)}).decode("utf-8"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
