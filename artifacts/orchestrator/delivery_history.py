"""Strict, read-only decoding of historical v1 delivery records.

This module has no provider, builder, execution, migration or write dependency.
Legacy data remains historical evidence, never renewed execution authority.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

TASK_VERSION = "vibapp.delivery-task.experimental-v1"
ATTEMPT_VERSION = "vibapp.delivery-attempt.experimental-v1"
HISTORY_VERSION = "vibapp.delivery-controller.experimental-v2"
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_HISTORY_BYTES = 512 * 1024
MAX_ATTEMPTS = 64
TASK_ID = re.compile(r"^development-[0-9a-f]{32}$")
ATTEMPT_ID = re.compile(r"^attempt-[0-9]{4}-[0-9a-f]{16}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LEGACY_TASK_KEYS = {
    "schema_version", "document_type", "task_id", "need_id", "attempt_count",
    "current_attempt_id", "attempt_ids", "created_at_utc", "updated_at_utc",
}
LEGACY_ATTEMPT_KEYS = {
    "schema_version", "document_type", "task_id", "attempt_id", "attempt_number",
    "retry_of_attempt_id", "need_id", "title", "need_spec_revision", "job_id",
    "registry_request_id", "registry_evidence_sha256", "canonical_task_sha256",
    "immutable_task_digest_sha256", "status", "stage", "progress_percent", "error",
    "outputs", "event_count", "created_at_utc", "updated_at_utc",
}


class HistoryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _matches(value: Any, pattern: re.Pattern) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _utc(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") == value
    except ValueError:
        return False


def validate_legacy_task(value: dict) -> dict:
    ids = value.get("attempt_ids")
    if (set(value) != LEGACY_TASK_KEYS or value.get("schema_version") != TASK_VERSION
            or value.get("document_type") != "delivery-task"
            or not _matches(value.get("task_id"), TASK_ID) or not _matches(value.get("need_id"), IDENTIFIER)
            or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ATTEMPTS
            or any(not _matches(item, ATTEMPT_ID) for item in ids) or len(set(ids)) != len(ids)
            or type(value.get("attempt_count")) is not int or value["attempt_count"] != len(ids)
            or value.get("current_attempt_id") != ids[-1]
            or not _utc(value.get("created_at_utc")) or not _utc(value.get("updated_at_utc"))):
        raise HistoryError("legacy-task-binding-invalid")
    return value


def validate_legacy_attempt(value: dict) -> dict:
    if (set(value) != LEGACY_ATTEMPT_KEYS or value.get("schema_version") != ATTEMPT_VERSION
            or value.get("document_type") != "delivery-attempt"
            or not _matches(value.get("task_id"), TASK_ID) or not _matches(value.get("attempt_id"), ATTEMPT_ID)
            or type(value.get("attempt_number")) is not int or not 1 <= value["attempt_number"] <= MAX_ATTEMPTS
            or (value.get("retry_of_attempt_id") is not None and not _matches(value["retry_of_attempt_id"], ATTEMPT_ID))
            or not _matches(value.get("need_id"), IDENTIFIER) or not _matches(value.get("job_id"), IDENTIFIER)
            or not isinstance(value.get("title"), str) or not 1 <= len(value["title"]) <= 80
            or type(value.get("need_spec_revision")) is not int or value["need_spec_revision"] < 1
            or not isinstance(value.get("registry_request_id"), str) or not 1 <= len(value["registry_request_id"]) <= 128
            or any(not _matches(value.get(key), SHA256) for key in (
                "registry_evidence_sha256", "canonical_task_sha256", "immutable_task_digest_sha256"))
            or value.get("status") not in {"queued", "running", "failed", "private-appstore-ready"}
            or not isinstance(value.get("stage"), str) or not 1 <= len(value["stage"]) <= 80
            or type(value.get("progress_percent")) is not int or not 0 <= value["progress_percent"] <= 100
            or (value.get("error") is not None and not isinstance(value["error"], dict))
            or not isinstance(value.get("outputs"), dict)
            or type(value.get("event_count")) is not int or value["event_count"] < 0
            or not _utc(value.get("created_at_utc")) or not _utc(value.get("updated_at_utc"))):
        raise HistoryError("legacy-attempt-binding-invalid")
    return value


def _directory(path: Path) -> None:
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022):
        raise HistoryError("legacy-history-unsafe-directory")


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise HistoryError("legacy-history-duplicate-json-key")
        value[key] = item
    return value


def _invalid_constant(_):
    raise HistoryError("legacy-history-invalid-json-number")


def _read(path: Path) -> tuple[dict, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or metadata.st_mode & 0o022
                or not 1 <= metadata.st_size <= MAX_DOCUMENT_BYTES):
            raise HistoryError("legacy-history-unsafe-document")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise HistoryError("legacy-history-document-limit")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise HistoryError("legacy-history-invalid-document")
        return value, len(raw)
    finally:
        os.close(descriptor)


def read_legacy_history(controller_root: Path, task_id: str) -> dict | None:
    """Return original v1 documents, or None to keep modern parsing unchanged.

    Every read is bounded and nonmutating. This never creates/chmods a directory,
    touches run/consent locks, repairs an index or reconciles a running worker.
    """
    if not _matches(task_id, TASK_ID):
        raise HistoryError("invalid-task-id")
    root = Path(controller_root)
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise HistoryError("legacy-history-unsafe-directory")
    directory = root / "tasks" / task_id
    for path in (root, root / "tasks", directory):
        _directory(path)
    task, total_bytes = _read(directory / "task.json")
    if task.get("schema_version") != TASK_VERSION:
        return None
    validate_legacy_task(task)
    if task["task_id"] != task_id:
        raise HistoryError("legacy-task-binding-invalid")
    attempts_root = directory / "attempts"
    _directory(attempts_root)
    observed = []
    with os.scandir(attempts_root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_ATTEMPTS or not _matches(entry.name, ATTEMPT_ID) or not entry.is_dir(follow_symlinks=False):
                raise HistoryError("legacy-attempt-inventory-invalid")
            observed.append(entry.name)
    if set(observed) != set(task["attempt_ids"]):
        raise HistoryError("legacy-attempt-index-stale")
    attempts = []
    for index, attempt_id in enumerate(task["attempt_ids"]):
        attempt_root = attempts_root / attempt_id
        _directory(attempt_root)
        attempt, size = _read(attempt_root / "attempt.json")
        total_bytes += size
        if total_bytes > MAX_HISTORY_BYTES:
            raise HistoryError("legacy-history-size-limit")
        validate_legacy_attempt(attempt)
        if (attempt["task_id"] != task_id or attempt["need_id"] != task["need_id"]
                or attempt["attempt_id"] != attempt_id or attempt["attempt_number"] != index + 1
                or attempt["retry_of_attempt_id"] != (task["attempt_ids"][index - 1] if index else None)):
            raise HistoryError("legacy-attempt-binding-invalid")
        attempts.append(attempt)
    result = {"schema_version": HISTORY_VERSION, "task": task, "attempts": attempts, "legacy_read_only": True}
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) + 128 > MAX_HISTORY_BYTES:
        raise HistoryError("legacy-history-size-limit")
    return result
