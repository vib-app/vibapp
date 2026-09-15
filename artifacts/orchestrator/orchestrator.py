#!/usr/bin/env python3
"""Bounded local VibApp development queue; never invokes live provider mode."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any


SCHEMA_VERSION = "vibapp.local-development-queue.experimental-v1"
RECEIPT_VERSION = "vibapp.local-development-receipt.experimental-v2"
TASK_SCHEMA_VERSION = "vibapp.cloud-codeagent-task.experimental-v3"
MAX_REQUEST_BYTES = 128 * 1024
MAX_QUEUE_FILES = 64
MAX_QUEUE_BYTES = 256 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_RECORD_BYTES = 8 * 1024
MAX_CHILD_OUTPUT_BYTES = 64 * 1024
MAX_CHILD_MEMORY_BYTES = 512 * 1024 * 1024
MAX_CONCURRENCY = 2
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_ID = re.compile(r"^attempt-[0-9]{4}-[0-9a-f]{16}$")
IDEMPOTENCY_KEY = re.compile(
    r"^codeagent-([0-9a-f]{64})-(attempt-[0-9]{4}-[0-9a-f]{16})$"
)
UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DRY_RUN_ADAPTER = Path(__file__).resolve().with_name("dry_run_adapter.py")


class QueueError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QueueError("request-invalid", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json_bytes(raw: bytes, context: str) -> dict[str, Any]:
    if not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise QueueError("request-limit", f"{context} must be 1..{MAX_REQUEST_BYTES} bytes")
    try:
        value = json.loads(raw, object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueueError("request-invalid", f"{context} is not strict JSON: {error}") from error
    if not isinstance(value, dict):
        raise QueueError("request-invalid", f"{context} must be a JSON object")
    return value


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise QueueError("unsafe-path", f"queue path is not a real directory: {path}")
    if metadata.st_uid != os.getuid():
        raise QueueError("unsafe-path", f"queue directory has another owner: {path}")
    path.chmod(0o700)
    return path.resolve(strict=True)


def queue_layout(root: Path) -> dict[str, Path]:
    root = ensure_private_directory(root)
    layout = {"root": root}
    for name in (
        "ready", "processing", "done", "receipts", "quarantine", "attempts",
        "dry-runs", "locks", "slots", "validation", "logs", "runtime-home",
    ):
        layout[name] = ensure_private_directory(root / name)
    return layout


def regular_file(path: Path, maximum: int) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
        raise QueueError("unsafe-path", f"not a private regular file: {path}")
    if metadata.st_uid != os.getuid() or metadata.st_size > maximum:
        raise QueueError("request-limit", f"file ownership/size is invalid: {path}")
    return path.read_bytes()


def atomic_write(path: Path, value: Any, *, exclusive: bool = False) -> None:
    payload = canonical_json(value) + b"\n"
    if len(payload) > MAX_REQUEST_BYTES:
        raise QueueError("request-limit", "persisted record exceeds 128 KiB")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(path.parent)
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def append_event(layout: dict[str, Path], event: str, **fields: Any) -> None:
    record = {"schema_version": SCHEMA_VERSION, "event": event, "at_utc": now_utc(), **fields}
    payload = canonical_json(record) + b"\n"
    if len(payload) > MAX_LOG_RECORD_BYTES:
        raise QueueError("log-limit", "structured log record exceeds 8 KiB")
    path = layout["logs"] / "events.jsonl"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if os.fstat(descriptor).st_size + len(payload) > MAX_LOG_BYTES:
            raise QueueError("log-limit", "structured queue log reached 2 MiB")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def queue_usage(layout: dict[str, Path]) -> tuple[int, int]:
    count = 0
    total = 0
    for name in ("ready", "processing", "done", "receipts", "quarantine", "attempts", "dry-runs"):
        for root, directories, files in os.walk(layout[name], followlinks=False):
            root_path = Path(root)
            for directory in directories:
                metadata = (root_path / directory).lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise QueueError("unsafe-path", "queue contains a symlink directory")
            for filename in files:
                path = root_path / filename
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise QueueError("unsafe-path", "queue contains a non-regular file")
                count += 1
                total += metadata.st_size
    if count >= MAX_QUEUE_FILES or total >= MAX_QUEUE_BYTES:
        raise QueueError("queue-limit", "local queue reached its file or 256 MiB storage limit")
    return count, total


def validate_submission_shape(
    task: dict[str, Any], explicit_submit: bool
) -> tuple[str, str, dict[str, Any], str, str]:
    if explicit_submit is not True:
        raise QueueError("explicit-submit-required", "the user must explicitly submit this completed preview")
    if task.get("schema_version") != TASK_SCHEMA_VERSION or task.get("document_type") != "cloud-codeagent-task":
        raise QueueError("schema-preview-invalid", "unsupported cloud task preview")
    if task.get("need_spec_complete") is not True:
        raise QueueError("need-incomplete", "NeedSpec must be complete before queue submission")
    if task.get("remote_processing_consent") is not True:
        raise QueueError("consent-required", "single-use remote processing consent is required")
    job_id = task.get("job_id")
    digest = task.get("immutable_task_digest_sha256")
    consent = task.get("consent")
    execution_attempt = task.get("execution_attempt")
    provider_identity = task.get("provider_execution_identity")
    if "model" not in task:
        raise QueueError("model-binding-required", "legacy task lacks an immutable CodeAgent model binding")
    model = task.get("model")
    if (
        not isinstance(model, str)
        or not 1 <= len(model) <= 256
        or model.strip() != model
        or model.startswith("-")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in model)
    ):
        raise QueueError("model-binding-required", "task requires an explicit bounded non-blank CodeAgent model")
    if not isinstance(job_id, str) or not IDENTIFIER.fullmatch(job_id):
        raise QueueError("schema-preview-invalid", "job_id is invalid")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise QueueError("schema-preview-invalid", "immutable task digest is invalid")
    if (
        not isinstance(execution_attempt, dict)
        or set(execution_attempt) != {"attempt_id", "ordinal"}
        or not isinstance(execution_attempt.get("attempt_id"), str)
        or not ATTEMPT_ID.fullmatch(execution_attempt["attempt_id"])
        or type(execution_attempt.get("ordinal")) is not int
        or not 1 <= execution_attempt["ordinal"] <= 9999
        or execution_attempt["attempt_id"][8:12]
        != f"{execution_attempt['ordinal']:04d}"
    ):
        raise QueueError(
            "attempt-binding-required",
            "task requires an exact v3 execution attempt ID and matching ordinal",
        )
    identity_digest = (
        provider_identity.get("identity_sha256")
        if isinstance(provider_identity, dict)
        else None
    )
    if not isinstance(identity_digest, str) or not SHA256.fullmatch(identity_digest):
        raise QueueError(
            "provider-identity-binding-required",
            "task requires a digest-bound provider execution identity",
        )
    if not isinstance(consent, dict):
        raise QueueError("consent-required", "consent binding is missing")
    if (
        consent.get("consent_type") != "remote-processing"
        or consent.get("decision") != "granted"
        or consent.get("single_use") is not True
        or consent.get("job_id") != job_id
        or consent.get("attempt_id") != execution_attempt["attempt_id"]
        or consent.get("payload_digest_sha256") != digest
        or consent.get("provider") != task.get("provider")
        or "model" not in consent
        or consent.get("model") != model
        or consent.get("provider_execution_identity_sha256") != identity_digest
    ):
        raise QueueError(
            "consent-required",
            "remote consent is not exact, single-use, and bound to job/attempt/provider/model/request/identity",
        )
    idempotency_key = f"codeagent-{digest}-{execution_attempt['attempt_id']}"
    return job_id, digest, execution_attempt, identity_digest, idempotency_key


def child_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_QUEUE_BYTES, MAX_QUEUE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 11))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (MAX_CHILD_MEMORY_BYTES, MAX_CHILD_MEMORY_BYTES))


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    for chosen_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, chosen_signal)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            continue
    raise QueueError("subprocess-cleanup-failed", "bounded child did not terminate")


def child_rss(pid: int) -> int:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
        return int(result.stdout.strip() or b"0") * 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return MAX_CHILD_MEMORY_BYTES + 1


def run_bounded(command: list[str], *, timeout_seconds: int, home: Path) -> tuple[int, bytes, bytes]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=home,
            env=environment,
            close_fds=True,
            start_new_session=True,
            preexec_fn=child_limits,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise QueueError("subprocess-start-failed", f"cannot start bounded child: {error}") from error
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                kill_process_group(process)
                raise QueueError("subprocess-timeout", f"bounded child exceeded {timeout_seconds}s")
            for key, _ in selector.select(timeout=0.05):
                try:
                    chunk = os.read(key.fileobj.fileno(), 16 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output[key.data].extend(chunk)
                if len(output[key.data]) > MAX_CHILD_OUTPUT_BYTES:
                    kill_process_group(process)
                    raise QueueError("subprocess-output-limit", "bounded child output exceeded 64 KiB")
            if child_rss(process.pid) > MAX_CHILD_MEMORY_BYTES:
                kill_process_group(process)
                raise QueueError("subprocess-memory-limit", "bounded child RSS exceeded 512 MiB")
        remaining = max(0.0, deadline - time.monotonic())
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            kill_process_group(process)
            raise QueueError("subprocess-timeout", f"bounded child exceeded {timeout_seconds}s") from error
        return code, bytes(output["stdout"]), bytes(output["stderr"])
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
        if process.poll() is None:
            kill_process_group(process)


def parse_child_status(raw: bytes, expected: str) -> dict[str, Any]:
    if len(raw.splitlines()) != 1:
        raise QueueError("child-receipt-invalid", "cloud-agent must return exactly one JSON record")
    value = load_json_bytes(raw, "cloud-agent status")
    if value.get("status") != expected:
        raise QueueError("child-receipt-invalid", f"cloud-agent status is not {expected}")
    if value.get("external_request_attempted") is not False or value.get("external_request_observed") is not False:
        raise QueueError("truth-label-invalid", "local orchestration cannot accept external request truth")
    if value.get("gateway_request_id") is not None:
        raise QueueError("truth-label-invalid", "local orchestration cannot accept a gateway request ID")
    return value


def cloud_agent_call(
    layout: dict[str, Path],
    cloud_agent: Path,
    task_path: Path,
    action: str,
    output_root: Path | None = None,
) -> dict[str, Any]:
    configured_cloud_agent = cloud_agent
    cloud_agent = cloud_agent.resolve(strict=False)
    if not cloud_agent.is_file() or configured_cloud_agent.is_symlink() or cloud_agent.is_symlink():
        raise QueueError("cloud-agent-unavailable", "reviewed cloud-agent entrypoint is unavailable")
    command = [sys.executable, str(cloud_agent), action, str(task_path)]
    timeout = 10
    expected = "valid-consented-task"
    if action == "dry-run":
        if output_root is None:
            raise QueueError("internal-error", "dry-run output root is missing")
        adapter = DRY_RUN_ADAPTER.resolve(strict=False)
        if not adapter.is_file() or DRY_RUN_ADAPTER.is_symlink() or adapter.is_symlink():
            raise QueueError("dry-run-adapter-unavailable", "reviewed local dry-run adapter is unavailable")
        command = [
            sys.executable,
            str(adapter),
            "--cloud-agent",
            str(cloud_agent),
            "--task",
            str(task_path),
            "--output-root",
            str(output_root),
        ]
        timeout = 30
        expected = "source-handoff-created"
    code, stdout, stderr = run_bounded(command, timeout_seconds=timeout, home=layout["runtime-home"])
    if code != 0:
        message = stderr.decode("utf-8", "replace")[:1000]
        raise QueueError(f"cloud-agent-{action}-failed", message or f"cloud-agent exited {code}")
    return parse_child_status(stdout, expected)


def task_filename(idempotency_key: str) -> str:
    match = IDEMPOTENCY_KEY.fullmatch(idempotency_key)
    if match is None:
        raise QueueError("request-invalid", "idempotency key is invalid")
    digest, attempt_id = match.groups()
    return f"task-{digest}-{attempt_id}.json"


def idempotency_from_task_filename(filename: str) -> str:
    if not filename.startswith("task-") or not filename.endswith(".json"):
        raise QueueError("queue-integrity-failure", "queue task filename is invalid")
    key = f"codeagent-{filename[5:-5]}"
    if IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise QueueError("queue-integrity-failure", "queue task filename binding is invalid")
    return key


def receipt_path(layout: dict[str, Path], idempotency_key: str) -> Path:
    match = IDEMPOTENCY_KEY.fullmatch(idempotency_key)
    if match is None:
        raise QueueError("request-invalid", "idempotency key is invalid")
    digest, attempt_id = match.groups()
    return layout["receipts"] / f"receipt-{digest}-{attempt_id}.json"


def validate_local_receipt(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "document_type", "queue_id", "job_id", "need_id", "title",
        "idempotency_key", "canonical_task_sha256", "immutable_task_digest_sha256",
        "execution_attempt", "provider_execution_identity_sha256",
        "status", "validation", "dry_run", "external_request_attempted",
        "external_request_observed", "gateway_request_id", "authorities",
        "created_at_utc", "updated_at_utc",
    }
    if set(value) != expected:
        raise QueueError("receipt-integrity-failure", "local receipt fields are not exact")
    digest = value.get("immutable_task_digest_sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise QueueError("receipt-integrity-failure", "receipt immutable digest is invalid")
    execution_attempt = value.get("execution_attempt")
    identity_digest = value.get("provider_execution_identity_sha256")
    if (
        not isinstance(execution_attempt, dict)
        or set(execution_attempt) != {"attempt_id", "ordinal"}
        or not isinstance(execution_attempt.get("attempt_id"), str)
        or not ATTEMPT_ID.fullmatch(execution_attempt["attempt_id"])
        or type(execution_attempt.get("ordinal")) is not int
        or not 1 <= execution_attempt["ordinal"] <= 9999
        or execution_attempt["attempt_id"][8:12]
        != f"{execution_attempt['ordinal']:04d}"
        or not isinstance(identity_digest, str)
        or not SHA256.fullmatch(identity_digest)
    ):
        raise QueueError("receipt-integrity-failure", "receipt attempt/identity binding is invalid")
    expected_key = f"codeagent-{digest}-{execution_attempt['attempt_id']}"
    expected_queue_id = f"local-{sha256_bytes(expected_key.encode())[:32]}"
    if value.get("idempotency_key") != expected_key or value.get("queue_id") != expected_queue_id:
        raise QueueError("receipt-integrity-failure", "receipt queue/idempotency binding is invalid")
    if not isinstance(value.get("job_id"), str) or not IDENTIFIER.fullmatch(value["job_id"]):
        raise QueueError("receipt-integrity-failure", "receipt job ID is invalid")
    if not isinstance(value.get("need_id"), str) or not IDENTIFIER.fullmatch(value["need_id"]):
        raise QueueError("receipt-integrity-failure", "receipt need ID is invalid")
    if not isinstance(value.get("title"), str) or not 1 <= len(value["title"]) <= 80:
        raise QueueError("receipt-integrity-failure", "receipt title is invalid")
    if not isinstance(value.get("canonical_task_sha256"), str) or not SHA256.fullmatch(value["canonical_task_sha256"]):
        raise QueueError("receipt-integrity-failure", "receipt canonical task digest is invalid")
    if value.get("status") not in {"queued-for-codeagent", "waiting-for-external-runner", "dry-run-complete"}:
        raise QueueError("receipt-integrity-failure", "receipt status is invalid")
    if value.get("external_request_attempted") is not False or value.get("external_request_observed") is not False:
        raise QueueError("receipt-integrity-failure", "receipt external truth must remain false")
    if value.get("gateway_request_id") is not None:
        raise QueueError("receipt-integrity-failure", "local receipt cannot contain a gateway request ID")
    validation = value.get("validation")
    if validation != {
        "status": "valid-consented-task",
        "external_request_attempted": False,
        "external_request_observed": False,
    }:
        raise QueueError("receipt-integrity-failure", "receipt validation evidence is invalid")
    dry_run = value.get("dry_run")
    if value["status"] in {"queued-for-codeagent", "waiting-for-external-runner"} and dry_run is not None:
        raise QueueError("receipt-integrity-failure", "queued receipt cannot claim a dry-run")
    if value["status"] == "dry-run-complete":
        if not isinstance(dry_run, dict) or set(dry_run) != {
            "status", "handoff_relative_path", "handoff_sha256",
            "external_request_attempted", "external_request_observed",
        }:
            raise QueueError("receipt-integrity-failure", "dry-run receipt fields are invalid")
        if (
            dry_run.get("status") != "source-handoff-created"
            or dry_run.get("external_request_attempted") is not False
            or dry_run.get("external_request_observed") is not False
            or not isinstance(dry_run.get("handoff_sha256"), str)
            or not SHA256.fullmatch(dry_run["handoff_sha256"])
            or not isinstance(dry_run.get("handoff_relative_path"), str)
            or not 1 <= len(dry_run["handoff_relative_path"]) <= 512
        ):
            raise QueueError("receipt-integrity-failure", "dry-run receipt evidence is invalid")
    if value.get("authorities") != {
        "registry_embedding": "separate-prior-decision",
        "remote_processing": "granted-single-use-not-consumed-by-local-dry-run",
        "public_publication": "separate-not-performed",
        "builder": "not-invoked",
    }:
        raise QueueError("receipt-integrity-failure", "receipt authority separation is invalid")
    if any(not isinstance(value.get(field), str) or not UTC.fullmatch(value[field]) for field in ("created_at_utc", "updated_at_utc")):
        raise QueueError("receipt-integrity-failure", "receipt timestamp is invalid")
    return value


def load_receipt(layout: dict[str, Path], idempotency_key: str) -> dict[str, Any] | None:
    path = receipt_path(layout, idempotency_key)
    if not path.exists():
        return None
    return validate_local_receipt(load_json_bytes(regular_file(path, MAX_REQUEST_BYTES), "queue receipt"))


def acquire_named_lock(layout: dict[str, Path], name: str, wait_seconds: float = 2.0) -> Path:
    path = layout["locks"] / f"{name}.lock"
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            return path
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 30:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.02)
    raise QueueError("queue-busy", "another process owns the queue item lock")


def acquire_slot(layout: dict[str, Path]) -> Path:
    for index in range(MAX_CONCURRENCY):
        path = layout["slots"] / f"worker-{index}.lock"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            return path
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 60:
                    path.unlink()
                    return acquire_slot(layout)
            except FileNotFoundError:
                return acquire_slot(layout)
    raise QueueError("concurrency-limit", f"at most {MAX_CONCURRENCY} local workers may run")


def slot_owner_active(path: Path) -> bool:
    try:
        raw = regular_file(path, 64).decode("ascii").strip()
        pid = int(raw)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (QueueError, UnicodeDecodeError, ValueError, PermissionError):
        return True


def release_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def quarantine_rejection(layout: dict[str, Path], code: str, raw: bytes) -> None:
    digest = sha256_bytes(raw)
    record = {
        "schema_version": SCHEMA_VERSION,
        "document_type": "local-development-quarantine-record",
        "decision": "rejected",
        "reason_code": code,
        "input_sha256": digest,
        "input_bytes": len(raw),
        "external_request_attempted": False,
        "external_request_observed": False,
        "created_at_utc": now_utc(),
    }
    atomic_write(layout["quarantine"] / f"rejected-{digest}.json", record)


def submit(
    root: Path,
    cloud_agent: Path,
    raw: bytes,
    *,
    explicit_submit: bool,
) -> dict[str, Any]:
    layout = queue_layout(root)
    queue_usage(layout)
    try:
        task = load_json_bytes(raw, "task preview")
        (
            job_id,
            immutable_digest,
            execution_attempt,
            provider_identity_digest,
            idempotency_key,
        ) = validate_submission_shape(task, explicit_submit)
    except QueueError as error:
        quarantine_rejection(layout, error.code, raw[:MAX_REQUEST_BYTES])
        append_event(layout, "submission-rejected", reason_code=error.code, input_sha256=sha256_bytes(raw))
        raise
    canonical = canonical_json(task)
    canonical_digest = sha256_bytes(canonical)
    lock = acquire_named_lock(layout, f"submit-{immutable_digest}")
    validation_path = (
        layout["validation"]
        / f"validate-{immutable_digest}-{execution_attempt['attempt_id']}-{os.getpid()}.json"
    )
    try:
        existing = load_receipt(layout, idempotency_key)
        if existing is not None:
            return {"duplicate": True, "receipt": existing}
        atomic_write(validation_path, task, exclusive=True)
        validation = cloud_agent_call(layout, cloud_agent, validation_path, "validate")
        filename = task_filename(idempotency_key)
        ready = layout["ready"] / filename
        atomic_write(ready, task, exclusive=True)
        receipt = {
            "schema_version": RECEIPT_VERSION,
            "document_type": "local-development-receipt",
            "queue_id": f"local-{sha256_bytes(idempotency_key.encode())[:32]}",
            "job_id": job_id,
            "need_id": task.get("need_spec", {}).get("need_id"),
            "title": task.get("package_intent", {}).get("display_name"),
            "idempotency_key": idempotency_key,
            "canonical_task_sha256": canonical_digest,
            "immutable_task_digest_sha256": immutable_digest,
            "execution_attempt": execution_attempt,
            "provider_execution_identity_sha256": provider_identity_digest,
            "status": "queued-for-codeagent",
            "validation": {
                "status": validation["status"],
                "external_request_attempted": False,
                "external_request_observed": False,
            },
            "dry_run": None,
            "external_request_attempted": False,
            "external_request_observed": False,
            "gateway_request_id": None,
            "authorities": {
                "registry_embedding": "separate-prior-decision",
                "remote_processing": "granted-single-use-not-consumed-by-local-dry-run",
                "public_publication": "separate-not-performed",
                "builder": "not-invoked",
            },
            "created_at_utc": now_utc(),
            "updated_at_utc": now_utc(),
        }
        try:
            atomic_write(receipt_path(layout, idempotency_key), receipt, exclusive=True)
        except Exception:
            ready.unlink(missing_ok=True)
            raise
        append_event(
            layout,
            "task-enqueued",
            queue_id=receipt["queue_id"],
            job_id=job_id,
            idempotency_key=idempotency_key,
            external_request_attempted=False,
            external_request_observed=False,
        )
        return {"duplicate": False, "receipt": receipt}
    finally:
        release_file(validation_path)
        release_file(lock)


def recover(root: Path) -> dict[str, int]:
    layout = queue_layout(root)
    restored = 0
    quarantined = 0
    for slot in sorted(layout["slots"].glob("worker-*.lock")):
        if slot_owner_active(slot):
            return {"restored": 0, "quarantined": 0}
        release_file(slot)
    for path in sorted(layout["processing"].glob("task-*.json")):
        destination = layout["ready"] / path.name
        if destination.exists():
            os.replace(path, layout["quarantine"] / f"duplicate-{uuid.uuid4().hex}-{path.name}")
            quarantined += 1
        else:
            os.replace(path, destination)
            restored += 1
    for path in sorted(layout["attempts"].iterdir()):
        if path.name.startswith("attempt-"):
            os.replace(path, layout["quarantine"] / f"recovered-{uuid.uuid4().hex}-{path.name}")
            quarantined += 1
    if restored or quarantined:
        append_event(layout, "crash-recovery", restored=restored, quarantined=quarantined)
    return {"restored": restored, "quarantined": quarantined}


def quarantine_processing(layout: dict[str, Path], processing: Path, code: str) -> None:
    try:
        idempotency_key = idempotency_from_task_filename(processing.name)
    except QueueError:
        idempotency_key = None
    if processing.exists():
        destination = layout["quarantine"] / f"{code}-{uuid.uuid4().hex}-{processing.name}"
        os.replace(processing, destination)
    if idempotency_key is not None:
        receipt = receipt_path(layout, idempotency_key)
        if receipt.exists():
            os.replace(
                receipt,
                layout["quarantine"] / f"{code}-{uuid.uuid4().hex}-{receipt.name}",
            )
    append_event(layout, "task-quarantined", task_name=processing.name, reason_code=code)


def process_one(root: Path, cloud_agent: Path, idempotency_key: str | None = None) -> dict[str, Any]:
    layout = queue_layout(root)
    recover(root)
    queue_usage(layout)
    slot = acquire_slot(layout)
    processing: Path | None = None
    validation_path: Path | None = None
    attempt: Path | None = None
    try:
        if idempotency_key is None:
            candidates = sorted(layout["ready"].glob("task-*.json"))
            if not candidates:
                raise QueueError("queue-empty", "no local development task is waiting")
            ready = candidates[0]
            idempotency_key = idempotency_from_task_filename(ready.name)
        else:
            ready = layout["ready"] / task_filename(idempotency_key)
        existing = load_receipt(layout, idempotency_key)
        if existing is not None and existing.get("status") == "dry-run-complete":
            return existing
        processing = layout["processing"] / ready.name
        try:
            os.replace(ready, processing)
        except FileNotFoundError as error:
            raise QueueError("queue-item-unavailable", "requested queue item is not ready") from error
        task = load_json_bytes(regular_file(processing, MAX_REQUEST_BYTES), "queued task")
        (
            job_id,
            immutable_digest,
            execution_attempt,
            provider_identity_digest,
            expected_key,
        ) = validate_submission_shape(task, True)
        if expected_key != idempotency_key:
            raise QueueError("queue-integrity-failure", "queued task does not match its idempotency key")
        canonical_digest = sha256_bytes(canonical_json(task))
        if existing is None or existing.get("canonical_task_sha256") != canonical_digest:
            raise QueueError("queue-integrity-failure", "queued task does not match its receipt")
        validation_path = (
            layout["validation"]
            / f"process-{immutable_digest}-{execution_attempt['attempt_id']}-{os.getpid()}.json"
        )
        atomic_write(validation_path, task, exclusive=True)
        validation = cloud_agent_call(layout, cloud_agent, validation_path, "validate")
        attempt = (
            layout["attempts"]
            / f"{execution_attempt['attempt_id']}-{immutable_digest}-{uuid.uuid4().hex}"
        )
        attempt.mkdir(mode=0o700)
        dry_status = cloud_agent_call(layout, cloud_agent, validation_path, "dry-run", attempt)
        handoff = dry_status.get("handoff")
        if not isinstance(handoff, str):
            raise QueueError("child-receipt-invalid", "dry-run handoff path is missing")
        handoff_path = Path(handoff).resolve(strict=True)
        attempt_root = attempt.resolve(strict=True)
        try:
            handoff_path.relative_to(attempt_root)
        except ValueError as error:
            raise QueueError("child-receipt-invalid", "dry-run handoff escaped its attempt root") from error
        if handoff_path.is_symlink() or not handoff_path.is_file():
            raise QueueError("child-receipt-invalid", "dry-run handoff is not a regular file")
        final_run = layout["dry-runs"] / f"run-{immutable_digest}-{execution_attempt['attempt_id']}"
        if final_run.exists():
            raise QueueError("queue-integrity-failure", "dry-run destination already exists")
        os.replace(attempt, final_run)
        attempt = None
        final_handoff = final_run / handoff_path.relative_to(attempt_root)
        handoff_digest = sha256_bytes(regular_file(final_handoff, MAX_REQUEST_BYTES))
        receipt = {
            **existing,
            "status": "dry-run-complete",
            "validation": {
                "status": validation["status"],
                "external_request_attempted": False,
                "external_request_observed": False,
            },
            "dry_run": {
                "status": dry_status["status"],
                "handoff_relative_path": final_handoff.relative_to(layout["root"]).as_posix(),
                "handoff_sha256": handoff_digest,
                "external_request_attempted": False,
                "external_request_observed": False,
            },
            "external_request_attempted": False,
            "external_request_observed": False,
            "gateway_request_id": None,
            "updated_at_utc": now_utc(),
        }
        if (
            receipt.get("execution_attempt") != execution_attempt
            or receipt.get("provider_execution_identity_sha256") != provider_identity_digest
        ):
            raise QueueError(
                "queue-integrity-failure",
                "queued receipt changed its attempt or provider execution identity",
            )
        atomic_write(receipt_path(layout, idempotency_key), receipt)
        os.replace(processing, layout["done"] / processing.name)
        processing = None
        append_event(
            layout,
            "dry-run-complete",
            queue_id=receipt["queue_id"],
            job_id=job_id,
            idempotency_key=idempotency_key,
            external_request_attempted=False,
            external_request_observed=False,
        )
        return receipt
    except QueueError as error:
        if processing is not None:
            quarantine_processing(layout, processing, error.code)
            processing = None
        raise
    finally:
        if validation_path is not None:
            release_file(validation_path)
        if attempt is not None and attempt.exists():
            os.replace(attempt, layout["quarantine"] / f"failed-{uuid.uuid4().hex}-{attempt.name}")
        release_file(slot)


def read_task_argument(path: str) -> bytes:
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise QueueError("request-limit", "stdin task exceeds 128 KiB")
        return raw
    return regular_file(Path(path), MAX_REQUEST_BYTES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VibApp bounded local development queue")
    subcommands = parser.add_subparsers(dest="command", required=True)
    submit_parser = subcommands.add_parser("submit")
    submit_parser.add_argument("--root", type=Path, required=True)
    submit_parser.add_argument("--cloud-agent", type=Path, required=True)
    submit_parser.add_argument("--task", required=True)
    submit_parser.add_argument("--explicit-submit", action="store_true")
    submit_parser.add_argument("--process-dry-run", action="store_true")
    process_parser = subcommands.add_parser("process-one")
    process_parser.add_argument("--root", type=Path, required=True)
    process_parser.add_argument("--cloud-agent", type=Path, required=True)
    process_parser.add_argument("--idempotency-key")
    recover_parser = subcommands.add_parser("recover")
    recover_parser.add_argument("--root", type=Path, required=True)
    status_parser = subcommands.add_parser("status")
    status_parser.add_argument("--root", type=Path, required=True)
    status_parser.add_argument("--idempotency-key", required=True)
    return parser


def json_output(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "submit":
            result = submit(
                args.root,
                args.cloud_agent,
                read_task_argument(args.task),
                explicit_submit=args.explicit_submit,
            )
            if args.process_dry_run:
                result["receipt"] = process_one(
                    args.root,
                    args.cloud_agent,
                    result["receipt"]["idempotency_key"],
                )
            json_output({"ok": True, **result})
        elif args.command == "process-one":
            json_output({"ok": True, "receipt": process_one(args.root, args.cloud_agent, args.idempotency_key)})
        elif args.command == "recover":
            json_output({"ok": True, "recovery": recover(args.root)})
        elif args.command == "status":
            layout = queue_layout(args.root)
            receipt = load_receipt(layout, args.idempotency_key)
            if receipt is None:
                raise QueueError("not-found", "queue receipt does not exist")
            json_output({"ok": True, "receipt": receipt})
        return 0
    except QueueError as error:
        json_output({
            "ok": False,
            "code": error.code,
            "message": str(error),
            "external_request_attempted": False,
            "external_request_observed": False,
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
