"""Experimental transport-neutral CodeAgent launcher contract.

This module deliberately contains no Docker, Kubernetes, provider, network, or
credential integration.  Executors are trusted server-side adapters selected by a
preconfigured provider profile; callers cannot supply execution primitives.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


SCHEMA_VERSION = "codeagent-launcher.experimental-v0"
RECEIPT_SCHEMA_VERSION = "codeagent-launcher-receipt.experimental-v0"
MAX_REQUEST_BYTES = 8 * 1024
MAX_ID_LENGTH = 128
MAX_MODEL_LENGTH = 256
MAX_BACKEND_EXECUTION_ID_LENGTH = 256
MAX_MEDIA_TYPE_LENGTH = 128
MAX_ABSOLUTE_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_EVENT_PAGE_SIZE = 100
MAX_EVENTS_PER_JOB = 32

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)


class ContractError(ValueError):
    """Base class for a stable launcher contract failure."""

    code = "invalid-request"


class DuplicateKeyError(ContractError):
    code = "duplicate-json-key"


class ConflictError(ContractError):
    code = "conflict"


class NotFoundError(ContractError):
    code = "not-found"


class ResultUnavailableError(ContractError):
    code = "result-unavailable"


class ExecutorUnavailable(RuntimeError):
    """Executor proved no backend execution was created for the requested ID."""


class ExecutorKind(str, Enum):
    DOCKER = "docker"
    KUBERNETES = "kubernetes"


class BackendState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LauncherState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel-requested"
    CLEANUP_PENDING = "cleanup-pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HELD = "held"


class EventPhase(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    QUIESCING = "quiescing"
    TERMINAL = "terminal"


class EventCode(str, Enum):
    JOB_QUEUED = "job-queued"
    EXECUTOR_STARTING = "executor-starting"
    BACKEND_RUNNING = "backend-running"
    CANCEL_REQUESTED = "cancel-requested"
    CLEANUP_PENDING = "cleanup-pending"
    JOB_SUCCEEDED = "job-succeeded"
    JOB_FAILED = "job-failed"
    JOB_CANCELLED = "job-cancelled"
    JOB_HELD = "job-held"


TERMINAL_STATES = frozenset(
    {
        LauncherState.SUCCEEDED,
        LauncherState.FAILED,
        LauncherState.CANCELLED,
        LauncherState.HELD,
    }
)


_SUBMIT_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "attempt_id",
        "idempotency_key",
        "provider_profile_id",
        "provider_id",
        "model",
        "task_digest_sha256",
        "input_digest_sha256",
        "prompt_digest_sha256",
        "resource_policy_id",
        "network_policy_id",
    }
)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field}:expected-string")
    return value


def _require_id(value: Any, field: str) -> str:
    text = _require_string(value, field)
    if len(text) > MAX_ID_LENGTH or _ID_PATTERN.fullmatch(text) is None:
        raise ContractError(f"{field}:invalid-id")
    return text


def _require_model(value: Any) -> str:
    text = _require_string(value, "model")
    if (
        not 1 <= len(text) <= MAX_MODEL_LENGTH
        or text.strip() != text
        or text.startswith("-")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
    ):
        raise ContractError("model:invalid-model-id")
    return text


def _require_digest(value: Any, field: str) -> str:
    text = _require_string(value, field)
    if _DIGEST_PATTERN.fullmatch(text) is None:
        raise ContractError(f"{field}:invalid-sha256")
    return text


def _require_backend_execution_id(value: Any) -> str:
    text = _require_string(value, "backend_execution_id")
    if not text or len(text) > MAX_BACKEND_EXECUTION_ID_LENGTH:
        raise ContractError("backend_execution_id:invalid")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in text):
        raise ContractError("backend_execution_id:invalid")
    return text


def _require_media_type(value: Any) -> str:
    text = _require_string(value, "output_media_type")
    if len(text) > MAX_MEDIA_TYPE_LENGTH or _MEDIA_TYPE_PATTERN.fullmatch(text) is None:
        raise ContractError("output_media_type:invalid")
    return text


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate-json-key:{key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes) -> Any:
    """Parse bounded JSON while rejecting duplicate object keys at every depth."""

    if isinstance(raw, bytes):
        raw_bytes = raw
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ContractError("request:invalid-utf8") from error
    elif isinstance(raw, str):
        text = raw
        try:
            raw_bytes = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ContractError("request:invalid-utf8") from error
    else:
        raise ContractError("request:expected-json-text")
    if len(raw_bytes) > MAX_REQUEST_BYTES:
        raise ContractError("request:too-large")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except DuplicateKeyError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise ContractError("request:malformed-json") from error


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class LaunchRequest:
    schema_version: str
    job_id: str
    attempt_id: str
    idempotency_key: str
    provider_profile_id: str
    provider_id: str
    model: str
    task_digest_sha256: str
    input_digest_sha256: str
    prompt_digest_sha256: str
    resource_policy_id: str
    network_policy_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LaunchRequest":
        if not isinstance(value, Mapping):
            raise ContractError("request:expected-object")
        if any(not isinstance(key, str) for key in value):
            raise ContractError("request:non-string-field-name")
        keys = frozenset(value.keys())
        unknown = sorted(keys - _SUBMIT_FIELDS)
        missing = sorted(_SUBMIT_FIELDS - keys)
        if unknown:
            raise ContractError(f"request:unknown-fields:{','.join(unknown)}")
        if missing:
            raise ContractError(f"request:missing-fields:{','.join(missing)}")
        schema_version = _require_string(value["schema_version"], "schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ContractError("schema_version:unsupported")
        return cls(
            schema_version=schema_version,
            job_id=_require_id(value["job_id"], "job_id"),
            attempt_id=_require_id(value["attempt_id"], "attempt_id"),
            idempotency_key=_require_id(value["idempotency_key"], "idempotency_key"),
            provider_profile_id=_require_id(
                value["provider_profile_id"], "provider_profile_id"
            ),
            provider_id=_require_id(value["provider_id"], "provider_id"),
            model=_require_model(value["model"]),
            task_digest_sha256=_require_digest(
                value["task_digest_sha256"], "task_digest_sha256"
            ),
            input_digest_sha256=_require_digest(
                value["input_digest_sha256"], "input_digest_sha256"
            ),
            prompt_digest_sha256=_require_digest(
                value["prompt_digest_sha256"], "prompt_digest_sha256"
            ),
            resource_policy_id=_require_id(
                value["resource_policy_id"], "resource_policy_id"
            ),
            network_policy_id=_require_id(
                value["network_policy_id"], "network_policy_id"
            ),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "LaunchRequest":
        parsed = strict_json_loads(raw)
        if not isinstance(parsed, dict):
            raise ContractError("request:expected-object")
        return cls.from_mapping(parsed)

    def canonical_digest_sha256(self) -> str:
        return _sha256_bytes(_canonical_json_bytes(asdict(self)))


@dataclass(frozen=True)
class ProviderProfile:
    """Trusted server-side selection; none of these execution facts are caller-set."""

    provider_profile_id: str
    provider_profile_digest_sha256: str
    provider_id: str
    allowed_models: frozenset[str]
    executor_kind: ExecutorKind
    image_digest_sha256: str
    resource_policy_id: str
    resource_policy_digest_sha256: str
    network_policy_id: str
    network_policy_digest_sha256: str
    max_output_bytes: int
    output_media_type: str

    def validate(self) -> None:
        _require_id(self.provider_profile_id, "provider_profile_id")
        _require_digest(
            self.provider_profile_digest_sha256,
            "provider_profile_digest_sha256",
        )
        _require_id(self.provider_id, "provider_id")
        if not self.allowed_models:
            raise ContractError("allowed_models:empty")
        for model in self.allowed_models:
            _require_model(model)
        if not isinstance(self.executor_kind, ExecutorKind):
            raise ContractError("executor_kind:invalid")
        _require_digest(self.image_digest_sha256, "image_digest_sha256")
        _require_id(self.resource_policy_id, "resource_policy_id")
        _require_digest(
            self.resource_policy_digest_sha256,
            "resource_policy_digest_sha256",
        )
        _require_id(self.network_policy_id, "network_policy_id")
        _require_digest(
            self.network_policy_digest_sha256,
            "network_policy_digest_sha256",
        )
        if (
            not isinstance(self.max_output_bytes, int)
            or isinstance(self.max_output_bytes, bool)
            or self.max_output_bytes < 1
            or self.max_output_bytes > MAX_ABSOLUTE_OUTPUT_BYTES
        ):
            raise ContractError("max_output_bytes:out-of-range")
        _require_media_type(self.output_media_type)


@dataclass(frozen=True)
class BackendLaunch:
    backend_execution_id: str


@dataclass(frozen=True)
class SuccessReceipt:
    schema_version: str
    job_id: str
    attempt_id: str
    idempotency_key: str
    request_digest_sha256: str
    backend_execution_id: str
    executor_kind: ExecutorKind
    provider_profile_id: str
    provider_profile_digest_sha256: str
    provider_id: str
    model: str
    image_digest_sha256: str
    resource_policy_id: str
    resource_policy_digest_sha256: str
    network_policy_id: str
    network_policy_digest_sha256: str
    task_digest_sha256: str
    input_digest_sha256: str
    prompt_digest_sha256: str
    output_digest_sha256: str
    output_media_type: str
    output_size_bytes: int
    cleanup_confirmed: bool
    whole_job_quiescent: bool


@dataclass(frozen=True)
class BackendSuccess:
    output: bytes
    output_media_type: str
    receipt: SuccessReceipt


@dataclass(frozen=True)
class BackendObservation:
    backend_execution_id: str
    state: BackendState
    cleanup_confirmed: bool = False
    whole_job_quiescent: bool = False
    success: BackendSuccess | None = None


class Executor(Protocol):
    """Transport-independent trusted executor adapter."""

    kind: ExecutorKind

    def launch(
        self,
        request: LaunchRequest,
        profile: ProviderProfile,
        backend_execution_id: str,
    ) -> BackendLaunch:
        ...

    def status(self, backend_execution_id: str) -> BackendObservation:
        ...

    def cancel(self, backend_execution_id: str) -> BackendObservation:
        ...


class KubernetesExecutor:
    """Reserved remote seam. It intentionally performs no cluster access."""

    kind = ExecutorKind.KUBERNETES
    _ERROR = "kubernetes-executor-unavailable"

    def launch(
        self,
        request: LaunchRequest,
        profile: ProviderProfile,
        backend_execution_id: str,
    ) -> BackendLaunch:
        del request, profile, backend_execution_id
        raise ExecutorUnavailable(self._ERROR)

    def status(self, backend_execution_id: str) -> BackendObservation:
        del backend_execution_id
        raise ExecutorUnavailable(self._ERROR)

    def cancel(self, backend_execution_id: str) -> BackendObservation:
        del backend_execution_id
        raise ExecutorUnavailable(self._ERROR)


@dataclass
class _JobRecord:
    request: LaunchRequest
    request_digest_sha256: str
    profile: ProviderProfile
    state: LauncherState
    backend_execution_id: str | None
    failure_code: str | None = None
    success: BackendSuccess | None = None
    events: list[tuple[int, EventPhase, EventCode]] | None = None
    next_event_sequence: int = 1
    pending_terminal: BackendState | None = None


class LauncherService:
    """Deterministic in-memory contract core for submit/status/cancel/result."""

    def __init__(
        self,
        *,
        profiles: Sequence[ProviderProfile],
        executors: Sequence[Executor],
    ) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, ProviderProfile] = {}
        self._executors: dict[ExecutorKind, Executor] = {}
        self._jobs: dict[tuple[str, str], _JobRecord] = {}
        self._idempotency: dict[str, tuple[str, tuple[str, str]]] = {}

        for profile in profiles:
            profile.validate()
            if profile.provider_profile_id in self._profiles:
                raise ContractError("profiles:duplicate-provider-profile-id")
            self._profiles[profile.provider_profile_id] = profile
        for executor in executors:
            if not isinstance(executor.kind, ExecutorKind):
                raise ContractError("executors:invalid-kind")
            if executor.kind in self._executors:
                raise ContractError("executors:duplicate-kind")
            self._executors[executor.kind] = executor

    def submit_json(self, raw: str | bytes) -> dict[str, Any]:
        return self.submit(LaunchRequest.from_json(raw))

    def submit(self, request: LaunchRequest) -> dict[str, Any]:
        if not isinstance(request, LaunchRequest):
            raise ContractError("request:invalid-type")
        # Reparse the dataclass projection so directly constructed instances cannot
        # bypass the same closed-field and scalar checks used by JSON transports.
        request = LaunchRequest.from_mapping(asdict(request))
        request_digest = request.canonical_digest_sha256()
        job_key = (request.job_id, request.attempt_id)

        with self._lock:
            prior_idempotency = self._idempotency.get(request.idempotency_key)
            if prior_idempotency is not None:
                prior_digest, prior_job_key = prior_idempotency
                if prior_digest != request_digest or prior_job_key != job_key:
                    raise ConflictError("idempotency-key:payload-conflict")
                return self._job_view(self._jobs[prior_job_key])

            prior_job = self._jobs.get(job_key)
            if prior_job is not None:
                raise ConflictError("job-attempt:already-exists")

            profile = self._profiles.get(request.provider_profile_id)
            if profile is None:
                raise ContractError("provider_profile_id:unknown")
            self._validate_request_against_profile(request, profile)
            executor = self._executors.get(profile.executor_kind)
            if executor is None:
                raise ExecutorUnavailable("executor-unavailable")

            record = _JobRecord(
                request=request,
                request_digest_sha256=request_digest,
                profile=profile,
                state=LauncherState.QUEUED,
                backend_execution_id=self._allocate_backend_execution_id(
                    request_digest
                ),
                events=[],
            )
            # Reserve both namespaces before crossing the executor boundary. A lost
            # executor response can therefore never turn a retry into a second run.
            self._jobs[job_key] = record
            self._idempotency[request.idempotency_key] = (request_digest, job_key)
            self._append_event(record, EventPhase.QUEUED, EventCode.JOB_QUEUED)
            record.state = LauncherState.STARTING
            self._append_event(
                record, EventPhase.STARTING, EventCode.EXECUTOR_STARTING
            )
            try:
                launch = executor.launch(
                    request, profile, record.backend_execution_id
                )
                confirmed_execution_id = _require_backend_execution_id(
                    launch.backend_execution_id
                )
                if confirmed_execution_id != record.backend_execution_id:
                    self._quiescence_pending(
                        record, "backend-execution-id-mismatch"
                    )
                    return self._job_view(record)
            except ExecutorUnavailable:
                # This exception class promises that no backend was created.
                record.state = LauncherState.FAILED
                record.failure_code = "executor-unavailable"
                self._append_event(
                    record, EventPhase.TERMINAL, EventCode.JOB_FAILED
                )
                return self._job_view(record)
            except Exception:
                # The exact identity was persisted before launch, so a lost launch
                # response is recoverable through status/cancel under that identity.
                self._quiescence_pending(record, "launch-outcome-unknown")
                return self._job_view(record)

            record.state = LauncherState.RUNNING
            self._append_event(record, EventPhase.RUNNING, EventCode.BACKEND_RUNNING)
            return self._job_view(record)

    def status(self, job_id: str, attempt_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._get_job(job_id, attempt_id)
            if record.state not in TERMINAL_STATES:
                if record.backend_execution_id is None:
                    self._quiescence_pending(
                        record, "missing-backend-execution-id"
                    )
                    return self._job_view(record)
                observation = self._executor(record).status(record.backend_execution_id)
                self._apply_observation(record, observation)
            return self._job_view(record)

    def cancel(self, job_id: str, attempt_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._get_job(job_id, attempt_id)
            if record.state in TERMINAL_STATES:
                return self._job_view(record)
            record.state = LauncherState.CANCEL_REQUESTED
            self._append_event(
                record, EventPhase.QUIESCING, EventCode.CANCEL_REQUESTED
            )
            if record.backend_execution_id is None:
                self._quiescence_pending(
                    record, "missing-backend-execution-id"
                )
                return self._job_view(record)
            observation = self._executor(record).cancel(record.backend_execution_id)
            self._apply_observation(record, observation)
            return self._job_view(record)

    def list_events(
        self,
        job_id: str,
        attempt_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a bounded, cursor-compatible page of closed progress events."""

        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < 0
            or after_sequence > (2**63 - 1)
        ):
            raise ContractError("after_sequence:out-of-range")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_EVENT_PAGE_SIZE
        ):
            raise ContractError("limit:out-of-range")
        with self._lock:
            record = self._get_job(job_id, attempt_id)
            events = record.events or []
            latest_sequence = events[-1][0] if events else 0
            if after_sequence > latest_sequence:
                raise ContractError("after_sequence:unknown-cursor")
            matching = [event for event in events if event[0] > after_sequence]
            page = matching[:limit]
            cursor = page[-1][0] if page else after_sequence
            return {
                "schema_version": SCHEMA_VERSION,
                "job_id": record.request.job_id,
                "attempt_id": record.request.attempt_id,
                "after_sequence": after_sequence,
                "events": [
                    {
                        "sequence": sequence,
                        "phase": phase.value,
                        "code": code.value,
                    }
                    for sequence, phase, code in page
                ],
                "cursor": cursor,
                "has_more": len(matching) > len(page),
            }

    def result(self, job_id: str, attempt_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._get_job(job_id, attempt_id)
            if record.state not in TERMINAL_STATES:
                if record.backend_execution_id is None:
                    raise ResultUnavailableError(
                        "result-unavailable:missing-backend-execution-id"
                    )
                observation = self._executor(record).status(record.backend_execution_id)
                self._apply_observation(record, observation)
            if record.state is not LauncherState.SUCCEEDED or record.success is None:
                raise ResultUnavailableError(f"result-unavailable:{record.state.value}")
            # _apply_observation already validated the complete binding. Keep this
            # invariant explicit at the release point as defense in depth.
            if not self._valid_success(record, record.success):
                self._hold(record, "invalid-terminal-receipt")
                raise ResultUnavailableError("result-unavailable:held")
            return self._result_view(record)

    def _get_job(self, job_id: str, attempt_id: str) -> _JobRecord:
        key = (_require_id(job_id, "job_id"), _require_id(attempt_id, "attempt_id"))
        record = self._jobs.get(key)
        if record is None:
            raise NotFoundError("job-attempt:not-found")
        return record

    def _executor(self, record: _JobRecord) -> Executor:
        executor = self._executors.get(record.profile.executor_kind)
        if executor is None:
            raise ExecutorUnavailable("executor-unavailable")
        return executor

    @staticmethod
    def _allocate_backend_execution_id(request_digest_sha256: str) -> str:
        # The full request digest includes job, attempt and idempotency identity.
        # Executors MUST treat this caller-assigned ID as an idempotent create key.
        return f"vibapp-{request_digest_sha256}"

    @staticmethod
    def _append_event(
        record: _JobRecord, phase: EventPhase, code: EventCode
    ) -> None:
        if record.events is None:
            record.events = []
        if len(record.events) >= MAX_EVENTS_PER_JOB:
            # Exhausting observability budget never fabricates quiescence.
            record.state = LauncherState.CLEANUP_PENDING
            record.failure_code = "event-budget-exhausted"
            record.success = None
            return
        if record.events and record.events[-1][1:] == (phase, code):
            return
        record.events.append((record.next_event_sequence, phase, code))
        record.next_event_sequence += 1

    @staticmethod
    def _validate_request_against_profile(
        request: LaunchRequest, profile: ProviderProfile
    ) -> None:
        if request.provider_id != profile.provider_id:
            raise ContractError("provider_id:profile-mismatch")
        if request.model not in profile.allowed_models:
            raise ContractError("model:profile-mismatch")
        if request.resource_policy_id != profile.resource_policy_id:
            raise ContractError("resource_policy_id:profile-mismatch")
        if request.network_policy_id != profile.network_policy_id:
            raise ContractError("network_policy_id:profile-mismatch")

    def _apply_observation(
        self, record: _JobRecord, observation: BackendObservation
    ) -> None:
        try:
            observed_execution_id = _require_backend_execution_id(
                observation.backend_execution_id
            )
        except ContractError:
            self._quiescence_pending(record, "invalid-backend-observation")
            return
        if observed_execution_id != record.backend_execution_id:
            self._quiescence_pending(
                record, "backend-execution-id-mismatch"
            )
            return
        if not isinstance(observation.state, BackendState):
            self._quiescence_pending(record, "invalid-backend-observation")
            return

        if observation.state is BackendState.RUNNING:
            if observation.success is not None:
                self._quiescence_pending(record, "invalid-backend-observation")
            elif record.pending_terminal is not None:
                self._quiescence_pending(
                    record,
                    "invalid-state-regression",
                    pending_terminal=record.pending_terminal,
                )
            elif record.state is not LauncherState.CANCEL_REQUESTED:
                record.state = LauncherState.RUNNING
                record.failure_code = None
                self._append_event(
                    record, EventPhase.RUNNING, EventCode.BACKEND_RUNNING
                )
            return

        if not isinstance(observation.cleanup_confirmed, bool) or not isinstance(
            observation.whole_job_quiescent, bool
        ):
            self._quiescence_pending(record, "invalid-backend-observation")
            return
        if not (
            observation.cleanup_confirmed and observation.whole_job_quiescent
        ):
            self._quiescence_pending(
                record,
                "cleanup-not-confirmed",
                pending_terminal=observation.state,
            )
            return


        self._append_event(record, EventPhase.QUIESCING, EventCode.CLEANUP_PENDING)

        if observation.state is BackendState.SUCCEEDED:
            if observation.success is None or not self._valid_success(
                record, observation.success
            ):
                self._hold(record, "invalid-terminal-receipt")
                return
            record.success = observation.success
            record.failure_code = None
            record.pending_terminal = None
            record.state = LauncherState.SUCCEEDED
            self._append_event(
                record, EventPhase.TERMINAL, EventCode.JOB_SUCCEEDED
            )
            return

        if observation.success is not None:
            self._hold(record, "invalid-backend-observation")
            return
        record.success = None
        record.pending_terminal = None
        if observation.state is BackendState.FAILED:
            record.failure_code = "backend-failed"
            record.state = LauncherState.FAILED
            self._append_event(record, EventPhase.TERMINAL, EventCode.JOB_FAILED)
        elif observation.state is BackendState.CANCELLED:
            record.failure_code = None
            record.state = LauncherState.CANCELLED
            self._append_event(
                record, EventPhase.TERMINAL, EventCode.JOB_CANCELLED
            )
        else:
            self._hold(record, "invalid-backend-observation")

    @staticmethod
    def _hold(record: _JobRecord, failure_code: str) -> None:
        record.state = LauncherState.HELD
        record.failure_code = failure_code
        record.success = None
        LauncherService._append_event(
            record, EventPhase.TERMINAL, EventCode.JOB_HELD
        )

    @staticmethod
    def _quiescence_pending(
        record: _JobRecord,
        failure_code: str,
        *,
        pending_terminal: BackendState | None = None,
    ) -> None:
        record.state = LauncherState.CLEANUP_PENDING
        record.failure_code = failure_code
        record.success = None
        record.pending_terminal = pending_terminal
        LauncherService._append_event(
            record, EventPhase.QUIESCING, EventCode.CLEANUP_PENDING
        )

    @staticmethod
    def _valid_success(record: _JobRecord, success: BackendSuccess) -> bool:
        if not isinstance(success.output, bytes):
            return False
        if len(success.output) > record.profile.max_output_bytes:
            return False
        if success.output_media_type != record.profile.output_media_type:
            return False
        receipt = success.receipt
        if not isinstance(receipt, SuccessReceipt):
            return False
        if not isinstance(receipt.executor_kind, ExecutorKind):
            return False
        if receipt.cleanup_confirmed is not True:
            return False
        if receipt.whole_job_quiescent is not True:
            return False
        if (
            not isinstance(receipt.output_size_bytes, int)
            or isinstance(receipt.output_size_bytes, bool)
        ):
            return False
        expected = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "job_id": record.request.job_id,
            "attempt_id": record.request.attempt_id,
            "idempotency_key": record.request.idempotency_key,
            "request_digest_sha256": record.request_digest_sha256,
            "backend_execution_id": record.backend_execution_id,
            "executor_kind": record.profile.executor_kind,
            "provider_profile_id": record.profile.provider_profile_id,
            "provider_profile_digest_sha256": (
                record.profile.provider_profile_digest_sha256
            ),
            "provider_id": record.request.provider_id,
            "model": record.request.model,
            "image_digest_sha256": record.profile.image_digest_sha256,
            "resource_policy_id": record.profile.resource_policy_id,
            "resource_policy_digest_sha256": (
                record.profile.resource_policy_digest_sha256
            ),
            "network_policy_id": record.profile.network_policy_id,
            "network_policy_digest_sha256": (
                record.profile.network_policy_digest_sha256
            ),
            "task_digest_sha256": record.request.task_digest_sha256,
            "input_digest_sha256": record.request.input_digest_sha256,
            "prompt_digest_sha256": record.request.prompt_digest_sha256,
            "output_digest_sha256": _sha256_bytes(success.output),
            "output_media_type": success.output_media_type,
            "output_size_bytes": len(success.output),
            "cleanup_confirmed": True,
            "whole_job_quiescent": True,
        }
        return asdict(receipt) == expected

    @staticmethod
    def _job_view(record: _JobRecord) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": record.request.job_id,
            "attempt_id": record.request.attempt_id,
            "request_digest_sha256": record.request_digest_sha256,
            "provider_profile_id": record.profile.provider_profile_id,
            "executor_kind": record.profile.executor_kind.value,
            "backend_execution_id": record.backend_execution_id,
            "state": record.state.value,
            "failure_code": record.failure_code,
        }

    @staticmethod
    def _result_view(record: _JobRecord) -> dict[str, Any]:
        assert record.success is not None
        success = record.success
        receipt = asdict(success.receipt)
        receipt["executor_kind"] = success.receipt.executor_kind.value
        output_digest = _sha256_bytes(success.output)
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": record.request.job_id,
            "attempt_id": record.request.attempt_id,
            "state": LauncherState.SUCCEEDED.value,
            "backend_execution_id": record.backend_execution_id,
            "output": {
                "encoding": "base64",
                "media_type": success.output_media_type,
                "size_bytes": len(success.output),
                "sha256": output_digest,
                "data": base64.b64encode(success.output).decode("ascii"),
            },
            "receipt": receipt,
        }


__all__ = [
    "BackendLaunch",
    "BackendObservation",
    "BackendState",
    "BackendSuccess",
    "ConflictError",
    "ContractError",
    "DuplicateKeyError",
    "Executor",
    "ExecutorKind",
    "ExecutorUnavailable",
    "EventCode",
    "EventPhase",
    "KubernetesExecutor",
    "LaunchRequest",
    "LauncherService",
    "LauncherState",
    "NotFoundError",
    "ProviderProfile",
    "RECEIPT_SCHEMA_VERSION",
    "ResultUnavailableError",
    "SCHEMA_VERSION",
    "SuccessReceipt",
    "strict_json_loads",
]
