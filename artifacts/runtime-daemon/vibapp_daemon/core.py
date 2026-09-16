from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import stat
import struct
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from .service_executor import ServiceExecutionError, ServiceWorker, resolve_runtime_binary
from .host_storage import fcntl, owner_controlled, protect, sync_directory


STATE_SCHEMA = "vibapp.runtime-daemon.state.experimental-v1"
CANDIDATE_SCHEMA = "vibapp.builder-candidate.experimental-v1"
TRANSPORT_SCHEMA = "vibapp.daemon-transport.experimental-v1"
APP_STATE_SCHEMA = "vibapp.app-state-routing.experimental-v1"
UPDATE_SCHEMA = "vibapp.update-transaction.experimental-v1"
ENABLE_SCHEMA = "vibapp.enable-transaction.experimental-v1"
GC_SCHEMA = "vibapp.generation-gc.experimental-v1"
SETTINGS_SCHEMA = "vibapp.settings-snapshot.experimental-v1"
UI_FEEDBACK_SCHEMA = "vibapp.ui-feedback-binding.experimental-v1"
PRESENTATION_SCHEMA = "vibapp.host-presentation.experimental-v1"
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_RECORD_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PACKAGE_FILES = 256
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_IDEMPOTENCY_RECORDS = 4096
MAX_AUDIT_FIELD_CHARS = 512
CRASH_LOOP_COUNT = 3
CRASH_LOOP_WINDOW_SECONDS = 5 * 60
MAX_ACTIVE_SERVICE_WORKERS = 64
MAX_ACTIVE_SERVICE_WORKERS_PER_APP = 8
MAX_APP_STATE_FILES = 256
MAX_APP_STATE_BYTES = 16 * 1024 * 1024
MAX_UPDATE_HISTORY = 16
MAX_GC_DELETIONS_PER_PASS = 8
OBSERVATION_WINDOW_SECONDS = 5 * 60
MAX_SETTINGS_VALUES = 256
MIN_UI_REFRESH_INTERVAL_SECONDS = 1.0

# This table describes the capabilities implemented by the current local
# Component host.  A linked typed stub is intentionally `unavailable`; static
# Component linking must never be promoted into a live capability claim.
LOCAL_HOST_CAPABILITY_AVAILABILITY = {
    "vibapp:experimental-v0/clock@0.0.1": "brokered",
    "vibapp:experimental-v0/scheduler@0.0.1": "unavailable",
    "vibapp:experimental-v0/notification@0.0.1": "unavailable",
    "vibapp:experimental-v0/kv@0.0.1": "brokered",
    "vibapp:experimental-v0/log@0.0.1": "brokered",
    "vibapp:experimental-v0/host-info@0.0.1": "brokered",
    "vibapp:experimental-v0/settings@0.0.1": "brokered",
    "vibapp:experimental-v0/system-metrics@0.0.1": "unavailable",
    "vibapp:experimental-v0/http@0.0.1": "unavailable",
}
LIVE_CAPABILITY_AVAILABILITY = frozenset({"native", "brokered"})
STUB_CAPABILITY_AVAILABILITY = frozenset({"denied", "unavailable"})


class DaemonError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> dt.datetime:
    if not isinstance(value, str) or len(value) != 20 or value[4] != "-" or value[7] != "-" or value[10] != "T" or value[13] != ":" or value[16] != ":" or value[19] != "Z":
        raise DaemonError("invalid-argument", "timestamp must be canonical YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise DaemonError("invalid-argument", "timestamp is not a valid UTC instant") from exc
    return parsed


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DaemonError("malformed-output", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads_strict_json(data: bytes, *, maximum: int) -> Any:
    if not data or len(data) > maximum:
        raise DaemonError("resource-limit", "JSON input is empty or exceeds its byte limit")
    try:
        return json.loads(data, object_pairs_hook=_reject_duplicate_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except DaemonError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise DaemonError("malformed-output", "input is not strict JSON") from exc


def _validate_json_numbers(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if value < -(2**63) or value > 2**64 - 1:
            raise DaemonError("invalid-argument", "JSON integer is outside the supported exact range")
        return
    if isinstance(value, float):
        raise DaemonError("invalid-argument", "floating-point manifest values are not supported by this bounded verifier")
    if isinstance(value, list):
        for item in value:
            _validate_json_numbers(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DaemonError("invalid-argument", "JSON object keys must be strings")
            _validate_json_numbers(item)
        return
    raise DaemonError("invalid-argument", "manifest contains an unsupported JSON value")


def canonical_json(value: Any) -> bytes:
    _validate_json_numbers(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, expected_maximum: int = MAX_PACKAGE_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_maximum:
                raise DaemonError("resource-limit", "package artifact exceeds the byte ceiling")
            digest.update(chunk)
    return digest.hexdigest(), total


def _read_regular_nofollow(path: Path, maximum: int) -> bytes:
    before = _regular_file(path, maximum=maximum)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_dev != before.st_dev or opened.st_ino != before.st_ino or opened.st_size != before.st_size:
            raise DaemonError("integrity-failure", "artifact changed while it was opened")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != before.st_size or len(data) > maximum:
            raise DaemonError("integrity-failure", "artifact changed while it was read")
        return data
    finally:
        os.close(descriptor)


def _regular_file(path: Path, *, maximum: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DaemonError("not-found", f"required file is unavailable: {path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0 or info.st_size > maximum:
        raise DaemonError("integrity-failure", f"file is not one bounded, regular, single-link file: {path.name}")
    return info


def _bounded_text(value: Any, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 0x20 for char in value):
        raise DaemonError("invalid-argument", f"{field} is not a bounded text value")
    return value


def _digest(value: Any, field: str) -> str:
    text = _bounded_text(value, field, 64)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise DaemonError("integrity-failure", f"{field} is not a lowercase SHA-256 digest")
    return text


def _safe_app_id(value: Any) -> str:
    app_id = _bounded_text(value, "app.id", 128)
    if not app_id[0].isascii() or not app_id[0].islower() or any(not (char.isascii() and (char.islower() or char.isdigit() or char in ".-")) for char in app_id):
        raise DaemonError("invalid-argument", "app.id contains unsupported characters")
    if app_id.endswith((".", "-")) or ".." in app_id or ".-" in app_id or "-." in app_id:
        raise DaemonError("invalid-argument", "app.id has an ambiguous separator")
    return app_id


def _safe_relative_path(value: Any) -> str:
    text = _bounded_text(value, "artifact path", 512)
    if unicodedata.normalize("NFC", text) != text or not text.isascii():
        raise DaemonError("integrity-failure", "artifact path must be normalized ASCII")
    if "\\" in text or "//" in text or text.startswith("/") or text.endswith("/") or "\x00" in text:
        raise DaemonError("integrity-failure", "artifact path is not a canonical relative path")
    path = PurePosixPath(text)
    if any(part in ("", ".", "..") for part in path.parts):
        raise DaemonError("integrity-failure", "artifact path escapes the package")
    return text


def _copy_json(value: Any) -> Any:
    return copy.deepcopy(value)


def _derive_host_presentation(
    app_kind: str, display_name: str, description: str
) -> dict[str, Any]:
    if app_kind not in {"ui", "service", "hybrid"}:
        raise DaemonError("incompatible-contract", "presentation app kind is unsupported")
    normalized = unicodedata.normalize(
        "NFKC", f"{display_name}\n{description}"
    ).casefold()
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


def _host_presentation(
    value: Any, app_kind: str, display_name: str, description: str
) -> dict[str, Any]:
    if value is None:
        return _derive_host_presentation(app_kind, display_name, description)
    fields = {
        "schema_version", "preferred_width", "preferred_height",
        "minimum_width", "minimum_height", "resizable",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise DaemonError("integrity-failure", "presentation fields are malformed")
    if value.get("schema_version") != PRESENTATION_SCHEMA:
        raise DaemonError("unsupported-version", "presentation schema is unsupported")
    bounds = {
        "preferred_width": (320, 1920),
        "preferred_height": (240, 1200),
        "minimum_width": (320, 1280),
        "minimum_height": (240, 960),
    }
    normalized: dict[str, Any] = {"schema_version": PRESENTATION_SCHEMA}
    for field, (minimum, maximum) in bounds.items():
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise DaemonError("resource-limit", f"presentation.{field} is outside the host bound")
        normalized[field] = item
    if normalized["preferred_width"] < normalized["minimum_width"] or normalized["preferred_height"] < normalized["minimum_height"]:
        raise DaemonError("incompatible-contract", "presentation preferred size is below its minimum")
    if not isinstance(value.get("resizable"), bool):
        raise DaemonError("integrity-failure", "presentation.resizable is not boolean")
    normalized["resizable"] = value["resizable"]
    return normalized


def _capability_interfaces(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise DaemonError("incompatible-contract", "manifest capability list is malformed")
    interfaces: list[str] = []
    for capability in value:
        if not isinstance(capability, dict):
            raise DaemonError("incompatible-contract", "manifest capability row is malformed")
        interface = _bounded_text(capability.get("interface"), "capability.interface", 256)
        if interface in interfaces:
            raise DaemonError("incompatible-contract", "manifest capability interface is duplicated")
        interfaces.append(interface)
    return tuple(interfaces)


@dataclass(frozen=True)
class VerifiedCandidate:
    record_path: Path
    package_dir: Path
    package_digest: str
    component_digest: str
    manifest_digest: str
    app_id: str
    app_version: str
    app_kind: str
    display_name: str
    publisher_id: str
    publisher_display_name: str
    ui_entrypoints: tuple[dict[str, Any], ...]
    service_entrypoints: tuple[dict[str, Any], ...]
    allowed_dispositions: tuple[str, ...]
    default_disposition: str
    runtime_world: str
    state_schema: int
    migratable_from_min: int
    migratable_from_max: int
    capability_contract_sha256: str
    kv_storage_limit_bytes: int | None
    permissions: tuple[str, ...]
    presentation: dict[str, Any]
    manifest: dict[str, Any]


class RuntimeDaemon:
    """Durable lifecycle authority used by both socket server and tests."""

    def __init__(
        self,
        runtime_root: Path | str,
        promotion_root: Path | str,
        *,
        clock: Callable[[], str] = _utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        service_runtime_binary: Path | str | None = None,
        update_fault: str | None = None,
    ):
        self.root = Path(runtime_root).expanduser().resolve()
        self.promotion_root = Path(promotion_root).expanduser().resolve()
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self._thread_lock = threading.RLock()
        self._service_runtime_binary = service_runtime_binary
        self._update_fault = update_fault
        self._workers: dict[tuple[str, str], ServiceWorker] = {}
        self._ui_workers: dict[tuple[str, str], ServiceWorker] = {}
        self._ui_refresh_at: dict[tuple[str, str], float] = {}
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"
        self.audit_path = self.root / "audit.jsonl"
        self.packages_root = self.root / "packages"
        self.data_root = self.root / "app-data"
        self.retained_root = self.root / "retained"
        self.exports_root = self.root / "exports"
        self.updates_root = self.root / "updates"
        self._prepare_roots()
        with self._locked_state() as state:
            changed = self._reconcile_recovery(state)
            if changed:
                self._write_state(state)

    def _prepare_roots(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(self.root, directory=True)
        try:
            promotion_info = self.promotion_root.lstat()
        except OSError as exc:
            raise DaemonError("not-found", "configured promotion root must already exist") from exc
        if not stat.S_ISDIR(promotion_info.st_mode) or stat.S_ISLNK(promotion_info.st_mode):
            raise DaemonError("integrity-failure", "configured promotion root must be a real directory")
        if not owner_controlled(self.promotion_root, promotion_info):
            raise DaemonError("permission-denied", "configured promotion root must be owner-controlled and not group/world writable")
        for path in (
            self.packages_root,
            self.data_root,
            self.retained_root,
            self.exports_root,
            self.updates_root,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "nt":
                protect(path, directory=True)
        if self.lock_path.exists():
            lock_info = self.lock_path.lstat()
            if not stat.S_ISREG(lock_info.st_mode) or stat.S_ISLNK(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise DaemonError("integrity-failure", "daemon lock file is unsafe")
        else:
            descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
            os.close(descriptor)
        protect(self.lock_path)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA,
            "sequence": 0,
            "apps": {},
            "idempotency": {},
            "retained": {},
            "exports": {},
            "daemon": {"owner": "daemon", "last_recovered_at_utc": None},
        }

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock, self.lock_path.open("r+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read_state()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            state = self._empty_state()
            self._write_state(state)
            return state
        info = _regular_file(self.state_path, maximum=16 * 1024 * 1024)
        if not owner_controlled(self.state_path, info, private=True):
            raise DaemonError("integrity-failure", "daemon state permissions are not owner-private")
        state = loads_strict_json(self.state_path.read_bytes(), maximum=16 * 1024 * 1024)
        if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA:
            raise DaemonError("integrity-failure", "daemon state schema is unsupported")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        data = canonical_json(state) + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            protect(temporary)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            sync_directory(self.root)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _append_audit(self, state: dict[str, Any], event: str, *, principal: str, app_id: str | None, result: str, fields: dict[str, Any] | None = None) -> None:
        state["sequence"] += 1
        safe_fields: dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if isinstance(value, str):
                safe_fields[key] = value[:MAX_AUDIT_FIELD_CHARS].replace("\n", "\\n").replace("\r", "\\r")
            elif value is None or isinstance(value, (bool, int)):
                safe_fields[key] = value
        record = {
            "sequence": state["sequence"],
            "at_utc": self.clock(),
            "event": event,
            "principal": principal[:128],
            "app_id": app_id,
            "result": result,
            "fields": safe_fields,
        }
        encoded = canonical_json(record) + b"\n"
        descriptor = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise DaemonError("integrity-failure", "daemon audit file is unsafe")
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _reconcile_recovery(self, state: dict[str, Any]) -> bool:
        changed = False
        recovered = 0
        quiesced = 0
        for app_id, app in state["apps"].items():
            try:
                if self._ensure_app_update_metadata(app):
                    changed = True
            except DaemonError as error:
                # An observation candidate is intentionally still reversible.
                # Integrity failure in that active candidate must restore the
                # already-bound previous generation before any trigger metadata
                # or Guest process can be recovered from the damaged package.
                if not self._rollback_observation_update(
                    state,
                    app_id,
                    app,
                    reason=f"candidate recovery failed: {error.message}",
                    principal="daemon",
                ):
                    raise
                changed = True
                if self._ensure_app_update_metadata(app):
                    changed = True
            if self._recover_pending_enable(state, app_id, app):
                changed = True
            if self._recover_pending_update(state, app_id, app):
                changed = True
            if self._expire_observation_window(state, app_id, app):
                changed = True
            if self._garbage_collect_app(state, app_id, app):
                changed = True
            if (
                app.get("enabled")
                and app.get("runtime_world") == "ui-only-reference"
                and app.get("update", {}).get("status") == "observation-window"
            ):
                validation_worker: ServiceWorker | None = None
                try:
                    _validation, validation_worker = self._create_service_generation(
                        app_id,
                        app,
                        app["ui_entrypoints"][0]["id"],
                        reason="host-restart",
                        activate_service=False,
                    )
                except DaemonError as error:
                    if self._rollback_observation_update(
                        state,
                        app_id,
                        app,
                        reason=f"candidate UI recovery failed: {error.message}",
                        principal="daemon",
                    ):
                        changed = True
                finally:
                    if validation_worker is not None:
                        validation_worker.terminate()
            services = app.get("services", {})
            if app.get("enabled") and not app.get("quarantined"):
                for entrypoint, service in services.items():
                    if service.get("state") == "running":
                        if (app_id, entrypoint) in self._workers:
                            # A rollback performed earlier in this reconciliation
                            # pass already created and registered the authoritative
                            # replacement. Do not leak it by cold-starting twice.
                            service["recovered_at_utc"] = self.clock()
                            recovered += 1
                            changed = True
                            continue
                        try:
                            replacement = self._activate_service_generation(app_id, app, entrypoint, reason="host-restart")
                            services[entrypoint] = replacement
                            replacement["recovered_at_utc"] = self.clock()
                            recovered += 1
                        except DaemonError as error:
                            if self._rollback_observation_update(
                                state,
                                app_id,
                                app,
                                reason=f"candidate recovery failed: {error.message}",
                                principal="daemon",
                            ):
                                recovered += sum(
                                    1
                                    for restored in app["services"].values()
                                    if restored.get("state") == "running"
                                )
                                changed = True
                                break
                            service["state"] = "failed"
                            service["stop_reason"] = "recovery-failed"
                            service["stopped_at_utc"] = self.clock()
                            service["last_error"] = self._service_error(error.code, error.message, error.retryable, "recovery", at_utc=self.clock())
                            app["last_error"] = error.code
                            quiesced += 1
                        changed = True
            else:
                for service in services.values():
                    if service.get("state") == "running":
                        service["state"] = "stopped"
                        service["stop_reason"] = "recovery-quiesce"
                        service["stopped_at_utc"] = self.clock()
                        quiesced += 1
                        changed = True
        if recovered or quiesced:
            state["daemon"]["last_recovered_at_utc"] = self.clock()
            self._append_audit(state, "daemon-recovery", principal="daemon", app_id=None, result="reconciled", fields={"recovered_services": recovered, "quiesced_services": quiesced})
        return changed

    def _candidate_record_path(self, supplied: Path | str) -> Path:
        raw = Path(supplied).expanduser()
        if not raw.is_absolute():
            raise DaemonError("invalid-argument", "promotion record path must be absolute")
        try:
            supplied_info = raw.lstat()
        except OSError as exc:
            raise DaemonError("not-found", "promotion record does not exist") from exc
        if not stat.S_ISREG(supplied_info.st_mode) or stat.S_ISLNK(supplied_info.st_mode):
            raise DaemonError("integrity-failure", "promotion record must not be a link")
        if not owner_controlled(raw, supplied_info):
            raise DaemonError("permission-denied", "promotion record is not owner-controlled")
        try:
            canonical = raw.resolve(strict=True)
        except OSError as exc:
            raise DaemonError("not-found", "promotion record does not exist") from exc
        if not canonical.is_relative_to(self.promotion_root):
            raise DaemonError("permission-denied", "promotion record is outside the configured promotion root")
        _regular_file(canonical, maximum=MAX_RECORD_BYTES)
        return canonical

    @staticmethod
    def _descriptor(value: Any, expected_path: str | None = None) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise DaemonError("integrity-failure", "artifact descriptor must be an object")
        path = _safe_relative_path(value.get("path"))
        if expected_path is not None and path != expected_path:
            raise DaemonError("integrity-failure", f"artifact path must be {expected_path}")
        size = value.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_PACKAGE_BYTES:
            raise DaemonError("integrity-failure", f"{path}.size_bytes is invalid")
        return {"path": path, "sha256": _digest(value.get("sha256"), f"{path}.sha256"), "size_bytes": size}

    def _manifest_descriptors(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise DaemonError("incompatible-contract", "manifest.artifacts is missing")
        descriptors = [self._descriptor(artifacts.get("canonical_component"), "component.wasm")]
        assets = artifacts.get("assets")
        derivations = artifacts.get("browser_derivations")
        if not isinstance(assets, list) or not isinstance(derivations, list):
            raise DaemonError("incompatible-contract", "manifest artifact arrays are malformed")
        descriptors.extend(self._descriptor(item) for item in assets)
        for derivation in derivations:
            if not isinstance(derivation, dict) or not isinstance(derivation.get("files"), list):
                raise DaemonError("incompatible-contract", "browser derivation is malformed")
            descriptors.extend(self._descriptor(item) for item in derivation["files"])
            descriptors.append(self._descriptor(derivation.get("derivation_attestation")))
        descriptors.append(self._descriptor(artifacts.get("provenance"), "provenance.json"))
        descriptors.append(self._descriptor(artifacts.get("sbom"), "sbom.cdx.json"))
        paths = [item["path"] for item in descriptors]
        if len(paths) != len(set(paths)):
            raise DaemonError("integrity-failure", "manifest contains duplicate artifact paths")
        return descriptors

    def _verify_package_tree(self, package_dir: Path, record: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        try:
            supplied_info = package_dir.lstat()
        except OSError as exc:
            raise DaemonError("not-found", "candidate package directory is unavailable") from exc
        if not stat.S_ISDIR(supplied_info.st_mode) or stat.S_ISLNK(supplied_info.st_mode):
            raise DaemonError("integrity-failure", "candidate package must not be a link")
        if not owner_controlled(package_dir, supplied_info):
            raise DaemonError("permission-denied", "candidate package directory is not owner-controlled")
        try:
            canonical_dir = package_dir.resolve(strict=True)
        except OSError as exc:
            raise DaemonError("not-found", "candidate package directory is unavailable") from exc
        if not canonical_dir.is_dir():
            raise DaemonError("integrity-failure", "candidate package is not a regular directory")
        manifest_record = self._descriptor(record.get("manifest"), "manifest.json")
        component_record = self._descriptor(record.get("component"), "component.wasm")
        manifest_path = canonical_dir / "manifest.json"
        manifest_info = _regular_file(manifest_path, maximum=MAX_MANIFEST_BYTES)
        manifest_bytes = manifest_path.read_bytes()
        manifest_hash = _sha256_bytes(manifest_bytes)
        if manifest_hash != manifest_record["sha256"] or manifest_info.st_size != manifest_record["size_bytes"]:
            raise DaemonError("integrity-failure", "manifest bytes do not match verifier promotion")
        manifest = loads_strict_json(manifest_bytes, maximum=MAX_MANIFEST_BYTES)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "vibapp.manifest.experimental-v0.0.1" or manifest.get("package_format") != "vibapp.package.experimental-v0":
            raise DaemonError("unsupported-version", "manifest contract or package format is unsupported")
        descriptors = self._manifest_descriptors(manifest)
        expected_paths = {"manifest.json", *(item["path"] for item in descriptors)}
        observed_paths: set[str] = set()
        total_bytes = 0
        for root, directories, files in os.walk(canonical_dir, followlinks=False):
            directories.sort()
            files.sort()
            relative_root = Path(root).relative_to(canonical_dir)
            for directory in list(directories):
                target = Path(root) / directory
                info = target.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise DaemonError("integrity-failure", "package contains a linked or non-directory node")
            for name in files:
                target = Path(root) / name
                info = _regular_file(target, maximum=MAX_PACKAGE_BYTES)
                if not owner_controlled(target, info):
                    raise DaemonError("permission-denied", "candidate artifact is not owner-controlled")
                relative = (relative_root / name).as_posix()
                observed_paths.add(relative)
                total_bytes += info.st_size
                if len(observed_paths) > MAX_PACKAGE_FILES or total_bytes > MAX_PACKAGE_BYTES:
                    raise DaemonError("resource-limit", "candidate package exceeds file or byte limits")
        if observed_paths != expected_paths:
            raise DaemonError("integrity-failure", "candidate package has missing or extra files")
        for descriptor in descriptors:
            target = canonical_dir / descriptor["path"]
            actual_hash, actual_size = _sha256_file(target)
            if actual_hash != descriptor["sha256"] or actual_size != descriptor["size_bytes"]:
                raise DaemonError("integrity-failure", f"artifact bytes differ from manifest: {descriptor['path']}")
        if component_record != next(item for item in descriptors if item["path"] == "component.wasm"):
            raise DaemonError("integrity-failure", "verifier component descriptor differs from manifest")
        manifest_canonical = canonical_json(manifest)
        preimage = bytearray(b"VIBAPP-PACKAGE\x00experimental-v0\x00")
        preimage.extend(struct.pack(">Q", len(manifest_canonical)))
        preimage.extend(manifest_canonical)
        for descriptor in sorted(descriptors, key=lambda item: item["path"].encode("utf-8")):
            path_bytes = descriptor["path"].encode("utf-8")
            if len(path_bytes) > 65535:
                raise DaemonError("resource-limit", "artifact path is too long")
            preimage.extend(struct.pack(">H", len(path_bytes)))
            preimage.extend(path_bytes)
            preimage.extend(bytes.fromhex(descriptor["sha256"]))
            preimage.extend(struct.pack(">Q", descriptor["size_bytes"]))
        return manifest, _sha256_bytes(bytes(preimage)), total_bytes

    def _verify_candidate(self, supplied: Path | str, expected_digest: str) -> VerifiedCandidate:
        record_path = self._candidate_record_path(supplied)
        record = loads_strict_json(record_path.read_bytes(), maximum=MAX_RECORD_BYTES)
        if not isinstance(record, dict):
            raise DaemonError("malformed-output", "promotion record is not an object")
        required_keys = {
            "schema_version", "document_type", "state", "job_id", "source_tree_sha256",
            "package_digest_sha256", "package_directory", "component", "manifest",
            "quarantine_receipt_sha256", "verification", "authority",
        }
        # Preserve exact v1 compatibility: old candidates omit presentation;
        # new candidates may add exactly that verifier-owned sidecar and no more.
        if set(record) not in (required_keys, required_keys | {"presentation"}):
            raise DaemonError("integrity-failure", "promotion record fields do not match the supported candidate schema")
        if record.get("schema_version") != CANDIDATE_SCHEMA or record.get("document_type") != "verifier-promoted-candidate" or record.get("state") != "candidate-ready":
            raise DaemonError("integrity-failure", "promotion record is not a verifier-ready candidate")
        package_digest = _digest(record.get("package_digest_sha256"), "package_digest_sha256")
        if package_digest != expected_digest:
            raise DaemonError("integrity-failure", "install digest differs from the promotion record")
        _digest(record.get("source_tree_sha256"), "source_tree_sha256")
        _digest(record.get("quarantine_receipt_sha256"), "quarantine_receipt_sha256")
        _bounded_text(record.get("job_id"), "job_id", 128)
        if record.get("package_directory") != "package":
            raise DaemonError("integrity-failure", "candidate package directory must be the frozen package path")
        if not isinstance(record.get("component"), dict) or set(record["component"]) != {"path", "sha256", "size_bytes"}:
            raise DaemonError("integrity-failure", "promotion component fields do not match the frozen schema")
        if not isinstance(record.get("manifest"), dict) or set(record["manifest"]) != {"path", "sha256", "size_bytes"}:
            raise DaemonError("integrity-failure", "promotion manifest fields do not match the frozen schema")
        verification = record.get("verification")
        if not isinstance(verification, dict) or set(verification) != {"authority", "verifier_version", "verified_at_utc", "checks"}:
            raise DaemonError("integrity-failure", "verification fields do not match the frozen schema")
        if verification.get("authority") != "independent-verifier" or verification.get("verifier_version") != "app-verifier.experimental-v1":
            raise DaemonError("integrity-failure", "candidate lacks an independent verifier decision")
        _parse_utc(verification.get("verified_at_utc"))
        if not isinstance(verification.get("checks"), list) or not (1 <= len(verification["checks"]) <= 64):
            raise DaemonError("integrity-failure", "candidate verification checks are absent or unbounded")
        for check in verification["checks"]:
            if not isinstance(check, dict) or set(check) != {"id", "outcome", "tool", "detail"} or check.get("outcome") != "pass":
                raise DaemonError("integrity-failure", "not every independent verification check passed")
            _bounded_text(check.get("id"), "verification.check.id", 128)
            _bounded_text(check.get("tool"), "verification.check.tool", 256)
            _bounded_text(check.get("detail"), "verification.check.detail", 1000)
        authority = record.get("authority")
        if authority != {"install": "daemon", "publish": "none"}:
            raise DaemonError("integrity-failure", "candidate authority is not install-daemon/private-only")
        package_dir = record_path.parent / "package"
        if not package_dir.resolve(strict=True).is_relative_to(self.promotion_root):
            raise DaemonError("permission-denied", "candidate package escapes the promotion root")
        manifest, actual_package_digest, _ = self._verify_package_tree(package_dir, record)
        if actual_package_digest != package_digest:
            raise DaemonError("integrity-failure", "package digest does not match independently promoted bytes")
        app = manifest.get("app")
        runtime = manifest.get("runtime")
        lifecycle = manifest.get("lifecycle")
        entrypoints = manifest.get("entrypoints")
        if not isinstance(app, dict) or not isinstance(runtime, dict) or not isinstance(lifecycle, dict) or not isinstance(entrypoints, list):
            raise DaemonError("incompatible-contract", "manifest lifecycle identity is incomplete")
        app_id = _safe_app_id(app.get("id"))
        app_version = _bounded_text(app.get("version"), "app.version", 64)
        app_kind = app.get("kind")
        if app_kind not in ("ui", "service", "hybrid"):
            raise DaemonError("incompatible-contract", "manifest app kind is unsupported")
        display_name = _bounded_text(app.get("display_name"), "app.display_name", 128)
        description = _bounded_text(app.get("description"), "app.description", 1000)
        publisher = app.get("publisher")
        if not isinstance(publisher, dict):
            raise DaemonError("incompatible-contract", "manifest publisher is missing")
        publisher_id = _bounded_text(publisher.get("id"), "publisher.id", 128)
        publisher_display_name = _bounded_text(
            publisher.get("display_name"), "publisher.display_name", 128
        )
        normalized_entrypoints: list[dict[str, Any]] = []
        seen_entrypoints: set[str] = set()
        for item in entrypoints:
            if not isinstance(item, dict) or item.get("kind") not in ("launcher-ui", "service", "settings"):
                raise DaemonError("incompatible-contract", "manifest entrypoint is malformed")
            entrypoint_id = _bounded_text(item.get("id"), "entrypoint.id", 128)
            if entrypoint_id in seen_entrypoints:
                raise DaemonError("incompatible-contract", "manifest entrypoint IDs are not unique")
            seen_entrypoints.add(entrypoint_id)
            normalized = {
                "id": entrypoint_id,
                "kind": item["kind"],
                "initial_route": (item.get("routes") or {}).get("initial"),
            }
            if item["kind"] == "service":
                triggers = item.get("triggers")
                canonical_triggers = [
                    trigger
                    for trigger in ("on-enable", "scheduler", "manual")
                    if isinstance(triggers, list) and trigger in triggers
                ]
                if (
                    not isinstance(triggers, list)
                    or not triggers
                    or len(triggers) > 3
                    or triggers != canonical_triggers
                ):
                    raise DaemonError(
                        "incompatible-contract",
                        "service entrypoint triggers are missing, duplicated, unknown, or non-canonical",
                    )
                health_interval = item.get("health_check_interval_seconds")
                if (
                    isinstance(health_interval, bool)
                    or not isinstance(health_interval, int)
                    or not 10 <= health_interval <= 86_400
                ):
                    raise DaemonError(
                        "incompatible-contract",
                        "service entrypoint health interval is invalid",
                    )
                normalized["triggers"] = list(triggers)
                normalized["health_check_interval_seconds"] = health_interval
            normalized_entrypoints.append(normalized)
        ui_entrypoints = tuple(item for item in normalized_entrypoints if item["kind"] == "launcher-ui")
        service_entrypoints = tuple(item for item in normalized_entrypoints if item["kind"] == "service")
        if (app_kind == "ui" and (not ui_entrypoints or service_entrypoints)) or (app_kind == "service" and (ui_entrypoints or not service_entrypoints)) or (app_kind == "hybrid" and (not ui_entrypoints or not service_entrypoints)):
            raise DaemonError("incompatible-contract", "app kind and entrypoints disagree")
        runtime_world = runtime.get("world")
        expected_world = {"ui": "ui-only-reference", "service": "service-only-reference", "hybrid": "hybrid-reference"}[app_kind]
        if runtime_world != expected_world:
            raise DaemonError("incompatible-contract", "app kind and runtime world disagree")
        uninstall = lifecycle.get("uninstall")
        if not isinstance(uninstall, dict) or not isinstance(uninstall.get("allowed_data_dispositions"), list):
            raise DaemonError("incompatible-contract", "uninstall disposition contract is missing")
        dispositions = tuple(uninstall["allowed_data_dispositions"])
        if not dispositions or any(item not in ("delete", "retain", "export-then-delete") for item in dispositions) or len(dispositions) != len(set(dispositions)):
            raise DaemonError("incompatible-contract", "uninstall dispositions are invalid")
        default = uninstall.get("default_data_disposition")
        if default not in dispositions:
            raise DaemonError("incompatible-contract", "default uninstall disposition is not allowed")
        state_contract = manifest.get("state")
        if not isinstance(state_contract, dict) or set(state_contract) != {
            "schema",
            "migratable_from_min",
            "migratable_from_max",
        }:
            raise DaemonError("incompatible-contract", "manifest state migration contract is missing")
        state_schema = state_contract.get("schema")
        migratable_from_min = state_contract.get("migratable_from_min")
        migratable_from_max = state_contract.get("migratable_from_max")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 2**32 - 1
            for item in (state_schema, migratable_from_min, migratable_from_max)
        ) or migratable_from_min > migratable_from_max:
            raise DaemonError("incompatible-contract", "manifest state migration range is invalid")
        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, list):
            raise DaemonError("incompatible-contract", "manifest capability contract is missing")
        capability_contract_sha256 = _sha256_bytes(canonical_json(capabilities))
        kv_storage_limit_bytes = self._kv_storage_limit(capabilities)
        permissions = _capability_interfaces(capabilities)
        if any(
            "scheduler" in entrypoint["triggers"]
            for entrypoint in service_entrypoints
        ) and "vibapp:experimental-v0/scheduler@0.0.1" not in permissions:
            raise DaemonError(
                "incompatible-contract",
                "scheduler-triggered service lacks the structural scheduler import",
            )
        presentation = _host_presentation(
            record.get("presentation"), app_kind, display_name, description
        )
        if presentation != _derive_host_presentation(app_kind, display_name, description):
            raise DaemonError(
                "integrity-failure",
                "candidate presentation differs from active manifest policy",
            )
        component_digest = _digest(record["component"].get("sha256"), "component.sha256")
        manifest_digest = _digest(record["manifest"].get("sha256"), "manifest.sha256")
        return VerifiedCandidate(
            record_path,
            package_dir,
            package_digest,
            component_digest,
            manifest_digest,
            app_id,
            app_version,
            app_kind,
            display_name,
            publisher_id,
            publisher_display_name,
            ui_entrypoints,
            service_entrypoints,
            dispositions,
            default,
            runtime_world,
            state_schema,
            migratable_from_min,
            migratable_from_max,
            capability_contract_sha256,
            kv_storage_limit_bytes,
            permissions,
            presentation,
            manifest,
        )

    @staticmethod
    def _kv_storage_limit(capabilities: list[Any]) -> int | None:
        matches = [
            capability
            for capability in capabilities
            if isinstance(capability, dict)
            and capability.get("interface") == "vibapp:experimental-v0/kv@0.0.1"
        ]
        if len(matches) > 1:
            raise DaemonError("incompatible-contract", "manifest declares KV more than once")
        if not matches:
            return None
        capability = matches[0]
        profiles = capability.get("profiles")
        scope = capability.get("scope")
        desktop = (
            next(
                (
                    profile
                    for profile in profiles
                    if isinstance(profile, dict) and profile.get("profile") == "desktop"
                ),
                None,
            )
            if isinstance(profiles, list)
            else None
        )
        limit = scope.get("maximum_storage_bytes") if isinstance(scope, dict) else None
        if capability.get("grant") != "automatic":
            return None
        if (
            not isinstance(desktop, dict)
            or desktop.get("availability") not in {"native", "brokered"}
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10 * 1024 * 1024
        ):
            raise DaemonError(
                "capability-unavailable",
                "the automatic local KV migration grant lacks a bounded desktop profile",
            )
        return limit

    def _require_activation_capabilities(
        self, app: dict[str, Any], *, profile: str = "desktop"
    ) -> None:
        """Fail closed before a generation can obtain an activation route.

        Manifest availability is an app/profile promise, while the table above
        is current host truth.  Required non-live capabilities block activation;
        degradable imports remain linkable only through their typed stub.
        """

        manifest = self._installed_manifest(app)
        runtime = manifest.get("runtime")
        capabilities = manifest.get("capabilities")
        if not isinstance(runtime, dict) or not isinstance(capabilities, list):
            raise DaemonError(
                "incompatible-contract",
                "installed manifest lacks runtime capability metadata",
            )
        runtime_profiles = runtime.get("profiles")
        required_imports = runtime.get("required_imports")
        if not isinstance(runtime_profiles, list) or not isinstance(required_imports, list):
            raise DaemonError(
                "incompatible-contract", "installed runtime profile/import metadata is malformed"
            )
        profile_names: list[str] = []
        for row in runtime_profiles:
            if not isinstance(row, dict):
                raise DaemonError(
                    "incompatible-contract", "installed runtime profile row is malformed"
                )
            name = row.get("profile")
            if not isinstance(name, str) or name in profile_names:
                raise DaemonError(
                    "incompatible-contract", "installed runtime profiles are invalid or duplicated"
                )
            profile_names.append(name)
        if profile not in profile_names:
            raise DaemonError(
                "unsupported-surface", f"installed package does not support the {profile} profile"
            )
        if (
            any(not isinstance(interface, str) for interface in required_imports)
            or len(required_imports) != len(set(required_imports))
        ):
            raise DaemonError(
                "incompatible-contract", "installed required imports are invalid or duplicated"
            )

        capability_rows: dict[str, dict[str, Any]] = {}
        for capability in capabilities:
            if not isinstance(capability, dict):
                raise DaemonError(
                    "incompatible-contract", "installed capability row is malformed"
                )
            interface = capability.get("interface")
            if not isinstance(interface, str) or interface in capability_rows:
                raise DaemonError(
                    "incompatible-contract", "installed capability interfaces are invalid or duplicated"
                )
            capability_rows[interface] = capability
        if set(capability_rows) != set(required_imports):
            raise DaemonError(
                "incompatible-contract",
                "installed capabilities do not equal the runtime required imports",
            )

        for interface in required_imports:
            capability = capability_rows[interface]
            necessity = capability.get("necessity")
            if necessity not in {"required", "degradable"}:
                raise DaemonError(
                    "incompatible-contract", "installed capability necessity is invalid"
                )
            profile_rows = capability.get("profiles")
            if not isinstance(profile_rows, list):
                raise DaemonError(
                    "incompatible-contract", "installed capability profile metadata is malformed"
                )
            by_profile: dict[str, dict[str, Any]] = {}
            for row in profile_rows:
                if not isinstance(row, dict):
                    raise DaemonError(
                        "incompatible-contract", "installed capability profile row is malformed"
                    )
                name = row.get("profile")
                if not isinstance(name, str) or name in by_profile:
                    raise DaemonError(
                        "incompatible-contract",
                        "installed capability profiles are invalid or duplicated",
                    )
                by_profile[name] = row
            if set(by_profile) != set(profile_names):
                raise DaemonError(
                    "incompatible-contract",
                    "installed capability profile coverage differs from the runtime",
                )
            declared = by_profile[profile].get("availability")
            if declared not in {
                "native",
                "brokered",
                "mock",
                "denied",
                "unavailable",
            }:
                raise DaemonError(
                    "incompatible-contract", "installed capability availability is invalid"
                )
            host = LOCAL_HOST_CAPABILITY_AVAILABILITY.get(interface)
            if host is None:
                raise DaemonError(
                    "missing-interface", f"local host does not link {interface}"
                )
            if interface == "vibapp:experimental-v0/kv@0.0.1" and app.get(
                "kv_storage_limit_bytes"
            ) is None:
                host = "unavailable"
            if declared == "mock":
                raise DaemonError(
                    "incompatible-contract",
                    f"desktop activation cannot use a mock {interface} capability",
                )
            if host in STUB_CAPABILITY_AVAILABILITY and declared in LIVE_CAPABILITY_AVAILABILITY:
                raise DaemonError(
                    "incompatible-contract",
                    f"manifest advertises live {interface} authority that the local host does not provide",
                )
            effective = host if host in STUB_CAPABILITY_AVAILABILITY else declared
            if necessity == "required" and effective == "denied":
                raise DaemonError(
                    "permission-denied",
                    f"required capability is denied for {profile}: {interface}",
                )
            if necessity == "required" and effective == "unavailable":
                raise DaemonError(
                    "capability-unavailable",
                    f"required capability is unavailable for {profile}: {interface}",
                )

    def _stage_package(self, candidate: VerifiedCandidate, record: dict[str, Any]) -> Path:
        app_root = self.packages_root / candidate.app_id
        app_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = app_root / candidate.package_digest
        if destination.exists():
            _manifest, actual_digest, _total = self._verify_package_tree(destination, record)
            if actual_digest != candidate.package_digest:
                raise DaemonError("integrity-failure", "installed package digest changed")
            return destination
        temporary = app_root / f".stage-{uuid.uuid4().hex}"
        try:
            temporary.mkdir(mode=0o700)
            expected_paths = ["manifest.json", *(item["path"] for item in self._manifest_descriptors(candidate.manifest))]
            for relative in sorted(expected_paths):
                source = candidate.package_dir.joinpath(*PurePosixPath(relative).parts)
                if source.resolve(strict=True) != source:
                    raise DaemonError("integrity-failure", "package copy source traverses a link")
                data = _read_regular_nofollow(source, MAX_PACKAGE_BYTES)
                target = temporary.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    with contextlib.suppress(FileNotFoundError):
                        target.unlink()
                    raise
            _manifest, actual_digest, _total = self._verify_package_tree(temporary, record)
            if actual_digest != candidate.package_digest:
                raise DaemonError("integrity-failure", "staged package digest differs from the promoted candidate")
            os.replace(temporary, destination)
            return destination
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    @staticmethod
    def _new_update_state() -> dict[str, Any]:
        return {
            "schema_version": UPDATE_SCHEMA,
            "status": "none",
            "pending": None,
            "previous": None,
            "last_transaction": None,
            "history": [],
        }

    @staticmethod
    def _new_gc_state() -> dict[str, Any]:
        return {
            "schema_version": GC_SCHEMA,
            "status": "idle",
            "pending": [],
            "last_completed_at_utc": None,
            "last_removed": [],
        }

    def _verified_installed_manifest(
        self, app: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Rebind recovery metadata to the exact installed package bytes."""
        app_id = _safe_app_id(app.get("app_id"))
        package_digest = _digest(
            app.get("package_digest_sha256"), "installed package digest"
        )
        relative = _safe_relative_path(app.get("package_path"))
        package = self.root.joinpath(*PurePosixPath(relative).parts)
        expected = self.packages_root / app_id / package_digest
        if package != expected:
            raise DaemonError(
                "integrity-failure",
                "installed package path is not bound to its app and package digest",
            )
        try:
            package_info = package.lstat()
            canonical_package = package.resolve(strict=True)
        except OSError as error:
            raise DaemonError("integrity-failure", "installed package is unavailable") from error
        if (
            not stat.S_ISDIR(package_info.st_mode)
            or stat.S_ISLNK(package_info.st_mode)
            or canonical_package != expected
        ):
            raise DaemonError("integrity-failure", "installed package path is unsafe")

        manifest_path = package / "manifest.json"
        manifest_bytes = _read_regular_nofollow(manifest_path, MAX_MANIFEST_BYTES)
        manifest_digest = _sha256_bytes(manifest_bytes)
        recorded_manifest_digest = app.get("manifest_sha256")
        if recorded_manifest_digest is not None and (
            _digest(recorded_manifest_digest, "installed manifest digest")
            != manifest_digest
        ):
            raise DaemonError(
                "integrity-failure", "installed manifest digest changed after install"
            )
        component_path = package / "component.wasm"
        component_info = _regular_file(component_path, maximum=MAX_PACKAGE_BYTES)
        component_digest = _digest(
            app.get("component_sha256"), "installed component digest"
        )
        verification_record = {
            "manifest": {
                "path": "manifest.json",
                "sha256": manifest_digest,
                "size_bytes": len(manifest_bytes),
            },
            "component": {
                "path": "component.wasm",
                "sha256": component_digest,
                "size_bytes": component_info.st_size,
            },
        }
        manifest, actual_package_digest, _total = self._verify_package_tree(
            package, verification_record
        )
        if actual_package_digest != package_digest:
            raise DaemonError(
                "integrity-failure", "installed package digest changed after install"
            )
        return manifest, manifest_digest

    def _installed_manifest(self, app: dict[str, Any]) -> dict[str, Any]:
        manifest, _digest_value = self._verified_installed_manifest(app)
        return manifest

    def _ensure_app_presentation(self, app: dict[str, Any]) -> bool:
        changed = False
        presentation = app.get("presentation")
        publisher_display_name = app.get("publisher_display_name")
        manifest, manifest_digest = self._verified_installed_manifest(app)
        if app.get("manifest_sha256") is None:
            app["manifest_sha256"] = manifest_digest
            changed = True
        identity = manifest.get("app")
        if not isinstance(identity, dict) or not isinstance(identity.get("publisher"), dict):
            raise DaemonError("integrity-failure", "installed manifest identity is incomplete")
        if (
            identity.get("id") != app.get("app_id")
            or identity.get("version") != app.get("version")
            or identity.get("kind") != app.get("kind")
            or identity.get("display_name") != app.get("display_name")
            or identity["publisher"].get("id") != app.get("publisher_id")
        ):
            raise DaemonError(
                "integrity-failure",
                "installed state identity differs from its active manifest",
            )
        description = _bounded_text(identity.get("description"), "app.description", 1000)
        derived_presentation = _host_presentation(
            None, identity["kind"], identity["display_name"], description
        )
        if presentation is None:
            app["presentation"] = derived_presentation
            changed = True
        else:
            normalized = _host_presentation(
                presentation, identity["kind"], identity["display_name"], ""
            )
            if normalized != derived_presentation:
                raise DaemonError(
                    "integrity-failure",
                    "installed presentation differs from active manifest policy",
                )
            if normalized != presentation:
                app["presentation"] = normalized
                changed = True
        if publisher_display_name is None:
            app["publisher_display_name"] = _bounded_text(
                identity["publisher"].get("display_name"),
                "publisher.display_name",
                128,
            )
            changed = True
        elif publisher_display_name != identity["publisher"].get("display_name"):
            raise DaemonError(
                "integrity-failure",
                "installed publisher name differs from active manifest",
            )
        permissions = list(_capability_interfaces(manifest.get("capabilities")))
        if "permissions" not in app:
            app["permissions"] = permissions
            changed = True
        elif app["permissions"] != permissions:
            raise DaemonError(
                "integrity-failure",
                "installed permissions differ from active manifest",
            )
        return changed

    def _ensure_service_entrypoint_metadata(self, app: dict[str, Any]) -> bool:
        manifest = self._installed_manifest(app)
        entrypoints = manifest.get("entrypoints")
        if not isinstance(entrypoints, list) or len(entrypoints) > 16:
            raise DaemonError(
                "integrity-failure",
                "installed manifest entrypoints are malformed",
            )
        expected: list[dict[str, Any]] = []
        for item in entrypoints:
            if not isinstance(item, dict) or item.get("kind") != "service":
                continue
            entrypoint_id = _bounded_text(item.get("id"), "entrypoint.id", 128)
            triggers = item.get("triggers")
            canonical_triggers = [
                trigger
                for trigger in ("on-enable", "scheduler", "manual")
                if isinstance(triggers, list) and trigger in triggers
            ]
            health_interval = item.get("health_check_interval_seconds")
            if (
                not isinstance(triggers, list)
                or not triggers
                or len(triggers) > 3
                or triggers != canonical_triggers
                or isinstance(health_interval, bool)
                or not isinstance(health_interval, int)
                or not 10 <= health_interval <= 86_400
            ):
                raise DaemonError(
                    "integrity-failure",
                    "installed service entrypoint lifecycle metadata is invalid",
                )
            expected.append(
                {
                    "id": entrypoint_id,
                    "kind": "service",
                    "initial_route": None,
                    "triggers": list(triggers),
                    "health_check_interval_seconds": health_interval,
                }
            )
        current = app.get("service_entrypoints")
        if not isinstance(current, list):
            raise DaemonError(
                "integrity-failure",
                "installed service entrypoint state is malformed",
            )
        if current == expected:
            return False
        legacy_identity = [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "initial_route": item.get("initial_route"),
            }
            for item in current
            if isinstance(item, dict)
        ]
        expected_identity = [
            {
                "id": item["id"],
                "kind": item["kind"],
                "initial_route": item["initial_route"],
            }
            for item in expected
        ]
        if len(legacy_identity) != len(current) or legacy_identity != expected_identity:
            raise DaemonError(
                "integrity-failure",
                "installed service entrypoints differ from the active manifest",
            )
        app["service_entrypoints"] = expected
        return True

    def _ensure_app_update_metadata(self, app: dict[str, Any]) -> bool:
        changed = self._ensure_app_presentation(app)
        if "enable_transaction" not in app:
            app["enable_transaction"] = None
            changed = True
        elif app["enable_transaction"] is not None and not isinstance(
            app["enable_transaction"], dict
        ):
            raise DaemonError(
                "integrity-failure", "installed enable transaction metadata is malformed"
            )
        if self._ensure_service_entrypoint_metadata(app):
            changed = True
        state_record = app.get("state")
        update_record = app.get("update")
        capability_digest = app.get("capability_contract_sha256")
        base_valid = (
            isinstance(state_record, dict)
            and state_record.get("schema_version") == APP_STATE_SCHEMA
            and isinstance(state_record.get("schema"), int)
            and not isinstance(state_record.get("schema"), bool)
            and isinstance(state_record.get("revision"), int)
            and not isinstance(state_record.get("revision"), bool)
            and state_record.get("schema", 0) > 0
            and state_record.get("revision", 0) > 0
            and state_record.get("package_digest_sha256") == app.get("package_digest_sha256")
            and isinstance(update_record, dict)
            and update_record.get("schema_version") == UPDATE_SCHEMA
            and isinstance(update_record.get("history"), list)
            and isinstance(capability_digest, str)
            and len(capability_digest) == 64
        )
        if base_valid:
            gc = app.get("gc")
            if gc is None:
                app["gc"] = self._new_gc_state()
                changed = True
            elif (
                not isinstance(gc, dict)
                or gc.get("schema_version") != GC_SCHEMA
                or gc.get("status") not in {"idle", "in-progress"}
                or not isinstance(gc.get("pending"), list)
            ):
                raise DaemonError("integrity-failure", "installed generation GC metadata is malformed")
            settings_record = app.get("settings")
            if settings_record is None:
                app["settings"] = {
                    "schema_version": SETTINGS_SCHEMA,
                    "schema_revision": 1,
                    "config_revision": 1,
                    "values": [],
                }
                changed = True
            else:
                self._settings_snapshot_for_worker(
                    app["app_id"],
                    app,
                    "metadata-validation",
                    state_record["revision"],
                )
            if "kv_storage_limit_bytes" in app:
                kv_limit = app["kv_storage_limit_bytes"]
                if kv_limit is None or (
                    isinstance(kv_limit, int)
                    and not isinstance(kv_limit, bool)
                    and 1 <= kv_limit <= 10 * 1024 * 1024
                ):
                    return changed
                raise DaemonError("integrity-failure", "installed KV storage metadata is malformed")
            manifest = self._installed_manifest(app)
            capabilities = manifest.get("capabilities")
            if not isinstance(capabilities, list):
                raise DaemonError("integrity-failure", "installed manifest lacks capability metadata")
            app["kv_storage_limit_bytes"] = self._kv_storage_limit(capabilities)
            return True
        if any(key in app for key in ("state", "update", "capability_contract_sha256")):
            raise DaemonError("integrity-failure", "installed update or state metadata is malformed")
        manifest = self._installed_manifest(app)
        state_contract = manifest.get("state")
        capabilities = manifest.get("capabilities")
        if not isinstance(state_contract, dict) or not isinstance(capabilities, list):
            raise DaemonError("integrity-failure", "installed manifest lacks state or capability metadata")
        schema = state_contract.get("schema")
        if isinstance(schema, bool) or not isinstance(schema, int) or not 1 <= schema <= 2**32 - 1:
            raise DaemonError("integrity-failure", "installed state schema is invalid")
        app["state"] = {
            "schema_version": APP_STATE_SCHEMA,
            "schema": schema,
            "revision": 1,
            "package_digest_sha256": app["package_digest_sha256"],
        }
        app["capability_contract_sha256"] = _sha256_bytes(canonical_json(capabilities))
        app["kv_storage_limit_bytes"] = self._kv_storage_limit(capabilities)
        app["update"] = self._new_update_state()
        app["gc"] = self._new_gc_state()
        app["settings"] = {
            "schema_version": SETTINGS_SCHEMA,
            "schema_revision": 1,
            "config_revision": 1,
            "values": [],
        }
        changed = True
        return changed

    def _copy_state_tree(self, source: Path, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise DaemonError("integrity-failure", "update state destination already exists")
        destination.mkdir(parents=True, mode=0o700)
        if not source.exists():
            self._fsync_directory(destination)
            return
        source_info = source.lstat()
        if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
            raise DaemonError("integrity-failure", "active app state is not a real directory")
        count = 0
        total = 0
        for root, directories, files in os.walk(source, followlinks=False):
            directories.sort()
            files.sort()
            root_path = Path(root)
            relative_root = root_path.relative_to(source)
            for name in directories:
                child = root_path / name
                info = child.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise DaemonError("integrity-failure", "app state contains a linked directory")
                (destination / relative_root / name).mkdir(mode=0o700)
            for name in files:
                child = root_path / name
                if relative_root == Path(".") and name == "kv-state.lock":
                    # Lock identity is generation-local synchronization metadata,
                    # not guest state; the candidate broker creates its own file.
                    continue
                info = _regular_file(child, maximum=MAX_APP_STATE_BYTES)
                count += 1
                total += info.st_size
                if count > MAX_APP_STATE_FILES or total > MAX_APP_STATE_BYTES:
                    raise DaemonError("resource-limit", "app state exceeds the update snapshot bound")
                data = _read_regular_nofollow(child, MAX_APP_STATE_BYTES)
                target = destination / relative_root / name
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
        directories = [Path(root) for root, _children, _files in os.walk(destination)]
        for directory in reversed(directories):
            self._fsync_directory(directory)

    @staticmethod
    def _kv_state_evidence(directory: Path, maximum_bytes: int) -> dict[str, Any]:
        path = directory / "kv-state.json"
        data = _read_regular_nofollow(path, maximum_bytes)
        document = loads_strict_json(data, maximum=maximum_bytes)
        keys = set(document) if isinstance(document, dict) else set()
        if (
            not isinstance(document, dict)
            or keys not in (
                {"schema_version", "state_revision", "entries"},
                {"schema_version", "state_revision", "entries", "transactions"},
            )
            or document.get("schema_version") != "vibapp.kv-state.experimental-v1"
            or isinstance(document.get("state_revision"), bool)
            or not isinstance(document.get("state_revision"), int)
            or document.get("state_revision", 0) <= 0
            or not isinstance(document.get("entries"), dict)
            or "transactions" in document
            and (
                not isinstance(document["transactions"], dict)
                or len(document["transactions"]) > MAX_IDEMPOTENCY_RECORDS
                or any(
                    not isinstance(key, str)
                    or not key
                    or len(key) > 128
                    or not isinstance(record, dict)
                    or set(record) != {"request_sha256", "state_revision"}
                    or not isinstance(record.get("request_sha256"), str)
                    or len(record["request_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in record["request_sha256"])
                    or isinstance(record.get("state_revision"), bool)
                    or not isinstance(record.get("state_revision"), int)
                    or not 0 < record["state_revision"] <= document.get("state_revision", 0)
                    for key, record in document["transactions"].items()
                )
            )
        ):
            raise DaemonError("integrity-failure", "migrated KV state document is malformed")
        return {
            "state_revision": document["state_revision"],
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
        }

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        sync_directory(path)

    def _remove_package(self, app_id: str, package_digest: str) -> None:
        package = self.packages_root / app_id / package_digest
        if package.exists() and package.is_relative_to(self.packages_root):
            shutil.rmtree(package)

    def _inject_update_fault(self, phase: str) -> None:
        if self._update_fault == phase:
            raise DaemonError("internal", f"injected update fault at {phase}", retryable=True)

    @staticmethod
    def _previous_generation(app: dict[str, Any], running_entrypoints: list[str]) -> dict[str, Any]:
        keys = (
            "version",
            "kind",
            "display_name",
            "publisher_id",
            "publisher_display_name",
            "permissions",
            "presentation",
            "package_digest_sha256",
            "component_sha256",
            "manifest_sha256",
            "runtime_world",
            "package_path",
            "ui_entrypoints",
            "service_entrypoints",
            "allowed_data_dispositions",
            "default_data_disposition",
            "capability_contract_sha256",
            "kv_storage_limit_bytes",
            "state",
            "surfaces",
        )
        return {
            "schema_version": "vibapp.previous-generation.experimental-v1",
            **{key: _copy_json(app[key]) for key in keys},
            "running_service_entrypoints": running_entrypoints,
        }

    @staticmethod
    def _restore_previous_metadata(app: dict[str, Any], previous: dict[str, Any]) -> None:
        if previous.get("schema_version") != "vibapp.previous-generation.experimental-v1":
            raise DaemonError("integrity-failure", "previous generation record is malformed")
        for key in (
            "version",
            "kind",
            "display_name",
            "publisher_id",
            "publisher_display_name",
            "permissions",
            "presentation",
            "package_digest_sha256",
            "component_sha256",
            "runtime_world",
            "package_path",
            "ui_entrypoints",
            "service_entrypoints",
            "allowed_data_dispositions",
            "default_data_disposition",
            "capability_contract_sha256",
            "kv_storage_limit_bytes",
            "state",
            "surfaces",
        ):
            if key not in previous:
                raise DaemonError("integrity-failure", "previous generation record is incomplete")
            app[key] = _copy_json(previous[key])
        # Older durable update journals predate this redundant digest binding.
        # Recovery may safely backfill it only after the restored package digest
        # has been independently recomputed by _verified_installed_manifest.
        if "manifest_sha256" in previous:
            app["manifest_sha256"] = _copy_json(previous["manifest_sha256"])
        else:
            app.pop("manifest_sha256", None)

    @staticmethod
    def _append_update_history(app: dict[str, Any], transaction: dict[str, Any]) -> None:
        history = app["update"].setdefault("history", [])
        history.append(_copy_json(transaction))
        del history[:-MAX_UPDATE_HISTORY]
        app["update"]["last_transaction"] = _copy_json(transaction)

    def _update_path(self, value: Any, field: str) -> Path:
        relative = _safe_relative_path(value)
        path = self.root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_relative_to(self.updates_root):
            raise DaemonError("integrity-failure", f"{field} escaped update storage")
        return path

    @staticmethod
    def _format_utc(value: dt.datetime) -> str:
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _replace_update_history(app: dict[str, Any], transaction: dict[str, Any]) -> None:
        history = app["update"].setdefault("history", [])
        transaction_id = transaction.get("transaction_id")
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("transaction_id") == transaction_id:
                history[index] = _copy_json(transaction)
                break
        else:
            history.append(_copy_json(transaction))
            del history[:-MAX_UPDATE_HISTORY]
        app["update"]["last_transaction"] = _copy_json(transaction)

    def _expire_observation_window(
        self,
        state: dict[str, Any],
        app_id: str,
        app: dict[str, Any],
    ) -> bool:
        update = app.get("update")
        rollback = update.get("previous") if isinstance(update, dict) else None
        if not isinstance(update, dict) or update.get("status") != "observation-window":
            return False
        if not isinstance(rollback, dict) or rollback.get("schema_version") != UPDATE_SCHEMA:
            raise DaemonError("integrity-failure", "observation window lacks its rollback generation")
        transaction = update.get("last_transaction")
        if not isinstance(transaction, dict):
            raise DaemonError("integrity-failure", "observation window lacks its durable transaction")
        started_at = rollback.get("observation_started_at_utc")
        deadline_at = rollback.get("observation_deadline_utc")
        changed = False
        if started_at is None or deadline_at is None:
            started_at = transaction.get("completed_at_utc")
            started = _parse_utc(started_at)
            deadline_at = self._format_utc(
                started + dt.timedelta(seconds=OBSERVATION_WINDOW_SECONDS)
            )
            rollback["observation_started_at_utc"] = started_at
            rollback["observation_deadline_utc"] = deadline_at
            transaction["observation_started_at_utc"] = started_at
            transaction["observation_deadline_utc"] = deadline_at
            self._replace_update_history(app, transaction)
            changed = True
        started = _parse_utc(started_at)
        deadline = _parse_utc(deadline_at)
        if deadline != started + dt.timedelta(seconds=OBSERVATION_WINDOW_SECONDS):
            raise DaemonError("integrity-failure", "observation deadline is not bound to its fixed window")
        observed_at_text = self.clock()
        observed_at = _parse_utc(observed_at_text)
        if observed_at < started:
            return changed
        if observed_at < deadline:
            return changed
        transaction["status"] = "committed"
        transaction["phase"] = "committed"
        transaction["observation_completed_at_utc"] = observed_at_text
        transaction["rollback_retained"] = True
        update["status"] = "committed"
        rollback["retention"] = "last-rollback-generation"
        self._replace_update_history(app, transaction)
        self._append_audit(
            state,
            "update-observation-complete",
            principal="daemon",
            app_id=app_id,
            result="committed",
            fields={
                "active_package_digest_sha256": app["package_digest_sha256"],
                "rollback_package_digest_sha256": rollback.get(
                    "from_package_digest_sha256"
                ),
                "observation_deadline_utc": deadline_at,
            },
        )
        return True

    def _generation_gc_targets(self, app_id: str, app: dict[str, Any]) -> list[dict[str, str]]:
        protected_packages = {app["package_digest_sha256"]}
        protected_updates: set[str] = set()
        update = app.get("update")
        if isinstance(update, dict):
            for record in (update.get("pending"), update.get("previous")):
                if not isinstance(record, dict):
                    continue
                for field in (
                    "from_package_digest_sha256",
                    "to_package_digest_sha256",
                ):
                    digest = record.get(field)
                    if isinstance(digest, str) and len(digest) == 64:
                        protected_packages.add(_digest(digest, "retained package digest"))
                root = record.get("transaction_root")
                if root is not None:
                    protected_updates.add(
                        str(self._update_path(root, "transaction_root").relative_to(self.root))
                    )
        enable_transaction = app.get("enable_transaction")
        if isinstance(enable_transaction, dict):
            root = enable_transaction.get("transaction_root")
            if root is not None:
                protected_updates.add(
                    str(
                        self._update_path(
                            root, "enable_transaction.transaction_root"
                        ).relative_to(self.root)
                    )
                )
        targets: list[dict[str, str]] = []
        package_root = self.packages_root / app_id
        if package_root.exists():
            if not package_root.is_dir() or package_root.is_symlink():
                raise DaemonError("integrity-failure", "app package generation root is unsafe")
            children = sorted(package_root.iterdir(), key=lambda path: path.name)
            if len(children) > MAX_PACKAGE_FILES:
                raise DaemonError("resource-limit", "app package generation inventory is unbounded")
            for child in children:
                if child.name in protected_packages:
                    continue
                if not child.is_dir() or child.is_symlink():
                    raise DaemonError("integrity-failure", "package generation is not a real directory")
                targets.append(
                    {
                        "kind": "package",
                        "path": str(child.relative_to(self.root)),
                    }
                )
        update_root = self.updates_root / app_id
        if update_root.exists():
            if not update_root.is_dir() or update_root.is_symlink():
                raise DaemonError("integrity-failure", "app update generation root is unsafe")
            children = sorted(update_root.iterdir(), key=lambda path: path.name)
            if len(children) > MAX_UPDATE_HISTORY + MAX_GC_DELETIONS_PER_PASS:
                raise DaemonError("resource-limit", "app update generation inventory is unbounded")
            for child in children:
                relative = str(child.relative_to(self.root))
                if relative in protected_updates:
                    continue
                if not child.is_dir() or child.is_symlink():
                    raise DaemonError("integrity-failure", "update generation is not a real directory")
                targets.append({"kind": "update", "path": relative})
        return targets

    def _garbage_collect_app(
        self,
        state: dict[str, Any],
        app_id: str,
        app: dict[str, Any],
    ) -> bool:
        gc = app.get("gc")
        if not isinstance(gc, dict) or gc.get("schema_version") != GC_SCHEMA:
            raise DaemonError("integrity-failure", "generation GC metadata is unavailable")
        pending = gc.get("pending") if gc.get("status") == "in-progress" else None
        if not isinstance(pending, list) or not pending:
            pending = self._generation_gc_targets(app_id, app)[:MAX_GC_DELETIONS_PER_PASS]
            if not pending:
                if gc.get("status") != "idle" or gc.get("pending"):
                    gc["status"] = "idle"
                    gc["pending"] = []
                    return True
                return False
            gc["status"] = "in-progress"
            gc["pending"] = _copy_json(pending)
            self._write_state(state)
        completed_at = self.clock()
        _parse_utc(completed_at)
        removed: list[str] = []
        while gc["pending"]:
            target = gc["pending"][0]
            if (
                not isinstance(target, dict)
                or set(target) != {"kind", "path"}
                or target.get("kind") not in {"package", "update"}
            ):
                raise DaemonError("integrity-failure", "generation GC journal is malformed")
            relative = _safe_relative_path(target.get("path"))
            path = self.root.joinpath(*PurePosixPath(relative).parts)
            allowed_root = self.packages_root if target["kind"] == "package" else self.updates_root
            if not path.is_relative_to(allowed_root):
                raise DaemonError("integrity-failure", "generation GC target escaped storage")
            removable = {
                item["path"] for item in self._generation_gc_targets(app_id, app)
            }
            if relative in removable and path.exists():
                if not path.is_dir() or path.is_symlink():
                    raise DaemonError("integrity-failure", "generation GC target became unsafe")
                shutil.rmtree(path)
                removed.append(relative)
                self._inject_update_fault("during-gc")
            gc["pending"].pop(0)
            self._write_state(state)
        gc["status"] = "idle"
        gc["last_completed_at_utc"] = completed_at
        gc["last_removed"] = removed
        self._write_state(state)
        return True

    def _recover_pending_enable(
        self, state: dict[str, Any], app_id: str, app: dict[str, Any]
    ) -> bool:
        transaction = app.get("enable_transaction")
        if transaction is None:
            return False
        expected_fields = {
            "schema_version",
            "transaction_id",
            "status",
            "app_id",
            "package_digest_sha256",
            "transaction_root",
            "candidate_state_path",
            "previous_state_path",
            "original_state_revision",
            "service_entrypoints",
            "started_at_utc",
        }
        if not isinstance(transaction, dict) or set(transaction) != expected_fields:
            raise DaemonError("integrity-failure", "pending enable journal is malformed")
        if transaction.get("schema_version") != ENABLE_SCHEMA:
            raise DaemonError("integrity-failure", "pending enable journal schema is unsupported")
        transaction_id = _bounded_text(
            transaction.get("transaction_id"), "enable transaction ID", 64
        )
        if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            raise DaemonError("integrity-failure", "pending enable transaction ID is invalid")
        status = transaction.get("status")
        if status not in {"preparing", "switching", "committed"}:
            raise DaemonError("integrity-failure", "pending enable status is invalid")
        if transaction.get("app_id") != app_id:
            raise DaemonError("integrity-failure", "pending enable app binding changed")
        if (
            _digest(
                transaction.get("package_digest_sha256"),
                "enable package digest",
            )
            != app.get("package_digest_sha256")
        ):
            raise DaemonError("integrity-failure", "pending enable package binding changed")
        _parse_utc(transaction.get("started_at_utc"))
        original_revision = transaction.get("original_state_revision")
        if (
            isinstance(original_revision, bool)
            or not isinstance(original_revision, int)
            or original_revision <= 0
        ):
            raise DaemonError("integrity-failure", "pending enable state revision is invalid")
        entrypoints = transaction.get("service_entrypoints")
        expected_entrypoints = sorted(
            item["id"]
            for item in app.get("service_entrypoints", [])
            if isinstance(item, dict) and "on-enable" in item.get("triggers", [])
        )
        if entrypoints != expected_entrypoints:
            raise DaemonError("integrity-failure", "pending enable entrypoint binding changed")

        transaction_root = self._update_path(
            transaction.get("transaction_root"), "enable transaction root"
        )
        candidate_state = self._update_path(
            transaction.get("candidate_state_path"), "enable candidate state"
        )
        previous_state = self._update_path(
            transaction.get("previous_state_path"), "enable previous state"
        )
        expected_root = self.updates_root / app_id / f"enable-{transaction_id}"
        if (
            transaction_root != expected_root
            or candidate_state != transaction_root / "candidate-state"
            or previous_state != transaction_root / "previous-state"
        ):
            raise DaemonError("integrity-failure", "pending enable paths are not canonical")
        active_state = self.data_root / app_id

        if status == "committed":
            if not app.get("enabled"):
                raise DaemonError(
                    "integrity-failure", "committed enable journal lacks enabled state"
                )
            if (
                not active_state.exists()
                or not active_state.is_dir()
                or active_state.is_symlink()
                or app["state"]["revision"] < original_revision
            ):
                raise DaemonError(
                    "integrity-failure", "committed enable active state is unavailable"
                )
            if transaction_root.exists():
                if not transaction_root.is_dir() or transaction_root.is_symlink():
                    raise DaemonError(
                        "integrity-failure", "committed enable transaction root is unsafe"
                    )
                shutil.rmtree(transaction_root)
            app["enable_transaction"] = None
            self._append_audit(
                state,
                "enable-recovery",
                principal="daemon",
                app_id=app_id,
                result="committed",
                fields={"transaction_id": transaction_id},
            )
            return True

        if app.get("enabled"):
            raise DaemonError(
                "integrity-failure", "uncommitted enable journal has enabled state"
            )
        if previous_state.exists():
            if not previous_state.is_dir() or previous_state.is_symlink():
                raise DaemonError(
                    "integrity-failure", "pending enable previous state is unsafe"
                )
            if active_state.exists() or active_state.is_symlink():
                if not active_state.is_dir() or active_state.is_symlink():
                    raise DaemonError(
                        "integrity-failure", "pending enable active state is unsafe"
                    )
                shutil.rmtree(active_state)
            os.replace(previous_state, active_state)
            self._fsync_directory(self.data_root)
        elif (
            not active_state.exists()
            or not active_state.is_dir()
            or active_state.is_symlink()
        ):
            raise DaemonError(
                "integrity-failure", "pending enable lacks its original active state"
            )
        if transaction_root.exists():
            if not transaction_root.is_dir() or transaction_root.is_symlink():
                raise DaemonError(
                    "integrity-failure", "pending enable transaction root is unsafe"
                )
            shutil.rmtree(transaction_root)
        app["enable_transaction"] = None
        self._append_audit(
            state,
            "enable-recovery",
            principal="daemon",
            app_id=app_id,
            result="rolled-back",
            fields={"transaction_id": transaction_id, "phase": status},
        )
        return True

    def _recover_pending_update(self, state: dict[str, Any], app_id: str, app: dict[str, Any]) -> bool:
        pending = app["update"].get("pending")
        if pending is None:
            return False
        if not isinstance(pending, dict) or pending.get("schema_version") != UPDATE_SCHEMA:
            raise DaemonError("integrity-failure", "pending update journal is malformed")
        previous = pending.get("previous")
        if not isinstance(previous, dict):
            raise DaemonError("integrity-failure", "pending update lacks a previous generation")
        active_state = self.data_root / app_id
        previous_state = self._update_path(pending.get("previous_state_path"), "previous_state_path")
        candidate_state = self._update_path(
            pending.get("candidate_state_path"),
            "candidate_state_path",
        )
        transaction_root = self._update_path(pending.get("transaction_root"), "transaction_root")
        if previous_state.exists():
            if not previous_state.is_dir() or previous_state.is_symlink():
                raise DaemonError("integrity-failure", "pending update previous state is unsafe")
            if active_state.exists():
                shutil.rmtree(active_state)
            os.replace(previous_state, active_state)
            self._fsync_directory(self.data_root)
        elif not candidate_state.is_dir() or candidate_state.is_symlink():
            raise DaemonError(
                "integrity-failure",
                "pending update lacks both a previous snapshot and an unswapped candidate snapshot",
            )
        elif not active_state.is_dir() or active_state.is_symlink():
            raise DaemonError("integrity-failure", "pending update active state is unavailable")
        self._restore_previous_metadata(app, previous)
        candidate_digest = _digest(
            pending.get("to_package_digest_sha256"),
            "pending.to_package_digest_sha256",
        )
        self._remove_package(app_id, candidate_digest)
        if transaction_root.exists():
            shutil.rmtree(transaction_root)
        transaction = {
            **{key: _copy_json(value) for key, value in pending.items() if key != "previous"},
            "status": "rolled-back",
            "completed_at_utc": self.clock(),
            "rollback_reason": "daemon-recovery-restored-previous-generation",
        }
        app["update"]["status"] = "rolled-back"
        app["update"]["pending"] = None
        app["update"]["previous"] = None
        self._append_update_history(app, transaction)
        app["lifecycle_state"] = "enabled" if app.get("enabled") else "disabled"
        app["last_error"] = "internal"
        self._append_audit(
            state,
            "update-recovery",
            principal="daemon",
            app_id=app_id,
            result="rolled-back",
            fields={
                "from_package_digest_sha256": previous["package_digest_sha256"],
                "rejected_package_digest_sha256": candidate_digest,
                "rollback_reason": transaction["rollback_reason"],
            },
        )
        return True

    @staticmethod
    def _parse_envelope(envelope: Any) -> tuple[str, str, str, str | None, str, Any]:
        if not isinstance(envelope, dict) or set(envelope) != {"request_id", "idempotency_key", "client", "subject", "command"}:
            raise DaemonError("invalid-argument", "daemon request fields do not match the canonical envelope")
        request_id = _bounded_text(envelope["request_id"], "request_id", 128)
        idempotency_key = _bounded_text(envelope["idempotency_key"], "idempotency_key", 256)
        client = envelope["client"]
        if client not in ("launcher", "cli", "web"):
            raise DaemonError("invalid-argument", "client is not a closed client-kind")
        subject = envelope["subject"]
        if subject is not None:
            subject = _safe_app_id(subject)
        command = envelope["command"]
        if not isinstance(command, dict) or set(command) != {"tag", "value"}:
            raise DaemonError("invalid-argument", "command is not a canonical tag/value variant")
        tag = _bounded_text(command["tag"], "command.tag", 64)
        return request_id, idempotency_key, client, subject, tag, command["value"]

    @staticmethod
    def _authorize(subject: str | None, tag: str, allowed_apps: set[str]) -> None:
        if tag == "install":
            if subject is not None:
                raise DaemonError("invalid-argument", "install has no installed-app subject")
            return
        if subject is None:
            raise DaemonError("invalid-argument", "installed-app command requires a subject")
        if "*" not in allowed_apps and subject not in allowed_apps:
            raise DaemonError("forged-identifier", "subject is outside the authenticated app scope")

    @staticmethod
    def _mutating(tag: str) -> bool:
        return tag in {"install", "update", "enable", "disable", "service-start", "service-stop", "service-trigger", "service-health", "launch", "ui-action", "surface-close", "uninstall"}

    @classmethod
    def _writes_state(cls, tag: str) -> bool:
        return cls._mutating(tag) or tag == "ui-render-failure"

    def execute(self, envelope: Any, *, principal: str, allowed_apps: set[str] | None = None, promotion_record: Path | str | None = None) -> dict[str, Any]:
        request_id = "unknown"
        subject: str | None = None
        tag = "invalid"
        try:
            request_id, idempotency_key, _client, subject, tag, value = self._parse_envelope(envelope)
            allowed = allowed_apps or {"*"}
            self._authorize(subject, tag, allowed)
            if tag not in {"install", "update"} and promotion_record is not None:
                raise DaemonError("invalid-argument", "promotion record is accepted only for install or update")
            with self._locked_state() as state:
                maintenance_changed = False
                for maintenance_app_id, maintenance_app in state["apps"].items():
                    if self._ensure_app_update_metadata(maintenance_app):
                        maintenance_changed = True
                    if self._expire_observation_window(
                        state,
                        maintenance_app_id,
                        maintenance_app,
                    ):
                        maintenance_changed = True
                if maintenance_changed:
                    self._write_state(state)
                for maintenance_app_id, maintenance_app in state["apps"].items():
                    self._garbage_collect_app(
                        state,
                        maintenance_app_id,
                        maintenance_app,
                    )
                normalized_payload = canonical_json({"subject": subject, "tag": tag, "value": value})
                dedup_namespace = f"{principal}\u0000{subject or '-'}\u0000{tag}\u0000{idempotency_key}"
                payload_digest = _sha256_bytes(normalized_payload)
                if self._mutating(tag) and dedup_namespace in state["idempotency"]:
                    previous = state["idempotency"][dedup_namespace]
                    if previous["payload_sha256"] != payload_digest:
                        raise DaemonError("conflict", "idempotency key was reused with a different payload")
                    return {"request_id": request_id, "outcome": _copy_json(previous["outcome"])}
                outcome = self._dispatch(state, subject, tag, value, principal, promotion_record)
                if self._mutating(tag):
                    if len(state["idempotency"]) >= MAX_IDEMPOTENCY_RECORDS:
                        raise DaemonError("resource-limit", "idempotency ledger is full")
                    state["idempotency"][dedup_namespace] = {"payload_sha256": payload_digest, "outcome": _copy_json(outcome)}
                if self._writes_state(tag):
                    self._write_state(state)
                return {"request_id": request_id, "outcome": outcome}
        except DaemonError as error:
            with contextlib.suppress(Exception):
                with self._locked_state() as state:
                    self._append_audit(state, f"{tag}-rejected", principal=principal, app_id=subject, result=error.code)
                    self._write_state(state)
            return {"request_id": request_id, "error": error.as_dict()}

    def _dispatch(self, state: dict[str, Any], subject: str | None, tag: str, value: Any, principal: str, promotion_record: Path | str | None) -> dict[str, Any]:
        if tag == "install":
            return self._install(state, value, principal, promotion_record)
        if subject is None or subject not in state["apps"]:
            raise DaemonError("not-found", "installed app was not found")
        if tag == "update":
            return self._update(state, subject, value, principal, promotion_record)
        if tag == "enable":
            return self._enable(state, subject, value, principal)
        if tag == "disable":
            return self._disable(state, subject, value, principal)
        if tag == "service-start":
            return self._service_start(state, subject, value, principal)
        if tag == "service-stop":
            return self._service_stop(state, subject, value, principal)
        if tag == "service-trigger":
            return self._service_trigger(state, subject, value, principal)
        if tag == "service-health":
            return self._service_health(state, subject, value, principal)
        if tag == "launch":
            return self._launch(state, subject, value, principal)
        if tag == "ui-action":
            return self._ui_action(state, subject, value, principal)
        if tag == "ui-refresh":
            return self._ui_refresh(state, subject, value, principal)
        if tag == "ui-render-failure":
            return self._ui_render_failure(state, subject, value, principal)
        if tag == "surface-close":
            return self._surface_close(state, subject, value, principal)
        if tag == "status":
            return self._status(state, subject, value)
        if tag == "uninstall":
            return self._uninstall(state, subject, value, principal)
        raise DaemonError("invalid-argument", "command is not implemented by the bounded product daemon")

    def _install(self, state: dict[str, Any], value: Any, principal: str, promotion_record: Path | str | None) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"package_digest_sha256", "enable_after_install"}:
            raise DaemonError("invalid-argument", "install request fields are invalid")
        package_digest = _digest(value["package_digest_sha256"], "package_digest_sha256")
        if value["enable_after_install"] is not False:
            raise DaemonError("consent-required", "verified packages are staged disabled; enable is a separate command")
        if promotion_record is None:
            raise DaemonError("consent-required", "install requires an explicit verifier promotion record")
        candidate = self._verify_candidate(promotion_record, package_digest)
        if candidate.app_id in state["apps"]:
            existing = state["apps"][candidate.app_id]
            if existing["package_digest_sha256"] != package_digest:
                raise DaemonError("conflict", "an update requires the separate update transaction")
            return {"tag": "accepted", "value": None}
        record = loads_strict_json(candidate.record_path.read_bytes(), maximum=MAX_RECORD_BYTES)
        installed_dir = self._stage_package(candidate, record)
        state["apps"][candidate.app_id] = {
            "app_id": candidate.app_id,
            "version": candidate.app_version,
            "kind": candidate.app_kind,
            "display_name": candidate.display_name,
            "publisher_id": candidate.publisher_id,
            "publisher_display_name": candidate.publisher_display_name,
            "permissions": list(candidate.permissions),
            "presentation": _copy_json(candidate.presentation),
            "package_digest_sha256": candidate.package_digest,
            "component_sha256": candidate.component_digest,
            "manifest_sha256": candidate.manifest_digest,
            "runtime_world": candidate.runtime_world,
            "package_path": str(installed_dir.relative_to(self.root)),
            "enabled": False,
            "lifecycle_state": "installed-disabled",
            "quarantined": False,
            "last_error": None,
            "ui_entrypoints": list(candidate.ui_entrypoints),
            "service_entrypoints": list(candidate.service_entrypoints),
            "allowed_data_dispositions": list(candidate.allowed_dispositions),
            "default_data_disposition": candidate.default_disposition,
            "services": {},
            "surfaces": {},
            "service_instance_sequence": 0,
            "surface_sequence": 0,
            "crash_events": [],
            "installed_at_utc": self.clock(),
            "guest_execution_performed": False,
            "enable_transaction": None,
            "capability_contract_sha256": candidate.capability_contract_sha256,
            "kv_storage_limit_bytes": candidate.kv_storage_limit_bytes,
            "state": {
                "schema_version": APP_STATE_SCHEMA,
                "schema": candidate.state_schema,
                "revision": 1,
                "package_digest_sha256": candidate.package_digest,
            },
            "update": {
                "schema_version": UPDATE_SCHEMA,
                "status": "none",
                "pending": None,
                "previous": None,
                "last_transaction": None,
                "history": [],
            },
            "gc": self._new_gc_state(),
            "settings": {
                "schema_version": SETTINGS_SCHEMA,
                "schema_revision": 1,
                "config_revision": 1,
                "values": [],
            },
        }
        (self.data_root / candidate.app_id).mkdir(parents=True, exist_ok=True, mode=0o700)
        self._append_audit(state, "install", principal=principal, app_id=candidate.app_id, result="installed-disabled", fields={"package_digest_sha256": candidate.package_digest, "verifier": "independent-verifier"})
        return {"tag": "accepted", "value": None}

    @staticmethod
    def _candidate_generation_app(
        current: dict[str, Any],
        candidate: VerifiedCandidate,
        installed_dir: Path,
        runtime_root: Path,
        target_state_revision: int,
    ) -> dict[str, Any]:
        result = _copy_json(current)
        result.update(
            {
                "version": candidate.app_version,
                "kind": candidate.app_kind,
                "display_name": candidate.display_name,
                "publisher_id": candidate.publisher_id,
                "publisher_display_name": candidate.publisher_display_name,
                "permissions": list(candidate.permissions),
                "presentation": _copy_json(candidate.presentation),
                "package_digest_sha256": candidate.package_digest,
                "component_sha256": candidate.component_digest,
                "manifest_sha256": candidate.manifest_digest,
                "runtime_world": candidate.runtime_world,
                "package_path": str(installed_dir.relative_to(runtime_root)),
                "ui_entrypoints": list(candidate.ui_entrypoints),
                "service_entrypoints": list(candidate.service_entrypoints),
                "allowed_data_dispositions": list(candidate.allowed_dispositions),
                "default_data_disposition": candidate.default_disposition,
                "capability_contract_sha256": candidate.capability_contract_sha256,
                "kv_storage_limit_bytes": candidate.kv_storage_limit_bytes,
                "state": {
                    "schema_version": APP_STATE_SCHEMA,
                    "schema": candidate.state_schema,
                    "revision": target_state_revision,
                    "package_digest_sha256": candidate.package_digest,
                },
                "services": {},
                "surfaces": _copy_json(current.get("surfaces", {})),
            }
        )
        return result

    @staticmethod
    def _settings_snapshot_for_worker(
        app_id: str,
        app: dict[str, Any],
        generation: str,
        state_revision: int,
    ) -> dict[str, Any]:
        settings_record = app.get("settings")
        if (
            not isinstance(settings_record, dict)
            or settings_record.get("schema_version") != SETTINGS_SCHEMA
            or set(settings_record) != {
                "schema_version",
                "schema_revision",
                "config_revision",
                "values",
            }
            or isinstance(settings_record.get("schema_revision"), bool)
            or not isinstance(settings_record.get("schema_revision"), int)
            or settings_record.get("schema_revision", 0) <= 0
            or isinstance(settings_record.get("config_revision"), bool)
            or not isinstance(settings_record.get("config_revision"), int)
            or settings_record.get("config_revision", 0) <= 0
            or not isinstance(settings_record.get("values"), list)
            or len(settings_record["values"]) > MAX_SETTINGS_VALUES
        ):
            raise DaemonError("integrity-failure", "installed settings snapshot metadata is malformed")
        return {
            **_copy_json(settings_record),
            "app_id": app_id,
            "package_digest_sha256": app["package_digest_sha256"],
            "generation": generation,
            "state_revision": state_revision,
        }

    def _update(
        self,
        state: dict[str, Any],
        app_id: str,
        value: Any,
        principal: str,
        promotion_record: Path | str | None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"package_digest_sha256"}:
            raise DaemonError("invalid-argument", "update request fields are invalid")
        package_digest = _digest(value["package_digest_sha256"], "package_digest_sha256")
        if promotion_record is None:
            raise DaemonError("consent-required", "update requires an explicit verifier promotion record")
        app = state["apps"][app_id]
        self._ensure_app_update_metadata(app)
        if package_digest == app["package_digest_sha256"]:
            return {"tag": "accepted", "value": None}
        if app["update"].get("status") == "observation-window":
            raise DaemonError(
                "upgrade-in-progress",
                "another candidate is still inside its rollback observation window",
            )
        candidate = self._verify_candidate(promotion_record, package_digest)
        if candidate.app_id != app_id:
            raise DaemonError("forged-identifier", "update candidate belongs to another app")
        if candidate.publisher_id != app["publisher_id"]:
            raise DaemonError("consent-required", "publisher changes require a separate trust decision")
        if candidate.app_kind != app["kind"] or candidate.runtime_world != app["runtime_world"]:
            raise DaemonError("incompatible-contract", "bounded update cannot change app kind or runtime world")
        current_ui = {
            (
                item["id"],
                item["kind"],
                item.get("initial_route"),
            )
            for item in app["ui_entrypoints"]
        }
        current_services = {
            (
                item["id"],
                item["kind"],
                tuple(item.get("triggers", [])),
                item.get("health_check_interval_seconds"),
            )
            for item in app["service_entrypoints"]
        }
        candidate_ui = {
            (
                item["id"],
                item["kind"],
                item.get("initial_route"),
            )
            for item in candidate.ui_entrypoints
        }
        candidate_services = {
            (
                item["id"],
                item["kind"],
                tuple(item.get("triggers", [])),
                item.get("health_check_interval_seconds"),
            )
            for item in candidate.service_entrypoints
        }
        if current_ui != candidate_ui or current_services != candidate_services:
            raise DaemonError("incompatible-contract", "bounded update cannot change entrypoint identity")
        if candidate.capability_contract_sha256 != app["capability_contract_sha256"]:
            raise DaemonError("consent-required", "capability changes require a separate permission plan")
        source_schema = app["state"]["schema"]
        source_state_revision = app["state"]["revision"]
        if not candidate.migratable_from_min <= source_schema <= candidate.migratable_from_max:
            raise DaemonError("incompatible-contract", "candidate cannot migrate the installed state schema")
        if candidate.state_schema < source_schema:
            raise DaemonError("incompatible-contract", "update cannot silently downgrade the state schema")
        running_entrypoints = sorted(
            entrypoint
            for entrypoint, service in app["services"].items()
            if service.get("state") == "running"
        )
        if len(running_entrypoints) > MAX_ACTIVE_SERVICE_WORKERS_PER_APP:
            raise DaemonError(
                "resource-limit",
                "app has more live services than its per-app generation ceiling",
            )
        validation_entrypoints = (
            running_entrypoints
            if running_entrypoints
            else [candidate.service_entrypoints[0]["id"]]
            if candidate.service_entrypoints
            else [candidate.ui_entrypoints[0]["id"]]
        )
        record = loads_strict_json(candidate.record_path.read_bytes(), maximum=MAX_RECORD_BYTES)
        installed_dir = self._stage_package(candidate, record)
        target_state_revision = source_state_revision + 1
        transaction_id = f"update-{uuid.uuid4().hex}"
        transaction_root = self.updates_root / app_id / transaction_id
        candidate_state = transaction_root / "candidate-state"
        previous_state = transaction_root / "previous-state"
        active_state = self.data_root / app_id
        try:
            transaction_root.mkdir(parents=True, mode=0o700)
            self._copy_state_tree(active_state, candidate_state)
        except Exception:
            if transaction_root.exists():
                shutil.rmtree(transaction_root)
            self._remove_package(app_id, candidate.package_digest)
            raise
        previous = self._previous_generation(app, running_entrypoints)
        transaction: dict[str, Any] = {
            "schema_version": UPDATE_SCHEMA,
            "transaction_id": transaction_id,
            "status": "prepared",
            "phase": "prepared",
            "from_package_digest_sha256": app["package_digest_sha256"],
            "to_package_digest_sha256": candidate.package_digest,
            "from_version": app["version"],
            "to_version": candidate.app_version,
            "from_state_schema": source_schema,
            "to_state_schema": candidate.state_schema,
            "source_state_revision": source_state_revision,
            "target_state_revision": target_state_revision,
            "migration": {"status": "pending", "target_state_revision": target_state_revision},
            "activation_health": {"status": "pending", "checks": []},
            "rollback_reason": None,
            "error": None,
            "started_at_utc": self.clock(),
            "completed_at_utc": None,
            "transaction_root": str(transaction_root.relative_to(self.root)),
            "candidate_state_path": str(candidate_state.relative_to(self.root)),
            "previous_state_path": str(previous_state.relative_to(self.root)),
            "previous": previous,
        }
        original_app = _copy_json(app)
        app["update"]["status"] = "prepared"
        app["update"]["pending"] = _copy_json(transaction)
        app["lifecycle_state"] = "update-prepared"
        self._append_audit(
            state,
            "update-staged",
            principal=principal,
            app_id=app_id,
            result="prepared",
            fields={
                "from_package_digest_sha256": transaction["from_package_digest_sha256"],
                "to_package_digest_sha256": transaction["to_package_digest_sha256"],
                "from_version": transaction["from_version"],
                "to_version": transaction["to_version"],
            },
        )
        try:
            self._write_state(state)
        except Exception:
            app.clear()
            app.update(original_app)
            if transaction_root.exists():
                shutil.rmtree(transaction_root)
            self._remove_package(app_id, candidate.package_digest)
            raise

        candidate_app = self._candidate_generation_app(
            app,
            candidate,
            installed_dir,
            self.root,
            target_state_revision,
        )
        shadow_services: dict[str, dict[str, Any]] = {}
        shadow_workers: dict[str, ServiceWorker] = {}
        old_quiesced = False
        state_swapped = False
        try:
            self._inject_update_fault("after-journal")
            transaction["phase"] = "migration"

            def record_migration(migration: dict[str, Any]) -> None:
                if (
                    migration.get("tag") != "migration"
                    or migration.get("status") not in {"unchanged", "migrated"}
                    or migration.get("target_state_revision") != target_state_revision
                    or candidate.kv_storage_limit_bytes is not None
                    and migration.get("kv_state_revision") != target_state_revision
                ):
                    raise DaemonError(
                        "malformed-output",
                        "guest migration result is not bound to the isolated target revision",
                    )
                persisted_migration = _copy_json(migration)
                if candidate.kv_storage_limit_bytes is not None:
                    evidence = self._kv_state_evidence(
                        candidate_state,
                        candidate.kv_storage_limit_bytes,
                    )
                    if evidence["state_revision"] != target_state_revision:
                        raise DaemonError(
                            "stale-revision",
                            "migrated KV bytes were not committed at the target revision",
                        )
                    persisted_migration["kv_state_sha256"] = evidence["sha256"]
                    persisted_migration["kv_state_bytes"] = evidence["bytes"]
                transaction["migration"] = persisted_migration
                transaction["phase"] = "post-migration"
                self._inject_update_fault("after-migration")
                transaction["phase"] = "activation"

            validation_reports: list[dict[str, Any]] = []
            for index, validation_entrypoint in enumerate(validation_entrypoints):
                activate_service = candidate.runtime_world != "ui-only-reference"
                shadow_service, shadow_worker = self._create_service_generation(
                    app_id,
                    candidate_app,
                    validation_entrypoint,
                    reason="update",
                    state_directory=candidate_state,
                    state_revision=(
                        source_state_revision if index == 0 else target_state_revision
                    ),
                    migration=(
                        {
                            "from_schema": source_schema,
                            "to_schema": candidate.state_schema,
                            "source_state_revision": source_state_revision,
                            "target_state_revision": target_state_revision,
                        }
                        if index == 0
                        else None
                    ),
                    on_migrated=record_migration if index == 0 else None,
                    activate_service=activate_service,
                    replaces_active_generation=(
                        validation_entrypoint in running_entrypoints
                    ),
                )
                validation_reports.append(
                    {
                        "entrypoint": validation_entrypoint,
                        "status": shadow_service["health"]["status"],
                        "checks": _copy_json(shadow_service["health"].get("checks", [])),
                    }
                )
                if validation_entrypoint in running_entrypoints:
                    shadow_services[validation_entrypoint] = shadow_service
                    shadow_workers[validation_entrypoint] = shadow_worker
                elif activate_service:
                    shadow_worker.stop("update")
                else:
                    shadow_worker.terminate()
            activated_state_revision = target_state_revision
            if candidate.kv_storage_limit_bytes is not None:
                activated_evidence = self._kv_state_evidence(
                    candidate_state,
                    candidate.kv_storage_limit_bytes,
                )
                activated_state_revision = activated_evidence["state_revision"]
                if activated_state_revision < target_state_revision:
                    raise DaemonError(
                        "stale-revision",
                        "candidate activation regressed the migrated state revision",
                    )
                transaction["activated_state_revision"] = activated_state_revision
                transaction["activated_kv_state_sha256"] = activated_evidence["sha256"]
            candidate_app["state"]["revision"] = activated_state_revision
            for shadow_service in shadow_services.values():
                shadow_service["state_revision"] = activated_state_revision
            transaction["activation_health"] = {
                "status": (
                    "degraded"
                    if any(report["status"] == "degraded" for report in validation_reports)
                    else "healthy"
                ),
                "checks": validation_reports,
            }
            transaction["status"] = "validated"
            transaction["phase"] = "validated"
            app["update"]["status"] = "validated"
            app["update"]["pending"] = _copy_json(transaction)
            self._write_state(state)

            transaction["status"] = "switching"
            transaction["phase"] = "switching"
            app["update"]["status"] = "switching"
            app["update"]["pending"] = _copy_json(transaction)
            self._write_state(state)
            self._quiesce(app_id, app, reason="update", close_surfaces=False)
            old_quiesced = True
            if active_state.exists():
                os.replace(active_state, previous_state)
            os.replace(candidate_state, active_state)
            self._fsync_directory(self.data_root)
            self._fsync_directory(transaction_root)
            state_swapped = True
            for shadow_worker in shadow_workers.values():
                rebound = shadow_worker.rebind_state(active_state)
                if (
                    rebound.get("tag") != "state-rebound"
                    or rebound.get("state_revision") != activated_state_revision
                ):
                    raise DaemonError(
                        "integrity-failure",
                        "candidate worker did not bind the activated state revision",
                    )
            self._inject_update_fault("after-state-swap")

            preserved_update = app["update"]
            preserved_sequence = max(
                app.get("service_instance_sequence", 0),
                candidate_app.get("service_instance_sequence", 0),
            )
            app.clear()
            app.update(candidate_app)
            app["update"] = preserved_update
            app["service_instance_sequence"] = preserved_sequence
            app["enabled"] = original_app["enabled"]
            app["quarantined"] = False
            app["last_error"] = None
            app["lifecycle_state"] = "enabled" if app["enabled"] else "disabled"
            for entrypoint in running_entrypoints:
                app["services"][entrypoint] = shadow_services[entrypoint]
                self._workers[(app_id, entrypoint)] = shadow_workers[entrypoint]
            observation_started_at = self.clock()
            observation_started = _parse_utc(observation_started_at)
            observation_deadline_at = self._format_utc(
                observation_started + dt.timedelta(seconds=OBSERVATION_WINDOW_SECONDS)
            )
            transaction["status"] = "observation-window"
            transaction["phase"] = "observation"
            transaction["completed_at_utc"] = observation_started_at
            transaction["observation_started_at_utc"] = observation_started_at
            transaction["observation_deadline_utc"] = observation_deadline_at
            transaction.pop("previous", None)
            rollback = {
                "schema_version": UPDATE_SCHEMA,
                "transaction_id": transaction_id,
                "from_package_digest_sha256": previous["package_digest_sha256"],
                "to_package_digest_sha256": candidate.package_digest,
                "previous_state_path": str(previous_state.relative_to(self.root)),
                "transaction_root": str(transaction_root.relative_to(self.root)),
                "previous": previous,
                "observation_started_at_utc": observation_started_at,
                "observation_deadline_utc": observation_deadline_at,
                "retention": "automatic-rollback-window",
            }
            app["update"]["status"] = "observation-window"
            app["update"]["pending"] = None
            app["update"]["previous"] = rollback
            self._append_update_history(app, transaction)
            self._append_audit(
                state,
                "update-activated",
                principal=principal,
                app_id=app_id,
                result="observation-window",
                fields={
                    "from_package_digest_sha256": previous["package_digest_sha256"],
                    "to_package_digest_sha256": candidate.package_digest,
                    "migration_status": transaction["migration"]["status"],
                    "activation_health": transaction["activation_health"]["status"],
                },
            )
            self._write_state(state)
            return {"tag": "accepted", "value": None}
        except (DaemonError, OSError, ServiceExecutionError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, DaemonError)
                else DaemonError("internal", str(raw_error), retryable=True)
            )
            failed_phase = transaction.get("phase")
            if failed_phase == "migration":
                transaction["migration"] = {
                    "status": "failed",
                    "target_state_revision": target_state_revision,
                    "error": error.as_dict(),
                }
                transaction["activation_health"] = {
                    "status": "not-run",
                    "checks": [],
                    "error": error.as_dict(),
                }
            elif failed_phase == "activation":
                transaction["activation_health"] = {
                    "status": "failed",
                    "checks": [],
                    "error": error.as_dict(),
                }
            elif failed_phase == "post-migration":
                transaction["activation_health"] = {
                    "status": "not-run",
                    "checks": [],
                    "error": error.as_dict(),
                }
            elif transaction["migration"].get("status") == "pending":
                transaction["migration"] = {
                    "status": "not-run",
                    "target_state_revision": target_state_revision,
                    "error": error.as_dict(),
                }
            for entrypoint, shadow_worker in shadow_workers.items():
                self._workers.pop((app_id, entrypoint), None)
                shadow_worker.terminate()
            if state_swapped and previous_state.exists():
                if active_state.exists():
                    shutil.rmtree(active_state)
                os.replace(previous_state, active_state)
                self._fsync_directory(self.data_root)
            app.clear()
            app.update(original_app)
            if old_quiesced:
                app["services"] = {}
                for entrypoint in running_entrypoints:
                    try:
                        app["services"][entrypoint] = self._activate_service_generation(
                            app_id,
                            app,
                            entrypoint,
                            reason="update",
                        )
                    except DaemonError as recovery_error:
                        app["services"][entrypoint] = {
                            "entrypoint": entrypoint,
                            "state": "failed",
                            "generation": None,
                            "last_error": self._service_error(
                                recovery_error.code,
                                recovery_error.message,
                                recovery_error.retryable,
                                "rollback-recovery",
                                at_utc=self.clock(),
                            ),
                        }
            if transaction_root.exists():
                shutil.rmtree(transaction_root)
            self._remove_package(app_id, candidate.package_digest)
            transaction.pop("previous", None)
            transaction["status"] = "rolled-back"
            transaction["completed_at_utc"] = self.clock()
            transaction["rollback_reason"] = error.message
            transaction["error"] = error.as_dict()
            app["update"]["status"] = "rolled-back"
            app["update"]["pending"] = None
            self._append_update_history(app, transaction)
            app["lifecycle_state"] = "enabled" if app.get("enabled") else "disabled"
            self._append_audit(
                state,
                "update-rollback",
                principal=principal,
                app_id=app_id,
                result="rolled-back",
                fields={
                    "from_package_digest_sha256": transaction["from_package_digest_sha256"],
                    "rejected_package_digest_sha256": transaction["to_package_digest_sha256"],
                    "rollback_reason": transaction["rollback_reason"],
                },
            )
            self._write_state(state)
            raise error

    @staticmethod
    def _require_null(value: Any, tag: str) -> None:
        if value is not None:
            raise DaemonError("invalid-argument", f"{tag} command has no payload")

    def _enable(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        self._require_null(value, "enable")
        app = state["apps"][app_id]
        if app["quarantined"]:
            raise DaemonError("resource-limit", "app is quarantined after a bounded crash loop")
        self._require_activation_capabilities(app)
        if app["enabled"]:
            return {"tag": "accepted", "value": None}

        on_enable_entrypoints = sorted(
            [
                entrypoint
                for entrypoint in app["service_entrypoints"]
                if "on-enable" in entrypoint.get("triggers", [])
            ],
            key=lambda entrypoint: entrypoint["id"],
        )
        if not on_enable_entrypoints:
            app["enabled"] = True
            app["lifecycle_state"] = "enabled"
            app["last_error"] = None
            self._append_audit(
                state,
                "enable",
                principal=principal,
                app_id=app_id,
                result="enabled",
                fields={"services_started": 0},
            )
            return {"tag": "accepted", "value": None}

        original_app = _copy_json(app)
        candidate_app = _copy_json(app)
        active_state = self.data_root / app_id
        transaction_id = uuid.uuid4().hex
        transaction_parent = self.updates_root / app_id
        transaction_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        transaction_root = transaction_parent / f"enable-{transaction_id}"
        transaction_root.mkdir(mode=0o700)
        candidate_state = transaction_root / "candidate-state"
        previous_state = transaction_root / "previous-state"
        self._copy_state_tree(active_state, candidate_state)
        self._fsync_directory(transaction_root)
        self._fsync_directory(transaction_parent)
        transaction = {
            "schema_version": ENABLE_SCHEMA,
            "transaction_id": transaction_id,
            "status": "preparing",
            "app_id": app_id,
            "package_digest_sha256": app["package_digest_sha256"],
            "transaction_root": str(transaction_root.relative_to(self.root)),
            "candidate_state_path": str(candidate_state.relative_to(self.root)),
            "previous_state_path": str(previous_state.relative_to(self.root)),
            "original_state_revision": app["state"]["revision"],
            "service_entrypoints": [item["id"] for item in on_enable_entrypoints],
            "started_at_utc": self.clock(),
        }
        app["enable_transaction"] = _copy_json(transaction)
        self._write_state(state)

        started: list[tuple[dict[str, Any], ServiceWorker]] = []
        state_swapped = False
        try:
            for entrypoint in on_enable_entrypoints:
                service, worker = self._create_service_generation(
                    app_id,
                    candidate_app,
                    entrypoint["id"],
                    reason="enabled",
                    state_directory=candidate_state,
                    state_revision=candidate_app["state"]["revision"],
                )
                candidate_app["state"]["revision"] = max(
                    candidate_app["state"]["revision"], service["state_revision"]
                )
                candidate_app["services"][entrypoint["id"]] = service
                started.append((service, worker))

            transaction["status"] = "switching"
            app["enable_transaction"] = _copy_json(transaction)
            self._write_state(state)
            if active_state.exists():
                if not active_state.is_dir() or active_state.is_symlink():
                    raise DaemonError(
                        "integrity-failure", "enable active state is not a real directory"
                    )
                os.replace(active_state, previous_state)
            os.replace(candidate_state, active_state)
            self._fsync_directory(self.data_root)
            self._fsync_directory(transaction_root)
            state_swapped = True

            final_revision = candidate_app["state"]["revision"]
            for service, worker in started:
                rebound = worker.rebind_state(active_state)
                if (
                    rebound.get("tag") != "state-rebound"
                    or rebound.get("state_revision") != final_revision
                ):
                    raise DaemonError(
                        "integrity-failure",
                        "enabled service did not bind the committed state revision",
                    )
                service["state_revision"] = final_revision

            transaction["status"] = "committed"
            candidate_app["enable_transaction"] = _copy_json(transaction)
            candidate_app["enabled"] = True
            candidate_app["lifecycle_state"] = "enabled"
            candidate_app["guest_execution_performed"] = True
            candidate_app["last_error"] = None
            app.clear()
            app.update(candidate_app)
            for service, worker in started:
                self._workers[(app_id, service["entrypoint"])] = worker
            # This is the commit point: the durable state and active directory
            # now agree even if the process exits before request replay metadata.
            self._write_state(state)
        except Exception:
            for service, worker in reversed(started):
                self._workers.pop((app_id, service["entrypoint"]), None)
                with contextlib.suppress(Exception):
                    worker.stop("disabled")
                worker.terminate()
            if previous_state.exists():
                if active_state.exists() or active_state.is_symlink():
                    if not active_state.is_dir() or active_state.is_symlink():
                        raise DaemonError(
                            "integrity-failure",
                            "failed enable left an unsafe active state directory",
                        )
                    shutil.rmtree(active_state)
                os.replace(previous_state, active_state)
                self._fsync_directory(self.data_root)
            elif state_swapped:
                raise DaemonError(
                    "integrity-failure", "failed enable lost its previous state snapshot"
                )
            app.clear()
            app.update(original_app)
            if transaction_root.exists():
                shutil.rmtree(transaction_root)
            self._write_state(state)
            raise

        with contextlib.suppress(OSError):
            if transaction_root.exists():
                shutil.rmtree(transaction_root)
                self._fsync_directory(transaction_parent)
        if not transaction_root.exists():
            app["enable_transaction"] = None
        for service, _worker in started:
            self._append_audit(
                state,
                "service-start",
                principal=principal,
                app_id=app_id,
                result="running",
                fields={
                    "entrypoint": service["entrypoint"],
                    "instance": service["instance"],
                    "generation": service["generation"],
                    "start_reason": "enabled",
                    "execution": "component",
                },
            )
        self._append_audit(
            state,
            "enable",
            principal=principal,
            app_id=app_id,
            result="enabled",
            fields={"services_started": len(started), "transaction_id": transaction_id},
        )
        self._write_state(state)
        return {"tag": "accepted", "value": None}

    @staticmethod
    def _service_error(code: str, message: str, retryable: bool, operation: str, *, at_utc: str | None = None) -> dict[str, Any]:
        return {
            "code": code,
            "message": message[:512].replace("\n", " ").replace("\r", " "),
            "retryable": retryable,
            "operation": operation,
            "at_utc": at_utc,
        }

    def _component_for_app(self, app: dict[str, Any]) -> Path:
        package = self.root / app["package_path"]
        component = package / "component.wasm"
        if not component.is_relative_to(self.packages_root):
            raise DaemonError("integrity-failure", "installed component escaped package storage")
        data = _read_regular_nofollow(component, MAX_PACKAGE_BYTES)
        if _sha256_bytes(data) != app["component_sha256"]:
            raise DaemonError("integrity-failure", "installed component digest changed before service execution")
        return component

    def _create_service_generation(
        self,
        app_id: str,
        app: dict[str, Any],
        entrypoint: str,
        *,
        reason: str,
        guest_start_reason: str | None = None,
        state_directory: Path | None = None,
        state_revision: int | None = None,
        migration: dict[str, int] | None = None,
        on_migrated: Callable[[dict[str, Any]], None] | None = None,
        activate_service: bool = True,
        replaces_active_generation: bool = False,
    ) -> tuple[dict[str, Any], ServiceWorker]:
        runtime_world = app.get("runtime_world")
        if runtime_world not in {
            "ui-only-reference",
            "service-only-reference",
            "hybrid-reference",
        }:
            raise DaemonError(
                "unsupported-version",
                "installed runtime world is not generation-capable",
            )
        self._require_activation_capabilities(app)
        if activate_service and runtime_world == "ui-only-reference":
            raise DaemonError("unsupported-surface", "UI-only generation cannot own a service")
        if (
            activate_service
            and not replaces_active_generation
            and sum(
                1
                for worker_app, _entrypoint in self._workers
                if worker_app == app_id
            )
            >= MAX_ACTIVE_SERVICE_WORKERS_PER_APP
        ):
            raise DaemonError("resource-limit", "app active service worker ceiling is reached", retryable=True)
        if (
            activate_service
            and not replaces_active_generation
            and len(self._workers) >= MAX_ACTIVE_SERVICE_WORKERS
        ):
            raise DaemonError("resource-limit", "daemon active service worker ceiling is reached", retryable=True)
        worker: ServiceWorker | None = None
        try:
            binary = resolve_runtime_binary(self._service_runtime_binary)
            component = self._component_for_app(app)
            app["service_instance_sequence"] += 1
            sequence = app["service_instance_sequence"]
            instance = f"svc:{app_id}:{entrypoint}:{sequence}"
            generation = f"gen:{app_id}:{entrypoint}:{sequence}"
            bound_state_directory = state_directory or (self.data_root / app_id)
            bound_state_revision = state_revision or app["state"]["revision"]
            settings_snapshot = self._settings_snapshot_for_worker(
                app_id,
                app,
                generation,
                bound_state_revision,
            )
            worker = ServiceWorker(
                binary=binary,
                component=component,
                expected_sha256=app["component_sha256"],
                package_digest_sha256=app["package_digest_sha256"],
                app_id=app_id,
                app_version=app["version"],
                entrypoint=entrypoint,
                entrypoint_kind="service" if activate_service else "launcher-ui",
                instance=instance,
                generation=generation,
                runtime_world=app["runtime_world"],
                runtime_root=self.root,
                state_directory=bound_state_directory,
                state_revision=bound_state_revision,
                kv_maximum_bytes=app.get("kv_storage_limit_bytes"),
                settings_snapshot=settings_snapshot,
            )
            if migration is not None:
                migrated = worker.migrate(
                    from_schema=migration["from_schema"],
                    to_schema=migration["to_schema"],
                    source_state_revision=migration["source_state_revision"],
                    target_state_revision=migration["target_state_revision"],
                )
                if on_migrated is not None:
                    on_migrated(migrated)
            validated = worker.validate()
            if validated.get("tag") != "validated":
                raise DaemonError(
                    "malformed-output",
                    "service runtime returned an invalid descriptor validation result",
                )
            # experimental-v0 has no scheduled start enum. Scheduler cold-start
            # therefore remains explicitly visible as the daemon cause while the
            # frozen Guest ABI receives its conservative `manual` compatibility
            # reason. A real `scheduled` reason needs a versioned WIT contract.
            effective_guest_start_reason = guest_start_reason or reason
            started = (
                worker.start(effective_guest_start_reason)
                if activate_service
                else {"diagnostic": None}
            )
            health = worker.health()
            report = health.get("report")
            observed_state_revision = health.get("state_revision")
            if not isinstance(report, dict) or report.get("status") not in {"healthy", "degraded", "unhealthy"}:
                worker.terminate()
                raise DaemonError("malformed-output", "service runtime returned an invalid health report")
            if (
                isinstance(observed_state_revision, bool)
                or not isinstance(observed_state_revision, int)
                or observed_state_revision < bound_state_revision
            ):
                worker.terminate()
                raise DaemonError(
                    "malformed-output",
                    "service runtime health omitted its non-regressing state revision",
                )
            if report["status"] == "unhealthy":
                with contextlib.suppress(ServiceExecutionError):
                    worker.stop("unhealthy")
                raise DaemonError("internal", "service generation reported unhealthy during activation", retryable=True)
        except ServiceExecutionError as error:
            if worker is not None:
                worker.terminate()
            raise DaemonError(error.code, error.message, retryable=error.retryable) from error
        except Exception:
            if worker is not None:
                worker.terminate()
            raise
        now = self.clock()
        return {
            "entrypoint": entrypoint,
            "instance": instance,
            "generation": generation,
            "state": "running" if activate_service else "validated",
            "owner": "daemon",
            "started_at_utc": now,
            "start_reason": reason,
            "guest_start_reason": (
                effective_guest_start_reason if activate_service else None
            ),
            "guest_execution": True,
            "start_diagnostic": started.get("diagnostic"),
            "health": {**report, "observed_at_utc": now, "source": "daemon-runtime"},
            "last_error": None,
            "event_count": 2 if activate_service else 1,
            "last_trigger": None,
            "package_digest_sha256": app["package_digest_sha256"],
            "state_revision": observed_state_revision,
        }, worker

    def _activate_service_generation(
        self,
        app_id: str,
        app: dict[str, Any],
        entrypoint: str,
        *,
        reason: str,
        guest_start_reason: str | None = None,
    ) -> dict[str, Any]:
        service, worker = self._create_service_generation(
            app_id,
            app,
            entrypoint,
            reason=reason,
            guest_start_reason=guest_start_reason,
        )
        # A guest may commit app-scoped KV during its start event.  Adopt the
        # exact revision observed by the activation health check before any
        # later UI or service generation is bound to this app's state.
        app["state"]["revision"] = max(
            app["state"]["revision"],
            service["state_revision"],
        )
        self._workers[(app_id, entrypoint)] = worker
        return service

    def _stop_service_generation(self, app_id: str, service: dict[str, Any], *, reason: str) -> dict[str, Any] | None:
        worker = self._workers.pop((app_id, service["entrypoint"]), None)
        if worker is None:
            return None
        wit_reason = reason if reason in {"disabled", "update", "uninstall", "unhealthy", "host-shutdown"} else "host-shutdown"
        try:
            return worker.stop(wit_reason)
        except ServiceExecutionError as error:
            return self._service_error(error.code, error.message, error.retryable, "stop", at_utc=self.clock())

    def _quiesce(
        self,
        app_id: str,
        app: dict[str, Any],
        *,
        reason: str,
        close_surfaces: bool = True,
    ) -> int:
        stopped = 0
        for key in [key for key in self._ui_workers if key[0] == app_id]:
            self._ui_workers.pop(key).terminate()
            self._ui_refresh_at.pop(key, None)
        for service in app["services"].values():
            if service["state"] == "running":
                stop_result = self._stop_service_generation(app_id, service, reason=reason)
                service["state"] = "stopped"
                service["stop_reason"] = reason
                service["stopped_at_utc"] = self.clock()
                if isinstance(stop_result, dict) and "code" in stop_result:
                    service["last_error"] = stop_result
                stopped += 1
        if close_surfaces:
            for surface in app["surfaces"].values():
                if surface["state"] == "open":
                    surface["state"] = "closed"
                    surface["closed_at_utc"] = self.clock()
                    surface["close_reason"] = reason
        return stopped

    def _disable(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        self._require_null(value, "disable")
        app = state["apps"][app_id]
        stopped = self._quiesce(app_id, app, reason="disabled")
        if app["enabled"]:
            app["enabled"] = False
            app["lifecycle_state"] = "disabled"
            self._append_audit(state, "disable", principal=principal, app_id=app_id, result="disabled", fields={"services_stopped": stopped})
        return {"tag": "accepted", "value": None}

    @staticmethod
    def _entrypoint_request(value: Any) -> str:
        if not isinstance(value, dict) or set(value) != {"entrypoint"}:
            raise DaemonError("invalid-argument", "service control request fields are invalid")
        return _bounded_text(value["entrypoint"], "entrypoint", 128)

    def _service_start(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        entrypoint = self._entrypoint_request(value)
        app = state["apps"][app_id]
        if not app["enabled"]:
            raise DaemonError("app-disabled", "disabled app cannot start a service")
        if app["quarantined"]:
            raise DaemonError("resource-limit", "quarantined app cannot start a service")
        entrypoint_contract = next(
            (item for item in app["service_entrypoints"] if item["id"] == entrypoint),
            None,
        )
        if entrypoint_contract is None:
            raise DaemonError("not-found", "service entrypoint is not declared")
        if "manual" not in entrypoint_contract.get("triggers", []):
            raise DaemonError(
                "capability-unavailable",
                "service entrypoint does not declare a manual start trigger",
            )
        existing = app["services"].get(entrypoint)
        if existing and existing["state"] == "running" and (app_id, entrypoint) in self._workers:
            return {"tag": "accepted", "value": None}
        if existing and existing["state"] == "running":
            existing["state"] = "failed"
            existing["last_error"] = self._service_error("internal", "authoritative worker was missing", True, "start", at_utc=self.clock())
        try:
            service = self._activate_service_generation(app_id, app, entrypoint, reason="manual")
        except DaemonError as error:
            now = self.clock()
            app["services"][entrypoint] = {
                "entrypoint": entrypoint,
                "instance": None,
                "generation": None,
                "state": "failed",
                "owner": "daemon",
                "started_at_utc": None,
                "start_reason": "manual",
                "guest_execution": False,
                "health": None,
                "last_error": self._service_error(error.code, error.message, error.retryable, "start", at_utc=now),
                "event_count": 0,
                "last_trigger": None,
            }
            app["last_error"] = error.code
            self._append_audit(state, "service-start", principal=principal, app_id=app_id, result="failed", fields={"entrypoint": entrypoint, "error_code": error.code})
            self._write_state(state)
            raise
        app["services"][entrypoint] = service
        app["guest_execution_performed"] = True
        app["last_error"] = None
        self._append_audit(state, "service-start", principal=principal, app_id=app_id, result="running", fields={"entrypoint": entrypoint, "instance": service["instance"], "generation": service["generation"], "execution": "component"})
        return {"tag": "accepted", "value": None}

    def _service_stop(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        entrypoint = self._entrypoint_request(value)
        app = state["apps"][app_id]
        if entrypoint not in {item["id"] for item in app["service_entrypoints"]}:
            raise DaemonError("not-found", "service entrypoint is not declared")
        service = app["services"].get(entrypoint)
        if service and service["state"] == "running":
            stop_result = self._stop_service_generation(app_id, service, reason="host-shutdown")
            service["state"] = "stopped"
            service["stop_reason"] = "manual"
            service["stopped_at_utc"] = self.clock()
            if isinstance(stop_result, dict) and "code" in stop_result:
                service["last_error"] = stop_result
            self._append_audit(state, "service-stop", principal=principal, app_id=app_id, result="stopped", fields={"entrypoint": entrypoint, "instance": service["instance"], "guest_stop_delivered": not (isinstance(stop_result, dict) and "code" in stop_result)})
        return {"tag": "accepted", "value": None}

    @staticmethod
    def _trigger_request(value: Any) -> tuple[str, str, list[int]]:
        if not isinstance(value, dict) or set(value) != {"entrypoint", "trigger_id", "payload"}:
            raise DaemonError("invalid-argument", "service trigger request fields are invalid")
        entrypoint = _bounded_text(value["entrypoint"], "entrypoint", 128)
        trigger_id = _bounded_text(value["trigger_id"], "trigger_id", 128)
        payload = value["payload"]
        if not isinstance(payload, list) or len(payload) > 64 * 1024 or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 255 for item in payload):
            raise DaemonError("resource-limit", "service trigger payload must be at most 64 KiB of bytes")
        return entrypoint, trigger_id, payload

    def _running_worker(self, app_id: str, app: dict[str, Any], entrypoint: str) -> tuple[dict[str, Any], ServiceWorker]:
        if not app["enabled"]:
            raise DaemonError("app-disabled", "disabled app cannot execute a service")
        service = app["services"].get(entrypoint)
        worker = self._workers.get((app_id, entrypoint))
        if service is None or service.get("state") != "running" or worker is None:
            raise DaemonError("not-found", "running service generation was not found")
        return service, worker

    def _execution_failed(self, state: dict[str, Any], app_id: str, service: dict[str, Any], error: ServiceExecutionError, operation: str, principal: str) -> None:
        self._workers.pop((app_id, service["entrypoint"]), None)
        service["state"] = "failed"
        service["stop_reason"] = f"{operation}-failed"
        service["stopped_at_utc"] = self.clock()
        service["last_error"] = self._service_error(error.code, error.message, error.retryable, operation, at_utc=self.clock())
        app = state["apps"][app_id]
        app["last_error"] = error.code
        self._append_audit(state, f"service-{operation}", principal=principal, app_id=app_id, result="failed", fields={"entrypoint": service["entrypoint"], "generation": service.get("generation"), "error_code": error.code})
        self._rollback_observation_update(
            state,
            app_id,
            app,
            reason=f"candidate {operation} failed: {error.message}",
            principal=principal,
        )
        self._write_state(state)

    @staticmethod
    def _apply_worker_state_revision(
        app: dict[str, Any],
        service: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        revision = result.get("state_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise DaemonError(
                "malformed-output",
                "service runtime omitted its exact state revision",
            )
        current = app["state"]["revision"]
        if revision < service.get("state_revision", current):
            raise DaemonError(
                "stale-revision",
                "service runtime returned a regressed state revision",
            )
        service["state_revision"] = revision
        app["state"]["revision"] = max(current, revision)

    def _service_trigger(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        entrypoint, trigger_id, payload = self._trigger_request(value)
        app = state["apps"][app_id]
        if not app["enabled"]:
            raise DaemonError("app-disabled", "disabled app cannot execute a service")
        if app["quarantined"]:
            raise DaemonError("resource-limit", "quarantined app cannot execute a service")
        entrypoint_contract = next(
            (item for item in app["service_entrypoints"] if item["id"] == entrypoint),
            None,
        )
        if entrypoint_contract is None:
            raise DaemonError("not-found", "service entrypoint is not declared")
        if "scheduler" not in entrypoint_contract.get("triggers", []):
            raise DaemonError(
                "capability-unavailable",
                "service entrypoint does not declare a scheduler trigger",
            )
        service = app["services"].get(entrypoint)
        worker = self._workers.get((app_id, entrypoint))
        if service is None or service.get("state") != "running" or worker is None:
            if service is not None and service.get("state") == "running":
                service["state"] = "failed"
                service["last_error"] = self._service_error(
                    "internal",
                    "authoritative worker was missing",
                    True,
                    "scheduler-start",
                    at_utc=self.clock(),
                )
            try:
                service = self._activate_service_generation(
                    app_id,
                    app,
                    entrypoint,
                    reason="scheduler",
                    guest_start_reason="manual",
                )
            except DaemonError as error:
                app["services"][entrypoint] = {
                    "entrypoint": entrypoint,
                    "instance": None,
                    "generation": None,
                    "state": "failed",
                    "owner": "daemon",
                    "started_at_utc": None,
                    "start_reason": "scheduler",
                    "guest_start_reason": "manual",
                    "guest_execution": False,
                    "health": None,
                    "last_error": self._service_error(
                        error.code,
                        error.message,
                        error.retryable,
                        "scheduler-start",
                        at_utc=self.clock(),
                    ),
                    "event_count": 0,
                    "last_trigger": None,
                }
                app["last_error"] = error.code
                self._append_audit(
                    state,
                    "service-start",
                    principal=principal,
                    app_id=app_id,
                    result="failed",
                    fields={
                        "entrypoint": entrypoint,
                        "start_reason": "scheduler",
                        "guest_start_reason": "manual",
                        "error_code": error.code,
                    },
                )
                self._write_state(state)
                raise
            app["services"][entrypoint] = service
            app["guest_execution_performed"] = True
            app["last_error"] = None
            worker = self._workers[(app_id, entrypoint)]
            self._append_audit(
                state,
                "service-start",
                principal=principal,
                app_id=app_id,
                result="running",
                fields={
                    "entrypoint": entrypoint,
                    "instance": service["instance"],
                    "generation": service["generation"],
                    "start_reason": "scheduler",
                    "guest_start_reason": "manual",
                    "execution": "component",
                },
            )
        try:
            result = worker.trigger(trigger_id, payload)
        except ServiceExecutionError as error:
            self._execution_failed(state, app_id, service, error, "trigger", principal)
            raise DaemonError(error.code, error.message, retryable=error.retryable) from error
        self._apply_worker_state_revision(app, service, result)
        service["event_count"] += 1
        service["last_trigger"] = {"id": trigger_id, "payload_bytes": len(payload), "at_utc": self.clock(), "diagnostic": result.get("diagnostic")}
        self._append_audit(state, "service-trigger", principal=principal, app_id=app_id, result="delivered", fields={"entrypoint": entrypoint, "generation": service["generation"], "trigger_id": trigger_id, "payload_bytes": len(payload), "cause": "scheduler"})
        return {"tag": "accepted", "value": None}

    def _service_health(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        entrypoint = self._entrypoint_request(value)
        app = state["apps"][app_id]
        service, worker = self._running_worker(app_id, app, entrypoint)
        try:
            result = worker.health()
        except ServiceExecutionError as error:
            self._execution_failed(state, app_id, service, error, "health", principal)
            raise DaemonError(error.code, error.message, retryable=error.retryable) from error
        report = result.get("report")
        if not isinstance(report, dict) or report.get("status") not in {"healthy", "degraded", "unhealthy"}:
            error = ServiceExecutionError("malformed-output", "service runtime returned an invalid health report")
            worker.terminate()
            self._execution_failed(state, app_id, service, error, "health", principal)
            raise DaemonError(error.code, error.message)
        self._apply_worker_state_revision(app, service, result)
        service["health"] = {**report, "observed_at_utc": self.clock(), "source": "daemon-runtime"}
        service["event_count"] += 1
        if report["status"] == "unhealthy":
            worker.terminate()
            error = ServiceExecutionError("internal", "service generation reported unhealthy", retryable=True)
            self._execution_failed(state, app_id, service, error, "health", principal)
            raise DaemonError(error.code, error.message, retryable=True)
        self._append_audit(state, "service-health", principal=principal, app_id=app_id, result=report["status"], fields={"entrypoint": entrypoint, "generation": service["generation"]})
        return {"tag": "accepted", "value": None}

    def _launch(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"entrypoint", "route"}:
            raise DaemonError("invalid-argument", "launch request fields are invalid")
        app = state["apps"][app_id]
        if not app["enabled"]:
            raise DaemonError("app-disabled", "disabled app cannot execute a UI surface")
        entrypoint = _bounded_text(value["entrypoint"], "entrypoint", 128)
        declared = next((item for item in app["ui_entrypoints"] if item["id"] == entrypoint), None)
        if declared is None:
            raise DaemonError("unsupported-surface", "launcher entrypoint is not declared")
        route = value["route"]
        if route is None:
            route = declared.get("initial_route")
        route = _bounded_text(route, "route", 128)
        existing = next((surface for surface in app["surfaces"].values() if surface["entrypoint"] == entrypoint and surface["state"] == "open"), None)
        if existing is None:
            app["surface_sequence"] += 1
            sequence = app["surface_sequence"]
            session = f"session:{app_id}:{sequence}"
            surface_id = f"surface:{app_id}:{sequence}"
            existing = {"entrypoint": entrypoint, "session": session, "surface": surface_id, "route": route, "state": "open", "opened_at_utc": self.clock()}
            app["surfaces"][surface_id] = existing
            self._append_audit(state, "ui-open", principal=principal, app_id=app_id, result="open", fields={"entrypoint": entrypoint, "surface": surface_id})
        elif existing["route"] != route:
            raise DaemonError("conflict", "open surface is bound to another route")
        key = (app_id, existing["surface"])
        previous_worker = self._ui_workers.pop(key, None)
        self._ui_refresh_at.pop(key, None)
        if previous_worker is not None:
            previous_worker.terminate()
        if len(self._ui_workers) >= MAX_ACTIVE_SERVICE_WORKERS:
            raise DaemonError("resource-limit", "daemon active UI worker ceiling is reached", retryable=True)
        worker: ServiceWorker | None = None
        try:
            generation_record, worker = self._create_service_generation(
                app_id,
                app,
                entrypoint,
                reason="manual",
                activate_service=False,
            )
            runtime_outcome = worker.ui_launch(
                session=existing["session"],
                surface=existing["surface"],
                route=existing["route"],
            )
            trusted_surface = self._trusted_ui_surface(
                runtime_outcome,
                session=existing["session"],
                surface=existing["surface"],
                route=existing["route"],
            )
        except ServiceExecutionError as error:
            if worker is not None:
                worker.terminate()
            raise DaemonError(error.code, error.message, retryable=error.retryable) from error
        except Exception:
            if worker is not None:
                worker.terminate()
            raise
        generation = generation_record["generation"]
        token = secrets.token_hex(32)
        existing["generation"] = generation
        existing["package_digest_sha256"] = app["package_digest_sha256"]
        existing["component_sha256"] = app["component_sha256"]
        existing["trusted_surface"] = trusted_surface
        existing["render_feedback"] = {
            "schema_version": UI_FEEDBACK_SCHEMA,
            "principal": principal,
            "token_sha256": _sha256_bytes(token.encode("ascii")),
            "used": False,
            "issued_at_utc": self.clock(),
        }
        app["state"]["revision"] = max(
            app["state"]["revision"],
            generation_record["state_revision"],
            runtime_outcome.get("state_revision", 0),
        )
        app["guest_execution_performed"] = True
        self._ui_workers[key] = worker
        self._ui_refresh_at[key] = self.monotonic_clock()
        return {
            "tag": "launched",
            "value": {
                "entrypoint": entrypoint,
                "package_digest_sha256": app["package_digest_sha256"],
                "component_sha256": app["component_sha256"],
                "generation": generation,
                "session": existing["session"],
                "surface": existing["surface"],
                "route": existing["route"],
                "render_failure_token": token,
                "semantic_surface": trusted_surface,
            },
        }

    @staticmethod
    def _trusted_ui_surface(
        outcome: Any,
        *,
        session: str,
        surface: str,
        route: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(outcome, dict)
            or outcome.get("tag") != "ui-updated"
            or not isinstance(outcome.get("surfaces"), list)
            or len(outcome["surfaces"]) != 1
            or not isinstance(outcome["surfaces"][0], dict)
        ):
            raise DaemonError("malformed-output", "UI runtime omitted its bound semantic surface")
        update = outcome["surfaces"][0]
        if (
            update.get("session") != session
            or update.get("surface") != surface
            or update.get("route") != route
            or not isinstance(update.get("view"), dict)
        ):
            raise DaemonError("forged-identifier", "UI runtime changed its surface identity")
        return _copy_json(update)

    def _ui_action(
        self,
        state: dict[str, Any],
        app_id: str,
        value: Any,
        principal: str,
    ) -> dict[str, Any]:
        expected = {
            "entrypoint",
            "package_digest_sha256",
            "component_sha256",
            "generation",
            "session",
            "surface",
            "route",
            "action",
            "event_id",
            "fields",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise DaemonError("invalid-argument", "UI action fields are invalid")
        app = state["apps"][app_id]
        if not app["enabled"]:
            raise DaemonError("app-disabled", "disabled app cannot receive UI actions")
        package_digest = _digest(value["package_digest_sha256"], "package_digest_sha256")
        component_digest = _digest(value["component_sha256"], "component_sha256")
        entrypoint = _bounded_text(value["entrypoint"], "entrypoint", 128)
        generation = _bounded_text(value["generation"], "generation", 256)
        session = _bounded_text(value["session"], "session", 256)
        surface_id = _bounded_text(value["surface"], "surface", 256)
        route = _bounded_text(value["route"], "route", 128)
        action = _bounded_text(value["action"], "action", 128)
        event_id = _bounded_text(value["event_id"], "event_id", 128)
        fields = value["fields"]
        if not isinstance(fields, list) or len(fields) > 256:
            raise DaemonError("resource-limit", "UI action fields exceed their bound")
        surface = app["surfaces"].get(surface_id)
        feedback = surface.get("render_feedback") if isinstance(surface, dict) else None
        if (
            surface is None
            or surface.get("state") != "open"
            or surface.get("entrypoint") != entrypoint
            or surface.get("session") != session
            or surface.get("route") != route
            or surface.get("generation") != generation
            or surface.get("package_digest_sha256") != package_digest
            or surface.get("component_sha256") != component_digest
            or app["package_digest_sha256"] != package_digest
            or app["component_sha256"] != component_digest
            or not isinstance(feedback, dict)
            or feedback.get("principal") != principal
        ):
            raise DaemonError("forged-identifier", "UI action does not match the current trusted binding")
        worker = self._ui_workers.get((app_id, surface_id))
        if worker is None or worker.generation != generation:
            raise DaemonError("stale-revision", "UI action generation is no longer live")
        try:
            runtime_outcome = worker.ui_action(
                session=session,
                surface=surface_id,
                route=route,
                action=action,
                event_id=event_id,
                fields=_copy_json(fields),
            )
            trusted_surface = self._trusted_ui_surface(
                runtime_outcome,
                session=session,
                surface=surface_id,
                route=route,
            )
        except ServiceExecutionError as error:
            if (
                error.code == "invalid-argument"
                and type(error.ui_rejection_revision) is int
                and error.ui_rejection_revision == app["state"]["revision"]
            ):
                # A bound host-attested rejection did not attempt any durable
                # writes. Keep this exact generation/surface; return the error
                # without a retry, a new generation, or a successful outcome.
                raise DaemonError(error.code, error.message, retryable=error.retryable) from error
            worker.terminate()
            self._ui_workers.pop((app_id, surface_id), None)
            self._ui_refresh_at.pop((app_id, surface_id), None)
            surface["state"] = "closed"
            surface["closed_at_utc"] = self.clock()
            surface["close_reason"] = "ui-action-failed"
            self._rollback_observation_update(
                state,
                app_id,
                app,
                reason=f"candidate UI action failed: {error.message}",
                principal="runtime-observer",
            )
            raise DaemonError(error.code, error.message, retryable=error.retryable) from error
        surface["trusted_surface"] = trusted_surface
        revision = runtime_outcome.get("state_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < app["state"]["revision"]:
            worker.terminate()
            self._ui_workers.pop((app_id, surface_id), None)
            self._ui_refresh_at.pop((app_id, surface_id), None)
            raise DaemonError("stale-revision", "UI action returned a regressed state revision")
        app["state"]["revision"] = revision
        self._append_audit(state, "ui-action", principal=principal, app_id=app_id, result="delivered", fields={"surface": surface_id, "generation": generation, "action": action, "event_id": event_id})
        return {
            "tag": "ui-updated",
            "value": {
                "entrypoint": entrypoint,
                "package_digest_sha256": package_digest,
                "component_sha256": component_digest,
                "generation": generation,
                "session": session,
                "surface": surface_id,
                "route": route,
                "semantic_surface": trusted_surface,
            },
        }

    def _ui_refresh(
        self,
        state: dict[str, Any],
        app_id: str,
        value: Any,
        principal: str,
    ) -> dict[str, Any]:
        expected = {
            "entrypoint",
            "package_digest_sha256",
            "component_sha256",
            "generation",
            "session",
            "surface",
            "route",
            "event_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise DaemonError("invalid-argument", "UI refresh fields are invalid")
        app = state["apps"][app_id]
        if not app["enabled"]:
            raise DaemonError("app-disabled", "disabled app cannot refresh a UI surface")
        package_digest = _digest(value["package_digest_sha256"], "package_digest_sha256")
        component_digest = _digest(value["component_sha256"], "component_sha256")
        entrypoint = _bounded_text(value["entrypoint"], "entrypoint", 128)
        generation = _bounded_text(value["generation"], "generation", 256)
        session = _bounded_text(value["session"], "session", 256)
        surface_id = _bounded_text(value["surface"], "surface", 256)
        route = _bounded_text(value["route"], "route", 128)
        event_id = _bounded_text(value["event_id"], "event_id", 128)
        surface = app["surfaces"].get(surface_id)
        feedback = surface.get("render_feedback") if isinstance(surface, dict) else None
        if (
            surface is None
            or surface.get("state") != "open"
            or surface.get("entrypoint") != entrypoint
            or surface.get("session") != session
            or surface.get("route") != route
            or surface.get("generation") != generation
            or surface.get("package_digest_sha256") != package_digest
            or surface.get("component_sha256") != component_digest
            or app["package_digest_sha256"] != package_digest
            or app["component_sha256"] != component_digest
            or not isinstance(feedback, dict)
            or feedback.get("principal") != principal
        ):
            raise DaemonError(
                "forged-identifier",
                "UI refresh does not match the current trusted binding",
            )
        key = (app_id, surface_id)
        worker = self._ui_workers.get(key)
        if worker is None or worker.generation != generation:
            raise DaemonError("stale-revision", "UI refresh generation is no longer live")
        now = self.monotonic_clock()
        last_refresh = self._ui_refresh_at.get(key)
        if (
            last_refresh is not None
            and now - last_refresh < MIN_UI_REFRESH_INTERVAL_SECONDS
        ):
            raise DaemonError(
                "resource-limit",
                "UI surface refresh is limited to one successful delivery per second",
                retryable=True,
            )
        try:
            runtime_outcome = worker.ui_refresh(
                session=session,
                surface=surface_id,
                route=route,
                event_id=event_id,
            )
            trusted_surface = self._trusted_ui_surface(
                runtime_outcome,
                session=session,
                surface=surface_id,
                route=route,
            )
        except ServiceExecutionError as error:
            self._ui_workers.pop(key, None)
            self._ui_refresh_at.pop(key, None)
            surface["state"] = "closed"
            surface["closed_at_utc"] = self.clock()
            surface["close_reason"] = "ui-refresh-failed"
            self._rollback_observation_update(
                state,
                app_id,
                app,
                reason=f"candidate UI refresh failed: {error.message}",
                principal="runtime-observer",
            )
            self._write_state(state)
            raise DaemonError(error.code, error.message, retryable=error.retryable) from error
        previous_revision = app["state"]["revision"]
        revision = runtime_outcome.get("state_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < app["state"]["revision"]
        ):
            worker.terminate()
            self._ui_workers.pop(key, None)
            self._ui_refresh_at.pop(key, None)
            surface["state"] = "closed"
            surface["closed_at_utc"] = self.clock()
            surface["close_reason"] = "ui-refresh-stale-revision"
            self._write_state(state)
            raise DaemonError(
                "stale-revision",
                "UI refresh returned a regressed state revision",
            )
        surface["trusted_surface"] = trusted_surface
        app["state"]["revision"] = revision
        self._ui_refresh_at[key] = now
        if revision > previous_revision:
            self._write_state(state)
        return {
            "tag": "ui-updated",
            "value": {
                "entrypoint": entrypoint,
                "package_digest_sha256": package_digest,
                "component_sha256": component_digest,
                "generation": generation,
                "session": session,
                "surface": surface_id,
                "route": route,
                "semantic_surface": trusted_surface,
            },
        }

    def _ui_render_failure(
        self,
        state: dict[str, Any],
        app_id: str,
        value: Any,
        principal: str,
    ) -> dict[str, Any]:
        expected = {
            "entrypoint",
            "package_digest_sha256",
            "component_sha256",
            "generation",
            "session",
            "surface",
            "route",
            "event_id",
            "render_failure_token",
            "reason",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise DaemonError("invalid-argument", "UI failure report fields are invalid")
        app = state["apps"][app_id]
        entrypoint = _bounded_text(value["entrypoint"], "entrypoint", 128)
        package_digest = _digest(value["package_digest_sha256"], "package_digest_sha256")
        component_digest = _digest(value["component_sha256"], "component_sha256")
        generation = _bounded_text(value["generation"], "generation", 256)
        session = _bounded_text(value["session"], "session", 256)
        surface_id = _bounded_text(value["surface"], "surface", 256)
        route = _bounded_text(value["route"], "route", 128)
        event_id = _bounded_text(value["event_id"], "event_id", 128)
        token = _bounded_text(value["render_failure_token"], "render_failure_token", 128)
        reason = _bounded_text(value["reason"], "reason", 512)
        surface = app["surfaces"].get(surface_id)
        feedback = surface.get("render_feedback") if isinstance(surface, dict) else None
        supplied_hash = _sha256_bytes(token.encode("utf-8"))
        if (
            surface is None
            or surface.get("state") != "open"
            or surface.get("entrypoint") != entrypoint
            or surface.get("session") != session
            or surface.get("route") != route
            or surface.get("generation") != generation
            or surface.get("package_digest_sha256") != package_digest
            or surface.get("component_sha256") != component_digest
            or app.get("package_digest_sha256") != package_digest
            or app.get("component_sha256") != component_digest
            or not isinstance(feedback, dict)
            or feedback.get("schema_version") != UI_FEEDBACK_SCHEMA
            or feedback.get("principal") != principal
            or not isinstance(feedback.get("token_sha256"), str)
            or not hmac.compare_digest(feedback["token_sha256"], supplied_hash)
        ):
            raise DaemonError("forged-identifier", "UI failure report does not match the trusted launch binding")
        if feedback.get("used") is not False:
            raise DaemonError("conflict", "UI failure report token was already consumed")
        update = app.get("update")
        rollback = update.get("previous") if isinstance(update, dict) else None
        if (
            not isinstance(update, dict)
            or update.get("status") != "observation-window"
            or not isinstance(rollback, dict)
            or rollback.get("to_package_digest_sha256") != package_digest
        ):
            raise DaemonError("conflict", "UI generation is not inside its rollback observation window")
        feedback["used"] = True
        feedback["event_id"] = event_id
        feedback["reported_at_utc"] = self.clock()
        worker = self._ui_workers.pop((app_id, surface_id), None)
        self._ui_refresh_at.pop((app_id, surface_id), None)
        if worker is not None:
            worker.terminate()
        surface["state"] = "closed"
        surface["closed_at_utc"] = self.clock()
        surface["close_reason"] = "render-failure"
        if not self._rollback_observation_update(
            state,
            app_id,
            app,
            reason=f"candidate UI render failed: {reason}",
            principal=principal,
        ):
            raise DaemonError("internal", "validated UI rollback binding could not be applied", retryable=True)
        self._append_audit(state, "ui-render-failure", principal=principal, app_id=app_id, result="rolled-back", fields={"surface": surface_id, "generation": generation, "event_id": event_id})
        return {"tag": "accepted", "value": {"result": "rolled-back"}}

    def _surface_close(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"session", "surface"}:
            raise DaemonError("invalid-argument", "surface-close fields are invalid")
        session = _bounded_text(value["session"], "session", 256)
        surface_id = _bounded_text(value["surface"], "surface", 256)
        app = state["apps"][app_id]
        surface = app["surfaces"].get(surface_id)
        if surface is None or surface["session"] != session:
            raise DaemonError("forged-identifier", "surface/session does not belong to the subject")
        if surface["state"] == "open":
            worker = self._ui_workers.pop((app_id, surface_id), None)
            self._ui_refresh_at.pop((app_id, surface_id), None)
            if worker is not None:
                worker.terminate()
            surface["state"] = "closed"
            surface["closed_at_utc"] = self.clock()
            surface["close_reason"] = "ui-close"
            self._append_audit(state, "ui-close", principal=principal, app_id=app_id, result="closed", fields={"surface": surface_id})
        return {"tag": "accepted", "value": None}

    def _status(self, state: dict[str, Any], app_id: str, value: Any) -> dict[str, Any]:
        self._require_null(value, "status")
        app = state["apps"][app_id]
        running = sorted(entrypoint for entrypoint, service in app["services"].items() if service["state"] == "running")
        return {"tag": "status", "value": {
            "app": app_id,
            "version": app["version"],
            "kind": app["kind"],
            "display_name": app["display_name"],
            "publisher_id": app["publisher_id"],
            "publisher_display_name": app["publisher_display_name"],
            "permissions": _copy_json(app["permissions"]),
            "presentation": _copy_json(app["presentation"]),
            "enabled": app["enabled"],
            "active_profile": "desktop" if app["enabled"] else None,
            "active_service_entrypoints": running,
            "last_error": app["last_error"],
            "package_digest_sha256": app["package_digest_sha256"],
            "component_sha256": app["component_sha256"],
            "state": _copy_json(app["state"]),
            "update": _copy_json(app["update"]),
            "gc": _copy_json(app["gc"]),
            "surfaces": [
                self._public_surface(surface)
                for _surface_id, surface in sorted(app["surfaces"].items())
            ],
            "services": [
                {
                    "entrypoint": entrypoint,
                    "state": service.get("state"),
                    "instance": service.get("instance"),
                    "generation": service.get("generation"),
                    "started_at_utc": service.get("started_at_utc"),
                    "start_reason": service.get("start_reason"),
                    "guest_start_reason": service.get("guest_start_reason"),
                    "recovered_at_utc": service.get("recovered_at_utc"),
                    "guest_execution": bool(service.get("guest_execution")),
                    "health": _copy_json(service.get("health")),
                    "last_error": _copy_json(service.get("last_error")),
                    "event_count": int(service.get("event_count", 0)),
                    "last_trigger": _copy_json(service.get("last_trigger")),
                    "package_digest_sha256": service.get("package_digest_sha256"),
                    "state_revision": service.get("state_revision"),
                }
                for entrypoint, service in sorted(app["services"].items())
            ],
        }}

    @staticmethod
    def _public_surface(surface: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: _copy_json(value)
            for key, value in surface.items()
            if key != "render_feedback"
        }
        feedback = surface.get("render_feedback")
        if isinstance(feedback, dict):
            result["render_feedback"] = {
                "schema_version": feedback.get("schema_version"),
                "used": feedback.get("used"),
                "issued_at_utc": feedback.get("issued_at_utc"),
                "reported_at_utc": feedback.get("reported_at_utc"),
                "event_id": feedback.get("event_id"),
            }
        return result

    def _uninstall(self, state: dict[str, Any], app_id: str, value: Any, principal: str) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"disposition"}:
            raise DaemonError("invalid-argument", "uninstall request fields are invalid")
        disposition = value["disposition"]
        app = state["apps"][app_id]
        if disposition not in app["allowed_data_dispositions"]:
            raise DaemonError("invalid-argument", "requested data disposition is not allowed by the manifest")
        stopped = self._quiesce(app_id, app, reason="uninstall")
        app["enabled"] = False
        app["lifecycle_state"] = "uninstall-deactivated"
        data_dir = self.data_root / app_id
        archive_id = f"{app_id}-{state['sequence'] + 1}"
        if disposition == "retain":
            destination = self.retained_root / archive_id
            if data_dir.exists():
                os.replace(data_dir, destination)
            state["retained"][archive_id] = {"app_id": app_id, "publisher_id": app["publisher_id"], "sealed": True, "guest_accessible": False, "retained_at_utc": self.clock()}
        elif disposition == "export-then-delete":
            export_path = self.exports_root / f"{archive_id}.json"
            export = {"schema_version": "vibapp.app-data-export.experimental-v1", "app_id": app_id, "publisher_id": app["publisher_id"], "exported_at_utc": self.clock(), "data_present": data_dir.exists()}
            descriptor = os.open(export_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json(export) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            state["exports"][archive_id] = {"app_id": app_id, "path": str(export_path.relative_to(self.root)), "created_at_utc": self.clock()}
            if data_dir.exists():
                shutil.rmtree(data_dir)
        elif data_dir.exists():
            shutil.rmtree(data_dir)
        package_path = self.packages_root / app_id
        if package_path.exists() and package_path.is_relative_to(self.packages_root):
            shutil.rmtree(package_path)
        update_path = self.updates_root / app_id
        if update_path.exists() and update_path.is_relative_to(self.updates_root):
            shutil.rmtree(update_path)
        self._append_audit(state, "uninstall", principal=principal, app_id=app_id, result=disposition, fields={"services_stopped": stopped, "package_removed": True})
        del state["apps"][app_id]
        return {"tag": "accepted", "value": None}

    def _rollback_observation_update(
        self,
        state: dict[str, Any],
        app_id: str,
        app: dict[str, Any],
        *,
        reason: str,
        principal: str,
    ) -> bool:
        update = app.get("update")
        rollback = update.get("previous") if isinstance(update, dict) else None
        if (
            not isinstance(update, dict)
            or update.get("status") != "observation-window"
            or not isinstance(rollback, dict)
            or rollback.get("schema_version") != UPDATE_SCHEMA
            or rollback.get("to_package_digest_sha256") != app.get("package_digest_sha256")
        ):
            return False
        previous = rollback.get("previous")
        if not isinstance(previous, dict):
            raise DaemonError("integrity-failure", "rollback record lacks the previous generation")
        candidate_digest = app["package_digest_sha256"]
        current_sequence = app.get("service_instance_sequence", 0)
        current_surfaces = _copy_json(app.get("surfaces", {}))
        self._quiesce(app_id, app, reason="unhealthy", close_surfaces=False)
        active_state = self.data_root / app_id
        previous_state = self._update_path(rollback.get("previous_state_path"), "previous_state_path")
        transaction_root = self._update_path(rollback.get("transaction_root"), "transaction_root")
        if not previous_state.is_dir() or previous_state.is_symlink():
            raise DaemonError("integrity-failure", "rollback state snapshot is unavailable")
        if active_state.exists():
            shutil.rmtree(active_state)
        os.replace(previous_state, active_state)
        self._fsync_directory(self.data_root)
        self._restore_previous_metadata(app, previous)
        app["surfaces"] = current_surfaces
        app["service_instance_sequence"] = max(
            int(current_sequence),
            int(app.get("service_instance_sequence", 0)),
        )
        app["services"] = {}
        app["last_error"] = None
        recovery_errors: list[str] = []
        if app.get("enabled"):
            for entrypoint in previous.get("running_service_entrypoints", []):
                try:
                    app["services"][entrypoint] = self._activate_service_generation(
                        app_id,
                        app,
                        entrypoint,
                        reason="update",
                    )
                except DaemonError as error:
                    recovery_errors.append(f"{entrypoint}:{error.code}")
                    app["services"][entrypoint] = {
                        "entrypoint": entrypoint,
                        "state": "failed",
                        "generation": None,
                        "last_error": self._service_error(
                            error.code,
                            error.message,
                            error.retryable,
                            "rollback-recovery",
                            at_utc=self.clock(),
                        ),
                    }
        self._remove_package(app_id, candidate_digest)
        if transaction_root.exists():
            shutil.rmtree(transaction_root)
        last = update.get("last_transaction")
        transaction = _copy_json(last) if isinstance(last, dict) else {
            "schema_version": UPDATE_SCHEMA,
            "transaction_id": rollback.get("transaction_id"),
            "from_package_digest_sha256": previous["package_digest_sha256"],
            "to_package_digest_sha256": candidate_digest,
            "from_version": previous["version"],
            "to_version": None,
        }
        transaction["status"] = "rolled-back"
        transaction["completed_at_utc"] = self.clock()
        transaction["rollback_reason"] = reason[:512].replace("\n", " ").replace("\r", " ")
        transaction["error"] = {
            "code": "internal",
            "message": transaction["rollback_reason"],
            "retryable": bool(recovery_errors),
        }
        if recovery_errors:
            transaction["rollback_recovery_errors"] = recovery_errors
            app["last_error"] = "internal"
        update["status"] = "rolled-back"
        update["pending"] = None
        update["previous"] = None
        self._append_update_history(app, transaction)
        app["lifecycle_state"] = "enabled" if app.get("enabled") else "disabled"
        self._append_audit(
            state,
            "update-rollback",
            principal=principal,
            app_id=app_id,
            result="rolled-back",
            fields={
                "restored_package_digest_sha256": previous["package_digest_sha256"],
                "rejected_package_digest_sha256": candidate_digest,
                "rollback_reason": transaction["rollback_reason"],
            },
        )
        return True

    def record_service_crash(self, app_id: str, entrypoint: str, *, at_utc: str | None = None) -> dict[str, Any]:
        """Trusted runtime-observer hook; not exposed as a guest/control command."""
        observed_at = at_utc or self.clock()
        now = _parse_utc(observed_at)
        with self._locked_state() as state:
            app = state["apps"].get(app_id)
            if app is None:
                raise DaemonError("not-found", "installed app was not found")
            service = app["services"].get(entrypoint)
            if service is None or service["state"] != "running":
                raise DaemonError("not-found", "running service instance was not found")
            worker = self._workers.pop((app_id, entrypoint), None)
            if worker is not None:
                worker.terminate()
            service["state"] = "stopped"
            service["stop_reason"] = "abnormal-termination"
            service["stopped_at_utc"] = observed_at
            if self._rollback_observation_update(
                state,
                app_id,
                app,
                reason=f"candidate generation {service.get('generation')} terminated abnormally",
                principal="runtime-observer",
            ):
                self._write_state(state)
                return {"result": "rolled-back", "window_count": 1, "quarantined": False}
            threshold = now - dt.timedelta(seconds=CRASH_LOOP_WINDOW_SECONDS)
            recent = [item for item in app["crash_events"] if _parse_utc(item) >= threshold]
            recent.append(observed_at)
            recent.sort()
            app["crash_events"] = recent[-CRASH_LOOP_COUNT:]
            if len(recent) >= CRASH_LOOP_COUNT:
                app["quarantined"] = True
                app["enabled"] = False
                app["lifecycle_state"] = "quarantined"
                app["last_error"] = "resource-limit"
                self._quiesce(app_id, app, reason="crash-loop")
                result = "quarantined"
            else:
                result = "restart-eligible"
            self._append_audit(state, "service-crash", principal="runtime-observer", app_id=app_id, result=result, fields={"entrypoint": entrypoint, "window_count": len(recent)})
            self._write_state(state)
            return {"result": result, "window_count": len(recent), "quarantined": app["quarantined"]}

    def shutdown(self) -> None:
        """Drop volatile Stores while preserving desired running state for recovery."""
        with self._thread_lock:
            workers = list(self._workers.values())
            self._workers.clear()
            ui_workers = list(self._ui_workers.values())
            self._ui_workers.clear()
            self._ui_refresh_at.clear()
        for worker in workers:
            with contextlib.suppress(ServiceExecutionError):
                worker.stop("host-shutdown")
        for worker in ui_workers:
            worker.terminate()

    def inspect_state(self) -> dict[str, Any]:
        """Read-only test/diagnostic snapshot; not a daemon control response."""
        with self._locked_state() as state:
            return _copy_json(state)
