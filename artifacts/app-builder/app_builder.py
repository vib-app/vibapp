#!/usr/bin/env python3
"""Bounded source-handoff intake and quarantine-only VibApp Builder."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import time
import tomllib
from typing import Any, Protocol

from common import (
    APP_ID_RE,
    CAPABILITY_GRANT_POLICY,
    IDENTIFIER_RE,
    LOCAL_HOST_CAPABILITY_AVAILABILITY,
    SEMVER_RE,
    PipelineError,
    ProcessLimits,
    atomic_json,
    canonical_json,
    derive_host_presentation,
    ensure_private_directory,
    exclusive_copy,
    exclusive_write,
    load_json,
    lstat_regular,
    normalized_relative_path,
    now_utc,
    package_digest,
    read_bounded,
    remove_tree,
    require_exact_object,
    require_sha256,
    run_bounded,
    safe_child,
    sha256_bytes,
    sha256_file,
    source_tree_digest,
)


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
LAUNCHER_DIR = BASE.parent / "codeagent-launcher"
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))
import host_budget

MAX_COMPILER_WAIT_SECONDS = 120
CONTRACT = REPO / "wit/experimental-v0/contract.wit"
DEFAULT_TOOL_LAYER = (
    REPO
    / "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980"
)
MAX_HANDOFF_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_COMPONENT_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
BUILDER_VERSION = "app-builder.experimental-v1"
XCODE_CLANG = Path(
    "/Applications/Xcode.app/Contents/Developer/Toolchains/"
    "XcodeDefault.xctoolchain/usr/bin/clang"
)
XCODE_SDK = Path(
    "/Applications/Xcode.app/Contents/Developer/Platforms/"
    "MacOSX.platform/Developer/SDKs/MacOSX.sdk"
)


WORLD_IMPORTS = {
    "ui-only-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
    ],
    "service-only-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
        "vibapp:experimental-v0/system-metrics@0.0.1",
        "vibapp:experimental-v0/http@0.0.1",
    ],
    "hybrid-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/notification@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
    ],
    "web-preview-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
        "vibapp:experimental-v0/system-metrics@0.0.1",
        "vibapp:experimental-v0/http@0.0.1",
    ],
}
WORLD_KIND = {
    "ui-only-reference": "ui",
    "service-only-reference": "service",
    "hybrid-reference": "hybrid",
}
SERVICE_TRIGGER_ORDER = ("on-enable", "scheduler", "manual")


@dataclass(frozen=True)
class ValidatedHandoff:
    document: dict[str, Any]
    handoff_path: Path
    source_root: Path
    records: list[dict[str, Any]]


@dataclass(frozen=True)
class RunnerResult:
    component_bytes: bytes
    cargo_lock_bytes: bytes
    stdout: bytes
    stderr: bytes
    execution_mode: str
    runner_identity_sha256: str
    command: list[str]
    tool_versions: dict[str, str]
    isolation: dict[str, Any]
    resource_observations: dict[str, int]
    cache_acceptance_sha256: str | None


class BuilderRunner(Protocol):
    def preflight(self) -> dict[str, Any]: ...

    def execute(self, source_root: Path, workspace: Path, limits: ProcessLimits) -> RunnerResult: ...


class SafeFixtureRunner:
    """In-process inert runner used only by tests with already-safe component bytes."""

    def __init__(self, component_bytes: bytes, *, fabricated_success: bool = False):
        self.component_bytes = bytes(component_bytes)
        self.fabricated_success = fabricated_success

    def preflight(self) -> dict[str, Any]:
        if not self.component_bytes or len(self.component_bytes) > MAX_COMPONENT_BYTES:
            raise PipelineError("resource-limit", "safe fixture component is not bounded")
        return {
            "status": "ready",
            "compile_authority": "inert-test-fixture",
            "verify_authority": "independent-verifier",
            "network": "not-used",
            "source_executed": False,
            "production_isolation": False,
        }

    def execute(self, source_root: Path, workspace: Path, limits: ProcessLimits) -> RunnerResult:
        del source_root, workspace
        if not self.component_bytes or len(self.component_bytes) > MAX_COMPONENT_BYTES:
            raise PipelineError("resource-limit", "safe fixture component is not bounded")
        identity = sha256_bytes(b"vibapp-safe-fixture-runner-v1")
        stdout = b"repository says tests passed\n" if self.fabricated_success else b"safe fixture runner\n"
        return RunnerResult(
            component_bytes=self.component_bytes,
            cargo_lock_bytes=b"# safe bounded fixture lock\nversion = 4\n",
            stdout=stdout,
            stderr=b"",
            execution_mode="safe-test-fixture-no-source-execution",
            runner_identity_sha256=identity,
            command=["safe-fixture-runner"],
            tool_versions={"fixture": "1"},
            isolation={
                "claim": "inert-test-fixture",
                "network": "not-used",
                "home": "not-used",
                "docker_socket": "not-used",
                "source_execution_observed": False,
                "production_isolation": False,
            },
            resource_observations={
                "peak_rss_bytes": 0,
                "peak_pids": 1,
                "disk_bytes": len(self.component_bytes),
                "elapsed_ms": 0,
            },
            cache_acceptance_sha256=None,
        )


class MacSandboxCargoRunner:
    """Explicit local product implementation runner; it never claims VM/container-grade isolation."""

    def __init__(
        self,
        tool_layer: Path,
        cargo_home: Path,
        cache_acceptance: Path,
        builder_input_root: Path | None = None,
    ):
        self.tool_layer = tool_layer.resolve(strict=True)
        self.cargo_home = cargo_home.resolve(strict=True)
        self.cache_acceptance = cache_acceptance.resolve(strict=True)
        configured_root = builder_input_root if builder_input_root is not None else REPO / "generated"
        try:
            root_metadata = configured_root.lstat()
        except OSError as error:
            raise PipelineError(
                "isolation-unavailable",
                "Builder input root is unavailable",
            ) from error
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            raise PipelineError(
                "isolation-unavailable",
                "Builder input root must be a real directory",
            )
        self.builder_input_root = configured_root.resolve(strict=True)

    def _validate_inputs(self) -> tuple[dict[str, Any], str]:
        if not Path("/usr/bin/sandbox-exec").is_file():
            raise PipelineError("isolation-unavailable", "macOS sandbox-exec is unavailable")
        accepted_root = self.builder_input_root
        try:
            root_metadata = accepted_root.lstat()
            resolved_root = accepted_root.resolve(strict=True)
        except OSError as error:
            raise PipelineError(
                "isolation-unavailable",
                "Builder input root is unavailable at execution preflight",
            ) from error
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or resolved_root != accepted_root
        ):
            raise PipelineError(
                "isolation-unavailable",
                "Builder input root changed after configuration",
            )
        for path, context in (
            (self.tool_layer, "tool layer"),
            (self.cargo_home, "Cargo cache"),
            (self.cache_acceptance, "cache acceptance"),
        ):
            if path != accepted_root and accepted_root not in path.parents:
                raise PipelineError(
                    "isolation-unavailable",
                    f"{context} must stay below the explicit Builder input root",
                )
        acceptance = load_json(self.cache_acceptance, MAX_JSON_BYTES, "cache acceptance")
        require_exact_object(
            acceptance,
            {
                "schema_version",
                "state",
                "cargo_home",
                "network",
                "read_only",
                "allowed_proc_macros",
                "accepted_by",
            },
            "cache acceptance",
        )
        if (
            acceptance["schema_version"] != "vibapp.offline-cache-acceptance.experimental-v1"
            or acceptance["state"] != "independently-accepted"
            or acceptance["network"] != "none"
            or acceptance["read_only"] is not True
            or not isinstance(acceptance["accepted_by"], str)
            or acceptance["accepted_by"] == "builder"
        ):
            raise PipelineError("isolation-unavailable", "offline cache lacks independent acceptance")
        if Path(acceptance["cargo_home"]).resolve(strict=True) != self.cargo_home:
            raise PipelineError("integrity-failure", "cache acceptance binds another Cargo home")
        macros = acceptance["allowed_proc_macros"]
        if not isinstance(macros, list) or not macros:
            raise PipelineError("isolation-unavailable", "accepted proc-macro inventory is absent")
        acceptance_digest = sha256_file(
            self.cache_acceptance, MAX_JSON_BYTES, "cache acceptance"
        )
        return acceptance, acceptance_digest

    def preflight(self) -> dict[str, Any]:
        """Validate immutable local Builder inputs without compiling guest source."""
        acceptance, acceptance_digest = self._validate_inputs()
        tools: dict[str, str] = {}
        for path, context, maximum in (
            (self.tool_layer / "toolchain/bin/cargo", "Cargo", 100 * 1024 * 1024),
            (self.tool_layer / "toolchain/bin/rustc", "Rustc", 100 * 1024 * 1024),
            (XCODE_CLANG, "Xcode clang", 160 * 1024 * 1024),
        ):
            metadata = lstat_regular(path, maximum, context)
            if not metadata.st_mode & 0o111:
                raise PipelineError("integrity-failure", f"{context} is not executable")
            tools[context.lower().replace(" ", "_")] = str(path.resolve(strict=True))
        lstat_regular(XCODE_SDK / "SDKSettings.json", MAX_JSON_BYTES, "Xcode SDK settings")
        return {
            "status": "ready",
            "compile_authority": "isolated-builder",
            "verify_authority": "independent-verifier",
            "network": "none",
            "source_executed": False,
            "sandbox": "/usr/bin/sandbox-exec",
            "builder_input_root": str(self.builder_input_root),
            "tool_layer": str(self.tool_layer),
            "cargo_home": str(self.cargo_home),
            "cache_acceptance": str(self.cache_acceptance),
            "cache_acceptance_sha256": acceptance_digest,
            "cache_accepted_by": acceptance["accepted_by"],
            "maximum_jobs_per_host_user": 1,
            "maximum_compiler_wait_seconds": MAX_COMPILER_WAIT_SECONDS,
            "host_budget_sha256": sha256_file(Path(host_budget.__file__), MAX_JSON_BYTES, "host budget policy"),
            "tools": tools,
        }

    @staticmethod
    def _sandbox_profile(read_roots: list[Path], write_root: Path) -> str:
        quoted_reads = "\n".join(
            f"(allow file-read* (subpath {json.dumps(str(path))}))" for path in read_roots
        )
        return f"""(version 1)
(deny default)
(deny network*)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata)
(allow file-read* (literal \"/\"))
(allow file-read* (subpath \"/System\"))
(allow file-read* (subpath \"/Library/Developer/CommandLineTools\"))
(allow file-read* (subpath \"/Applications/Xcode.app/Contents\"))
(allow file-read* (subpath \"/usr/lib\"))
(allow file-read* (subpath \"/usr/share\"))
(allow file-read* (literal \"/private/etc/ssl/openssl.cnf\"))
(allow file-read* (literal \"/dev/null\"))
(allow file-read* (literal \"/dev/urandom\"))
{quoted_reads}
(allow file-write* (subpath {json.dumps(str(write_root))}))
"""

    def execute(self, source_root: Path, workspace: Path, limits: ProcessLimits, *, cancellation=None) -> RunnerResult:
        del workspace
        if (type(limits.wall_seconds) not in (int, float)
                or not math.isfinite(limits.wall_seconds) or limits.wall_seconds <= 0):
            raise PipelineError("resource-limit", "compiler wall time must be a positive finite bound")
        started = time.monotonic()
        deadline = started + limits.wall_seconds
        try:
            # Source probes and final handoff builds enter this same lease,
            # including callers running in separate Desktop/Web/CLI processes.
            with host_budget.lease("compiler", cancellation=cancellation,
                                   wait_seconds=min(MAX_COMPILER_WAIT_SECONDS, limits.wall_seconds)):
                self._remaining_limits(limits, deadline, cancellation)
                execution_workspace = Path(tempfile.mkdtemp(prefix="vibapp-mac-sandbox-runner-")).resolve(strict=True)
                execution_workspace.chmod(0o700)
                try:
                    result = self._execute_in_workspace(source_root, execution_workspace,
                                                        self._remaining_limits(limits, deadline, cancellation),
                                                        cancellation=cancellation)
                    self._remaining_limits(limits, deadline, cancellation)
                    return replace(result, resource_observations={
                        **result.resource_observations,
                        "elapsed_ms": max(result.resource_observations["elapsed_ms"], int((time.monotonic() - started) * 1000)),
                    })
                finally:
                    remove_tree(execution_workspace)
        except host_budget.BudgetError as error:
            code = "cancelled" if error.code == "provider-cancelled" else error.code
            raise PipelineError(code, str(error)) from error

    @staticmethod
    def _remaining_limits(limits: ProcessLimits, deadline: float, cancellation=None) -> ProcessLimits:
        if cancellation is not None and cancellation.is_set():
            raise PipelineError("cancelled", "Builder operation was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PipelineError("timeout", "compiler operation exhausted its wall-time budget")
        return replace(limits, wall_seconds=remaining)

    def _execute_in_workspace(
        self, source_root: Path, workspace: Path, limits: ProcessLimits, *, cancellation=None
    ) -> RunnerResult:
        deadline = time.monotonic() + limits.wall_seconds
        builder_preflight = self.preflight()
        acceptance_digest = builder_preflight["cache_acceptance_sha256"]
        sdk_settings = XCODE_SDK / "SDKSettings.json"
        cargo = self.tool_layer / "toolchain/bin/cargo"
        rustc = self.tool_layer / "toolchain/bin/rustc"
        project = workspace / "project"
        project.mkdir(mode=0o700)
        for source in sorted(source_root.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(source_root)
            destination = project / relative
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            exclusive_copy(source, destination, MAX_SOURCE_FILE_BYTES)
            destination.chmod(0o600)
        synthetic_home = workspace / "synthetic-home"
        temporary = workspace / "tmp"
        target = workspace / "target"
        synthetic_home.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
        target.mkdir(mode=0o700)
        sandbox_profile = workspace / "sandbox.sb"
        profile = self._sandbox_profile(
            [
                self.tool_layer,
                self.cargo_home,
                workspace,
                project,
                sandbox_profile,
                Path("/bin"),
                Path("/usr/bin"),
            ],
            workspace,
        )
        exclusive_write(sandbox_profile, profile.encode("utf-8"), mode=0o400)
        environment = {
            "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER": str(XCODE_CLANG),
            "CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER": str(XCODE_CLANG),
            "PATH": f"{self.tool_layer / 'toolchain/bin'}:/usr/bin:/bin",
            "HOME": str(synthetic_home),
            "CARGO_HOME": str(self.cargo_home),
            "CARGO_TARGET_DIR": str(target),
            "CARGO_INCREMENTAL": "0",
            "CARGO_NET_OFFLINE": "true",
            "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer",
            "RUSTUP_AUTO_INSTALL": "0",
            "SDKROOT": str(XCODE_SDK),
            "SOURCE_DATE_EPOCH": "1787796000",
            "TMPDIR": str(temporary),
            "TZ": "UTC",
            "LANG": "C",
            "LC_ALL": "C",
            "RUSTC": str(rustc),
            "RUSTFLAGS": f"--remap-path-prefix={project}=/workspace",
        }
        prefix = ["/usr/bin/sandbox-exec", "-f", str(sandbox_profile), "/usr/bin/env", "-i"]
        env_args = [f"{key}={value}" for key, value in sorted(environment.items())]
        generate = prefix + env_args + [str(cargo), "generate-lockfile", "--offline"]
        generated = run_bounded(
            generate, cwd=project, environment={"PATH": "/usr/bin:/bin"},
            limits=self._remaining_limits(limits, deadline, cancellation), disk_root=workspace, cancellation=cancellation
        )
        build_command = prefix + env_args + [
            str(cargo),
            "build",
            "--release",
            "--target",
            "wasm32-wasip2",
            "--locked",
            "--offline",
            "--jobs",
            "1",
            "--verbose",
        ]
        built = run_bounded(
            build_command,
            cwd=project,
            environment={"PATH": "/usr/bin:/bin"},
            limits=self._remaining_limits(limits, deadline, cancellation),
            disk_root=workspace,
            cancellation=cancellation,
        )
        cargo_document = tomllib.loads((project / "Cargo.toml").read_text(encoding="utf-8"))
        crate_name = cargo_document["package"]["name"].replace("-", "_")
        component = target / "wasm32-wasip2/release" / f"{crate_name}.wasm"
        component_bytes = read_bounded(component, MAX_COMPONENT_BYTES, "compiled component")
        lock_bytes = read_bounded(project / "Cargo.lock", MAX_JSON_BYTES, "generated Cargo.lock")
        return RunnerResult(
            component_bytes=component_bytes,
            cargo_lock_bytes=lock_bytes,
            stdout=generated.stdout + built.stdout,
            stderr=generated.stderr + built.stderr,
            execution_mode="local-macos-sandbox-experimental",
            runner_identity_sha256=sha256_bytes(
                canonical_json(
                    {
                        "version": BUILDER_VERSION,
                        "cargo_sha256": sha256_file(cargo, 100 * 1024 * 1024, "Cargo"),
                        "rustc_sha256": sha256_file(rustc, 100 * 1024 * 1024, "Rustc"),
                        "xcode_clang_sha256": sha256_file(
                            XCODE_CLANG, 160 * 1024 * 1024, "Xcode clang"
                        ),
                        "xcode_sdk_settings_sha256": sha256_file(
                            sdk_settings, MAX_JSON_BYTES, "Xcode SDK settings"
                        ),
                        "cache_acceptance_sha256": acceptance_digest,
                        "host_budget_sha256": builder_preflight["host_budget_sha256"],
                        "maximum_compiler_wait_seconds": MAX_COMPILER_WAIT_SECONDS,
                        "sandbox_profile_sha256": sha256_bytes(profile.encode("utf-8")),
                    }
                )
            ),
            command=build_command,
            tool_versions={
                "rust": "1.93.0",
                "cargo": "1.93.0",
                "target": "wasm32-wasip2",
                "host_linker": "xcode-clang-byte-bound",
                "host_sdk": "xcode-sdk-settings-byte-bound",
            },
            isolation={
                "claim": "local-macos-process-experimental",
                "network": "sandbox-denied",
                "home": "synthetic-empty",
                "docker_socket": "not-mounted",
                "source_execution_observed": True,
                "production_isolation": False,
            },
            resource_observations={
                "peak_rss_bytes": max(generated.peak_rss_bytes, built.peak_rss_bytes),
                "peak_pids": max(generated.peak_pids, built.peak_pids),
                "disk_bytes": max(generated.disk_bytes, built.disk_bytes),
                "elapsed_ms": generated.elapsed_ms + built.elapsed_ms,
            },
            cache_acceptance_sha256=acceptance_digest,
        )


def _cargo_package_name(app_id: str) -> str:
    if not isinstance(app_id, str) or not APP_ID_RE.fullmatch(app_id):
        raise PipelineError("source-invalid", "package_intent.app_id is not a VibApp identifier")
    candidate = app_id.replace(".", "_")
    if len(candidate) <= 64:
        return candidate
    return f"{candidate[:51]}-{sha256_bytes(app_id.encode('ascii'))[:12]}"


def _validate_cargo_manifest(path: Path, package_intent: dict[str, Any] | None = None) -> None:
    try:
        document = tomllib.loads(read_bounded(path, MAX_SOURCE_FILE_BYTES, "Cargo.toml").decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PipelineError("source-invalid", f"Cargo.toml is invalid: {error}") from error
    require_exact_object(document, {"package", "lib", "dependencies"}, "Cargo.toml")
    package = require_exact_object(
        document["package"],
        {
            "name",
            "version",
            "edition",
            "publish",
            "autobins",
            "autoexamples",
            "autotests",
            "autobenches",
        },
        "Cargo.toml.package",
    )
    if (
        not isinstance(package["name"], str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", package["name"])
        or not SEMVER_RE.fullmatch(str(package["version"]))
        or package["edition"] != "2024"
        or package["publish"] is not False
        or any(package[key] is not False for key in ("autobins", "autoexamples", "autotests", "autobenches"))
    ):
        raise PipelineError("source-invalid", "Cargo package policy differs from CodeAgent v1")
    if package_intent is not None and (
        package["name"] != _cargo_package_name(package_intent.get("app_id"))
        or package["version"] != package_intent.get("version")
    ):
        raise PipelineError(
            "source-invalid",
            "Cargo package name/version do not match package_intent",
        )
    library = require_exact_object(document["lib"], {"crate-type"}, "Cargo.toml.lib")
    if library["crate-type"] != ["cdylib"]:
        raise PipelineError("source-invalid", "Cargo library must be one cdylib")
    dependencies = require_exact_object(document["dependencies"], {"wit-bindgen"}, "Cargo.toml.dependencies")
    wit_bindgen = require_exact_object(
        dependencies["wit-bindgen"], {"version", "default-features", "features"}, "wit-bindgen"
    )
    if (
        wit_bindgen["version"] != "=0.60.0"
        or wit_bindgen["default-features"] is not False
        or wit_bindgen["features"] != ["bitflags", "macro-string", "macros", "realloc"]
    ):
        raise PipelineError("source-invalid", "wit-bindgen policy differs from the accepted source handoff")
    rust_source = path.parent / "src/lib.rs"
    if not rust_source.is_file() or rust_source.is_symlink():
        raise PipelineError("source-invalid", "Cargo source must contain regular src/lib.rs")
    try:
        source_text = read_bounded(
            rust_source, MAX_SOURCE_FILE_BYTES, rust_source.name
        ).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PipelineError("source-invalid", "src/lib.rs must be UTF-8") from error
    if (
        "#![no_std]" not in source_text
        or "extern crate alloc" not in source_text
        or "#[global_allocator]" not in source_text
        or "#[panic_handler]" not in source_text
        or "extern crate std" in source_text
        or "std::" in source_text
    ):
        raise PipelineError(
            "source-invalid",
            "CodeAgent Rust must be no_std/alloc with an explicit allocator and panic handler",
        )


def validate_handoff(handoff_path: Path) -> ValidatedHandoff:
    handoff_path = handoff_path.resolve(strict=True)
    document = load_json(handoff_path, MAX_HANDOFF_BYTES, "source handoff")
    require_exact_object(
        document,
        {
            "schema_version",
            "document_type",
            "status",
            "job_id",
            "need_spec_digest_sha256",
            "source_tree_sha256",
            "source_directory",
            "files",
            "package_intent",
            "target",
            "provider_execution",
            "authority",
            "created_at_utc",
        },
        "source handoff",
    )
    if (
        document["schema_version"] != "vibapp.codeagent-source-handoff.experimental-v2"
        or document["document_type"] != "codeagent-source-handoff"
        or document["status"] != "untrusted-source-awaiting-builder"
        or document["source_directory"] != "source"
    ):
        raise PipelineError("schema-invalid", "source handoff constants are unsupported")
    if not isinstance(document["job_id"], str) or not IDENTIFIER_RE.fullmatch(document["job_id"]):
        raise PipelineError("schema-invalid", "job_id is invalid")
    require_sha256(document["need_spec_digest_sha256"], "need_spec_digest_sha256")
    require_sha256(document["source_tree_sha256"], "source_tree_sha256")
    authority = require_exact_object(
        document["authority"], {"compile", "verify", "install", "publish"}, "authority"
    )
    if authority != {
        "compile": "separate-builder",
        "verify": "separate-verifier",
        "install": "none",
        "publish": "none",
    }:
        raise PipelineError("authority-invalid", "CodeAgent handoff grants forbidden authority")
    provider = require_exact_object(
        document["provider_execution"],
        {
            "mode",
            "adapter",
            "provider_id",
            "external_request_attempted",
            "external_request_observed",
            "gateway_request_id",
            "executable_sha256",
            "isolation_policy_version",
            "runner_id",
            "runner_identity_sha256",
            "isolation_policy_sha256",
            "receipt_digest_sha256",
        },
        "provider_execution",
    )
    if provider["mode"] not in {"dry-run-fixture", "live-explicit-opt-in", "direct-local-explicit-opt-in", "docker-local-explicit-opt-in"}:
        raise PipelineError("schema-invalid", "unknown CodeAgent provider mode")
    direct_provider_ids = {"codex", "claude-code", "opencode", "gemini-cli"}
    nullable_attestation = {
        "gateway_request_id",
        "isolation_policy_version",
        "runner_id",
        "runner_identity_sha256",
        "isolation_policy_sha256",
        "receipt_digest_sha256",
    }
    if provider["mode"] == "dry-run-fixture":
        expected = {
            "mode": "dry-run-fixture",
            "adapter": "local-static-fixture",
            "provider_id": "static-fixture",
            "external_request_attempted": False,
            "external_request_observed": False,
            "gateway_request_id": None,
            "executable_sha256": None,
            "isolation_policy_version": None,
            "runner_id": None,
            "runner_identity_sha256": None,
            "isolation_policy_sha256": None,
            "receipt_digest_sha256": None,
        }
        if provider != expected:
            raise PipelineError("schema-invalid", "dry-run provider execution is not an exact static fixture")
    elif provider["mode"] == "direct-local-explicit-opt-in":
        if (
            provider["adapter"] != "local-codeagent-adapter"
            or provider["provider_id"] not in direct_provider_ids
            or provider["external_request_attempted"] is not True
            or provider["external_request_observed"] is not False
            or any(provider[key] is not None for key in nullable_attestation)
        ):
            raise PipelineError("schema-invalid", "direct provider execution metadata is inconsistent")
        require_sha256(provider["executable_sha256"], "provider_execution.executable_sha256")
    elif provider["mode"] == "docker-local-explicit-opt-in":
        if (provider["adapter"] != "docker-codeagent-launcher" or provider["provider_id"] not in {"codex", "opencode"}
            or provider["external_request_attempted"] is not True or provider["external_request_observed"] is not True
            or provider["isolation_policy_version"] != "vibapp.docker-source-v1"
            or not isinstance(provider["runner_id"], str) or not IDENTIFIER_RE.fullmatch(provider["runner_id"])
            or provider["gateway_request_id"] != provider["runner_id"]):
            raise PipelineError("schema-invalid", "Docker provider execution metadata is inconsistent")
        for key in ("executable_sha256", "runner_identity_sha256", "isolation_policy_sha256", "receipt_digest_sha256"):
            require_sha256(provider[key], f"provider_execution.{key}")
    else:
        if (
            provider["adapter"] != "external-isolated-provider-runner"
            or provider["provider_id"] != "codex"
            or provider["external_request_attempted"] is not True
            or provider["external_request_observed"] is not True
            or provider["isolation_policy_version"]
            != "vibapp.provider-runner-isolation.experimental-v1"
            or not isinstance(provider["gateway_request_id"], str)
            or not IDENTIFIER_RE.fullmatch(provider["gateway_request_id"])
            or not isinstance(provider["runner_id"], str)
            or not IDENTIFIER_RE.fullmatch(provider["runner_id"])
        ):
            raise PipelineError("schema-invalid", "isolated provider execution metadata is inconsistent")
        for key in (
            "executable_sha256",
            "runner_identity_sha256",
            "isolation_policy_sha256",
            "receipt_digest_sha256",
        ):
            require_sha256(provider[key], f"provider_execution.{key}")
    package = require_exact_object(
        document["package_intent"],
        {"app_id", "version", "display_name", "description", "entrypoints"},
        "package_intent",
    )
    if not isinstance(package["app_id"], str) or not APP_ID_RE.fullmatch(package["app_id"]):
        raise PipelineError("schema-invalid", "package app_id is invalid")
    if not isinstance(package["version"], str) or not SEMVER_RE.fullmatch(package["version"]):
        raise PipelineError("schema-invalid", "package version is invalid")
    for key, limit in (("display_name", 80), ("description", 1000)):
        if not isinstance(package[key], str) or not package[key] or len(package[key].encode("utf-8")) > limit:
            raise PipelineError("schema-invalid", f"package {key} is invalid")
    entrypoints = package["entrypoints"]
    if not isinstance(entrypoints, list) or not 1 <= len(entrypoints) <= 16:
        raise PipelineError("schema-invalid", "package entrypoints are invalid")
    seen_entrypoints: set[str] = set()
    kinds: set[str] = set()
    for index, entrypoint in enumerate(entrypoints):
        entrypoint_fields = {"id", "kind", "label", "initial_route"}
        if isinstance(entrypoint, dict) and "triggers" in entrypoint:
            entrypoint_fields.add("triggers")
        row = require_exact_object(entrypoint, entrypoint_fields, f"entrypoint[{index}]")
        if not isinstance(row["id"], str) or not IDENTIFIER_RE.fullmatch(row["id"]) or row["id"] in seen_entrypoints:
            raise PipelineError("schema-invalid", "entrypoint ID is invalid or duplicate")
        if row["kind"] not in {"launcher-ui", "service", "settings"}:
            raise PipelineError("schema-invalid", "entrypoint kind is invalid")
        if not isinstance(row["label"], str) or not row["label"] or len(row["label"].encode("utf-8")) > 80:
            raise PipelineError("schema-invalid", "entrypoint label is invalid")
        if row["kind"] == "launcher-ui" and not isinstance(row["initial_route"], str):
            raise PipelineError("schema-invalid", "launcher entrypoint requires initial_route")
        if row["kind"] != "launcher-ui" and row["initial_route"] is not None:
            raise PipelineError("schema-invalid", "non-launcher initial_route must be null")
        if row["kind"] == "service":
            triggers = row.get("triggers", ["manual"])
            if (
                not isinstance(triggers, list)
                or not triggers
                or len(triggers) > len(SERVICE_TRIGGER_ORDER)
                or triggers
                != [trigger for trigger in SERVICE_TRIGGER_ORDER if trigger in triggers]
            ):
                raise PipelineError(
                    "schema-invalid",
                    "service triggers must be a canonical non-empty subset",
                )
        elif "triggers" in row:
            raise PipelineError(
                "schema-invalid",
                "only service entrypoints may declare triggers",
            )
        seen_entrypoints.add(row["id"])
        kinds.add(row["kind"])
    target = require_exact_object(
        document["target"],
        {
            "contract",
            "wasi",
            "rust_target",
            "wit_world",
            "app_kind",
            "profiles",
            "required_imports",
            "required_capabilities",
        },
        "target",
    )
    if (
        target["contract"] != "vibapp:experimental-v0@0.0.1"
        or target["wasi"] != "0.2"
        or target["rust_target"] != "wasm32-wasip2"
        or target["wit_world"] not in WORLD_IMPORTS
        or target["required_imports"] != WORLD_IMPORTS[target["wit_world"]]
    ):
        raise PipelineError("incompatible-contract", "target contract/world/imports are not exact")
    if target["wit_world"] == "web-preview-reference":
        raise PipelineError("incompatible-contract", "web-preview world has no canonical product build route")
    if target["app_kind"] != WORLD_KIND[target["wit_world"]]:
        raise PipelineError("incompatible-contract", "target world and app kind disagree")
    required_capabilities = target["required_capabilities"]
    if (
        not isinstance(required_capabilities, list)
        or any(not isinstance(interface, str) for interface in required_capabilities)
        or len(set(required_capabilities)) != len(required_capabilities)
        or any(interface not in target["required_imports"] for interface in required_capabilities)
    ):
        raise PipelineError(
            "incompatible-contract",
            "target required capabilities must be a unique subset of structural imports",
        )
    profiles = target["profiles"]
    if not isinstance(profiles, list) or not profiles or len(set(profiles)) != len(profiles):
        raise PipelineError("schema-invalid", "target profiles are invalid")
    if any(profile not in {"desktop", "headless"} for profile in profiles):
        raise PipelineError(
            "browser-derivation-unavailable",
            "canonical Builder cannot claim a browser profile before trusted derivation",
        )
    if target["app_kind"] == "ui" and ("launcher-ui" not in kinds or "service" in kinds):
        raise PipelineError("incompatible-contract", "UI entrypoints disagree with app kind")
    if target["app_kind"] == "service" and ("service" not in kinds or "launcher-ui" in kinds):
        raise PipelineError("incompatible-contract", "service entrypoints disagree with app kind")
    if target["app_kind"] == "hybrid" and not {"launcher-ui", "service"} <= kinds:
        raise PipelineError("incompatible-contract", "hybrid entrypoints are incomplete")

    source_root = safe_child(handoff_path.parent, "source", "source directory")
    source_metadata = source_root.lstat()
    if not stat.S_ISDIR(source_metadata.st_mode) or stat.S_ISLNK(source_metadata.st_mode):
        raise PipelineError("integrity-failure", "source directory must be a real directory")
    files = document["files"]
    if not isinstance(files, list) or not 2 <= len(files) <= 256:
        raise PipelineError("schema-invalid", "source handoff file inventory is invalid")
    records: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total = 0
    for index, item in enumerate(files):
        record = require_exact_object(item, {"path", "sha256", "size_bytes", "role"}, f"files[{index}]")
        relative = normalized_relative_path(record["path"], f"files[{index}].path")
        if relative in seen_paths:
            raise PipelineError("schema-invalid", "source inventory has a duplicate path")
        require_sha256(record["sha256"], f"files[{index}].sha256")
        if not isinstance(record["size_bytes"], int) or not 1 <= record["size_bytes"] <= MAX_SOURCE_FILE_BYTES:
            raise PipelineError("resource-limit", "source file size is outside limits")
        if record["role"] not in {"generated-source", "authoritative-contract-copy"}:
            raise PipelineError("schema-invalid", "source file role is invalid")
        source_path = safe_child(source_root, relative, f"source file {relative}")
        metadata = lstat_regular(source_path, MAX_SOURCE_FILE_BYTES, f"source file {relative}")
        if metadata.st_mode & 0o111:
            raise PipelineError("source-invalid", f"source file is executable: {relative}")
        if metadata.st_size != record["size_bytes"]:
            raise PipelineError("integrity-failure", f"source size mismatch: {relative}")
        if sha256_file(source_path, MAX_SOURCE_FILE_BYTES, relative) != record["sha256"]:
            raise PipelineError("integrity-failure", f"source digest mismatch: {relative}")
        if relative == "wit/contract.wit":
            if record["role"] != "authoritative-contract-copy":
                raise PipelineError("source-invalid", "WIT copy has the wrong role")
            if read_bounded(source_path, MAX_SOURCE_FILE_BYTES, relative) != read_bounded(
                CONTRACT, MAX_SOURCE_FILE_BYTES, "authoritative contract"
            ):
                raise PipelineError("integrity-failure", "source WIT differs from the authoritative contract")
        elif record["role"] != "generated-source":
            raise PipelineError("source-invalid", f"non-contract file has an authority role: {relative}")
        pure = PurePosixPath(relative)
        allowed = relative in {"Cargo.toml", "README.md", "manifest.intent.json", "wit/contract.wit"} or (
            len(pure.parts) >= 2 and pure.parts[0] in {"src", "tests"} and pure.suffix == ".rs"
        )
        if not allowed or pure.name in {"build.rs", "Cargo.lock"}:
            raise PipelineError("source-invalid", f"source path is forbidden: {relative}")
        seen_paths.add(relative)
        total += metadata.st_size
        if total > MAX_SOURCE_BYTES:
            raise PipelineError("resource-limit", "source tree exceeds aggregate byte limit")
        records.append(dict(record))
    actual_files: set[str] = set()
    for root, directories, filenames in os.walk(source_root, topdown=True, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            directory_path = root_path / directory
            metadata = directory_path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise PipelineError("integrity-failure", "source tree contains an unsafe directory")
        for filename in filenames:
            actual_files.add((root_path / filename).relative_to(source_root).as_posix())
    if actual_files != seen_paths:
        raise PipelineError(
            "integrity-failure",
            f"source inventory mismatch missing={sorted(actual_files-seen_paths)} extra={sorted(seen_paths-actual_files)}",
        )
    records.sort(key=lambda row: row["path"].encode("utf-8"))
    if source_tree_digest(records) != document["source_tree_sha256"]:
        raise PipelineError("integrity-failure", "source tree digest does not match handoff")
    if "Cargo.toml" not in seen_paths or "wit/contract.wit" not in seen_paths or not any(
        path.startswith("src/") and path.endswith(".rs") for path in seen_paths
    ):
        raise PipelineError("source-invalid", "source requires Cargo.toml, WIT, and src Rust")
    _validate_cargo_manifest(source_root / "Cargo.toml", package)
    return ValidatedHandoff(document, handoff_path, source_root, records)


def _artifact(path: str, media_type: str, file_path: Path) -> dict[str, Any]:
    return {
        "path": path,
        "media_type": media_type,
        "sha256": sha256_file(file_path, 64 * 1024 * 1024, path),
        "size_bytes": file_path.stat().st_size,
    }


def _profile_row(kind: str, profile: str) -> dict[str, str]:
    if profile == "desktop":
        return {
            "profile": profile,
            "mode": "full",
            "background": "not-applicable" if kind == "ui" else "daemon",
            "artifact_role": "canonical-component",
            "degradation": "Local product candidate; verification and daemon admission remain separate.",
        }
    return {
        "profile": profile,
        "mode": "degraded",
        "background": "not-applicable" if kind == "ui" else "process",
        "artifact_role": "canonical-component",
        "degradation": "Headless management is available; launcher UI rendering is unsupported.",
    }


def _capability(
    interface: str,
    profiles: list[str],
    required_capabilities: set[str],
) -> dict[str, Any]:
    short = interface.split("/")[-1].split("@")[0]
    try:
        host_availability = LOCAL_HOST_CAPABILITY_AVAILABILITY[interface]
        grant = CAPABILITY_GRANT_POLICY[interface]
    except KeyError as error:
        raise PipelineError(
            "unknown-interface", f"Builder has no host capability policy for {interface}"
        ) from error
    necessity = "required" if interface in required_capabilities else "degradable"
    scope: dict[str, Any] = {}
    if short == "kv":
        scope = {"maximum_storage_bytes": 1_048_576}
    elif short == "scheduler":
        scope = {"maximum_schedules": 16}
    elif short == "system-metrics":
        scope = {"metrics": ["cpu-temperature-celsius"]}
    rows = []
    for profile in profiles:
        # Structural-only imports receive no live authority.  Their typed denied
        # stubs satisfy static Component linking without turning the fixed world
        # into an application requirement.
        availability = host_availability if necessity == "required" else "denied"
        if availability == "unavailable":
            behavior = (
                "Typed capability-unavailable stub; the current local host provides "
                "no live authority for this interface."
            )
        elif availability == "denied":
            behavior = (
                "Structural import only; the app did not request this capability, "
                "so the host links a typed permission-denied stub."
            )
        else:
            behavior = (
                "Bounded daemon broker; linking the interface does not bypass "
                "per-call authority checks."
            )
        rows.append({"profile": profile, "availability": availability, "behavior": behavior})
    return {
        "interface": interface,
        "necessity": necessity,
        "grant": grant,
        "reason": (
            f"{necessity.capitalize()} {short} capability; necessity comes from the "
            "consent-bound app requirement while the import comes from the selected WIT world."
        ),
        "scope": scope,
        "profiles": rows,
    }


def _entrypoint(row: dict[str, Any], profiles: list[str]) -> dict[str, Any]:
    base = {"id": row["id"], "kind": row["kind"], "label": row["label"], "profiles": profiles}
    if row["kind"] == "launcher-ui":
        base.update(
            {
                "routes": {"initial": row["initial_route"], "allowed": [row["initial_route"]]},
                "restoration": "route-only",
            }
        )
    elif row["kind"] == "service":
        base.update(
            {
                # Legacy handoffs did not carry trigger intent. Preserve their
                # repackage path without inventing automatic/scheduled behavior.
                "triggers": list(row.get("triggers", ["manual"])),
                "health_check_interval_seconds": 60,
            }
        )
    else:
        base.update({"schema_export": "get-settings-schema"})
    return base


def _consent_bound_entrypoints(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the exact handoff lifecycle intent into a verifier-readable binding."""
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "label": row["label"],
            "initial_route": row["initial_route"],
            # Keep a uniform shape. Legacy service handoffs conservatively mean
            # manual-only; non-service entrypoints cannot own service triggers.
            "triggers": (
                list(row.get("triggers", ["manual"]))
                if row["kind"] == "service"
                else []
            ),
        }
        for row in rows
    ]


def _manifest(
    handoff: ValidatedHandoff,
    runner: RunnerResult,
    package_dir: Path,
    provenance_path: Path,
    sbom_path: Path,
    publisher_id: str = "ai.vibapp.local",
) -> dict[str, Any]:
    document = handoff.document
    package = document["package_intent"]
    target = document["target"]
    profiles = list(target["profiles"])
    required_capabilities = set(target["required_capabilities"])
    platform_profiles = list(profiles)
    return {
        "schema_version": "vibapp.manifest.experimental-v0.0.1",
        "package_format": "vibapp.package.experimental-v0",
        "app": {
            "id": package["app_id"],
            "version": package["version"],
            "kind": target["app_kind"],
            "display_name": package["display_name"],
            "description": package["description"],
            "publisher": {"id": publisher_id, "display_name": "VibApp Local Builder" if publisher_id == "ai.vibapp.local" else publisher_id},
        },
        "artifacts": {
            "canonical_component": _artifact(
                "component.wasm", "application/wasm", package_dir / "component.wasm"
            ),
            "assets": [],
            "browser_derivations": [],
            "provenance": _artifact("provenance.json", "application/json", provenance_path),
            "sbom": _artifact(
                "sbom.cdx.json", "application/vnd.cyclonedx+json", sbom_path
            ),
        },
        "runtime": {
            "contract": target["contract"],
            "wasi": target["wasi"],
            "world": target["wit_world"],
            "required_imports": target["required_imports"],
            "profiles": [_profile_row(target["app_kind"], profile) for profile in profiles],
            "platforms": [{"os": "macos", "arch": "aarch64", "profiles": platform_profiles}],
        },
        "entrypoints": [_entrypoint(row, profiles) for row in package["entrypoints"]],
        "capabilities": [
            _capability(interface, profiles, required_capabilities)
            for interface in target["required_imports"]
        ],
        "resources": {
            "linear_memory_bytes": 16_777_216,
            "event_wall_time_ms": 100,
            "health_migration_wall_time_ms": 1000,
            "output_bytes": 131_072,
            "stored_data_bytes": 1_048_576,
            "durable_schedules": 0 if target["app_kind"] == "ui" else 16,
            "log_bytes_per_day": 131_072,
        },
        "state": {"schema": 1, "migratable_from_min": 1, "migratable_from_max": 1},
        "lifecycle": {
            "disable": {
                "retains_package": True,
                "retains_state": True,
                "stops_services": True,
                "cancels_schedules": True,
            },
            "uninstall": {
                "allowed_data_dispositions": ["delete", "retain", "export-then-delete"],
                "default_data_disposition": "retain" if target["app_kind"] != "ui" else "delete",
            },
        },
        "source": {
            "language": "rust",
            "revision": f"codeagent-{document['source_tree_sha256'][:24]}",
            "cargo_lock_sha256": sha256_bytes(runner.cargo_lock_bytes),
            "builder_image_digest": f"sha256:{runner.runner_identity_sha256}",
        },
        "license": {"spdx_expression": "Apache-2.0"},
        "privacy": {"stores_personal_data": False, "network_access": "none"},
        "verification": {"status": "unverified", "revocation": "not-revoked"},
    }


def build_handoff(
    handoff_path: Path,
    output_root: Path,
    runner: BuilderRunner,
    *,
    limits: ProcessLimits | None = None,
    publisher_id: str = "ai.vibapp.local",
    cancellation=None,
) -> Path:
    # Supplied by an authenticated trusted controller, never read from generated
    # source. Existing local-only callers retain their previous identity.
    if not isinstance(publisher_id, str) or len(publisher_id) > 128 or not APP_ID_RE.fullmatch(publisher_id):
        raise PipelineError("schema-invalid", "publisher ID is invalid")
    limits = limits or ProcessLimits()
    handoff = validate_handoff(handoff_path)
    output_root = ensure_private_directory(output_root)
    jobs = ensure_private_directory(output_root / "jobs")
    lock_path = output_root / ".builder.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PipelineError("busy", "another Builder job is active") from error
        final_job = jobs / handoff.document["job_id"]
        if final_job.exists() or final_job.is_symlink():
            raise PipelineError("conflict", "Builder job_id already exists")
        staging = Path(tempfile.mkdtemp(prefix=f".{handoff.document['job_id']}.", dir=jobs))
        staging.chmod(0o700)
        try:
            atomic_json(
                staging / "job.json",
                {
                    "schema_version": "vibapp.builder-job.experimental-v1",
                    "job_id": handoff.document["job_id"],
                    "state": "building",
                    "source_tree_sha256": handoff.document["source_tree_sha256"],
                    "limits": limits.as_dict(),
                    "started_at_utc": now_utc(),
                },
                mode=0o600,
            )
            copied_source = staging / "source"
            copied_source.mkdir(mode=0o700)
            for record in handoff.records:
                source = safe_child(handoff.source_root, record["path"], record["path"])
                destination = copied_source.joinpath(*PurePosixPath(record["path"]).parts)
                exclusive_copy(source, destination, MAX_SOURCE_FILE_BYTES)
            runner_workspace = staging / "runner-workspace"
            runner_workspace.mkdir(mode=0o700)
            try:
                if isinstance(runner, MacSandboxCargoRunner):
                    result = runner.execute(copied_source, runner_workspace, limits, cancellation=cancellation)
                else:
                    result = runner.execute(copied_source, runner_workspace, limits)
            except PipelineError as error:
                atomic_json(
                    staging / "job.json",
                    {
                        "schema_version": "vibapp.builder-job.experimental-v1",
                        "job_id": handoff.document["job_id"],
                        "state": "failed",
                        "source_tree_sha256": handoff.document["source_tree_sha256"],
                        "error": {"code": error.code, "message": str(error)[:4096]},
                        "limits": limits.as_dict(),
                        "finished_at_utc": now_utc(),
                    },
                    mode=0o600,
                )
                os.rename(staging, final_job)
                raise
            if len(result.component_bytes) == 0 or len(result.component_bytes) > MAX_COMPONENT_BYTES:
                raise PipelineError("resource-limit", "runner component output is not bounded")
            if len(result.stdout) > limits.stdout_bytes or len(result.stderr) > limits.stderr_bytes:
                raise PipelineError("resource-limit", "runner returned an oversized stream")
            logs = canonical_json(
                {
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                }
            )
            if len(logs) > MAX_LOG_BYTES:
                raise PipelineError("resource-limit", "structured build log exceeds limit")
            exclusive_write(staging / "build.log.json", logs + b"\n", mode=0o400)
            quarantine = staging / "quarantine"
            package_dir = quarantine / "package"
            package_dir.mkdir(parents=True, mode=0o700)
            exclusive_write(package_dir / "component.wasm", result.component_bytes, mode=0o400)
            provenance = {
                "schema_version": "vibapp.builder-provenance.experimental-v1",
                "scope": "local-product-prototype",
                "job_id": handoff.document["job_id"],
                "need_spec_digest_sha256": handoff.document["need_spec_digest_sha256"],
                "source_tree_sha256": handoff.document["source_tree_sha256"],
                "cargo_lock_sha256": sha256_bytes(result.cargo_lock_bytes),
                "contract_sha256": sha256_file(CONTRACT, MAX_SOURCE_FILE_BYTES, "contract"),
                "component_wasm_sha256": sha256_bytes(result.component_bytes),
                "builder": BUILDER_VERSION,
                "runner_identity_sha256": result.runner_identity_sha256,
                "execution_mode": result.execution_mode,
                "tool_versions": result.tool_versions,
                "command": result.command,
                "isolation": result.isolation,
                "limits": limits.as_dict(),
                "resource_observations": result.resource_observations,
                "cache_acceptance_sha256": result.cache_acceptance_sha256,
                "network_phase": "none",
                "finished_at_utc": now_utc(),
            }
            exclusive_write(
                package_dir / "provenance.json", canonical_json(provenance) + b"\n", mode=0o400
            )
            sbom = {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "version": 1,
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": handoff.document["package_intent"]["app_id"],
                        "version": handoff.document["package_intent"]["version"],
                    }
                },
                "components": [
                    {"type": "library", "name": "wit-bindgen", "version": "0.60.0"}
                ],
                "properties": [
                    {"name": "ai.vibapp.scope", "value": "local-product-prototype"},
                    {"name": "ai.vibapp.network-phase", "value": "none"},
                ],
            }
            exclusive_write(
                package_dir / "sbom.cdx.json", canonical_json(sbom) + b"\n", mode=0o400
            )
            manifest = _manifest(
                handoff,
                result,
                package_dir,
                package_dir / "provenance.json",
                package_dir / "sbom.cdx.json",
                publisher_id,
            )
            exclusive_write(
                package_dir / "manifest.json", canonical_json(manifest) + b"\n", mode=0o400
            )
            digest = package_digest(manifest)
            package_files = []
            for name in ("manifest.json", "component.wasm", "provenance.json", "sbom.cdx.json"):
                path = package_dir / name
                package_files.append(
                    {
                        "path": name,
                        "sha256": sha256_file(path, 64 * 1024 * 1024, name),
                        "size_bytes": path.stat().st_size,
                    }
                )
            receipt = {
                "schema_version": "vibapp.builder-quarantine.experimental-v2",
                "document_type": "builder-quarantine-receipt",
                "state": "quarantined-awaiting-verifier",
                "scope": "local-product-prototype",
                "job_id": handoff.document["job_id"],
                "need_spec_digest_sha256": handoff.document["need_spec_digest_sha256"],
                "source_tree_sha256": handoff.document["source_tree_sha256"],
                "required_capabilities": handoff.document["target"]["required_capabilities"],
                "package_entrypoints": _consent_bound_entrypoints(
                    handoff.document["package_intent"]["entrypoints"]
                ),
                "source_directory": "source",
                "package_directory": "package",
                "package_digest_sha256": digest,
                "package_files": package_files,
                "component_sha256": sha256_bytes(result.component_bytes),
                "manifest_sha256": sha256_file(
                    package_dir / "manifest.json", MAX_JSON_BYTES, "manifest"
                ),
                "presentation": derive_host_presentation(
                    manifest["app"]["kind"],
                    manifest["app"]["display_name"],
                    manifest["app"]["description"],
                ),
                "build_log_sha256": sha256_file(
                    staging / "build.log.json", MAX_LOG_BYTES, "build log"
                ),
                "builder": {
                    "version": BUILDER_VERSION,
                    "runner_identity_sha256": result.runner_identity_sha256,
                    "execution_mode": result.execution_mode,
                    "source_execution_observed": bool(
                        result.isolation.get("source_execution_observed", False)
                    ),
                    "production_isolation": bool(
                        result.isolation.get("production_isolation", False)
                    ),
                },
                "limits": limits.as_dict(),
                "terminal_outcome": "build-output-untrusted",
                "authority": {
                    "builder": "quarantine-only",
                    "verify": "independent-verifier",
                    "install": "none",
                    "publish": "none",
                },
                "created_at_utc": now_utc(),
            }
            atomic_json(quarantine / "quarantine-receipt.json", receipt, mode=0o400)
            atomic_json(
                staging / "job.json",
                {
                    "schema_version": "vibapp.builder-job.experimental-v1",
                    "job_id": handoff.document["job_id"],
                    "state": "quarantined-awaiting-verifier",
                    "source_tree_sha256": handoff.document["source_tree_sha256"],
                    "package_digest_sha256": digest,
                    "quarantine_receipt": "quarantine/quarantine-receipt.json",
                    "limits": limits.as_dict(),
                    "finished_at_utc": now_utc(),
                },
                mode=0o600,
            )
            remove_tree(runner_workspace)
            os.rename(staging, final_job)
            return final_job / "quarantine/quarantine-receipt.json"
        except Exception as error:
            if staging.exists() and not final_job.exists():
                code = error.code if isinstance(error, PipelineError) else "internal"
                try:
                    atomic_json(
                        staging / "job.json",
                        {
                            "schema_version": "vibapp.builder-job.experimental-v1",
                            "job_id": handoff.document["job_id"],
                            "state": "failed",
                            "source_tree_sha256": handoff.document["source_tree_sha256"],
                            "error": {"code": code, "message": str(error)[:4096]},
                            "limits": limits.as_dict(),
                            "finished_at_utc": now_utc(),
                        },
                        mode=0o600,
                    )
                    os.rename(staging, final_job)
                except Exception:
                    remove_tree(staging)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantine-only VibApp product Builder")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("handoff", type=Path)
    build_parser.add_argument("--output-root", type=Path, required=True)
    build_parser.add_argument("--builder-input-root", type=Path, default=REPO / "generated")
    build_parser.add_argument("--tool-layer", type=Path, default=DEFAULT_TOOL_LAYER)
    build_parser.add_argument("--cargo-home", type=Path, required=True)
    build_parser.add_argument("--cache-acceptance", type=Path, required=True)
    args = parser.parse_args()
    try:
        runner = MacSandboxCargoRunner(
            args.tool_layer,
            args.cargo_home,
            args.cache_acceptance,
            args.builder_input_root,
        )
        receipt = build_handoff(args.handoff, args.output_root, runner)
    except (PipelineError, OSError, ValueError) as error:
        code = error.code if isinstance(error, PipelineError) else "internal"
        print(f"BUILD_FAILED code={code} detail={str(error)[:4096]}", file=sys.stderr)
        return 1
    print(json.dumps({"state": "quarantined-awaiting-verifier", "receipt": str(receipt)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
