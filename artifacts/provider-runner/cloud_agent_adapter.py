"""Adapter from artifacts/cloud-agent ProviderRunner Protocol to this runner.

The adapter has no default credential-handle broker or production backend.  Merely
constructing it cannot enable live work.
"""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import secrets
import shutil
from typing import Any, Callable, Protocol

from provider_runner import (
    ExternalOneJobProviderRunner,
    Limits,
    ProviderReceiptV3,
    RunnerError,
    bundle_digest,
    validate_provider_runner_request_envelope,
)


class CredentialHandleBroker(Protocol):
    def issue_single_use_handle(
        self,
        job_id: str,
        attempt_id: str,
        immutable_task_digest_sha256: str,
        provider_execution_identity_sha256: str,
    ) -> bytes: ...

    def close_single_use_handle(
        self, handle: bytes, job_id: str, attempt_id: str, outcome: str
    ) -> None: ...


class UnavailableCredentialHandleBroker:
    def issue_single_use_handle(
        self,
        job_id: str,
        attempt_id: str,
        immutable_task_digest_sha256: str,
        provider_execution_identity_sha256: str,
    ) -> bytes:
        raise RunnerError("credential-broker-unavailable", "no opaque credential-handle broker is configured")

    def close_single_use_handle(
        self, handle: bytes, job_id: str, attempt_id: str, outcome: str
    ) -> None:
        raise RunnerError("credential-broker-unavailable", "no opaque credential-handle broker is configured")


def _remove_private_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        path.unlink()
        return
    for current, directories, files in os.walk(path, topdown=False):
        for name in files:
            (Path(current) / name).chmod(0o600)
        for name in directories:
            (Path(current) / name).chmod(0o700)
    path.chmod(0o700)
    shutil.rmtree(path)


def _validate_exact_model_binding(request: Any) -> None:
    model = getattr(request, "model", None)
    if (
        getattr(request, "provider", None) != "openai-codex"
        or not isinstance(model, str)
        or not 1 <= len(model) <= 256
        or model.strip() != model
        or model.startswith("-")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in model)
    ):
        raise RunnerError("model-binding-required", "runner request lacks an explicit authorized model")
    positions = [index for index, value in enumerate(request.command) if value == "--model"]
    if len(positions) != 1 or positions[0] + 1 >= len(request.command):
        raise RunnerError("model-mismatch", "runner command must carry exactly one explicit --model")
    if request.command[positions[0] + 1] != model:
        raise RunnerError("model-mismatch", "runner command model differs from the authorized request")


class CloudAgentProviderRunnerAdapter:
    def __init__(
        self,
        service: ExternalOneJobProviderRunner,
        receipt_factory: Callable[..., Any],
        credential_broker: CredentialHandleBroker | None = None,
    ):
        self.service = service
        self.receipt_factory = receipt_factory
        self.credential_broker = credential_broker or UnavailableCredentialHandleBroker()

    def preflight(self, required_policy: dict[str, Any]) -> dict[str, Any]:
        return self.service.preflight(required_policy)

    def execute(self, request: Any) -> Any:
        execution_attempt, _ = validate_provider_runner_request_envelope(request)
        _validate_exact_model_binding(request)
        self.service.preflight(request.isolation_policy)
        workspace = Path(request.workspace).resolve(strict=True)
        parent = workspace.parent
        returned = parent / f".{workspace.name}.runner-output-{secrets.token_hex(8)}"
        limits = Limits(
            wall_time_seconds=request.limits["wall_time_seconds"],
            cpu_seconds=request.limits["cpu_seconds"],
            rss_bytes=request.limits["memory_bytes"],
            pids=request.limits["pids"],
            disk_bytes=request.limits["workspace_bytes"],
            stdout_bytes=request.limits["stdout_bytes"],
            stderr_bytes=request.limits["stderr_bytes"],
            output_files=min(512, request.limits["source_files"] + 32),
        )
        claimed = self.service.begin(
            request_schema_version=request.schema_version,
            request_document_type=request.document_type,
            job_id=request.job_id,
            execution_attempt=execution_attempt,
            immutable_task_digest_sha256=request.immutable_task_digest_sha256,
            provider_execution_identity=request.provider_execution_identity,
            provider_execution_identity_sha256=request.provider_execution_identity_sha256,
            input_root=workspace,
            expected_input_bundle_sha256=request.input_bundle_sha256,
            executable_path=Path(request.executable_identity["resolved_path"]),
            command=list(request.command),
            prompt=request.prompt,
            limits=limits,
            isolation_policy=request.isolation_policy,
            expected_isolation_policy_sha256=request.isolation_policy_sha256,
            returned_output_root=returned,
        )
        try:
            handle = self.credential_broker.issue_single_use_handle(
                request.job_id,
                execution_attempt["attempt_id"],
                request.immutable_task_digest_sha256,
                request.provider_execution_identity_sha256,
            )
        except Exception:
            self.service.abandon_claim(claimed)
            raise
        handle_closed = False
        try:
            receipt: ProviderReceiptV3 = self.service.execute_claimed(claimed, handle)
            backup = parent / f".{workspace.name}.runner-input-{secrets.token_hex(8)}"
            transfer_complete = False
            try:
                os.replace(workspace, backup)
                os.replace(returned, workspace)
                transferred_digest = bundle_digest(
                    workspace,
                    claimed.limits.output_files,
                    claimed.limits.disk_bytes,
                )
                if transferred_digest != receipt.output_bundle_sha256:
                    raise RunnerError(
                        "bundle-mutated",
                        "atomically transferred output differs from the signed immutable snapshot",
                    )
                transfer_complete = True
            except Exception:
                if backup.exists():
                    _remove_private_tree(workspace)
                    os.replace(backup, workspace)
                raise
            finally:
                if transfer_complete:
                    _remove_private_tree(backup)
                _remove_private_tree(returned)
            created = self.receipt_factory(**asdict(receipt))
            handle_closed = True
            self.credential_broker.close_single_use_handle(
                handle,
                request.job_id,
                execution_attempt["attempt_id"],
                "success-consumed",
            )
            return created
        except Exception:
            close_error: Exception | None = None
            if not handle_closed:
                handle_closed = True
                try:
                    self.credential_broker.close_single_use_handle(
                        handle,
                        request.job_id,
                        execution_attempt["attempt_id"],
                        "failed-revoke-or-consume",
                    )
                except Exception as error:
                    close_error = error
            _remove_private_tree(returned)
            if close_error is not None:
                raise RunnerError(
                    "credential-handle-close-failed",
                    "broker could not confirm failed-job handle revocation/consumption",
                ) from close_error
            raise
