#!/usr/bin/env python3
"""Recoverably hide terminal failed delivery tasks without deleting their ledgers.

The authoritative task tree is deliberately never moved: its paths bind source
provenance, consumed consent and same-task deduplication. A private metadata
snapshot and an exact-record-bound marker change list visibility only. Changed
records (including a legitimate new attempt) automatically invalidate the marker.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from delivery_controller import (  # noqa: E402
    ATTEMPT_ID, IDENTIFIER, MAX_ATTEMPTS, TASK_ID, _validate_attempt_record, _validate_task_record,
)
from delivery_history import (  # noqa: E402
    HistoryError, LEGACY_TASK_KEYS, validate_legacy_attempt, validate_legacy_task,
)

SCHEMA = "vibapp.delivery-task-archive-v1"
SNAPSHOT_SCHEMA = "vibapp.delivery-task-archive-snapshot-v1"
REASON = "user-requested-problem-task-reset"
DOCUMENT_LIMIT = 512 * 1024
SNAPSHOT_LIMIT = 4 * 1024 * 1024
BATCH_LIMIT = 16 * 1024 * 1024
MARKER_LIMIT = 16 * 1024
TARGET_LIMIT = 64
TASK_LIMIT = 512
SHA = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_ID = re.compile(r"^archive-[0-9a-f]{32}$")
MARKER_KEYS = {
    "schema_version", "task_id", "immutable_task_digest_sha256", "current_attempt_id",
    "task_record_sha256", "attempt_records", "archive_id", "snapshot_sha256",
    "archived_at_utc", "reason",
}


class ArchiveError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError("duplicate-json-key")
        result[key] = value
    return result


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _retain(records: dict, name: str, document: dict, raw: bytes) -> None:
    # Bound accumulation before loading an entire 64-attempt history into memory.
    if sum(item["size_bytes"] for item in records.values()) + len(raw) > SNAPSHOT_LIMIT:
        raise ArchiveError("snapshot-limit")
    records[name] = {"sha256": _digest(raw), "size_bytes": len(raw), "document": document}


def _directory(path: Path, *, private: bool = False) -> None:
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022 or (private and metadata.st_mode & 0o077)):
        raise ArchiveError("unsafe-directory")


def _read(path: Path, *, limit: int = DOCUMENT_LIMIT, private: bool = True) -> tuple[dict, bytes]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or metadata.st_mode & (0o077 if private else 0o022)
                or not 1 <= metadata.st_size <= limit):
            raise ArchiveError("unsafe-document")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ArchiveError("document-limit")
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ArchiveError("invalid-json-number")))
        if not isinstance(value, dict):
            raise ArchiveError("invalid-document")
        return value, data
    finally:
        os.close(descriptor)


def _validate_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ArchiveError("path-outside-root") from error
    cursor = root
    _directory(cursor, private=True)
    for part in relative.parts[:-1]:
        cursor /= part
        _directory(cursor)


def _read_in(root: Path, path: Path, *, limit=DOCUMENT_LIMIT, private=True):
    _validate_path(root, path)
    return _read(path, limit=limit, private=private)


def _optional(root: Path, path: Path, *, private=True):
    try:
        return _read_in(root, path, private=private)
    except FileNotFoundError:
        return None


def _utc(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") == value
    except ValueError:
        return False


def _task_document(value: dict) -> bool:
    """Validate old documents in place; never migrate legacy retry/consent ledgers."""
    legacy = value.get("schema_version") == "vibapp.delivery-task.experimental-v1"
    if not legacy:
        _validate_task_record(value)
        return False
    try:
        validate_legacy_task(value)
    except HistoryError as error:
        raise ArchiveError(error.code) from error
    return True


def _attempt_document(value: dict, legacy: bool) -> None:
    if not legacy:
        _validate_attempt_record(value)
        return
    try:
        validate_legacy_attempt(value)
    except HistoryError as error:
        raise ArchiveError(error.code) from error


@contextmanager
def _lock(root: Path, path: Path):
    """Never create/unlink a lock while inspecting existing task lifecycle."""
    _validate_path(root, path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or metadata.st_mode & 0o077):
            raise ArchiveError("unsafe-lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ArchiveError("task-active") from error
        yield
    finally:
        os.close(descriptor)


def _exclusive_json(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TaskArchive:
    def __init__(self, data_root: Path, *, allowed_roots: tuple[Path, ...] | None = None):
        # CLI roots are intentionally explicit; embedded product callers/tests may
        # supply their independently authenticated root, never a task-supplied path.
        if allowed_roots is None:
            allowed_roots = (
                Path.home() / "Library/Application Support/ai.vibapp.launcher",
                Path(__file__).resolve().parents[1] / "product-platform/website/output/local-product-data",
            )
        self.data_root = Path(data_root)
        if (not self.data_root.is_absolute() or ".." in self.data_root.parts
                or self.data_root != self.data_root.resolve(strict=True)
                or self.data_root not in allowed_roots):
            raise ArchiveError("unrecognized-data-root")
        _directory(self.data_root, private=True)
        self.root = self.data_root / "delivery-controller"
        _directory(self.root, private=True)
        _directory(self.root / "tasks", private=True)

    def _ids(self, task_ids: list[str] | None) -> list[str]:
        if task_ids is not None:
            if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= TARGET_LIMIT
                    or any(not isinstance(item, str) or not TASK_ID.fullmatch(item) for item in task_ids)
                    or len(set(task_ids)) != len(task_ids)):
                raise ArchiveError("invalid-explicit-targets")
            return sorted(task_ids)
        ids = []
        with os.scandir(self.root / "tasks") as entries:
            for index, entry in enumerate(entries):
                if index >= TASK_LIMIT:
                    raise ArchiveError("task-inventory-limit")
                if TASK_ID.fullmatch(entry.name):
                    ids.append(entry.name)
        return sorted(ids)

    def _records(self, task_id: str, stack: ExitStack | None = None) -> tuple[dict, list[dict], dict]:
        directory = self.root / "tasks" / task_id
        _directory(directory, private=True)
        task, raw = _read_in(self.root, directory / "task.json")
        legacy = _task_document(task)
        if task["task_id"] != task_id or not task["attempt_ids"]:
            raise ArchiveError("task-binding-invalid")
        attempts_directory = directory / "attempts"
        _directory(attempts_directory, private=True)
        observed = []
        with os.scandir(attempts_directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ATTEMPTS or not ATTEMPT_ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                    raise ArchiveError("attempt-inventory-invalid")
                observed.append(entry.name)
        if set(observed) != set(task["attempt_ids"]):
            raise ArchiveError("attempt-index-stale")
        records = {}
        _retain(records, "task.json", task, raw)
        attempts = []
        for index, attempt_id in enumerate(task["attempt_ids"]):
            attempt_dir = attempts_directory / attempt_id
            _directory(attempt_dir, private=True)
            if stack is not None:
                stack.enter_context(_lock(self.root, attempt_dir / ".run.lock"))
            attempt, raw = _read_in(self.root, attempt_dir / "attempt.json")
            _attempt_document(attempt, legacy)
            binding_keys = ("task_id", "need_id") if legacy else (
                "task_id", "need_id", "need_spec_revision", "job_id",
                "immutable_task_digest_sha256", "provider_execution_identity_sha256")
            if (attempt["attempt_id"] != attempt_id or attempt["attempt_number"] != index + 1
                    or attempt["retry_of_attempt_id"] != (task["attempt_ids"][index - 1] if index else None)
                    or any(attempt.get(key) != task.get(key) for key in binding_keys)):
                raise ArchiveError("attempt-binding-invalid")
            _retain(records, f"attempts/{attempt_id}/attempt.json", attempt, raw)
            attempts.append(attempt)
        if legacy:
            # Legacy tasks may contain edited revisions. The marker still binds
            # every original byte and current attempt, not a rewritten v2 ledger.
            task = {**task, "job_id": attempts[-1]["job_id"],
                    "immutable_task_digest_sha256": attempts[-1]["immutable_task_digest_sha256"]}
        return task, attempts, records

    def _quiescent(self, attempt: dict, records: dict, stack: ExitStack) -> None:
        task_id, attempt_id = attempt["task_id"], attempt["attempt_id"]
        attempt_dir = self.root / "tasks" / task_id / "attempts" / attempt_id
        output_root = attempt_dir / "codeagent/output"
        execution_lock = output_root / ".execution.lock"
        # The adapter can acquire its lock before writing status.json. Always
        # check that lock, including a dangling link, independently of status.
        if execution_lock.exists() or execution_lock.is_symlink():
            stack.enter_context(_lock(self.root, execution_lock))
        # This is visibility-only archive, not workload deletion or cleanup.
        # All authoritative run locks are held; terminal records remain in place.
        # Historical missing observations are retained as missing, never invented.
        worker_path = self.root / "desktop-workers" / f"{task_id}-{attempt_id}.json"
        worker_item = _optional(self.root, worker_path)
        if worker_item is not None:
            worker, raw = worker_item
            if (worker.get("schema_version") != "vibapp.desktop-delivery-worker.experimental-v2"
                    or any(worker.get(key) != attempt[key] for key in ("task_id", "attempt_id", "immutable_task_digest_sha256"))
                    or worker.get("status") not in {"finished", "failed", "cancelled"}
                    or worker.get("terminal") is False or worker.get("cleanup_confirmed") is False):
                raise ArchiveError("worker-cleanup-unproven")
            _retain(records, f"desktop-workers/{worker_path.name}", worker, raw)
        status_path = attempt_dir / "codeagent/status.json"
        status_item = _optional(self.root, status_path)
        if status_item is not None:
            status_document, raw = status_item
            if (status_document.get("schema_version") != "vibapp.codeagent-adapter-status.experimental-v2"
                    or status_document.get("status") not in {"codeagent-failed", "codeagent-cancelled", "source-ready"}
                    or any(status_document.get(key) != attempt[key] for key in (
                        "job_id", "need_id", "immutable_task_digest_sha256"))
                    or any(key in attempt and status_document.get(key) != attempt[key] for key in (
                        "provider_execution_identity_sha256", "consent_id"))):
                raise ArchiveError("codeagent-state-unproven")
            _retain(records, f"attempts/{attempt_id}/codeagent/status.json", status_document, raw)
            terminal_path = output_root / "executions" / attempt_id / "docker-terminal.json"
            # Older executor output is 0644 under this task's 0700 parent.
            # Evidence must still be owned, single-link and never writable by others.
            terminal_item = _optional(self.root, terminal_path, private=False)
            if terminal_item is not None:
                terminal, raw = terminal_item
                if (terminal.get("state") not in {"failed", "cancelled", "succeeded"}
                        or terminal.get("cleanup_confirmed") is not True
                        or not isinstance(terminal.get("backend_execution_id"), str)
                        or not re.fullmatch(r"vibapp-[0-9a-f]{64}", terminal["backend_execution_id"])
                        or (status_document.get("gateway_request_id") is not None
                            and terminal["backend_execution_id"] != status_document["gateway_request_id"])):
                    raise ArchiveError("docker-cleanup-unproven")
                # failure_diagnostic.container_running is a pre-cleanup snapshot;
                # the later cleanup_confirmed result is authoritative here.
                _retain(records, f"attempts/{attempt_id}/codeagent/output/executions/{attempt_id}/docker-terminal.json", terminal, raw)

    def _eligible(self, task_id: str, stack: ExitStack):
        task, attempts, records = self._records(task_id, stack)
        for attempt in attempts:
            if attempt["status"] != "failed":
                raise ArchiveError("task-not-terminal-failed")
            if any(key in attempt["outputs"] for key in ("app_id", "appstore_candidate_path", "appstore_created")):
                raise ArchiveError("task-has-appstore-output")
            self._quiescent(attempt, records, stack)
        return task, attempts, records

    def _archived_row(self, task_id: str) -> dict | None:
        """Fail visible: malformed/stale markers never hide an active or new task."""
        try:
            if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
                return None
            with ExitStack() as stack:
                stack.enter_context(_lock(self.root, self.root / ".delivery-controller.lock"))
                validated = self._eligible(task_id, stack)
                return self._marker_row(task_id, validated)
        except (ArchiveError, ValueError, OSError, KeyError, TypeError):
            return None

    def _marker_row(self, task_id: str, validated: tuple) -> dict | None:
        """Called only while the current eligibility snapshot's locks are held."""
        try:
            if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
                return None
            task, attempts, records = validated
            marker, _ = _read_in(self.root, self.root / "tasks" / task_id / ".archived.json", limit=MARKER_LIMIT)
            if (set(marker) != MARKER_KEYS or marker["schema_version"] != SCHEMA or marker["reason"] != REASON
                    or marker["task_id"] != task_id or not _utc(marker["archived_at_utc"])
                    or not isinstance(marker["archive_id"], str) or not ARCHIVE_ID.fullmatch(marker["archive_id"])
                    or not isinstance(marker["snapshot_sha256"], str) or not SHA.fullmatch(marker["snapshot_sha256"])
                    or marker["immutable_task_digest_sha256"] != task["immutable_task_digest_sha256"]
                    or marker["current_attempt_id"] != task["current_attempt_id"]
                    or marker["task_record_sha256"] != records["task.json"]["sha256"]
                    or marker["attempt_records"] != [
                        {"attempt_id": attempt["attempt_id"], "sha256": records[f"attempts/{attempt['attempt_id']}/attempt.json"]["sha256"]}
                        for attempt in attempts]
                    or any(attempt["status"] != "failed" or any(key in attempt["outputs"] for key in (
                        "app_id", "appstore_candidate_path", "appstore_created")) for attempt in attempts)):
                return None
            _, raw = _read_in(self.root, self.root / "task-archives" / marker["archive_id"] / "snapshot.json", limit=SNAPSHOT_LIMIT)
            return {"task_id": task_id, "job_id": task["job_id"],
                    "immutable_task_digest_sha256": attempts[-1]["immutable_task_digest_sha256"],
                    "provider_execution_identity_sha256": attempts[-1].get("provider_execution_identity_sha256"),
                    "consent_id": attempts[-1].get("consent_id")} if _digest(raw) == marker["snapshot_sha256"] else None
        except (ArchiveError, ValueError, OSError, KeyError, TypeError):
            return None

    def is_archived(self, task_id: str) -> bool:
        return self._archived_row(task_id) is not None

    def inventory(self, task_ids: list[str] | None = None) -> dict:
        rows = []
        for task_id in self._ids(task_ids):
            row = {"task_id": task_id, "eligible": False, "archived": False}
            try:
                with ExitStack() as stack:
                    stack.enter_context(_lock(self.root, self.root / ".delivery-controller.lock"))
                    task, attempts, _ = self._records(task_id)
                    row.update(current_attempt_id=task["current_attempt_id"], status=attempts[-1]["status"],
                               error_code=(attempts[-1]["error"] or {}).get("code"))
                    validated = self._eligible(task_id, stack)
                    row.update(eligible=True, archived=self._marker_row(task_id, validated) is not None,
                               reason="terminal-failed-run-locks-idle")
            except Exception as error:
                row["reason"] = error.code if isinstance(error, ArchiveError) else "record-or-lifecycle-unproven"
            rows.append(row)
        return {"schema_version": "vibapp.delivery-task-archive-inventory-v1", "data_root": str(self.data_root),
                "mutation_performed": False, "tasks": rows, "eligible_count": sum(row["eligible"] and not row["archived"] for row in rows)}

    def archived_tasks(self) -> dict:
        rows = []
        for task_id in self._ids(None):
            # Avoid loading task histories that have no marker at all.
            marker = self.root / "tasks" / task_id / ".archived.json"
            if marker.exists():
                row = self._archived_row(task_id)
                if row is not None:
                    rows.append(row)
        return {"schema_version": "vibapp.delivery-task-archive-list-v1", "archived": rows}

    def archive(self, task_ids: list[str]) -> dict:
        ids = self._ids(task_ids)
        with ExitStack() as stack:
            stack.enter_context(_lock(self.root, self.root / ".delivery-controller.lock"))
            # Preflight every explicit target before any snapshot or marker write.
            items = []
            total_bytes = 0
            for task_id in ids:
                task, attempts, records = self._eligible(task_id, stack)
                if self._marker_row(task_id, (task, attempts, records)) is not None:
                    continue
                marker_path = self.root / "tasks" / task_id / ".archived.json"
                if marker_path.exists() or marker_path.is_symlink():
                    raise ArchiveError("existing-marker-invalid")
                snapshot = _bytes({"schema_version": SNAPSHOT_SCHEMA, "task_id": task_id,
                                   "original_records_retained": True, "records": records})
                if len(snapshot) > SNAPSHOT_LIMIT:
                    raise ArchiveError("snapshot-limit")
                total_bytes += len(snapshot)
                if total_bytes > BATCH_LIMIT:
                    raise ArchiveError("archive-batch-limit")
                items.append((task, attempts, records, snapshot, marker_path))
            archive_root = self.root / "task-archives"
            if items and not archive_root.exists():
                archive_root.mkdir(mode=0o700)
            if items:
                _directory(archive_root, private=True)
            results = []
            for task, attempts, records, snapshot, marker_path in items:
                archive_id = "archive-" + uuid.uuid4().hex
                archive_dir = archive_root / archive_id
                archive_dir.mkdir(mode=0o700)
                _exclusive_json(archive_dir / "snapshot.json", snapshot)
                _fsync(archive_dir)
                _fsync(archive_root)
                marker = {"schema_version": SCHEMA, "task_id": task["task_id"],
                          "immutable_task_digest_sha256": task["immutable_task_digest_sha256"],
                          "current_attempt_id": task["current_attempt_id"],
                          "task_record_sha256": records["task.json"]["sha256"],
                          "attempt_records": [{"attempt_id": attempt["attempt_id"], "sha256": records[f"attempts/{attempt['attempt_id']}/attempt.json"]["sha256"]} for attempt in attempts],
                          "archive_id": archive_id, "snapshot_sha256": _digest(snapshot),
                          "archived_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": REASON}
                pending = archive_dir / "marker.json"
                _exclusive_json(pending, _bytes(marker))
                # Atomic same-filesystem publication; no task content is removed.
                os.rename(pending, marker_path)
                _fsync(marker_path.parent)
                results.append({"task_id": task["task_id"], "archive_id": archive_id})
            return {"schema_version": SCHEMA, "archived": results, "original_records_retained": True,
                    "restore": "Move the exact .archived.json marker out of its task directory; originals were never deleted."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--apply", action="store_true", help="Archive explicit terminal-failed targets; default is read-only inventory")
    parser.add_argument("--archived-only", action="store_true", help="Read-only validated hidden IDs for the development feed")
    arguments = parser.parse_args(argv)
    try:
        archive = TaskArchive(arguments.data_root)
        if arguments.apply and arguments.task_ids is None:
            raise ArchiveError("explicit-targets-required")
        if arguments.archived_only and (arguments.apply or arguments.task_ids is not None):
            raise ArchiveError("conflicting-operation")
        result = (archive.archived_tasks() if arguments.archived_only else
                  archive.archive(arguments.task_ids) if arguments.apply else archive.inventory(arguments.task_ids))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"error": error.code if isinstance(error, ArchiveError) else "archive-validation-failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
