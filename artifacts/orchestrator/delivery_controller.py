#!/usr/bin/env python3
"""Durable no-match -> CodeAgent -> Builder -> Verifier -> private AppStore controller.

This product-layer controller composes existing authority-separated modules. Source
compilation, independent verification and isolated first-surface readiness remain
separate workers; it never publishes or installs into the user's runtime.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import signal
import sys
import threading
from typing import Any, Callable


BASE = Path(__file__).resolve().parent
ARTIFACTS = BASE.parent
REPO = ARTIFACTS.parent
BUILDER_DIR = ARTIFACTS / "app-builder"
REGISTRY_DIR = ARTIFACTS / "registry-store"
CODEAGENT_DIR = ARTIFACTS / "codeagent-adapter"
CLOUD_DIR = ARTIFACTS / "cloud-agent"
for dependency in (str(BASE), str(BUILDER_DIR), str(REGISTRY_DIR), str(ARTIFACTS / "codeagent-launcher")):
    if dependency not in sys.path:
        sys.path.insert(0, dependency)

from app_builder import (  # noqa: E402
    MacSandboxCargoRunner,
    PipelineError,
    SafeFixtureRunner,
    build_handoff,
    validate_handoff,
)
from common import ProcessLimits, sha256_file as builder_sha256_file  # noqa: E402
from local_appstore import LocalAppStore, StoreError  # noqa: E402
from verifier import (  # noqa: E402
    DEFAULT_WASM_TOOLS,
    WASM_TOOLS_SHA256,
    verifier_preflight,
    verify_and_promote,
)
from docker_executor import DockerError, validate_failure_diagnostic, validate_model_request_budget  # noqa: E402
from runtime_readiness import check_runtime_readiness, RuntimeReadinessError  # noqa: E402


SCHEMA_VERSION = "vibapp.delivery-controller.experimental-v2"
TASK_VERSION = "vibapp.delivery-task.experimental-v2"
ATTEMPT_VERSION = "vibapp.delivery-attempt.experimental-v2"
EVENT_VERSION = "vibapp.delivery-event.experimental-v1"
TERMINAL_STATUSES = {"private-appstore-ready", "failed"}
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_EVENT_BYTES = 16 * 1024
MAX_EVENT_LOG_BYTES = 1024 * 1024
MAX_ATTEMPTS = 64
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TASK_ID = re.compile(r"^development-[0-9a-f]{32}$")
ATTEMPT_ID = re.compile(r"^attempt-[0-9]{4}-[0-9a-f]{16}$")
UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
APP_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
SERVICE_TRIGGER_ORDER = ("on-enable", "scheduler", "manual")
TASK_PROVIDER_TO_ADAPTER = {
    "openai-codex": "codex",
    "anthropic-claude-code": "claude-code",
    "opencode": "opencode",
    "google-gemini-cli": "gemini-cli",
}
AUTHORITATIVE_REGISTRY_ROUTE_SCHEMA = "vibapp.registry-route.experimental.2026-08-24.1"
AUTHORITATIVE_REGISTRY_STATUS = "experimental-product-hold"
NON_RETRYABLE_FAILURE_MARKERS = (
    "admission",
    "authorization",
    "binding",
    "consent",
    "digest",
    "forged",
    "identity",
    "integrity",
    "invalid",
    "malformed",
    "mismatch",
    "path-escape",
    "replayed",
    "schema",
    "unsafe",
)
TRANSIENT_FAILURE_CODES = frozenset({
    "provider-timeout", "provider-rate-limited", "provider-network-error",
    "provider-upstream-unavailable",
    "local-capacity-busy", "builder-timeout", "verifier-timeout",
    "codeagent-interrupted", "provider-cancelled",
})
DIAGNOSTIC_FAILURE_CODES = frozenset({
    "provider-upstream-unavailable", "provider-rate-limited", "provider-authentication-failed",
    "provider-upstream-rejected", "provider-timeout", "provider-network-error",
    "provider-cancelled", "provider-request-budget-exhausted", "provider-request-limit",
    "provider-response-limit", "provider-output-invalid", "provider-output-limit",
    "provider-protocol-invalid", "provider-upstream-protocol-invalid", "provider-failed",
    "provider-container-failed", "docker-cleanup-unconfirmed", "docker-execution-failed",
})
DIAGNOSTIC_PHASES = frozenset({
    "validate", "connect", "send-request", "response-headers", "http-rejected",
    "response-body", "complete",
})
MAX_DIAGNOSTIC_BYTES = 32 * 1024


def _read_execution_diagnostic(root: Path, task_id: str, attempt_id: str, name: str):
    """Read only the exact owner-held attempt path; never follow any symlink.

    The presence bit keeps even malformed terminal evidence ahead of stale progress.
    These optional diagnostics cannot change delivery authority or fail status reads.
    """
    if (not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id)
            or not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id)
            or name not in {"docker-progress.json", "docker-terminal.json"}):
        return True, None
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        descriptors.append(descriptor)
        for part in ("tasks", task_id, "attempts", attempt_id, "codeagent", "output", "executions", attempt_id):
            if os.fstat(descriptor).st_uid != os.getuid():
                return True, None
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        if os.fstat(descriptor).st_uid != os.getuid():
            return True, None
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or not 1 <= metadata.st_size <= MAX_DIAGNOSTIC_BYTES):
            return True, None
        raw = bytearray()
        while len(raw) <= MAX_DIAGNOSTIC_BYTES:
            chunk = os.read(descriptor, min(4096, MAX_DIAGNOSTIC_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_DIAGNOSTIC_BYTES:
            return True, None
        value = json.loads(raw, object_pairs_hook=_strict_object)
        return True, value if isinstance(value, dict) else None
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError, RecursionError, DeliveryError):
        return True, None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _bounded_integer(value: Any, maximum: int, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _codeagent_diagnostics(root: Path, attempt: dict[str, Any]) -> dict[str, Any] | None:
    task_id, attempt_id = attempt.get("task_id"), attempt.get("attempt_id")
    terminal, value = _read_execution_diagnostic(root, task_id, attempt_id, "docker-terminal.json")
    if not terminal:
        if attempt.get("status") != "running" or attempt.get("stage") != "codeagent-running":
            return None
        _, value = _read_execution_diagnostic(root, task_id, attempt_id, "docker-progress.json")
    if not isinstance(value, dict):
        return None
    state = value.get("state")
    if not isinstance(state, str) or state not in ({"succeeded", "failed", "cancelled"} if terminal else {"authoring", "compiler-feedback", "model-retry"}):
        return None
    # OpenCode's historical request ceiling is 64; bounded HTTP retries are Codex-only.
    if not _bounded_integer(value.get("model_requests"), 64) or not _bounded_integer(value.get("compiler_checks"), 3):
        return None
    result = {"state": state, "model_requests": value["model_requests"], "compiler_checks": value["compiler_checks"]}
    if "model_request_budget" in value:
        try:
            result["model_request_budget"] = validate_model_request_budget(value["model_request_budget"], value["model_requests"])
        except DockerError:
            pass  # Optional diagnostics do not alter delivery or retry authority.
    for key, maximum in (("logical_model_requests", 64), ("model_retries", 8)):
        if key in value:
            if not _bounded_integer(value[key], maximum) or value[key] > value["model_requests"] + (1 if terminal and key == "logical_model_requests" else 0):
                return None
            result[key] = value[key]
    if "logical_model_requests" in result and "model_retries" in result:
        if result["logical_model_requests"] + result["model_retries"] - result["model_requests"] not in ({0, 1} if terminal else {0}):
            return None
    if state == "model-retry":
        retry = value.get("retry")
        if (not isinstance(retry, dict)
                or set(retry) != {"attempt", "max_attempts", "http_status", "delay_seconds", "reason"}
                or not _bounded_integer(retry.get("attempt"), 3, 2)
                or type(retry.get("max_attempts")) is not int or retry["max_attempts"] != 3
                or not _bounded_integer(retry.get("http_status"), 504, 429)
                or retry["http_status"] not in {429, 502, 503, 504}
                or type(retry.get("delay_seconds")) not in (int, float)
                or not 0 <= retry["delay_seconds"] <= 30
                or retry.get("reason") != ("provider-rate-limited" if retry["http_status"] == 429 else "provider-upstream-unavailable")
                or "logical_model_requests" not in result or "model_retries" not in result
                or result["model_requests"] > 48):
            return None
        result["retry"] = dict(retry)
    if terminal:
        # A contradictory terminal diagnostic never turns an accepted app into a failure.
        if attempt.get("status") == "private-appstore-ready" and state != "succeeded":
            return None
        if state in {"failed", "cancelled"} and isinstance(value.get("failure_code"), str) and value["failure_code"] in DIAGNOSTIC_FAILURE_CODES:
            result["failure_code"] = value["failure_code"]
        if state in {"failed", "cancelled"} and "failure_diagnostic" in value:
            try:
                result["failure_diagnostic"] = validate_failure_diagnostic(value["failure_diagnostic"])
            except DockerError:
                pass
        if _bounded_integer(value.get("upstream_status"), 599, 100):
            result["upstream_status"] = value["upstream_status"]
        request = value.get("upstream_request")
        if isinstance(request, dict):
            safe_request = {}
            for key, maximum in (("ordinal", 64), ("bytes", 8 * 1024 * 1024), ("connect_attempts", 2)):
                if _bounded_integer(request.get(key), maximum):
                    safe_request[key] = request[key]
            if isinstance(request.get("phase"), str) and request["phase"] in DIAGNOSTIC_PHASES:
                safe_request["phase"] = request["phase"]
            if isinstance(request.get("transport_error"), str) and request["transport_error"] in {"timeout", "http-framing", "connection"}:
                safe_request["transport_error"] = request["transport_error"]
            if safe_request:
                result["upstream_request"] = safe_request
    return result


def failure_retry_policy(code: str) -> tuple[bool, str]:
    """Unknown/configuration failures require diagnosis, never blind retries."""
    normalized = str(code).lower()
    if any(marker in normalized for marker in NON_RETRYABLE_FAILURE_MARKERS):
        return False, "review-task-or-trust-binding"
    if normalized in TRANSIENT_FAILURE_CODES:
        return True, "retry-with-new-attempt-and-consent"
    if any(marker in normalized for marker in ("unavailable", "config", "authentication", "credential")):
        return False, "configure-backend-before-new-attempt"
    return False, "inspect-diagnostic-before-new-attempt"

RECEIPT_FIELDS = {
    "schema_version",
    "document_type",
    "state",
    "scope",
    "job_id",
    "need_spec_digest_sha256",
    "source_tree_sha256",
    "required_capabilities",
    "package_entrypoints",
    "source_directory",
    "package_directory",
    "package_digest_sha256",
    "package_files",
    "component_sha256",
    "manifest_sha256",
    "build_log_sha256",
    "builder",
    "limits",
    "terminal_outcome",
    "authority",
    "created_at_utc",
}


class DeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, stage: str = "controller"):
        super().__init__(message)
        self.code = code
        self.stage = stage


class UnconfiguredBuilderRunner:
    def preflight(self) -> dict[str, Any]:
        raise PipelineError(
            "builder-not-configured",
            "automatic run requires an accepted tool layer, offline Cargo home, and cache acceptance",
        )

    def execute(self, source_root: Path, workspace: Path, limits: Any) -> Any:
        del source_root, workspace, limits
        raise PipelineError(
            "builder-not-configured",
            "automatic run requires an accepted tool layer, offline Cargo home, and cache acceptance",
        )


def delivery_readiness_preflight(builder_runner: Any, wasm_tools: Path) -> dict[str, Any]:
    """Run the exact Builder and Verifier checks required by their execution paths."""
    try:
        builder = builder_runner.preflight()
    except Exception as error:
        cause = getattr(error, "code", "internal")
        raise DeliveryError(
            "builder-preflight-failed",
            f"Builder configuration preflight failed ({cause}): {str(error)[:3072]}",
            stage="builder",
        ) from error
    if (
        not isinstance(builder, dict)
        or builder.get("status") != "ready"
        or builder.get("source_executed") is not False
        or builder.get("network") not in {"none", "not-used"}
    ):
        raise DeliveryError(
            "builder-preflight-failed",
            "Builder configuration preflight returned an untrusted readiness result",
            stage="builder",
        )
    try:
        verifier = verifier_preflight(wasm_tools)
    except Exception as error:
        cause = getattr(error, "code", "internal")
        raise DeliveryError(
            "verifier-preflight-failed",
            f"Verifier configuration preflight failed ({cause}): {str(error)[:3072]}",
            stage="verifier",
        ) from error
    if (
        verifier.get("status") != "ready"
        or verifier.get("source_executed") is not False
        or verifier.get("network") != "none"
        or verifier.get("wasm_tools_sha256") != WASM_TOOLS_SHA256
        or verifier.get("expected_wasm_tools_sha256") != WASM_TOOLS_SHA256
    ):
        raise DeliveryError(
            "verifier-preflight-failed",
            "Verifier configuration preflight returned an untrusted readiness result",
            stage="verifier",
        )
    return {
        "schema_version": "vibapp.delivery-readiness.experimental-v1",
        "status": "ready",
        "provider_process_started": False,
        "external_request_attempted": False,
        "external_request_observed": False,
        "source_executed": False,
        "builder": builder,
        "verifier": verifier,
    }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeliveryError("malformed-json", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _package_entrypoint_binding(value: Any, context: str) -> list[dict[str, Any]]:
    """Return the exact consent-bound entrypoint/trigger projection.

    Older service tasks did not carry ``triggers``.  Their only safe replay meaning
    is manual-only, matching the Builder's frozen compatibility behavior.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise DeliveryError("admission-binding-conflict", f"{context} is not a bounded list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise DeliveryError(
                "admission-binding-conflict", f"{context}[{index}] is not an object"
            )
        kind = item.get("kind")
        expected_fields = {"id", "kind", "label", "initial_route"}
        if kind == "service" and "triggers" in item:
            expected_fields.add("triggers")
        if set(item) != expected_fields:
            raise DeliveryError(
                "admission-binding-conflict", f"{context}[{index}] fields changed"
            )
        entrypoint_id = item.get("id")
        label = item.get("label")
        route = item.get("initial_route")
        if (
            not isinstance(entrypoint_id, str)
            or not APP_IDENTIFIER.fullmatch(entrypoint_id)
            or entrypoint_id in seen
            or kind not in {"launcher-ui", "service", "settings"}
            or not isinstance(label, str)
            or not label
            or len(label.encode("utf-8")) > 80
        ):
            raise DeliveryError(
                "admission-binding-conflict", f"{context}[{index}] identity changed"
            )
        if kind == "launcher-ui":
            if not isinstance(route, str) or not APP_IDENTIFIER.fullmatch(route):
                raise DeliveryError(
                    "admission-binding-conflict", f"{context}[{index}] route changed"
                )
            triggers: list[str] = []
        else:
            if route is not None:
                raise DeliveryError(
                    "admission-binding-conflict", f"{context}[{index}] route changed"
                )
            triggers = list(item.get("triggers", ["manual"])) if kind == "service" else []
        if kind == "service" and (
            not triggers
            or any(not isinstance(trigger, str) for trigger in triggers)
            or triggers
            != [trigger for trigger in SERVICE_TRIGGER_ORDER if trigger in triggers]
        ):
            raise DeliveryError(
                "admission-binding-conflict", f"{context}[{index}] triggers changed"
            )
        seen.add(entrypoint_id)
        result.append(
            {
                "id": entrypoint_id,
                "kind": kind,
                "label": label,
                "initial_route": route,
                "triggers": triggers,
            }
        )
    return result


def _manifest_entrypoint_binding(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise DeliveryError("manifest-binding-mismatch", f"{context} is not a bounded list")
    projected: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context}[{index}] is not an object"
            )
        kind = item.get("kind")
        routes = item.get("routes") if kind == "launcher-ui" else None
        projected.append(
            {
                "id": item.get("id"),
                "kind": kind,
                "label": item.get("label"),
                "initial_route": routes.get("initial") if isinstance(routes, dict) else None,
                "triggers": list(item.get("triggers", [])) if kind == "service" else [],
            }
        )
    return projected


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise DeliveryError("unsafe-path", f"not a private real directory: {path}")
    if metadata.st_uid != os.getuid():
        raise DeliveryError("unsafe-path", f"directory is owned by another user: {path}")
    path.chmod(0o700)
    return path.resolve(strict=True)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or not 1 <= metadata.st_size <= MAX_DOCUMENT_BYTES
    ):
        raise DeliveryError("record-integrity-failure", f"unsafe or oversized {label}: {path}")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DeliveryError("record-integrity-failure", f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise DeliveryError("record-integrity-failure", f"{label} must be an object")
    return value


def _validate_task_record(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "document_type", "task_id", "need_id", "need_spec_revision",
        "job_id", "immutable_task_digest_sha256", "provider_execution_identity_sha256",
        "supersedes_task_id", "attempt_count", "current_attempt_id", "attempt_ids",
        "created_at_utc", "updated_at_utc",
    }
    attempt_ids = value.get("attempt_ids")
    if (
        set(value) != expected
        or value.get("schema_version") != TASK_VERSION
        or value.get("document_type") != "delivery-task"
        or not isinstance(value.get("task_id"), str)
        or not TASK_ID.fullmatch(value["task_id"])
        or not isinstance(value.get("need_id"), str)
        or not IDENTIFIER.fullmatch(value["need_id"])
        or type(value.get("need_spec_revision")) is not int
        or value["need_spec_revision"] < 1
        or not isinstance(value.get("job_id"), str)
        or not IDENTIFIER.fullmatch(value["job_id"])
        or not isinstance(value.get("immutable_task_digest_sha256"), str)
        or not SHA256.fullmatch(value["immutable_task_digest_sha256"])
        or not isinstance(value.get("provider_execution_identity_sha256"), str)
        or not SHA256.fullmatch(value["provider_execution_identity_sha256"])
        or (
            value.get("supersedes_task_id") is not None
            and (
                not isinstance(value["supersedes_task_id"], str)
                or not TASK_ID.fullmatch(value["supersedes_task_id"])
                or value["supersedes_task_id"] == value["task_id"]
            )
        )
        or not isinstance(attempt_ids, list)
        or len(attempt_ids) > MAX_ATTEMPTS
        or any(not isinstance(item, str) or not ATTEMPT_ID.fullmatch(item) for item in attempt_ids)
        or len(set(attempt_ids)) != len(attempt_ids)
        or type(value.get("attempt_count")) is not int
        or value["attempt_count"] < 0
        or value["attempt_count"] != len(attempt_ids)
        or (value.get("current_attempt_id") is not None and value["current_attempt_id"] not in attempt_ids)
        or value.get("current_attempt_id") != (attempt_ids[-1] if attempt_ids else None)
        or any(not isinstance(value.get(field), str) or not UTC.fullmatch(value[field]) for field in ("created_at_utc", "updated_at_utc"))
    ):
        raise DeliveryError("record-integrity-failure", "delivery task record is malformed")
    return value


def _validate_attempt_record(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "document_type", "task_id", "attempt_id", "attempt_number",
        "retry_of_attempt_id", "need_id", "title", "need_spec_revision", "job_id",
        "consent_id", "provider_execution_identity_sha256",
        "registry_request_id", "registry_evidence_sha256", "canonical_task_sha256",
        "immutable_task_digest_sha256", "status", "stage", "progress_percent", "error",
        "outputs", "event_count", "created_at_utc", "updated_at_utc",
    }
    digests = (
        value.get("registry_evidence_sha256"),
        value.get("canonical_task_sha256"),
        value.get("immutable_task_digest_sha256"),
    )
    if (
        set(value) != expected
        or value.get("schema_version") != ATTEMPT_VERSION
        or value.get("document_type") != "delivery-attempt"
        or not isinstance(value.get("task_id"), str)
        or not TASK_ID.fullmatch(value["task_id"])
        or not isinstance(value.get("attempt_id"), str)
        or not ATTEMPT_ID.fullmatch(value["attempt_id"])
        or type(value.get("attempt_number")) is not int
        or not 1 <= value["attempt_number"] <= MAX_ATTEMPTS
        or (
            value.get("retry_of_attempt_id") is not None
            and (
                not isinstance(value["retry_of_attempt_id"], str)
                or not ATTEMPT_ID.fullmatch(value["retry_of_attempt_id"])
            )
        )
        or not isinstance(value.get("need_id"), str)
        or not IDENTIFIER.fullmatch(value["need_id"])
        or not isinstance(value.get("title"), str)
        or not 1 <= len(value["title"]) <= 80
        or type(value.get("need_spec_revision")) is not int
        or value["need_spec_revision"] < 1
        or not isinstance(value.get("job_id"), str)
        or not IDENTIFIER.fullmatch(value["job_id"])
        or not isinstance(value.get("consent_id"), str)
        or not IDENTIFIER.fullmatch(value["consent_id"])
        or not isinstance(value.get("provider_execution_identity_sha256"), str)
        or not SHA256.fullmatch(value["provider_execution_identity_sha256"])
        or not isinstance(value.get("registry_request_id"), str)
        or not 1 <= len(value["registry_request_id"]) <= 128
        or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in digests)
        or value.get("status") not in {"queued", "running", "failed", "private-appstore-ready"}
        or not isinstance(value.get("stage"), str)
        or not 1 <= len(value["stage"]) <= 80
        or type(value.get("progress_percent")) is not int
        or not 0 <= value["progress_percent"] <= 100
        or (value.get("error") is not None and not isinstance(value["error"], dict))
        or not isinstance(value.get("outputs"), dict)
        or type(value.get("event_count")) is not int
        or value["event_count"] < 0
        or any(not isinstance(value.get(field), str) or not UTC.fullmatch(value[field]) for field in ("created_at_utc", "updated_at_utc"))
    ):
        raise DeliveryError("record-integrity-failure", "delivery attempt record is malformed")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    payload = canonical_json(value) + b"\n"
    if not 1 <= len(payload) <= MAX_DOCUMENT_BYTES:
        raise DeliveryError("record-limit", "durable record exceeds 512 KiB")
    _private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if exclusive:
        descriptor = os.open(path, flags | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(path.parent)
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    descriptor = os.open(temporary, flags | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _load_module(name: str, path: Path) -> Any:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise DeliveryError("dependency-unavailable", f"reviewed dependency is unavailable: {path}")
    specification = importlib.util.spec_from_file_location(name, resolved)
    if specification is None or specification.loader is None:
        raise DeliveryError("dependency-unavailable", f"cannot load dependency: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


class CodeAgentStage:
    """Configured adapter invocation; explicit consent is still checked by the adapter."""

    def __init__(
        self,
        *,
        adapter_path: Path = CODEAGENT_DIR / "codeagent_adapter.py",
        cloud_agent_path: Path = CLOUD_DIR / "cloud_agent.py",
        provider_id: str = "codex",
        model: str,
        provider: Any | None = None,
        acknowledge_external_cost: bool = False,
    ):
        suffix = os.urandom(6).hex()
        self.adapter = _load_module(f"vibapp_delivery_adapter_{suffix}", adapter_path)
        self.cloud = self.adapter.load_cloud_agent(cloud_agent_path)
        self.provider_id = provider_id
        if (
            not isinstance(model, str)
            or not model
            or len(model) > 256
            or model.strip() != model
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in model)
            or model.startswith("-")
        ):
            raise DeliveryError("model-invalid", "an explicit bounded non-blank CodeAgent model is required")
        providers = self.adapter.provider_registry(self.cloud, model) if provider is None else {}
        self.provider = provider if provider is not None else providers.get(provider_id)
        if self.provider is None:
            raise DeliveryError("provider-unavailable", f"CodeAgent provider is unavailable: {provider_id}")
        self.acknowledge_external_cost = acknowledge_external_cost

    def validate(self, task: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.adapter.validate_task_value_for_provider(
                self.cloud,
                copy.deepcopy(task),
                self.provider,
            )
        except self.adapter.AdapterError as error:
            raise DeliveryError(error.code, str(error), stage="admission") from error

    def run(self, task_path: Path, output_root: Path, status_path: Path) -> dict[str, Any]:
        task = _load_json(task_path, "CodeAgent task")
        previous_signals = {}
        cancel = getattr(self.provider, "request_cancel", None)
        if cancel is not None and threading.current_thread() is threading.main_thread():
            def cancel_author(signum, frame):
                cancel()
                # Preserve the enclosing attempt's cancellation token as well
                # as the provider's own whole-container cancellation scope.
                previous = previous_signals.get(signum)
                if callable(previous) and getattr(previous, "_vibapp_attempt_cancellation", False):
                    previous(signum, frame)

            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_signals[signum] = signal.getsignal(signum)
                signal.signal(signum, cancel_author)
        try:
            return self.adapter.execute_task(
                self.cloud,
                self.provider,
                task_path,
                output_root,
                status_path,
                confirm_job=task["job_id"],
                confirm_consent=task["consent"]["consent_id"],
                acknowledge_external_cost=self.acknowledge_external_cost,
            )
        except self.adapter.AdapterError as error:
            raise DeliveryError(error.code, str(error), stage="codeagent") from error
        finally:
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)


class DeliveryController:
    def __init__(
        self,
        root: Path,
        codeagent: CodeAgentStage,
        builder_runner: Any,
        *,
        wasm_tools: Path = DEFAULT_WASM_TOOLS,
        appstore_root: Path | None = None,
        appstore_factory: Callable[[Path], Any] = LocalAppStore,
        runtime_readiness_checker: Callable[..., dict[str, Any]] | None = None,
    ):
        self.root = _private_directory(root)
        self.tasks = _private_directory(self.root / "tasks")
        self.appstore_root = _private_directory(appstore_root or self.root / "appstore")
        self.codeagent = codeagent
        self.builder_runner = builder_runner
        configure_builder = getattr(getattr(codeagent, "provider", None), "configure_builder", None)
        if configure_builder is not None and isinstance(builder_runner, MacSandboxCargoRunner):
            configure_builder(builder_runner, ProcessLimits)
        # Admission and history remain available in packaged Desktop builds without
        # verifier tooling. Reaching verification without exact wasm-tools still
        # fails closed inside the independent Verifier.
        self.wasm_tools = wasm_tools.resolve(strict=False)
        self.appstore_factory = appstore_factory
        # Explicit injection is for synthetic controller tests; production never
        # silently substitutes a compiler/fixture result for first-surface execution.
        self.runtime_readiness_checker = runtime_readiness_checker or check_runtime_readiness
        self.lock_path = self.root / ".delivery-controller.lock"

    def preflight(self) -> dict[str, Any]:
        return delivery_readiness_preflight(self.builder_runner, self.wasm_tools)

    def _check_runtime_readiness(self, candidate, manifest, candidate_sha, *, cancellation=None):
        try:
            readiness = self.runtime_readiness_checker(candidate, cancellation=cancellation)
            if (not isinstance(readiness, dict) or readiness.get("status") not in {"PASS", "not-applicable"}
                    or manifest["app"]["kind"] == "ui" and readiness.get("status") != "PASS"
                    or readiness.get("business_function_acceptance") is not False
                    or readiness.get("status") == "PASS" and readiness.get("cleanup_confirmed") is not True):
                raise RuntimeReadinessError("UI candidate did not pass bounded first-surface readiness")
            if builder_sha256_file(candidate, MAX_DOCUMENT_BYTES, "candidate record") != candidate_sha:
                raise RuntimeReadinessError("candidate changed after runtime readiness")
            return readiness
        except Exception as error:
            raise DeliveryError("runtime-readiness-failed", str(error)[:512], stage="appstore") from error

    def _controller_lock(self):
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return os.fdopen(descriptor, "r+")

    def _task_dir(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise DeliveryError("invalid-task-id", "delivery task ID is invalid")
        path = self.tasks / task_id
        if not path.exists():
            raise DeliveryError("task-not-found", f"delivery task does not exist: {task_id}")
        return _private_directory(path)

    @staticmethod
    def _task_record(task_dir: Path) -> dict[str, Any]:
        """Reconcile an index view from immutable attempts after an interrupted admission."""
        record = _validate_task_record(_load_json(task_dir / "task.json", "delivery task"))
        observed: list[tuple[int, str, dict[str, Any]]] = []
        attempts_dir = _private_directory(task_dir / "attempts")
        for path in attempts_dir.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            attempt_path = path / "attempt.json"
            if not attempt_path.is_file() or attempt_path.is_symlink():
                continue
            attempt = _validate_attempt_record(_load_json(attempt_path, "delivery attempt"))
            if (
                attempt.get("task_id") != record.get("task_id")
                or attempt.get("attempt_id") != path.name
                or attempt.get("need_id") != record.get("need_id")
                or attempt.get("need_spec_revision") != record.get("need_spec_revision")
                or attempt.get("job_id") != record.get("job_id")
                or attempt.get("immutable_task_digest_sha256")
                != record.get("immutable_task_digest_sha256")
                or attempt.get("provider_execution_identity_sha256")
                != record.get("provider_execution_identity_sha256")
            ):
                raise DeliveryError("record-integrity-failure", "attempt index binding is malformed")
            number = attempt.get("attempt_number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise DeliveryError("record-integrity-failure", "attempt number is malformed")
            observed.append((number, path.name, attempt))
        observed.sort(key=lambda item: item[0])
        if len({number for number, _, _ in observed}) != len(observed):
            raise DeliveryError("record-integrity-failure", "delivery task has duplicate attempt numbers")
        observed_ids = [attempt_id for _, attempt_id, _ in observed]
        if [number for number, _, _ in observed] != list(range(1, len(observed) + 1)):
            raise DeliveryError("record-integrity-failure", "delivery task attempt ordinals are not contiguous")
        if len({attempt["consent_id"] for _, _, attempt in observed}) != len(observed):
            raise DeliveryError("record-integrity-failure", "delivery task reuses a single-use consent")
        for index, (_, attempt_id, attempt) in enumerate(observed):
            expected_retry = observed_ids[index - 1] if index else None
            if (
                attempt_id[8:12] != f"{index + 1:04d}"
                or attempt.get("retry_of_attempt_id") != expected_retry
            ):
                raise DeliveryError(
                    "record-integrity-failure",
                    "delivery task retry lineage does not match its execution attempts",
                )
        if observed_ids != record.get("attempt_ids"):
            record = {
                **record,
                "attempt_count": len(observed_ids),
                "attempt_ids": observed_ids,
                "current_attempt_id": observed_ids[-1] if observed_ids else None,
            }
        return record

    @staticmethod
    def _validate_registry_no_match(
        registry: dict[str, Any], need_spec: dict[str, Any]
    ) -> None:
        required_keys = {
            "schema_version",
            "status",
            "route",
            "request_id",
            "need_id",
            "recommendations",
            "refinement",
            "retrieval",
            "rejected",
            "codeagent_handoff",
        }
        allowed_keys = required_keys | {"considered"}
        if not isinstance(registry, dict) or not isinstance(need_spec, dict):
            raise DeliveryError("registry-evidence-invalid", "Registry evidence must be an object")
        need_id = need_spec.get("need_id")
        expected_request_id = f"registry.{sha256_bytes(canonical_json(need_spec))[:24]}"
        request_id = registry.get("request_id")
        handoff = registry.get("codeagent_handoff")
        refinement = registry.get("refinement")
        if (
            not required_keys.issubset(registry)
            or not set(registry).issubset(allowed_keys)
            or registry.get("schema_version") != AUTHORITATIVE_REGISTRY_ROUTE_SCHEMA
            or registry.get("status") != AUTHORITATIVE_REGISTRY_STATUS
            or not isinstance(request_id, str)
            or request_id != expected_request_id
            or not isinstance(need_id, str)
            or not IDENTIFIER.fullmatch(need_id)
            or registry.get("need_id") != need_id
            or registry.get("route") != "refinement"
            or registry.get("recommendations") != []
            or not isinstance(refinement, dict)
            or set(refinement) != {"reason_code", "message"}
            or not isinstance(refinement.get("reason_code"), str)
            or not 1 <= len(refinement["reason_code"]) <= 128
            or refinement["reason_code"] == "registry-unavailable"
            or not isinstance(refinement.get("message"), str)
            or not 1 <= len(refinement["message"]) <= 4096
            or not isinstance(registry.get("retrieval"), dict)
            or not isinstance(registry.get("rejected"), list)
            or len(registry["rejected"]) > 50
            or (
                "considered" in registry
                and (
                    not isinstance(registry["considered"], list)
                    or len(registry["considered"]) > 20
                )
            )
            or not isinstance(handoff, dict)
            or set(handoff) != {"created", "permitted", "reason"}
            or handoff.get("created") is not False
            or handoff.get("permitted") is not False
            or not isinstance(handoff.get("reason"), str)
            or not 1 <= len(handoff["reason"]) <= 1000
        ):
            raise DeliveryError(
                "registry-no-match-required",
                "development requires an authoritative Registry refinement bound to the exact NeedSpec digest, with no recommendations or prior handoff",
                stage="admission",
            )

    def submit(
        self,
        task: dict[str, Any],
        registry_no_match: dict[str, Any],
        *,
        explicit_user_submit: bool,
        retry_task_id: str | None = None,
        edited_from_task_id: str | None = None,
    ) -> dict[str, Any]:
        if explicit_user_submit is not True:
            raise DeliveryError("explicit-submit-required", "the user must explicitly submit the task")
        if retry_task_id is not None and edited_from_task_id is not None:
            raise DeliveryError(
                "retry-mode-conflict",
                "same-task retry and edited-NeedSpec replacement are distinct operations",
            )
        task = self.codeagent.validate(task)
        self._validate_registry_no_match(registry_no_match, task["need_spec"])
        immutable_digest = task["immutable_task_digest_sha256"]
        if not isinstance(immutable_digest, str) or not SHA256.fullmatch(immutable_digest):
            raise DeliveryError("task-invalid", "immutable task digest is invalid")
        need_id = task["need_spec"]["need_id"]
        if not isinstance(need_id, str) or not IDENTIFIER.fullmatch(need_id):
            raise DeliveryError("task-invalid", "NeedSpec ID is invalid")
        execution_attempt = task.get("execution_attempt")
        provider_identity = task.get("provider_execution_identity")
        consent = task.get("consent")
        if (
            not isinstance(execution_attempt, dict)
            or not isinstance(execution_attempt.get("attempt_id"), str)
            or not ATTEMPT_ID.fullmatch(execution_attempt["attempt_id"])
            or type(execution_attempt.get("ordinal")) is not int
            or not isinstance(provider_identity, dict)
            or not isinstance(provider_identity.get("identity_sha256"), str)
            or not SHA256.fullmatch(provider_identity["identity_sha256"])
            or not isinstance(consent, dict)
            or consent.get("attempt_id") != execution_attempt["attempt_id"]
            or consent.get("provider_execution_identity_sha256")
            != provider_identity["identity_sha256"]
        ):
            raise DeliveryError(
                "attempt-binding-conflict",
                "task v3 execution attempt, consent, and provider identity are not exact",
                stage="admission",
            )
        attempt_id = execution_attempt["attempt_id"]
        attempt_ordinal = execution_attempt["ordinal"]
        identity_digest = provider_identity["identity_sha256"]
        computed_task_id = (
            f"development-{sha256_bytes((need_id + ':' + immutable_digest).encode())[:32]}"
        )
        task_id = retry_task_id or computed_task_id
        with self._controller_lock():
            if retry_task_id is None:
                task_dir = self.tasks / task_id
                if task_dir.exists():
                    task_record = self._task_record(_private_directory(task_dir))
                    if attempt_id in task_record["attempt_ids"]:
                        existing = _validate_attempt_record(
                            _load_json(
                                task_dir / "attempts" / attempt_id / "attempt.json",
                                "delivery attempt",
                            )
                        )
                        self._validate_attempt_inputs(
                            task_dir / "attempts" / attempt_id,
                            existing,
                            expected_task=task,
                            expected_registry=registry_no_match,
                        )
                        return existing
                    if task_record["attempt_count"] == 0:
                        if (
                            task_record.get("need_id") != need_id
                            or task_record.get("need_spec_revision")
                            != task["need_spec_current_revision"]
                            or task_record.get("job_id") != task["job_id"]
                            or task_record.get("immutable_task_digest_sha256")
                            != immutable_digest
                            or task_record.get("provider_execution_identity_sha256")
                            != identity_digest
                            or task_record.get("supersedes_task_id")
                            != edited_from_task_id
                            or attempt_ordinal != 1
                        ):
                            raise DeliveryError(
                                "admission-binding-conflict",
                                "interrupted task admission is bound to different immutable inputs",
                            )
                        retry_of = None
                    else:
                        raise DeliveryError(
                            "retry-intent-required",
                            "a new execution attempt for the same immutable task requires retry_task_id",
                        )
                else:
                    if attempt_ordinal != 1:
                        raise DeliveryError(
                            "attempt-sequence-conflict",
                            "a new immutable delivery task must begin at execution attempt ordinal 1",
                        )
                    supersedes_task_id = None
                    if edited_from_task_id is not None:
                        prior_dir = self._task_dir(edited_from_task_id)
                        prior_record = self._task_record(prior_dir)
                        prior_attempt_id = prior_record.get("current_attempt_id")
                        if not isinstance(prior_attempt_id, str):
                            raise DeliveryError(
                                "edited-task-binding-conflict",
                                "the replaced delivery task has no prior attempt",
                            )
                        prior_attempt = _validate_attempt_record(
                            _load_json(
                                prior_dir / "attempts" / prior_attempt_id / "attempt.json",
                                "replaced delivery attempt",
                            )
                        )
                        if prior_attempt.get("status") != "failed":
                            raise DeliveryError(
                                "edited-task-not-allowed",
                                "only a failed delivery task can be replaced by an edited NeedSpec",
                            )
                        if (
                            prior_record.get("need_id") != need_id
                            or task["need_spec_current_revision"]
                            <= prior_record.get("need_spec_revision", 0)
                            or task["job_id"] == prior_record.get("job_id")
                            or immutable_digest
                            == prior_record.get("immutable_task_digest_sha256")
                            or any(
                                _validate_attempt_record(
                                    _load_json(
                                        prior_dir
                                        / "attempts"
                                        / existing_id
                                        / "attempt.json",
                                        "replaced delivery attempt",
                                    )
                                ).get("consent_id")
                                == consent.get("consent_id")
                                for existing_id in prior_record["attempt_ids"]
                            )
                        ):
                            raise DeliveryError(
                                "edited-task-revision-required",
                                "edited NeedSpec replacement requires the same NeedSpec ID, a newer revision, new job, new immutable request, and new consent",
                            )
                        supersedes_task_id = edited_from_task_id
                    _private_directory(task_dir)
                    _private_directory(task_dir / "attempts")
                    task_record = {
                        "schema_version": TASK_VERSION,
                        "document_type": "delivery-task",
                        "task_id": task_id,
                        "need_id": need_id,
                        "need_spec_revision": task["need_spec_current_revision"],
                        "job_id": task["job_id"],
                        "immutable_task_digest_sha256": immutable_digest,
                        "provider_execution_identity_sha256": identity_digest,
                        "supersedes_task_id": supersedes_task_id,
                        "attempt_count": 0,
                        "current_attempt_id": None,
                        "attempt_ids": [],
                        "created_at_utc": now_utc(),
                        "updated_at_utc": now_utc(),
                    }
                    _write_json(task_dir / "task.json", task_record, exclusive=True)
                retry_of = None
            else:
                if task_id != computed_task_id:
                    raise DeliveryError(
                        "retry-binding-conflict",
                        "same-task retry changed the immutable delivery task identity",
                    )
                task_dir = self._task_dir(task_id)
                task_record = self._task_record(task_dir)
                if (
                    task_record.get("need_id") != need_id
                    or task_record.get("need_spec_revision")
                    != task["need_spec_current_revision"]
                    or task_record.get("job_id") != task["job_id"]
                    or task_record.get("immutable_task_digest_sha256") != immutable_digest
                    or task_record.get("provider_execution_identity_sha256") != identity_digest
                ):
                    raise DeliveryError(
                        "retry-binding-conflict",
                        "same-task retry changed the NeedSpec, job, immutable request, or provider identity",
                    )
                retry_of = task_record.get("current_attempt_id")
                if not isinstance(retry_of, str):
                    raise DeliveryError("retry-binding-conflict", "delivery task has no prior attempt")
                previous = _validate_attempt_record(
                    _load_json(
                        task_dir / "attempts" / retry_of / "attempt.json",
                        "previous delivery attempt",
                    )
                )
                if previous.get("status") != "failed":
                    raise DeliveryError("retry-not-allowed", "only a failed attempt can be retried")
                if previous.get("error", {}).get("same_task_retry_allowed") is not True:
                    raise DeliveryError(
                        "retry-not-transient",
                        "the prior failure is not classified for same immutable-task retry",
                    )
                previous_task = _load_json(
                    task_dir / "attempts" / retry_of / "input/task.json", "previous CodeAgent task"
                )
                if (
                    task["job_id"] != previous_task["job_id"]
                    or immutable_digest != previous_task["immutable_task_digest_sha256"]
                    or identity_digest
                    != previous_task["provider_execution_identity"]["identity_sha256"]
                    or task["need_spec_current_revision"]
                    != previous_task["need_spec_current_revision"]
                    or canonical_json(task["need_spec"])
                    != canonical_json(previous_task["need_spec"])
                ):
                    raise DeliveryError(
                        "retry-binding-conflict",
                        "same-task retry must preserve the exact immutable request and NeedSpec",
                    )
                if (
                    attempt_ordinal != task_record["attempt_count"] + 1
                    or attempt_id in task_record["attempt_ids"]
                    or any(
                        _validate_attempt_record(
                            _load_json(
                                task_dir / "attempts" / existing_id / "attempt.json",
                                "prior delivery attempt",
                            )
                        ).get("consent_id")
                        == consent.get("consent_id")
                        for existing_id in task_record["attempt_ids"]
                    )
                ):
                    raise DeliveryError(
                        "retry-attempt-required",
                        "same-task retry requires the next execution ordinal, a new attempt ID, and a new single-use consent",
                    )
            if task_record["attempt_count"] >= MAX_ATTEMPTS:
                raise DeliveryError("attempt-limit", f"delivery task reached {MAX_ATTEMPTS} attempts")
            number = task_record["attempt_count"] + 1
            if attempt_ordinal != number or attempt_id[8:12] != f"{number:04d}":
                raise DeliveryError(
                    "attempt-sequence-conflict",
                    "execution attempt ID and ordinal must equal the controller's next attempt",
                )
            if retry_of is not None:
                _, previous_registry = self._validate_attempt_inputs(
                    task_dir / "attempts" / retry_of,
                    previous,
                )
                if canonical_json(previous_registry) != canonical_json(registry_no_match):
                    raise DeliveryError(
                        "retry-binding-conflict",
                        "same-task retry cannot substitute different Registry no-match evidence",
                    )
            attempt_dir = _private_directory(task_dir / "attempts" / attempt_id)
            input_dir = _private_directory(attempt_dir / "input")
            _write_json(input_dir / "task.json", task, exclusive=True)
            _write_json(input_dir / "registry-no-match.json", registry_no_match, exclusive=True)
            created = now_utc()
            record = {
                "schema_version": ATTEMPT_VERSION,
                "document_type": "delivery-attempt",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "attempt_number": number,
                "retry_of_attempt_id": retry_of,
                "need_id": need_id,
                "title": task["package_intent"]["display_name"],
                "need_spec_revision": task["need_spec_current_revision"],
                "job_id": task["job_id"],
                "consent_id": consent["consent_id"],
                "provider_execution_identity_sha256": identity_digest,
                "registry_request_id": registry_no_match["request_id"],
                "registry_evidence_sha256": sha256_bytes(canonical_json(registry_no_match)),
                "canonical_task_sha256": sha256_bytes(canonical_json(task)),
                "immutable_task_digest_sha256": immutable_digest,
                "status": "queued",
                "stage": "confirmed-registry-no-match",
                "progress_percent": 5,
                "error": None,
                "outputs": {},
                "event_count": 0,
                "created_at_utc": created,
                "updated_at_utc": created,
            }
            _write_json(attempt_dir / "attempt.json", record, exclusive=True)
            record = self._transition(attempt_dir, "attempt-admitted", "queued", 5)
            task_record.update(
                {
                    "attempt_count": number,
                    "current_attempt_id": attempt_id,
                    "attempt_ids": [*task_record["attempt_ids"], attempt_id],
                    "updated_at_utc": now_utc(),
                }
            )
            _write_json(task_dir / "task.json", task_record)
            return record

    def _transition(
        self,
        attempt_dir: Path,
        stage: str,
        status: str,
        progress: int,
        *,
        error: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record_path = attempt_dir / "attempt.json"
        record = _validate_attempt_record(_load_json(record_path, "delivery attempt"))
        events_path = attempt_dir / "events.jsonl"
        durable_event_count = 0
        if events_path.exists():
            metadata = events_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
            ):
                raise DeliveryError("record-integrity-failure", "delivery event log is unsafe")
            raw = events_path.read_bytes()
            if len(raw) > MAX_EVENT_LOG_BYTES:
                raise DeliveryError("event-limit", "delivery event log exceeds 1 MiB")
            for expected_sequence, line in enumerate(raw.splitlines(), 1):
                try:
                    existing_event = json.loads(line, object_pairs_hook=_strict_object)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise DeliveryError("record-integrity-failure", "delivery event log is malformed") from error
                if not isinstance(existing_event, dict) or existing_event.get("sequence") != expected_sequence:
                    raise DeliveryError("record-integrity-failure", "delivery event sequence is not contiguous")
                durable_event_count = expected_sequence
        sequence = durable_event_count + 1
        at = now_utc()
        event = {
            "schema_version": EVENT_VERSION,
            "sequence": sequence,
            "event": stage,
            "status": status,
            "progress_percent": progress,
            "error": error,
            "at_utc": at,
        }
        payload = canonical_json(event) + b"\n"
        if len(payload) > MAX_EVENT_BYTES:
            raise DeliveryError("event-limit", "delivery event exceeds 16 KiB")
        descriptor = os.open(
            events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if os.fstat(descriptor).st_size + len(payload) > MAX_EVENT_LOG_BYTES:
                raise DeliveryError("event-limit", "delivery event log exceeds 1 MiB")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        merged_outputs = dict(record.get("outputs", {}))
        if outputs:
            merged_outputs.update(outputs)
        record.update(
            {
                "stage": stage,
                "status": status,
                "progress_percent": progress,
                "error": error,
                "outputs": merged_outputs,
                "event_count": sequence,
                "updated_at_utc": at,
            }
        )
        _write_json(record_path, record)
        return record

    def _relative(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError as error:
            raise DeliveryError("path-escape", f"pipeline output escaped controller root: {path}") from error

    def _output_path(self, value: Any, context: str) -> Path:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 1024
            or "\\" in value
            or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))
        ):
            raise DeliveryError("record-integrity-failure", f"{context} path is invalid")
        path = (self.root / value).resolve(strict=True)
        root = self.root.resolve(strict=True)
        if root not in path.parents:
            raise DeliveryError("path-escape", f"{context} escaped the controller root")
        return path

    def _bound_output_path(self, value: Any, expected: Path, context: str) -> Path:
        """Resolve one durable output only at its canonical attempt-owned path."""
        expected = expected.absolute()
        try:
            expected_relative = expected.relative_to(self.root).as_posix()
        except ValueError as error:
            raise DeliveryError("path-escape", f"{context} canonical path escaped the controller") from error
        if value != expected_relative:
            raise DeliveryError(
                "record-integrity-failure", f"{context} is not bound to this delivery attempt"
            )
        resolved = self._output_path(value, context)
        if resolved != expected:
            # A final-component symlink would otherwise disappear inside resolve().
            raise DeliveryError("path-escape", f"{context} canonical path was redirected")
        return resolved

    def _validate_replay_task(self, task: dict[str, Any]) -> None:
        """Recheck immutable task authority without re-evaluating consent expiry.

        Expiry gates a new provider invocation.  A completed delivery must remain
        reproducibly auditable after that time, so replay checks the original
        shape/digests/authority tuple but deliberately does not compare timestamps
        with the current clock.
        """
        cloud = self.codeagent.cloud
        try:
            cloud.validate_task_schema(copy.deepcopy(task))
            immutable_digest = cloud.immutable_task_digest(task, cloud.CONTRACT_DIGEST_PIN)
            instructions_digest = cloud.provider_instructions_digest(task.get("provider"), task)
        except Exception as error:
            raise DeliveryError(
                "admission-binding-conflict",
                f"durable CodeAgent task no longer satisfies its frozen schema: {str(error)[:2048]}",
                stage="admission",
            ) from error
        consent = task.get("consent")
        need = task.get("need_spec")
        execution_attempt = task.get("execution_attempt")
        provider_identity = task.get("provider_execution_identity")
        if (
            not isinstance(consent, dict)
            or not isinstance(need, dict)
            or not isinstance(execution_attempt, dict)
            or not isinstance(provider_identity, dict)
        ):
            raise DeliveryError(
                "admission-binding-conflict",
                "durable task lost its attempt, identity, consent, or NeedSpec binding",
                stage="admission",
            )
        need_digest = sha256_bytes(canonical_json(need))
        provider = task.get("provider")
        if (
            task.get("need_spec_digest_sha256") != need_digest
            or task.get("immutable_task_digest_sha256") != immutable_digest
            or consent.get("payload_digest_sha256") != immutable_digest
            or consent.get("contract_digest_sha256") != cloud.CONTRACT_DIGEST_PIN
            or consent.get("instructions_digest_sha256") != instructions_digest
            or consent.get("policy_version") != cloud.POLICY_VERSION
            or consent.get("uploaded_data_classes") != cloud.DISCLOSED_DATA_CLASSES
            or consent.get("decision") != "granted"
            or consent.get("single_use") is not True
            or consent.get("job_id") != task.get("job_id")
            or consent.get("attempt_id") != execution_attempt.get("attempt_id")
            or consent.get("provider") != provider
            or consent.get("model") != task.get("model")
            or consent.get("provider_execution_identity_sha256")
            != provider_identity.get("identity_sha256")
            or consent.get("subject") != need.get("owner")
            or task.get("need_spec_current_revision") != need.get("revision")
            or task.get("need_spec_complete") is not True
            or task.get("remote_processing_consent") is not True
            or provider not in TASK_PROVIDER_TO_ADAPTER
        ):
            raise DeliveryError(
                "admission-binding-conflict",
                "durable task, NeedSpec, immutable provider request, and consent no longer match",
                stage="admission",
            )

    def _validate_attempt_inputs(
        self,
        attempt_dir: Path,
        record: dict[str, Any],
        *,
        expected_task: dict[str, Any] | None = None,
        expected_registry: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        task = _load_json(attempt_dir / "input/task.json", "CodeAgent task")
        registry = _load_json(attempt_dir / "input/registry-no-match.json", "Registry no-match")
        self._validate_replay_task(task)
        self._validate_registry_no_match(registry, task["need_spec"])
        task_sha = sha256_bytes(canonical_json(task))
        registry_sha = sha256_bytes(canonical_json(registry))
        if (
            record.get("canonical_task_sha256") != task_sha
            or record.get("registry_evidence_sha256") != registry_sha
            or record.get("immutable_task_digest_sha256") != task.get("immutable_task_digest_sha256")
            or record.get("attempt_id") != task.get("execution_attempt", {}).get("attempt_id")
            or record.get("attempt_number") != task.get("execution_attempt", {}).get("ordinal")
            or record.get("need_id") != task.get("need_spec", {}).get("need_id")
            or record.get("need_spec_revision") != task.get("need_spec_current_revision")
            or record.get("job_id") != task.get("job_id")
            or record.get("consent_id") != task.get("consent", {}).get("consent_id")
            or record.get("provider_execution_identity_sha256")
            != task.get("provider_execution_identity", {}).get("identity_sha256")
            or record.get("registry_request_id") != registry.get("request_id")
        ):
            raise DeliveryError(
                "admission-binding-conflict",
                "durable attempt inputs no longer match the admitted task and Registry evidence",
            )
        if expected_task is not None and canonical_json(expected_task) != canonical_json(task):
            raise DeliveryError(
                "admission-binding-conflict",
                "an existing task ID is bound to different immutable task bytes",
            )
        if expected_registry is not None and canonical_json(expected_registry) != canonical_json(registry):
            raise DeliveryError(
                "admission-binding-conflict",
                "an existing task ID is bound to different Registry no-match evidence",
            )
        return task, registry

    def _fail(
        self,
        attempt_dir: Path,
        stage: str,
        error: Exception,
        *,
        outputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        code = getattr(error, "code", "internal")
        detail = str(error).replace("\x00", "")[:4096]
        same_task_retry_allowed, recovery_action = failure_retry_policy(code)
        diagnostic = {
            "stage": stage,
            "code": code,
            "message": detail,
            "retryable_with_new_attempt": same_task_retry_allowed,
            "same_task_retry_allowed": same_task_retry_allowed,
            "recovery_action": recovery_action,
            "same_task_retry_requirements": [
                "same-need-spec-id-and-revision",
                "same-job-id",
                "same-immutable-task-digest",
                "same-provider-execution-identity",
                "next-execution-attempt-ordinal",
                "new-attempt-id",
                "new-single-use-consent-bound-to-attempt",
                "same-registry-no-match-evidence",
            ],
            "edited_need_creates_new_task": True,
            "edit_retry_requirements": [
                "new-delivery-task",
                "newer-need-spec-revision",
                "new-job-id",
                "new-immutable-task-digest",
                "execution-attempt-ordinal-1",
                "new-single-use-consent",
            ],
        }
        return self._transition(
            attempt_dir,
            f"{stage}-failed",
            "failed",
            0,
            error=diagnostic,
            outputs=outputs,
        )

    @staticmethod
    def _validate_receipt_binding(
        receipt: dict[str, Any], task: dict[str, Any], handoff: dict[str, Any]
    ) -> list[dict[str, Any]]:
        expected_entrypoints = _package_entrypoint_binding(
            task.get("package_intent", {}).get("entrypoints"), "task.package_intent.entrypoints"
        )
        if set(receipt) not in (RECEIPT_FIELDS, RECEIPT_FIELDS | {"presentation"}):
            raise DeliveryError(
                "builder-receipt-invalid",
                "Builder receipt is not the strict entrypoint-bound v2 record",
                stage="builder",
            )
        if (
            handoff.get("package_intent") != task.get("package_intent")
            or handoff.get("target") != task.get("target")
            or receipt.get("schema_version") != "vibapp.builder-quarantine.experimental-v2"
            or receipt.get("document_type") != "builder-quarantine-receipt"
            or receipt.get("state") != "quarantined-awaiting-verifier"
            or receipt.get("scope") != "local-product-prototype"
            or receipt.get("job_id") != task.get("job_id")
            or receipt.get("need_spec_digest_sha256") != task.get("need_spec_digest_sha256")
            or receipt.get("source_tree_sha256") != handoff.get("source_tree_sha256")
            or receipt.get("required_capabilities")
            != task.get("target", {}).get("required_capabilities")
            or receipt.get("package_entrypoints") != expected_entrypoints
            or receipt.get("source_directory") != "source"
            or receipt.get("package_directory") != "package"
            or receipt.get("terminal_outcome") != "build-output-untrusted"
            or receipt.get("authority")
            != {
                "builder": "quarantine-only",
                "verify": "independent-verifier",
                "install": "none",
                "publish": "none",
            }
        ):
            raise DeliveryError(
                "builder-receipt-invalid",
                "Builder receipt changed the exact task/handoff entrypoints, triggers, or authority",
                stage="builder",
            )
        return expected_entrypoints

    @staticmethod
    def _validate_manifest_binding(
        manifest: dict[str, Any],
        task: dict[str, Any],
        handoff: dict[str, Any],
        receipt: dict[str, Any],
        *,
        context: str,
    ) -> None:
        package = task.get("package_intent")
        target = task.get("target")
        if not isinstance(package, dict) or not isinstance(target, dict):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} lost its task package target", stage="verifier"
            )
        expected_entrypoints = _package_entrypoint_binding(
            package.get("entrypoints"), "task.package_intent.entrypoints"
        )
        app = manifest.get("app")
        runtime = manifest.get("runtime")
        capabilities = manifest.get("capabilities")
        entrypoints = manifest.get("entrypoints")
        if (
            handoff.get("package_intent") != package
            or handoff.get("target") != target
            or receipt.get("package_entrypoints") != expected_entrypoints
            or manifest.get("schema_version") != "vibapp.manifest.experimental-v0.0.1"
            or manifest.get("package_format") != "vibapp.package.experimental-v0"
            or not isinstance(app, dict)
            or not isinstance(runtime, dict)
            or not isinstance(capabilities, list)
            or not isinstance(entrypoints, list)
        ):
            raise DeliveryError(
                "manifest-binding-mismatch",
                f"{context} is not bound to the exact task/handoff record",
                stage="verifier",
            )
        expected_app = {
            "id": package.get("app_id"),
            "version": package.get("version"),
            "kind": target.get("app_kind"),
            "display_name": package.get("display_name"),
            "description": package.get("description"),
        }
        if any(app.get(key) != value for key, value in expected_app.items()):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} app identity differs from the task", stage="verifier"
            )
        if app.get("publisher") != {
            "id": "ai.vibapp.local",
            "display_name": "VibApp Local Builder",
        }:
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} changed the local Builder publisher", stage="verifier"
            )
        expected_profiles = target.get("profiles")
        profile_rows = runtime.get("profiles")
        if (
            runtime.get("contract") != target.get("contract")
            or runtime.get("wasi") != target.get("wasi")
            or runtime.get("world") != target.get("wit_world")
            or runtime.get("required_imports") != target.get("required_imports")
            or not isinstance(profile_rows, list)
            or [row.get("profile") if isinstance(row, dict) else None for row in profile_rows]
            != expected_profiles
        ):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} runtime target differs from the task", stage="verifier"
            )
        manifest_entrypoints = _manifest_entrypoint_binding(entrypoints, f"{context}.entrypoints")
        if manifest_entrypoints != expected_entrypoints:
            raise DeliveryError(
                "manifest-binding-mismatch",
                f"{context} entrypoints or lifecycle triggers differ from the task",
                stage="verifier",
            )
        for row, expected in zip(entrypoints, expected_entrypoints, strict=True):
            if row.get("profiles") != expected_profiles:
                raise DeliveryError(
                    "manifest-binding-mismatch", f"{context} entrypoint profiles changed", stage="verifier"
                )
            if expected["kind"] == "launcher-ui" and (
                row.get("routes")
                != {
                    "initial": expected["initial_route"],
                    "allowed": [expected["initial_route"]],
                }
                or row.get("restoration") != "route-only"
            ):
                raise DeliveryError(
                    "manifest-binding-mismatch", f"{context} launcher route authority expanded", stage="verifier"
                )
            if expected["kind"] == "service" and (
                row.get("triggers") != expected["triggers"]
                or row.get("health_check_interval_seconds") != 60
            ):
                raise DeliveryError(
                    "manifest-binding-mismatch", f"{context} service lifecycle changed", stage="verifier"
                )
            if expected["kind"] == "settings" and row.get("schema_export") != "get-settings-schema":
                raise DeliveryError(
                    "manifest-binding-mismatch", f"{context} settings export changed", stage="verifier"
                )
        expected_imports = target.get("required_imports")
        interfaces = [row.get("interface") if isinstance(row, dict) else None for row in capabilities]
        required = {
            row.get("interface")
            for row in capabilities
            if isinstance(row, dict) and row.get("necessity") == "required"
        }
        if interfaces != expected_imports or required != set(target.get("required_capabilities", [])):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} capability necessity differs from consent", stage="verifier"
            )
        source = manifest.get("source")
        source_tree = handoff.get("source_tree_sha256")
        if (
            not isinstance(source, dict)
            or not isinstance(source_tree, str)
            or source.get("revision") != f"codeagent-{source_tree[:24]}"
        ):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} source provenance differs from the handoff", stage="verifier"
            )

    def _load_bound_manifest(
        self,
        path: Path,
        expected_sha256: Any,
        task: dict[str, Any],
        handoff: dict[str, Any],
        receipt: dict[str, Any],
        *,
        context: str,
    ) -> dict[str, Any]:
        if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256):
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} has no valid manifest digest", stage="verifier"
            )
        actual_sha256 = builder_sha256_file(path, MAX_DOCUMENT_BYTES, context)
        if actual_sha256 != expected_sha256:
            raise DeliveryError(
                "manifest-binding-mismatch", f"{context} bytes changed", stage="verifier"
            )
        manifest = _load_json(path, context)
        self._validate_manifest_binding(
            manifest, task, handoff, receipt, context=context
        )
        return manifest

    def _consume_candidate(
        self, candidate: Path, receipt: dict[str, Any], receipt_path: Path
    ) -> dict[str, Any]:
        value = _load_json(candidate, "verifier candidate")
        receipt_sha = builder_sha256_file(receipt_path, MAX_DOCUMENT_BYTES, "quarantine receipt")
        if (
            value.get("schema_version") != "vibapp.builder-candidate.experimental-v1"
            or value.get("document_type") != "verifier-promoted-candidate"
            or value.get("state") != "candidate-ready"
            or value.get("package_directory") != "package"
            or value.get("job_id") != receipt.get("job_id")
            or value.get("source_tree_sha256") != receipt.get("source_tree_sha256")
            or value.get("package_digest_sha256") != receipt.get("package_digest_sha256")
            or value.get("component", {}).get("path") != "component.wasm"
            or value.get("component", {}).get("sha256") != receipt.get("component_sha256")
            or value.get("manifest", {}).get("path") != "manifest.json"
            or value.get("manifest", {}).get("sha256") != receipt.get("manifest_sha256")
            or value.get("quarantine_receipt_sha256") != receipt_sha
            or value.get("verification", {}).get("authority") != "independent-verifier"
            or value.get("authority") != {"install": "daemon", "publish": "none"}
        ):
            raise DeliveryError(
                "candidate-digest-mismatch", "verifier candidate does not bind the exact quarantine receipt", stage="verifier"
            )
        return value

    @staticmethod
    def _validate_appstore_record(
        appstore: dict[str, Any],
        receipt: dict[str, Any],
        receipt_sha: str,
        candidate_record: dict[str, Any],
        candidate_sha: str,
        manifest: dict[str, Any],
    ) -> None:
        digests = appstore.get("digests", {})
        runtime = manifest.get("runtime", {})
        manifest_runtime_projection = {
            "contract": runtime.get("contract"),
            "wasi": runtime.get("wasi"),
            "world": runtime.get("world"),
            "profiles": runtime.get("profiles"),
            "platforms": runtime.get("platforms"),
            "entrypoints": manifest.get("entrypoints"),
            "capabilities": manifest.get("capabilities"),
        }
        if (
            appstore.get("schema_version") != "vibapp.local-appstore-record.experimental-v1"
            or appstore.get("state") != "private"
            or appstore.get("visibility") != "private"
            or appstore.get("publication") != {"state": "not-published", "authority": "none"}
            or appstore.get("authority") != {"install": "daemon", "publish": "none"}
            or digests.get("package_sha256") != receipt.get("package_digest_sha256")
            or digests.get("package_sha256") != candidate_record.get("package_digest_sha256")
            or digests.get("component_sha256") != receipt.get("component_sha256")
            or digests.get("component_sha256") != candidate_record.get("component", {}).get("sha256")
            or digests.get("manifest_sha256") != receipt.get("manifest_sha256")
            or digests.get("manifest_sha256") != candidate_record.get("manifest", {}).get("sha256")
            or digests.get("source_tree_sha256") != receipt.get("source_tree_sha256")
            or digests.get("source_tree_sha256") != candidate_record.get("source_tree_sha256")
            or digests.get("quarantine_receipt_sha256") != receipt_sha
            or digests.get("candidate_record_sha256") != candidate_sha
            or appstore.get("app") != manifest.get("app")
            or appstore.get("runtime") != manifest_runtime_projection
            or appstore.get("app_state") != manifest.get("state")
        ):
            raise DeliveryError(
                "appstore-digest-mismatch",
                "private AppStore record changed an authoritative digest",
                stage="appstore",
            )

    def _revalidate_private_ready(
        self,
        attempt_dir: Path,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        task, _ = self._validate_attempt_inputs(attempt_dir, record)
        expected_provider_id = TASK_PROVIDER_TO_ADAPTER.get(task.get("provider"))
        if expected_provider_id is None:
            raise DeliveryError(
                "codeagent-status-invalid", "task provider has no accepted adapter binding", stage="codeagent"
            )
        outputs = record.get("outputs", {})
        status = _load_json(attempt_dir / "codeagent/status.json", "CodeAgent status")
        expected_status = {
            "schema_version": "vibapp.codeagent-adapter-status.experimental-v2",
            "job_id": task.get("job_id"),
            "need_id": task.get("need_spec", {}).get("need_id"),
            "need_spec_revision": task.get("need_spec_current_revision"),
            "immutable_task_digest_sha256": task.get("immutable_task_digest_sha256"),
            "consent_id": task.get("consent", {}).get("consent_id"),
            "provider_execution_identity_sha256": task.get(
                "provider_execution_identity", {}
            ).get("identity_sha256"),
            "identity_observed": True,
            "execution_available": True,
            "provider_id": expected_provider_id,
            "task_provider": task.get("provider"),
            "model": task.get("model"),
            "adapter": "local-codeagent-adapter",
            "status": "source-ready",
            "current_stage": "awaiting-separate-builder",
            "consent_consumed": True,
            "builder_invoked": False,
            "verifier_invoked": False,
            "installation_performed": False,
            "publication_performed": False,
        }
        if any(status.get(key) != value for key, value in expected_status.items()) or any(
            type(status.get(key)) is not bool
            for key in (
                "provider_process_started",
                "external_request_attempted",
                "external_request_observed",
            )
        ):
            raise DeliveryError(
                "codeagent-status-invalid",
                "terminal CodeAgent status is not bound to the immutable task and authority boundary",
                stage="codeagent",
            )
        if (
            outputs.get("codeagent_provider_id") != status.get("provider_id")
            or outputs.get("codeagent_model") != status.get("model")
            or outputs.get("external_request_attempted") != status.get("external_request_attempted")
            or outputs.get("external_request_observed") != status.get("external_request_observed")
            or outputs.get("gateway_request_id") != status.get("gateway_request_id")
        ):
            raise DeliveryError(
                "codeagent-output-binding-mismatch",
                "terminal outputs do not match the immutable CodeAgent status",
                stage="codeagent",
            )
        handoff_path = self._output_path(outputs.get("source_handoff_path"), "CodeAgent handoff")
        handoff_sha = builder_sha256_file(handoff_path, MAX_DOCUMENT_BYTES, "CodeAgent handoff")
        output_root = (attempt_dir / "codeagent/output").resolve(strict=True)
        if output_root not in handoff_path.parents:
            raise DeliveryError(
                "path-escape", "terminal CodeAgent handoff escaped its output root", stage="codeagent"
            )
        handoff_relative = handoff_path.relative_to(output_root).as_posix()
        if (
            handoff_sha != outputs.get("source_handoff_sha256")
            or handoff_sha != status.get("handoff_sha256")
            or status.get("handoff_relative_path") != handoff_relative
        ):
            raise DeliveryError("handoff-digest-mismatch", "terminal CodeAgent handoff changed", stage="codeagent")
        handoff = _load_json(handoff_path, "CodeAgent handoff")
        try:
            validated_handoff = validate_handoff(handoff_path)
        except PipelineError as error:
            raise DeliveryError(
                "handoff-binding-mismatch",
                f"terminal source handoff/source tree no longer validates: {str(error)[:2048]}",
                stage="codeagent",
            ) from error
        if canonical_json(validated_handoff.document) != canonical_json(handoff):
            raise DeliveryError(
                "handoff-binding-mismatch",
                "terminal source handoff parser changed its durable meaning",
                stage="codeagent",
            )
        execution = handoff.get("provider_execution", {})
        if (
            handoff.get("schema_version") != "vibapp.codeagent-source-handoff.experimental-v2"
            or handoff.get("document_type") != "codeagent-source-handoff"
            or handoff.get("status") != "untrusted-source-awaiting-builder"
            or handoff.get("job_id") != task.get("job_id")
            or handoff.get("need_spec_digest_sha256") != task.get("need_spec_digest_sha256")
            or handoff.get("package_intent") != task.get("package_intent")
            or handoff.get("target") != task.get("target")
            or handoff.get("source_directory") != "source"
            or handoff.get("authority")
            != {"compile": "separate-builder", "verify": "separate-verifier", "install": "none", "publish": "none"}
            or execution.get("mode") not in {"direct-local-explicit-opt-in", "docker-local-explicit-opt-in"}
            or execution.get("adapter") != ("docker-codeagent-launcher" if execution.get("mode") == "docker-local-explicit-opt-in" else "local-codeagent-adapter")
            or execution.get("provider_id") != expected_provider_id
            or execution.get("executable_sha256")
            != task.get("provider_execution_identity", {}).get("runtime", {}).get(
                "executable_sha256"
            )
            or execution.get("external_request_attempted") is not True
            or execution.get("external_request_observed") is not (execution.get("mode") == "docker-local-explicit-opt-in")
            or (execution.get("mode") == "direct-local-explicit-opt-in" and execution.get("gateway_request_id") is not None)
            or not isinstance(execution.get("executable_sha256"), str)
            or not SHA256.fullmatch(execution["executable_sha256"])
            or (execution.get("mode") == "direct-local-explicit-opt-in" and any(
                execution.get(key) is not None for key in (
                    "isolation_policy_version", "runner_id", "runner_identity_sha256",
                    "isolation_policy_sha256", "receipt_digest_sha256",
                )
            ))
            or execution.get("external_request_attempted") != status.get("external_request_attempted")
            or execution.get("external_request_observed") != status.get("external_request_observed")
            or execution.get("gateway_request_id") != status.get("gateway_request_id")
        ):
            raise DeliveryError(
                "handoff-binding-mismatch",
                "terminal source handoff is not bound to the exact CodeAgent task and execution",
                stage="codeagent",
            )
        self._validate_docker_handoff_receipt(execution, status, task, record)
        receipt_path = self._bound_output_path(
            outputs.get("builder_receipt_path"),
            attempt_dir
            / "builder"
            / "jobs"
            / str(task.get("job_id"))
            / "quarantine"
            / "quarantine-receipt.json",
            "Builder receipt",
        )
        receipt = _load_json(receipt_path, "Builder quarantine receipt")
        receipt_sha = builder_sha256_file(receipt_path, MAX_DOCUMENT_BYTES, "quarantine receipt")
        self._validate_receipt_binding(receipt, task, handoff)
        if (
            receipt_sha != outputs.get("builder_receipt_sha256")
            or receipt.get("package_digest_sha256") != outputs.get("package_digest_sha256")
            or receipt.get("component_sha256") != outputs.get("component_sha256")
            or receipt.get("manifest_sha256") != outputs.get("manifest_sha256")
        ):
            raise DeliveryError(
                "builder-receipt-invalid",
                "terminal Builder receipt is not bound to the exact source handoff",
                stage="builder",
            )
        quarantine_manifest = self._load_bound_manifest(
            receipt_path.parent / "package" / "manifest.json",
            receipt.get("manifest_sha256"),
            task,
            handoff,
            receipt,
            context="Builder quarantine manifest",
        )
        package_digest = receipt.get("package_digest_sha256")
        if not isinstance(package_digest, str) or not SHA256.fullmatch(package_digest):
            raise DeliveryError("appstore-digest-mismatch", "terminal package digest is invalid", stage="appstore")
        candidate_path = self._bound_output_path(
            outputs.get("candidate_path"),
            attempt_dir / "builder" / "candidates" / package_digest / "candidate.json",
            "Verifier candidate",
        )
        candidate = self._consume_candidate(candidate_path, receipt, receipt_path)
        candidate_sha = builder_sha256_file(candidate_path, MAX_DOCUMENT_BYTES, "candidate record")
        if candidate_sha != outputs.get("candidate_record_sha256"):
            raise DeliveryError("candidate-digest-mismatch", "terminal Verifier candidate changed", stage="verifier")
        candidate_manifest = self._load_bound_manifest(
            candidate_path.parent / "package" / "manifest.json",
            candidate.get("manifest", {}).get("sha256"),
            task,
            handoff,
            receipt,
            context="Verifier candidate manifest",
        )
        if canonical_json(candidate_manifest) != canonical_json(quarantine_manifest):
            raise DeliveryError(
                "manifest-binding-mismatch",
                "Verifier candidate manifest differs from the Builder quarantine manifest",
                stage="verifier",
            )
        detail = self.appstore_factory(self.appstore_root).detail(package_digest)
        appstore = detail.get("record", {})
        appstore_manifest = self._load_bound_manifest(
            self.appstore_root / "candidates" / package_digest / "package" / "manifest.json",
            appstore.get("digests", {}).get("manifest_sha256"),
            task,
            handoff,
            receipt,
            context="private AppStore manifest",
        )
        if canonical_json(appstore_manifest) != canonical_json(candidate_manifest):
            raise DeliveryError(
                "manifest-binding-mismatch",
                "private AppStore manifest differs from the verified candidate manifest",
                stage="appstore",
            )
        self._validate_appstore_record(
            appstore, receipt, receipt_sha, candidate, candidate_sha, appstore_manifest
        )
        if (
            outputs.get("appstore_candidate_path") != appstore.get("paths", {}).get("candidate")
            or outputs.get("app_id") != appstore.get("app", {}).get("id")
            or outputs.get("app_version") != appstore.get("app", {}).get("version")
            or outputs.get("publication_performed") is not False
            or outputs.get("digest_equality_proven") is not True
            or outputs.get("task_handoff_manifest_binding_proven") is not True
        ):
            raise DeliveryError(
                "appstore-record-mismatch",
                "terminal outputs do not identify the exact private AppStore record",
                stage="appstore",
            )
        # Read-only history/completed-record validation must never execute a
        # guest. Readiness is freshly checked only on the pre-ingest mutation path.
        return record

    def revalidate_attempt(self, task_id: str, attempt_id: str) -> dict[str, Any]:
        task_dir = self._task_dir(task_id)
        if attempt_id not in self._task_record(task_dir).get("attempt_ids", []):
            raise DeliveryError("attempt-not-found", "delivery attempt is not part of this task")
        attempt_dir = _private_directory(task_dir / "attempts" / attempt_id)
        record = _validate_attempt_record(_load_json(attempt_dir / "attempt.json", "delivery attempt"))
        if record.get("status") != "private-appstore-ready":
            raise DeliveryError("live-chain-incomplete", "delivery attempt is not private-AppStore-ready")
        return self._revalidate_private_ready(attempt_dir, record)

    def _validate_resumable_source(
        self, attempt_dir: Path, record: dict[str, Any], task_record: dict[str, Any]
    ) -> None:
        """Recheck retained source authority without invoking or reauthorizing a provider."""
        if (
            record.get("status") != "failed"
            or record.get("attempt_id") != task_record.get("current_attempt_id")
            or "cancel" in str(record.get("stage", "")).lower()
            or "cancel" in str((record.get("error") or {}).get("code", "")).lower()
        ):
            raise DeliveryError("source-resume-not-allowed", "only the current failed, non-cancelled attempt can resume its retained source")
        task, _ = self._validate_attempt_inputs(attempt_dir, record)
        # Desktop cancellation is durable separately from the controller record.
        worker_path = self.root / "desktop-workers" / f"{record['task_id']}-{record['attempt_id']}.json"
        if worker_path.exists():
            worker = _load_json(worker_path, "delivery worker status")
            if (
                worker.get("schema_version") != "vibapp.desktop-delivery-worker.experimental-v2"
                or worker.get("task_id") != record["task_id"]
                or worker.get("attempt_id") != record["attempt_id"]
                or worker.get("immutable_task_digest_sha256") != task["immutable_task_digest_sha256"]
                or worker.get("terminal") is not True
                or worker.get("cleanup_confirmed") is not True
                or "cancel" in str(worker.get("status", "")).lower()
                or worker.get("owner_cancellation_reason") is not None
            ):
                raise DeliveryError("source-resume-not-allowed", "delivery worker is active, cancelled, unbound, or has unconfirmed cleanup")
        status_path = attempt_dir / "codeagent/status.json"
        if not status_path.is_file():
            raise DeliveryError("source-resume-not-ready", "retained source-ready CodeAgent status is required")
        status = _load_json(status_path, "CodeAgent status")
        expected = {
            "schema_version": "vibapp.codeagent-adapter-status.experimental-v2",
            "adapter": "local-codeagent-adapter", "status": "source-ready",
            "current_stage": "awaiting-separate-builder", "error": None,
            "job_id": task["job_id"], "need_id": task["need_spec"]["need_id"],
            "need_spec_revision": task["need_spec_current_revision"],
            "immutable_task_digest_sha256": task["immutable_task_digest_sha256"],
            "consent_id": task["consent"]["consent_id"], "consent_consumed": True,
            "provider_execution_identity_sha256": task["provider_execution_identity"]["identity_sha256"],
            "provider_id": TASK_PROVIDER_TO_ADAPTER[task["provider"]],
            "task_provider": task["provider"], "model": task["model"],
            "identity_observed": True, "execution_available": True,
            "builder_invoked": False, "verifier_invoked": False,
            "installation_performed": False, "publication_performed": False,
        }
        if (
            any(status.get(key) != value or (isinstance(value, bool) and type(status.get(key)) is not bool) for key, value in expected.items())
            or status.get("provider_id") != self.codeagent.provider_id
            or status.get("model") != getattr(self.codeagent.provider, "model", None)
            or any(type(status.get(key)) is not bool for key in ("provider_process_started", "external_request_attempted", "external_request_observed"))
        ):
            raise DeliveryError("source-resume-not-ready", "retained CodeAgent status is not bound to the exact admitted task", stage="codeagent")
        outputs = record.get("outputs", {})
        for output_key, status_key in (
            ("codeagent_provider_id", "provider_id"), ("codeagent_model", "model"),
            ("external_request_attempted", "external_request_attempted"),
            ("external_request_observed", "external_request_observed"),
            ("gateway_request_id", "gateway_request_id"),
        ):
            if output_key not in outputs or outputs[output_key] != status.get(status_key):
                raise DeliveryError("codeagent-output-binding-mismatch", "retained outputs differ from the exact CodeAgent status", stage="codeagent")
        relative = f"runs/{task['job_id']}/handoff.json"
        handoff_path = self._bound_output_path(
            outputs.get("source_handoff_path"), attempt_dir / "codeagent/output" / relative, "retained CodeAgent handoff"
        )
        digest = builder_sha256_file(handoff_path, MAX_DOCUMENT_BYTES, "retained CodeAgent handoff")
        if status.get("handoff_relative_path") != relative or digest != status.get("handoff_sha256") or digest != outputs.get("source_handoff_sha256"):
            raise DeliveryError("handoff-digest-mismatch", "retained source handoff changed", stage="codeagent")
        handoff = validate_handoff(handoff_path).document
        execution = handoff["provider_execution"]
        if (
            any(handoff.get(key) != task.get(key) for key in ("job_id", "need_spec_digest_sha256", "package_intent", "target"))
            or execution.get("provider_id") != expected["provider_id"]
            or execution.get("executable_sha256") != task["provider_execution_identity"]["runtime"]["executable_sha256"]
            or any(execution.get(key) != status.get(key) for key in ("external_request_attempted", "external_request_observed", "gateway_request_id"))
        ):
            raise DeliveryError("handoff-binding-mismatch", "retained source differs from the immutable task or provider execution", stage="codeagent")
        self._validate_docker_handoff_receipt(execution, status, task, record)

    @staticmethod
    def _validate_docker_handoff_receipt(
        execution: dict[str, Any], status: dict[str, Any], task: dict[str, Any], record: dict[str, Any]
    ) -> None:
        if execution.get("mode") == "docker-local-explicit-opt-in":
            receipt = status.get("container_receipt")
            expected_receipt = {
                "schema_version": "codeagent-launcher-receipt.experimental-v0", "executor_kind": "docker",
                "job_id": task["job_id"], "attempt_id": record["attempt_id"],
                "provider_id": TASK_PROVIDER_TO_ADAPTER[task["provider"]], "model": task["model"],
                "task_digest_sha256": task["immutable_task_digest_sha256"],
                "provider_profile_digest_sha256": task["provider_execution_identity"]["identity_sha256"],
                "idempotency_key": task["consent"]["consent_id"],
                "image_digest_sha256": execution.get("runner_identity_sha256"),
                "network_policy_digest_sha256": execution.get("isolation_policy_sha256"),
                "backend_execution_id": execution.get("runner_id"),
            }
            if (
                not isinstance(receipt, dict)
                or receipt.get("cleanup_confirmed") is not True
                or receipt.get("whole_job_quiescent") is not True
                or any(receipt.get(key) != value for key, value in expected_receipt.items())
                or sha256_bytes(canonical_json(receipt)) != execution.get("receipt_digest_sha256")
            ):
                raise DeliveryError("handoff-binding-mismatch", "retained Docker receipt is not bound and quiescent", stage="codeagent")

    def _preserve_failed_builder_job(
        self, attempt_dir: Path, builder_root: Path, task: dict[str, Any], handoff: dict[str, Any]
    ) -> None:
        """Keep exact failed Builder evidence before rebuilding its canonical job path."""
        job_path = builder_root / "jobs" / task["job_id"]
        if not job_path.exists():
            return
        descriptor = os.open(builder_root / ".builder.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeliveryError("attempt-active", "Builder is still using this attempt") from error
            _private_directory(job_path)
            job = _load_json(job_path / "job.json", "failed Builder job")
            if (
                job.get("schema_version") != "vibapp.builder-job.experimental-v1"
                or job.get("state") != "failed"
                or job.get("job_id") != task["job_id"]
                or job.get("source_tree_sha256") != handoff["source_tree_sha256"]
            ):
                raise DeliveryError("source-resume-not-allowed", "existing Builder job is not a bound terminal failure")
            record = _load_json(attempt_dir / "attempt.json", "delivery attempt")
            history = _private_directory(builder_root / "failed-jobs")
            preserved = history / f"{task['job_id']}-{record['event_count']:06d}"
            if preserved.exists() or preserved.is_symlink():
                raise DeliveryError("source-resume-not-allowed", "failed Builder evidence destination already exists")
            os.rename(job_path, preserved)
            _fsync_directory(job_path.parent)
            _fsync_directory(history)
            self._transition(attempt_dir, "builder-failure-preserved", "running", 45, outputs={"builder_previous_failure_path": self._relative(preserved)})
        finally:
            os.close(descriptor)

    def run_attempt(
        self, task_id: str, attempt_id: str | None = None, *, resume_source_handoff: bool = False
    ) -> dict[str, Any]:
        if type(resume_source_handoff) is not bool:
            raise DeliveryError("source-resume-not-allowed", "source handoff recovery must be an explicit boolean")
        # This lease spans authoring, compilation and verification, even when
        # separate Desktop/Web processes use different product data roots.
        # The adapter also retains its own admission lock for direct CLI users.
        cancellation = threading.Event()
        previous_signals = {}

        def cancel_attempt(*_):
            cancellation.set()
            cancel = getattr(self.codeagent.provider, "request_cancel", None)
            if callable(cancel):
                cancel()

        cancel_attempt._vibapp_attempt_cancellation = True

        try:
            with self.codeagent.adapter.one_job_lease(
                self.root, budget_kind="pipeline",
                parallel_authoring=getattr(self.codeagent.provider, "parallel_authoring", False),
            ):
                # Cover final compiler queue/processes too; CodeAgent's narrower
                # handler restores these handlers when authoring ends.
                if threading.current_thread() is threading.main_thread():
                    for signum in (signal.SIGTERM, signal.SIGINT):
                        previous_signals[signum] = signal.getsignal(signum)
                        signal.signal(signum, cancel_attempt)
                return self._run_attempt_under_budget(task_id, attempt_id, resume_source_handoff=resume_source_handoff,
                                                      cancellation=cancellation)
        except self.codeagent.adapter.AdapterError as error:
            raise DeliveryError(error.code, str(error), stage="admission") from error
        finally:
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)

    def _run_attempt_under_budget(
        self, task_id: str, attempt_id: str | None = None, *, resume_source_handoff: bool = False, cancellation=None
    ) -> dict[str, Any]:
        def check_cancellation():
            if cancellation is not None and cancellation.is_set():
                raise DeliveryError("provider-cancelled", "delivery attempt was cancelled")

        task_dir = self._task_dir(task_id)
        task_record = self._task_record(task_dir)
        attempt_id = attempt_id or task_record.get("current_attempt_id")
        if not isinstance(attempt_id, str) or attempt_id not in task_record.get("attempt_ids", []):
            raise DeliveryError("attempt-not-found", "delivery attempt is not part of this task")
        attempt_dir = _private_directory(task_dir / "attempts" / attempt_id)
        lock_path = attempt_dir / ".run.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeliveryError("attempt-active", "another worker is running this attempt") from error
            current = _validate_attempt_record(
                _load_json(attempt_dir / "attempt.json", "delivery attempt")
            )
            if resume_source_handoff:
                # Validate before reopening: rejected recovery leaves the failed
                # record/event history intact and can never enter CodeAgent.run.
                self._validate_resumable_source(attempt_dir, current, task_record)
                self._transition(attempt_dir, "source-handoff-resumed", "running", 45)
            elif current["status"] in TERMINAL_STATUSES:
                if current["status"] == "private-appstore-ready":
                    return self._revalidate_private_ready(attempt_dir, current)
                self._validate_attempt_inputs(attempt_dir, current)
                return current
            task_path = attempt_dir / "input/task.json"
            task, _ = self._validate_attempt_inputs(attempt_dir, current)
            codeagent_root = _private_directory(attempt_dir / "codeagent")
            codeagent_output = _private_directory(codeagent_root / "output")
            status_path = codeagent_root / "status.json"
            try:
                check_cancellation()
                if status_path.exists():
                    agent_status = _load_json(status_path, "CodeAgent status")
                    if agent_status.get("status") != "source-ready":
                        recover = getattr(self.codeagent.provider, "recover", None)
                        if not resume_source_handoff and recover is not None and not self.codeagent.adapter.process_active(agent_status.get("process_id")):
                            recover(task, codeagent_output)
                        raise DeliveryError(
                            "codeagent-interrupted",
                            "CodeAgent has no restart-safe source-ready handoff; submit a new same-task execution attempt with fresh attempt-bound consent",
                            stage="codeagent",
                        )
                else:
                    if resume_source_handoff:
                        raise DeliveryError("source-resume-not-ready", "retained CodeAgent status disappeared; recovery never invokes a provider", stage="codeagent")
                    try:
                        self.preflight()
                    except DeliveryError as error:
                        return self._fail(
                            attempt_dir,
                            error.stage,
                            error,
                            outputs={
                                "provider_process_started": False,
                                "external_request_attempted": False,
                                "external_request_observed": False,
                                "source_executed": False,
                            },
                        )
                    self._transition(attempt_dir, "codeagent-running", "running", 20)
                    agent_status = self.codeagent.run(task_path, codeagent_output, status_path)
                check_cancellation()
                if (
                    agent_status.get("status") != "source-ready"
                    or agent_status.get("job_id") != task.get("job_id")
                    or agent_status.get("need_id") != task.get("need_spec", {}).get("need_id")
                    or agent_status.get("need_spec_revision")
                    != task.get("need_spec_current_revision")
                    or agent_status.get("immutable_task_digest_sha256")
                    != task.get("immutable_task_digest_sha256")
                    or agent_status.get("consent_id")
                    != task.get("consent", {}).get("consent_id")
                    or agent_status.get("provider_execution_identity_sha256")
                    != task.get("provider_execution_identity", {}).get("identity_sha256")
                    or agent_status.get("identity_observed") is not True
                    or agent_status.get("execution_available") is not True
                    or agent_status.get("provider_id") != self.codeagent.provider_id
                    or agent_status.get("model") != getattr(self.codeagent.provider, "model", None)
                    or type(agent_status.get("external_request_attempted")) is not bool
                    or type(agent_status.get("external_request_observed")) is not bool
                ):
                    raise DeliveryError(
                        "codeagent-status-invalid",
                        "CodeAgent terminal status is not bound to the configured provider/model",
                        stage="codeagent",
                    )
                handoff_relative = agent_status.get("handoff_relative_path")
                if not isinstance(handoff_relative, str):
                    raise DeliveryError("handoff-missing", "CodeAgent did not return a source handoff", stage="codeagent")
                handoff = (codeagent_output / handoff_relative).resolve(strict=True)
                if codeagent_output.resolve(strict=True) not in handoff.parents:
                    raise DeliveryError("path-escape", "CodeAgent handoff escaped its output root", stage="codeagent")
                handoff_sha = builder_sha256_file(handoff, MAX_DOCUMENT_BYTES, "CodeAgent handoff")
                if handoff_sha != agent_status.get("handoff_sha256"):
                    raise DeliveryError("handoff-digest-mismatch", "CodeAgent handoff digest changed", stage="codeagent")
                self._transition(
                    attempt_dir,
                    "source-ready",
                    "running",
                    45,
                    outputs={
                        "source_handoff_path": self._relative(handoff),
                        "source_handoff_sha256": handoff_sha,
                        "codeagent_provider_id": agent_status.get("provider_id"),
                        "codeagent_model": agent_status.get("model"),
                        "external_request_attempted": agent_status.get("external_request_attempted"),
                        "external_request_observed": agent_status.get("external_request_observed"),
                        "gateway_request_id": agent_status.get("gateway_request_id"),
                    },
                )
            except Exception as error:
                if getattr(error, "code", None) == "local-capacity-busy" and not status_path.exists():
                    self._transition(attempt_dir, "awaiting-author-capacity", "queued", 5)
                    raise DeliveryError("local-capacity-busy", "host author capacity is occupied", stage="admission") from error
                return self._fail(attempt_dir, "codeagent", error)

            try:
                handoff_document = validate_handoff(handoff).document
                if (
                    handoff_document.get("job_id") != task.get("job_id")
                    or handoff_document.get("need_spec_digest_sha256")
                    != task.get("need_spec_digest_sha256")
                    or handoff_document.get("package_intent") != task.get("package_intent")
                    or handoff_document.get("target") != task.get("target")
                ):
                    raise DeliveryError(
                        "handoff-binding-mismatch",
                        "CodeAgent handoff differs from the immutable task",
                        stage="codeagent",
                    )
            except Exception as error:
                return self._fail(attempt_dir, "codeagent", error)

            builder_root = _private_directory(attempt_dir / "builder")
            receipt_path = builder_root / "jobs" / task["job_id"] / "quarantine/quarantine-receipt.json"
            try:
                check_cancellation()
                if not receipt_path.exists():
                    if resume_source_handoff:
                        self._preserve_failed_builder_job(attempt_dir, builder_root, task, handoff_document)
                    self._transition(attempt_dir, "builder-running", "running", 55)
                    receipt_path = build_handoff(handoff, builder_root, self.builder_runner, cancellation=cancellation)
                check_cancellation()
                receipt = _load_json(receipt_path, "Builder quarantine receipt")
                self._validate_receipt_binding(receipt, task, handoff_document)
                quarantine_manifest = self._load_bound_manifest(
                    receipt_path.parent / "package" / "manifest.json",
                    receipt.get("manifest_sha256"),
                    task,
                    handoff_document,
                    receipt,
                    context="Builder quarantine manifest",
                )
                receipt_sha = builder_sha256_file(receipt_path, MAX_DOCUMENT_BYTES, "quarantine receipt")
                self._transition(
                    attempt_dir,
                    "quarantined-awaiting-verifier",
                    "running",
                    68,
                    outputs={
                        "builder_receipt_path": self._relative(receipt_path),
                        "builder_receipt_sha256": receipt_sha,
                        "package_digest_sha256": receipt.get("package_digest_sha256"),
                        "component_sha256": receipt.get("component_sha256"),
                        "manifest_sha256": receipt.get("manifest_sha256"),
                    },
                )
            except Exception as error:
                return self._fail(attempt_dir, "builder", error)

            expected_digest = receipt.get("package_digest_sha256")
            candidate = builder_root / "candidates" / str(expected_digest) / "candidate.json"
            try:
                check_cancellation()
                if not candidate.exists():
                    self._transition(attempt_dir, "verifier-running", "running", 75)
                    candidate = verify_and_promote(receipt_path, builder_root, wasm_tools=self.wasm_tools)
                check_cancellation()
                candidate_record = self._consume_candidate(candidate, receipt, receipt_path)
                candidate_manifest = self._load_bound_manifest(
                    candidate.parent / "package" / "manifest.json",
                    candidate_record.get("manifest", {}).get("sha256"),
                    task,
                    handoff_document,
                    receipt,
                    context="Verifier candidate manifest",
                )
                if canonical_json(candidate_manifest) != canonical_json(quarantine_manifest):
                    raise DeliveryError(
                        "manifest-binding-mismatch",
                        "Verifier candidate manifest differs from the Builder quarantine manifest",
                        stage="verifier",
                    )
                candidate_sha = builder_sha256_file(candidate, MAX_DOCUMENT_BYTES, "candidate record")
                self._transition(
                    attempt_dir,
                    "candidate-ready",
                    "running",
                    88,
                    outputs={
                        "candidate_path": self._relative(candidate),
                        "candidate_record_sha256": candidate_sha,
                    },
                )
            except Exception as error:
                return self._fail(attempt_dir, "verifier", error)

            try:
                check_cancellation()
                readiness = self._check_runtime_readiness(candidate, candidate_manifest, candidate_sha, cancellation=cancellation)
                check_cancellation()
                self._transition(attempt_dir, "appstore-ingesting", "running", 94)
                result = self.appstore_factory(self.appstore_root).ingest(candidate)
                appstore = result["record"]
                receipt_sha = builder_sha256_file(receipt_path, MAX_DOCUMENT_BYTES, "quarantine receipt")
                candidate_sha = builder_sha256_file(candidate, MAX_DOCUMENT_BYTES, "candidate record")
                package_digest = receipt.get("package_digest_sha256")
                appstore_manifest = self._load_bound_manifest(
                    self.appstore_root
                    / "candidates"
                    / str(package_digest)
                    / "package"
                    / "manifest.json",
                    appstore.get("digests", {}).get("manifest_sha256"),
                    task,
                    handoff_document,
                    receipt,
                    context="private AppStore manifest",
                )
                if canonical_json(appstore_manifest) != canonical_json(candidate_manifest):
                    raise DeliveryError(
                        "manifest-binding-mismatch",
                        "private AppStore manifest differs from the verified candidate manifest",
                        stage="appstore",
                    )
                self._validate_appstore_record(
                    appstore,
                    receipt,
                    receipt_sha,
                    candidate_record,
                    candidate_sha,
                    appstore_manifest,
                )
                return self._transition(
                    attempt_dir,
                    "private-appstore-ready",
                    "private-appstore-ready",
                    100,
                    outputs={
                        "appstore_created": result["created"],
                        "runtime_readiness": readiness,
                        "appstore_candidate_path": appstore["paths"]["candidate"],
                        "app_id": appstore["app"]["id"],
                        "app_version": appstore["app"]["version"],
                        "publication_performed": False,
                        "digest_equality_proven": True,
                        "task_handoff_manifest_binding_proven": True,
                    },
                )
            except Exception as error:
                return self._fail(attempt_dir, "appstore", error)
        finally:
            os.close(descriptor)

    def status(self, task_id: str) -> dict[str, Any]:
        task_dir = self._task_dir(task_id)
        task = self._task_record(task_dir)
        attempt_id = task.get("current_attempt_id")
        if not isinstance(attempt_id, str):
            return {"task": task, "attempt": None}
        attempt = _validate_attempt_record(
            _load_json(task_dir / "attempts" / attempt_id / "attempt.json", "delivery attempt")
        )
        diagnostics = _codeagent_diagnostics(self.root, attempt)
        if diagnostics is not None:
            attempt = {**attempt, "codeagent_diagnostics": diagnostics}
        return {"task": task, "attempt": attempt}

    def history(self, task_id: str) -> dict[str, Any]:
        # Reading historical v1 records never upgrades them into executable v2
        # authority. Modern records retain their original resolver/validator path;
        # no legacy path policy is allowed to change their history behavior.
        # Probe without _task_dir: that existing resolver chmods directories.
        # Probe failures fall through to the unchanged modern validation path.
        legacy_schema = False
        if isinstance(task_id, str) and TASK_ID.fullmatch(task_id):
            candidate = self.root / "tasks" / task_id
            try:
                safe = True
                for path in (self.root, self.root / "tasks", candidate):
                    metadata = path.lstat()
                    safe = safe and stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
                if safe:
                    document = _load_json(candidate / "task.json", "delivery task")
                    legacy_schema = document.get("schema_version") == "vibapp.delivery-task.experimental-v1"
            except (DeliveryError, OSError):
                pass
        if legacy_schema:
            specification = importlib.util.spec_from_file_location(
                "vibapp_readonly_delivery_history", BASE / "delivery_history.py"
            )
            if specification is None or specification.loader is None:
                raise DeliveryError("history-reader-unavailable", "read-only history decoder is unavailable")
            history_reader = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(history_reader)
            legacy = history_reader.read_legacy_history(self.root, task_id)
            if legacy is None:
                raise DeliveryError("record-integrity-failure", "legacy task changed while history was read")
            return legacy
        task_dir = self._task_dir(task_id)
        task = self._task_record(task_dir)
        attempts = [
            _validate_attempt_record(
                _load_json(task_dir / "attempts" / attempt_id / "attempt.json", "delivery attempt")
            )
            for attempt_id in task["attempt_ids"]
        ]
        for attempt in attempts:
            diagnostics = _codeagent_diagnostics(self.root, attempt)
            if diagnostics is not None:
                attempt["codeagent_diagnostics"] = diagnostics
        return {"schema_version": SCHEMA_VERSION, "task": task, "attempts": attempts}


def _read_argument(path: Path) -> dict[str, Any]:
    return _load_json(path.resolve(strict=True), path.name)


def _build_runner(arguments: argparse.Namespace, *, required: bool) -> Any:
    configured = (
        arguments.builder_input_root,
        arguments.tool_layer,
        arguments.cargo_home,
        arguments.cache_acceptance,
    )
    if all(value is not None for value in configured):
        try:
            return MacSandboxCargoRunner(
                arguments.tool_layer,
                arguments.cargo_home,
                arguments.cache_acceptance,
                arguments.builder_input_root,
            )
        except Exception as error:
            cause = getattr(error, "code", "internal")
            raise DeliveryError(
                "builder-preflight-failed",
                f"Builder configuration preflight failed ({cause}): {str(error)[:3072]}",
                stage="builder",
            ) from error
    if required:
        raise DeliveryError(
            "builder-not-configured",
            "Builder preflight requires --builder-input-root, --tool-layer, --cargo-home, and --cache-acceptance",
            stage="builder",
        )
    return UnconfiguredBuilderRunner()


def _build_controller(arguments: argparse.Namespace) -> DeliveryController:
    codeagent = CodeAgentStage(
        adapter_path=arguments.codeagent_adapter,
        cloud_agent_path=arguments.cloud_agent,
        provider_id=arguments.provider,
        model=arguments.model,
        acknowledge_external_cost=arguments.acknowledge_external_cost,
    )
    runner = _build_runner(
        arguments,
        required=arguments.command == "run" or getattr(arguments, "run", False),
    )
    return DeliveryController(
        arguments.root,
        codeagent,
        runner,
        wasm_tools=arguments.wasm_tools,
        appstore_root=arguments.appstore_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable VibApp automatic delivery controller")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--codeagent-adapter", type=Path, default=CODEAGENT_DIR / "codeagent_adapter.py")
    parser.add_argument("--cloud-agent", type=Path, default=CLOUD_DIR / "cloud_agent.py")
    parser.add_argument("--provider", default="codex")
    parser.add_argument("--model", required=True)
    parser.add_argument("--acknowledge-external-cost", action="store_true")
    parser.add_argument("--builder-input-root", type=Path, default=REPO / "generated")
    parser.add_argument("--tool-layer", type=Path)
    parser.add_argument("--cargo-home", type=Path)
    parser.add_argument("--cache-acceptance", type=Path)
    parser.add_argument("--wasm-tools", type=Path, default=DEFAULT_WASM_TOOLS)
    parser.add_argument("--appstore-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    submit = commands.add_parser("submit")
    submit.add_argument("--task", type=Path, required=True)
    submit.add_argument("--registry-no-match", type=Path, required=True)
    submit.add_argument("--explicit-user-submit", action="store_true")
    submit.add_argument("--retry-task-id")
    submit.add_argument("--edited-from-task-id")
    submit.add_argument("--run", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("task_id")
    run.add_argument("--attempt-id")
    run.add_argument("--resume-source-handoff", action="store_true", help="Explicitly recover the current failed attempt from its unchanged source-ready handoff; never invokes CodeAgent")
    status = commands.add_parser("status")
    status.add_argument("task_id")
    history = commands.add_parser("history")
    history.add_argument("task_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "preflight":
            result = delivery_readiness_preflight(
                _build_runner(arguments, required=True),
                arguments.wasm_tools,
            )
            print(canonical_json({"ok": True, "result": result}).decode())
            return 0
        controller = _build_controller(arguments)
        if arguments.command == "submit":
            result = controller.submit(
                _read_argument(arguments.task),
                _read_argument(arguments.registry_no_match),
                explicit_user_submit=arguments.explicit_user_submit,
                retry_task_id=arguments.retry_task_id,
                edited_from_task_id=arguments.edited_from_task_id,
            )
            if arguments.run:
                result = controller.run_attempt(result["task_id"], result["attempt_id"])
        elif arguments.command == "run":
            result = controller.run_attempt(arguments.task_id, arguments.attempt_id, resume_source_handoff=arguments.resume_source_handoff)
        elif arguments.command == "status":
            result = controller.status(arguments.task_id)
        else:
            result = controller.history(arguments.task_id)
    except (DeliveryError, PipelineError, StoreError, OSError, ValueError) as error:
        code = getattr(error, "code", "internal")
        stage = getattr(error, "stage", "controller")
        print(
            canonical_json(
                {"ok": False, "stage": stage, "code": code, "message": str(error)[:4096]}
            ).decode(),
            file=sys.stderr,
        )
        return 1
    print(canonical_json({"ok": True, "result": result}).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
