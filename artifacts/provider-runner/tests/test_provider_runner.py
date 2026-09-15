from __future__ import annotations

import base64
import copy
from dataclasses import asdict, replace
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_RESULT_SCHEMA = ROOT.parent / "cloud-agent/schemas/provider-result.schema.json"
CLOUD_RUNNER_INTERFACE_SCHEMA = ROOT.parent / "cloud-agent/schemas/provider-runner-interface.schema.json"
RUNNER_INTERFACE_SCHEMA = ROOT / "schemas/provider-runner-interface.schema.json"
sys.path.insert(0, str(ROOT))

from provider_runner import (  # noqa: E402
    ACCEPTED_PROFILE_VERSION,
    AcceptedRunnerProfile,
    BackendResult,
    DESTINATION_POLICY_PATH,
    ExternalOneJobProviderRunner,
    GATEWAY_ATTESTATION_TYPE,
    GATEWAY_RECEIPT_VERSION,
    GatewayReceipt,
    Limits,
    POLICY_PATH,
    PROFILE_TEMPLATE_PATH,
    ProviderReceiptV3,
    REQUEST_VERSION,
    RECEIPT_ATTESTATION_TYPE,
    RECEIPT_VERSION,
    RunnerError,
    SyntheticLifecycleRunner,
    bundle_digest,
    canonical_json,
    default_requested_isolation_policy,
    durable_claim_name,
    gateway_receipt_digest,
    load_strict_json,
    normalize_cloud_agent_command,
    provider_receipt_digest,
    sha256_bytes,
    sha256_file,
    validate_gateway_receipt,
    validate_gateway_destination_policy,
    validate_provider_receipt_v3,
    validate_provider_runner_request_envelope,
    validate_requested_isolation_policy,
)
import provider_runner as provider_runner_module  # noqa: E402
from cloud_agent_adapter import CloudAgentProviderRunnerAdapter, UnavailableCredentialHandleBroker  # noqa: E402


class TestDetachedKey:
    """Synthetic-only keyed oracle; not production key material or an accepted signer."""

    def __init__(self, key_id: str):
        self.key_id = key_id
        self._key = hashlib.sha256(f"synthetic:{key_id}".encode()).digest()

    def sign(self, digest: bytes, key_id: str) -> str:
        if key_id != self.key_id:
            raise ValueError("wrong key")
        return base64.urlsafe_b64encode(hmac.new(self._key, digest, hashlib.sha256).digest()).decode().rstrip("=")

    def verify(self, digest: bytes, signature: str, key_id: str) -> bool:
        try:
            return hmac.compare_digest(self.sign(digest, key_id), signature)
        except ValueError:
            return False


class InertFakeBackend:
    """Does not spawn, connect, or interpret the provider command."""

    def __init__(self, gateway_key: TestDetachedKey, profile: AcceptedRunnerProfile):
        self.gateway_key = gateway_key
        self.profile = profile
        self.calls = 0
        self.last_job = None
        self.actual_input_digest = None

    def execute(self, job):
        self.calls += 1
        self.last_job = job
        self.actual_input_digest = bundle_digest(
            job.input_directory,
            job.limits.output_files,
            job.limits.disk_bytes,
        )
        (job.output_directory / "provider-last-message.json").write_text("{}\n", encoding="utf-8")
        value = GatewayReceipt(
            schema_version=GATEWAY_RECEIPT_VERSION,
            document_type="provider-gateway-receipt",
            job_id=job.job_id,
            immutable_task_digest_sha256=job.immutable_task_digest_sha256,
            input_bundle_sha256=job.input_bundle_sha256,
            executed_executable_sha256=job.executable_sha256,
            destination_policy_sha256=job.destination_policy_sha256,
            credential_handle_sha256=job.credential_handle_sha256,
            external_request_attempted=True,
            external_request_observed=True,
            gateway_request_id="gateway-inert-fixture",
            attestation_type=GATEWAY_ATTESTATION_TYPE,
            attestation_key_id=self.profile.gateway_key_id,
            receipt_digest_sha256="0" * 64,
            attestation_signature="A" * 43,
        )
        digest = gateway_receipt_digest(value)
        gateway = replace(
            value,
            receipt_digest_sha256=digest,
            attestation_signature=self.gateway_key.sign(bytes.fromhex(digest), self.gateway_key.key_id),
        )
        return BackendResult(
            exit_code=0,
            stdout_bytes=0,
            stderr_bytes=0,
            whole_job_quiescent=True,
            gateway_receipt=gateway,
            applied_identity=job.identity,
            applied_policy_sha256=job.isolation_policy_sha256,
            executed_executable_sha256=job.executable_sha256,
            wall_time_milliseconds=1,
            cpu_time_milliseconds=1,
            maximum_rss_bytes=1024,
            maximum_pids=1,
            maximum_disk_bytes=3,
        )


class ObservedOverLimitBackend(InertFakeBackend):
    def __init__(self, gateway_key: TestDetachedKey, profile: AcceptedRunnerProfile, field: str, value: int):
        super().__init__(gateway_key, profile)
        self.field = field
        self.value = value

    def execute(self, job):
        result = super().execute(job)
        return replace(result, **{self.field: self.value})


class InertCredentialBroker:
    def __init__(self):
        self.issue_count = 0
        self.issue_bindings: list[tuple[str, str, str, str]] = []
        self.close_events: list[tuple[bytes, str, str, str]] = []

    def issue_single_use_handle(
        self,
        job_id: str,
        attempt_id: str,
        immutable_task_digest_sha256: str,
        provider_execution_identity_sha256: str,
    ) -> bytes:
        self.issue_count += 1
        self.issue_bindings.append(
            (
                job_id,
                attempt_id,
                immutable_task_digest_sha256,
                provider_execution_identity_sha256,
            )
        )
        return f"opaque-{job_id}-{attempt_id}-{immutable_task_digest_sha256[:8]}".encode("ascii")

    def close_single_use_handle(
        self, handle: bytes, job_id: str, attempt_id: str, outcome: str
    ) -> None:
        self.close_events.append((handle, job_id, attempt_id, outcome))


class FailingBackend:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def execute(self, job):
        self.calls += 1
        raise self.error


class FailingIssueBroker(InertCredentialBroker):
    def issue_single_use_handle(
        self,
        job_id: str,
        attempt_id: str,
        immutable_task_digest_sha256: str,
        provider_execution_identity_sha256: str,
    ) -> bytes:
        self.issue_count += 1
        raise RunnerError("credential-broker-unavailable", "synthetic broker refusal")


class ProfileMutatingBroker(InertCredentialBroker):
    """Mutate the accepted profile only after the durable claim exists."""

    def __init__(self, service: ExternalOneJobProviderRunner, changes: dict[str, str]):
        super().__init__()
        self.service = service
        self.changes = changes

    def issue_single_use_handle(
        self,
        job_id: str,
        attempt_id: str,
        immutable_task_digest_sha256: str,
        provider_execution_identity_sha256: str,
    ) -> bytes:
        handle = super().issue_single_use_handle(
            job_id,
            attempt_id,
            immutable_task_digest_sha256,
            provider_execution_identity_sha256,
        )
        assert self.service.accepted_profile is not None
        self.service.accepted_profile = replace(self.service.accepted_profile, **self.changes)
        return handle


def load_cloud_agent_module():
    path = ROOT.parent / "cloud-agent/cloud_agent.py"
    spec = importlib.util.spec_from_file_location("vibapp_cloud_agent_for_runner_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load cloud-agent module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_host_command(executable: Path, workspace: Path) -> list[str]:
    return [
        str(executable), "exec", "--model", "gpt-provider-runner-fixture", "--ephemeral", "-s", "workspace-write", "-C", str(workspace),
        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
        "--output-schema", str(PROVIDER_RESULT_SCHEMA), "--json", "-o",
        str(workspace / "provider-last-message.json"), "-",
    ]


def make_limits(**changes: int) -> Limits:
    values = {
        "wall_time_seconds": 3,
        "cpu_seconds": 2,
        "rss_bytes": 256 * 1024 * 1024,
        "pids": 8,
        "disk_bytes": 1024 * 1024,
        "stdout_bytes": 4096,
        "stderr_bytes": 4096,
        "output_files": 32,
    }
    values.update(changes)
    return Limits(**values)


def accepted_profile() -> AcceptedRunnerProfile:
    return AcceptedRunnerProfile(
        schema_version=ACCEPTED_PROFILE_VERSION,
        document_type="provider-runner-accepted-profile",
        runner_id="runner.synthetic-acceptance-oracle",
        runner_identity_sha256="1" * 64,
        runner_policy_sha256="2" * 64,
        executable_sha256="3" * 64,
        receipt_key_id="runner-key-test",
        gateway_key_id="gateway-key-test",
        destination_policy_sha256="4" * 64,
        status="independently-accepted",
    )


def runtime_profile(executable: Path, suffix: str) -> AcceptedRunnerProfile:
    policy = load_strict_json(POLICY_PATH)
    destination = load_strict_json(DESTINATION_POLICY_PATH)
    return AcceptedRunnerProfile(
        schema_version=ACCEPTED_PROFILE_VERSION,
        document_type="provider-runner-accepted-profile",
        runner_id=f"runner.{suffix}",
        runner_identity_sha256="7" * 64,
        runner_policy_sha256=sha256_bytes(canonical_json(policy)),
        executable_sha256=sha256_file(executable),
        receipt_key_id=f"runner-key-{suffix}",
        gateway_key_id=f"gateway-key-{suffix}",
        destination_policy_sha256=sha256_bytes(canonical_json(destination)),
        status="independently-accepted",
    )


def configured_service(root: Path, suffix: str, backend_factory=None):
    executable = Path(sys.executable).resolve(strict=True)
    profile = runtime_profile(executable, suffix)
    gateway_key = TestDetachedKey(profile.gateway_key_id)
    receipt_key = TestDetachedKey(profile.receipt_key_id)
    backend = (
        backend_factory(gateway_key, profile)
        if backend_factory is not None
        else InertFakeBackend(gateway_key, profile)
    )
    service = ExternalOneJobProviderRunner(
        state_root=root / "state",
        backend=backend,
        accepted_profile=profile,
        receipt_signer=receipt_key,
        gateway_verifier=gateway_key,
    )
    return service, backend, profile, executable, receipt_key


def execution_attempt(job_id: str, ordinal: int = 1) -> dict[str, object]:
    suffix = hashlib.sha256(f"{job_id}:{ordinal}".encode("utf-8")).hexdigest()[:16]
    return {"attempt_id": f"attempt-{ordinal:04d}-{suffix}", "ordinal": ordinal}


def provider_execution_identity(executable: Path) -> dict[str, object]:
    endpoint = {"kind": "provider-managed", "canonical_endpoint": "provider-managed"}
    endpoint["endpoint_sha256"] = sha256_bytes(canonical_json(endpoint))
    identity: dict[str, object] = {
        "schema_version": "vibapp.provider-execution-identity.experimental-v1",
        "endpoint": endpoint,
        "runtime": {
            "adapter_id": "provider-runner-test-adapter",
            "adapter_version": "experimental-v2",
            "adapter_sha256": "8" * 64,
            "package_id": "python-inert-executable",
            "package_version": "fixture-1.0.0",
            "executable_sha256": sha256_file(executable),
        },
        "non_secret_config_sha256": "9" * 64,
    }
    identity["identity_sha256"] = sha256_bytes(canonical_json(identity))
    return identity


def executable_identity(executable: Path) -> dict[str, object]:
    metadata = executable.stat()
    return {
        "configured_path": str(executable),
        "resolved_path": str(executable),
        "symlink_chain": [str(executable)],
        "version": "fixture-1.0.0",
        "sha256": sha256_file(executable),
        "owner_uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def service_request_binding(executable: Path, job_id: str, ordinal: int = 1) -> dict[str, object]:
    identity = provider_execution_identity(executable)
    return {
        "request_schema_version": REQUEST_VERSION,
        "request_document_type": "provider-runner-request",
        "execution_attempt": execution_attempt(job_id, ordinal),
        "provider_execution_identity": identity,
        "provider_execution_identity_sha256": identity["identity_sha256"],
    }


def cloud_request(
    cloud,
    workspace: Path,
    executable: Path,
    job_id: str,
    *,
    ordinal: int = 1,
):
    limits = {
        "wall_time_seconds": 3,
        "cpu_seconds": 2,
        "memory_bytes": 256 * 1024 * 1024,
        "pids": 8,
        "workspace_bytes": 1024 * 1024,
        "stdout_bytes": 4096,
        "stderr_bytes": 4096,
        "source_bytes": 512 * 1024,
        "source_files": 32,
    }
    isolation = cloud.required_isolation_policy({"limits": limits})
    provider_identity = provider_execution_identity(executable)
    return cloud.ProviderRunnerRequest(
        schema_version=REQUEST_VERSION,
        document_type="provider-runner-request",
        job_id=job_id,
        execution_attempt=execution_attempt(job_id, ordinal),
        immutable_task_digest_sha256="a" * 64,
        provider_execution_identity=provider_identity,
        provider_execution_identity_sha256=provider_identity["identity_sha256"],
        provider="openai-codex",
        model="gpt-provider-runner-fixture",
        executable_identity=executable_identity(executable),
        command=canonical_host_command(executable, workspace),
        prompt=b"synthetic inert prompt",
        workspace=workspace,
        limits=limits,
        isolation_policy=isolation,
        isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
        input_bundle_sha256=cloud.workspace_tree_digest(workspace),
    )


def signed_provider_receipt(key: TestDetachedKey, profile: AcceptedRunnerProfile) -> ProviderReceiptV3:
    value = ProviderReceiptV3(
        schema_version=RECEIPT_VERSION,
        document_type="provider-runner-receipt",
        job_id="job-receipt-1",
        execution_attempt=execution_attempt("job-receipt-1"),
        immutable_task_digest_sha256="a" * 64,
        provider_execution_identity_sha256="f" * 64,
        input_bundle_sha256="b" * 64,
        output_bundle_sha256="c" * 64,
        runner_id=profile.runner_id,
        runner_identity_sha256=profile.runner_identity_sha256,
        isolation_policy_sha256="d" * 64,
        external_request_attempted=True,
        external_request_observed=True,
        gateway_request_id="gateway-request-1",
        whole_job_quiescent=True,
        exit_code=0,
        stdout_bytes=12,
        stderr_bytes=0,
        executed_executable_sha256=profile.executable_sha256,
        attestation_type=RECEIPT_ATTESTATION_TYPE,
        attestation_key_id=profile.receipt_key_id,
        receipt_digest_sha256="0" * 64,
        attestation_signature="A" * 43,
    )
    digest = provider_receipt_digest(value)
    return replace(value, receipt_digest_sha256=digest, attestation_signature=key.sign(bytes.fromhex(digest), key.key_id))


def signed_gateway_receipt(key: TestDetachedKey, profile: AcceptedRunnerProfile) -> GatewayReceipt:
    value = GatewayReceipt(
        schema_version=GATEWAY_RECEIPT_VERSION,
        document_type="provider-gateway-receipt",
        job_id="job-receipt-1",
        immutable_task_digest_sha256="a" * 64,
        input_bundle_sha256="b" * 64,
        executed_executable_sha256=profile.executable_sha256,
        destination_policy_sha256=profile.destination_policy_sha256,
        credential_handle_sha256="e" * 64,
        external_request_attempted=True,
        external_request_observed=True,
        gateway_request_id="gateway-request-1",
        attestation_type=GATEWAY_ATTESTATION_TYPE,
        attestation_key_id=profile.gateway_key_id,
        receipt_digest_sha256="0" * 64,
        attestation_signature="A" * 43,
    )
    digest = gateway_receipt_digest(value)
    return replace(value, receipt_digest_sha256=digest, attestation_signature=key.sign(bytes.fromhex(digest), key.key_id))


class ProviderRunnerTests(unittest.TestCase):
    def test_interface_schema_and_provider_result_pin_match_current_cloud_agent(self) -> None:
        authored_json = sorted((ROOT / "deployment").glob("*.json")) + sorted(
            (ROOT / "schemas").glob("*.json")
        )
        self.assertEqual(len(authored_json), 8)
        for path in authored_json:
            with self.subTest(path=path.name):
                parsed = load_strict_json(path, 512 * 1024)
                self.assertIsInstance(parsed, dict)
                if path.parent.name == "schemas":
                    self.assertEqual(parsed["$schema"], "https://json-schema.org/draft/2020-12/schema")
        runner_schema = load_strict_json(RUNNER_INTERFACE_SCHEMA, 512 * 1024)
        cloud_schema = load_strict_json(CLOUD_RUNNER_INTERFACE_SCHEMA, 512 * 1024)
        self.assertEqual(runner_schema, cloud_schema)
        self.assertEqual(
            runner_schema["$defs"]["request"]["properties"]["schema_version"]["const"],
            REQUEST_VERSION,
        )
        self.assertEqual(
            runner_schema["$defs"]["receipt"]["properties"]["schema_version"]["const"],
            RECEIPT_VERSION,
        )
        self.assertEqual(
            runner_schema["$defs"]["request"]["properties"]["document_type"]["const"],
            "provider-runner-request",
        )
        self.assertEqual(
            runner_schema["$defs"]["receipt"]["properties"]["document_type"]["const"],
            "provider-runner-receipt",
        )
        policy = load_strict_json(POLICY_PATH)
        runtime_inputs = policy["immutable_runtime_inputs"]
        self.assertEqual(runtime_inputs["provider_result_schema_sha256"], sha256_file(PROVIDER_RESULT_SCHEMA))
        provider_schema = load_strict_json(PROVIDER_RESULT_SCHEMA)
        self.assertEqual(
            runtime_inputs["provider_result_schema_version"],
            provider_schema["properties"]["schema_version"]["const"],
        )
        self.assertEqual(
            runtime_inputs["provider_result_document_type"],
            provider_schema["properties"]["document_type"]["const"],
        )

    def test_cloud_adapter_has_no_default_credential_handle(self) -> None:
        with self.assertRaises(RunnerError) as caught:
            UnavailableCredentialHandleBroker().issue_single_use_handle(
                "job-1", "attempt-0001-0123456789abcdef", "a" * 64, "b" * 64
            )
        self.assertEqual(caught.exception.code, "credential-broker-unavailable")

    def test_adapter_rejects_stale_contract_attempt_and_identity_before_claim(self) -> None:
        cloud = load_cloud_agent_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "request.json").write_text("{}\n", encoding="utf-8")
            service, backend, _, executable, _ = configured_service(root, "request-bindings")
            broker = InertCredentialBroker()
            adapter = CloudAgentProviderRunnerAdapter(service, cloud.ProviderRunnerReceipt, broker)
            request = cloud_request(cloud, workspace, executable, "job-request-bindings")

            bad_identity = copy.deepcopy(request.provider_execution_identity)
            bad_identity["non_secret_config_sha256"] = "d" * 64
            bad_executable = dict(request.executable_identity)
            bad_executable["sha256"] = "e" * 64
            cases = (
                replace(request, schema_version="vibapp.provider-runner-request.experimental-v1"),
                replace(request, document_type="provider-runner-request-stale"),
                replace(
                    request,
                    execution_attempt={
                        "attempt_id": request.execution_attempt["attempt_id"],
                        "ordinal": 2,
                    },
                ),
                replace(request, provider_execution_identity=bad_identity),
                replace(request, provider_execution_identity_sha256="f" * 64),
                replace(request, executable_identity=bad_executable),
            )
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(RunnerError):
                    validate_provider_runner_request_envelope(candidate)
                with self.assertRaises(RunnerError):
                    adapter.execute(candidate)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(broker.issue_count, 0)
            self.assertFalse(any((root / "state/job-claims").glob("*.json")))

    def test_retry_uses_new_attempt_claim_and_receipt_binding(self) -> None:
        cloud = load_cloud_agent_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "request.json").write_text("{}\n", encoding="utf-8")
            service, backend, profile, executable, receipt_key = configured_service(root, "retry-attempt")
            broker = InertCredentialBroker()
            adapter = CloudAgentProviderRunnerAdapter(service, cloud.ProviderRunnerReceipt, broker)
            first = cloud_request(cloud, workspace, executable, "job-retry-attempt", ordinal=1)
            first_receipt = adapter.execute(first)
            retry = cloud_request(cloud, workspace, executable, "job-retry-attempt", ordinal=2)
            retry_receipt = adapter.execute(retry)

            self.assertEqual(first.immutable_task_digest_sha256, retry.immutable_task_digest_sha256)
            self.assertNotEqual(first.execution_attempt, retry.execution_attempt)
            self.assertEqual(first_receipt.execution_attempt, first.execution_attempt)
            self.assertEqual(retry_receipt.execution_attempt, retry.execution_attempt)
            self.assertEqual(
                first_receipt.provider_execution_identity_sha256,
                retry_receipt.provider_execution_identity_sha256,
            )
            self.assertEqual(backend.calls, 2)
            self.assertEqual(broker.issue_count, 2)
            self.assertEqual([binding[1] for binding in broker.issue_bindings], [
                first.execution_attempt["attempt_id"],
                retry.execution_attempt["attempt_id"],
            ])
            self.assertEqual(len(list((root / "state/job-claims").glob("*.json"))), 2)
            accepted = cloud.AcceptedRunnerProfile(
                runner_id=profile.runner_id,
                runner_identity_sha256=profile.runner_identity_sha256,
                attestation_key_id=profile.receipt_key_id,
            )
            self.assertEqual(
                cloud.validate_provider_receipt(retry_receipt, retry, accepted, receipt_key),
                retry_receipt,
            )

    def test_inert_fake_backend_binds_gateway_output_policy_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "request.json").write_text("{}\n", encoding="utf-8")
            policy = load_strict_json(POLICY_PATH)
            destination = load_strict_json(DESTINATION_POLICY_PATH)
            executable = Path(sys.executable).resolve(strict=True)
            profile = AcceptedRunnerProfile(
                schema_version=ACCEPTED_PROFILE_VERSION,
                document_type="provider-runner-accepted-profile",
                runner_id="runner.inert-fixture",
                runner_identity_sha256="1" * 64,
                runner_policy_sha256=sha256_bytes(canonical_json(policy)),
                executable_sha256=sha256_file(executable),
                receipt_key_id="runner-key-inert",
                gateway_key_id="gateway-key-inert",
                destination_policy_sha256=sha256_bytes(canonical_json(destination)),
                status="independently-accepted",
            )
            gateway_key = TestDetachedKey(profile.gateway_key_id)
            receipt_key = TestDetachedKey(profile.receipt_key_id)
            backend = InertFakeBackend(gateway_key, profile)
            service = ExternalOneJobProviderRunner(
                state_root=state,
                backend=backend,
                accepted_profile=profile,
                receipt_signer=receipt_key,
                gateway_verifier=gateway_key,
            )
            limits = make_limits()
            isolation = default_requested_isolation_policy(limits)
            input_digest = bundle_digest(input_root, limits.output_files, limits.disk_bytes)
            returned = root / "returned"
            receipt = service.execute(
                job_id="job-inert-backend",
                **service_request_binding(executable, "job-inert-backend"),
                immutable_task_digest_sha256="a" * 64,
                input_root=input_root,
                expected_input_bundle_sha256=input_digest,
                executable_path=executable,
                command=canonical_host_command(executable, input_root),
                prompt=b"synthetic inert prompt",
                credential_handle=b"opaque-handle-123",
                limits=limits,
                isolation_policy=isolation,
                expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                returned_output_root=returned,
            )
            self.assertEqual(backend.calls, 1)
            self.assertIsNotNone(backend.last_job)
            self.assertEqual(backend.last_job.command[0], "/opt/vibapp/bin/codex")
            self.assertIn("/job/work", backend.last_job.command)
            self.assertNotIn(str(input_root), backend.last_job.command)
            self.assertTrue((returned / "provider-last-message.json").is_file())
            output_digest = bundle_digest(returned, limits.output_files, limits.disk_bytes)
            self.assertEqual(receipt.output_bundle_sha256, output_digest)
            self.assertEqual(receipt.gateway_request_id, "gateway-inert-fixture")
            validate_provider_receipt_v3(
                receipt,
                request_job_id="job-inert-backend",
                execution_attempt=execution_attempt("job-inert-backend"),
                immutable_task_digest_sha256="a" * 64,
                provider_execution_identity_sha256=provider_execution_identity(executable)["identity_sha256"],
                input_bundle_sha256=input_digest,
                output_bundle_sha256=output_digest,
                isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                profile=profile,
                verifier=receipt_key,
            )
            with self.assertRaises(RunnerError) as replay:
                service.execute(
                    job_id="job-inert-backend",
                    **service_request_binding(executable, "job-inert-backend"),
                    immutable_task_digest_sha256="a" * 64,
                    input_root=input_root,
                    expected_input_bundle_sha256=input_digest,
                    executable_path=executable,
                    command=canonical_host_command(executable, input_root),
                    prompt=b"synthetic inert prompt",
                    credential_handle=b"opaque-handle-123",
                    limits=limits,
                    isolation_policy=isolation,
                    expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                    returned_output_root=root / "returned-2",
                )
            self.assertEqual(replay.exception.code, "replay-rejected")

    def test_adapter_returns_exact_cloud_agent_receipt_and_output_bundle(self) -> None:
        cloud = load_cloud_agent_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "request.json").write_text("{}\n", encoding="utf-8")
            policy = load_strict_json(POLICY_PATH)
            destination = load_strict_json(DESTINATION_POLICY_PATH)
            executable = Path(sys.executable).resolve(strict=True)
            profile = AcceptedRunnerProfile(
                schema_version=ACCEPTED_PROFILE_VERSION,
                document_type="provider-runner-accepted-profile",
                runner_id="runner.adapter-fixture",
                runner_identity_sha256="5" * 64,
                runner_policy_sha256=sha256_bytes(canonical_json(policy)),
                executable_sha256=sha256_file(executable),
                receipt_key_id="runner-key-adapter",
                gateway_key_id="gateway-key-adapter",
                destination_policy_sha256=sha256_bytes(canonical_json(destination)),
                status="independently-accepted",
            )
            gateway_key = TestDetachedKey(profile.gateway_key_id)
            receipt_key = TestDetachedKey(profile.receipt_key_id)
            service = ExternalOneJobProviderRunner(
                state_root=root / "state",
                backend=InertFakeBackend(gateway_key, profile),
                accepted_profile=profile,
                receipt_signer=receipt_key,
                gateway_verifier=gateway_key,
            )
            broker = InertCredentialBroker()
            adapter = CloudAgentProviderRunnerAdapter(
                service,
                cloud.ProviderRunnerReceipt,
                broker,
            )
            limits = {
                "wall_time_seconds": 3,
                "cpu_seconds": 2,
                "memory_bytes": 256 * 1024 * 1024,
                "pids": 8,
                "workspace_bytes": 1024 * 1024,
                "stdout_bytes": 4096,
                "stderr_bytes": 4096,
                "source_bytes": 512 * 1024,
                "source_files": 32,
            }
            isolation = cloud.required_isolation_policy({"limits": limits})
            provider_identity = provider_execution_identity(executable)
            request = cloud.ProviderRunnerRequest(
                schema_version=REQUEST_VERSION,
                document_type="provider-runner-request",
                job_id="job-adapter-fixture",
                execution_attempt=execution_attempt("job-adapter-fixture"),
                immutable_task_digest_sha256="a" * 64,
                provider_execution_identity=provider_identity,
                provider_execution_identity_sha256=provider_identity["identity_sha256"],
                provider="openai-codex",
                model="gpt-provider-runner-fixture",
                executable_identity=executable_identity(executable),
                command=canonical_host_command(executable, workspace),
                prompt=b"synthetic inert prompt",
                workspace=workspace,
                limits=limits,
                isolation_policy=isolation,
                isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                input_bundle_sha256=cloud.workspace_tree_digest(workspace),
            )
            receipt = adapter.execute(request)
            self.assertIs(type(receipt), cloud.ProviderRunnerReceipt)
            self.assertEqual(receipt.output_bundle_sha256, cloud.workspace_tree_digest(workspace))
            accepted = cloud.AcceptedRunnerProfile(
                runner_id=profile.runner_id,
                runner_identity_sha256=profile.runner_identity_sha256,
                attestation_key_id=profile.receipt_key_id,
            )
            self.assertEqual(cloud.validate_provider_receipt(receipt, request, accepted, receipt_key), receipt)
            self.assertEqual(broker.issue_count, 1)
            self.assertEqual([event[3] for event in broker.close_events], ["success-consumed"])
            with self.assertRaises(RunnerError) as replay:
                adapter.execute(request)
            self.assertEqual(replay.exception.code, "replay-rejected")
            self.assertEqual(broker.issue_count, 1, "known replay must not consume another broker handle")
            self.assertEqual(len(broker.close_events), 1)

    def test_trusted_supervisor_cpu_and_rss_overlimit_observations_reject(self) -> None:
        for index, (field, value) in enumerate((
            ("cpu_time_milliseconds", 2001),
            ("maximum_rss_bytes", 256 * 1024 * 1024 + 1),
        )):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                input_root = root / "input"
                input_root.mkdir()
                (input_root / "request.json").write_text("{}\n", encoding="utf-8")
                policy = load_strict_json(POLICY_PATH)
                destination = load_strict_json(DESTINATION_POLICY_PATH)
                executable = Path(sys.executable).resolve(strict=True)
                profile = AcceptedRunnerProfile(
                    schema_version=ACCEPTED_PROFILE_VERSION,
                    document_type="provider-runner-accepted-profile",
                    runner_id=f"runner.limit-{index}",
                    runner_identity_sha256="6" * 64,
                    runner_policy_sha256=sha256_bytes(canonical_json(policy)),
                    executable_sha256=sha256_file(executable),
                    receipt_key_id=f"runner-key-limit-{index}",
                    gateway_key_id=f"gateway-key-limit-{index}",
                    destination_policy_sha256=sha256_bytes(canonical_json(destination)),
                    status="independently-accepted",
                )
                gateway_key = TestDetachedKey(profile.gateway_key_id)
                receipt_key = TestDetachedKey(profile.receipt_key_id)
                service = ExternalOneJobProviderRunner(
                    state_root=root / "state",
                    backend=ObservedOverLimitBackend(gateway_key, profile, field, value),
                    accepted_profile=profile,
                    receipt_signer=receipt_key,
                    gateway_verifier=gateway_key,
                )
                limits = make_limits()
                isolation = default_requested_isolation_policy(limits)
                with self.assertRaises(RunnerError) as caught:
                    service.execute(
                        job_id=f"job-observed-limit-{index}",
                        **service_request_binding(executable, f"job-observed-limit-{index}"),
                        immutable_task_digest_sha256="a" * 64,
                        input_root=input_root,
                        expected_input_bundle_sha256=bundle_digest(input_root, limits.output_files, limits.disk_bytes),
                        executable_path=executable,
                        command=canonical_host_command(executable, input_root),
                        prompt=b"synthetic inert prompt",
                        credential_handle=b"opaque-handle-limit",
                        limits=limits,
                        isolation_policy=isolation,
                        expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                        returned_output_root=root / "returned",
                    )
                self.assertEqual(caught.exception.code, "resource-limit")

    def test_default_production_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = ExternalOneJobProviderRunner(state_root=Path(directory))
            with self.assertRaisesRegex(RunnerError, "accepted runner profile") as caught:
                runner.preflight(default_requested_isolation_policy())
            self.assertEqual(caught.exception.code, "runner-attestation-unavailable")

    def test_unprovisioned_profile_cannot_be_loaded_as_accepted(self) -> None:
        with self.assertRaises(RunnerError) as caught:
            AcceptedRunnerProfile.from_dict(load_strict_json(PROFILE_TEMPLATE_PATH))
        self.assertEqual(caught.exception.code, "runner-profile-unaccepted")

    def test_placeholder_profile_mutations_and_direct_dataclass_bypass_fail_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = Path(sys.executable).resolve(strict=True)
            valid = runtime_profile(executable, "profile-positive")
            gateway_key = TestDetachedKey(valid.gateway_key_id)
            receipt_key = TestDetachedKey(valid.receipt_key_id)
            backend = InertFakeBackend(gateway_key, valid)
            valid_service = ExternalOneJobProviderRunner(
                state_root=root / "valid-state",
                backend=backend,
                accepted_profile=valid,
                receipt_signer=receipt_key,
                gateway_verifier=gateway_key,
            )
            required = default_requested_isolation_policy(make_limits())
            self.assertEqual(AcceptedRunnerProfile.from_dict(asdict(valid)), valid)
            self.assertEqual(valid_service.preflight(required), required)

            transformed = (
                {"runner_id": "UN-PROVISIONED"},
                {"receipt_key_id": "PLACE_HOLDER"},
                {"gateway_key_id": "replace.with-real-value"},
                {"runner_identity_sha256": "0" * 64},
                {"receipt_key_id": "0" * 64},
                {"gateway_key_id": "0" * 64},
            )
            for index, changes in enumerate(transformed):
                with self.subTest(changes=changes):
                    attacked = replace(valid, **changes)
                    attacked_service = ExternalOneJobProviderRunner(
                        state_root=root / f"attacked-state-{index}",
                        backend=backend,
                        accepted_profile=attacked,
                        receipt_signer=receipt_key,
                        gateway_verifier=gateway_key,
                    )
                    with self.assertRaises(RunnerError) as caught:
                        attacked_service.preflight(required)
                    self.assertEqual(caught.exception.code, "runner-profile-unaccepted")

            template_attacks = (
                {"status": "independently-accepted"},
                {
                    "runner_policy_sha256": valid.runner_policy_sha256,
                    "executable_sha256": valid.executable_sha256,
                    "destination_policy_sha256": valid.destination_policy_sha256,
                },
                {
                    "status": "independently-accepted",
                    "runner_policy_sha256": valid.runner_policy_sha256,
                    "executable_sha256": valid.executable_sha256,
                    "destination_policy_sha256": valid.destination_policy_sha256,
                    "runner_id": valid.runner_id,
                },
            )
            for changes in template_attacks:
                with self.subTest(template_changes=changes):
                    template = load_strict_json(PROFILE_TEMPLATE_PATH)
                    template.update(changes)
                    with self.assertRaises(RunnerError) as mutated_template:
                        AcceptedRunnerProfile.from_dict(template)
                    self.assertEqual(mutated_template.exception.code, "runner-profile-unaccepted")

    def test_input_request_digest_then_source_change_is_rejected_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            source = input_root / "request.json"
            source.write_text("{\"version\":1}\n", encoding="utf-8")
            service, backend, _, executable, _ = configured_service(root, "input-initial")
            limits = make_limits()
            isolation = default_requested_isolation_policy(limits)
            initial_digest = bundle_digest(input_root, limits.output_files, limits.disk_bytes)
            source.write_text("{\"version\":2}\n", encoding="utf-8")
            with self.assertRaises(RunnerError) as caught:
                service.begin(
                    job_id="job-input-initial-toctou",
                    **service_request_binding(executable, "job-input-initial-toctou"),
                    immutable_task_digest_sha256="a" * 64,
                    input_root=input_root,
                    expected_input_bundle_sha256=initial_digest,
                    executable_path=executable,
                    command=canonical_host_command(executable, input_root),
                    prompt=b"synthetic inert prompt",
                    limits=limits,
                    isolation_policy=isolation,
                    expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                    returned_output_root=root / "returned",
                )
            self.assertEqual(caught.exception.code, "input-bundle-mismatch")
            self.assertEqual(backend.calls, 0)
            claim = root / "state/job-claims" / durable_claim_name(
                "job-input-initial-toctou",
                execution_attempt("job-input-initial-toctou"),
            )
            self.assertTrue(claim.is_file())

    def test_input_mutation_during_descriptor_copy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            source = input_root / "request.json"
            source.write_text("{\"stable\":true}\n", encoding="utf-8")
            service, backend, _, executable, _ = configured_service(root, "input-copy")
            limits = make_limits()
            isolation = default_requested_isolation_policy(limits)
            initial_digest = bundle_digest(input_root, limits.output_files, limits.disk_bytes)
            original_copy = provider_runner_module._copy_open_file_to_snapshot
            changed = False
            descriptor_bound = threading.Event()
            mutation_complete = threading.Event()

            def mutate_after_descriptor_binding() -> None:
                nonlocal changed
                if not descriptor_bound.wait(timeout=2):
                    return
                source.write_text("{\"stable\":false,\"changed\":true}\n", encoding="utf-8")
                changed = True
                mutation_complete.set()

            def copy_after_synchronized_mutation(source_fd, destination, initial, budget):
                descriptor_bound.set()
                if not mutation_complete.wait(timeout=2):
                    raise AssertionError("descriptor-bound mutation did not complete")
                return original_copy(source_fd, destination, initial, budget)

            mutator = threading.Thread(target=mutate_after_descriptor_binding, daemon=True)
            mutator.start()
            try:
                with mock.patch.object(
                    provider_runner_module,
                    "_copy_open_file_to_snapshot",
                    copy_after_synchronized_mutation,
                ):
                    with self.assertRaises(RunnerError) as caught:
                        service.begin(
                            job_id="job-input-copy-toctou",
                            **service_request_binding(executable, "job-input-copy-toctou"),
                            immutable_task_digest_sha256="a" * 64,
                            input_root=input_root,
                            expected_input_bundle_sha256=initial_digest,
                            executable_path=executable,
                            command=canonical_host_command(executable, input_root),
                            prompt=b"synthetic inert prompt",
                            limits=limits,
                            isolation_policy=isolation,
                            expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                            returned_output_root=root / "returned",
                        )
            finally:
                mutator.join(timeout=2)
            self.assertFalse(mutator.is_alive())
            self.assertTrue(changed)
            self.assertEqual(caught.exception.code, "bundle-mutated")
            self.assertEqual(backend.calls, 0)

    def test_backend_reads_sealed_input_snapshot_bound_to_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            source = input_root / "request.json"
            source.write_text("{\"stable\":true}\n", encoding="utf-8")
            service, backend, _, executable, _ = configured_service(root, "input-positive")
            limits = make_limits()
            isolation = default_requested_isolation_policy(limits)
            input_digest = bundle_digest(input_root, limits.output_files, limits.disk_bytes)
            claimed = service.begin(
                job_id="job-input-positive",
                **service_request_binding(executable, "job-input-positive"),
                immutable_task_digest_sha256="a" * 64,
                input_root=input_root,
                expected_input_bundle_sha256=input_digest,
                executable_path=executable,
                command=canonical_host_command(executable, input_root),
                prompt=b"synthetic inert prompt",
                limits=limits,
                isolation_policy=isolation,
                expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                returned_output_root=root / "returned",
            )
            source.write_text("{\"attacker\":\"changed original\"}\n", encoding="utf-8")
            receipt = service.execute_claimed(claimed, b"opaque-input-positive")
            self.assertNotEqual(bundle_digest(input_root, limits.output_files, limits.disk_bytes), input_digest)
            self.assertEqual(backend.actual_input_digest, input_digest)
            self.assertEqual(receipt.input_bundle_sha256, input_digest)

    def test_claimed_attempt_identity_and_policy_mutation_is_rejected_before_backend(self) -> None:
        def mutate_attempt(claimed) -> None:
            claimed.execution_attempt["ordinal"] = 2

        def mutate_identity(claimed) -> None:
            claimed.provider_execution_identity["runtime"]["adapter_version"] = "mutated-after-claim"

        def mutate_policy(claimed) -> None:
            claimed.isolation_policy["network_policy"] = "unrestricted"

        for index, (binding, mutate) in enumerate((
            ("execution-attempt", mutate_attempt),
            ("provider-execution-identity", mutate_identity),
            ("isolation-policy", mutate_policy),
        )):
            with self.subTest(binding=binding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                input_root = root / "input"
                input_root.mkdir()
                (input_root / "request.json").write_text("{}\n", encoding="utf-8")
                service, backend, _, executable, _ = configured_service(
                    root,
                    f"claimed-binding-{index}",
                )
                limits = make_limits()
                isolation = default_requested_isolation_policy(limits)
                job_id = f"job-claimed-binding-{index}"
                claimed = service.begin(
                    job_id=job_id,
                    **service_request_binding(executable, job_id),
                    immutable_task_digest_sha256="a" * 64,
                    input_root=input_root,
                    expected_input_bundle_sha256=bundle_digest(
                        input_root,
                        limits.output_files,
                        limits.disk_bytes,
                    ),
                    executable_path=executable,
                    command=canonical_host_command(executable, input_root),
                    prompt=b"synthetic inert prompt",
                    limits=limits,
                    isolation_policy=isolation,
                    expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                    returned_output_root=root / "returned",
                )
                mutate(claimed)
                with self.assertRaises(RunnerError) as caught:
                    service.execute_claimed(claimed, b"opaque-mutated-claim")
                self.assertEqual(caught.exception.code, "claim-invalid")
                self.assertEqual(backend.calls, 0)

    def test_output_mutation_during_snapshot_is_rejected_before_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "request.json").write_text("{}\n", encoding="utf-8")
            service, backend, _, executable, _ = configured_service(root, "output-copy")
            limits = make_limits()
            isolation = default_requested_isolation_policy(limits)
            claimed = service.begin(
                job_id="job-output-copy-toctou",
                **service_request_binding(executable, "job-output-copy-toctou"),
                immutable_task_digest_sha256="a" * 64,
                input_root=input_root,
                expected_input_bundle_sha256=bundle_digest(input_root, limits.output_files, limits.disk_bytes),
                executable_path=executable,
                command=canonical_host_command(executable, input_root),
                prompt=b"synthetic inert prompt",
                limits=limits,
                isolation_policy=isolation,
                expected_isolation_policy_sha256=sha256_bytes(canonical_json(isolation)),
                returned_output_root=root / "returned",
            )
            original_copy = provider_runner_module._copy_open_file_to_snapshot
            changed = False

            def mutate_backend_output_after_open(source_fd, destination, initial, budget):
                nonlocal changed
                if not changed:
                    changed = True
                    backend_output = backend.last_job.output_directory / "provider-last-message.json"
                    backend_output.write_text("{\"changed\":true}\n", encoding="utf-8")
                return original_copy(source_fd, destination, initial, budget)

            with mock.patch.object(provider_runner_module, "_copy_open_file_to_snapshot", mutate_backend_output_after_open):
                with self.assertRaises(RunnerError) as caught:
                    service.execute_claimed(claimed, b"opaque-output-copy")
            self.assertTrue(changed)
            self.assertEqual(caught.exception.code, "bundle-mutated")

    def test_adapter_detects_post_signature_transfer_mutation_and_restores_input(self) -> None:
        cloud = load_cloud_agent_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            original = workspace / "request.json"
            original.write_text("{\"original\":true}\n", encoding="utf-8")
            service, _, _, executable, _ = configured_service(root, "output-transfer")
            broker = InertCredentialBroker()
            adapter = CloudAgentProviderRunnerAdapter(service, cloud.ProviderRunnerReceipt, broker)
            request = cloud_request(cloud, workspace, executable, "job-output-transfer-toctou")
            real_digest = bundle_digest
            changed = False

            def mutate_transferred_tree(bundle_root, maximum_files, maximum_bytes):
                nonlocal changed
                if Path(bundle_root).resolve() == workspace.resolve() and not changed:
                    changed = True
                    workspace.chmod(0o700)
                    output_file = workspace / "provider-last-message.json"
                    output_file.chmod(0o600)
                    output_file.write_text("{\"changed-after-signature\":true}\n", encoding="utf-8")
                return real_digest(bundle_root, maximum_files, maximum_bytes)

            with mock.patch("cloud_agent_adapter.bundle_digest", side_effect=mutate_transferred_tree):
                with self.assertRaises(RunnerError) as caught:
                    adapter.execute(request)
            self.assertTrue(changed)
            self.assertEqual(caught.exception.code, "bundle-mutated")
            self.assertEqual(original.read_text(encoding="utf-8"), "{\"original\":true}\n")
            self.assertEqual(broker.issue_count, 1)
            self.assertEqual([event[3] for event in broker.close_events], ["failed-revoke-or-consume"])

    def test_adapter_rejects_missing_blank_or_substituted_model_before_claim(self) -> None:
        cloud = load_cloud_agent_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "request.json").write_text("{}\n", encoding="utf-8")
            service, backend, _, executable, _ = configured_service(root, "exact-model")
            adapter = CloudAgentProviderRunnerAdapter(
                service,
                cloud.ProviderRunnerReceipt,
                InertCredentialBroker(),
            )
            request = cloud_request(cloud, workspace, executable, "job-exact-model")
            cases = (
                replace(request, model=""),
                replace(request, model="   "),
                replace(request, command=[str(executable), "exec"]),
                replace(request, command=[str(executable), "exec", "--model", "gpt-settings-drift"]),
            )
            for candidate in cases:
                with self.subTest(model=candidate.model, command=candidate.command), self.assertRaises(RunnerError) as caught:
                    adapter.execute(candidate)
                self.assertIn(caught.exception.code, {"model-binding-required", "model-mismatch"})
            self.assertEqual(backend.calls, 0)
            self.assertFalse(any((root / "state/job-claims").glob("*.json")))

    def test_durable_claim_precedes_handle_issue_and_later_failures_close_handle(self) -> None:
        cloud = load_cloud_agent_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "issue-workspace"
            workspace.mkdir()
            (workspace / "request.json").write_text("{}\n", encoding="utf-8")
            service, _, _, executable, _ = configured_service(root / "issue", "issue-order")
            broker = FailingIssueBroker()
            adapter = CloudAgentProviderRunnerAdapter(service, cloud.ProviderRunnerReceipt, broker)
            request = cloud_request(cloud, workspace, executable, "job-broker-issue-order")
            with self.assertRaises(RunnerError) as first:
                adapter.execute(request)
            self.assertEqual(first.exception.code, "credential-broker-unavailable")
            self.assertEqual(broker.issue_count, 1)
            self.assertEqual(broker.close_events, [])
            with self.assertRaises(RunnerError) as replay:
                adapter.execute(request)
            self.assertEqual(replay.exception.code, "replay-rejected")
            self.assertEqual(broker.issue_count, 1, "claimed replay must fail before broker issue")

            failures = (
                RunnerError("resource-limit", "synthetic resource refusal"),
                RunnerError("deadline-exceeded", "synthetic timeout"),
                RuntimeError("synthetic backend crash"),
            )
            for index, failure in enumerate(failures):
                with self.subTest(failure=type(failure).__name__, code=getattr(failure, "code", None)):
                    case_root = root / f"failure-{index}"
                    case_workspace = case_root / "workspace"
                    case_workspace.mkdir(parents=True)
                    (case_workspace / "request.json").write_text("{}\n", encoding="utf-8")
                    def factory(_gateway_key, _profile, selected=failure):
                        return FailingBackend(selected)
                    case_service, case_backend, _, case_executable, _ = configured_service(
                        case_root,
                        f"failure-{index}",
                        factory,
                    )
                    case_broker = InertCredentialBroker()
                    case_adapter = CloudAgentProviderRunnerAdapter(
                        case_service,
                        cloud.ProviderRunnerReceipt,
                        case_broker,
                    )
                    case_request = cloud_request(
                        cloud,
                        case_workspace,
                        case_executable,
                        f"job-broker-failure-{index}",
                    )
                    with self.assertRaises(type(failure)):
                        case_adapter.execute(case_request)
                    self.assertEqual(case_backend.calls, 1)
                    self.assertEqual(case_broker.issue_count, 1)
                    self.assertEqual(
                        [event[3] for event in case_broker.close_events],
                        ["failed-revoke-or-consume"],
                    )

    def test_adapter_rejects_policy_or_executable_profile_drift_after_begin(self) -> None:
        cloud = load_cloud_agent_module()
        mutations = (
            {"runner_policy_sha256": "e" * 64},
            {"executable_sha256": "f" * 64},
        )
        for index, changes in enumerate(mutations):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "request.json").write_text("{}\n", encoding="utf-8")
                service, backend, _, executable, _ = configured_service(root, f"profile-drift-{index}")
                broker = ProfileMutatingBroker(service, changes)
                adapter = CloudAgentProviderRunnerAdapter(service, cloud.ProviderRunnerReceipt, broker)
                request = cloud_request(cloud, workspace, executable, f"job-profile-drift-{index}")
                with self.assertRaises(RunnerError) as caught:
                    adapter.execute(request)
                self.assertEqual(caught.exception.code, "runner-profile-unaccepted")
                self.assertEqual(backend.calls, 0)
                self.assertEqual(broker.issue_count, 1)
                self.assertEqual(
                    [event[3] for event in broker.close_events],
                    ["failed-revoke-or-consume"],
                )
                claim = root / "state/job-claims" / durable_claim_name(
                    request.job_id,
                    request.execution_attempt,
                )
                self.assertTrue(claim.is_file())

    def test_reviewed_policy_has_required_denials_and_caps(self) -> None:
        policy = load_strict_json(POLICY_PATH)
        request = default_requested_isolation_policy()
        self.assertEqual(validate_requested_isolation_policy(request, policy), request)
        denied = set(policy["environment"]["explicitly_denied"])
        self.assertTrue({"HOME", "CODEX_HOME", "HTTP_PROXY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "DOCKER_HOST"} <= denied)
        self.assertEqual(policy["credential_broker"]["delivery"], "opaque-single-use-handle-on-fd")
        self.assertEqual(policy["network"]["workload_egress"], "trusted-provider-gateway-only")
        gateway_policy = validate_gateway_destination_policy(load_strict_json(DESTINATION_POLICY_PATH))
        self.assertEqual(gateway_policy["default_egress"], "deny")
        self.assertEqual(len(gateway_policy["allowed_egress"]), 1)
        weakened = dict(request)
        weakened["environment_inheritance"] = "host"
        with self.assertRaises(RunnerError):
            validate_requested_isolation_policy(weakened, policy)

    def test_host_command_paths_and_sandbox_flags_cannot_escape_isolate_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable = Path(sys.executable).resolve(strict=True)
            policy = load_strict_json(POLICY_PATH)
            command = canonical_host_command(executable, workspace)
            inner = normalize_cloud_agent_command(
                command,
                executable_path=executable,
                workspace=workspace,
                runner_policy=policy,
            )
            self.assertEqual(inner[0], "/opt/vibapp/bin/codex")
            self.assertNotIn(str(workspace), inner)
            sandbox_escape = list(command)
            sandbox_escape[6] = "danger-full-access"
            with self.assertRaises(RunnerError) as sandbox_error:
                normalize_cloud_agent_command(
                    sandbox_escape,
                    executable_path=executable,
                    workspace=workspace,
                    runner_policy=policy,
                )
            self.assertEqual(sandbox_error.exception.code, "command-invalid")
            output_escape = list(command)
            output_escape[16] = str(workspace.parent / "escaped.json")
            with self.assertRaises(RunnerError) as output_error:
                normalize_cloud_agent_command(
                    output_escape,
                    executable_path=executable,
                    workspace=workspace,
                    runner_policy=policy,
                )
            self.assertEqual(output_error.exception.code, "command-invalid")

    def test_synthetic_success_uses_fresh_identity_minimal_env_and_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "request.json").write_text("{}\n", encoding="utf-8")
            runner = SyntheticLifecycleRunner(root / "state")
            first = runner.run("job-synthetic-1", input_root, make_limits())
            second = runner.run("job-synthetic-2", input_root, make_limits())
            self.assertEqual(first.status, "synthetic-pass")
            self.assertEqual(first.input_bundle_sha256, bundle_digest(input_root, 32, 1024 * 1024))
            self.assertRegex(first.output_bundle_sha256 or "", r"^[0-9a-f]{64}$")
            self.assertNotEqual(first.isolate_id, second.isolate_id)
            self.assertNotEqual(first.allocated_uid, second.allocated_uid)
            self.assertEqual(first.identity_enforcement, "synthetic-label-only")
            self.assertFalse(first.external_request_attempted)
            self.assertFalse(first.external_request_observed)
            self.assertIsNone(first.gateway_request_id)
            self.assertTrue(first.whole_job_quiescent)
            forbidden = {"HOME", "CODEX_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "SSH_AUTH_SOCK", "DOCKER_HOST"}
            self.assertFalse(forbidden & set(first.minimal_environment_keys))
            result_paths = list((root / "state").glob("job-synthetic-1-*/output/result.json"))
            self.assertEqual(len(result_paths), 1)
            observed = json.loads(result_paths[0].read_text(encoding="utf-8"))
            self.assertFalse(forbidden & set(observed["environment_keys"]))

    def test_job_replay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "x").write_bytes(b"x")
            runner = SyntheticLifecycleRunner(root / "state")
            runner.run("job-replay", input_root, make_limits())
            with self.assertRaises(RunnerError) as caught:
                runner.run("job-replay", input_root, make_limits())
            self.assertEqual(caught.exception.code, "replay-rejected")

    def test_output_wall_pid_and_disk_caps_fail_closed_and_quiesce(self) -> None:
        cases = (
            ("output-bomb", make_limits(stdout_bytes=512)),
            ("hang", make_limits(wall_time_seconds=1, cpu_seconds=1)),
            ("fork-burst", make_limits(pids=3)),
            ("disk-bomb", make_limits(disk_bytes=128 * 1024)),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "x").write_bytes(b"x")
            runner = SyntheticLifecycleRunner(root / "state")
            for index, (mode, limits) in enumerate(cases):
                report = runner.run(f"job-cap-{index}", input_root, limits, mode=mode)
                self.assertEqual(report.status, "synthetic-rejected", mode)
                self.assertIsNotNone(report.rejected_code, mode)
                self.assertTrue(report.whole_job_quiescent, mode)
                self.assertIsNone(report.output_bundle_sha256, mode)

    def test_leader_exit_with_child_is_not_silent_and_is_quiesced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "x").write_bytes(b"x")
            report = SyntheticLifecycleRunner(root / "state").run(
                "job-child-holds", input_root, make_limits(), mode="child-holds"
            )
            self.assertEqual(report.status, "synthetic-pass")
            self.assertTrue(report.whole_job_quiescent)
            self.assertGreater(report.stdout_bytes, 0)

    def test_symlink_and_hardlink_bundle_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.write_bytes(b"secret")
            symlink_tree = root / "symlink"
            symlink_tree.mkdir()
            (symlink_tree / "escape").symlink_to(outside)
            with self.assertRaises(RunnerError) as symlink_error:
                bundle_digest(symlink_tree, 10, 1024)
            self.assertEqual(symlink_error.exception.code, "bundle-unsafe")
            hardlink_tree = root / "hardlink"
            hardlink_tree.mkdir()
            os.link(outside, hardlink_tree / "alias")
            with self.assertRaises(RunnerError) as hardlink_error:
                bundle_digest(hardlink_tree, 10, 1024)
            self.assertEqual(hardlink_error.exception.code, "bundle-unsafe")

    def test_valid_receipt_then_wrong_binding_bad_signature_nonquiescence(self) -> None:
        profile = accepted_profile()
        key = TestDetachedKey(profile.receipt_key_id)
        receipt = signed_provider_receipt(key, profile)
        accepted = validate_provider_receipt_v3(
            receipt,
            request_job_id=receipt.job_id,
            execution_attempt=receipt.execution_attempt,
            immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
            provider_execution_identity_sha256=receipt.provider_execution_identity_sha256,
            input_bundle_sha256=receipt.input_bundle_sha256,
            output_bundle_sha256=receipt.output_bundle_sha256 or "",
            isolation_policy_sha256=receipt.isolation_policy_sha256,
            profile=profile,
            verifier=key,
        )
        self.assertEqual(accepted, receipt)
        wrong_job = replace(receipt, job_id="job-wrong")
        wrong_digest = provider_receipt_digest(wrong_job)
        wrong_job = replace(wrong_job, receipt_digest_sha256=wrong_digest, attestation_signature=key.sign(bytes.fromhex(wrong_digest), key.key_id))
        with self.assertRaises(RunnerError) as binding_error:
            validate_provider_receipt_v3(
                wrong_job,
                request_job_id=receipt.job_id,
                execution_attempt=receipt.execution_attempt,
                immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
                provider_execution_identity_sha256=receipt.provider_execution_identity_sha256,
                input_bundle_sha256=receipt.input_bundle_sha256,
                output_bundle_sha256=receipt.output_bundle_sha256 or "",
                isolation_policy_sha256=receipt.isolation_policy_sha256,
                profile=profile,
                verifier=key,
            )
        self.assertEqual(binding_error.exception.code, "runner-receipt-invalid")
        binding_mutations = (
            {
                "execution_attempt": {
                    "attempt_id": "attempt-0002-fedcba9876543210",
                    "ordinal": 2,
                }
            },
            {"provider_execution_identity_sha256": "0" * 64},
        )
        for mutation in binding_mutations:
            with self.subTest(mutation=mutation):
                attacked = replace(receipt, **mutation)
                attacked_digest = provider_receipt_digest(attacked)
                attacked = replace(
                    attacked,
                    receipt_digest_sha256=attacked_digest,
                    attestation_signature=key.sign(bytes.fromhex(attacked_digest), key.key_id),
                )
                with self.assertRaises(RunnerError) as attacked_error:
                    validate_provider_receipt_v3(
                        attacked,
                        request_job_id=receipt.job_id,
                        execution_attempt=receipt.execution_attempt,
                        immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
                        provider_execution_identity_sha256=receipt.provider_execution_identity_sha256,
                        input_bundle_sha256=receipt.input_bundle_sha256,
                        output_bundle_sha256=receipt.output_bundle_sha256 or "",
                        isolation_policy_sha256=receipt.isolation_policy_sha256,
                        profile=profile,
                        verifier=key,
                    )
                self.assertEqual(attacked_error.exception.code, "runner-receipt-invalid")
        with self.assertRaises(RunnerError) as signature_error:
            validate_provider_receipt_v3(
                replace(receipt, attestation_signature="B" * 43),
                request_job_id=receipt.job_id,
                execution_attempt=receipt.execution_attempt,
                immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
                provider_execution_identity_sha256=receipt.provider_execution_identity_sha256,
                input_bundle_sha256=receipt.input_bundle_sha256,
                output_bundle_sha256=receipt.output_bundle_sha256 or "",
                isolation_policy_sha256=receipt.isolation_policy_sha256,
                profile=profile,
                verifier=key,
            )
        self.assertEqual(signature_error.exception.code, "runner-attestation-invalid")
        nonquiet = replace(receipt, whole_job_quiescent=False)
        nonquiet_digest = provider_receipt_digest(nonquiet)
        nonquiet = replace(nonquiet, receipt_digest_sha256=nonquiet_digest, attestation_signature=key.sign(bytes.fromhex(nonquiet_digest), key.key_id))
        with self.assertRaises(RunnerError) as quiet_error:
            validate_provider_receipt_v3(
                nonquiet,
                request_job_id=receipt.job_id,
                execution_attempt=receipt.execution_attempt,
                immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
                provider_execution_identity_sha256=receipt.provider_execution_identity_sha256,
                input_bundle_sha256=receipt.input_bundle_sha256,
                output_bundle_sha256=receipt.output_bundle_sha256 or "",
                isolation_policy_sha256=receipt.isolation_policy_sha256,
                profile=profile,
                verifier=key,
            )
        self.assertEqual(quiet_error.exception.code, "process-tree-not-quiescent")

    def test_gateway_truth_requires_typed_trusted_gateway_receipt(self) -> None:
        profile = accepted_profile()
        key = TestDetachedKey(profile.gateway_key_id)
        receipt = signed_gateway_receipt(key, profile)
        accepted = validate_gateway_receipt(
            receipt,
            job_id=receipt.job_id,
            immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
            input_bundle_sha256=receipt.input_bundle_sha256,
            executable_sha256=receipt.executed_executable_sha256,
            destination_policy_sha256=receipt.destination_policy_sha256,
            credential_handle_sha256=receipt.credential_handle_sha256,
            accepted_key_id=profile.gateway_key_id,
            verifier=key,
        )
        self.assertEqual(accepted.gateway_request_id, "gateway-request-1")
        with self.assertRaises(RunnerError) as untyped:
            validate_gateway_receipt(
                asdict(receipt),
                job_id=receipt.job_id,
                immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
                input_bundle_sha256=receipt.input_bundle_sha256,
                executable_sha256=receipt.executed_executable_sha256,
                destination_policy_sha256=receipt.destination_policy_sha256,
                credential_handle_sha256=receipt.credential_handle_sha256,
                accepted_key_id=profile.gateway_key_id,
                verifier=key,
            )
        self.assertEqual(untyped.exception.code, "gateway-receipt-invalid")
        with self.assertRaises(RunnerError) as bad_signature:
            validate_gateway_receipt(
                replace(receipt, attestation_signature="B" * 43),
                job_id=receipt.job_id,
                immutable_task_digest_sha256=receipt.immutable_task_digest_sha256,
                input_bundle_sha256=receipt.input_bundle_sha256,
                executable_sha256=receipt.executed_executable_sha256,
                destination_policy_sha256=receipt.destination_policy_sha256,
                credential_handle_sha256=receipt.credential_handle_sha256,
                accepted_key_id=profile.gateway_key_id,
                verifier=key,
            )
        self.assertEqual(bad_signature.exception.code, "gateway-attestation-invalid")


if __name__ == "__main__":
    unittest.main()
