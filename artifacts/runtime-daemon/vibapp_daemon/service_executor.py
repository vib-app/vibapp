from __future__ import annotations

import ctypes
import json
import os
import platform
import selectors
import signal
import shutil
import stat
import subprocess
import time
import uuid
import threading
import queue
from pathlib import Path
from typing import Any
from .host_storage import owner_controlled, protect


PROTOCOL_SCHEMA = "vibapp.service-runtime.protocol.experimental-v1"
MAX_RUNTIME_OUTPUT_BYTES = 256 * 1024
STARTUP_TIMEOUT_SECONDS = 5.0
EVENT_TIMEOUT_SECONDS = 1.0
HEALTH_TIMEOUT_SECONDS = 3.0
MIGRATION_TIMEOUT_SECONDS = 3.0
MAX_RUNTIME_RESIDENT_BYTES = 512 * 1024 * 1024
MEMORY_SAMPLE_SECONDS = 0.05


def _strict_response_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate runtime response field")
        result[key] = value
    return result


def _reject_response_constant(value: str) -> None:
    raise ValueError("non-finite runtime response number")


class _DarwinTaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("context_switches", ctypes.c_int32),
        ("thread_count", ctypes.c_int32),
        ("running_threads", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


def _resident_bytes(pid: int) -> int | None:
    system = platform.system()
    if system == "Darwin":
        try:
            libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            proc_pidinfo.restype = ctypes.c_int
            info = _DarwinTaskInfo()
            size = ctypes.sizeof(info)
            if proc_pidinfo(pid, 4, 0, ctypes.byref(info), size) == size:
                return int(info.resident_size)
        except (AttributeError, OSError):
            return None
    elif system == "Linux":
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
    return None


class ServiceExecutionError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, ui_rejection_revision: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message[:512]
        self.retryable = retryable
        # Internal host evidence only; never part of the public error envelope.
        self.ui_rejection_revision = ui_rejection_revision

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def resolve_runtime_binary(explicit: Path | str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
    configured = os.environ.get("VIBAPP_SERVICE_RUNTIME_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())
    artifact_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            artifact_root / "target-service-1_98" / "release" / "vibapp-service-runtime",
            artifact_root / "target-service-1_98" / "debug" / "vibapp-service-runtime",
            artifact_root / "service-runtime" / "vibapp-service-runtime",
        ]
    )
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        trusted_owner = owner_controlled(candidate, info, system=True)
        writable_by_others = os.name != "nt" and bool(info.st_mode & 0o022)
        if (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and trusted_owner
            and not writable_by_others
            and (os.name == "nt" or info.st_mode & 0o111)
        ):
            return candidate.resolve(strict=True)
    raise ServiceExecutionError(
        "capability-unavailable",
        "the bounded VibApp service runtime binary is not installed or is not owner-controlled",
        retryable=True,
    )


class ServiceWorker:
    """One disposable Wasmtime Store in one daemon-owned child process."""

    def __init__(
        self,
        *,
        binary: Path,
        component: Path,
        expected_sha256: str,
        package_digest_sha256: str,
        app_id: str,
        app_version: str,
        entrypoint: str,
        entrypoint_kind: str,
        instance: str,
        generation: str,
        runtime_world: str,
        runtime_root: Path,
        state_directory: Path,
        state_revision: int,
        kv_maximum_bytes: int | None,
        settings_snapshot: dict[str, Any],
    ) -> None:
        self.app_id = app_id
        self.entrypoint = entrypoint
        self.instance = instance
        self.generation = generation
        self._closed = False
        self._context_directory: Path | None = None
        try:
            state_directory = state_directory.resolve(strict=True)
            runtime_root = runtime_root.resolve(strict=True)
            state_info = state_directory.lstat()
        except OSError as exc:
            raise ServiceExecutionError(
                "integrity-failure",
                "the generation state directory is unavailable",
            ) from exc
        if (
            not state_directory.is_relative_to(runtime_root)
            or not stat.S_ISDIR(state_info.st_mode)
            or stat.S_ISLNK(state_info.st_mode)
            or state_revision <= 0
            or kv_maximum_bytes is not None
            and not 1 <= kv_maximum_bytes <= 10 * 1024 * 1024
        ):
            raise ServiceExecutionError(
                "integrity-failure",
                "the generation state binding is outside its trusted bounds",
            )
        encoded_settings = json.dumps(
            settings_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        if len(encoded_settings) > MAX_RUNTIME_OUTPUT_BYTES:
            raise ServiceExecutionError(
                "resource-limit",
                "settings snapshot exceeds the runtime context bound",
            )
        context_root = runtime_root / "runtime-contexts"
        context_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(context_root, directory=True)
        context_directory = context_root / uuid.uuid4().hex
        context_directory.mkdir(mode=0o700)
        settings_path = context_directory / "settings.json"
        descriptor = os.open(
            settings_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded_settings)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            shutil.rmtree(context_directory, ignore_errors=True)
            raise
        self._context_directory = context_directory
        arguments = [
            str(binary),
            "--component", str(component),
            "--expected-sha256", expected_sha256,
            "--package-digest-sha256", package_digest_sha256,
            "--app-id", app_id,
            "--app-version", app_version,
            "--entrypoint", entrypoint,
            "--entrypoint-kind", entrypoint_kind,
            "--instance", instance,
            "--generation", generation,
            "--world", runtime_world,
            "--state-directory", str(state_directory),
            "--state-revision", str(state_revision),
            "--kv-enabled", "true" if kv_maximum_bytes is not None else "false",
            "--kv-maximum-bytes", str(kv_maximum_bytes or 1),
            "--settings-snapshot", str(settings_path),
        ]
        try:
            self.process = subprocess.Popen(
                arguments,
                cwd=runtime_root,
                env={"LANG": "C", "LC_ALL": "C"},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            shutil.rmtree(context_directory, ignore_errors=True)
            self._context_directory = None
            raise ServiceExecutionError("capability-unavailable", "the bounded service runtime could not be started", retryable=True) from exc
        try:
            ready = self._read_response(STARTUP_TIMEOUT_SECONDS)
            if ready.get("schema_version") != PROTOCOL_SCHEMA or ready.get("tag") != "ready":
                error = ready.get("error")
                if isinstance(error, dict):
                    raise ServiceExecutionError(
                        str(error.get("code", "incompatible-contract")),
                        str(error.get("message", "service runtime rejected the component")),
                        retryable=bool(error.get("retryable", False)),
                    )
                raise ServiceExecutionError("incompatible-contract", "service runtime did not return a canonical ready record")
            if ready.get("component_sha256") != expected_sha256 or ready.get("package_digest_sha256") != package_digest_sha256 or ready.get("generation") != generation or ready.get("entrypoint") != entrypoint or ready.get("world") != runtime_world:
                raise ServiceExecutionError("integrity-failure", "service runtime ready record is not bound to the requested generation")
        except Exception:
            self.terminate()
            raise

    @property
    def pid(self) -> int:
        return self.process.pid

    def _read_response(self, timeout: float) -> dict[str, Any]:
        if self.process.stdout is None:
            raise ServiceExecutionError("internal", "service runtime output channel is unavailable")
        if os.name == "nt":
            line = self._read_windows_line(timeout)
        else:
            line = self._read_unix_line(timeout)
        if not line:
            self.terminate()
            raise ServiceExecutionError("internal", "service runtime exited without a response", retryable=True)
        if len(line) > MAX_RUNTIME_OUTPUT_BYTES + 1 or not line.endswith(b"\n"):
            self.terminate()
            raise ServiceExecutionError("resource-limit", "service runtime response exceeds its byte bound")
        try:
            value = json.loads(line, object_pairs_hook=_strict_response_object, parse_constant=_reject_response_constant)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self.terminate()
            raise ServiceExecutionError("malformed-output", "service runtime response is not strict JSON") from exc
        if not isinstance(value, dict) or value.get("schema_version") != PROTOCOL_SCHEMA:
            self.terminate()
            raise ServiceExecutionError("malformed-output", "service runtime response schema is invalid")
        return value

    def _read_windows_line(self, timeout: float) -> bytes:
        # Anonymous Windows process pipes cannot be waited on by select(). One
        # bounded reader is joined after timeout/termination, never abandoned.
        result = queue.Queue(maxsize=1)
        def read():
            try:
                result.put(self.process.stdout.readline(MAX_RUNTIME_OUTPUT_BYTES + 2))
            except OSError:
                result.put(b"")
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            return result.get(timeout=timeout)
        except queue.Empty:
            self.process.kill()
            self.process.wait(timeout=1)
            reader.join(timeout=1)
            self.terminate()
            raise ServiceExecutionError("deadline-exceeded", "service runtime exceeded its outer deadline", retryable=True)
        finally:
            if not reader.is_alive():
                reader.join()

    def _read_unix_line(self, timeout: float) -> bytes:
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while True:
                resident = _resident_bytes(self.process.pid)
                if resident is not None and resident > MAX_RUNTIME_RESIDENT_BYTES:
                    self.terminate()
                    raise ServiceExecutionError(
                        "resource-limit",
                        "service runtime exceeded its 512 MiB resident-memory ceiling",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.terminate()
                    raise ServiceExecutionError(
                        "deadline-exceeded",
                        "service runtime exceeded its outer deadline",
                        retryable=True,
                    )
                if selector.select(min(MEMORY_SAMPLE_SECONDS, remaining)):
                    break
        finally:
            selector.close()
        return self.process.stdout.readline(MAX_RUNTIME_OUTPUT_BYTES + 2)

    def _ui_rejection_revision(self, response: dict[str, Any], command: dict[str, Any]) -> int | None:
        marker = response.get("ui_input_rejection")
        error = response.get("error")
        if (
            command.get("command") != "ui-action"
            or set(response) != {"schema_version", "request_id", "error", "ui_input_rejection"}
            or response.get("schema_version") != PROTOCOL_SCHEMA
            or not isinstance(error, dict)
            or set(error) != {"code", "message", "retryable"}
            or error.get("code") != "invalid-argument"
            or not isinstance(error.get("message"), str)
            or len(error["message"]) > 512
            or type(error.get("retryable")) is not bool
            or not isinstance(marker, dict)
            or set(marker) != {"origin", "generation", "session", "surface", "route", "event_id", "state_revision"}
            or marker.get("origin") != "typed-guest-ui-action"
            or marker.get("generation") != self.generation
            or any(not isinstance(command.get(key), str) or not command[key] or marker.get(key) != command[key]
                   for key in ("session", "surface", "route", "event_id"))
        ):
            return None
        revision = marker["state_revision"]
        return revision if type(revision) is int and 0 < revision <= 2**64 - 1 else None

    def request(self, command: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if self._closed or self.process.poll() is not None or self.process.stdin is None:
            raise ServiceExecutionError("internal", "service generation is no longer running", retryable=True)
        request_id = f"runtime-{uuid.uuid4().hex}"
        payload = {"request_id": request_id, **command}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > 64 * 1024:
            raise ServiceExecutionError("resource-limit", "service runtime command exceeds 64 KiB")
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.terminate()
            raise ServiceExecutionError("internal", "service runtime command channel closed", retryable=True) from exc
        response = self._read_response(timeout)
        if response.get("request_id") != request_id:
            self.terminate()
            raise ServiceExecutionError("forged-identifier", "service runtime response request identity is invalid")
        error = response.get("error")
        if isinstance(error, dict):
            rejection_revision = self._ui_rejection_revision(response, command)
            if rejection_revision is None:
                self.terminate()
            raise ServiceExecutionError(
                str(error.get("code", "internal")),
                str(error.get("message", "service runtime failed")),
                retryable=bool(error.get("retryable", False)),
                ui_rejection_revision=rejection_revision,
            )
        outcome = response.get("outcome")
        if not isinstance(outcome, dict):
            self.terminate()
            raise ServiceExecutionError("malformed-output", "service runtime response has no typed outcome")
        return outcome

    def start(self, reason: str) -> dict[str, Any]:
        return self.request({"command": "start", "reason": reason}, timeout=EVENT_TIMEOUT_SECONDS)

    def validate(self) -> dict[str, Any]:
        return self.request({"command": "validate"}, timeout=HEALTH_TIMEOUT_SECONDS)

    def trigger(self, trigger_id: str, payload: list[int]) -> dict[str, Any]:
        return self.request({"command": "trigger", "trigger_id": trigger_id, "payload": payload}, timeout=EVENT_TIMEOUT_SECONDS)

    def health(self) -> dict[str, Any]:
        return self.request({"command": "health"}, timeout=HEALTH_TIMEOUT_SECONDS)

    def ui_launch(self, *, session: str, surface: str, route: str) -> dict[str, Any]:
        return self.request(
            {
                "command": "ui-launch",
                "session": session,
                "surface": surface,
                "route": route,
            },
            timeout=EVENT_TIMEOUT_SECONDS,
        )

    def ui_action(
        self,
        *,
        session: str,
        surface: str,
        route: str,
        action: str,
        event_id: str,
        fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.request(
            {
                "command": "ui-action",
                "session": session,
                "surface": surface,
                "route": route,
                "action": action,
                "event_id": event_id,
                "fields": fields,
            },
            timeout=EVENT_TIMEOUT_SECONDS,
        )

    def ui_refresh(
        self,
        *,
        session: str,
        surface: str,
        route: str,
        event_id: str,
    ) -> dict[str, Any]:
        return self.request(
            {
                "command": "ui-refresh",
                "session": session,
                "surface": surface,
                "route": route,
                "event_id": event_id,
            },
            timeout=EVENT_TIMEOUT_SECONDS,
        )

    def rebind_state(self, state_directory: Path) -> dict[str, Any]:
        return self.request(
            {"command": "rebind-state", "state_directory": str(state_directory)},
            timeout=EVENT_TIMEOUT_SECONDS,
        )

    def migrate(
        self,
        *,
        from_schema: int,
        to_schema: int,
        source_state_revision: int,
        target_state_revision: int,
    ) -> dict[str, Any]:
        return self.request(
            {
                "command": "migrate",
                "from_schema": from_schema,
                "to_schema": to_schema,
                "source_state_revision": source_state_revision,
                "target_state_revision": target_state_revision,
            },
            timeout=MIGRATION_TIMEOUT_SECONDS,
        )

    def stop(self, reason: str) -> dict[str, Any]:
        try:
            return self.request({"command": "stop", "reason": reason}, timeout=EVENT_TIMEOUT_SECONDS)
        finally:
            self.terminate()

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            try:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "nt":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.process.kill()
                self.process.wait(timeout=1.0)
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.stdout:
            self.process.stdout.close()
        if self._context_directory is not None:
            shutil.rmtree(self._context_directory, ignore_errors=True)
            self._context_directory = None
