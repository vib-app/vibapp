from __future__ import annotations

import copy
from dataclasses import replace
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

BASE = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(BASE))

from cloud_agent import (  # noqa: E402
    AcceptedRunnerProfile, CloudAgentWorker, CONSENT_CLAIM_VERSION, CONTRACT_DIGEST_PIN,
    INSTRUCTIONS_DIGEST,
    LEGACY_POLICY_VERSION, LEGACY_TASK_SCHEMA_VERSION, OTHER_PROVIDER_INSTRUCTIONS_DIGEST,
    POLICY_VERSION,
    SUPPORTED_TASK_PROVIDERS,
    PROVIDER_REQUEST_VERSION, SOURCE_HANDOFF_VERSION, TASK_SCHEMA_VERSION, WORLD_IMPORTS,
    ProviderRunnerReceipt, ProviderRunnerRequest, RUNNER_ATTESTATION_TYPE, RUNNER_RECEIPT_VERSION,
    RUNNER_REQUEST_VERSION,
    WorkerError, audit_workspace, canonical_cargo_manifest, canonical_json, claim_consent, codex_command,
    create_prompt, immutable_task_digest, load_json, process_snapshot,
    normalized_source_path, prepare_workspace, provider_instructions_digest, provider_receipt_digest,
    provider_request, required_isolation_policy,
    resolve_executable_identity, run_bounded_provider, rust_code_projection, sha256_bytes,
    validate_authorization, validate_cargo_manifest, validate_generated_semantics,
    validate_provider_receipt, validate_provider_result, validate_task_schema,
    validate_json_schema_instance, validate_resolved_provider_identity,
    validate_runner_request_model,
)

VALID = BASE / "fixtures/valid-task.json"
DENIED = BASE / "fixtures/missing-consent-task.json"
INVALID = BASE / "fixtures/schema-invalid-task.json"
FIXED_CLOCK = dt.datetime(2026, 8, 23, 20, 45, tzinfo=dt.timezone.utc)


def task_fixture() -> dict:
    return json.loads(VALID.read_text(encoding="utf-8"))


def legacy_v2_task_fixture() -> dict:
    task = task_fixture()
    task["schema_version"] = LEGACY_TASK_SCHEMA_VERSION
    del task["execution_attempt"]
    del task["provider_execution_identity"]
    del task["consent"]["attempt_id"]
    del task["consent"]["provider_execution_identity_sha256"]
    task["consent"]["policy_version"] = LEGACY_POLICY_VERSION
    task["consent"]["uploaded_data_classes"] = task["consent"][
        "uploaded_data_classes"
    ][:-1]
    return rebind(task)


def rebind(task: dict) -> dict:
    task["need_spec_digest_sha256"] = sha256_bytes(canonical_json(task["need_spec"]))
    if task.get("schema_version") == TASK_SCHEMA_VERSION:
        task["consent"]["attempt_id"] = task["execution_attempt"]["attempt_id"]
        task["consent"]["provider_execution_identity_sha256"] = task[
            "provider_execution_identity"
        ]["identity_sha256"]
    task["immutable_task_digest_sha256"] = immutable_task_digest(task, CONTRACT_DIGEST_PIN)
    task["consent"]["payload_digest_sha256"] = task["immutable_task_digest_sha256"]
    return task


def rebind_provider_identity(task: dict) -> dict:
    identity = task["provider_execution_identity"]
    endpoint = identity["endpoint"]
    endpoint["endpoint_sha256"] = sha256_bytes(
        canonical_json(
            {
                "kind": endpoint["kind"],
                "canonical_endpoint": endpoint["canonical_endpoint"],
            }
        )
    )
    identity["identity_sha256"] = sha256_bytes(
        canonical_json(
            {
                "schema_version": identity["schema_version"],
                "endpoint": endpoint,
                "runtime": identity["runtime"],
                "non_secret_config_sha256": identity["non_secret_config_sha256"],
            }
        )
    )
    return task


def synthetic_provider_identity(provider: str, *, config_digest: str = "c" * 64) -> dict:
    if provider == "opencode":
        endpoint = {"kind": "https", "canonical_endpoint": "https://api.example.com/v1"}
        package = "opencode-ai"
    else:
        endpoint = {"kind": "provider-managed", "canonical_endpoint": "provider-managed"}
        package = "provider-cli"
    endpoint["endpoint_sha256"] = sha256_bytes(canonical_json(endpoint))
    identity = {
        "schema_version": "vibapp.provider-execution-identity.experimental-v1",
        "endpoint": endpoint,
        "runtime": {
            "adapter_id": "local-codeagent-adapter",
            "adapter_version": "experimental-v1",
            "adapter_sha256": "a" * 64,
            "package_id": package,
            "package_version": "fixture-1.0.0",
            "executable_sha256": "b" * 64,
        },
        "non_secret_config_sha256": config_digest,
    }
    identity["identity_sha256"] = sha256_bytes(canonical_json(identity))
    return identity


def service_task_fixture() -> dict:
    task = task_fixture()
    task["need_spec"]["goal"] = "Run a local background health probe while the app is enabled."
    task["need_spec"]["requirements"] = [
        {
            "requirement_id": "req-service",
            "text": "Handle daemon service lifecycle events and report healthy status.",
            "priority": "must-have",
            "acceptance_examples": ["A daemon-issued service start event records a running probe."],
        }
    ]
    task["package_intent"]["entrypoints"] = [
        {
            "id": "probe",
            "kind": "service",
            "label": "Health probe",
            "initial_route": None,
            "triggers": ["on-enable", "manual"],
        }
    ]
    task["target"]["wit_world"] = "service-only-reference"
    task["target"]["app_kind"] = "service"
    task["target"]["required_imports"] = WORLD_IMPORTS["service-only-reference"]
    task["target"]["required_capabilities"] = [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
    ]
    return rebind(task)


def reminder_task_fixture() -> dict:
    task = task_fixture()
    task["need_spec"]["goal"] = "Create a weekday water reminder with reliable background notification."
    task["need_spec"]["requirements"] = [
        {
            "requirement_id": "req-reminder",
            "text": "Let the user select a reminder time and pause and resume the reminder.",
            "priority": "must-have",
            "acceptance_examples": [
                "Set a weekday reminder, pause it, resume it, and receive one catch-up notification."
            ],
        }
    ]
    task["package_intent"]["entrypoints"] = [
        {"id": "main", "kind": "launcher-ui", "label": "Reminder", "initial_route": "home"},
        {
            "id": "reminder",
            "kind": "service",
            "label": "Reminder service",
            "initial_route": None,
            "triggers": ["on-enable", "scheduler", "manual"],
        },
    ]
    task["target"]["wit_world"] = "hybrid-reference"
    task["target"]["app_kind"] = "hybrid"
    task["target"]["required_imports"] = WORLD_IMPORTS["hybrid-reference"]
    task["target"]["required_capabilities"] = [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/notification@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
    ]
    return rebind(task)


def synthetic_limits(**overrides: int) -> dict[str, int]:
    limits = {
        "wall_time_seconds": 10, "cpu_seconds": 4,
        "memory_bytes": 256 * 1024 * 1024, "pids": 8,
        "workspace_bytes": 2 * 1024 * 1024,
        "stdout_bytes": 4096, "stderr_bytes": 4096,
        "source_bytes": 1024 * 1024, "source_files": 16,
    }
    limits.update(overrides)
    return limits


def active(pid: int) -> bool:
    item = process_snapshot().get(pid)
    return item is not None and not str(item["state"]).startswith("Z")


def synthetic_executable_pin(root: Path) -> tuple[Path, dict[str, str]]:
    executable = root / "0.149.0/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"reviewed inert executable")
    executable.chmod(0o700)
    return executable, {
        "configured_path": str(executable),
        "resolved_path": str(executable),
        "version": "codex-cli 0.149.0",
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


ACCEPTED_RUNNER = AcceptedRunnerProfile(
    runner_id="runner-fixture-001",
    runner_identity_sha256="1" * 64,
    attestation_key_id="key-fixture-001",
)


class NeverExecutedRunner:
    def preflight(self, required_policy: dict) -> dict:
        return copy.deepcopy(required_policy)

    def execute(self, request) -> ProviderRunnerReceipt:
        raise AssertionError("unaccepted runner must never execute")


class PreflightSentinelRunner:
    def __init__(self):
        self.preflight_calls = 0

    def preflight(self, required_policy: dict) -> dict:
        self.preflight_calls += 1
        raise AssertionError("placeholder trust material reached runner preflight")

    def execute(self, request) -> ProviderRunnerReceipt:
        raise AssertionError("placeholder trust material reached runner execution")


class AcceptFixtureSignature:
    def verify(self, digest: bytes, signature: str, key_id: str) -> bool:
        return len(digest) == 32 and signature == "A" * 43 and key_id == ACCEPTED_RUNNER.attestation_key_id


class RejectFixtureSignature:
    def verify(self, digest: bytes, signature: str, key_id: str) -> bool:
        return False


def receipt_request(workspace: Path) -> ProviderRunnerRequest:
    task = validate_task_schema(task_fixture())
    policy = required_isolation_policy(task)
    return ProviderRunnerRequest(
        schema_version=RUNNER_REQUEST_VERSION,
        document_type="provider-runner-request",
        job_id=task["job_id"],
        execution_attempt=copy.deepcopy(task["execution_attempt"]),
        immutable_task_digest_sha256=task["immutable_task_digest_sha256"],
        provider_execution_identity=copy.deepcopy(task["provider_execution_identity"]),
        provider_execution_identity_sha256=task["provider_execution_identity"]["identity_sha256"],
        provider=task["provider"],
        model=task["model"],
        executable_identity={"sha256": "2" * 64},
        command=["/reviewed/codex", "exec", "--model", task["model"]],
        prompt=b"fixture-only",
        workspace=workspace,
        limits=task["limits"],
        isolation_policy=policy,
        isolation_policy_sha256=sha256_bytes(canonical_json(policy)),
        input_bundle_sha256="3" * 64,
    )


def signed_receipt(request: ProviderRunnerRequest, **changes) -> ProviderRunnerReceipt:
    receipt = ProviderRunnerReceipt(
        schema_version=RUNNER_RECEIPT_VERSION,
        document_type="provider-runner-receipt",
        job_id=request.job_id,
        execution_attempt=copy.deepcopy(request.execution_attempt),
        immutable_task_digest_sha256=request.immutable_task_digest_sha256,
        provider_execution_identity_sha256=request.provider_execution_identity_sha256,
        input_bundle_sha256=request.input_bundle_sha256,
        output_bundle_sha256="4" * 64,
        runner_id=ACCEPTED_RUNNER.runner_id,
        runner_identity_sha256=ACCEPTED_RUNNER.runner_identity_sha256,
        isolation_policy_sha256=request.isolation_policy_sha256,
        external_request_attempted=True,
        external_request_observed=True,
        gateway_request_id="gateway-fixture-001",
        whole_job_quiescent=True,
        exit_code=0,
        stdout_bytes=12,
        stderr_bytes=0,
        executed_executable_sha256=request.executable_identity["sha256"],
        attestation_type=RUNNER_ATTESTATION_TYPE,
        attestation_key_id=ACCEPTED_RUNNER.attestation_key_id,
        receipt_digest_sha256="0" * 64,
        attestation_signature="A" * 43,
    )
    receipt = replace(receipt, **changes)
    return replace(receipt, receipt_digest_sha256=provider_receipt_digest(receipt))


class CloudAgentTests(unittest.TestCase):
    def test_valid_complete_consented_task(self) -> None:
        task = validate_task_schema(load_json(VALID, 128 * 1024, "valid fixture"))
        validate_authorization(task, FIXED_CLOCK)
        self.assertEqual(task["immutable_task_digest_sha256"], immutable_task_digest(task))

    def test_every_supported_provider_has_an_exact_consent_bound_prompt(self) -> None:
        for provider in SUPPORTED_TASK_PROVIDERS:
            with self.subTest(provider=provider):
                task = task_fixture()
                task["provider"] = provider
                task["consent"]["provider"] = provider
                task["consent"]["instructions_digest_sha256"] = provider_instructions_digest(provider)
                task["provider_execution_identity"] = synthetic_provider_identity(provider)
                rebind(task)

                parsed = validate_task_schema(task)
                validate_authorization(parsed, FIXED_CLOCK)
                prompt = create_prompt(parsed, CONTRACT_DIGEST_PIN).decode("utf-8")
                self.assertIn(provider, prompt)
                self.assertIn(provider_instructions_digest(provider), prompt)
                with tempfile.TemporaryDirectory(prefix="vibapp-provider-claim-") as temporary:
                    output = Path(temporary) / "output"
                    output.mkdir(mode=0o700)
                    claimed = json.loads(claim_consent(output, parsed).read_text(encoding="utf-8"))
                    self.assertEqual(
                        claimed["instructions_digest_sha256"],
                        provider_instructions_digest(provider),
                    )

                tampered = copy.deepcopy(parsed)
                tampered["consent"]["instructions_digest_sha256"] = "0" * 64
                with self.assertRaises(WorkerError) as caught:
                    validate_authorization(tampered, FIXED_CLOCK)
                self.assertEqual(caught.exception.code, "consent-required")

        self.assertNotEqual(provider_instructions_digest("openai-codex"), INSTRUCTIONS_DIGEST)
        self.assertNotEqual(
            provider_instructions_digest("anthropic-claude-code"),
            OTHER_PROVIDER_INSTRUCTIONS_DIGEST,
        )

    def test_schema_valid_but_missing_authorization_is_rejected(self) -> None:
        task = validate_task_schema(load_json(DENIED, 128 * 1024, "denied fixture"))
        with self.assertRaises(WorkerError) as caught:
            validate_authorization(task, FIXED_CLOCK)
        self.assertEqual(caught.exception.code, "consent-required")

    def test_schema_mismatch_is_rejected_before_work(self) -> None:
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(load_json(INVALID, 128 * 1024, "invalid fixture"))
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_fail_closed_schema_subset_covers_refs_combinators_bounds_and_uniqueness(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "item": {
                    "type": "string",
                    "pattern": "^item-[0-9]+$",
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["count", "items", "choice", "flexible"],
                    "properties": {
                        "count": {"type": "integer", "minimum": 1, "maximum": 3},
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "uniqueItems": True,
                            "items": {"$ref": "#/$defs/item"},
                        },
                        "choice": {
                            "oneOf": [
                                {"type": "string", "pattern": "^alpha"},
                                {"type": "string", "pattern": "omega$"},
                            ]
                        },
                        "flexible": {
                            "anyOf": [
                                {"type": "integer", "minimum": 10},
                                {"const": "manual"},
                            ]
                        },
                    },
                },
                "nested": {"$ref": "#/$defs/payload"},
            },
            "$ref": "#/$defs/nested",
        }
        valid = {
            "count": 3,
            "items": ["item-1", "item-2"],
            "choice": "alpha-only",
            "flexible": "manual",
        }
        validate_json_schema_instance(valid, schema, context="synthetic schema")

        mutations = (
            lambda value: value.__setitem__("extra", True),
            lambda value: value.__setitem__("count", 4),
            lambda value: value["items"].__setitem__(1, "item-1"),
            lambda value: value["items"].__setitem__(0, "bad-pattern"),
            lambda value: value.__setitem__("choice", "alpha-omega"),
            lambda value: value.__setitem__("flexible", 9),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                invalid = copy.deepcopy(valid)
                mutate(invalid)
                with self.assertRaises(WorkerError) as caught:
                    validate_json_schema_instance(invalid, schema, context="synthetic schema")
                self.assertEqual(caught.exception.code, "schema-invalid")

    def test_unknown_nested_schema_keyword_never_silently_falls_back(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "payload": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "format": "hostname"},
                    },
                }
            },
            "$ref": "#/$defs/payload",
        }
        with self.assertRaises(WorkerError) as caught:
            validate_json_schema_instance({"name": "example.com"}, schema)
        self.assertEqual(caught.exception.code, "schema-validator-unavailable")

    def test_historical_v1_task_without_required_capabilities_fails_closed(self) -> None:
        task = task_fixture()
        task["schema_version"] = "vibapp.cloud-codeagent-task.experimental-v1"
        del task["target"]["required_capabilities"]
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(task)
        self.assertEqual(caught.exception.code, "schema-invalid")
        self.assertIn("unsupported cloud task schema_version", str(caught.exception))

    def test_historical_v2_is_schema_readable_but_execution_fails_closed(self) -> None:
        task = validate_task_schema(legacy_v2_task_fixture())
        self.assertEqual(task["schema_version"], LEGACY_TASK_SCHEMA_VERSION)
        with self.assertRaises(WorkerError) as caught:
            validate_authorization(task, FIXED_CLOCK)
        self.assertEqual(caught.exception.code, "legacy-provider-identity-unbound")

    def test_v2_and_v3_resource_ceilings_are_versioned_without_cpu_expansion(self) -> None:
        legacy = legacy_v2_task_fixture()
        legacy["limits"]["wall_time_seconds"] = 1800
        rebind(legacy)
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(legacy)
        self.assertEqual(caught.exception.code, "schema-invalid")

        current = task_fixture()
        current["limits"]["wall_time_seconds"] = 1800
        current["limits"]["cpu_seconds"] = 900
        rebind(current)
        validate_authorization(validate_task_schema(current), FIXED_CLOCK)

        cpu_expanded = copy.deepcopy(current)
        cpu_expanded["limits"]["cpu_seconds"] = 901
        rebind(cpu_expanded)
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(cpu_expanded)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_service_structural_imports_do_not_become_required_authority(self) -> None:
        task = validate_task_schema(service_task_fixture())
        self.assertIn(
            "vibapp:experimental-v0/scheduler@0.0.1",
            task["target"]["required_imports"],
        )
        self.assertIn(
            "vibapp:experimental-v0/system-metrics@0.0.1",
            task["target"]["required_imports"],
        )
        self.assertNotIn(
            "vibapp:experimental-v0/scheduler@0.0.1",
            task["target"]["required_capabilities"],
        )
        self.assertNotIn(
            "vibapp:experimental-v0/system-metrics@0.0.1",
            task["target"]["required_capabilities"],
        )
        # The base NeedSpec explicitly forbids HTTP.  That is compatible with
        # the service world's structural HTTP import because it is not an app
        # requirement and will be linked as a denied typed stub.
        validate_authorization(task, FIXED_CLOCK)

    def test_required_capabilities_must_be_structural_import_subset(self) -> None:
        task = task_fixture()
        task["target"]["required_capabilities"].append(
            "vibapp:experimental-v0/http@0.0.1"
        )
        rebind(task)
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(task)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_multiline_need_uses_existing_text_contract_without_abi_change(self) -> None:
        task = task_fixture()
        task["need_spec"]["goal"] = "Create a local multiline notes editor."
        task["need_spec"]["requirements"] = [
            {
                "requirement_id": "req-multiline",
                "text": "Edit arbitrary multi-line text in one field and preserve every newline.",
                "priority": "must-have",
                "acceptance_examples": ["Paste first line\\nsecond line and save both line breaks."],
            }
        ]
        rebind(task)
        validated = validate_task_schema(task)
        self.assertEqual(validated["need_spec"]["goal"], task["need_spec"]["goal"])

    def test_action_need_requires_both_a_rendered_button_and_action_handler(self) -> None:
        task = validate_task_schema(task_fixture())
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(
                task,
                {"src/lib.rs": "match event { LauncherEvent::Action(event) => calculate(event) }"},
            )
        self.assertEqual(caught.exception.code, "semantic-ui-action-unreachable")
        self.assertIn("ui.button-node", str(caught.exception))

        validate_generated_semantics(
            task,
            {
                "src/lib.rs": """
                    match event { LauncherEvent::Action(event) => calculate(event) }
                    let node = NodeKind::Button(ButtonNode { disabled: false });
                """,
            },
        )

    def test_foreground_resume_is_not_misclassified_as_a_user_action(self) -> None:
        task = task_fixture()
        task["need_spec"]["goal"] = "Create an LED wall clock that updates automatically."
        task["need_spec"]["requirements"] = [
            {
                "requirement_id": "req-clock",
                "text": "显示当前时间；切走再恢复前台后仍自动更新。",
                "priority": "must-have",
                "acceptance_examples": ["Return to the foreground and see the current time."],
            }
        ]
        task = validate_task_schema(rebind(task))

        # A clock that updates from lifecycle/time events needs no fake action
        # button merely because its prose uses the Chinese word for "resume".
        validate_generated_semantics(
            task,
            {"src/lib.rs": "let node = NodeKind::Heading(HeadingNode { level: 1 });"},
        )

    def test_comments_and_strings_cannot_forge_action_semantic_evidence(self) -> None:
        projected = rust_code_projection(
            '/* NodeKind::Button(fake) */ let note = "LauncherEvent::Action(fake)";'
        )
        self.assertNotIn("NodeKind", projected)
        self.assertNotIn("LauncherEvent", projected)
        task = validate_task_schema(task_fixture())
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(
                task,
                {
                    "src/lib.rs": """
                        // NodeKind::Button(ButtonNode {})
                        const CLAIM: &str = "LauncherEvent::Action(event)";
                    """,
                },
            )
        self.assertEqual(caught.exception.code, "semantic-ui-action-unreachable")

    def test_service_entrypoint_requires_explicit_service_event_handling(self) -> None:
        task = validate_task_schema(service_task_fixture())
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(
                task,
                {"src/lib.rs": "match event { AppEvent::Launcher(_) => render() }"},
            )
        self.assertEqual(caught.exception.code, "semantic-service-unimplemented")
        self.assertIn("guest.app-event=service", str(caught.exception))

        validate_generated_semantics(
            task,
            {
                "src/lib.rs": """
                    match event {
                        AppEvent::Service(service) => match service {
                            ServiceEvent::Start(event) => record_started(event),
                            ServiceEvent::Trigger(event) => probe(event),
                            ServiceEvent::Stop(event) => record_stopped(event),
                        }
                    }
                """,
            },
        )

    def test_service_trigger_contract_is_explicit_canonical_and_semantically_bound(self) -> None:
        task = service_task_fixture()
        task["package_intent"]["entrypoints"][0]["triggers"] = [
            "on-enable",
            "scheduler",
            "manual",
        ]
        task = validate_task_schema(rebind(task))
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(
                task,
                {
                    "src/lib.rs": """
                        match event {
                            AppEvent::Service(service) => match service {
                                ServiceEvent::Start(event) => record_started(event),
                                _ => stop(),
                            }
                        }
                    """,
                },
            )
        self.assertEqual(caught.exception.code, "semantic-service-unimplemented")
        self.assertIn("guest.service-event=trigger", str(caught.exception))

        for triggers in (["manual", "on-enable"], ["on-enable", "unknown"]):
            with self.subTest(triggers=triggers):
                invalid = service_task_fixture()
                invalid["package_intent"]["entrypoints"][0]["triggers"] = triggers
                with self.assertRaises(WorkerError) as invalid_error:
                    validate_task_schema(invalid)
                self.assertEqual(invalid_error.exception.code, "schema-invalid")

    def test_reminder_requires_alarm_fire_once_and_requested_controls(self) -> None:
        task = validate_task_schema(reminder_task_fixture())
        wrong_policy = """
            let request = scheduler::upsert(ScheduleRequest {
                purpose: SchedulePurpose::Alarm,
                missed: MissedPolicy::Skip,
            });
        """
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(task, {"src/lib.rs": wrong_policy})
        self.assertEqual(caught.exception.code, "semantic-reminder-policy-invalid")
        self.assertIn("missed-policy=fire-once", str(caught.exception))

        policy_without_controls = """
            let request = scheduler::upsert(ScheduleRequest {
                purpose: SchedulePurpose::Alarm,
                missed: MissedPolicy::FireOnce,
            });
        """
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(task, {"src/lib.rs": policy_without_controls})
        self.assertEqual(caught.exception.code, "semantic-reminder-controls-missing")
        self.assertIn("configurable-time", str(caught.exception))

        validate_generated_semantics(
            task,
            {
                "src/lib.rs": """
                    let request = scheduler::upsert(ScheduleRequest {
                        purpose: SchedulePurpose::Alarm,
                        missed: MissedPolicy::FireOnce,
                    });
                    let field = FieldKind::Time;
                    let pause = scheduler::disable(id, key);
                    let resume = scheduler::enable(id, key);
                    let node = NodeKind::Button(ButtonNode { disabled: false });
                    match event {
                        LauncherEvent::Action(event) => update(event),
                        AppEvent::Service(service) => match service {
                            ServiceEvent::Start(event) => start(event),
                            ServiceEvent::Trigger(event) => deliver(event),
                            _ => stop(),
                        },
                    }
                """,
            },
        )

    def test_legacy_task_without_model_binding_fails_closed(self) -> None:
        task = task_fixture()
        del task["model"]
        del task["consent"]["model"]
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(task)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_null_blank_and_whitespace_models_fail_schema_validation(self) -> None:
        for value in (None, "", "   ", " gpt-5.6-sol", "gpt-5.6-sol "):
            for location in ("task", "consent"):
                with self.subTest(value=value, location=location):
                    task = task_fixture()
                    if location == "task":
                        task["model"] = value
                    else:
                        task["consent"]["model"] = value
                    with self.assertRaises(WorkerError) as caught:
                        validate_task_schema(task)
                    self.assertEqual(caught.exception.code, "schema-invalid")

    def test_dry_run_creates_source_only_builder_handoff_with_truthful_labels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-test-") as temporary:
            output = Path(temporary) / "output"
            handoff_path = CloudAgentWorker(clock=FIXED_CLOCK).execute(VALID, output, dry_run=True)
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            execution = handoff["provider_execution"]
            self.assertEqual(handoff["schema_version"], SOURCE_HANDOFF_VERSION)
            self.assertEqual(handoff["status"], "untrusted-source-awaiting-builder")
            self.assertEqual(handoff["authority"]["compile"], "separate-builder")
            self.assertEqual(handoff["authority"]["verify"], "separate-verifier")
            self.assertEqual(handoff["authority"]["install"], "none")
            self.assertEqual(handoff["authority"]["publish"], "none")
            self.assertFalse(execution["external_request_attempted"])
            self.assertFalse(execution["external_request_observed"])
            self.assertIsNone(execution["gateway_request_id"])
            self.assertEqual(
                set(execution),
                {
                    "mode", "adapter", "provider_id", "external_request_attempted", "external_request_observed",
                    "gateway_request_id", "executable_sha256", "isolation_policy_version",
                    "runner_id", "runner_identity_sha256", "isolation_policy_sha256",
                    "receipt_digest_sha256",
                },
            )
            self.assertEqual(execution["provider_id"], "static-fixture")
            source = handoff_path.parent / handoff["source_directory"]
            self.assertTrue((source / "Cargo.toml").is_file())
            self.assertEqual((source / "Cargo.toml").read_bytes(), canonical_cargo_manifest(task_fixture()))
            self.assertEqual((source / "Cargo.toml").stat().st_mode & 0o777, 0o444)
            cargo_record = next(record for record in handoff["files"] if record["path"] == "Cargo.toml")
            self.assertEqual(cargo_record["sha256"], sha256_bytes(canonical_cargo_manifest(task_fixture())))
            self.assertTrue((source / "src/lib.rs").is_file())
            self.assertTrue((source / "wit/contract.wit").is_file())
            self.assertEqual(list((output / "workspaces").iterdir()), [])
            self.assertFalse((output / "consents").exists())

    def test_live_adapter_requires_redundant_cli_opt_in(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-test-") as temporary:
            with self.assertRaises(WorkerError) as caught:
                CloudAgentWorker(clock=FIXED_CLOCK).execute(VALID, Path(temporary) / "output", dry_run=False)
            self.assertEqual(caught.exception.code, "external-opt-in-required")

    def test_live_without_external_isolation_fails_closed_before_consent_claim(self) -> None:
        task = task_fixture()
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-test-") as temporary:
            root = Path(temporary)
            output = root / "output"
            executable, pin = synthetic_executable_pin(root)
            with self.assertRaises(WorkerError) as caught:
                CloudAgentWorker(codex_bin=executable, executable_pin=pin, clock=FIXED_CLOCK).execute(
                    VALID, output, dry_run=False,
                    confirm_job=task["job_id"], confirm_consent=task["consent"]["consent_id"],
                    acknowledge_external_cost=True,
                )
            self.assertEqual(caught.exception.code, "external-isolation-unavailable")
            self.assertFalse(caught.exception.external_request_attempted)
            self.assertFalse(caught.exception.external_request_observed)
            self.assertFalse((output / "consents").exists())
            self.assertFalse((output / "workspaces").exists())

    def test_unaccepted_custom_runner_fails_before_claim_workspace_or_execute(self) -> None:
        task = task_fixture()
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-test-") as temporary:
            root = Path(temporary)
            output = root / "output"
            executable, pin = synthetic_executable_pin(root)
            worker = CloudAgentWorker(
                codex_bin=executable,
                provider_runner=NeverExecutedRunner(),
                executable_pin=pin,
                clock=FIXED_CLOCK,
            )
            with self.assertRaises(WorkerError) as caught:
                worker.execute(
                    VALID, output, dry_run=False,
                    confirm_job=task["job_id"], confirm_consent=task["consent"]["consent_id"],
                    acknowledge_external_cost=True,
                )
            self.assertEqual(caught.exception.code, "runner-attestation-unavailable")
            self.assertFalse(caught.exception.external_request_attempted)
            self.assertFalse(caught.exception.external_request_observed)
            self.assertIsNone(caught.exception.gateway_request_id)
            self.assertFalse((output / "consents").exists())
            self.assertFalse((output / "workspaces").exists())

    def test_placeholder_runner_profile_is_rejected_before_runner_preflight(self) -> None:
        profiles = (
            replace(ACCEPTED_RUNNER, runner_id="UN-PROVISIONED"),
            replace(ACCEPTED_RUNNER, attestation_key_id="PLACE_HOLDER"),
            replace(ACCEPTED_RUNNER, runner_identity_sha256="0" * 64),
            replace(ACCEPTED_RUNNER, runner_id="0"),
        )
        task = task_fixture()
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-profile-") as temporary:
            root = Path(temporary)
            executable = root / "0.149.0" / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"reviewed inert executable")
            executable.chmod(0o700)
            pin = {
                "configured_path": str(executable),
                "resolved_path": str(executable),
                "version": "codex-cli 0.149.0",
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            }
            for index, profile in enumerate(profiles):
                with self.subTest(profile=profile):
                    runner = PreflightSentinelRunner()
                    output = root / f"output-{index}"
                    worker = CloudAgentWorker(
                        codex_bin=executable,
                        provider_runner=runner,
                        executable_pin=pin,
                        clock=FIXED_CLOCK,
                        accepted_runner=profile,
                        signature_verifier=AcceptFixtureSignature(),
                    )
                    with self.assertRaises(WorkerError) as caught:
                        worker.execute(
                            VALID, output, dry_run=False,
                            confirm_job=task["job_id"], confirm_consent=task["consent"]["consent_id"],
                            acknowledge_external_cost=True,
                        )
                    self.assertEqual(caught.exception.code, "runner-attestation-invalid")
                    self.assertEqual(runner.preflight_calls, 0)
                    self.assertFalse((output / "consents").exists())
                    self.assertFalse((output / "workspaces").exists())

    def test_consent_claim_is_atomic_single_use_and_binds_full_digest(self) -> None:
        task = validate_task_schema(task_fixture())
        validate_authorization(task, FIXED_CLOCK)
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-test-") as temporary:
            output = Path(temporary) / "output"
            output.mkdir(mode=0o700)
            claim = claim_consent(output, task)
            claimed = json.loads(claim.read_text(encoding="utf-8"))
            self.assertEqual(claimed["schema_version"], CONSENT_CLAIM_VERSION)
            self.assertEqual(claimed["immutable_task_digest_sha256"], task["immutable_task_digest_sha256"])
            self.assertEqual(claimed["policy_version"], POLICY_VERSION)
            self.assertEqual(claimed["instructions_digest_sha256"], provider_instructions_digest(task["provider"], task))
            self.assertEqual(claimed["model"], task["model"])
            with self.assertRaises(WorkerError) as caught:
                claim_consent(output, task)
            self.assertEqual(caught.exception.code, "consent-replayed")

    def test_consent_digest_rejects_mutated_package_target_and_limits(self) -> None:
        mutations = (
            lambda task: task["package_intent"].__setitem__("description", "mutated provider-visible intent"),
            lambda task: task["target"].__setitem__("app_kind", "service"),
            lambda task: task["limits"].__setitem__("source_bytes", 4 * 1024 * 1024),
        )
        for mutate in mutations:
            task = task_fixture()
            mutate(task)
            if task["target"]["app_kind"] == "service":
                task["target"]["app_kind"] = "ui"
                task["target"]["profiles"] = ["desktop", "web-preview"]
                task["need_spec"]["profiles"] = ["desktop", "web-preview"]
                task["need_spec"]["platforms"].append({"os": "browser", "arch": "wasm32", "profile": "web-preview"})
                task["need_spec_digest_sha256"] = sha256_bytes(canonical_json(task["need_spec"]))
            parsed = validate_task_schema(task)
            with self.assertRaises(WorkerError) as caught:
                validate_authorization(parsed, FIXED_CLOCK)
            self.assertEqual(caught.exception.code, "consent-required")

    def test_model_is_bound_to_task_digest_and_consent(self) -> None:
        original = task_fixture()
        mutated = task_fixture()
        mutated["model"] = "gpt-different-authorized"
        self.assertNotEqual(immutable_task_digest(original), immutable_task_digest(mutated))
        parsed = validate_task_schema(mutated)
        with self.assertRaises(WorkerError) as caught:
            validate_authorization(parsed, FIXED_CLOCK)
        self.assertEqual(caught.exception.code, "consent-required")

        rebound = rebind(mutated)
        rebound["consent"]["model"] = "gpt-different-authorized"
        validate_authorization(validate_task_schema(rebound), FIXED_CLOCK)

    def test_provider_execution_identity_integrity_covers_endpoint_runtime_and_config(self) -> None:
        base = task_fixture()
        mutations = (
            lambda task: task["provider_execution_identity"]["runtime"].__setitem__(
                "adapter_version", "experimental-v2"
            ),
            lambda task: task["provider_execution_identity"].__setitem__(
                "non_secret_config_sha256", "d" * 64
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                task = copy.deepcopy(base)
                mutate(task)
                with self.assertRaises(WorkerError) as caught:
                    validate_task_schema(task)
                self.assertEqual(caught.exception.code, "integrity-failure")

        endpoint_task = task_fixture()
        endpoint_task["provider"] = "opencode"
        endpoint_task["consent"]["provider"] = "opencode"
        endpoint_task["consent"]["instructions_digest_sha256"] = provider_instructions_digest(
            "opencode"
        )
        endpoint_task["provider_execution_identity"] = synthetic_provider_identity("opencode")
        rebind(endpoint_task)
        endpoint_task["provider_execution_identity"]["endpoint"][
            "canonical_endpoint"
        ] = "https://gateway.example.com/v1"
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(endpoint_task)
        self.assertEqual(caught.exception.code, "integrity-failure")

    def test_fixed_lan_endpoint_is_exact_and_opencode_only(self) -> None:
        task = task_fixture()
        task["provider"] = task["consent"]["provider"] = "opencode"
        task["consent"]["instructions_digest_sha256"] = provider_instructions_digest("opencode")
        task["provider_execution_identity"] = synthetic_provider_identity("opencode")
        endpoint = task["provider_execution_identity"]["endpoint"]
        endpoint["kind"] = "fixed-lan-http"
        endpoint["canonical_endpoint"] = "http://192.168.199.170:8081/v1/chat/completions"
        rebind(rebind_provider_identity(task))
        validate_authorization(validate_task_schema(task), FIXED_CLOCK)
        for address in ("http://127.0.0.1:8081/v1/chat/completions",
                        "http://192.168.199.170:8081/v1/chat/completions/",
                        "http://192.168.199.170:8081/v1/chat/completions?key=x",
                        "http://192.168.199.170:8081/v1/models"):
            changed = copy.deepcopy(task)
            changed["provider_execution_identity"]["endpoint"]["canonical_endpoint"] = address
            rebind(rebind_provider_identity(changed))
            with self.subTest(endpoint=address), self.assertRaises(WorkerError):
                validate_task_schema(changed)
        changed = copy.deepcopy(task)
        changed["provider"] = changed["consent"]["provider"] = "openai-codex"
        changed["consent"]["instructions_digest_sha256"] = provider_instructions_digest("openai-codex")
        rebind(changed)
        with self.assertRaises(WorkerError):
            validate_task_schema(changed)

    def test_recomputed_provider_identity_requires_fresh_task_and_consent_binding(self) -> None:
        task = task_fixture()
        original_digest = task["immutable_task_digest_sha256"]
        original_identity_digest = task["provider_execution_identity"]["identity_sha256"]
        task["provider_execution_identity"]["runtime"]["adapter_version"] = "experimental-v2"
        rebind_provider_identity(task)
        self.assertNotEqual(
            task["provider_execution_identity"]["identity_sha256"],
            original_identity_digest,
        )

        parsed = validate_task_schema(task)
        with self.assertRaises(WorkerError) as caught:
            validate_authorization(parsed, FIXED_CLOCK)
        self.assertEqual(caught.exception.code, "consent-required")

        rebind(task)
        self.assertNotEqual(task["immutable_task_digest_sha256"], original_digest)
        validate_authorization(validate_task_schema(task), FIXED_CLOCK)

    def test_custom_endpoint_identity_requires_one_canonical_secret_free_https_value(self) -> None:
        base = task_fixture()
        base["provider"] = "opencode"
        base["consent"]["provider"] = "opencode"
        base["consent"]["instructions_digest_sha256"] = provider_instructions_digest("opencode")
        base["provider_execution_identity"] = synthetic_provider_identity("opencode")
        rebind(base)
        validate_authorization(validate_task_schema(base), FIXED_CLOCK)

        for endpoint in (
            "https://API.example.com/v1",
            "https://api.example.com:443/v1",
            "https://api.example.com:080/v1",
            "https://user@api.example.com/v1",
            "https://api.example.com/v1/",
            "https://api.example.com/v1/../v2",
            "https://api.example.com/a b",
            "https://api.example.com/v1?key=value",
        ):
            with self.subTest(endpoint=endpoint):
                task = copy.deepcopy(base)
                task["provider_execution_identity"]["endpoint"]["canonical_endpoint"] = endpoint
                rebind_provider_identity(task)
                rebind(task)
                with self.assertRaises(WorkerError) as caught:
                    validate_task_schema(task)
                self.assertEqual(caught.exception.code, "schema-invalid")

        provider_managed = copy.deepcopy(base)
        provider_managed["provider_execution_identity"]["endpoint"] = {
            "kind": "provider-managed",
            "canonical_endpoint": "provider-managed",
            "endpoint_sha256": "0" * 64,
        }
        rebind_provider_identity(provider_managed)
        rebind(provider_managed)
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(provider_managed)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_provider_identity_schema_forbids_secret_values(self) -> None:
        task = task_fixture()
        task["provider_execution_identity"]["api_key"] = "must-never-be-serialized"
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(task)
        self.assertEqual(caught.exception.code, "schema-invalid")

        missing_executable_identity = task_fixture()
        missing_executable_identity["provider_execution_identity"]["runtime"][
            "executable_sha256"
        ] = None
        rebind_provider_identity(missing_executable_identity)
        rebind(missing_executable_identity)
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(missing_executable_identity)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_retry_is_a_new_attempt_not_a_fake_need_revision(self) -> None:
        first = task_fixture()
        retry = copy.deepcopy(first)
        retry["execution_attempt"] = {
            "attempt_id": "attempt-0002-fedcba9876543210",
            "ordinal": 2,
        }
        retry["consent"]["consent_id"] = "consent-cloud-ui-001-retry-0002"
        rebind(retry)

        self.assertEqual(first["need_spec_current_revision"], retry["need_spec_current_revision"])
        self.assertEqual(first["need_spec"]["revision"], retry["need_spec"]["revision"])
        self.assertEqual(first["need_spec_digest_sha256"], retry["need_spec_digest_sha256"])
        self.assertEqual(
            first["immutable_task_digest_sha256"], retry["immutable_task_digest_sha256"]
        )
        validate_authorization(validate_task_schema(first), FIXED_CLOCK)
        validate_authorization(validate_task_schema(retry), FIXED_CLOCK)

        with tempfile.TemporaryDirectory(prefix="vibapp-attempt-claims-") as temporary:
            output = Path(temporary)
            claim_consent(output, first)
            claim_consent(output, retry)

        mismatched = copy.deepcopy(retry)
        mismatched["execution_attempt"]["ordinal"] = 3
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(mismatched)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_consent_metadata_ttl_revision_and_disclosure_are_exact(self) -> None:
        cases = []
        task = task_fixture(); task["consent"]["instructions_digest_sha256"] = "0" * 64; cases.append(task)
        task = task_fixture(); task["consent"]["contract_digest_sha256"] = "0" * 64; cases.append(task)
        task = task_fixture(); task["consent"]["issued_at_utc"] = "2026-08-23T19:00:00Z"; task["consent"]["expires_at_utc"] = "2026-08-23T21:00:01Z"; cases.append(task)
        task = task_fixture(); task["need_spec_current_revision"] = 2; cases.append(task)
        for task in cases:
            parsed = validate_task_schema(task)
            with self.assertRaises(WorkerError) as caught:
                validate_authorization(parsed, FIXED_CLOCK)
            self.assertIn(caught.exception.code, {"consent-required", "need-incomplete"})

        disclosure_missing = task_fixture()
        disclosure_missing["consent"]["uploaded_data_classes"] = disclosure_missing["consent"][
            "uploaded_data_classes"
        ][:-1]
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(disclosure_missing)
        self.assertEqual(caught.exception.code, "schema-invalid")

    def test_profile_coverage_and_negative_constraints_are_reconciled(self) -> None:
        task = task_fixture()
        task["need_spec"]["profiles"].append("web-preview")
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(task)
        self.assertEqual(caught.exception.code, "need-incomplete")
        task = task_fixture()
        task["need_spec"]["negative_constraints"][0]["value"] = task["target"]["required_capabilities"][0]
        rebind(task)
        parsed = validate_task_schema(task)
        with self.assertRaises(WorkerError) as caught:
            validate_authorization(parsed, FIXED_CLOCK)
        self.assertEqual(caught.exception.code, "need-incomplete")

    def test_prompt_contains_only_consent_bound_request_not_claim_state(self) -> None:
        task = validate_task_schema(task_fixture())
        prompt = create_prompt(task, CONTRACT_DIGEST_PIN).decode("utf-8")
        self.assertIn(task["job_id"], prompt)
        self.assertIn(INSTRUCTIONS_DIGEST, prompt)
        self.assertNotIn(task["consent"]["consent_id"], prompt)
        self.assertNotIn("issued_at_utc", prompt)
        self.assertIn("pending Builder/Verifier work as unresolved", prompt)
        self.assertIn("source/Cargo.toml is a read-only deterministic policy scaffold", prompt)
        self.assertIn("physically create source/src/lib.rs", prompt)
        self.assertIn("read-only inspection inside the current workspace", prompt)
        request = provider_request(task, CONTRACT_DIGEST_PIN)
        self.assertEqual(task["schema_version"], TASK_SCHEMA_VERSION)
        self.assertEqual(request["schema_version"], PROVIDER_REQUEST_VERSION)
        self.assertEqual(request["policy_version"], POLICY_VERSION)
        self.assertEqual(request["cargo_scaffold"]["package_name"], "ai_vibapp_meeting-prep")
        self.assertEqual(request["cargo_scaffold"]["package_version"], "0.1.0")
        self.assertEqual(
            request["cargo_scaffold"]["sha256"],
            sha256_bytes(canonical_cargo_manifest(task)),
        )

    def test_codex_command_is_ephemeral_and_schema_constrained_but_not_isolation(self) -> None:
        command = codex_command(
            Path("/opt/homebrew/bin/codex"),
            Path("/private/tmp/job"),
            model="gpt-explicit-authorized",
        )
        self.assertEqual(command[:2], ["/opt/homebrew/bin/codex", "exec"])
        self.assertIn(
            ["--model", "gpt-explicit-authorized"],
            [command[index:index + 2] for index in range(len(command) - 1)],
        )
        self.assertIn("--ephemeral", command)
        self.assertIn("workspace-write", command)
        self.assertIn("--output-schema", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--add-dir", command)
        selected = codex_command(
            Path("/opt/homebrew/bin/codex"),
            Path("/private/tmp/job"),
            model="gpt-5.6-sol",
        )
        self.assertIn(
            ["--model", "gpt-5.6-sol"],
            [selected[index:index + 2] for index in range(len(selected) - 1)],
        )
        for invalid in (None, "", "   "):
            with self.subTest(invalid=invalid), self.assertRaises(WorkerError) as caught:
                codex_command(Path("/opt/homebrew/bin/codex"), Path("/private/tmp/job"), invalid)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "model-invalid")

    def test_external_runner_policy_requires_all_host_boundaries_and_resource_ceilings(self) -> None:
        task = validate_task_schema(task_fixture())
        policy = required_isolation_policy(task)
        self.assertEqual(policy["environment_inheritance"], "none")
        self.assertEqual(policy["credential_delivery"], "broker-only")
        self.assertEqual(policy["network_policy"], "provider-gateway-only")
        self.assertTrue(policy["one_job_per_isolate"])
        self.assertTrue(policy["whole_job_kill"])
        self.assertFalse(policy["host_home_mounted"])
        self.assertFalse(policy["host_repository_mounted"])
        self.assertFalse(policy["host_ssh_mounted"])
        self.assertFalse(policy["host_cloud_credentials_mounted"])
        self.assertFalse(policy["host_container_socket_mounted"])
        self.assertEqual(policy["cpu_seconds"], task["limits"]["cpu_seconds"])
        self.assertEqual(policy["rss_bytes"], task["limits"]["memory_bytes"])
        self.assertEqual(policy["pids"], task["limits"]["pids"])
        self.assertEqual(policy["disk_bytes"], task["limits"]["workspace_bytes"])

    def test_runner_request_command_cannot_omit_or_substitute_authorized_model(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-runner-model-") as temporary:
            request = receipt_request(Path(temporary))
            validate_runner_request_model(request)
            with self.assertRaises(WorkerError) as caught:
                validate_runner_request_model(replace(request, schema_version="legacy-request"))
            self.assertEqual(caught.exception.code, "runner-request-invalid")
            for command in (
                ["/reviewed/codex", "exec"],
                ["/reviewed/codex", "exec", "--model", "gpt-settings-drift"],
                ["/reviewed/codex", "exec", "--model", request.model, "--model", request.model],
            ):
                with self.subTest(command=command), self.assertRaises(WorkerError) as caught:
                    validate_runner_request_model(replace(request, command=command))
                self.assertEqual(caught.exception.code, "model-mismatch")

            with self.assertRaises(WorkerError) as caught:
                validate_runner_request_model(
                    replace(
                        request,
                        execution_attempt={
                            "attempt_id": "attempt-0001-0123456789abcdef",
                            "ordinal": 2,
                        },
                    )
                )
            self.assertEqual(caught.exception.code, "schema-invalid")

            changed_identity = synthetic_provider_identity("openai-codex", config_digest="d" * 64)
            with self.assertRaises(WorkerError) as caught:
                validate_runner_request_model(
                    replace(request, provider_execution_identity=changed_identity)
                )
            self.assertEqual(caught.exception.code, "provider-identity-mismatch")

    def test_resolved_cli_bytes_and_version_must_match_consent_bound_runtime(self) -> None:
        task = validate_task_schema(task_fixture())
        runtime = task["provider_execution_identity"]["runtime"]
        validate_resolved_provider_identity(
            task,
            {"sha256": runtime["executable_sha256"], "version": runtime["package_version"]},
        )
        for observed in (
            {"sha256": "0" * 64, "version": runtime["package_version"]},
            {"sha256": runtime["executable_sha256"], "version": "different-version"},
        ):
            with self.subTest(observed=observed), self.assertRaises(WorkerError) as caught:
                validate_resolved_provider_identity(task, observed)
            self.assertEqual(caught.exception.code, "provider-identity-mismatch")

    def test_receipt_is_fully_bound_and_requires_independent_signature(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-receipt-") as temporary:
            request = receipt_request(Path(temporary))
            receipt = signed_receipt(request)
            self.assertIs(
                validate_provider_receipt(receipt, request, ACCEPTED_RUNNER, AcceptFixtureSignature()),
                receipt,
            )
            with self.assertRaises(WorkerError) as caught:
                validate_provider_receipt(receipt, request, ACCEPTED_RUNNER, RejectFixtureSignature())
            self.assertEqual(caught.exception.code, "runner-attestation-invalid")
            self.assertFalse(caught.exception.external_request_attempted)
            self.assertFalse(caught.exception.external_request_observed)

            stale_digest_mutations = {
                "job": {"job_id": "another-job"},
                "attempt": {
                    "execution_attempt": {
                        "attempt_id": "attempt-0002-fedcba9876543210",
                        "ordinal": 2,
                    }
                },
                "task": {"immutable_task_digest_sha256": "5" * 64},
                "provider-identity": {"provider_execution_identity_sha256": "a" * 64},
                "input": {"input_bundle_sha256": "6" * 64},
                "output": {"output_bundle_sha256": "7" * 64},
                "policy": {"isolation_policy_sha256": "8" * 64},
                "runner": {"runner_identity_sha256": "9" * 64},
            }
            for name, mutation in stale_digest_mutations.items():
                with self.subTest(name=name), self.assertRaises(WorkerError):
                    validate_provider_receipt(replace(receipt, **mutation), request, ACCEPTED_RUNNER, AcceptFixtureSignature())

    def test_receipt_rejects_untrusted_truth_oversized_gateway_and_nonquiescence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-receipt-") as temporary:
            request = receipt_request(Path(temporary))
            overlong = signed_receipt(request, gateway_request_id="g" * 129)
            with self.assertRaises(WorkerError):
                validate_provider_receipt(overlong, request, ACCEPTED_RUNNER, AcceptFixtureSignature())

            nonquiescent = signed_receipt(request, whole_job_quiescent=False)
            with self.assertRaises(WorkerError) as caught:
                validate_provider_receipt(nonquiescent, request, ACCEPTED_RUNNER, AcceptFixtureSignature())
            self.assertEqual(caught.exception.code, "process-tree-not-quiescent")
            self.assertTrue(caught.exception.external_request_attempted)
            self.assertTrue(caught.exception.external_request_observed)

            unsigned_observed = signed_receipt(request)
            with self.assertRaises(WorkerError) as caught:
                validate_provider_receipt(unsigned_observed, request, ACCEPTED_RUNNER, RejectFixtureSignature())
            self.assertEqual(caught.exception.code, "runner-attestation-invalid")
            self.assertFalse(caught.exception.external_request_attempted)
            self.assertFalse(caught.exception.external_request_observed)

            for mutation in (
                {"attestation_type": "self-reported"},
                {"attestation_signature": "!" * 43},
            ):
                malformed = signed_receipt(request, **mutation)
                with self.assertRaises(WorkerError) as caught:
                    validate_provider_receipt(malformed, request, ACCEPTED_RUNNER, AcceptFixtureSignature())
                self.assertEqual(caught.exception.code, "runner-receipt-invalid")

            with self.assertRaises(WorkerError) as caught:
                validate_provider_receipt(unsigned_observed.__dict__, request, ACCEPTED_RUNNER, AcceptFixtureSignature())
            self.assertEqual(caught.exception.code, "runner-receipt-invalid")

    def test_resolved_executable_identity_accepts_reviewed_symlink_and_rejects_retarget(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-identity-") as temporary:
            root = Path(temporary)
            executable = root / "0.149.0/bin/codex"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"reviewed executable bytes")
            executable.chmod(0o700)
            configured = root / "codex"
            configured.symlink_to(executable)
            pin = {
                "configured_path": str(configured), "resolved_path": str(executable),
                "version": "codex-cli 0.149.0",
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            }
            identity = resolve_executable_identity(configured, pin)
            self.assertEqual(identity["resolved_path"], str(executable.resolve()))
            executable.write_bytes(b"mutated bytes")
            with self.assertRaises(WorkerError) as caught:
                resolve_executable_identity(configured, pin)
            self.assertEqual(caught.exception.code, "executable-identity-invalid")
            replacement = root / "0.149.0/bin/codex-replacement"
            replacement.write_bytes(b"reviewed executable bytes")
            replacement.chmod(0o700)
            configured.unlink(); configured.symlink_to(replacement)
            with self.assertRaises(WorkerError) as caught:
                resolve_executable_identity(configured, pin)
            self.assertEqual(caught.exception.code, "executable-identity-invalid")

    def test_provider_stdout_bomb_kills_process_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-test-") as temporary:
            root = Path(temporary)
            fake = root / "output-bomb"
            fake.write_text(
                "#!/usr/bin/python3\nimport sys,time\nsys.stdin.buffer.read()\n"
                "sys.stdout.buffer.write(b'x'*4096)\nsys.stdout.buffer.flush()\ntime.sleep(10)\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            with self.assertRaises(WorkerError) as caught:
                run_bounded_provider([str(fake)], b"", synthetic_limits(stdout_bytes=1024), root)
            self.assertEqual(caught.exception.code, "provider-output-limit")

    def test_registered_child_instant_setsid_devnull_is_quiescent_before_return(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-descendant-") as temporary:
            root = Path(temporary)
            fake = root / "detached-descendant"
            fake.write_text(
                "#!/usr/bin/python3\nimport os,pathlib,subprocess,time\n"
                "fd=int(os.environ['VIBAPP_SYNTHETIC_REGISTRY_FD']); nonce=os.environ['VIBAPP_SYNTHETIC_REGISTRY_NONCE']\n"
                "code=\"import os,pathlib,signal,time; fd=int(os.environ['VIBAPP_SYNTHETIC_REGISTRY_FD']); nonce=os.environ['VIBAPP_SYNTHETIC_REGISTRY_NONCE']; os.write(fd,('REGISTER '+nonce+' '+str(os.getpid())+'\\\\n').encode('ascii')); os.kill(os.getpid(),signal.SIGSTOP); os.setsid(); dev=os.open('/dev/null',os.O_RDWR); [os.dup2(dev,n) for n in (0,1,2)]; os.close(fd); pathlib.Path('descendant.pid').write_text(str(os.getpid())); time.sleep(20)\"\n"
                "child=subprocess.Popen(['/usr/bin/python3','-c',code],pass_fds=(fd,))\n"
                "deadline=time.monotonic()+2\n"
                "while not pathlib.Path('descendant.pid').exists() and time.monotonic()<deadline: time.sleep(0.01)\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            run_bounded_provider([str(fake)], b"", synthetic_limits(), root)
            self.assertFalse(active(int((root / "descendant.pid").read_text())))

    def test_source_handoff_paths_reject_noncanonical_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-path-") as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            normalized, resolved = normalized_source_path("src/lib.rs", root, "fixture")
            self.assertEqual(normalized, "src/lib.rs")
            self.assertEqual(resolved, (root / "src/lib.rs").resolve(strict=False))
            for attack in ("", "/src/lib.rs", "src//lib.rs", "src/./lib.rs", "src/../lib.rs", "src\\lib.rs", "src/"):
                with self.subTest(attack=attack), self.assertRaises(WorkerError):
                    normalized_source_path(attack, root, "fixture")
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(WorkerError):
                normalized_source_path("link/lib.rs", root, "fixture")

            task = validate_task_schema(task_fixture())
            result = json.loads((BASE / "fixtures/dry-run/provider-result.json").read_text(encoding="utf-8"))
            result["source_files"][0] = "src//lib.rs"
            with self.assertRaises(WorkerError) as caught:
                validate_provider_result(result, task, root)
            self.assertEqual(caught.exception.code, "provider-schema-invalid")

            result = json.loads((BASE / "fixtures/dry-run/provider-result.json").read_text(encoding="utf-8"))
            result["unresolved"] = ["A concrete source requirement is still missing."]
            with self.assertRaises(WorkerError) as caught:
                validate_provider_result(result, task, root)
            self.assertEqual(caught.exception.code, "source-incomplete")
            self.assertIn("concrete source requirement", str(caught.exception))

    def test_provider_capability_mismatch_is_bounded_metadata_not_task_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-capabilities-") as temporary:
            source_root = Path(temporary) / "source"
            source_root.mkdir()
            task = validate_task_schema(task_fixture())
            authoritative_target = copy.deepcopy(task["target"])
            result = json.loads(
                (BASE / "fixtures/dry-run/provider-result.json").read_text(encoding="utf-8")
            )
            result["declared_capabilities"][-1] = "vibapp:experimental-v0/http@0.0.1"

            validated = validate_provider_result(result, task, source_root)
            self.assertEqual(validated["declared_capabilities"], result["declared_capabilities"])
            self.assertNotEqual(
                validated["declared_capabilities"], task["target"]["required_imports"]
            )
            self.assertEqual(task["target"], authoritative_target)

            invalid_values = (
                ["one", "two", "three", "four"],
                [f"capability-{index}" for index in range(9)],
                ["x" * 201, "two", "three", "four", "five"],
                ["duplicate", "duplicate", "three", "four", "five"],
            )
            for capabilities in invalid_values:
                with self.subTest(capabilities=capabilities):
                    invalid = copy.deepcopy(result)
                    invalid["declared_capabilities"] = capabilities
                    with self.assertRaises(WorkerError) as caught:
                        validate_provider_result(invalid, task, source_root)
                    self.assertEqual(caught.exception.code, "provider-schema-invalid")

    def test_cargo_policy_rejects_build_path_target_dependencies_and_unknown_sections(self) -> None:
        valid = (BASE / "fixtures/dry-run/source/Cargo.toml").read_text(encoding="utf-8")
        attacks = {
            "package-build": valid.replace('edition = "2024"', 'edition = "2024"\nbuild = "build.rs"'),
            "out-of-tree-lib": valid.replace('[lib]\n', '[lib]\npath = "../../escape.rs"\n'),
            "target-dependency": valid + "\n[target.'cfg(unix)'.dependencies]\nserde = \"1\"\n",
            "workspace": valid + "\n[workspace]\nmembers = [\"../outside\"]\n",
            "features": valid + "\n[features]\ndefault = []\n",
            "ambient-std": valid.replace('"realloc"]', '"realloc", "std"]'),
        }
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-cargo-") as temporary:
            root = Path(temporary)
            good = root / "good.toml"; good.write_text(valid, encoding="utf-8")
            validate_cargo_manifest(good)
            for name, content in attacks.items():
                path = root / f"{name}.toml"; path.write_text(content, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(WorkerError) as caught:
                    validate_cargo_manifest(path)
                self.assertEqual(caught.exception.code, "source-invalid")

    def test_workspace_preseeds_exact_task_derived_cargo_before_provider(self) -> None:
        task = validate_task_schema(task_fixture())
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-scaffold-") as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            self.assertEqual(prepare_workspace(workspace, task), CONTRACT_DIGEST_PIN)
            cargo = workspace / "source/Cargo.toml"
            self.assertEqual(cargo.read_bytes(), canonical_cargo_manifest(task))
            self.assertEqual(cargo.stat().st_mode & 0o777, 0o444)
            validate_cargo_manifest(cargo, task)

            changed_name = cargo.read_text(encoding="utf-8").replace(
                'name = "ai_vibapp_meeting-prep"',
                'name = "provider-chosen-name"',
            )
            cargo.chmod(0o644)
            cargo.write_text(changed_name, encoding="utf-8")
            cargo.chmod(0o444)
            with self.assertRaises(WorkerError) as caught:
                validate_cargo_manifest(cargo, task)
            self.assertEqual(caught.exception.code, "source-invalid")

    def test_post_provider_audit_rejects_even_policy_valid_cargo_byte_rewrite(self) -> None:
        task = validate_task_schema(task_fixture())
        result = json.loads((BASE / "fixtures/dry-run/provider-result.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-scaffold-audit-") as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            prepare_workspace(workspace, task)
            (workspace / "source/src").mkdir()
            (workspace / "source/src/lib.rs").write_bytes(
                (BASE / "fixtures/dry-run/source/src/lib.rs").read_bytes()
            )
            cargo = workspace / "source/Cargo.toml"
            cargo.chmod(0o644)
            cargo.write_bytes(canonical_cargo_manifest(task) + b"# provider rewrite\n")
            cargo.chmod(0o444)
            with self.assertRaises(WorkerError) as caught:
                audit_workspace(workspace, task, result, CONTRACT_DIGEST_PIN)
            self.assertEqual(caught.exception.code, "source-invalid")

    def test_wit_bindgen_error_code_owner_smoke_accepts_common_and_rejects_guest(self) -> None:
        task = validate_task_schema(task_fixture())
        result = json.loads((BASE / "fixtures/dry-run/provider-result.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="vibapp-cloud-agent-abi-owner-") as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            prepare_workspace(workspace, task)
            library = workspace / "source/src/lib.rs"
            library.parent.mkdir()
            correct = (BASE / "fixtures/dry-run/source/src/lib.rs").read_text(encoding="utf-8") + """

pub fn __vibapp_error_code_owner_smoke(
    value: vibapp::experimental_v0::common::ErrorCode,
) -> vibapp::experimental_v0::common::ErrorCode {
    value
}
"""
            library.write_text(correct, encoding="utf-8")
            records = audit_workspace(workspace, task, result, CONTRACT_DIGEST_PIN)
            self.assertIn("src/lib.rs", [record["path"] for record in records])

            library.write_text(
                correct.replace(
                    "vibapp::experimental_v0::common::ErrorCode",
                    "exports::vibapp::experimental_v0::guest::ErrorCode",
                ),
                encoding="utf-8",
            )
            with self.assertRaises(WorkerError) as caught:
                audit_workspace(workspace, task, result, CONTRACT_DIGEST_PIN)
            self.assertEqual(caught.exception.code, "source-invalid")
            self.assertIn("wit-bindgen 0.60 owns ErrorCode", str(caught.exception))

    def test_need_and_task_digests_are_exactly_bound(self) -> None:
        task = task_fixture()
        self.assertEqual(task["need_spec_digest_sha256"], sha256_bytes(canonical_json(task["need_spec"])))
        self.assertEqual(task["immutable_task_digest_sha256"], immutable_task_digest(task))
        self.assertEqual(task["consent"]["payload_digest_sha256"], task["immutable_task_digest_sha256"])

    def test_authored_json_schemas_parse_strictly(self) -> None:
        for schema in sorted((BASE / "schemas").glob("*.json")):
            parsed = load_json(schema, 512 * 1024, schema.name)
            self.assertEqual(parsed["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
