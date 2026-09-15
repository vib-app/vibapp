#!/usr/bin/env python3
"""Shared, authority-neutral primitives for the product-layer Builder pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
APP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOST_PRESENTATION_SCHEMA = "vibapp.host-presentation.experimental-v1"
HOST_PRESENTATION_FIELDS = {
    "schema_version",
    "preferred_width",
    "preferred_height",
    "minimum_width",
    "minimum_height",
    "resizable",
}

# Capability metadata has three independent inputs:
#
# * the selected WIT world fixes the structural import set;
# * target.required_capabilities fixes which imports are app requirements;
# * this table records only current host implementation availability.
#
# Keeping grant policy and host availability separate prevents a linkable fixed
# world import from silently becoming required authority.  The Builder denies
# live authority to structural-only imports and links their typed stubs.
CAPABILITY_GRANT_POLICY: dict[str, str] = {
    "vibapp:experimental-v0/clock@0.0.1": "automatic",
    "vibapp:experimental-v0/scheduler@0.0.1": "user",
    "vibapp:experimental-v0/notification@0.0.1": "user",
    "vibapp:experimental-v0/kv@0.0.1": "automatic",
    "vibapp:experimental-v0/log@0.0.1": "automatic",
    "vibapp:experimental-v0/host-info@0.0.1": "automatic",
    "vibapp:experimental-v0/settings@0.0.1": "automatic",
    "vibapp:experimental-v0/system-metrics@0.0.1": "user",
    "vibapp:experimental-v0/http@0.0.1": "user",
}

LOCAL_HOST_CAPABILITY_AVAILABILITY: dict[str, str] = {
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


class PipelineError(RuntimeError):
    """A closed, user-safe pipeline failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PipelineError("schema-invalid", f"duplicate JSON key: {key}")
        value[key] = item
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PipelineError("schema-invalid", f"{context} is not lowercase SHA-256")
    return value


def require_exact_object(value: Any, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineError("schema-invalid", f"{context} must be an object")
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing or extra:
        raise PipelineError(
            "schema-invalid",
            f"{context} fields mismatch missing={missing} extra={extra}",
        )
    return value


def lstat_regular(path: Path, maximum_bytes: int, context: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise PipelineError("not-found", f"{context} is missing") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise PipelineError("integrity-failure", f"{context} must be one regular unlinked file")
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise PipelineError(
            "resource-limit", f"{context} size must be 1..{maximum_bytes} bytes"
        )
    return metadata


def read_bounded(path: Path, maximum_bytes: int, context: str) -> bytes:
    metadata = lstat_regular(path, maximum_bytes, context)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise PipelineError("integrity-failure", f"{context} changed while opening")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != metadata.st_size or len(data) > maximum_bytes:
            raise PipelineError("integrity-failure", f"{context} changed while reading")
        return data
    finally:
        os.close(descriptor)


def sha256_file(path: Path, maximum_bytes: int, context: str) -> str:
    return sha256_bytes(read_bounded(path, maximum_bytes, context))


def load_json(path: Path, maximum_bytes: int, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            read_bounded(path, maximum_bytes, context), object_pairs_hook=strict_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineError("schema-invalid", f"invalid {context} JSON: {error}") from error
    if not isinstance(value, dict):
        raise PipelineError("schema-invalid", f"{context} JSON root must be an object")
    return value


def normalized_relative_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240:
        raise PipelineError("schema-invalid", f"{context} is not a bounded path")
    if "\\" in value or "//" in value or value.startswith("/") or value.endswith("/"):
        raise PipelineError("integrity-failure", f"{context} is not canonical")
    pure = PurePosixPath(value)
    if any(part in {"", ".", ".."} or not PATH_SEGMENT_RE.fullmatch(part) for part in pure.parts):
        raise PipelineError("integrity-failure", f"{context} contains an unsafe segment")
    if pure.as_posix() != value:
        raise PipelineError("integrity-failure", f"{context} normalization changed")
    return value


def safe_child(root: Path, relative: str, context: str) -> Path:
    relative = normalized_relative_path(relative, context)
    root_resolved = root.resolve(strict=True)
    candidate = root_resolved.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise PipelineError("not-found", f"{context} is missing or cyclic") from error
    if root_resolved not in resolved.parents:
        raise PipelineError("integrity-failure", f"{context} escapes its root")
    if resolved != candidate:
        raise PipelineError("integrity-failure", f"{context} traverses a link")
    return candidate


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PipelineError("integrity-failure", f"directory is unsafe: {path}")
    return path.resolve(strict=True)


def exclusive_copy(source: Path, destination: Path, maximum_bytes: int) -> None:
    data = read_bounded(source, maximum_bytes, f"copy source {source.name}")
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise


def exclusive_write(path: Path, payload: bytes, mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_json(path: Path, value: Any, mode: int = 0o400) -> None:
    payload = canonical_json(value) + b"\n"
    temporary = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    exclusive_write(temporary, payload, mode=0o600)
    try:
        os.replace(temporary, path)
        path.chmod(mode)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def source_tree_digest(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"VIBAPP-CODEAGENT-SOURCE\0")
    for record in sorted(records, key=lambda row: row["path"].encode("utf-8")):
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(record["sha256"]))
        digest.update(int(record["size_bytes"]).to_bytes(8, "big"))
    return digest.hexdigest()


def package_digest(manifest: dict[str, Any]) -> str:
    try:
        artifacts = manifest["artifacts"]
        descriptors: list[dict[str, Any]] = [
            artifacts["canonical_component"],
            *artifacts["assets"],
            artifacts["provenance"],
            artifacts["sbom"],
        ]
        for derivation in artifacts["browser_derivations"]:
            descriptors.extend(derivation["files"])
            descriptors.append(derivation["derivation_attestation"])
        descriptors.sort(key=lambda item: item["path"].encode("utf-8"))
        manifest_bytes = canonical_json(manifest)
        preimage = bytearray(b"VIBAPP-PACKAGE\0experimental-v0\0")
        preimage.extend(len(manifest_bytes).to_bytes(8, "big"))
        preimage.extend(manifest_bytes)
        for descriptor in descriptors:
            path_bytes = descriptor["path"].encode("utf-8")
            preimage.extend(len(path_bytes).to_bytes(2, "big"))
            preimage.extend(path_bytes)
            preimage.extend(bytes.fromhex(descriptor["sha256"]))
            preimage.extend(int(descriptor["size_bytes"]).to_bytes(8, "big"))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PipelineError("schema-invalid", f"manifest cannot form package digest: {error}") from error
    return sha256_bytes(bytes(preimage))


def derive_host_presentation(
    app_kind: str, display_name: str, description: str
) -> dict[str, Any]:
    """Derive bounded host-owned window hints without changing the package ABI.

    These values are presentation only. They confer no native-window handle,
    placement, fullscreen, decoration, transparency, or always-on-top authority.
    """
    if app_kind not in {"ui", "service", "hybrid"}:
        raise PipelineError("schema-invalid", "presentation app kind is unsupported")
    if not isinstance(display_name, str) or not isinstance(description, str):
        raise PipelineError("schema-invalid", "presentation source text is invalid")
    normalized = unicodedata.normalize(
        "NFKC", f"{display_name}\n{description}"
    ).casefold()
    if app_kind == "service":
        dimensions = (720, 480, 480, 320, False)
    elif any(
        marker in normalized
        for marker in (
            "clock",
            "timer",
            "stopwatch",
            "counter",
            "calculator",
            "reminder",
            "时钟",
            "计时",
            "秒表",
            "倒计时",
            "计数器",
            "计算器",
            "提醒",
        )
    ):
        dimensions = (520, 300, 420, 240, True)
    elif any(
        marker in normalized
        for marker in (
            "dashboard",
            "workspace",
            "editor",
            "studio",
            "inventory",
            "kanban",
            "看板",
            "工作台",
            "编辑器",
            "库存",
            "管理台",
        )
    ):
        dimensions = (1120, 760, 720, 480, True)
    elif app_kind == "hybrid":
        dimensions = (1040, 720, 640, 420, True)
    else:
        dimensions = (900, 640, 480, 360, True)
    preferred_width, preferred_height, minimum_width, minimum_height, resizable = dimensions
    return {
        "schema_version": HOST_PRESENTATION_SCHEMA,
        "preferred_width": preferred_width,
        "preferred_height": preferred_height,
        "minimum_width": minimum_width,
        "minimum_height": minimum_height,
        "resizable": resizable,
    }


def validate_host_presentation(value: Any, context: str = "presentation") -> dict[str, Any]:
    presentation = require_exact_object(value, HOST_PRESENTATION_FIELDS, context)
    if presentation.get("schema_version") != HOST_PRESENTATION_SCHEMA:
        raise PipelineError("schema-invalid", f"{context} schema is unsupported")
    bounds = {
        "preferred_width": (320, 1920),
        "preferred_height": (240, 1200),
        "minimum_width": (320, 1280),
        "minimum_height": (240, 960),
    }
    normalized: dict[str, Any] = {"schema_version": HOST_PRESENTATION_SCHEMA}
    for field, (minimum, maximum) in bounds.items():
        item = presentation.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise PipelineError(
                "schema-invalid",
                f"{context}.{field} must be an integer in {minimum}..{maximum}",
            )
        normalized[field] = item
    if (
        normalized["preferred_width"] < normalized["minimum_width"]
        or normalized["preferred_height"] < normalized["minimum_height"]
    ):
        raise PipelineError(
            "schema-invalid", f"{context} preferred size must cover its minimum size"
        )
    if not isinstance(presentation.get("resizable"), bool):
        raise PipelineError("schema-invalid", f"{context}.resizable must be boolean")
    normalized["resizable"] = presentation["resizable"]
    return normalized


@dataclass(frozen=True)
class ProcessLimits:
    wall_seconds: int = 300
    cpu_seconds: int = 240
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    pids: int = 64
    disk_bytes: int = 512 * 1024 * 1024
    stdout_bytes: int = 512 * 1024
    stderr_bytes: int = 512 * 1024
    open_files: int = 256

    def as_dict(self) -> dict[str, int]:
        return {
            "wall_seconds": self.wall_seconds,
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "disk_bytes": self.disk_bytes,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "open_files": self.open_files,
            "concurrent_jobs": 1,
        }


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_ms: int
    peak_rss_bytes: int
    peak_pids: int
    disk_bytes: int


def bounded_tree_size(root: Path, ceiling: int) -> int:
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # Compiler and build-script scratch files may be created and removed
            # between scandir and lstat. A disappeared path contributes zero; all
            # paths that still exist remain subject to the same link/type checks.
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise PipelineError("integrity-failure", "bounded workspace contains a symlink")
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                with os.scandir(current) as entries:
                    stack.extend(Path(entry.path) for entry in entries)
            except FileNotFoundError:
                # rustc also removes entire temporary directories after lstat
                # but before scandir. Do not mistake this normal race for a
                # failed source build. Permission/type/link failures still fail.
                continue
        else:
            raise PipelineError("integrity-failure", "bounded workspace contains a special file")
        if total > ceiling:
            raise PipelineError("resource-limit", f"workspace exceeded {ceiling} bytes")
    return total


def _limit_child(limits: ProcessLimits) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.disk_bytes, limits.disk_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
    if sys.platform != "darwin" and hasattr(resource, "RLIMIT_NPROC"):
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (limits.pids, limits.pids))
        except (OSError, ValueError):
            # The parent still enforces the finite process-group ceiling on every
            # supervision tick if the platform rejects RLIMIT_NPROC.
            pass
    # Darwin accounts RLIMIT_NPROC against the entire login UID, not this build's
    # process group. Applying it would couple an isolated build to unrelated user
    # processes and can prevent rustc from starting. Parent supervision below
    # enforces the same PID ceiling against this process group only.
    if sys.platform == "darwin" and hasattr(resource, "RLIMIT_RSS"):
        try:
            resource.setrlimit(resource.RLIMIT_RSS, (limits.memory_bytes, limits.memory_bytes))
        except (OSError, ValueError):
            # Darwin treats RLIMIT_RSS as advisory. The parent still enforces the
            # same aggregate process-group RSS ceiling and kills the whole group.
            pass
    if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        except (OSError, ValueError):
            # Aggregate process-group RSS remains a mandatory parent-side kill
            # boundary even when the platform rejects RLIMIT_AS.
            pass


def _process_group_usage(process_group: int) -> tuple[int, int]:
    try:
        probe = subprocess.run(
            ["/bin/ps", "-o", "pid=,rss=", "-g", str(process_group)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (0, 0)
    rss_kib = 0
    pids = 0
    for line in probe.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            int(fields[0])
            rss_kib += int(fields[1])
            pids += 1
        except ValueError:
            continue
    return rss_kib * 1024, pids


def bounded_failure_diagnostic(stderr: bytes) -> str:
    """Keep the root compiler error and final summary within the repair budget.

    The process supervisor already bounds the complete stderr stream. Do not
    hand a repair agent only the last 4 KiB: Cargo's final rustc command can
    consume that entire tail, hiding the errors the agent actually must fix.
    This is untrusted diagnostic data, never a successful build or instruction.
    """
    payload = stderr.decode("utf-8", errors="replace").encode("utf-8")
    if len(payload) <= 11000:
        return payload.decode("utf-8")
    prefix = ""
    first_error = re.search(rb"(?m)^error(?:\[[A-Z][0-9]+\])?:", payload)
    if first_error is not None and first_error.start():
        payload = payload[first_error.start():]
        prefix = "[Earlier build output omitted; starting at first compiler error]\n"
    if len(payload) <= 11000:
        return prefix + payload.decode("utf-8")
    # Decode each bounded part separately so a cut UTF-8 character cannot grow
    # the diagnostic beyond the byte ceiling, even for malformed tool output.
    first = payload[:8500].decode("utf-8", errors="ignore")
    last = payload[-1800:].decode("utf-8", errors="ignore")
    return prefix + first + "\n[Middle diagnostics omitted]\n" + last


def run_bounded(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    limits: ProcessLimits,
    disk_root: Path,
    cancellation=None,
) -> ProcessResult:
    if not command or not all(isinstance(item, str) and item for item in command):
        raise PipelineError("invalid-argument", "bounded command is empty or malformed")
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        preexec_fn=lambda: _limit_child(limits),
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    peak_rss = 0
    peak_pids = 1
    disk_bytes = 0
    failure: PipelineError | None = None
    try:
        while selector.get_map() or process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                failure = PipelineError("cancelled", "Builder operation was cancelled")
                break
            elapsed = time.monotonic() - started
            if elapsed > limits.wall_seconds:
                failure = PipelineError("timeout", f"process exceeded {limits.wall_seconds}s")
                break
            rss, pids = _process_group_usage(process.pid)
            peak_rss = max(peak_rss, rss)
            peak_pids = max(peak_pids, pids)
            if rss > limits.memory_bytes:
                failure = PipelineError("resource-limit", "aggregate process RSS exceeded limit")
                break
            if pids > limits.pids:
                failure = PipelineError("resource-limit", "process group PID count exceeded limit")
                break
            try:
                disk_bytes = bounded_tree_size(disk_root, limits.disk_bytes)
            except PipelineError as error:
                failure = error
                break
            for key, _ in selector.select(timeout=0.05):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 65_536)
                if not chunk:
                    selector.unregister(stream)
                    continue
                name = key.data
                buffers[name].extend(chunk)
                ceiling = limits.stdout_bytes if name == "stdout" else limits.stderr_bytes
                if len(buffers[name]) > ceiling:
                    failure = PipelineError("resource-limit", f"{name} exceeded {ceiling} bytes")
                    break
            if failure is not None:
                break
        if failure is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            raise failure
        returncode = process.wait(timeout=5)
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if returncode != 0:
        diagnostic = bounded_failure_diagnostic(bytes(buffers["stderr"]))
        raise PipelineError("process-failed", f"bounded process rc={returncode}: {diagnostic}")
    return ProcessResult(
        returncode=returncode,
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
        elapsed_ms=elapsed_ms,
        peak_rss_bytes=peak_rss,
        peak_pids=peak_pids,
        disk_bytes=disk_bytes,
    )


def remove_tree(path: Path) -> None:
    """Remove only a caller-resolved private job staging directory."""
    if path.exists() and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
