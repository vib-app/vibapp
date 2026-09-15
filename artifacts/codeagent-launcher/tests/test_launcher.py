from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codeagent_launcher import (  # noqa: E402
    BackendLaunch,
    BackendObservation,
    BackendState,
    BackendSuccess,
    ConflictError,
    ContractError,
    DuplicateKeyError,
    ExecutorKind,
    KubernetesExecutor,
    LaunchRequest,
    LauncherService,
    ProviderProfile,
    RECEIPT_SCHEMA_VERSION,
    ResultUnavailableError,
    SCHEMA_VERSION,
    SuccessReceipt,
)


def digest(character: str) -> str:
    return character * 64


def request_mapping(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": "job-001",
        "attempt_id": "attempt-001",
        "idempotency_key": "idem-001",
        "provider_profile_id": "codex-offline-v1",
        "provider_id": "codex",
        "model": "gpt-5.6-codex",
        "task_digest_sha256": digest("1"),
        "input_digest_sha256": digest("2"),
        "prompt_digest_sha256": digest("3"),
        "resource_policy_id": "codeagent-small-v1",
        "network_policy_id": "network-none-v1",
    }
    value.update(changes)
    return value


def profile(*, kind: ExecutorKind = ExecutorKind.DOCKER) -> ProviderProfile:
    return ProviderProfile(
        provider_profile_id="codex-offline-v1",
        provider_profile_digest_sha256=digest("4"),
        provider_id="codex",
        allowed_models=frozenset({"gpt-5.6-codex"}),
        executor_kind=kind,
        image_digest_sha256=digest("5"),
        resource_policy_id="codeagent-small-v1",
        resource_policy_digest_sha256=digest("6"),
        network_policy_id="network-none-v1",
        network_policy_digest_sha256=digest("7"),
        max_output_bytes=4096,
        output_media_type="application/vnd.vibapp.codeagent-source",
    )


class FakeExecutor:
    kind = ExecutorKind.DOCKER

    def __init__(self) -> None:
        self.launch_calls = 0
        self.status_calls = 0
        self.cancel_calls = 0
        self.launch_error: Exception | None = None
        self.launch_response_id: str | None = None
        self.assigned_execution_id: str | None = None
        self.observations: list[BackendObservation] = []
        self.cancel_observations: list[BackendObservation] = []

    def launch(
        self,
        request: LaunchRequest,
        selected_profile: ProviderProfile,
        backend_execution_id: str,
    ) -> BackendLaunch:
        del request, selected_profile
        self.launch_calls += 1
        self.assigned_execution_id = backend_execution_id
        if self.launch_error is not None:
            raise self.launch_error
        return BackendLaunch(self.launch_response_id or backend_execution_id)

    def status(self, backend_execution_id: str) -> BackendObservation:
        self.status_calls += 1
        if self.observations:
            return self.observations.pop(0)
        return BackendObservation(backend_execution_id, BackendState.RUNNING)

    def cancel(self, backend_execution_id: str) -> BackendObservation:
        self.cancel_calls += 1
        if self.cancel_observations:
            return self.cancel_observations.pop(0)
        return BackendObservation(backend_execution_id, BackendState.RUNNING)


def success_for(
    request: LaunchRequest,
    selected_profile: ProviderProfile,
    execution_id: str,
    output: bytes = b"source archive",
) -> BackendSuccess:
    import hashlib

    receipt = SuccessReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        job_id=request.job_id,
        attempt_id=request.attempt_id,
        idempotency_key=request.idempotency_key,
        request_digest_sha256=request.canonical_digest_sha256(),
        backend_execution_id=execution_id,
        executor_kind=selected_profile.executor_kind,
        provider_profile_id=selected_profile.provider_profile_id,
        provider_profile_digest_sha256=(
            selected_profile.provider_profile_digest_sha256
        ),
        provider_id=request.provider_id,
        model=request.model,
        image_digest_sha256=selected_profile.image_digest_sha256,
        resource_policy_id=selected_profile.resource_policy_id,
        resource_policy_digest_sha256=(
            selected_profile.resource_policy_digest_sha256
        ),
        network_policy_id=selected_profile.network_policy_id,
        network_policy_digest_sha256=(
            selected_profile.network_policy_digest_sha256
        ),
        task_digest_sha256=request.task_digest_sha256,
        input_digest_sha256=request.input_digest_sha256,
        prompt_digest_sha256=request.prompt_digest_sha256,
        output_digest_sha256=hashlib.sha256(output).hexdigest(),
        output_media_type=selected_profile.output_media_type,
        output_size_bytes=len(output),
        cleanup_confirmed=True,
        whole_job_quiescent=True,
    )
    return BackendSuccess(output, selected_profile.output_media_type, receipt)


class LauncherContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = profile()
        self.executor = FakeExecutor()
        self.service = LauncherService(
            profiles=[self.profile], executors=[self.executor]
        )
        self.request = LaunchRequest.from_mapping(request_mapping())

    def submit(self) -> dict[str, object]:
        return self.service.submit(self.request)

    def test_submit_is_closed_and_never_accepts_execution_primitives(self) -> None:
        forbidden = [
            "image",
            "argv",
            "command",
            "env",
            "mount",
            "volume",
            "host_path",
            "credential",
            "token",
            "docker_socket",
            "kubeconfig",
        ]
        for field in forbidden:
            with self.subTest(field=field), self.assertRaises(ContractError):
                LaunchRequest.from_mapping(request_mapping(**{field: "attacker"}))

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = json.dumps(request_mapping())
        raw = raw[:-1] + ',"job_id":"second"}'
        with self.assertRaises(DuplicateKeyError):
            LaunchRequest.from_json(raw)

    def test_malformed_and_bounded_request_values_fail(self) -> None:
        cases = [
            {"job_id": "x" * 129},
            {"job_id": "../escape"},
            {"model": " model-with-leading-space"},
            {"model": "model-with-trailing-space "},
            {"model": "-provider-option-like"},
            {"model": "model\u007fcontrol"},
            {"model": "m" * 257},
            {"task_digest_sha256": "A" * 64},
            {"prompt_digest_sha256": "0" * 63},
            {"schema_version": "future"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                LaunchRequest.from_mapping(request_mapping(**changes))
        with self.assertRaises(ContractError):
            LaunchRequest.from_json(" " * 8193)
        with self.assertRaises(ContractError):
            LaunchRequest.from_mapping({**request_mapping(), 7: "non-string-key"})
        with self.assertRaises(ContractError):
            LaunchRequest.from_json("\ud800")

    def test_provider_model_ids_are_not_overrestricted(self) -> None:
        model = "provider.example/org/model:thinking@2026-08"
        request = LaunchRequest.from_mapping(request_mapping(model=model))
        self.assertEqual(request.model, model)

    def test_profile_references_are_server_authoritative(self) -> None:
        for changes in [
            {"provider_id": "claude"},
            {"model": "gpt-5.6"},
            {"resource_policy_id": "unbounded"},
            {"network_policy_id": "host"},
        ]:
            request = LaunchRequest.from_mapping(request_mapping(**changes))
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.service.submit(request)
        self.assertEqual(self.executor.launch_calls, 0)

    def test_submit_assigns_exact_backend_id_before_executor_call(self) -> None:
        status = self.submit()
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["backend_execution_id"], self.executor.assigned_execution_id)
        self.assertTrue(str(status["backend_execution_id"]).startswith("vibapp-"))
        self.assertEqual(len(str(status["backend_execution_id"])), 71)

    def test_idempotent_replay_does_not_launch_twice(self) -> None:
        first = self.submit()
        second = self.service.submit(self.request)
        self.assertEqual(first, second)
        self.assertEqual(self.executor.launch_calls, 1)

        changed = LaunchRequest.from_mapping(
            request_mapping(prompt_digest_sha256=digest("8"))
        )
        with self.assertRaises(ConflictError):
            self.service.submit(changed)
        self.assertEqual(self.executor.launch_calls, 1)

    def test_job_attempt_identity_conflict_is_not_a_retry(self) -> None:
        self.submit()
        changed = LaunchRequest.from_mapping(
            request_mapping(idempotency_key="idem-002")
        )
        with self.assertRaises(ConflictError):
            self.service.submit(changed)

    def test_events_are_monotonic_closed_bounded_and_cursor_pageable(self) -> None:
        self.submit()
        first = self.service.list_events(
            self.request.job_id, self.request.attempt_id, limit=2
        )
        self.assertEqual(first["cursor"], 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(
            [(event["sequence"], event["phase"], event["code"]) for event in first["events"]],
            [
                (1, "queued", "job-queued"),
                (2, "starting", "executor-starting"),
            ],
        )
        second = self.service.list_events(
            self.request.job_id,
            self.request.attempt_id,
            after_sequence=first["cursor"],
            limit=2,
        )
        self.assertEqual(second["events"], [
            {"sequence": 3, "phase": "running", "code": "backend-running"}
        ])
        self.assertFalse(second["has_more"])
        serialized = json.dumps(first, sort_keys=True)
        for forbidden in ["prompt", "secret", "token", "exception", "source archive"]:
            self.assertNotIn(forbidden, serialized.lower())
        for event in first["events"]:
            self.assertEqual(set(event), {"sequence", "phase", "code"})

    def test_event_cursor_and_limit_bounds_fail_closed(self) -> None:
        self.submit()
        for after_sequence, limit in [(-1, 1), (0, 0), (0, 101), (99, 1)]:
            with self.subTest(
                after_sequence=after_sequence, limit=limit
            ), self.assertRaises(ContractError):
                self.service.list_events(
                    self.request.job_id,
                    self.request.attempt_id,
                    after_sequence=after_sequence,
                    limit=limit,
                )

    def test_result_is_released_only_after_exact_quiescent_receipt(self) -> None:
        status = self.submit()
        execution_id = str(status["backend_execution_id"])
        success = success_for(self.request, self.profile, execution_id)
        self.executor.observations.append(
            BackendObservation(
                execution_id,
                BackendState.SUCCEEDED,
                cleanup_confirmed=True,
                whole_job_quiescent=True,
                success=success,
            )
        )
        result = self.service.result(self.request.job_id, self.request.attempt_id)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["output"]["sha256"], success.receipt.output_digest_sha256)
        self.assertEqual(result["output"]["size_bytes"], len(success.output))
        self.assertTrue(result["receipt"]["cleanup_confirmed"])
        self.assertTrue(result["receipt"]["whole_job_quiescent"])

        events = self.service.list_events(
            self.request.job_id, self.request.attempt_id
        )["events"]
        self.assertEqual(events[-2]["phase"], "quiescing")
        self.assertEqual(events[-1], {
            "sequence": 5,
            "phase": "terminal",
            "code": "job-succeeded",
        })

    def test_non_quiescent_success_returns_no_output_until_cleanup(self) -> None:
        status = self.submit()
        execution_id = str(status["backend_execution_id"])
        success = success_for(self.request, self.profile, execution_id)
        self.executor.observations.extend(
            [
                BackendObservation(
                    execution_id,
                    BackendState.SUCCEEDED,
                    cleanup_confirmed=False,
                    whole_job_quiescent=False,
                    success=success,
                ),
                BackendObservation(
                    execution_id,
                    BackendState.SUCCEEDED,
                    cleanup_confirmed=True,
                    whole_job_quiescent=True,
                    success=success,
                ),
            ]
        )
        state = self.service.status(self.request.job_id, self.request.attempt_id)
        self.assertEqual(state["state"], "cleanup-pending")
        self.assertEqual(state["failure_code"], "cleanup-not-confirmed")
        result = self.service.result(self.request.job_id, self.request.attempt_id)
        self.assertEqual(result["state"], "succeeded")

    def test_success_receipt_binds_retry_provider_model_policy_and_output(self) -> None:
        mutation_cases = {
            "job_id": "job-other",
            "attempt_id": "attempt-other",
            "idempotency_key": "idem-other",
            "request_digest_sha256": digest("8"),
            "backend_execution_id": "other-backend",
            "provider_profile_id": "other-profile",
            "provider_profile_digest_sha256": digest("8"),
            "provider_id": "other-provider",
            "model": "other-model",
            "image_digest_sha256": digest("8"),
            "resource_policy_id": "other-policy",
            "resource_policy_digest_sha256": digest("8"),
            "network_policy_id": "other-network",
            "network_policy_digest_sha256": digest("8"),
            "task_digest_sha256": digest("8"),
            "input_digest_sha256": digest("8"),
            "prompt_digest_sha256": digest("8"),
            "output_digest_sha256": digest("8"),
            "output_media_type": "application/octet-stream",
            "output_size_bytes": 999,
            "cleanup_confirmed": False,
            "whole_job_quiescent": False,
        }
        for field, wrong_value in mutation_cases.items():
            with self.subTest(field=field):
                selected_profile = profile()
                executor = FakeExecutor()
                service = LauncherService(
                    profiles=[selected_profile], executors=[executor]
                )
                request = LaunchRequest.from_mapping(request_mapping())
                submitted = service.submit(request)
                execution_id = str(submitted["backend_execution_id"])
                success = success_for(request, selected_profile, execution_id)
                bad_success = replace(
                    success, receipt=replace(success.receipt, **{field: wrong_value})
                )
                executor.observations.append(
                    BackendObservation(
                        execution_id,
                        BackendState.SUCCEEDED,
                        cleanup_confirmed=True,
                        whole_job_quiescent=True,
                        success=bad_success,
                    )
                )
                state = service.status(request.job_id, request.attempt_id)
                self.assertEqual(state["state"], "held")
                self.assertEqual(state["failure_code"], "invalid-terminal-receipt")
                with self.assertRaises(ResultUnavailableError):
                    service.result(request.job_id, request.attempt_id)

    def test_output_over_server_policy_is_held(self) -> None:
        submitted = self.submit()
        execution_id = str(submitted["backend_execution_id"])
        success = success_for(
            self.request, self.profile, execution_id, output=b"x" * 4097
        )
        self.executor.observations.append(
            BackendObservation(
                execution_id,
                BackendState.SUCCEEDED,
                cleanup_confirmed=True,
                whole_job_quiescent=True,
                success=success,
            )
        )
        state = self.service.status(self.request.job_id, self.request.attempt_id)
        self.assertEqual(state["state"], "held")
        with self.assertRaises(ResultUnavailableError):
            self.service.result(self.request.job_id, self.request.attempt_id)

    def test_receipt_boolean_and_integer_types_are_not_interchangeable(self) -> None:
        for changes in [
            {"cleanup_confirmed": 1},
            {"whole_job_quiescent": 1},
            {"output_size_bytes": True},
            {"executor_kind": "docker"},
        ]:
            with self.subTest(changes=changes):
                selected_profile = profile()
                executor = FakeExecutor()
                service = LauncherService(
                    profiles=[selected_profile], executors=[executor]
                )
                request = LaunchRequest.from_mapping(request_mapping())
                submitted = service.submit(request)
                execution_id = str(submitted["backend_execution_id"])
                success = success_for(request, selected_profile, execution_id)
                bad_success = replace(
                    success, receipt=replace(success.receipt, **changes)
                )
                executor.observations.append(
                    BackendObservation(
                        execution_id,
                        BackendState.SUCCEEDED,
                        cleanup_confirmed=True,
                        whole_job_quiescent=True,
                        success=bad_success,
                    )
                )
                state = service.status(request.job_id, request.attempt_id)
                self.assertEqual(state["state"], "held")

    def test_launch_response_loss_reuses_exact_id_and_recovers_cleanup(self) -> None:
        self.executor.launch_error = RuntimeError("synthetic lost response")
        state = self.submit()
        self.assertEqual(state["state"], "cleanup-pending")
        self.assertEqual(state["failure_code"], "launch-outcome-unknown")
        assigned = self.executor.assigned_execution_id
        self.assertEqual(state["backend_execution_id"], assigned)

        replay = self.service.submit(self.request)
        self.assertEqual(replay, state)
        self.assertEqual(self.executor.launch_calls, 1)

        self.executor.observations.append(
            BackendObservation(str(assigned), BackendState.RUNNING)
        )
        recovered = self.service.status(self.request.job_id, self.request.attempt_id)
        self.assertEqual(recovered["state"], "running")
        self.executor.cancel_observations.append(
            BackendObservation(
                str(assigned),
                BackendState.CANCELLED,
                cleanup_confirmed=True,
                whole_job_quiescent=True,
            )
        )
        cancelled = self.service.cancel(self.request.job_id, self.request.attempt_id)
        self.assertEqual(cancelled["state"], "cancelled")

    def test_wrong_backend_observation_never_fabricates_cleanup(self) -> None:
        submitted = self.submit()
        execution_id = str(submitted["backend_execution_id"])
        self.executor.observations.append(
            BackendObservation(
                "wrong-id",
                BackendState.CANCELLED,
                cleanup_confirmed=True,
                whole_job_quiescent=True,
            )
        )
        state = self.service.status(self.request.job_id, self.request.attempt_id)
        self.assertEqual(state["state"], "cleanup-pending")
        self.assertEqual(state["failure_code"], "backend-execution-id-mismatch")

        self.executor.cancel_observations.append(
            BackendObservation(
                execution_id,
                BackendState.CANCELLED,
                cleanup_confirmed=True,
                whole_job_quiescent=True,
            )
        )
        state = self.service.cancel(self.request.job_id, self.request.attempt_id)
        self.assertEqual(state["state"], "cancelled")

    def test_failed_backend_is_terminal_only_after_cleanup(self) -> None:
        submitted = self.submit()
        execution_id = str(submitted["backend_execution_id"])
        self.executor.observations.extend(
            [
                BackendObservation(execution_id, BackendState.FAILED),
                BackendObservation(
                    execution_id,
                    BackendState.FAILED,
                    cleanup_confirmed=True,
                    whole_job_quiescent=True,
                ),
            ]
        )
        first = self.service.status(self.request.job_id, self.request.attempt_id)
        self.assertEqual(first["state"], "cleanup-pending")
        second = self.service.status(self.request.job_id, self.request.attempt_id)
        self.assertEqual(second["state"], "failed")
        self.assertEqual(second["failure_code"], "backend-failed")

    def test_cancel_is_idempotent_after_terminal_cleanup(self) -> None:
        submitted = self.submit()
        execution_id = str(submitted["backend_execution_id"])
        self.executor.cancel_observations.append(
            BackendObservation(
                execution_id,
                BackendState.CANCELLED,
                cleanup_confirmed=True,
                whole_job_quiescent=True,
            )
        )
        first = self.service.cancel(self.request.job_id, self.request.attempt_id)
        second = self.service.cancel(self.request.job_id, self.request.attempt_id)
        self.assertEqual(first, second)
        self.assertEqual(self.executor.cancel_calls, 1)

    def test_kubernetes_executor_is_fail_closed_placeholder(self) -> None:
        selected_profile = profile(kind=ExecutorKind.KUBERNETES)
        service = LauncherService(
            profiles=[selected_profile], executors=[KubernetesExecutor()]
        )
        state = service.submit(LaunchRequest.from_mapping(request_mapping()))
        self.assertEqual(state["executor_kind"], "kubernetes")
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["failure_code"], "executor-unavailable")
        with self.assertRaises(ResultUnavailableError):
            service.result("job-001", "attempt-001")

    def test_json_schemas_parse_and_keep_objects_closed(self) -> None:
        schema_paths = sorted((ROOT / "schemas").glob("*.schema.json"))
        self.assertEqual(len(schema_paths), 4)
        for schema_path in schema_paths:
            with self.subTest(schema=schema_path.name):
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])
        submit_schema = json.loads(
            (ROOT / "schemas" / "submit-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        for forbidden in ["image", "argv", "env", "mount", "host_path", "credential"]:
            self.assertNotIn(forbidden, submit_schema["properties"])


if __name__ == "__main__":
    unittest.main()
