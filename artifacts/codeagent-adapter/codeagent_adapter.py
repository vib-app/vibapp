#!/usr/bin/env python3
"""Pluggable, bounded local CodeAgent adapters.

This product-layer adapter may author source only.  It deliberately cannot compile,
verify, install, sign, launch, or publish a generated VibApp.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
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
from typing import Any, Protocol

LAUNCHER_DIR = Path(__file__).resolve().parent.parent / "codeagent-launcher"
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))
import host_budget


ADAPTER_VERSION = "vibapp.codeagent-adapter.experimental-v1"
STATUS_VERSION = "vibapp.codeagent-adapter-status.experimental-v2"
MAX_STATUS_BYTES = 128 * 1024
MAX_STATUS_EVENTS = 24
MAX_LOCAL_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
MAX_LOCAL_PIDS = 64
MAX_LOCAL_WALL_TIME_SECONDS = 1800
CODEX_PROVIDER_ID = "codex"
CLAUDE_PROVIDER_ID = "claude-code"
OPENCODE_PROVIDER_ID = "opencode"
GEMINI_PROVIDER_ID = "gemini-cli"
TASK_PROVIDER_BY_ID = {
    CODEX_PROVIDER_ID: "openai-codex",
    CLAUDE_PROVIDER_ID: "anthropic-claude-code",
    OPENCODE_PROVIDER_ID: "opencode",
    GEMINI_PROVIDER_ID: "google-gemini-cli",
}
CODEX_LAUNCHER_PATHS = (
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
)
CODEX_CASK_ROOTS = (
    Path("/opt/homebrew/Caskroom/codex"),
    Path("/usr/local/Caskroom/codex"),
)
DEFAULT_CODEX_BIN = CODEX_LAUNCHER_PATHS[0]
DEFAULT_CLAUDE_BIN = Path.home() / ".local/bin/claude"
DEFAULT_OPENCODE_BIN = Path("/opt/homebrew/bin/opencode")
DEFAULT_GEMINI_BIN = Path("/opt/homebrew/bin/gemini")
CODEX_COMPATIBILITY_POLICY_VERSION = "vibapp.codex-cli-compatibility.experimental-v1"
CODEX_PROTOCOL_VERSION = "vibapp.codex-exec-protocol.experimental-v1"
CODEX_VERSION_PATTERN = re.compile(r"^codex-cli (0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
LOCAL_CODEX_COMPATIBILITY_POLICY = {
    "policy_version": CODEX_COMPATIBILITY_POLICY_VERSION,
    "configured_paths": tuple(str(path) for path in CODEX_LAUNCHER_PATHS),
    "resolved_roots": tuple(str(path) for path in CODEX_CASK_ROOTS),
    # This range is audit metadata, not a launch gate.  The signed publisher,
    # exact executable digest and live protocol probe are the compatibility
    # evidence; a newer CLI is reported as unexercised instead of being rejected
    # solely because its version number moved.
    "minimum_version": (0, 149, 0),
    "maximum_version_exclusive": (0, 151, 0),
    "signing_identifier": "codex",
    "signing_team_identifier": "2DC432GLL2",
    "signing_requirement": (
        '=identifier "codex" and anchor apple generic '
        'and certificate leaf[subject.OU] = "2DC432GLL2"'
    ),
    # Known digests improve audit readability, but publisher signature and the
    # live protocol probe are the trust gates.
    "reviewed_digests": {
        "codex-cli 0.149.1": "f0d8762236594359b60cfbe17f4c7e945a3ce8d1c91e74778838c968d250fb6c",
        "codex-cli 0.150.1": "a14f9a907c12c8812878b70e6b7d65f81c39ed795513e46a55817d7428c0ca6b",
    },
}
LOCAL_CLAUDE_PIN = {
    "configured_path": str(DEFAULT_CLAUDE_BIN),
    "resolved_path": str(Path.home() / ".local/share/claude/versions/2.1.245"),
    "version": "Claude Code 2.1.245",
    "sha256": "9f7c2260251765a18d0b35198669dacc1912f6e8129a3b01f6b58d93365ff1f1",
}
LOCAL_OPENCODE_PIN = {
    "configured_path": str(DEFAULT_OPENCODE_BIN),
    "resolved_path": "/opt/homebrew/Cellar/opencode/1.18.23/bin/opencode",
    "version": "opencode 1.18.23",
    "sha256": "f7c45939a895e5a9febf141ab16307418bc41da31879aa0b2e65223190ca1c1a",
}
LOCAL_GEMINI_PIN = {
    "configured_path": str(DEFAULT_GEMINI_BIN),
    "resolved_path": "/opt/homebrew/Cellar/gemini-cli/0.46.0/libexec/lib/node_modules/@google/gemini-cli/bundle/gemini.js",
    "version": "gemini-cli 0.46.0",
    "sha256": "579d124ba01e1d8ea83d33ba835acc503b1e9c4af123dee868d7accf91f7234c",
    "bundle_sha256": "8e45b92741323af2842fe671204fb77e2af172a4b7f5126d433e47c529a4b809",
}
MAX_CREDENTIAL_BYTES = 1024 * 1024
MAX_API_KEY_CHARACTERS = 64 * 1024
MAX_PROVIDER_BUNDLE_FILES = 512
MAX_PROVIDER_BUNDLE_BYTES = 128 * 1024 * 1024
ADAPTER_SEATBELT = "adapter-seatbelt"
CODEX_NATIVE_WORKSPACE_SANDBOX = "codex-native-workspace-write"
LIVE_CONTAINMENT_ERROR = "local-live-containment-unavailable"
OPENCODE_SCOPED_CREDENTIAL_ERROR = "opencode-scoped-credential-unavailable"
OPENCODE_SECRET_KEY = re.compile(
    r'"[^"\r\n]*(?:api[-_]?key|token|secret|authorization|credential|password|headers)[^"\r\n]*"\s*:',
    re.IGNORECASE,
)
SIMPLE_JSON_OBJECT_KEY = re.compile(r'"([^"\r\n]+)"\s*:')


class DuplicateJsonKey(ValueError):
    pass


class AdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider_process_started: bool = False,
        external_request_attempted: bool = False,
        external_request_observed: bool = False,
        failure_diagnostic: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.provider_process_started = provider_process_started
        self.external_request_attempted = external_request_attempted
        self.external_request_observed = external_request_observed
        self.failure_diagnostic = None
        if failure_diagnostic is not None:
            from docker_executor import DockerError, validate_failure_diagnostic
            try:
                self.failure_diagnostic = validate_failure_diagnostic(failure_diagnostic)
            except DockerError:
                pass  # Optional malformed diagnostics never mask the original failure.


def reject_duplicate_json_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def strict_json_bytes(payload: bytes, context: str) -> Any:
    """Decode strict JSON while rejecting duplicate keys and non-finite numbers."""

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite number {value}")

    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_json_key,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey, ValueError) as error:
        raise AdapterError(
            "opencode-config-invalid",
            f"{context} must be strict UTF-8 JSON with unique object keys: {error}",
        ) from error


def require_live_containment_backend() -> None:
    """Fail closed until a backend can prove whole-descendant quiescence.

    A process group is not a containment boundary: a provider can fork, call
    ``setsid(2)``, close its inherited streams and outlive the observed PGID.  The
    product adapter therefore has no live local execution backend at present.
    Synthetic providers used by the offline contract tests do not call this path.
    """
    raise AdapterError(
        LIVE_CONTAINMENT_ERROR,
        "live local CodeAgent execution is paused: the current macOS PGID/Seatbelt "
        "supervisor cannot prove that forked or setsid descendants are terminated "
        "and quiescent; use a future container/harness containment backend",
    )


def require_model(value: Any, context: str = "CodeAgent model") -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value.strip() != value
        or value.startswith("-")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise AdapterError("model-invalid", f"{context} must be an explicit bounded non-blank selection")
    return value


@dataclass(frozen=True)
class ProviderExecution:
    provider_id: str
    provider_process_started: bool
    external_request_attempted: bool
    external_request_observed: bool
    gateway_request_id: str | None
    executable_path: str
    executable_version: str
    executable_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    container_receipt: dict[str, Any] | None = None


class CodeAgentProvider(Protocol):
    provider_id: str
    task_provider: str
    model: str

    def preflight(self) -> dict[str, Any]: ...

    def execute(
        self,
        *,
        workspace: Path,
        prompt: bytes,
        limits: dict[str, int],
        temporary_root: Path,
        cancel_file: Path | None,
    ) -> ProviderExecution: ...


def effective_local_limits(requested: dict[str, int]) -> dict[str, int]:
    """Apply the product adapter's stricter local single-job ceilings."""
    limits = dict(requested)
    limits["memory_bytes"] = min(limits["memory_bytes"], MAX_LOCAL_MEMORY_BYTES)
    limits["pids"] = min(limits["pids"], MAX_LOCAL_PIDS)
    limits["wall_time_seconds"] = min(limits["wall_time_seconds"], MAX_LOCAL_WALL_TIME_SECONDS)
    limits["cpu_seconds"] = min(limits["cpu_seconds"], limits["wall_time_seconds"])
    return limits


def cancellation_requested(cancel_file: Path | None) -> bool:
    if cancel_file is None:
        return False
    try:
        metadata = cancel_file.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size != 0
        or metadata.st_mode & 0o022
    ):
        raise AdapterError("cancel-signal-invalid", "cancellation marker is not a private empty regular file")
    return True


@contextmanager
def one_job_lease(output_root: Path, *, budget_kind: str = "codeagent", parallel_authoring: bool = False):
    """Shared host-user admission; legacy callers retain their serial lane.

    Docker opts into two authors. Legacy callers hold one additional singleton
    before entering the same shared budget, so mixed callers never exceed two.
    """
    del output_root  # Kept for caller compatibility; never defines the budget.
    if budget_kind not in {"codeagent", "pipeline"} or type(parallel_authoring) is not bool:
        raise AdapterError("execution-lock-invalid", "unknown host execution budget")
    try:
        with ExitStack() as stack:
            if not parallel_authoring:
                stack.enter_context(host_budget.lease(budget_kind + "-legacy"))
            stack.enter_context(host_budget.lease(budget_kind))
            yield
    except host_budget.BudgetError as error:
        raise AdapterError(error.code, str(error)) from error


@contextmanager
def _attempt_lease(output_root: Path):
    """Keep direct adapter callers from authoring twice into one output root."""
    root_descriptor = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(".execution.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=root_descriptor)
    finally:
        os.close(root_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_mode & 0o077:
            raise AdapterError("execution-lock-invalid", "local CodeAgent execution lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AdapterError("job-already-running", "another CodeAgent worker owns this output root") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def load_cloud_agent(path: Path) -> Any:
    configured = path
    path = path.resolve(strict=False)
    if configured.is_symlink() or not path.is_file() or path.is_symlink():
        raise AdapterError("cloud-agent-unavailable", "reviewed cloud-agent module is unavailable")
    specification = importlib.util.spec_from_file_location("vibapp_cloud_agent", path)
    if specification is None or specification.loader is None:
        raise AdapterError("cloud-agent-unavailable", "cannot load reviewed cloud-agent module")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def atomic_status(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_STATUS_BYTES:
        raise AdapterError("status-limit", "adapter status exceeds 128 KiB")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_size > MAX_STATUS_BYTES:
        raise AdapterError("status-invalid", "adapter status is not a bounded regular file")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("status-invalid", "adapter status is not valid JSON") from error
    if not isinstance(value, dict) or value.get("schema_version") != STATUS_VERSION:
        raise AdapterError("status-invalid", "adapter status version is unsupported")
    return value


def process_active(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def child_limits(limits: dict[str, int]) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    # Do not apply RLIMIT_FSIZE to the whole Codex process: it also covers the
    # already-existing local session log outside the job workspace. Workspace
    # output is bounded independently and continuously below.
    cpu = max(1, min(limits["cpu_seconds"], 900))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, min(cpu + 1, 901)))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (limits["memory_bytes"], limits["memory_bytes"]))


def kill_group(process: subprocess.Popen[bytes]) -> None:
    for chosen_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, chosen_signal)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue
    raise AdapterError(
        "provider-cleanup-failed",
        "local CodeAgent process group did not terminate",
        provider_process_started=True,
        external_request_attempted=True,
    )


def process_group_usage(process_group: int) -> tuple[int, int]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,rss=,state="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError("process-observation-failed", "cannot observe local CodeAgent process group") from error
    if result.returncode != 0 or len(result.stdout) > 2 * 1024 * 1024:
        raise AdapterError("process-observation-failed", "local CodeAgent process observation failed closed")
    count = 0
    rss = 0
    for line in result.stdout.splitlines():
        try:
            pgid_text, rss_text, state = line.decode("ascii").split(None, 2)
            if int(pgid_text) == process_group and not state.startswith("Z"):
                count += 1
                rss += int(rss_text) * 1024
        except (UnicodeDecodeError, ValueError) as error:
            raise AdapterError("process-observation-failed", "local process observation was malformed") from error
    return count, rss


def private_credential(path: Path, code: str, label: str) -> Path:
    """Validate credential metadata without opening or parsing its content."""
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError) as error:
        raise AdapterError(code, f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= MAX_CREDENTIAL_BYTES
        or metadata.st_mode & 0o022
    ):
        raise AdapterError(code, f"{label} has unsafe metadata")
    return path


def opaque_environment_secret(name: str, code: str, label: str) -> str:
    """Return an environment credential after presence/size checks only."""
    value = os.environ.get(name)
    if value is None or not value or len(value) > MAX_API_KEY_CHARACTERS:
        raise AdapterError(code, f"{label} environment credential is unavailable or unbounded")
    return value


def private_auth_source(directory: Path, filename: str, code: str, label: str) -> Path:
    try:
        resolved = directory.resolve(strict=True)
        metadata = resolved.lstat()
    except (FileNotFoundError, OSError) as error:
        raise AdapterError(code, f"{label} directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise AdapterError(code, f"{label} directory is unsafe")
    return private_credential(resolved / filename, code, label)


def clone_private_file(source: Path, destination: Path, label: str) -> None:
    """Use Darwin clonefile so Python never opens credential contents."""
    if sys.platform != "darwin":
        raise AdapterError(
            "credential-broker-unavailable",
            f"ephemeral {label} cloning is currently available only on macOS",
        )
    libc = ctypes.CDLL(None, use_errno=True)
    clonefile = getattr(libc, "clonefile", None)
    if clonefile is None:
        raise AdapterError("credential-broker-unavailable", "macOS clonefile is unavailable")
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise AdapterError("credential-broker-unavailable", f"{label} changed before clone")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if clonefile(os.fsencode(source), os.fsencode(destination), 0) != 0:
        error_number = ctypes.get_errno()
        raise AdapterError(
            "credential-broker-unavailable",
            f"macOS {label} clone failed with errno {error_number}",
        )
    after = source.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_uid, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_uid,
        after.st_mode,
    ):
        destination.unlink(missing_ok=True)
        raise AdapterError("credential-broker-unavailable", f"{label} changed during clone")
    private_credential(destination, "credential-broker-unavailable", f"cloned {label}")
    destination.chmod(0o600)


def probe_clone_facility() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="vibapp-codeagent-clone-preflight-"))
    try:
        synthetic = temporary / "synthetic-auth.json"
        descriptor = os.open(synthetic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(b"{}")
        clone_private_file(synthetic, temporary / "scoped-auth.json", "synthetic credential")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def discover_codex_binary() -> Path:
    """Choose from reviewed launcher locations without consulting ambient PATH."""
    for candidate in CODEX_LAUNCHER_PATHS:
        try:
            candidate.lstat()
        except OSError:
            continue
        return candidate
    return DEFAULT_CODEX_BIN


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_verified_executable_snapshot(
    source: Path,
    identity: dict[str, Any],
    temporary_root: Path,
    provider_label: str,
) -> Path:
    """Copy the already-verified executable inode into the private job root.

    Re-checking a pathname before consent is insufficient because that pathname can
    be replaced before ``Popen`` resolves it.  The command therefore executes this
    job-private byte snapshot.  The copy is streamed from an ``O_NOFOLLOW`` file
    descriptor, and its digest must equal the identity that was bound at preflight.
    """
    expected_digest = identity.get("sha256")
    resolved_path = identity.get("resolved_path")
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or not isinstance(resolved_path, str)
        or Path(resolved_path) != source
    ):
        raise AdapterError(
            "provider-execution-identity-mismatch",
            f"{provider_label} execution identity cannot bind an executable snapshot",
        )
    try:
        root_metadata = temporary_root.lstat()
    except OSError as error:
        raise AdapterError(
            "provider-executable-snapshot-failed",
            f"cannot inspect the private {provider_label} job root",
        ) from error
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
        or stat.S_IMODE(root_metadata.st_mode) & 0o077
    ):
        raise AdapterError(
            "provider-executable-snapshot-failed",
            f"private {provider_label} job root is unsafe",
        )

    snapshot_root = temporary_root / "provider-executable-snapshot"
    destination = snapshot_root / "executable"
    try:
        snapshot_root.mkdir(mode=0o700)
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, source_flags)
    except OSError as error:
        raise AdapterError(
            "provider-executable-snapshot-failed",
            f"cannot open the verified {provider_label} executable",
        ) from error
    destination_fd: int | None = None
    digest = hashlib.sha256()
    try:
        source_metadata_before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_metadata_before.st_mode)
            or source_metadata_before.st_uid not in {0, os.getuid()}
            or source_metadata_before.st_nlink != 1
            or source_metadata_before.st_mode & 0o022
        ):
            raise AdapterError(
                "provider-execution-identity-mismatch",
                f"verified {provider_label} executable metadata changed before snapshot",
            )
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o500,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_fd, remaining)
                if written <= 0:
                    raise OSError("short executable snapshot write")
                remaining = remaining[written:]
        os.fsync(destination_fd)
        source_metadata_after = os.fstat(source_fd)
        source_fingerprint_before = (
            source_metadata_before.st_dev,
            source_metadata_before.st_ino,
            source_metadata_before.st_size,
            source_metadata_before.st_mtime_ns,
            source_metadata_before.st_ctime_ns,
        )
        source_fingerprint_after = (
            source_metadata_after.st_dev,
            source_metadata_after.st_ino,
            source_metadata_after.st_size,
            source_metadata_after.st_mtime_ns,
            source_metadata_after.st_ctime_ns,
        )
        if source_fingerprint_before != source_fingerprint_after or digest.hexdigest() != expected_digest:
            raise AdapterError(
                "provider-execution-identity-mismatch",
                f"verified {provider_label} executable changed before snapshot completion",
            )
    except AdapterError:
        raise
    except OSError as error:
        raise AdapterError(
            "provider-executable-snapshot-failed",
            f"cannot create the verified {provider_label} executable snapshot",
        ) from error
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)

    try:
        destination.chmod(0o500)
        destination_metadata = destination.lstat()
    except OSError as error:
        raise AdapterError(
            "provider-executable-snapshot-failed",
            f"cannot seal the verified {provider_label} executable snapshot",
        ) from error
    if (
        not stat.S_ISREG(destination_metadata.st_mode)
        or stat.S_ISLNK(destination_metadata.st_mode)
        or destination_metadata.st_uid != os.getuid()
        or destination_metadata.st_nlink != 1
        or stat.S_IMODE(destination_metadata.st_mode) != 0o500
    ):
        raise AdapterError(
            "provider-executable-snapshot-failed",
            f"verified {provider_label} executable snapshot is unsafe",
        )
    return destination


def adapter_identity() -> dict[str, str]:
    """Return the exact adapter script identity without consulting an alias."""
    source = Path(__file__).resolve(strict=True)
    metadata = source.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
    ):
        raise AdapterError("adapter-identity-invalid", "CodeAgent adapter script identity is unsafe")
    return {
        "adapter_id": "local-codeagent-adapter",
        "adapter_version": ADAPTER_VERSION,
        "adapter_sha256": _sha256_file(source),
    }


def _probe_child_limits(maximum_output_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CPU, (2, 3))
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (maximum_output_bytes, maximum_output_bytes),
    )


def bounded_metadata_probe(
    command: list[str],
    *,
    environment: dict[str, str],
    timeout_seconds: int = 5,
    maximum_output_bytes: int = 64 * 1024,
) -> tuple[int, bytes, bytes]:
    """Run a no-request identity/protocol probe with bounded time and output."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=environment["HOME"],
                env=environment,
                close_fds=True,
                # Stay in the adapter's authority-owned process group so the
                # host can terminate the whole preflight tree on cancellation.
                # A metadata probe must never create an escaping session.
                start_new_session=False,
                preexec_fn=lambda: _probe_child_limits(maximum_output_bytes),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise AdapterError("provider-preflight-failed", "cannot start bounded Codex metadata probe") from error
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            for chosen_signal in (signal.SIGTERM, signal.SIGKILL):
                try:
                    process.send_signal(chosen_signal)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise AdapterError("provider-preflight-failed", "Codex metadata probe timed out") from error
        stdout_size = stdout.tell()
        stderr_size = stderr.tell()
        if stdout_size > maximum_output_bytes or stderr_size > maximum_output_bytes:
            raise AdapterError("provider-preflight-failed", "Codex metadata probe exceeded its output limit")
        stdout.seek(0)
        stderr.seek(0)
        return return_code, stdout.read(), stderr.read()


def parse_codex_version(value: bytes) -> tuple[str, tuple[int, int, int]]:
    try:
        version = value.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise AdapterError("provider-version-unsupported", "Codex version output is not UTF-8") from error
    match = CODEX_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise AdapterError("provider-version-unsupported", "Codex did not report a supported semantic CLI version")
    return version, tuple(int(item) for item in match.groups())


def validate_codex_protocol(help_output: bytes) -> str:
    try:
        help_text = help_output.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AdapterError("provider-protocol-incompatible", "Codex exec help is not UTF-8") from error
    required_fragments = (
        "Run Codex non-interactively",
        "--config <key=value>",
        "--model <MODEL>",
        "--sandbox <SANDBOX_MODE>",
        "workspace-write",
        "--cd <DIR>",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-last-message <FILE>",
    )
    missing = [fragment for fragment in required_fragments if fragment not in help_text]
    if missing:
        raise AdapterError(
            "provider-protocol-incompatible",
            f"Codex exec protocol is missing required capability: {missing[0]}",
        )
    return hashlib.sha256(help_output).hexdigest()


def _resolved_codex_version_segment(resolved: Path, policy: dict[str, Any]) -> str:
    for root_value in policy.get("resolved_roots", ()):
        root = Path(root_value).resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) == 3 and relative.parts[1:] == ("bin", "codex"):
            return relative.parts[0]
    raise AdapterError(
        "executable-identity-invalid",
        "resolved Codex executable is outside the reviewed Homebrew Cask layout",
    )


def resolve_codex_observed_identity(
    configured_path: Path,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe Codex file/signer identity without executing the provider binary."""
    policy = policy or LOCAL_CODEX_COMPATIBILITY_POLICY
    required_policy_fields = {
        "policy_version",
        "configured_paths",
        "resolved_roots",
        "minimum_version",
        "maximum_version_exclusive",
        "signing_identifier",
        "signing_team_identifier",
        "signing_requirement",
        "reviewed_digests",
    }
    if not required_policy_fields.issubset(policy):
        raise AdapterError("executable-identity-invalid", "Codex compatibility policy is incomplete")
    configured_text = str(configured_path)
    current = configured_path
    chain: list[str] = []
    for _ in range(16):
        try:
            metadata = current.lstat()
        except (FileNotFoundError, OSError) as error:
            raise AdapterError("executable-identity-invalid", "configured Codex executable is missing") from error
        if metadata.st_uid not in {0, os.getuid()}:
            raise AdapterError("executable-identity-invalid", "Codex executable chain has an unsafe owner")
        chain.append(str(current))
        if stat.S_ISLNK(metadata.st_mode):
            target = Path(os.readlink(current))
            current = Path(os.path.normpath(str(target if target.is_absolute() else current.parent / target)))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AdapterError("executable-identity-invalid", "resolved Codex executable is not a regular file")
        break
    else:
        raise AdapterError("executable-identity-invalid", "Codex executable symlink chain exceeds 16 links")
    resolved = current.resolve(strict=True)
    path_version = _resolved_codex_version_segment(resolved, policy)
    version_parts = path_version.split(".")
    if len(version_parts) != 3 or any(not item.isdigit() for item in version_parts):
        raise AdapterError("provider-version-unsupported", "Codex Cask version is not semantic")
    parsed_version = tuple(int(item) for item in version_parts)
    version_within_exercised_range = (
        tuple(policy["minimum_version"])
        <= parsed_version
        < tuple(policy["maximum_version_exclusive"])
    )
    configured_absolute = Path(os.path.abspath(configured_path))
    if configured_text not in policy["configured_paths"] and configured_absolute != resolved:
        raise AdapterError("executable-identity-invalid", "configured Codex path is not a reviewed launcher or Cask binary")
    metadata = resolved.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise AdapterError("executable-identity-invalid", "resolved Codex executable owner, mode or type is unsafe")
    before = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
    digest_before = _sha256_file(resolved)
    if sys.platform != "darwin":
        raise AdapterError("executable-identity-invalid", "Codex publisher verification currently requires macOS")
    codesign = Path("/usr/bin/codesign")
    if not codesign.is_file() or codesign.is_symlink():
        raise AdapterError("executable-identity-invalid", "macOS code-signing verifier is unavailable")
    with tempfile.TemporaryDirectory(prefix="vibapp-codex-static-preflight-") as temporary:
        home = Path(temporary)
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "HOME": str(home),
            "TMPDIR": str(home),
        }
        signature_status, _, _ = bounded_metadata_probe(
            [
                str(codesign),
                "--verify",
                "--strict",
                "--verbose=2",
                "-R",
                policy["signing_requirement"],
                str(resolved),
            ],
            environment=environment,
        )
        if signature_status != 0:
            raise AdapterError(
                "executable-identity-invalid",
                "Codex executable is not signed by the trusted OpenAI publisher identity",
            )
    after_metadata = resolved.lstat()
    after = (
        after_metadata.st_dev,
        after_metadata.st_ino,
        after_metadata.st_size,
        after_metadata.st_mtime_ns,
        after_metadata.st_ctime_ns,
    )
    digest_after = _sha256_file(resolved)
    if before != after or digest_before != digest_after:
        raise AdapterError("executable-identity-invalid", "Codex executable changed during static preflight")
    version = f"codex-cli {path_version}"
    return {
        "configured_path": configured_text,
        "resolved_path": str(resolved),
        "symlink_chain": chain,
        "version": version,
        "sha256": digest_after,
        "owner_uid": after_metadata.st_uid,
        "mode": stat.S_IMODE(after_metadata.st_mode),
        "identity_policy_version": policy["policy_version"],
        "identity_basis": "apple-developer-id-and-static-cask-identity",
        "signing_identifier": policy["signing_identifier"],
        "signing_team_identifier": policy["signing_team_identifier"],
        "signature_requirement_satisfied": True,
        "digest_reviewed": policy["reviewed_digests"].get(version) == digest_after,
        "version_within_exercised_range": version_within_exercised_range,
        "supported_version_minimum": ".".join(str(item) for item in policy["minimum_version"]),
        "supported_version_maximum_exclusive": ".".join(
            str(item) for item in policy["maximum_version_exclusive"]
        ),
        "protocol_version": CODEX_PROTOCOL_VERSION,
        "protocol_sha256": None,
        "protocol_preflight_passed": False,
        "protocol_observation": "provider-not-executed-without-containment",
    }


def resolve_codex_compatible_identity(
    configured_path: Path,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trust the OpenAI signer and exercised CLI protocol, not one patch hash."""
    require_live_containment_backend()
    policy = policy or LOCAL_CODEX_COMPATIBILITY_POLICY
    required_policy_fields = {
        "policy_version",
        "configured_paths",
        "resolved_roots",
        "minimum_version",
        "maximum_version_exclusive",
        "signing_identifier",
        "signing_team_identifier",
        "signing_requirement",
        "reviewed_digests",
    }
    if not required_policy_fields.issubset(policy):
        raise AdapterError("executable-identity-invalid", "Codex compatibility policy is incomplete")
    configured_text = str(configured_path)
    allowed_launcher = configured_text in policy["configured_paths"]
    current = configured_path
    chain: list[str] = []
    for _ in range(16):
        try:
            metadata = current.lstat()
        except (FileNotFoundError, OSError) as error:
            raise AdapterError("executable-identity-invalid", "configured Codex executable is missing") from error
        if metadata.st_uid not in {0, os.getuid()}:
            raise AdapterError("executable-identity-invalid", "Codex executable chain has an unsafe owner")
        chain.append(str(current))
        if stat.S_ISLNK(metadata.st_mode):
            target = Path(os.readlink(current))
            current = Path(os.path.normpath(str(target if target.is_absolute() else current.parent / target)))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AdapterError("executable-identity-invalid", "resolved Codex executable is not a regular file")
        break
    else:
        raise AdapterError("executable-identity-invalid", "Codex executable symlink chain exceeds 16 links")

    try:
        resolved = current.resolve(strict=True)
    except OSError as error:
        raise AdapterError("executable-identity-invalid", "resolved Codex executable is unavailable") from error
    path_version = _resolved_codex_version_segment(resolved, policy)
    configured_absolute = Path(os.path.abspath(configured_path))
    if not allowed_launcher and configured_absolute != resolved:
        raise AdapterError("executable-identity-invalid", "configured Codex path is not a reviewed launcher or Cask binary")
    metadata = resolved.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise AdapterError("executable-identity-invalid", "resolved Codex executable owner, mode or type is unsafe")
    before = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    digest_before = _sha256_file(resolved)

    if sys.platform != "darwin":
        raise AdapterError("executable-identity-invalid", "Codex publisher verification currently requires macOS")
    codesign = Path("/usr/bin/codesign")
    if not codesign.is_file() or codesign.is_symlink():
        raise AdapterError("executable-identity-invalid", "macOS code-signing verifier is unavailable")
    with tempfile.TemporaryDirectory(prefix="vibapp-codex-preflight-") as temporary:
        home = Path(temporary)
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "HOME": str(home),
            "TMPDIR": str(home),
            "CODEX_HOME": str(home / "codex-home"),
        }
        (home / "codex-home").mkdir(mode=0o700)
        signature_status, _, _ = bounded_metadata_probe(
            [
                str(codesign),
                "--verify",
                "--strict",
                "--verbose=2",
                "-R",
                policy["signing_requirement"],
                str(resolved),
            ],
            environment=environment,
        )
        if signature_status != 0:
            raise AdapterError(
                "executable-identity-invalid",
                "Codex executable is not signed by the trusted OpenAI publisher identity",
            )
        executable_snapshot = stage_verified_executable_snapshot(
            resolved,
            {"resolved_path": str(resolved), "sha256": digest_before},
            home,
            "Codex preflight",
        )
        version_status, version_output, _ = bounded_metadata_probe(
            [str(executable_snapshot), "--version"],
            environment=environment,
        )
        if version_status != 0:
            raise AdapterError("provider-preflight-failed", "Codex version probe failed")
        version, parsed_version = parse_codex_version(version_output)
        version_within_exercised_range = (
            tuple(policy["minimum_version"])
            <= parsed_version
            < tuple(policy["maximum_version_exclusive"])
        )
        if path_version != version.removeprefix("codex-cli "):
            raise AdapterError("executable-identity-invalid", "Codex Cask path and reported version disagree")
        protocol_status, protocol_output, _ = bounded_metadata_probe(
            [str(executable_snapshot), "exec", "--help"],
            environment=environment,
        )
        if protocol_status != 0:
            raise AdapterError("provider-preflight-failed", "Codex exec protocol probe failed")
        protocol_sha256 = validate_codex_protocol(protocol_output)

    after_metadata = resolved.lstat()
    after = (
        after_metadata.st_dev,
        after_metadata.st_ino,
        after_metadata.st_size,
        after_metadata.st_mtime_ns,
        after_metadata.st_ctime_ns,
    )
    digest_after = _sha256_file(resolved)
    if before != after or digest_before != digest_after:
        raise AdapterError("executable-identity-invalid", "Codex executable changed during preflight")
    reviewed_digest = policy["reviewed_digests"].get(version)
    return {
        "configured_path": configured_text,
        "resolved_path": str(resolved),
        "symlink_chain": chain,
        "version": version,
        "sha256": digest_after,
        "owner_uid": after_metadata.st_uid,
        "mode": stat.S_IMODE(after_metadata.st_mode),
        "identity_policy_version": policy["policy_version"],
        "identity_basis": "apple-developer-id-and-live-protocol",
        "signing_identifier": policy["signing_identifier"],
        "signing_team_identifier": policy["signing_team_identifier"],
        "signature_requirement_satisfied": True,
        "digest_reviewed": reviewed_digest == digest_after,
        "version_within_exercised_range": version_within_exercised_range,
        "supported_version_minimum": ".".join(str(item) for item in policy["minimum_version"]),
        "supported_version_maximum_exclusive": ".".join(
            str(item) for item in policy["maximum_version_exclusive"]
        ),
        "protocol_version": CODEX_PROTOCOL_VERSION,
        "protocol_sha256": protocol_sha256,
        "protocol_preflight_passed": True,
    }


def provider_tree_digest(root: Path) -> tuple[str, int, int]:
    """Bind a provider's supporting files without executing or importing them."""
    try:
        root_metadata = root.lstat()
    except (FileNotFoundError, OSError) as error:
        raise AdapterError("executable-identity-invalid", "provider bundle is unavailable") from error
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise AdapterError("executable-identity-invalid", "provider bundle is not a real directory")
    digest = hashlib.sha256(b"VIBAPP-PROVIDER-TREE\0")
    count = 0
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or metadata.st_mode & 0o022
        ):
            raise AdapterError("executable-identity-invalid", "provider bundle contains an unsafe entry")
        count += 1
        total += metadata.st_size
        if count > MAX_PROVIDER_BUNDLE_FILES or total > MAX_PROVIDER_BUNDLE_BYTES:
            raise AdapterError("executable-identity-invalid", "provider bundle exceeds its reviewed bounds")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        digest.update(file_digest.digest())
        digest.update(metadata.st_size.to_bytes(8, "big"))
    if count == 0:
        raise AdapterError("executable-identity-invalid", "provider bundle is empty")
    return digest.hexdigest(), count, total


def isolated_environment(temporary_root: Path) -> dict[str, str]:
    home = temporary_root / "home"
    temp = temporary_root / "tmp"
    cache = temporary_root / "cache"
    config = temporary_root / "config"
    data = temporary_root / "data"
    state = temporary_root / "state"
    for directory in (home, temp, cache, config, data, state):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "HOME": str(home),
        "TMPDIR": str(temp),
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
    }


def provider_preflight(
    *,
    provider_id: str,
    task_provider: str,
    model: str,
    identity: dict[str, Any],
    authentication: str,
    credential_delivery: str,
    endpoint_kind: str = "provider-managed",
    canonical_endpoint: str = "provider-managed",
    runtime_package_id: str | None = None,
    non_secret_config_sha256: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    adapter = adapter_identity()
    endpoint_body = {
        "kind": endpoint_kind,
        "canonical_endpoint": canonical_endpoint,
    }
    endpoint = {
        **endpoint_body,
        "endpoint_sha256": hashlib.sha256(
            json.dumps(endpoint_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    if non_secret_config_sha256 is None:
        public_config = {
            "provider_id": provider_id,
            "task_provider": task_provider,
            "model": require_model(model),
            "credential_delivery": credential_delivery,
            "provider_safety_flags": details.get("provider_safety_flags", []),
        }
        non_secret_config_sha256 = hashlib.sha256(
            json.dumps(public_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    runtime = {
        **adapter,
        "package_id": runtime_package_id or provider_id,
        "package_version": identity["version"],
        "executable_sha256": identity["sha256"],
    }
    execution_identity_body = {
        "schema_version": "vibapp.provider-execution-identity.experimental-v1",
        "endpoint": endpoint,
        "runtime": runtime,
        "non_secret_config_sha256": non_secret_config_sha256,
    }
    execution_identity = {
        **execution_identity_body,
        "identity_sha256": hashlib.sha256(
            json.dumps(
                execution_identity_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    result = {
        "schema_version": ADAPTER_VERSION,
        **adapter,
        "provider_id": provider_id,
        "task_provider": task_provider,
        "model": model,
        "provider_execution_identity": execution_identity,
        "identity_observed": True,
        "available": False,
        "execution_available": False,
        "execution_blocker": {
            "code": LIVE_CONTAINMENT_ERROR,
            "message": (
                "live local CodeAgent execution is paused because the current macOS PGID/Seatbelt "
                "supervisor cannot prove whole-descendant termination and quiescence"
            ),
        },
        "source_authoring_only": True,
        "configured_executable_path": identity["configured_path"],
        "executable_path": identity["resolved_path"],
        "executable_version": identity["version"],
        "executable_sha256": identity["sha256"],
        "authentication": authentication,
        "credential_delivery": credential_delivery,
        "process_mode": "non-interactive",
        "provider_process_started": False,
        "external_request_attempted": False,
        "external_request_observed": False,
        "consent_consumed": False,
        "macos_sandbox": "insufficient-without-whole-descendant-containment",
        "containment_backend": "required-not-available",
        "loads_user_config": False,
        "loads_user_rules": False,
        "loads_user_plugins": False,
        "loads_user_mcp": False,
        "shell_environment_inheritance": "none",
        "persists_provider_session": False,
        "maximum_local_rss_bytes": MAX_LOCAL_MEMORY_BYTES,
        "maximum_local_pids": MAX_LOCAL_PIDS,
        "maximum_local_wall_time_seconds": MAX_LOCAL_WALL_TIME_SECONDS,
        "one_job_per_output_root": True,
        "compile_authority": False,
        "verify_authority": False,
        "install_authority": False,
        "publish_authority": False,
    }
    for key in (
        "identity_policy_version",
        "identity_basis",
        "signing_identifier",
        "signing_team_identifier",
        "signature_requirement_satisfied",
        "digest_reviewed",
        "supported_version_minimum",
        "supported_version_maximum_exclusive",
        "version_within_exercised_range",
        "protocol_version",
        "protocol_sha256",
        "protocol_preflight_passed",
    ):
        if key in identity:
            result[key] = identity[key]
    result.update(details)
    return result


def write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(payload)


def discard_untrusted_provider_control(path: Path) -> None:
    """Remove only the provider-owned control-path entry, never follow it."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        raise AdapterError(
            "source-invalid",
            "provider-last-message.json is a directory; refusing recursive cleanup of untrusted content",
        )
    try:
        path.unlink()
    except OSError as error:
        raise AdapterError("source-invalid", "cannot replace untrusted provider control output") from error


def deterministic_source_inventory(cloud_agent: Any, source_root: Path, task: dict[str, Any]) -> list[str]:
    """Inventory the real source tree without trusting provider-authored metadata."""
    try:
        root_metadata = source_root.lstat()
    except (FileNotFoundError, OSError) as error:
        raise AdapterError("source-invalid", "provider did not leave a source directory") from error
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise AdapterError("source-invalid", "source must be a real directory")
    paths: list[str] = []
    for root, directories, files in os.walk(source_root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        root_path = Path(root)
        for directory in directories:
            candidate = root_path / directory
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AdapterError("source-invalid", "source inventory encountered an unsafe directory")
        for filename in files:
            candidate = root_path / filename
            metadata = candidate.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise AdapterError("source-invalid", "source inventory encountered a linked or non-regular file")
            relative = candidate.relative_to(source_root).as_posix()
            try:
                normalized, resolved = cloud_agent.normalized_source_path(
                    relative,
                    source_root,
                    f"adapter source inventory {relative}",
                )
            except cloud_agent.WorkerError as error:
                raise AdapterError(error.code, str(error)) from error
            if normalized != relative or resolved != candidate.resolve(strict=True):
                raise AdapterError("source-invalid", "source inventory path is not canonical")
            paths.append(relative)
            if len(paths) > task["limits"]["source_files"]:
                raise AdapterError("source-output-limit", "source inventory exceeds the task file limit")
    return paths


def derive_provider_control_record(
    cloud_agent: Any,
    workspace: Path,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Create the provider-result control record from authority-owned facts.

    Provider prose and provider-last-message.json are untrusted and ignored.  This
    projection intentionally claims only that a source tree exists; the independent
    workspace audit and later Builder/Verifier still decide source validity.
    """
    control_path = workspace / "provider-last-message.json"
    discard_untrusted_provider_control(control_path)
    source_files = deterministic_source_inventory(cloud_agent, workspace / "source", task)
    result = {
        "schema_version": "vibapp.cloud-codeagent-provider-result.experimental-v1",
        "document_type": "cloud-codeagent-provider-result",
        "job_id": task["job_id"],
        "status": "source-generated",
        "summary": (
            "Adapter-derived inventory from the immutable task and observed source tree; "
            "source remains untrusted until separate audit, build, and verification."
        ),
        "wit_world": task["target"]["wit_world"],
        "app_kind": task["target"]["app_kind"],
        "declared_capabilities": list(task["target"]["required_imports"]),
        "source_files": source_files,
        "unresolved": [],
    }
    write_private_json(control_path, result)
    return result


def _sbpl_string(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def require_macos_sandbox() -> Path:
    if sys.platform != "darwin":
        raise AdapterError("sandbox-unavailable", "local multi-provider execution requires macOS sandbox-exec")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file() or sandbox.is_symlink():
        raise AdapterError("sandbox-unavailable", "macOS sandbox-exec is unavailable")
    return sandbox


def sandboxed_command(command: list[str], workspace: Path, temporary_root: Path) -> list[str]:
    """Wrap a provider in Seatbelt with explicit read and write authority."""
    sandbox = require_macos_sandbox()
    readable = {
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/dev"),
        Path("/private/etc"),
        Path("/private/var/db"),
        Path("/opt/homebrew"),
        Path("/Library/Apple"),
        Path("/Library/Frameworks"),
        Path("/Applications/Xcode.app/Contents/Developer"),
        workspace,
        temporary_root,
    }
    writable = {workspace, temporary_root}
    for directory in (workspace, temporary_root):
        try:
            resolved = directory.resolve(strict=True)
        except OSError:
            continue
        readable.add(resolved)
        writable.add(resolved)
    executable = Path(command[0])
    if executable.is_absolute():
        readable.add(executable.parent)
        try:
            readable.add(executable.resolve(strict=True).parent)
        except OSError:
            pass
    readable_files: set[Path] = set()
    for argument in command[1:]:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.is_file() and not candidate.is_symlink():
            readable_files.add(candidate)
            readable.add(candidate.parent)
            try:
                resolved_candidate = candidate.resolve(strict=True)
                readable_files.add(resolved_candidate)
                readable.add(resolved_candidate.parent)
            except OSError:
                pass
    read_rules = " ".join(f'(subpath "{_sbpl_string(path)}")' for path in sorted(readable, key=str))
    file_rules = " ".join(f'(literal "{_sbpl_string(path)}")' for path in sorted(readable_files, key=str))
    write_rules = " ".join(f'(subpath "{_sbpl_string(path)}")' for path in sorted(writable, key=str))
    profile = "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm)",
            "(allow file-read-metadata)",
            '(allow file-read* (literal "/"))',
            f"(allow file-read* {read_rules} {file_rules})",
            f'(allow file-write* {write_rules} (literal "/dev/null"))',
            "(allow network-outbound)",
            "(deny network-inbound)",
            "",
        ]
    )
    profile_path = temporary_root / "provider.sb"
    descriptor = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
        handle.write(profile)
    return [str(sandbox), "-f", str(profile_path), *command]


def provider_process_command(
    command: list[str],
    workspace: Path,
    temporary_root: Path,
    *,
    provider_id: str,
    sandbox_owner: str,
) -> list[str]:
    """Select exactly one sandbox owner; nested macOS Seatbelt is unsupported."""
    if sandbox_owner == ADAPTER_SEATBELT:
        return sandboxed_command(command, workspace, temporary_root)
    if sandbox_owner != CODEX_NATIVE_WORKSPACE_SANDBOX:
        raise AdapterError("sandbox-policy-invalid", "local provider sandbox owner is unsupported")
    if provider_id != CODEX_PROVIDER_ID:
        raise AdapterError(
            "sandbox-policy-invalid",
            "only the reviewed Codex adapter may own its workspace sandbox",
        )
    prohibited = {
        "--add-dir",
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto",
    }
    if any(argument in prohibited for argument in command):
        raise AdapterError(
            "sandbox-policy-invalid",
            "Codex native sandbox command contains an authority-expanding option",
        )

    def exact_option(option: str, value: str) -> bool:
        positions = [index for index, argument in enumerate(command) if argument == option]
        return len(positions) == 1 and positions[0] + 1 < len(command) and command[positions[0] + 1] == value

    required_flags = {
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--json",
    }
    expected_output = workspace / "provider-last-message.json"
    if (
        not required_flags.issubset(command)
        or not exact_option("-s", "workspace-write")
        or not exact_option("-C", str(workspace))
        or not exact_option("-o", str(expected_output))
        or command[-1:] != ["-"]
    ):
        raise AdapterError(
            "sandbox-policy-invalid",
            "Codex native workspace sandbox is not bound to the exact job workspace",
        )
    return command


def supervise_provider(
    *,
    cloud_agent: Any,
    provider_id: str,
    provider_label: str,
    identity: dict[str, Any],
    command: list[str],
    environment: dict[str, str],
    workspace: Path,
    prompt: bytes,
    limits: dict[str, int],
    temporary_root: Path,
    cancel_file: Path | None,
    sandbox_owner: str = ADAPTER_SEATBELT,
) -> ProviderExecution:
    if cancellation_requested(cancel_file):
        raise AdapterError("provider-cancelled", "local CodeAgent job was cancelled before provider start")
    # Do not fall back to the legacy PGID monitor.  A provider can escape that
    # observation set with fork+setsid+closed stdio and mutate the workspace after
    # this function returns.  The code below remains as the implementation seam
    # for a future kernel/container/harness backend, but is unreachable until that
    # backend supplies a quiescence proof.
    require_live_containment_backend()
    command = provider_process_command(
        command,
        workspace,
        temporary_root,
        provider_id=provider_id,
        sandbox_owner=sandbox_owner,
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workspace,
            env=environment,
            close_fds=True,
            start_new_session=True,
            preexec_fn=lambda: child_limits(limits),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AdapterError("provider-start-failed", f"cannot start local {provider_label}: {error}") from error
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except (BrokenPipeError, OSError) as error:
        if process.poll() is None:
            kill_group(process)
        raise AdapterError(
            "provider-input-failed",
            f"local {provider_label} closed its task input early",
            provider_process_started=True,
            external_request_attempted=True,
        ) from error
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout: ("stdout", limits["stdout_bytes"]),
        process.stderr: ("stderr", limits["stderr_bytes"]),
    }
    counts = {"stdout": 0, "stderr": 0}
    for stream, details in streams.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, details)
    deadline = time.monotonic() + limits["wall_time_seconds"]
    return_code: int | None = None
    try:
        while selector.get_map():
            if cancellation_requested(cancel_file):
                kill_group(process)
                raise AdapterError(
                    "provider-cancelled",
                    "local CodeAgent job was cancelled",
                    provider_process_started=True,
                    external_request_attempted=True,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_group(process)
                raise AdapterError(
                    "provider-timeout",
                    f"local {provider_label} exceeded the task wall-time limit",
                    provider_process_started=True,
                    external_request_attempted=True,
                )
            for key, _ in selector.select(timeout=min(0.025, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name, maximum = key.data
                counts[name] += len(chunk)
                if counts[name] > maximum:
                    kill_group(process)
                    raise AdapterError(
                        "provider-output-limit",
                        f"local {provider_label} {name} exceeded its task limit",
                        provider_process_started=True,
                        external_request_attempted=True,
                    )
            process_count, rss = process_group_usage(process.pid)
            if process_count > limits["pids"]:
                kill_group(process)
                raise AdapterError(
                    "provider-pid-limit",
                    f"local {provider_label} exceeded its process-count limit",
                    provider_process_started=True,
                    external_request_attempted=True,
                )
            if rss > limits["memory_bytes"]:
                kill_group(process)
                raise AdapterError(
                    "provider-memory-limit",
                    f"local {provider_label} exceeded its aggregate process-group memory limit",
                    provider_process_started=True,
                    external_request_attempted=True,
                )
            try:
                cloud_agent.workspace_usage(workspace, limits)
            except cloud_agent.WorkerError as error:
                kill_group(process)
                raise AdapterError(
                    error.code,
                    str(error),
                    provider_process_started=True,
                    external_request_attempted=True,
                ) from error
            polled = process.poll()
            if polled is not None:
                return_code = polled
        if return_code is None:
            try:
                return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                kill_group(process)
                raise AdapterError(
                    "provider-timeout",
                    f"local {provider_label} exceeded the task wall-time limit",
                    provider_process_started=True,
                    external_request_attempted=True,
                ) from error
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
        if process.poll() is None:
            kill_group(process)
    if return_code != 0:
        raise AdapterError(
            "provider-failed",
            f"local {provider_label} exited nonzero ({return_code}); stdout={counts['stdout']} stderr={counts['stderr']}",
            provider_process_started=True,
            external_request_attempted=True,
        )
    return ProviderExecution(
        provider_id=provider_id,
        provider_process_started=True,
        external_request_attempted=True,
        external_request_observed=False,
        gateway_request_id=None,
        executable_path=identity["resolved_path"],
        executable_version=identity["version"],
        executable_sha256=identity["sha256"],
        stdout_bytes=counts["stdout"],
        stderr_bytes=counts["stderr"],
    )


class CodexProvider:
    provider_id = CODEX_PROVIDER_ID
    task_provider = TASK_PROVIDER_BY_ID[CODEX_PROVIDER_ID]
    sandbox_owner = CODEX_NATIVE_WORKSPACE_SANDBOX

    def __init__(
        self,
        cloud_agent: Any,
        codex_bin: Path | None = None,
        executable_pin: dict[str, Any] | None = None,
        *,
        model: str,
    ):
        self.cloud_agent = cloud_agent
        self.codex_bin = codex_bin or discover_codex_binary()
        self.executable_policy = executable_pin or LOCAL_CODEX_COMPATIBILITY_POLICY
        self.model = require_model(model)
        self._bound_execution_identity: dict[str, Any] | None = None

    def _identity(self) -> dict[str, Any]:
        return resolve_codex_observed_identity(self.codex_bin, self.executable_policy)

    def _auth_source(self) -> Path:
        """Resolve auth by metadata only; never open or parse credential content."""
        ambient_home = os.environ.get("HOME")
        configured_codex_home = os.environ.get("CODEX_HOME")
        if configured_codex_home:
            codex_home = Path(configured_codex_home)
        elif ambient_home:
            codex_home = Path(ambient_home) / ".codex"
        else:
            raise AdapterError("codex-auth-unavailable", "Codex authentication home is unavailable")
        try:
            resolved_codex_home = codex_home.resolve(strict=True)
            metadata = resolved_codex_home.lstat()
        except (FileNotFoundError, OSError) as error:
            raise AdapterError("codex-auth-unavailable", "Codex authentication home is unavailable") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise AdapterError("codex-auth-unavailable", "Codex authentication home is unsafe")
        auth_source = resolved_codex_home / "auth.json"
        try:
            auth_metadata = auth_source.lstat()
        except (FileNotFoundError, OSError) as error:
            raise AdapterError("codex-auth-unavailable", "Codex auth.json is unavailable") from error
        if (
            not stat.S_ISREG(auth_metadata.st_mode)
            or stat.S_ISLNK(auth_metadata.st_mode)
            or auth_metadata.st_uid != os.getuid()
            or auth_metadata.st_nlink != 1
            or not 1 <= auth_metadata.st_size <= 1024 * 1024
            or auth_metadata.st_mode & 0o022
        ):
            raise AdapterError("codex-auth-unavailable", "Codex auth.json is unsafe")
        return auth_source

    @staticmethod
    def _clone_auth(source: Path, destination: Path) -> None:
        clone_private_file(source, destination, "Codex credential")

    def _auth_environment(self, temporary_root: Path) -> dict[str, str]:
        auth_source = self._auth_source()
        scoped_codex_home = temporary_root / "codex-home"
        scoped_codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        scoped_auth = scoped_codex_home / "auth.json"
        try:
            self._clone_auth(auth_source, scoped_auth)
        except OSError as error:
            raise AdapterError("credential-broker-unavailable", "cannot stage scoped Codex authentication") from error
        environment = isolated_environment(temporary_root)
        environment["CODEX_HOME"] = str(scoped_codex_home)
        return environment

    def _preflight_for_identity(self, identity: dict[str, Any]) -> dict[str, Any]:
        return provider_preflight(
            provider_id=self.provider_id,
            task_provider=self.task_provider,
            model=self.model,
            identity=identity,
            authentication="local-codex-auth-metadata-present",
            credential_delivery="not-delivered-while-containment-gate-is-closed",
            runtime_package_id="openai-codex-cli",
            macos_sandbox="codex-native-workspace-write",
            sandbox_owner="provider",
            outer_seatbelt=False,
            provider_tool_network="codex-native-workspace-write-policy",
        )

    def preflight(self) -> dict[str, Any]:
        identity = self._identity()
        self._auth_source()
        result = self._preflight_for_identity(identity)
        self._bound_execution_identity = result["provider_execution_identity"]
        return result

    def assert_execution_identity(self, expected: dict[str, Any]) -> None:
        """Re-resolve immediately before consent/process use and require exact identity."""
        actual = self._preflight_for_identity(self._identity())["provider_execution_identity"]
        if self._bound_execution_identity != expected or actual != expected:
            raise AdapterError(
                "provider-execution-identity-mismatch",
                "Codex adapter/provider identity changed after immutable task preflight",
            )

    def command_preview(
        self,
        workspace: Path,
        identity: dict[str, Any] | None = None,
        executable_path: Path | None = None,
    ) -> list[str]:
        """Return the exact secret-free Codex argv without starting a process."""
        identity = identity or self._identity()
        command = self.cloud_agent.codex_command(
            executable_path or Path(identity["resolved_path"]),
            workspace,
            model=require_model(self.model),
        )
        command[2:2] = [
            "-c",
            'shell_environment_policy.inherit="none"',
            "-c",
            'shell_environment_policy.set={PATH="/usr/bin:/bin",LANG="C.UTF-8",LC_ALL="C.UTF-8",TZ="UTC"}',
        ]
        return command

    def execute(
        self,
        *,
        workspace: Path,
        prompt: bytes,
        limits: dict[str, int],
        temporary_root: Path,
        cancel_file: Path | None,
    ) -> ProviderExecution:
        require_live_containment_backend()
        identity = self._identity()
        actual_execution_identity = self._preflight_for_identity(identity)["provider_execution_identity"]
        if (
            self._bound_execution_identity is None
            or actual_execution_identity != self._bound_execution_identity
        ):
            raise AdapterError(
                "provider-execution-identity-mismatch",
                "Codex adapter/provider identity changed before provider process start",
            )
        executable_snapshot = stage_verified_executable_snapshot(
            Path(identity["resolved_path"]),
            identity,
            temporary_root,
            "Codex",
        )
        command = self.command_preview(workspace, identity, executable_snapshot)
        environment = self._auth_environment(temporary_root)
        return supervise_provider(
            cloud_agent=self.cloud_agent,
            provider_id=self.provider_id,
            provider_label="Codex",
            identity=identity,
            command=command,
            environment=environment,
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
            sandbox_owner=self.sandbox_owner,
        )


class ClaudeCodeProvider:
    provider_id = CLAUDE_PROVIDER_ID
    task_provider = TASK_PROVIDER_BY_ID[CLAUDE_PROVIDER_ID]

    def __init__(
        self,
        cloud_agent: Any,
        executable: Path = DEFAULT_CLAUDE_BIN,
        executable_pin: dict[str, str] | None = None,
        *,
        model: str,
    ):
        self.cloud_agent = cloud_agent
        self.executable = executable
        self.executable_pin = executable_pin or LOCAL_CLAUDE_PIN
        self.model = require_model(model)

    def _identity(self) -> dict[str, Any]:
        try:
            return self.cloud_agent.resolve_executable_identity(self.executable, self.executable_pin)
        except self.cloud_agent.WorkerError as error:
            raise AdapterError(error.code, str(error)) from error

    @staticmethod
    def _authentication_present() -> None:
        try:
            opaque_environment_secret("ANTHROPIC_API_KEY", "claude-auth-unavailable", "Claude Code")
        except AdapterError as error:
            raise AdapterError(
                error.code,
                "Claude Code bare mode requires a bounded ANTHROPIC_API_KEY; OAuth and keychain access are disabled",
            ) from error

    def _environment(self, temporary_root: Path) -> dict[str, str]:
        self._authentication_present()
        environment = isolated_environment(temporary_root)
        # The value is transferred opaquely. Python never parses, logs, or persists it.
        environment["ANTHROPIC_API_KEY"] = opaque_environment_secret(
            "ANTHROPIC_API_KEY", "claude-auth-unavailable", "Claude Code"
        )
        environment["CLAUDE_CONFIG_DIR"] = str(temporary_root / "claude-config")
        environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        environment["DISABLE_AUTOUPDATER"] = "1"
        environment["DISABLE_TELEMETRY"] = "1"
        return environment

    def _command(self, identity: dict[str, Any], temporary_root: Path) -> list[str]:
        model = require_model(self.model)
        mcp = temporary_root / "claude-mcp.json"
        write_private_json(mcp, {"mcpServers": {}})
        command = [
            identity["resolved_path"],
            "--print",
            "--bare",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp),
            "--permission-mode",
            "acceptEdits",
            "--tools",
            "Read,Write,Edit,Glob,Grep",
            "--allowed-tools",
            "Read,Write,Edit,Glob,Grep",
            "--output-format",
            "json",
        ]
        command.extend(["--model", model])
        return command

    def preflight(self) -> dict[str, Any]:
        identity = self._identity()
        self._authentication_present()
        return provider_preflight(
            provider_id=self.provider_id,
            task_provider=self.task_provider,
            model=self.model,
            identity=identity,
            authentication="ANTHROPIC_API_KEY-name-present",
            credential_delivery="not-delivered-while-containment-gate-is-closed",
            runtime_package_id="anthropic-claude-code",
            provider_safety_flags=[
                "--print", "--bare", "--safe-mode", "--disable-slash-commands",
                "--no-session-persistence", "--strict-mcp-config",
            ],
        )

    def execute(
        self,
        *,
        workspace: Path,
        prompt: bytes,
        limits: dict[str, int],
        temporary_root: Path,
        cancel_file: Path | None,
    ) -> ProviderExecution:
        require_live_containment_backend()
        identity = self._identity()
        environment = self._environment(temporary_root)
        return supervise_provider(
            cloud_agent=self.cloud_agent,
            provider_id=self.provider_id,
            provider_label="Claude Code",
            identity=identity,
            command=self._command(identity, temporary_root),
            environment=environment,
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
        )


class OpenCodeProvider:
    provider_id = OPENCODE_PROVIDER_ID
    task_provider = TASK_PROVIDER_BY_ID[OPENCODE_PROVIDER_ID]

    def __init__(
        self,
        cloud_agent: Any,
        executable: Path = DEFAULT_OPENCODE_BIN,
        executable_pin: dict[str, str] | None = None,
        *,
        model: str,
    ):
        self.cloud_agent = cloud_agent
        self.executable = executable
        self.executable_pin = executable_pin or LOCAL_OPENCODE_PIN
        self.model = require_model(model)

    def _identity(self) -> dict[str, Any]:
        try:
            return self.cloud_agent.resolve_executable_identity(self.executable, self.executable_pin)
        except self.cloud_agent.WorkerError as error:
            raise AdapterError(error.code, str(error)) from error

    @staticmethod
    def _config_source() -> Path:
        ambient_home = os.environ.get("HOME")
        if not ambient_home:
            raise AdapterError("opencode-config-unavailable", "OpenCode config home is unavailable")
        directory = Path(ambient_home) / ".config/opencode"
        source = directory / "opencode.json"
        jsonc = directory / "opencode.jsonc"
        if not source.exists() and jsonc.exists():
            raise AdapterError(
                "opencode-config-invalid",
                "OpenCode JSONC is not accepted by the security preflight; provide strict opencode.json",
            )
        return private_credential(
            source,
            "opencode-config-unavailable",
            "OpenCode opencode.json",
        )

    @staticmethod
    def _read_stable_private_bytes(source: Path, label: str) -> bytes:
        before = source.lstat()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_mode & 0o022
                or opened.st_size != before.st_size
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise AdapterError("opencode-config-unavailable", f"{label} changed or is unsafe")
            chunks: list[bytes] = []
            remaining = min(opened.st_size, MAX_CREDENTIAL_BYTES) + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) != opened.st_size or len(payload) > MAX_CREDENTIAL_BYTES:
                raise AdapterError("opencode-config-unavailable", f"{label} is incomplete or unbounded")
        finally:
            os.close(descriptor)
        after = source.lstat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AdapterError("opencode-config-unavailable", f"{label} changed during preflight")
        return payload

    def _strict_selected_provider_metadata(self) -> dict[str, Any]:
        """Validate public provider metadata without copying credential material.

        Credential-bearing fields are detected conservatively before JSON
        deserialization.  Escaped JSON strings are rejected as well, preventing a
        credential key from bypassing that pre-parse check with ``\\u`` escapes.
        """
        model = require_model(self.model, "OpenCode model")
        if "/" not in model:
            raise AdapterError(
                "model-invalid",
                "OpenCode model must use the provider/model form",
            )
        provider_id, _, model_name = model.partition("/")
        if not provider_id or not model_name:
            raise AdapterError("model-invalid", "OpenCode model must name both provider and model")
        source = self._config_source()
        payload = self._read_stable_private_bytes(source, "OpenCode opencode.json")
        if b"\\" in payload:
            raise AdapterError(
                "opencode-config-invalid",
                "OpenCode security preflight rejects escaped JSON strings; use literal UTF-8 in strict opencode.json",
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AdapterError("opencode-config-invalid", "OpenCode config is not UTF-8") from error
        if OPENCODE_SECRET_KEY.search(text):
            raise AdapterError(
                OPENCODE_SCOPED_CREDENTIAL_ERROR,
                "OpenCode config contains credential-bearing fields; the adapter will not parse or copy their values",
            )
        allowed_public_keys = {
            "$schema",
            "provider",
            provider_id,
            "name",
            "npm",
            "models",
            model_name,
            "options",
            "baseURL",
        }
        observed_keys = set(SIMPLE_JSON_OBJECT_KEY.findall(text))
        unknown_keys = observed_keys - allowed_public_keys
        if unknown_keys:
            raise AdapterError(
                "opencode-config-invalid",
                f"OpenCode config contains fields outside the secret-free preflight shape: {sorted(unknown_keys)}",
            )
        parsed = strict_json_bytes(payload, "OpenCode opencode.json")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("provider"), dict):
            raise AdapterError(
                "opencode-config-invalid",
                "OpenCode config lacks a provider map",
            )
        entry = parsed["provider"].get(provider_id)
        if not isinstance(entry, dict):
            raise AdapterError(
                "opencode-config-unavailable",
                f"OpenCode config lacks the {provider_id} provider definition required by model {model}",
            )
        allowed_entry_keys = {"name", "npm", "models", "options"}
        unknown_entry_keys = set(entry) - allowed_entry_keys
        if unknown_entry_keys:
            raise AdapterError(
                "opencode-config-invalid",
                f"OpenCode provider metadata contains unsupported fields: {sorted(unknown_entry_keys)}",
            )
        if "npm" in entry and (
            not isinstance(entry["npm"], str)
            or not 1 <= len(entry["npm"]) <= 256
            or any(ord(character) < 0x20 for character in entry["npm"])
        ):
            raise AdapterError("opencode-config-invalid", "OpenCode provider npm is invalid")
        models = entry.get("models")
        if models is not None:
            if not isinstance(models, dict) or model_name not in models or not isinstance(models[model_name], dict):
                raise AdapterError(
                    "opencode-config-unavailable",
                    f"OpenCode provider {provider_id} does not declare model {model_name}",
                )
        options = entry.get("options", {})
        if not isinstance(options, dict) or set(options) - {"baseURL"}:
            raise AdapterError(
                "opencode-config-invalid",
                "OpenCode provider options may contain only public baseURL metadata during preflight",
            )
        endpoint: str | None = None
        endpoint_digest: str | None = None
        if "baseURL" not in options:
            raise AdapterError(
                "opencode-config-unavailable",
                "OpenCode selected provider must expose an explicit public HTTPS baseURL identity",
            )
        if "baseURL" in options:
            base_url = options["baseURL"]
            if not isinstance(base_url, str) or not 1 <= len(base_url) <= 2048:
                raise AdapterError("opencode-config-invalid", "OpenCode provider baseURL is invalid")
            try:
                endpoint = self.cloud_agent.canonical_https_endpoint(base_url)
            except self.cloud_agent.WorkerError as error:
                raise AdapterError(
                    "opencode-config-invalid",
                    f"OpenCode provider baseURL is not canonical credential-free HTTPS: {error}",
                ) from error
            endpoint_digest = hashlib.sha256(
                json.dumps(
                    {"kind": "https", "canonical_endpoint": endpoint},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        return {
            "provider_id": provider_id,
            "model_name": model_name,
            "config_sha256": hashlib.sha256(payload).hexdigest(),
            "endpoint": endpoint,
            "endpoint_sha256": endpoint_digest,
            "runtime_package": entry.get("npm"),
            "model_declared": models is not None,
        }

    @staticmethod
    def _shared_auth_metadata_present() -> bool:
        ambient_home = os.environ.get("HOME")
        if not ambient_home:
            return False
        source = Path(ambient_home) / ".local/share/opencode/auth.json"
        try:
            private_credential(
                source,
                "opencode-auth-unavailable",
                "OpenCode shared auth.json",
            )
        except AdapterError as error:
            if not source.exists():
                return False
            raise error
        return True

    def _environment(self, temporary_root: Path) -> dict[str, str]:
        del temporary_root
        raise AdapterError(
            OPENCODE_SCOPED_CREDENTIAL_ERROR,
            "OpenCode exposes no reviewed per-provider credential handle; the adapter will not copy shared auth.json "
            "or rewrite credential-bearing provider config",
        )

    def _command(self, identity: dict[str, Any], workspace: Path) -> list[str]:
        model = require_model(self.model)
        command = [
            identity["resolved_path"],
            "run",
            "--pure",
            "--format",
            "json",
            "--dir",
            str(workspace),
            "--auto",
        ]
        command.extend(["--model", model])
        return command

    def preflight(self) -> dict[str, Any]:
        metadata = self._strict_selected_provider_metadata()
        shared_auth = self._shared_auth_metadata_present()
        identity = self._identity()
        return provider_preflight(
            provider_id=self.provider_id,
            task_provider=self.task_provider,
            model=self.model,
            identity=identity,
            authentication=(
                "shared-auth-metadata-present-not-deliverable"
                if shared_auth
                else "no-scoped-credential-handle"
            ),
            credential_delivery="disabled-no-reviewed-scoped-opencode-credential-interface",
            endpoint_kind="https",
            canonical_endpoint=metadata["endpoint"],
            runtime_package_id=metadata["runtime_package"] or "opencode-cli",
            non_secret_config_sha256=metadata["config_sha256"],
            provider_config_sha256=metadata["config_sha256"],
            provider_endpoint=metadata["endpoint"],
            provider_endpoint_sha256=metadata["endpoint_sha256"],
            provider_runtime_package=metadata["runtime_package"],
            provider_model_name=metadata["model_name"],
            provider_model_declared=metadata["model_declared"],
            provider_safety_flags=["run", "--pure", "--format=json"],
        )

    def execute(
        self,
        *,
        workspace: Path,
        prompt: bytes,
        limits: dict[str, int],
        temporary_root: Path,
        cancel_file: Path | None,
    ) -> ProviderExecution:
        require_live_containment_backend()
        identity = self._identity()
        environment = self._environment(temporary_root)
        return supervise_provider(
            cloud_agent=self.cloud_agent,
            provider_id=self.provider_id,
            provider_label="OpenCode",
            identity=identity,
            command=self._command(identity, workspace),
            environment=environment,
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
        )


class GeminiCliProvider:
    provider_id = GEMINI_PROVIDER_ID
    task_provider = TASK_PROVIDER_BY_ID[GEMINI_PROVIDER_ID]

    def __init__(
        self,
        cloud_agent: Any,
        executable: Path = DEFAULT_GEMINI_BIN,
        executable_pin: dict[str, str] | None = None,
        *,
        model: str,
    ):
        self.cloud_agent = cloud_agent
        self.executable = executable
        self.executable_pin = executable_pin or LOCAL_GEMINI_PIN
        self.model = require_model(model)

    def _identity(self) -> dict[str, Any]:
        try:
            identity = self.cloud_agent.resolve_executable_identity(self.executable, self.executable_pin)
        except self.cloud_agent.WorkerError as error:
            raise AdapterError(error.code, str(error)) from error
        bundle = Path(identity["resolved_path"]).parent
        digest, files, size = provider_tree_digest(bundle)
        if digest != self.executable_pin["bundle_sha256"]:
            raise AdapterError("executable-identity-invalid", "Gemini CLI bundle differs from the reviewed pin")
        return {**identity, "bundle_sha256": digest, "bundle_files": files, "bundle_bytes": size}

    @staticmethod
    def _oauth_source() -> Path:
        ambient_home = os.environ.get("HOME")
        if not ambient_home:
            raise AdapterError("gemini-auth-unavailable", "Gemini CLI authentication home is unavailable")
        return private_auth_source(
            Path(ambient_home) / ".gemini",
            "oauth_creds.json",
            "gemini-auth-unavailable",
            "Gemini CLI oauth_creds.json",
        )

    @staticmethod
    def _authentication() -> str:
        if "GEMINI_API_KEY" in os.environ:
            opaque_environment_secret("GEMINI_API_KEY", "gemini-auth-unavailable", "Gemini CLI")
            return "gemini-api-key"
        GeminiCliProvider._oauth_source()
        return "oauth-personal"

    def _environment(self, temporary_root: Path) -> dict[str, str]:
        environment = isolated_environment(temporary_root)
        gemini_home = Path(environment["HOME"])
        authentication = self._authentication()
        if authentication == "gemini-api-key":
            # The value is transferred opaquely. Python never parses, logs, or persists it.
            environment["GEMINI_API_KEY"] = opaque_environment_secret(
                "GEMINI_API_KEY", "gemini-auth-unavailable", "Gemini CLI"
            )
        else:
            clone_private_file(
                self._oauth_source(),
                gemini_home / ".gemini/oauth_creds.json",
                "Gemini CLI OAuth credential",
            )
        write_private_json(
            gemini_home / ".gemini/settings.json",
            {
                "security": {"auth": {"selectedType": authentication}},
                "hooksConfig": {"enabled": False},
                "mcpServers": {},
                "experimental": {"enableAgents": False},
                "tools": {"shell": {"enableInteractiveShell": False}},
                "general": {"defaultApprovalMode": "auto_edit"},
            },
        )
        environment["GEMINI_CLI_HOME"] = str(gemini_home)
        environment["GEMINI_CLI_NO_RELAUNCH"] = "1"
        environment["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        environment["NO_COLOR"] = "1"
        return environment

    def _command(self, identity: dict[str, Any]) -> list[str]:
        model = require_model(self.model)
        command = [
            identity["resolved_path"],
            "--prompt",
            "Execute the complete UTF-8 task supplied on stdin.",
            "--skip-trust",
            "--approval-mode",
            "auto_edit",
            "--output-format",
            "json",
        ]
        command.extend(["--model", model])
        return command

    def preflight(self) -> dict[str, Any]:
        identity = self._identity()
        authentication = self._authentication()
        return provider_preflight(
            provider_id=self.provider_id,
            task_provider=self.task_provider,
            model=self.model,
            identity=identity,
            authentication=f"{authentication}-metadata-or-name-present",
            credential_delivery="not-delivered-while-containment-gate-is-closed",
            runtime_package_id="google-gemini-cli",
            executable_bundle_sha256=identity["bundle_sha256"],
            executable_bundle_files=identity["bundle_files"],
            provider_safety_flags=["--prompt", "--approval-mode=auto_edit", "--output-format=json"],
        )

    def execute(
        self,
        *,
        workspace: Path,
        prompt: bytes,
        limits: dict[str, int],
        temporary_root: Path,
        cancel_file: Path | None,
    ) -> ProviderExecution:
        require_live_containment_backend()
        identity = self._identity()
        environment = self._environment(temporary_root)
        return supervise_provider(
            cloud_agent=self.cloud_agent,
            provider_id=self.provider_id,
            provider_label="Gemini CLI",
            identity=identity,
            command=self._command(identity),
            environment=environment,
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
        )


def provider_registry(cloud_agent: Any, model: str) -> dict[str, CodeAgentProvider]:
    model = require_model(model)
    for dependency in (Path(__file__).resolve().parent, Path(__file__).resolve().parents[1] / "codeagent-launcher"):
        if str(dependency) not in sys.path:
            sys.path.insert(0, str(dependency))
    from docker_provider import DockerCodexProvider, DockerOpenCodeProvider
    return {
        CODEX_PROVIDER_ID: DockerCodexProvider(sys.modules[__name__], cloud_agent, model=model),
        CLAUDE_PROVIDER_ID: ClaudeCodeProvider(cloud_agent, model=model),
        OPENCODE_PROVIDER_ID: DockerOpenCodeProvider(sys.modules[__name__], cloud_agent, model=model),
        GEMINI_PROVIDER_ID: GeminiCliProvider(cloud_agent, model=model),
    }


def validate_task_value_for_provider(
    cloud_agent: Any,
    task: Any,
    provider: CodeAgentProvider,
) -> dict[str, Any]:
    """Validate an in-memory task without changing its provider-bound payload."""
    expected = provider.task_provider
    if not isinstance(task, dict) or "model" not in task:
        raise AdapterError(
            "model-binding-required",
            "legacy task lacks an immutable CodeAgent model binding",
        )
    provider_model = getattr(provider, "model", None)
    if (
        task.get("provider") != expected
        or not isinstance(task.get("consent"), dict)
        or task["consent"].get("provider") != expected
    ):
        raise AdapterError(
            "provider-mismatch",
            f"task and consent provider must both be {expected} for adapter {provider.provider_id}",
        )
    if "model" not in task["consent"]:
        raise AdapterError(
            "model-binding-required",
            "legacy consent lacks an immutable CodeAgent model binding",
        )
    try:
        provider_model = require_model(provider_model, "adapter model")
        task_model = require_model(task["model"], "task model")
        consent_model = require_model(task["consent"]["model"], "consent model")
    except AdapterError as error:
        raise AdapterError("model-binding-required", str(error)) from error
    if task_model != provider_model or consent_model != provider_model:
        raise AdapterError(
            "model-mismatch",
            "task, consent and adapter must use the same immutable CodeAgent model selection",
        )
    try:
        cloud_agent.validate_task_schema(task)
        cloud_agent.validate_authorization(task)
    except cloud_agent.WorkerError as error:
        raise AdapterError(error.code, str(error)) from error
    return task


def validate_task_for_provider(cloud_agent: Any, task_path: Path, provider: CodeAgentProvider) -> dict[str, Any]:
    """Load a bounded strict-JSON task, then apply provider-aware validation."""
    try:
        task = cloud_agent.load_json(task_path, cloud_agent.MAX_TASK_BYTES, "cloud task")
    except cloud_agent.WorkerError as error:
        raise AdapterError(error.code, str(error)) from error
    return validate_task_value_for_provider(cloud_agent, task, provider)


def base_status(task: dict[str, Any], provider_id: str, limits: dict[str, int]) -> dict[str, Any]:
    return {
        "schema_version": STATUS_VERSION,
        "job_id": task["job_id"],
        "need_id": task["need_spec"]["need_id"],
        "title": task["package_intent"]["display_name"],
        "provider_id": provider_id,
        "task_provider": task["provider"],
        "model": task["model"],
        "adapter": "local-codeagent-adapter",
        "adapter_version": None,
        "adapter_sha256": None,
        "identity_observed": False,
        "execution_available": None,
        "execution_blocker": None,
        "provider_execution_identity_sha256": None,
        "process_id": os.getpid(),
        "status": "codeagent-running",
        "current_stage": "codeagent-authoring-source",
        "progress_percent": 30,
        "provider_process_started": False,
        "external_request_attempted": False,
        "external_request_observed": False,
        "gateway_request_id": None,
        "executable_path": None,
        "executable_version": None,
        "executable_sha256": None,
        "executable_identity_policy_version": None,
        "executable_identity_basis": None,
        "executable_signing_team_identifier": None,
        "executable_digest_reviewed": None,
        "provider_protocol_version": None,
        "provider_protocol_sha256": None,
        "provider_protocol_preflight_passed": False,
        "handoff_relative_path": None,
        "handoff_sha256": None,
        "error": None,
        "need_spec_revision": task["need_spec_current_revision"],
        "immutable_task_digest_sha256": task["immutable_task_digest_sha256"],
        "consent_id": task["consent"]["consent_id"],
        "consent_consumed": False,
        "retry_policy": "terminal-status-requires-new-job; new-consent-required-when-consumed",
        "requested_limits": task["limits"],
        "effective_limits": limits,
        "events": [],
        "builder_invoked": False,
        "verifier_invoked": False,
        "installation_performed": False,
        "publication_performed": False,
        "updated_at_utc": None,
    }


def transition_status(
    cloud_agent: Any,
    status_path: Path,
    state: dict[str, Any],
    event: str,
    **updates: Any,
) -> None:
    state.update(updates)
    state["updated_at_utc"] = cloud_agent.now_utc()
    state["events"].append(
        {
            "event": event,
            "stage": state["current_stage"],
            "progress_percent": state["progress_percent"],
            "at_utc": state["updated_at_utc"],
        }
    )
    state["events"] = state["events"][-MAX_STATUS_EVENTS:]
    atomic_status(status_path, state)


def execute_task(
    cloud_agent: Any,
    provider: CodeAgentProvider,
    task_path: Path,
    output_root: Path,
    status_path: Path,
    *,
    confirm_job: str,
    confirm_consent: str,
    acknowledge_external_cost: bool,
    cancel_file: Path | None = None,
) -> dict[str, Any]:
    task = validate_task_for_provider(cloud_agent, task_path, provider)
    if confirm_job != task["job_id"] or confirm_consent != task["consent"]["consent_id"]:
        raise AdapterError("external-opt-in-required", "job and consent confirmations must exactly match the task")
    if not acknowledge_external_cost:
        raise AdapterError("external-opt-in-required", "explicit external-cost acknowledgement is required")
    previous = read_status(status_path)
    if previous is not None:
        expected_binding = {
            "job_id": task["job_id"],
            "need_id": task["need_spec"]["need_id"],
            "need_spec_revision": task["need_spec_current_revision"],
            "immutable_task_digest_sha256": task["immutable_task_digest_sha256"],
            "consent_id": task["consent"]["consent_id"],
            "provider_id": provider.provider_id,
            "task_provider": provider.task_provider,
            "model": provider.model,
        }
        if any(previous.get(key) != value for key, value in expected_binding.items()):
            raise AdapterError("status-binding-conflict", "existing status is bound to another immutable task")
        if previous.get("status") == "source-ready":
            return previous
        if previous.get("status") == "codeagent-running" and process_active(previous.get("process_id")):
            raise AdapterError("job-already-running", "this CodeAgent job already has a live local worker")
        raise AdapterError("job-state-conflict", "this CodeAgent job already has a terminal or stale adapter status")
    output_root = cloud_agent.safe_private_directory(output_root)
    limits = effective_local_limits(task["limits"])
    state = base_status(task, provider.provider_id, limits)
    admitted = False
    temporary_root: Path | None = None
    workspace: Path | None = None
    try:
        with one_job_lease(output_root, parallel_authoring=getattr(provider, "parallel_authoring", False)), _attempt_lease(output_root):
            # Recheck after the stable per-attempt lock: another process may
            # have completed between the initial status read and admission.
            if read_status(status_path) is not None:
                raise AdapterError("job-state-conflict", "this CodeAgent output already has an adapter status")
            admitted = True
            transition_status(cloud_agent, status_path, state, "job-admitted")
            workspaces = cloud_agent.safe_private_directory(output_root / "workspaces")
            temporary_root = Path(tempfile.mkdtemp(prefix=f".{task['job_id']}-adapter-", dir=output_root))
            workspace = Path(tempfile.mkdtemp(prefix=f"{task['job_id']}-", dir=workspaces))
            contract_digest = cloud_agent.prepare_workspace(workspace, task)
            preflight = provider.preflight()
            transition_status(
                cloud_agent,
                status_path,
                state,
                "provider-identity-observed",
                adapter_version=preflight.get("adapter_version"),
                adapter_sha256=preflight.get("adapter_sha256"),
                identity_observed=bool(preflight.get("identity_observed", True)),
                execution_available=preflight.get("execution_available", True),
                execution_blocker=preflight.get("execution_blocker"),
                provider_execution_identity_sha256=(
                    preflight.get("provider_execution_identity", {}).get("identity_sha256")
                    if isinstance(preflight.get("provider_execution_identity"), dict)
                    else None
                ),
                executable_path=preflight.get("executable_path"),
                executable_version=preflight.get("executable_version"),
                executable_sha256=preflight.get("executable_sha256"),
                executable_identity_policy_version=preflight.get("identity_policy_version"),
                executable_identity_basis=preflight.get("identity_basis"),
                executable_signing_team_identifier=preflight.get("signing_team_identifier"),
                executable_digest_reviewed=preflight.get("digest_reviewed"),
                provider_protocol_version=preflight.get("protocol_version"),
                provider_protocol_sha256=preflight.get("protocol_sha256"),
                provider_protocol_preflight_passed=bool(preflight.get("protocol_preflight_passed")),
            )
            observed_identity = preflight.get("provider_execution_identity")
            if preflight.get("synthetic_offline_test") is not True:
                if not isinstance(observed_identity, dict):
                    raise AdapterError(
                        "provider-execution-identity-unavailable",
                        "provider preflight did not return an exact execution identity",
                    )
                try:
                    cloud_agent.validate_provider_execution_identity(
                        observed_identity,
                        task["provider"],
                        "adapter.preflight.provider_execution_identity",
                    )
                except cloud_agent.WorkerError as error:
                    raise AdapterError(error.code, str(error)) from error
                if observed_identity != task.get("provider_execution_identity"):
                    raise AdapterError(
                        "provider-execution-identity-mismatch",
                        "observed adapter/provider/endpoint identity differs from the immutable task and consent",
                    )
                assert_execution_identity = getattr(provider, "assert_execution_identity", None)
                if callable(assert_execution_identity):
                    assert_execution_identity(observed_identity)
            if preflight.get("execution_available", True) is not True:
                blocker = preflight.get("execution_blocker")
                if not isinstance(blocker, dict):
                    raise AdapterError(LIVE_CONTAINMENT_ERROR, "provider execution is unavailable")
                raise AdapterError(
                    str(blocker.get("code") or LIVE_CONTAINMENT_ERROR),
                    str(blocker.get("message") or "provider execution is unavailable"),
                )
            if cancellation_requested(cancel_file):
                raise AdapterError("provider-cancelled", "local CodeAgent job was cancelled before consent use")
            cloud_agent.claim_consent(output_root, task)
            transition_status(
                cloud_agent,
                status_path,
                state,
                "consent-consumed",
                consent_consumed=True,
                current_stage="codeagent-authoring-source",
                progress_percent=30,
            )
            bind_task = getattr(provider, "bind_task", None)
            if callable(bind_task):
                bind_task(task)
            execution = provider.execute(
                workspace=workspace,
                prompt=cloud_agent.create_prompt(task, contract_digest),
                limits=limits,
                temporary_root=temporary_root,
                cancel_file=cancel_file,
            )
            transition_status(
                cloud_agent,
                status_path,
                state,
                "provider-process-complete",
                container_receipt=execution.container_receipt,
                provider_process_started=execution.provider_process_started,
                external_request_attempted=execution.external_request_attempted,
                external_request_observed=execution.external_request_observed,
                gateway_request_id=execution.gateway_request_id,
                executable_path=execution.executable_path,
                executable_version=execution.executable_version,
                executable_sha256=execution.executable_sha256,
                progress_percent=55,
                current_stage="validating-untrusted-source",
            )
            provider_result_value = derive_provider_control_record(cloud_agent, workspace, task)
            transition_status(
                cloud_agent,
                status_path,
                state,
                "adapter-control-record-derived",
            )
            cloud_agent.workspace_usage(workspace, limits)
            provider_result = cloud_agent.validate_provider_result(
                provider_result_value,
                task,
                workspace / "source",
            )
            records = cloud_agent.audit_workspace(workspace, task, provider_result, contract_digest)
            provider_execution = {
                "mode": "direct-local-explicit-opt-in",
                "adapter": "local-codeagent-adapter",
                "provider_id": execution.provider_id,
                "external_request_attempted": execution.external_request_attempted,
                "external_request_observed": execution.external_request_observed,
                "gateway_request_id": execution.gateway_request_id,
                "executable_sha256": execution.executable_sha256,
                "isolation_policy_version": None,
                "runner_id": None,
                "runner_identity_sha256": None,
                "isolation_policy_sha256": None,
                "receipt_digest_sha256": None,
            }
            if execution.container_receipt is not None:
                receipt = execution.container_receipt
                provider_execution.update({
                    "mode": "docker-local-explicit-opt-in", "adapter": "docker-codeagent-launcher",
                    "isolation_policy_version": "vibapp.docker-source-v1",
                    "runner_id": receipt["backend_execution_id"],
                    "runner_identity_sha256": receipt["image_digest_sha256"],
                    "isolation_policy_sha256": receipt["network_policy_digest_sha256"],
                    "receipt_digest_sha256": hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                })
            handoff = cloud_agent.persist_handoff(output_root, workspace, task, records, provider_execution)
            relative_handoff = handoff.relative_to(output_root).as_posix()
            transition_status(
                cloud_agent,
                status_path,
                state,
                "source-handoff-persisted",
                status="source-ready",
                current_stage="awaiting-separate-builder",
                progress_percent=60,
                handoff_relative_path=relative_handoff,
                handoff_sha256=cloud_agent.sha256_file(handoff),
            )
            return state
    except AdapterError as error:
        if not admitted:
            raise  # Contention consumes no consent and writes no terminal state.
        transition_status(
            cloud_agent,
            status_path,
            state,
            "job-failed",
            status="codeagent-cancelled" if error.code == "provider-cancelled" else "codeagent-failed",
            current_stage="codeagent-cancelled" if error.code == "provider-cancelled" else "codeagent-failed-closed",
            provider_process_started=state["provider_process_started"] or error.provider_process_started,
            external_request_attempted=state["external_request_attempted"] or error.external_request_attempted,
            external_request_observed=state["external_request_observed"] or error.external_request_observed,
            **({"failure_diagnostic": error.failure_diagnostic} if error.failure_diagnostic is not None else {}),
            progress_percent=0,
            error={"code": error.code, "message": str(error)[:1000]},
        )
        raise
    except cloud_agent.WorkerError as error:
        attempted = bool(getattr(error, "external_request_attempted", False))
        transition_status(
            cloud_agent,
            status_path,
            state,
            "source-validation-failed",
            status="codeagent-failed",
            current_stage="source-validation-failed-closed",
            external_request_attempted=state["external_request_attempted"] or attempted,
            progress_percent=0,
            error={"code": error.code, "message": str(error)[:1000]},
        )
        raise AdapterError(
            error.code,
            str(error),
            provider_process_started=state["provider_process_started"],
            external_request_attempted=state["external_request_attempted"],
        ) from error
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VibApp pluggable local CodeAgent adapter")
    parser.add_argument("--cloud-agent", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)
    preflight = subcommands.add_parser("preflight")
    preflight.add_argument("--provider", default=CODEX_PROVIDER_ID)
    preflight.add_argument("--model", required=True)
    providers = subcommands.add_parser("providers")
    providers.add_argument("--provider", action="append")
    providers.add_argument("--model", required=True)
    status = subcommands.add_parser("status")
    status.add_argument("--status-file", type=Path, required=True)
    run = subcommands.add_parser("run")
    run.add_argument("--provider", default=CODEX_PROVIDER_ID)
    run.add_argument("--model", required=True)
    run.add_argument("--task", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--status-file", type=Path, required=True)
    run.add_argument("--confirm-job", required=True)
    run.add_argument("--confirm-consent", required=True)
    run.add_argument("--acknowledge-external-cost", action="store_true")
    run.add_argument("--cancel-file", type=Path)
    return parser


def output(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        cloud_agent = load_cloud_agent(arguments.cloud_agent)
        if arguments.command == "status":
            status = read_status(arguments.status_file)
            if status is None:
                raise AdapterError("status-not-found", "adapter status does not exist")
            output({"ok": True, "status": status})
            return 0
        requested_model = require_model(arguments.model)
        providers = provider_registry(cloud_agent, requested_model)
        if arguments.command == "providers":
            selected = arguments.provider or sorted(providers)
            unknown = [item for item in selected if item not in providers]
            if unknown:
                raise AdapterError("provider-unsupported", f"unsupported CodeAgent provider: {unknown[0]}")
            output({"ok": True, "schema_version": ADAPTER_VERSION, "providers": [providers[item].preflight() for item in selected]})
            return 0
        provider = providers.get(arguments.provider)
        if provider is None:
            raise AdapterError("provider-unsupported", f"unsupported CodeAgent provider: {arguments.provider}")
        if arguments.command == "preflight":
            output({"ok": True, "provider": provider.preflight()})
            return 0
        status = execute_task(
            cloud_agent,
            provider,
            arguments.task,
            arguments.output_root,
            arguments.status_file,
            confirm_job=arguments.confirm_job,
            confirm_consent=arguments.confirm_consent,
            acknowledge_external_cost=arguments.acknowledge_external_cost,
            cancel_file=arguments.cancel_file,
        )
        output({"ok": True, "status": status})
        return 0
    except (AdapterError, KeyError) as error:
        code = error.code if isinstance(error, AdapterError) else "provider-unsupported"
        output({
            "ok": False,
            "code": code,
            "message": str(error),
            "provider_process_started": bool(getattr(error, "provider_process_started", False)),
            "external_request_attempted": bool(getattr(error, "external_request_attempted", False)),
            "external_request_observed": False,
            "gateway_request_id": None,
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
