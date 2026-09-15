"""Bounded Docker source executor. No workload network, mounts or credentials.

The existing LauncherService is the transport-neutral control API. This adapter
implements its execution seam; Kubernetes can implement the same three methods.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import random
import re
import selectors
import shutil
import stat
import subprocess
import threading
import time
import tomllib
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.request import getproxies

from codeagent_launcher import (
    BackendLaunch, BackendObservation, BackendState, BackendSuccess, ContractError,
    ExecutorKind, ExecutorUnavailable, LaunchRequest, ProviderProfile,
    RECEIPT_SCHEMA_VERSION, SuccessReceipt,
)
import host_budget

IMAGE = "vibapp-codeagent-codex:local"
OPENCODE_IMAGE = "vibapp-codeagent-opencode:local"
LOCALAI_MODEL = "qwen3.8-27b-uncensored-mtp-q4"
LOCALAI_ENDPOINT = "http://192.168.199.170:8081/v1/chat/completions"
MAX_FRAME = 6 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_REQUESTS = 48
POLICY = {
    "version": "vibapp.docker-source-v1", "network": "none", "mounts": [],
    "provider_uid": 1000, "trusted_relay_uid": 0, "rootfs": "read-only",
    "capabilities": ["SETUID", "SETGID", "KILL", "CHOWN", "DAC_OVERRIDE"], "no_new_privileges": True,
    "credential_delivery": "host-only-stdio-responses-relay",
    "max_model_requests": MAX_REQUESTS, "max_response_bytes": MAX_RESPONSE,
    "model_request_budget": {"total": 48, "authoring": 40, "repair": 8},
    "max_compiler_repairs": 2, "compiler_authority": "separate-offline-builder",
    "stream_socket_timeout_seconds": 300,
    "connect_socket_timeout_seconds": 15,
    "max_pre_request_connect_attempts": 2,
    "transient_http_retry": {
        "statuses": [429, 502, 503, 504], "max_attempts_per_request": 3,
        # A long, otherwise healthy authoring session can encounter several
        # isolated rejections. Keep a job-wide cap (never reset on success or
        # repair) as well as the per-request cap and the shared 48/40/8 budget.
        "max_retries_per_job": 8, "max_delay_seconds": 30,
        "scope": "same-session-before-downstream-response-only",
        "uncertain_send_or_stream_replay": False,
        "all_attempts_consume_model_request_budget": True,
    },
    "https_proxy_policy": "host-configured-loopback-http-connect-only",
}
LOCALAI_RELAY_POLICY = {
    "mode": "localai-native-json-to-sse-v1", "upstream_stream": False, "downstream_stream": True,
    "reason": "fixed-localai-stream-tool-truncation", "tool_names": "preserve-native-sdk-case-repair",
}
OPENCODE_POLICY = {
    **{key: value for key, value in POLICY.items() if key != "model_request_budget"},
    "credential_delivery": "host-only-stdio-chat-relay",
    "max_model_requests": 64, "max_messages": 256,
    "provider": "opencode", "provider_version": "1.18.27",
    "upstream_endpoint": LOCALAI_ENDPOINT, "upstream_model": LOCALAI_MODEL,
    "context_tokens": 32768, "max_output_tokens": 16384, "max_request_bytes": 256 * 1024,
    "completion_socket_timeout_seconds": 300,
    "relay_policy": LOCALAI_RELAY_POLICY,
    "transient_http_retry": {"enabled": False},
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: bytes):
    return hashlib.sha256(value).hexdigest()


class DockerError(RuntimeError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


FAILURE_DIAGNOSTIC_SCHEMA = "vibapp.docker-failure-diagnostic-v1"
FAILURE_ORIGINS = frozenset({"provider-process", "provider-spawn", "provider-output-limit", "bridge-process",
                           "host-relay", "host-control", "host-deadline", "host-cancellation", "host-protocol", "unknown"})
PROVIDER_ERROR_CATEGORIES = frozenset({"stream-decode", "stream-disconnected", "authentication", "rate-limit",
                                     "thread-resource", "process-failed", "unknown", "none"})
CHILD_SIGNALS = frozenset({"SIGABRT", "SIGBUS", "SIGFPE", "SIGHUP", "SIGILL", "SIGINT", "SIGKILL", "SIGPIPE",
                          "SIGQUIT", "SIGSEGV", "SIGTERM", "SIGTRAP", "SIGXCPU", "SIGXFSZ", "other"})


def empty_failure_diagnostic(origin="unknown"):
    return {"schema_version": FAILURE_DIAGNOSTIC_SCHEMA, "failure_origin": origin,
            "provider_error_category": "unknown", "child_exit_code": None, "child_signal": None,
            "stdout_bytes": None, "stderr_bytes": None, "output_limit_exceeded": None,
            "frame_limit_exceeded": None, "bridge_stdout_bytes": None, "bridge_stderr_bytes": None,
            "container_exit_code": None, "container_oom_killed": None, "container_running": None}


def validate_failure_diagnostic(value):
    """Closed, informational observations only; never raw errors or retry authority."""
    invalid = DockerError("docker-failure-diagnostic-invalid")
    if type(value) is not dict or set(value) != set(empty_failure_diagnostic()):
        raise invalid
    for field, choices in (("schema_version", {FAILURE_DIAGNOSTIC_SCHEMA}), ("failure_origin", FAILURE_ORIGINS),
                           ("provider_error_category", PROVIDER_ERROR_CATEGORIES)):
        if type(value[field]) is not str or value[field] not in choices:
            raise invalid
    if value["child_signal"] is not None and (type(value["child_signal"]) is not str or value["child_signal"] not in CHILD_SIGNALS):
        raise invalid
    for field, maximum in (("child_exit_code", 255), ("container_exit_code", 255),
                           *((field, MAX_RESPONSE) for field in ("stdout_bytes", "stderr_bytes", "bridge_stdout_bytes", "bridge_stderr_bytes"))):
        if value[field] is not None and (type(value[field]) is not int or not 0 <= value[field] <= maximum):
            raise invalid
    for field in ("output_limit_exceeded", "frame_limit_exceeded", "container_oom_killed", "container_running"):
        if value[field] is not None and type(value[field]) is not bool:
            raise invalid
    return dict(value)


def budget_snapshot(*, phase="authoring", authoring_used=0, repair_used=0):
    policy = POLICY["model_request_budget"]
    used = {"total": authoring_used + repair_used, "authoring": authoring_used, "repair": repair_used}
    return {"schema_version": "vibapp.model-request-budget-v1", "phase": phase,
            **{f"{key}_limit": limit for key, limit in policy.items()},
            **{f"{key}_used": count for key, count in used.items()},
            **{f"{key}_remaining": policy[key] - count for key, count in used.items()}}


def validate_model_request_budget(value, model_requests):
    expected = {"schema_version", "phase", *(f"{phase}_{field}" for phase in ("total", "authoring", "repair")
                                             for field in ("limit", "used", "remaining"))}
    if (type(value) is not dict or set(value) != expected
            or value.get("schema_version") != "vibapp.model-request-budget-v1"
            or type(value.get("phase")) is not str or value["phase"] not in {"authoring", "repair"}
            or type(model_requests) is not int or not 0 <= model_requests <= 64):
        raise DockerError("docker-model-request-budget-invalid")
    for key in expected - {"schema_version", "phase"}:
        if type(value[key]) is not int or not 0 <= value[key] <= 64:
            raise DockerError("docker-model-request-budget-invalid")
    for phase in ("total", "authoring", "repair"):
        if value[f"{phase}_limit"] < 1 or value[f"{phase}_used"] + value[f"{phase}_remaining"] != value[f"{phase}_limit"]:
            raise DockerError("docker-model-request-budget-invalid")
    if (value["total_limit"] != value["authoring_limit"] + value["repair_limit"]
            or value["total_used"] != value["authoring_used"] + value["repair_used"]
            or value["total_used"] != model_requests
            or (value["phase"] == "authoring" and value["repair_used"] != 0)):
        raise DockerError("docker-model-request-budget-invalid")
    return dict(value)


def format_budget_notice(snapshot=None):
    value = snapshot if snapshot is not None else budget_snapshot()
    return ("Trusted VibApp execution budget: "
            f"{value['total_limit']} total model HTTP attempts; {value['authoring_limit']} for initial authoring "
            f"and {value['repair_limit']} shared across compiler repairs. Retries also consume the current phase allowance. "
            f"Current phase: {value['phase']}; used: {value['total_used']} total, "
            f"{value['authoring_used']} authoring, {value['repair_used']} repair; remaining: "
            f"{value['authoring_remaining']} authoring, {value['repair_remaining']} repair, {value['total_remaining']} total. "
            "Finish physical source and end the author turn within the current phase allowance; do not spend calls on "
            "redundant inventories or completion manifests. Compiler checks start only after successful author exit. "
            "Unused repair allowance cannot extend initial authoring; exhausted budgets do not authorize continuation.")


class TransientHTTPError(DockerError):
    """No response bytes or tool outputs have been delivered to the author."""
    def __init__(self, status, retry_after=None):
        super().__init__("provider-rate-limited" if status == 429 else "provider-upstream-unavailable")
        self.status, self.retry_after = status, retry_after


def docker_binary():
    for candidate in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", "/usr/bin/docker"):
        if Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise DockerError("docker-unavailable")


def docker_command(arguments, *, timeout=15):
    # The Docker client is trusted control-plane code. Its environment is never
    # propagated into the workload, which has no host mounts or Docker socket.
    try:
        result = subprocess.run([docker_binary(), *arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DockerError("docker-control-unavailable") from error
    if len(result.stdout) > 128 * 1024 or len(result.stderr) > 16 * 1024:
        raise DockerError("docker-control-output-limit")
    return result


def image_identity(image=IMAGE, *, provider="codex"):
    if provider not in {"codex", "opencode"}:
        raise DockerError("docker-provider-unsupported")
    result = docker_command(["image", "inspect", "--format", "{{.Id}}", image])
    image_id = result.stdout.decode().strip()
    if result.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise DockerError("docker-image-unavailable", "Build the CodeAgent Docker image first")
    # No provider request, credentials or network are involved in this probe.
    probe = docker_command(["run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                            "--security-opt=no-new-privileges", "--memory=256m", "--pids-limit=32",
                            image_id, "--identity"])
    if probe.returncode:
        raise DockerError("docker-image-identity-unavailable")
    try:
        identity = json.loads(probe.stdout)
        if not isinstance(identity, dict):
            raise ValueError()
        expected_keys = {"version", "bundle_sha256", "bridge_sha256"}
        if provider == "opencode":
            expected_keys.add("relay_policy")
            if canonical(identity.get("relay_policy")) != canonical(LOCALAI_RELAY_POLICY):
                raise ValueError()
        if set(identity) != expected_keys:
            raise ValueError()
        pattern = r"codex-cli \d+\.\d+\.\d+" if provider == "codex" else r"1\.18\.27"
        if not re.fullmatch(pattern, identity["version"]):
            raise ValueError()
        if any(not re.fullmatch(r"[a-f0-9]{64}", identity[key]) for key in ("bundle_sha256", "bridge_sha256")):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as error:
        raise DockerError("docker-image-identity-invalid") from error
    # An immutable image ID alone does not mean it contains this release's
    # workspace/relay fixes. Reject stale trusted bridge bytes before reading
    # credentials or spending a model request. This does not pin a Codex CLI
    # version: its version and bundle retain the existing compatibility policy.
    bridge_root = Path(__file__).resolve().parent / "docker"
    try:
        bridge = (bridge_root / "entry.mjs").read_bytes()
        if provider == "opencode":
            bridge += (bridge_root / "opencode-provider.mjs").read_bytes()
    except OSError as error:
        raise DockerError("docker-bridge-unavailable", "CodeAgent bridge source is unavailable") from error
    if identity["bridge_sha256"] != digest(bridge):
        raise DockerError("docker-image-stale", "Rebuild the selected CodeAgent Docker image; its workspace/relay code is out of date")
    policy = POLICY if provider == "codex" else OPENCODE_POLICY
    return {**identity, "image_id": image_id, "policy_sha256": digest(canonical(policy))}


def recover_job(state_root):
    """Cancel a previously reserved execution, never resubmit a spent consent."""
    path = state_root / "docker-job.json"
    if not path.exists():
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_mode & 0o022 or metadata.st_size > 8192:
            raise DockerError("docker-recovery-record-invalid")
        record = json.loads(os.read(descriptor, 8193))
    finally:
        os.close(descriptor)
    execution = record.get("backend_execution_id", "")
    if not re.fullmatch(r"vibapp-[0-9a-f]{64}", execution) or record.get("container_name") != "vibapp-ca-" + digest(execution.encode())[:32]:
        raise DockerError("docker-recovery-record-invalid")
    executor = DockerExecutor(image_id=record["image_id"], input_payload={}, gateway=None, limits={}, state_root=state_root)
    if not executor._cleanup(record["container_name"], execution):
        raise DockerError("docker-cleanup-unconfirmed")
    (state_root / "docker-recovery.json").write_bytes(canonical({"backend_execution_id": execution, "cleanup_confirmed": True, "resubmitted": False}))


def codex_https_proxy():
    """Use an existing local OS/env proxy; never accept routing from a job."""
    value = getproxies().get("https")
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError()
        return {"host": parsed.hostname, "port": parsed.port}
    except (ValueError, TypeError):
        raise DockerError("codex-proxy-unsupported", "Only an existing unauthenticated loopback HTTP CONNECT proxy is supported") from None


def codex_connection(*, include_secret=False):
    """Read only the selected model connection, never plugins/MCP/user rules.

    Secret bytes may be parsed by the trusted host but are never returned from
    preflight, recorded, or delivered to the container.
    """
    codex_root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    configuration = codex_root / "config.toml"
    config = {}
    if configuration.exists():
        metadata = configuration.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_size > 1024 * 1024 or metadata.st_mode & 0o022:
            raise DockerError("codex-config-invalid")
        config = tomllib.loads(configuration.read_text())
    selected = config.get("model_provider", "openai")
    if selected != "openai":
        provider = config.get("model_providers", {}).get(selected, {})
        endpoint = provider.get("base_url", "").rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or provider.get("wire_api", "responses") != "responses":
            raise DockerError("codex-endpoint-invalid", "Docker relay requires an exact HTTPS Responses endpoint")
        token = provider.get("experimental_bearer_token") or os.environ.get(provider.get("env_key", ""))
        if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(ord(character) < 32 for character in token):
            raise DockerError("codex-auth-unavailable", "Selected custom provider needs its own scoped bearer credential")
        public = {"kind": "https", "endpoint": endpoint, "auth_kind": "custom-api-key", "provider": selected,
                  "https_proxy": codex_https_proxy()}
        return public, token if include_secret else None
    auth_path = codex_root / "auth.json"
    try:
        metadata = auth_path.lstat()
    except OSError as error:
        raise DockerError("codex-auth-unavailable", "Log into Codex or configure a custom Responses provider") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_size > 1024 * 1024 or metadata.st_mode & 0o022:
        raise DockerError("codex-auth-invalid")
    return {"kind": "provider-managed", "endpoint": "provider-managed", "auth_kind": "codex-login", "provider": "openai",
            "https_proxy": codex_https_proxy()}, auth_path


class OpenAIGateway:
    """Host-only credential use, exact destination and model, no redirects."""
    def __init__(self, model: str):
        public, secret = codex_connection(include_secret=True)
        self.model = model
        self.custom_endpoint = public["endpoint"] if public["kind"] == "https" else None
        self.https_proxy = public.get("https_proxy")
        self.observed = False
        self.requests = 0
        self.budget_phase = "authoring"
        self.phase_requests = {"authoring": 0, "repair": 0}
        self.logical_requests = 0
        self.model_retries = 0
        self.request_history = []
        self.progress_callback = None
        self.last_status = None
        self.last_error = None
        self.last_request = None
        self.connection = None
        self.response_socket = None
        if self.custom_endpoint:
            self.chatgpt, self.account, self.token = False, None, secret
            return
        auth_path = secret
        descriptor = os.open(auth_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or metadata.st_mode & 0o022 or metadata.st_size > 1024 * 1024:
                raise DockerError("codex-auth-invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                auth = json.load(stream)
        finally:
            os.close(descriptor)
        tokens = auth.get("tokens") or {}
        api_key = auth.get("OPENAI_API_KEY")
        self.chatgpt = not bool(api_key)
        self.token = tokens.get("access_token") if self.chatgpt else api_key
        self.account = tokens.get("account_id") if self.chatgpt else None
        if not isinstance(self.token, str) or not self.token or len(self.token) > 16384:
            raise DockerError("codex-auth-unavailable")

    def close(self):
        connection = self.connection
        # HTTPConnection detaches its socket for Connection: close responses;
        # HTTPResponse can still be blocked reading that same socket afterwards.
        active_socket = getattr(self, "response_socket", None)
        if active_socket is None and connection is not None:
            active_socket = connection.sock
        if active_socket is not None:
            try:
                active_socket.shutdown(2)
            except OSError:
                pass
        if connection is not None:
            connection.close()

    def connect(self, connection, cancelled, read_timeout):
        # Retry only TCP/TLS/CONNECT establishment, before any model POST bytes.
        # A response/write failure is never replayed: its upstream effect is unknown.
        for attempt in range(POLICY["max_pre_request_connect_attempts"]):
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            if isinstance(getattr(self, "last_request", None), dict):
                self.last_request["connect_attempts"] = attempt + 1
            try:
                connection.connect()
                self.response_socket = connection.sock
                if cancelled.is_set():
                    raise DockerError("provider-cancelled")
                self.response_socket.settimeout(read_timeout)
                return
            except OSError:
                connection.close()
                self.response_socket = None
                if attempt + 1 == POLICY["max_pre_request_connect_attempts"]:
                    raise
                if cancelled.wait(.25):
                    raise DockerError("provider-cancelled")

    def budget_snapshot(self):
        return budget_snapshot(phase=self.budget_phase, authoring_used=self.phase_requests["authoring"],
                               repair_used=self.phase_requests["repair"])

    def set_phase(self, phase):
        if phase not in {"authoring", "repair"} or (self.budget_phase == "repair" and phase != "repair"):
            raise DockerError("provider-budget-phase-invalid")
        self.budget_phase = phase

    def _budget_available(self):
        return (self.requests < MAX_REQUESTS
                and self.phase_requests[self.budget_phase] < POLICY["model_request_budget"][self.budget_phase])

    def report_progress(self, state, retry=None):
        callback = self.progress_callback
        if callback is not None:
            value = {"state": state, "model_requests": self.requests,
                     "logical_model_requests": self.logical_requests, "model_retries": self.model_retries,
                     "model_request_budget": self.budget_snapshot()}
            if retry is not None:
                value["retry"] = retry
            try:
                callback(value)
            except Exception:
                pass  # Optional progress evidence cannot invent a network failure.

    @staticmethod
    def retry_delay(header, attempt):
        policy = POLICY["transient_http_retry"]
        delay = 2 ** (attempt - 1) + random.uniform(0, .25)
        if isinstance(header, str) and len(header) <= 128:
            try:
                requested = int(header) if re.fullmatch(r"[0-9]+", header) else (
                    parsedate_to_datetime(header).timestamp() - time.time())
                delay = max(delay, requested)
            except (ValueError, TypeError, OverflowError):
                pass
        # Do not disregard a long Retry-After by retrying sooner than requested.
        return round(delay, 3) if delay <= policy["max_delay_seconds"] else None

    def relay(self, message, emit, cancelled):
        """Recover explicit transient HTTP rejection inside the existing session.

        This endpoint only generates model output; tool execution stays in the
        existing Codex process. A retry is allowed only before any response has
        been forwarded. It may incur another model charge, within the already
        consent-bound request budget. Never retry an uncertain send or a partial
        stream, restart the author, or replay its completed tool calls.
        """
        if cancelled.is_set():
            self.last_error = "provider-cancelled"
            raise DockerError(self.last_error)
        if not self._budget_available():
            raise DockerError("provider-request-budget-exhausted")
        policy = POLICY["transient_http_retry"]
        for attempt in range(1, policy["max_attempts_per_request"] + 1):
            if cancelled.is_set():
                self.last_error = "provider-cancelled"
                raise DockerError(self.last_error)
            try:
                self._relay_once(message, emit, cancelled, retry=attempt > 1)
                self.report_progress("authoring")
                return
            except TransientHTTPError as error:
                delay = self.retry_delay(error.retry_after, attempt)
                if (attempt == policy["max_attempts_per_request"]
                        or self.model_retries >= policy["max_retries_per_job"]
                        or not self._budget_available() or delay is None):
                    raise DockerError(error.code) from error
                self.report_progress("model-retry", {
                    "attempt": attempt + 1, "max_attempts": policy["max_attempts_per_request"],
                    "http_status": error.status, "delay_seconds": delay, "reason": error.code,
                })
                if cancelled.wait(delay):
                    self.last_error = "provider-cancelled"
                    raise DockerError(self.last_error) from error

    def _relay_once(self, message, emit, cancelled, *, retry=False):
        request_id = message["id"]
        # A failure before headers must not report the previous request's HTTP200.
        self.last_status = self.last_error = None
        if not self._budget_available():
            raise DockerError("provider-request-budget-exhausted")
        try:
            body = base64.b64decode(message["body"], validate=True)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError()
            request = json.loads(body)
            if request.get("model") != self.model or request.get("stream") is not True:
                raise ValueError()
            if request.get("instructions") is not None and not isinstance(request["instructions"], str):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as error:
            raise DockerError("provider-relay-request-invalid") from error
        # Never accept destination, method or headers from the workload.
        custom = urlsplit(self.custom_endpoint) if self.custom_endpoint else None
        host = custom.hostname if custom else "chatgpt.com" if self.chatgpt else "api.openai.com"
        route = custom.path + "/responses" if custom else "/backend-api/codex/responses" if self.chatgpt else "/v1/responses"
        request["store"] = False
        if not self.chatgpt:
            request["max_output_tokens"] = min(request.get("max_output_tokens", 8192), 8192)
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "text/event-stream", "User-Agent": "vibapp-codeagent-relay/1",
                   "originator": "codex_cli_rs"}
        if self.account:
            headers["ChatGPT-Account-Id"] = self.account
        connection = http.client.HTTPSConnection(
            self.https_proxy["host"] if self.https_proxy else host,
            port=self.https_proxy["port"] if self.https_proxy else custom.port if custom else None,
            timeout=POLICY["connect_socket_timeout_seconds"],
        )
        # Explicit connect() owns attempts. Never let request() silently reopen
        # a socket closed by cancellation between connect and the model POST.
        connection.auto_open = 0
        if self.https_proxy:
            # HTTPSConnection wraps the CONNECT tunnel with origin TLS/SNI and
            # ordinary certificate validation. No bearer credential in CONNECT.
            connection.set_tunnel(host, port=custom.port if custom and custom.port else 443)
        self.connection = connection
        started = time.monotonic()
        self.last_request = {"ordinal": self.requests, "phase": "connect", "bytes": 0}
        try:
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            self.last_request["phase"] = "connect"
            self.connect(connection, cancelled, POLICY["stream_socket_timeout_seconds"])
            self.last_request["phase"] = "send-request"
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            next_snapshot = budget_snapshot(phase=self.budget_phase,
                authoring_used=self.phase_requests["authoring"] + (self.budget_phase == "authoring"),
                repair_used=self.phase_requests["repair"] + (self.budget_phase == "repair"))
            notice = format_budget_notice(next_snapshot)
            request["instructions"] = ((request.get("instructions") or "") + "\n\n" + notice).lstrip("\n")
            wire_body = canonical(request)
            # Count only admitted POST attempts, including an uncertain failed send.
            # Validation, refused capacity and cancelled/pre-connect failures do not
            # invent a model call. Retries remain within the same phase and total.
            self.requests += 1
            self.phase_requests[self.budget_phase] += 1
            if retry:
                self.model_retries += 1
            else:
                self.logical_requests += 1
            self.last_request["ordinal"] = self.requests
            try:
                connection.request("POST", route, body=wire_body, headers=headers)
            finally:
                self.report_progress("authoring")
            self.response_socket = connection.sock
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            self.last_request["phase"] = "response-headers"
            response = connection.getresponse()
            self.observed = True
            self.last_status = response.status
            if response.status in POLICY["transient_http_retry"]["statuses"]:
                # Close the rejected response without forwarding untrusted error
                # prose or draining an unbounded error body. Keep the author and
                # its workspace alive while the trusted host decides recovery.
                self.last_error = "provider-rate-limited" if response.status == 429 else "provider-upstream-unavailable"
                self.last_request["phase"] = "http-rejected"
                raise TransientHTTPError(response.status, response.getheader("Retry-After"))
            if response.status != 200:
                # Errors are classified without exposing credentials, requests or
                # arbitrary provider response prose in logs/status documents.
                self.last_error = "provider-rate-limited" if response.status == 429 else "provider-authentication-failed" if response.status in (401, 403) else "provider-upstream-rejected"
            emit({"type": "response-head", "id": request_id, "status": response.status,
                  "content_type": "text/event-stream" if response.status == 200 else "application/json"})
            size = 0
            self.last_request["phase"] = "response-body"
            while not cancelled.is_set():
                chunk = response.read1(16384)
                if not chunk:
                    # read1() may return EOF without IncompleteRead for a short
                    # Content-Length response. Do not bless a truncated stream.
                    if type(response.length) is int and response.length > 0:
                        raise http.client.IncompleteRead(b"")
                    break
                size += len(chunk)
                self.last_request["bytes"] = size
                if size > MAX_RESPONSE:
                    raise DockerError("provider-response-limit")
                emit({"type": "response-chunk", "id": request_id, "body": base64.b64encode(chunk).decode()})
            if cancelled.is_set():
                self.last_error = "provider-cancelled"
                raise DockerError(self.last_error)
            emit({"type": "response-end", "id": request_id})
            self.last_request["phase"] = "complete"
        except (TimeoutError, OSError, http.client.HTTPException) as error:
            # Closed classifications only; exception prose can contain secrets.
            self.last_error = "provider-cancelled" if cancelled.is_set() else (
                "provider-timeout" if isinstance(error, TimeoutError) else "provider-network-error"
            )
            self.last_request["transport_error"] = (
                "timeout" if isinstance(error, TimeoutError) else
                "http-framing" if isinstance(error, http.client.HTTPException) else "connection"
            )
            raise DockerError(self.last_error) from error
        finally:
            self.last_request["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.request_history.append({**self.last_request, "http_status": self.last_status,
                                         "error": self.last_error})
            connection.close()
            self.connection = None
            self.response_socket = None


class LocalAIGateway(OpenAIGateway):
    """Fixed credential-free native JSON upstream, SSE downstream; no fallback.

    The client's context setting is 32768, not a measurement or enforcement of
    the shared server window. Byte, message, tool, output and call counts are
    bounded here. Token-overflow responses never switch models or server config.
    """
    def __init__(self, model, *, evidence_root=None):
        if model not in {LOCALAI_MODEL, "vibapp/" + LOCALAI_MODEL}:
            raise DockerError("provider-model-unsupported")
        self.model = LOCALAI_MODEL
        self.evidence_root = evidence_root
        self.observed, self.requests = False, 0
        self.last_status = self.last_error = self.connection = None
        self.response_socket = None

    def validate_request(self, message):
        try:
            body = base64.b64decode(message["body"], validate=True)
            if len(body) > OPENCODE_POLICY["max_request_bytes"]:
                raise ValueError()
            request = self._json(body)
            allowed = {"model", "messages", "tools", "tool_choice", "parallel_tool_calls", "stream",
                       "stream_options", "max_tokens", "temperature", "top_p", "stop", "seed",
                       "presence_penalty", "frequency_penalty"}
            if not isinstance(request, dict) or set(request) - allowed or request.get("model") != self.model or request.get("stream") is not True:
                raise ValueError()
            messages = request.get("messages")
            if not isinstance(messages, list) or not 1 <= len(messages) <= OPENCODE_POLICY["max_messages"]:
                raise ValueError()
            for item in messages:
                if not isinstance(item, dict) or set(item) - {"role", "content", "tool_calls", "tool_call_id", "name"} or item.get("role") not in {"system", "user", "assistant", "tool"}:
                    raise ValueError()
                content = item.get("content")
                if isinstance(content, list):
                    if len(content) > 32 or any(not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "text" or not isinstance(part["text"], str) for part in content):
                        raise ValueError()
                elif content is not None and not isinstance(content, str):
                    raise ValueError()
                if len(canonical(content)) > 96 * 1024:
                    raise ValueError()
                for key in ("name", "tool_call_id"):
                    if key in item and (not isinstance(item[key], str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,256}", item[key])):
                        raise ValueError()
                calls = item.get("tool_calls", [])
                if not isinstance(calls, list) or len(calls) > 32:
                    raise ValueError()
                for call in calls:
                    if not isinstance(call, dict) or set(call) != {"id", "type", "function"} or call["type"] != "function" or not isinstance(call["id"], str) or len(call["id"]) > 256:
                        raise ValueError()
                    function = call["function"]
                    if not isinstance(function, dict) or set(function) != {"name", "arguments"} or not isinstance(function["arguments"], str) or len(function["arguments"]) > 96 * 1024 or not isinstance(function["name"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", function["name"]):
                        raise ValueError()
            tools = request.get("tools", [])
            if not isinstance(tools, list) or len(tools) > 48:
                raise ValueError()
            for tool in tools:
                if not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool["type"] != "function" or not isinstance(tool["function"], dict):
                    raise ValueError()
                function = tool["function"]
                if set(function) - {"name", "description", "parameters", "strict"} or not isinstance(function.get("name"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", function["name"]) or not isinstance(function.get("parameters"), dict):
                    raise ValueError()
                if "description" in function and (not isinstance(function["description"], str) or len(function["description"]) > 16384):
                    raise ValueError()
                if "strict" in function and not isinstance(function["strict"], bool):
                    raise ValueError()
            choice = request.get("tool_choice", "auto")
            if isinstance(choice, dict):
                if set(choice) != {"type", "function"} or choice["type"] != "function" or not isinstance(choice["function"], dict) or set(choice["function"]) != {"name"} or choice["function"]["name"] not in [tool["function"]["name"] for tool in tools]:
                    raise ValueError()
            elif choice not in {"auto", "none", "required"}:
                raise ValueError()
            if "stream_options" in request and (not isinstance(request["stream_options"], dict) or set(request["stream_options"]) != {"include_usage"} or not isinstance(request["stream_options"]["include_usage"], bool)):
                raise ValueError()
            if "parallel_tool_calls" in request and not isinstance(request["parallel_tool_calls"], bool):
                raise ValueError()
            for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1), ("presence_penalty", -2, 2), ("frequency_penalty", -2, 2)):
                if key in request and (type(request[key]) not in {int, float} or not low <= request[key] <= high):
                    raise ValueError()
            if "seed" in request and (type(request["seed"]) is not int or abs(request["seed"]) > 2**31 - 1):
                raise ValueError()
            if "stop" in request and (not isinstance(request["stop"], list) or len(request["stop"]) > 4 or any(not isinstance(value, str) or len(value) > 256 for value in request["stop"])):
                raise ValueError()
            output = request.get("max_tokens", OPENCODE_POLICY["max_output_tokens"])
            if type(output) is not int or not 1 <= output <= OPENCODE_POLICY["max_output_tokens"]:
                raise ValueError()
            request["max_tokens"] = output
            return request
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise DockerError("provider-relay-request-invalid") from error

    def _evidence(self, name, value):
        if self.evidence_root is None:
            return
        # The host-owned execution directory is outside every workload mount.
        descriptor = os.open(self.evidence_root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))

    @staticmethod
    def _json(value):
        def closed_pairs(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError()
                result[key] = item
            return result
        def invalid_constant(_):
            raise ValueError()
        return json.loads(value, object_pairs_hook=closed_pairs, parse_constant=invalid_constant)

    @staticmethod
    def _minimum_tool_input(value, schema, depth=0):
        """Check required fields/types without replacing the SDK's full schema gate."""
        if depth > 16 or not isinstance(schema, dict):
            raise ValueError()
        kind = schema.get("type")
        predicates = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
                      "string": lambda: isinstance(value, str), "integer": lambda: type(value) is int,
                      "number": lambda: type(value) in {int, float}, "boolean": lambda: type(value) is bool,
                      "null": lambda: value is None}
        if kind is not None and (kind not in predicates or not predicates[kind]()):
            raise ValueError()
        if isinstance(value, dict):
            required, properties = schema.get("required", []), schema.get("properties", {})
            if not isinstance(required, list) or not isinstance(properties, dict) or any(not isinstance(key, str) or key not in value for key in required):
                raise ValueError()
            for key, item in value.items():
                if key in properties:
                    LocalAIGateway._minimum_tool_input(item, properties[key], depth + 1)
        elif isinstance(value, list) and "items" in schema:
            if len(value) > 1024:
                raise ValueError()
            for item in value:
                LocalAIGateway._minimum_tool_input(item, schema["items"], depth + 1)

    def native_response(self, raw, request):
        """Accept native calls only, preserving argument bytes and tool spelling.

        OpenCode1.18.27 already repairs a capitalized offered tool name; this
        adapter never invents aliases, derives arguments, or executes prose/XML.
        """
        self.protocol_diagnostic = None
        phase = "response-json"
        finish = None
        try:
            response = self._json(raw)
            phase = "single-choice"
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ValueError()
            choice = choices[0]
            if type(choice.get("index")) is not int or choice["index"] != 0:
                raise ValueError()
            message, finish = choice["message"], choice["finish_reason"]
            phase = "assistant-message"
            if not isinstance(message, dict) or message.get("role") != "assistant" or finish not in {"stop", "tool_calls", "length"}:
                raise ValueError()
            content = message.get("content")
            if content is not None and (not isinstance(content, str) or len(content.encode()) > 96 * 1024):
                raise ValueError()
            calls = message.get("tool_calls", [])
            if calls is None:
                calls = []
            phase = "finish-tool-consistency"
            if not isinstance(calls, list) or len(calls) > 32 or bool(calls) != (finish == "tool_calls"):
                raise ValueError()
            if not content and not calls:
                raise ValueError()
            offered = [item["function"] for item in request.get("tools", [])]
            normalized, summaries, seen = [], [], set()
            for index, call in enumerate(calls):
                phase = "tool-identity"
                if not isinstance(call, dict) or not {"id", "type", "function"}.issubset(call) or set(call) - {"id", "type", "function", "index"} or call["type"] != "function" or not isinstance(call["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,256}", call["id"]) or call["id"] in seen:
                    raise ValueError()
                # LocalAI4.9 complete JSON can attach index=0 to every native
                # call. Array position is authoritative here, unlike upstream
                # SSE fragments. Normalize only this observed zero/default or
                # the exact position; IDs/arguments still validate separately.
                if "index" in call and (type(call["index"]) is not int or call["index"] not in (0, index)):
                    raise ValueError()
                seen.add(call["id"])
                function = call["function"]
                phase = "offered-tool"
                if not isinstance(function, dict) or set(function) != {"name", "arguments"} or not isinstance(function["name"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", function["name"]):
                    raise ValueError()
                matches = [tool for tool in offered if tool["name"].lower() == function["name"].lower()]
                if len(matches) != 1 or not isinstance(function["arguments"], str) or len(function["arguments"].encode()) > 96 * 1024:
                    raise ValueError()
                phase = "arguments-json"
                arguments = self._json(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError()
                phase = "arguments-schema"
                self._minimum_tool_input(arguments, matches[0]["parameters"])
                normalized.append({**call, "index": index})
                summaries.append({"name": function["name"], "arguments_sha256": digest(function["arguments"].encode()), "argument_keys": sorted(arguments)})
            delta = {"role": "assistant"}
            if content:
                delta["content"] = content
            if normalized:
                delta["tool_calls"] = normalized
            # No reasoning/reasoning_content/analysis field crosses this seam.
            events = []
            for item in ({"index": 0, "delta": delta, "finish_reason": None}, {"index": 0, "delta": {}, "finish_reason": finish}):
                events.append(b"data: " + canonical({"id": "vibapp-localai-native", "object": "chat.completion.chunk", "created": 0, "model": self.model, "choices": [item]}) + b"\n\n")
            stream = b"".join(events) + b"data: [DONE]\n\n"
            if len(stream) > MAX_RESPONSE:
                raise ValueError()
            return stream, {"finish_reason": finish, "tool_calls": summaries, "content_bytes": len((content or "").encode())}
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            self.protocol_diagnostic = {"validation_stage": phase,
                "finish_reason": finish if finish in ("stop", "tool_calls", "length") else "invalid-or-absent"}
            raise DockerError("provider-upstream-protocol-invalid") from error

    def relay(self, message, emit, cancelled):
        self.requests += 1
        self.protocol_diagnostic = None
        if self.requests > OPENCODE_POLICY["max_model_requests"]:
            raise DockerError("provider-request-limit")
        request = self.validate_request(message)
        # Explicit compatibility policy, not a retry/fallback. LocalAI's native
        # complete JSON tool calls work where its streaming tool parser truncates.
        request["stream"] = False
        request.pop("stream_options", None)
        request_id = message["id"]
        body = canonical(request)
        self._evidence(f"upstream-request-{self.requests:02d}.json", {
            "endpoint": LOCALAI_ENDPOINT, "model": self.model, "request_sha256": digest(body), "request": request,
            "completion_socket_timeout_seconds": OPENCODE_POLICY["completion_socket_timeout_seconds"],
            **LOCALAI_RELAY_POLICY,
        })
        # A complete native response can legitimately take longer than a stream's
        # first chunk. This ceiling is identity-bound; outer deadline/cancel still
        # calls close(), whose socket shutdown interrupts blocked getresponse().
        connection = http.client.HTTPConnection("192.168.199.170", port=8081,
                                                timeout=OPENCODE_POLICY["connect_socket_timeout_seconds"])
        connection.auto_open = 0
        self.connection = connection
        size, response_hash, summary = 0, hashlib.sha256(), {}
        try:
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            self.connect(connection, cancelled, OPENCODE_POLICY["completion_socket_timeout_seconds"])
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            connection.request("POST", "/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "vibapp-codeagent-localai-relay/1",
            })
            self.response_socket = connection.sock
            if cancelled.is_set():
                raise DockerError("provider-cancelled")
            response = connection.getresponse()
            self.observed, self.last_status = True, response.status
            if response.status != 200:
                self.last_error = "provider-rate-limited" if response.status == 429 else "provider-upstream-rejected"
                # No redirect following or arbitrary upstream error prose.
                emit({"type": "response-head", "id": request_id, "status": 502, "content_type": "application/json"})
                emit({"type": "response-chunk", "id": request_id, "body": base64.b64encode(canonical({"error": {"message": self.last_error}})).decode()})
            else:
                if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise DockerError("provider-upstream-protocol-invalid")
                chunks = []
                while not cancelled.is_set():
                    chunk = response.read1(16384)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise DockerError("provider-response-limit")
                    response_hash.update(chunk)
                    chunks.append(chunk)
                if cancelled.is_set():
                    raise DockerError("provider-cancelled")
                stream, summary = self.native_response(b"".join(chunks), request)
                emit({"type": "response-head", "id": request_id, "status": 200, "content_type": "text/event-stream"})
                for offset in range(0, len(stream), 16384):
                    emit({"type": "response-chunk", "id": request_id, "body": base64.b64encode(stream[offset:offset + 16384]).decode()})
            emit({"type": "response-end", "id": request_id})
        finally:
            connection.close()
            self.connection = None
            self.response_socket = None
            # Raw response/reasoning is deliberately not persisted or shown.
            self._evidence(f"upstream-response-{self.requests:02d}.json", {
                "status": self.last_status, "bytes": size, "sha256": response_hash.hexdigest(), "cancelled": cancelled.is_set(),
                "completion_socket_timeout_seconds": OPENCODE_POLICY["completion_socket_timeout_seconds"],
                **LOCALAI_RELAY_POLICY, **summary,
                **({"protocol_error": self.protocol_diagnostic} if self.protocol_diagnostic else {}),
            })


class DockerExecutor:
    kind = ExecutorKind.DOCKER

    def __init__(self, *, image_id: str, input_payload: dict, gateway, limits: dict, state_root: Path, source_checker=None):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ContractError("docker-image-must-be-immutable")
        self.image_id, self.input_payload, self.gateway = image_id, input_payload, gateway
        self.limits, self.state_root = limits, state_root
        self.jobs = {}
        self.failure_code = None
        self.failure_diagnostic = None
        self.source_checker = source_checker

    def _capture_failure_diagnostic(self, name, execution, origin, reported, stdout_bytes, stderr_bytes):
        value = empty_failure_diagnostic(origin)
        if reported is not None:
            try:
                value = validate_failure_diagnostic(reported)
            except DockerError:
                pass
        # Host classifications and inspected container state cannot be overridden
        # by workload-supplied fields. Unavailable evidence stays explicitly unknown.
        if origin != "provider-process":
            value["failure_origin"] = origin
        for key in ("container_exit_code", "container_running", "container_oom_killed"):
            value[key] = None
        value["bridge_stdout_bytes"] = min(stdout_bytes, MAX_RESPONSE) if type(stdout_bytes) is int else None
        value["bridge_stderr_bytes"] = min(stderr_bytes, MAX_RESPONSE) if type(stderr_bytes) is int else None
        try:
            container = self._inspect(name)
            labels = container.get("Config", {}).get("Labels", {}) if isinstance(container, dict) else {}
            if (container and container.get("Image") == self.image_id
                    and labels.get("ai.vibapp.execution") == execution
                    and labels.get("ai.vibapp.owner") == str(os.getuid())
                    and labels.get("ai.vibapp.role") == "codeagent"):
                state = container.get("State", {})
                code = state.get("ExitCode")
                if state.get("Running") is False and type(code) is int and 0 <= code <= 255:
                    value["container_exit_code"] = code
                for source, target in (("Running", "container_running"), ("OOMKilled", "container_oom_killed")):
                    if type(state.get(source)) is bool:
                        value[target] = state[source]
        except Exception:
            pass  # Optional evidence cannot prevent exact cleanup or leak exceptions.
        self.failure_diagnostic = validate_failure_diagnostic(value)

    def _inspect(self, name):
        result = docker_command(["container", "inspect", name])
        if result.returncode:
            if b"No such container" in result.stderr or b"No such object" in result.stderr:
                return None
            raise DockerError("docker-cleanup-unconfirmed")
        return json.loads(result.stdout)[0]

    @staticmethod
    def _reservations(directory):
        """Durable reservations survive a lost create reply or author process.

        A creating reservation with no visible container is uncertain, not free.
        It requires later exact-container cleanup; never expire it based on a PID
        or age while the Docker daemon might still finish the original request.
        """
        reservations = {}
        for filename in os.listdir(directory):
            if filename == "execution.lock":
                continue
            matched = re.fullmatch(r"(vibapp-ca-[0-9a-f]{32})\.(creating|created)", filename)
            if not matched or matched[1] in reservations:
                raise DockerError("docker-reservation-invalid")
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                metadata = os.fstat(descriptor)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or metadata.st_nlink != 1 or metadata.st_mode & 0o077 or metadata.st_size > 8192):
                    raise DockerError("docker-reservation-invalid")
                record = json.loads(os.read(descriptor, 8193))
            finally:
                os.close(descriptor)
            if (not isinstance(record, dict) or set(record) != {"execution", "image_id"}
                    or not isinstance(record["execution"], str)
                    or not re.fullmatch(r"vibapp-[0-9a-f]{64}", record["execution"])
                    or not isinstance(record["image_id"], str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["image_id"])
                    or matched[1] != "vibapp-ca-" + digest(record["execution"].encode())[:32]):
                raise DockerError("docker-reservation-invalid")
            reservations[matched[1]] = {**record, "phase": matched[2], "filename": filename}
        return reservations

    @staticmethod
    def _owned_containers():
        existing = docker_command([
            "ps", "--all", "--format", "{{.Names}}", "--filter", "label=ai.vibapp.role=codeagent",
            "--filter", f"label=ai.vibapp.owner={os.getuid()}"])
        if existing.returncode:
            raise DockerError("docker-capacity-unavailable")
        try:
            names = existing.stdout.decode("ascii").splitlines()
        except UnicodeDecodeError as error:
            raise DockerError("docker-capacity-unavailable") from error
        if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,255}", name) for name in names):
            raise DockerError("docker-capacity-unavailable")
        return set(names)

    def _create_reserved(self, name, execution, args, cancellation, wait_seconds):
        try:
            with host_budget.lease("docker-admission", cancellation=cancellation, wait_seconds=wait_seconds):
                with host_budget.private_directory("docker-admission") as directory:
                    reservations = self._reservations(directory)
                    if name in reservations or self._inspect(name) is not None:
                        raise DockerError("docker-execution-already-exists")
                    # Includes created, running, paused, exited and dead workers,
                    # plus creates still in flight when their caller disappeared.
                    if len(self._owned_containers() | reservations.keys()) >= host_budget.AUTHOR_LIMIT:
                        raise DockerError("docker-capacity-unavailable")
                    if cancellation.is_set():
                        raise DockerError("provider-cancelled")
                    filename = name + ".creating"
                    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                    try:
                        record = canonical({"execution": execution, "image_id": self.image_id})
                        if os.write(descriptor, record) != len(record):
                            raise DockerError("docker-reservation-invalid")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.fsync(directory)
                    created = docker_command(args)
                    if created.returncode:
                        raise DockerError("docker-create-failed")
                    os.rename(filename, name + ".created", src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
        except host_budget.BudgetError as error:
            raise DockerError("docker-capacity-unavailable" if error.code == "local-capacity-busy" else error.code) from error

    def _release_reservation(self, name, execution, *, container_observed):
        with host_budget.lease("docker-admission", wait_seconds=30):
            with host_budget.private_directory("docker-admission") as directory:
                record = self._reservations(directory).get(name)
                if record is None:
                    return True  # Legacy execution, or rejected before reserving.
                if record["execution"] != execution or record["image_id"] != self.image_id:
                    raise DockerError("docker-container-identity-mismatch")
                if record["phase"] == "creating" and not container_observed:
                    return False  # An uncertain daemon request cannot be replayed.
                os.unlink(record["filename"], dir_fd=directory)
                os.fsync(directory)
                return True

    def _cleanup(self, name, expected):
        container = self._inspect(name)
        if container is None:
            return self._release_reservation(name, expected, container_observed=False)
        labels = container.get("Config", {}).get("Labels", {})
        if (labels.get("ai.vibapp.execution") != expected
                or labels.get("ai.vibapp.owner") != str(os.getuid())
                or labels.get("ai.vibapp.role") != "codeagent"
                or container.get("Image") != self.image_id):
            raise DockerError("docker-container-identity-mismatch")
        result = docker_command(["container", "rm", "--force", container["Id"]])
        return (result.returncode == 0 and self._inspect(name) is None
                and self._release_reservation(name, expected, container_observed=True))

    def launch(self, request: LaunchRequest, profile: ProviderProfile, backend_execution_id: str):
        if backend_execution_id in self.jobs:
            raise ContractError("docker-execution-already-reserved")
        if profile.image_digest_sha256 != self.image_id.removeprefix("sha256:"):
            raise ContractError("docker-profile-image-mismatch")
        if request.input_digest_sha256 != digest(canonical(self.input_payload["files"])) or request.prompt_digest_sha256 != digest(self.input_payload["prompt"].encode()):
            raise ContractError("docker-input-digest-mismatch")
        name = "vibapp-ca-" + digest(backend_execution_id.encode())[:32]
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {"backend_execution_id": backend_execution_id, "container_name": name,
                  "image_id": self.image_id, "request_digest_sha256": request.canonical_digest_sha256()}
        # Reserve before create. A lost response never allocates a replacement.
        with (self.state_root / "docker-job.json").open("x", encoding="utf8") as stream:
            json.dump(record, stream)
        job = {"cancelled": threading.Event(), "observation": BackendObservation(backend_execution_id, BackendState.RUNNING), "name": name}
        self.jobs[backend_execution_id] = job
        worker = threading.Thread(target=self._run, args=(request, profile, backend_execution_id, job), daemon=False)
        job["thread"] = worker
        worker.start()
        return BackendLaunch(backend_execution_id)

    def _run(self, request, profile, execution_id, job):
        process = None
        gateway_thread = None
        compiler_thread = None
        compiler_checks = 0
        trace_bytes = 0
        stdout_size = stderr_size = None
        failure_origin = "host-control"
        reported_failure = None
        selector = selectors.DefaultSelector()
        outgoing = queue.Queue(maxsize=32)
        gateway_errors = queue.Queue(maxsize=1)
        result_bytes = None
        succeeded = False
        cleanup = False
        started = time.monotonic()
        deadline = started + min(self.limits["wall_time_seconds"], 1800)
        progress_lock = threading.Lock()
        def emit(value):
            while not job["cancelled"].is_set():
                try:
                    outgoing.put(canonical(value) + b"\n", timeout=.1)
                    return
                except queue.Full:
                    continue
        def relay(message):
            try:
                self.gateway.relay(message, emit, job["cancelled"])
            except Exception as error:
                gateway_errors.put(("host-relay", getattr(error, "code", "provider-network-error")))
        def progress(value):
            # Same owner-only execution directory as terminal evidence. Atomic
            # replacement avoids readers seeing half-written JSON during retry.
            temporary = self.state_root / ".docker-progress.pending"
            if isinstance(self.gateway, OpenAIGateway) and not isinstance(self.gateway, LocalAIGateway):
                value = {**value, "logical_model_requests": self.gateway.logical_requests,
                         "model_retries": self.gateway.model_retries, "model_request_budget": self.gateway.budget_snapshot()}
            with progress_lock:
                temporary.write_bytes(canonical({**value, "compiler_checks": compiler_checks}))
                temporary.replace(self.state_root / "docker-progress.json")
        if isinstance(self.gateway, OpenAIGateway) and not isinstance(self.gateway, LocalAIGateway):
            self.gateway.progress_callback = progress
        def check_source(message):
            try:
                feedback = self.source_checker(message, job["cancelled"], max(0, deadline - time.monotonic()))
                emit({"type": "compiler-feedback", "id": message["id"], **feedback})
            except Exception as error:
                gateway_errors.put(("host-control", getattr(error, "code", "source-check-failed")))
        try:
            memory = min(self.limits["memory_bytes"], 2 * 1024**3)
            disk = min(self.limits["workspace_bytes"], 64 * 1024**2)
            scratch = disk // 2
            args = ["create", "--name", job["name"], "--label", "ai.vibapp.execution=" + execution_id,
                    "--label", "ai.vibapp.role=codeagent", "--label", f"ai.vibapp.owner={os.getuid()}", "--interactive", "--init", "--network=none", "--read-only",
                    "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=KILL", "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE",
                    "--security-opt=no-new-privileges", "--memory", str(memory), "--memory-swap", str(memory),
                    "--cpus=2", "--pids-limit", str(min(self.limits["pids"], 64)), "--log-driver=none",
                    "--tmpfs", f"/tmp:rw,nosuid,nodev,size={scratch},mode=1777",
                    "--tmpfs", f"/work:rw,nosuid,nodev,size={disk - scratch},mode=755", self.image_id]
            self._create_reserved(job["name"], execution_id, args, job["cancelled"],
                                  min(30, max(0, deadline - time.monotonic())))
            if job["cancelled"].is_set():
                failure_origin = "host-cancellation"
                raise DockerError("provider-cancelled")
            process = subprocess.Popen([docker_binary(), "start", "--attach", "--interactive", job["name"]],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            stdout_size = stderr_size = 0
            failure_origin = "host-protocol"
            pending = bytearray(canonical({"type": "start", **self.input_payload,
                                          "wall_time_seconds": min(self.limits["wall_time_seconds"], 1800),
                                          "cpu_seconds": min(self.limits["cpu_seconds"], 1800),
                                          "compiler_feedback": self.source_checker is not None}) + b"\n")
            if len(pending) > MAX_FRAME:
                raise DockerError("docker-input-limit")
            buffered = bytearray()
            while True:
                if job["cancelled"].is_set():
                    failure_origin = "host-cancellation"
                    raise DockerError("provider-cancelled")
                if time.monotonic() >= deadline:
                    failure_origin = "host-deadline"
                    raise DockerError("provider-timeout")
                if not gateway_errors.empty():
                    failure_origin, failure_code = gateway_errors.get_nowait()
                    raise DockerError(failure_code)
                if not pending:
                    try:
                        pending.extend(outgoing.get_nowait())
                    except queue.Empty:
                        pass
                if pending:
                    try:
                        count = os.write(process.stdin.fileno(), pending[:65536])
                        del pending[:count]
                    except BlockingIOError:
                        pass
                for key, _ in selector.select(.02):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        stderr_size += len(chunk)
                        if stderr_size > 65536:
                            raise DockerError("docker-stderr-limit")
                        continue
                    stdout_size += len(chunk)
                    buffered.extend(chunk)
                    if len(buffered) > MAX_FRAME:
                        raise DockerError("docker-output-limit")
                    while b"\n" in buffered:
                        line, _, rest = buffered.partition(b"\n")
                        buffered = bytearray(rest)
                        message = json.loads(line)
                        if message.get("type") == "model-request":
                            # Gateway callback reports admitted counts, never the
                            # speculative next request rejected by a hard ceiling.
                            progress({"state": "authoring", "model_requests": self.gateway.requests})
                            if gateway_thread is not None:
                                gateway_thread.join(timeout=.1)
                            if gateway_thread is not None and gateway_thread.is_alive():
                                raise DockerError("provider-parallel-request-denied")
                            gateway_thread = threading.Thread(target=relay, args=(message,), daemon=False)
                            gateway_thread.start()
                        elif message.get("type") == "provider-trace":
                            trace = canonical(message) + b"\n"
                            trace_bytes += len(trace)
                            if len(trace) > 16384 or trace_bytes > 512 * 1024:
                                raise DockerError("provider-trace-limit")
                            with (self.state_root / "provider-trace.jsonl").open("ab") as stream:
                                stream.write(trace)
                        elif message.get("type") == "source-candidate":
                            if compiler_thread is not None:
                                compiler_thread.join(timeout=.1)
                            if self.source_checker is None or message.get("id") != compiler_checks or compiler_checks >= 3 or (compiler_thread is not None and compiler_thread.is_alive()):
                                raise DockerError("provider-compiler-protocol-invalid")
                            compiler_checks += 1
                            if isinstance(self.gateway, OpenAIGateway) and not isinstance(self.gateway, LocalAIGateway):
                                self.gateway.set_phase("repair")
                            progress({"state": "compiler-feedback", "model_requests": self.gateway.requests})
                            compiler_thread = threading.Thread(target=check_source, args=(message,), daemon=False)
                            compiler_thread.start()
                        elif message.get("type") == "result":
                            if result_bytes is not None:
                                raise DockerError("provider-duplicate-result")
                            result_bytes = canonical(message)
                            if len(result_bytes) > profile.max_output_bytes:
                                raise DockerError("provider-output-limit")
                        elif message.get("type") == "failed":
                            failure_origin = "provider-process"
                            reported_failure = message.get("failure_diagnostic")
                            raise DockerError(message.get("code") if message.get("code") in {"provider-resource-limit", "source-check-failed", "provider-failed", "provider-timeout", "provider-rate-limited", "provider-authentication-failed", "provider-output-invalid", "provider-spawn-failed", "provider-protocol-invalid"} else "provider-failed")
                        else:
                            raise DockerError("provider-protocol-invalid")
                if process.poll() is not None and not selector.get_map():
                    if process.returncode != 0 or result_bytes is None or not self.gateway.observed:
                        failure_origin = "bridge-process"
                        raise DockerError("provider-failed")
                    container = self._inspect(job["name"])
                    if not container or container["State"]["Running"] or container["State"]["ExitCode"] != 0 or container["State"].get("OOMKilled"):
                        failure_origin = "bridge-process"
                        raise DockerError("provider-container-failed")
                    succeeded = True
                    break
        except Exception as error:
            self.failure_code = getattr(error, "code", "docker-execution-failed")
            if self.failure_code == "provider-failed" and self.gateway.last_error:
                self.failure_code = self.gateway.last_error
                failure_origin = "host-relay"
        finally:
            # Stop upstream work immediately on the terminal cause. Optional
            # inspection may wait on Docker, but still precedes destructive cleanup.
            job["cancelled"].set()
            self.gateway.close()
            if not succeeded:
                self._capture_failure_diagnostic(job["name"], execution_id, failure_origin, reported_failure,
                                                 stdout_size, stderr_size)
            try:
                cleanup = self._cleanup(job["name"], execution_id)
                if not cleanup:
                    self.failure_code = "docker-cleanup-unconfirmed"
            except Exception:
                self.failure_code = "docker-cleanup-unconfirmed"
            if gateway_thread:
                gateway_thread.join(timeout=35)
            if compiler_thread:
                compiler_thread.join(timeout=15)
            if process:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup = False
                    self.failure_code = "docker-cleanup-unconfirmed"
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
            selector.close()
            cleanup = cleanup and (gateway_thread is None or not gateway_thread.is_alive()) and (compiler_thread is None or not compiler_thread.is_alive())
            state = BackendState.SUCCEEDED if succeeded else BackendState.CANCELLED if self.failure_code == "provider-cancelled" else BackendState.FAILED
            success = None
            if succeeded and cleanup:
                receipt = SuccessReceipt(
                    schema_version=RECEIPT_SCHEMA_VERSION, job_id=request.job_id, attempt_id=request.attempt_id,
                    idempotency_key=request.idempotency_key, request_digest_sha256=request.canonical_digest_sha256(),
                    backend_execution_id=execution_id, executor_kind=self.kind,
                    provider_profile_id=profile.provider_profile_id, provider_profile_digest_sha256=profile.provider_profile_digest_sha256,
                    provider_id=request.provider_id, model=request.model, image_digest_sha256=profile.image_digest_sha256,
                    resource_policy_id=profile.resource_policy_id, resource_policy_digest_sha256=profile.resource_policy_digest_sha256,
                    network_policy_id=profile.network_policy_id, network_policy_digest_sha256=profile.network_policy_digest_sha256,
                    task_digest_sha256=request.task_digest_sha256, input_digest_sha256=request.input_digest_sha256,
                    prompt_digest_sha256=request.prompt_digest_sha256, output_digest_sha256=digest(result_bytes),
                    output_media_type=profile.output_media_type, output_size_bytes=len(result_bytes),
                    cleanup_confirmed=True, whole_job_quiescent=True)
                success = BackendSuccess(result_bytes, profile.output_media_type, receipt)
                try:
                    (self.state_root / "docker-receipt.json").write_bytes(canonical(asdict(receipt)))
                except OSError:
                    # Persistence failure must not leave a finished thread marked
                    # RUNNING forever or grant success without its durable receipt.
                    self.failure_code = "docker-receipt-persistence-failed"
                    state = BackendState.FAILED
                    success = None
            job["observation"] = BackendObservation(execution_id, state, cleanup, cleanup, success)
            try:
                (self.state_root / "docker-terminal.json").write_bytes(canonical({
                    "backend_execution_id": execution_id, "state": state.value,
                    "cleanup_confirmed": cleanup, "failure_code": self.failure_code,
                    "model_requests": self.gateway.requests, "external_request_observed": self.gateway.observed,
                    "upstream_status": self.gateway.last_status, "upstream_error": self.gateway.last_error,
                    "upstream_request": self.gateway.last_request if isinstance(getattr(self.gateway, "last_request", None), dict) else None,
                    "compiler_checks": compiler_checks,
                    "logical_model_requests": getattr(self.gateway, "logical_requests", self.gateway.requests),
                    "model_retries": getattr(self.gateway, "model_retries", 0),
                    "request_history": getattr(self.gateway, "request_history", []),
                    **({"failure_diagnostic": self.failure_diagnostic} if self.failure_diagnostic is not None else {}),
                    **({"model_request_budget": self.gateway.budget_snapshot()}
                       if isinstance(self.gateway, OpenAIGateway) and not isinstance(self.gateway, LocalAIGateway) else {}),
                }))
            except OSError:
                pass  # Terminal observation remains authoritative; no credential data is recorded.

    def status(self, backend_execution_id):
        return self.jobs[backend_execution_id]["observation"]

    def cancel(self, backend_execution_id):
        job = self.jobs[backend_execution_id]
        job["cancelled"].set()
        self.gateway.close()
        job["thread"].join(timeout=1)
        return job["observation"]
