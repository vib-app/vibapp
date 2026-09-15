from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


BASE = Path(__file__).resolve().parents[1]
ARTIFACTS = BASE.parent


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


delivery = load_module("vibapp_delivery_controller_test", BASE / "delivery_controller.py")


class FailingBuilderRunner:
    def preflight(self):
        return {
            "status": "ready",
            "network": "not-used",
            "source_executed": False,
        }

    def execute(self, source_root, workspace, limits):
        del source_root, workspace, limits
        raise delivery.PipelineError("builder-timeout", "synthetic transient Builder timeout")


class DeliveryControllerTests(unittest.TestCase):
    @staticmethod
    def synthetic_readiness(candidate, *, cancellation=None):
        # Explicit fixture boundary: these tests do not claim guest execution.
        del candidate, cancellation
        return {"status": "PASS", "synthetic": True, "cleanup_confirmed": True,
                "business_function_acceptance": False}

    def controller(self, *args, **kwargs):
        kwargs.setdefault("runtime_readiness_checker", self.synthetic_readiness)
        return delivery.DeliveryController(*args, **kwargs)

    def test_runtime_readiness_failure_never_reaches_appstore_ingest(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-readiness-delivery-") as directory:
            factory = mock.Mock()
            checker = mock.Mock(side_effect=delivery.RuntimeReadinessError(
                "launch: malformed-output: UI semantic tree has an invalid or duplicate action"))
            controller = self.controller(Path(directory), stage, delivery.SafeFixtureRunner(self.component.read_bytes()),
                                         wasm_tools=self.wasm_tools, appstore_factory=factory,
                                         runtime_readiness_checker=checker)
            task = self.fresh_task(stage)
            admitted = controller.submit(task, self.registry(task), explicit_user_submit=True)
            failed = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["code"], "runtime-readiness-failed")
            self.assertEqual(failed["error"]["stage"], "appstore")
            self.assertIn("duplicate action", failed["error"]["message"])
            checker.assert_called_once()
            factory.assert_not_called()
            self.assertFalse(failed["error"]["same_task_retry_allowed"])

    def test_readonly_status_history_revalidation_and_terminal_replay_never_execute_readiness(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-readiness-readonly-") as directory:
            controller, task, registry, admitted, complete = self.complete_delivery(Path(directory))
            checker = mock.Mock(side_effect=AssertionError("read-only path executed guest"))
            controller.runtime_readiness_checker = checker
            controller.status(admitted["task_id"])
            controller.history(admitted["task_id"])
            self.assertEqual(controller.revalidate_attempt(admitted["task_id"], admitted["attempt_id"]), complete)
            self.assertEqual(controller.run_attempt(admitted["task_id"], admitted["attempt_id"]), complete)
            checker.assert_not_called()

    def test_readiness_does_not_accept_not_applicable_or_unconfirmed_cleanup_for_ui(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-readiness-result-") as directory:
            root = Path(directory)
            candidate = root / "candidate.json"
            candidate.write_bytes(b"synthetic")
            expected = delivery.sha256_bytes(candidate.read_bytes())
            controller = self.controller(root / "controller", self.make_stage(), FailingBuilderRunner())
            for result in ({"status": "not-applicable", "business_function_acceptance": False},
                           {"status": "PASS", "cleanup_confirmed": False, "business_function_acceptance": False}):
                controller.runtime_readiness_checker = mock.Mock(return_value=result)
                with self.assertRaises(delivery.DeliveryError) as caught:
                    controller._check_runtime_readiness(candidate, {"app": {"kind": "ui"}}, expected)
                self.assertEqual(caught.exception.code, "runtime-readiness-failed")

    def test_production_constructor_defaults_to_real_readiness(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-readiness-default-") as directory:
            controller = delivery.DeliveryController(Path(directory), self.make_stage(), FailingBuilderRunner())
            self.assertIs(controller.runtime_readiness_checker, delivery.check_runtime_readiness)

    def test_host_pipeline_budget_spans_different_task_roots_and_releases(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-pipeline-budget-") as directory:
            root = Path(directory)
            controller = self.controller(
                root, stage, delivery.SafeFixtureRunner(self.component.read_bytes()), wasm_tools=self.wasm_tools
            )
            task = self.fresh_task(stage, 1)
            admitted = controller.submit(task, self.registry(task, "budget test"), explicit_user_submit=True)
            with stage.adapter.one_job_lease(root / "other-output", budget_kind="pipeline"):
                with self.assertRaises(delivery.DeliveryError) as caught:
                    controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(caught.exception.code, "local-capacity-busy")
            completed = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(completed["status"], "private-appstore-ready")

    def test_retry_policy_never_blindly_retries_configuration_or_unknown_failures(self):
        for code in ("local-live-containment-unavailable", "provider-unavailable", "provider-upstream-rejected", "provider-authentication-failed", "internal", "source-invalid", "identity-mismatch"):
            self.assertFalse(delivery.failure_retry_policy(code)[0], code)
        for code in ("provider-timeout", "provider-upstream-unavailable", "local-capacity-busy", "builder-timeout"):
            self.assertTrue(delivery.failure_retry_policy(code)[0], code)

    @classmethod
    def setUpClass(cls):
        cls.component = ARTIFACTS / "desktop/runtime-apps/hello/component.wasm"
        cls.expected_component_sha = "d3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa"
        cls.wasm_tools = delivery.DEFAULT_WASM_TOOLS

    def make_stage(
        self,
        provider_id="codex",
        task_provider="openai-codex",
        model="gpt-fixture-explicit",
    ):
        stage = delivery.CodeAgentStage(
            provider_id=provider_id,
            provider=object(),
            model=model,
            acknowledge_external_cost=True,
        )
        endpoint = {
            "kind": "https" if task_provider == "opencode" else "provider-managed",
            "canonical_endpoint": (
                "https://opencode.ai/api" if task_provider == "opencode" else "provider-managed"
            ),
        }
        endpoint["endpoint_sha256"] = stage.cloud.sha256_bytes(
            stage.cloud.canonical_json(endpoint)
        )
        runtime = {
            "adapter_id": "local-codeagent-adapter",
            "adapter_version": "fixture-v1",
            "adapter_sha256": "d" * 64,
            "package_id": f"{provider_id}-fixture-package",
            "package_version": "1.0.0",
            "executable_sha256": "a" * 64,
        }
        identity_body = {
            "schema_version": "vibapp.provider-execution-identity.experimental-v1",
            "endpoint": endpoint,
            "runtime": runtime,
            "non_secret_config_sha256": "c" * 64,
        }
        stage.execution_identity = {
            **identity_body,
            "identity_sha256": stage.cloud.sha256_bytes(
                stage.cloud.canonical_json(identity_body)
            ),
        }

        class Provider:
            pass

            provider_id = "fixture-placeholder"
            task_provider = "fixture-placeholder"
            model = "gpt-fixture-explicit"

            @staticmethod
            def preflight():
                runtime = stage.execution_identity["runtime"]
                return {
                    "available": True,
                    "execution_available": True,
                    "identity_observed": True,
                    "provider_execution_identity": stage.execution_identity,
                    "adapter_version": runtime["adapter_version"],
                    "adapter_sha256": runtime["adapter_sha256"],
                    "executable_path": f"/synthetic/reviewed/{provider_id}",
                    "executable_version": runtime["package_version"],
                    "executable_sha256": runtime["executable_sha256"],
                    "mode": "local-static-fixture",
                    "external": False,
                }

            @staticmethod
            def execute(*, workspace, prompt, limits, temporary_root, cancel_file):
                del limits, temporary_root, cancel_file
                provider_request = json.loads(prompt.decode("utf-8").splitlines()[-1])
                source = ARTIFACTS / "cloud-agent/fixtures/dry-run/source"
                cargo = workspace / "source/Cargo.toml"
                if cargo.read_bytes() != stage.cloud.canonical_cargo_manifest(provider_request):
                    raise AssertionError("CloudAgent must preseed the task-derived Cargo scaffold")
                if cargo.stat().st_mode & 0o777 != 0o444:
                    raise AssertionError("CloudAgent must make the Cargo scaffold provider-read-only")
                (workspace / "source/src").mkdir(mode=0o700)
                shutil.copy2(source / "src/lib.rs", workspace / "source/src/lib.rs")
                provider_result = json.loads(
                    (ARTIFACTS / "cloud-agent/fixtures/dry-run/provider-result.json").read_text(
                        encoding="utf-8"
                    )
                )
                provider_result["job_id"] = provider_request["job_id"]
                (workspace / "provider-last-message.json").write_text(
                    json.dumps(provider_result, sort_keys=True), encoding="utf-8"
                )
                return stage.adapter.ProviderExecution(
                    provider_id=provider_id,
                    provider_process_started=False,
                    external_request_attempted=True,
                    external_request_observed=False,
                    gateway_request_id=None,
                    executable_path=f"/synthetic/reviewed/{provider_id}",
                    executable_version=f"{provider_id}-fixture 1.0.0",
                    executable_sha256=stage.execution_identity["runtime"]["executable_sha256"],
                    stdout_bytes=0,
                    stderr_bytes=0,
                )

        stage.provider = Provider()
        stage.provider.provider_id = provider_id
        stage.provider.task_provider = task_provider
        stage.provider.model = model
        return stage

    @staticmethod
    def registry(task, message="No compatible candidate passed every hard filter."):
        need_spec = task["need_spec"]
        request_id = f"registry.{delivery.sha256_bytes(delivery.canonical_json(need_spec))[:24]}"
        return {
            "schema_version": "vibapp.registry-route.experimental.2026-08-24.1",
            "status": "experimental-product-hold",
            "request_id": request_id,
            "need_id": need_spec["need_id"],
            "route": "refinement",
            "recommendations": [],
            "refinement": {
                "reason_code": "no-hard-filter-match",
                "message": message,
            },
            "retrieval": {"mode": "hard-filter-then-keyword-plus-embedding"},
            "rejected": [],
            "codeagent_handoff": {
                "created": False,
                "permitted": False,
                "reason": "Registry only recommends or requests refinement.",
            },
        }

    @staticmethod
    def fresh_task(stage, revision=1, attempt_ordinal=1):
        task = json.loads(
            (ARTIFACTS / "cloud-agent/fixtures/valid-task.json").read_text(encoding="utf-8")
        )
        task["job_id"] = f"job-delivery-{revision}"
        task["need_spec_current_revision"] = revision
        task["need_spec"]["revision"] = revision
        task["need_spec"]["requirements"][0]["text"] += f" Revision {revision}."
        # The SafeFixtureRunner returns the immutable hello Component.  Keep the
        # task/manifest identity exactly equal to that Component descriptor; a
        # retry advances NeedSpec/job/consent, not the guest's claimed identity.
        task["package_intent"] = {
            "app_id": "ai.vibapp.hello",
            "version": "0.1.0",
            "display_name": "Hello VibApp",
            "description": "A safe exact-binding delivery integration fixture.",
            "entrypoints": [
                {
                    "id": "main",
                    "kind": "launcher-ui",
                    "label": "Hello VibApp",
                    "initial_route": "home",
                }
            ],
        }
        task["provider"] = stage.provider.task_provider
        task["model"] = stage.provider.model
        attempt_seed = stage.cloud.sha256_bytes(
            f"{revision}:{attempt_ordinal}:{stage.provider.task_provider}".encode()
        )[:16]
        attempt_id = f"attempt-{attempt_ordinal:04d}-{attempt_seed}"
        task["execution_attempt"] = {
            "attempt_id": attempt_id,
            "ordinal": attempt_ordinal,
        }
        task["provider_execution_identity"] = stage.execution_identity
        task["consent"]["consent_id"] = (
            f"consent-delivery-{revision}-{attempt_ordinal}"
        )
        task["consent"]["job_id"] = task["job_id"]
        task["consent"]["attempt_id"] = attempt_id
        task["consent"]["provider"] = stage.provider.task_provider
        task["consent"]["model"] = stage.provider.model
        task["consent"]["provider_execution_identity_sha256"] = (
            stage.execution_identity["identity_sha256"]
        )
        task["consent"]["instructions_digest_sha256"] = (
            stage.cloud.provider_instructions_digest(stage.provider.task_provider)
        )
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        task["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task["need_spec_digest_sha256"] = stage.cloud.sha256_bytes(
            stage.cloud.canonical_json(task["need_spec"])
        )
        digest = stage.cloud.immutable_task_digest(task, stage.cloud.CONTRACT_DIGEST_PIN)
        task["immutable_task_digest_sha256"] = digest
        task["consent"]["payload_digest_sha256"] = digest
        return task

    @staticmethod
    def retry_task(stage, previous, attempt_ordinal):
        task = json.loads(json.dumps(previous))
        attempt_seed = stage.cloud.sha256_bytes(
            f"retry:{task['job_id']}:{attempt_ordinal}".encode()
        )[:16]
        attempt_id = f"attempt-{attempt_ordinal:04d}-{attempt_seed}"
        task["execution_attempt"] = {
            "attempt_id": attempt_id,
            "ordinal": attempt_ordinal,
        }
        task["consent"]["consent_id"] = f"consent-retry-{attempt_ordinal}"
        task["consent"]["attempt_id"] = attempt_id
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        task["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return task

    def complete_delivery(self, root, stage=None):
        stage = stage or self.make_stage()
        controller = self.controller(
            root,
            stage,
            delivery.SafeFixtureRunner(self.component.read_bytes()),
            wasm_tools=self.wasm_tools,
        )
        task = self.fresh_task(stage)
        registry = self.registry(task)
        admitted = controller.submit(task, registry, explicit_user_submit=True)
        complete = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
        self.assertEqual(complete["status"], "private-appstore-ready")
        return controller, task, registry, admitted, complete

    @staticmethod
    def mac_runner_inputs(root: Path, *, accepted_by: str):
        input_root = root / "builder-inputs"
        tool_layer = input_root / "tool-layer"
        cargo_home = input_root / "cargo-home"
        acceptance = input_root / "acceptance.json"
        tool_layer.mkdir(parents=True)
        cargo_home.mkdir(parents=True)
        acceptance.write_text(
            json.dumps(
                {
                    "schema_version": "vibapp.offline-cache-acceptance.experimental-v1",
                    "state": "independently-accepted",
                    "cargo_home": str(cargo_home.resolve(strict=True)),
                    "network": "none",
                    "read_only": True,
                    "allowed_proc_macros": [
                        {
                            "name": "wit-bindgen-rust-macro",
                            "version": "0.60.0",
                            "checksum": "a" * 64,
                        }
                    ],
                    "accepted_by": accepted_by,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return (
            delivery.MacSandboxCargoRunner(
                tool_layer,
                cargo_home,
                acceptance,
                input_root,
            ),
            input_root,
        )

    def assert_preflight_stopped_provider(self, root: Path, admitted: dict, failed: dict):
        attempt_root = root / "tasks" / admitted["task_id"] / "attempts" / admitted["attempt_id"]
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["outputs"]["provider_process_started"])
        self.assertFalse(failed["outputs"]["external_request_attempted"])
        self.assertFalse(failed["outputs"]["external_request_observed"])
        self.assertFalse((attempt_root / "codeagent/status.json").exists())

    @staticmethod
    def rewrite_json(path: Path, value: dict) -> None:
        path.chmod(0o600)
        path.write_bytes(delivery.canonical_json(value) + b"\n")

    def test_current_builder_and_verifier_readiness_is_explicitly_non_executing(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-preflight-ready-") as directory:
            controller = self.controller(
                Path(directory),
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            readiness = controller.preflight()
            self.assertEqual(readiness["status"], "ready")
            self.assertFalse(readiness["provider_process_started"])
            self.assertFalse(readiness["external_request_attempted"])
            self.assertFalse(readiness["source_executed"])
            self.assertEqual(
                readiness["verifier"]["wasm_tools_sha256"], delivery.WASM_TOOLS_SHA256
            )

    def test_invalid_builder_acceptance_stops_before_codeagent(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-preflight-acceptance-") as directory:
            root = Path(directory)
            runner, _ = self.mac_runner_inputs(root, accepted_by="builder")
            controller = self.controller(root / "controller", stage, runner)
            task = self.fresh_task(stage)
            admitted = controller.submit(
                task, self.registry(task), explicit_user_submit=True
            )
            original_is_file = Path.is_file

            def sandbox_is_file(path: Path) -> bool:
                if path == Path("/usr/bin/sandbox-exec"):
                    return True
                return original_is_file(path)

            with mock.patch.object(Path, "is_file", sandbox_is_file):
                failed = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(failed["error"]["stage"], "builder")
            self.assertEqual(failed["error"]["code"], "builder-preflight-failed")
            self.assertIn("offline cache lacks independent acceptance", failed["error"]["message"])
            self.assert_preflight_stopped_provider(root / "controller", admitted, failed)

    def test_invalid_builder_root_stops_before_codeagent(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-preflight-root-") as directory:
            root = Path(directory)
            runner, input_root = self.mac_runner_inputs(
                root, accepted_by="independent-test-verifier"
            )
            input_root.rename(root / "builder-inputs-moved")
            controller = self.controller(root / "controller", stage, runner)
            task = self.fresh_task(stage)
            admitted = controller.submit(
                task, self.registry(task), explicit_user_submit=True
            )
            original_is_file = Path.is_file

            def sandbox_is_file(path: Path) -> bool:
                if path == Path("/usr/bin/sandbox-exec"):
                    return True
                return original_is_file(path)

            with mock.patch.object(Path, "is_file", sandbox_is_file):
                failed = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(failed["error"]["stage"], "builder")
            self.assertEqual(failed["error"]["code"], "builder-preflight-failed")
            self.assertIn("Builder input root is unavailable", failed["error"]["message"])
            self.assert_preflight_stopped_provider(root / "controller", admitted, failed)

    def test_invalid_wasm_tools_stops_before_codeagent(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-preflight-wasm-tools-") as directory:
            root = Path(directory)
            wasm_tools = root / "wrong-wasm-tools"
            wasm_tools.write_bytes(b"synthetic wrong verifier executable")
            wasm_tools.chmod(0o700)
            controller = self.controller(
                root / "controller",
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=wasm_tools,
            )
            task = self.fresh_task(stage)
            admitted = controller.submit(
                task, self.registry(task), explicit_user_submit=True
            )
            failed = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(failed["error"]["stage"], "verifier")
            self.assertEqual(failed["error"]["code"], "verifier-preflight-failed")
            self.assertIn("differs from the accepted local tool layer", failed["error"]["message"])
            self.assert_preflight_stopped_provider(root / "controller", admitted, failed)

    def test_admission_keeps_each_codeagent_provider_bound_to_its_consent(self):
        mappings = {
            "codex": "openai-codex",
            "claude-code": "anthropic-claude-code",
            "opencode": "opencode",
            "gemini-cli": "google-gemini-cli",
        }
        for provider_id, task_provider in mappings.items():
            with self.subTest(provider_id=provider_id):
                model = f"{provider_id}-authorized-model"
                stage = self.make_stage(provider_id, task_provider, model)
                task = self.fresh_task(stage)
                validated = stage.validate(task)
                self.assertEqual(validated["provider"], task_provider)
                self.assertEqual(validated["consent"]["provider"], task_provider)
                self.assertEqual(validated["model"], model)
                self.assertEqual(validated["consent"]["model"], model)

    def test_registry_match_and_forged_handoff_are_rejected(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-reject-") as directory:
            controller = self.controller(
                Path(directory),
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            task = self.fresh_task(stage)
            matching = self.registry(task)
            matching["route"] = "recommendation"
            matching["recommendations"] = [{"app": {"id": "existing"}}]
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.submit(task, matching, explicit_user_submit=True)
            self.assertEqual(caught.exception.code, "registry-no-match-required")

    def test_registry_no_match_requires_authoritative_exact_needspec_binding(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-registry-authority-") as directory:
            controller = self.controller(
                Path(directory),
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            task = self.fresh_task(stage)
            cases = []
            wrong_schema = self.registry(task)
            wrong_schema["schema_version"] = "vibapp.registry-route.desktop-fallback.v1"
            cases.append(wrong_schema)
            wrong_status = self.registry(task)
            wrong_status["status"] = "client-asserted"
            cases.append(wrong_status)
            wrong_need = self.registry(task)
            wrong_need["need_id"] = "need-deadbeef"
            cases.append(wrong_need)
            wrong_request = self.registry(task)
            wrong_request["request_id"] = "registry." + "0" * 24
            cases.append(wrong_request)
            fallback = self.registry(task)
            fallback["refinement"]["reason_code"] = "registry-unavailable"
            cases.append(fallback)
            unknown_field = self.registry(task)
            unknown_field["client_claimed_no_match"] = True
            cases.append(unknown_field)
            for registry in cases:
                with self.subTest(registry=registry):
                    with self.assertRaises(delivery.DeliveryError) as caught:
                        controller.submit(task, registry, explicit_user_submit=True)
                    self.assertEqual(caught.exception.code, "registry-no-match-required")

    def test_duplicate_task_cannot_substitute_registry_evidence(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-registry-binding-") as directory:
            controller = self.controller(
                Path(directory),
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            task = self.fresh_task(stage)
            controller.submit(task, self.registry(task, "original evidence"), explicit_user_submit=True)
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.submit(
                    task, self.registry(task, "substituted evidence"), explicit_user_submit=True
                )
            self.assertEqual(caught.exception.code, "admission-binding-conflict")
            forged = self.registry(task)
            forged["codeagent_handoff"]["created"] = True
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.submit(task, forged, explicit_user_submit=True)
            self.assertEqual(caught.exception.code, "registry-no-match-required")

    def test_exact_digest_vertical_delivery_is_restart_idempotent(self):
        self.assertEqual(delivery.sha256_bytes(self.component.read_bytes()), self.expected_component_sha)
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-success-") as directory:
            root = Path(directory)
            controller = self.controller(
                root,
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            task = self.fresh_task(stage)
            registry = self.registry(task)
            admitted = controller.submit(task, registry, explicit_user_submit=True)
            duplicate = controller.submit(task, registry, explicit_user_submit=True)
            self.assertEqual(duplicate, admitted)
            complete = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(complete["status"], "private-appstore-ready")
            self.assertTrue(complete["outputs"]["digest_equality_proven"])
            self.assertTrue(
                complete["outputs"]["task_handoff_manifest_binding_proven"]
            )
            self.assertFalse(complete["outputs"]["publication_performed"])
            self.assertEqual(complete["outputs"]["component_sha256"], self.expected_component_sha)
            self.assertRegex(complete["outputs"]["manifest_sha256"], r"^[0-9a-f]{64}$")
            restarted = self.controller(
                root,
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            replay = restarted.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(replay, complete)
            history = restarted.history(admitted["task_id"])
            self.assertEqual(len(history["attempts"]), 1)
            self.assertEqual(
                history["attempts"][0]["title"], task["package_intent"]["display_name"]
            )
            self.assertGreaterEqual(history["attempts"][0]["event_count"], 8)

    def test_restart_settings_drift_cannot_substitute_the_authorized_model(self):
        authorized = self.make_stage(model="gpt-authorized-at-consent")
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-model-drift-") as directory:
            root = Path(directory)
            first = self.controller(
                root,
                authorized,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            task = self.fresh_task(authorized)
            admitted = first.submit(task, self.registry(task), explicit_user_submit=True)

            changed_settings = self.make_stage(model="gpt-current-setting-after-restart")
            restarted = self.controller(
                root,
                changed_settings,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            failed = restarted.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["stage"], "codeagent")
            self.assertEqual(failed["error"]["code"], "model-mismatch")
            self.assertFalse(failed["error"]["same_task_retry_allowed"])
            attempt_root = root / "tasks" / admitted["task_id"] / "attempts" / admitted["attempt_id"]
            persisted_task = json.loads((attempt_root / "input/task.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted_task["model"], "gpt-authorized-at-consent")
            self.assertEqual(persisted_task["consent"]["model"], "gpt-authorized-at-consent")
            self.assertFalse((attempt_root / "codeagent/status.json").exists())

    def test_terminal_revalidation_rejects_incomplete_output_record(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-incomplete-terminal-") as directory:
            root = Path(directory)
            controller, _task, _registry, admitted, complete = self.complete_delivery(root)
            attempt_path = (
                root
                / "tasks"
                / admitted["task_id"]
                / "attempts"
                / admitted["attempt_id"]
                / "attempt.json"
            )
            value = json.loads(attempt_path.read_text(encoding="utf-8"))
            del value["outputs"]["candidate_path"]
            attempt_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.revalidate_attempt(complete["task_id"], complete["attempt_id"])
            self.assertEqual(caught.exception.code, "record-integrity-failure")

    def test_terminal_revalidation_rejects_builder_digest_drift(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-receipt-drift-") as directory:
            root = Path(directory)
            controller, _task, _registry, admitted, complete = self.complete_delivery(root)
            receipt = root / complete["outputs"]["builder_receipt_path"]
            receipt.chmod(0o600)
            receipt.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(caught.exception.code, "builder-receipt-invalid")

    def test_terminal_revalidation_recomputes_the_immutable_task(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-task-drift-") as directory:
            root = Path(directory)
            controller, _task, _registry, admitted, complete = self.complete_delivery(root)
            attempt_root = (
                root
                / "tasks"
                / admitted["task_id"]
                / "attempts"
                / admitted["attempt_id"]
            )
            task_path = attempt_root / "input/task.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["package_intent"]["display_name"] = "Substituted after admission"
            self.rewrite_json(task_path, task)
            attempt_path = attempt_root / "attempt.json"
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            # Even rewriting the mutable index hash cannot replace the immutable
            # provider-request digest and its single-use consent binding.
            attempt["canonical_task_sha256"] = delivery.sha256_bytes(
                delivery.canonical_json(task)
            )
            self.rewrite_json(attempt_path, attempt)
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.revalidate_attempt(complete["task_id"], complete["attempt_id"])
            self.assertEqual(caught.exception.code, "admission-binding-conflict")

    def test_terminal_revalidation_does_not_reauthorize_against_the_current_clock(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-historical-consent-") as directory:
            root = Path(directory)
            controller, _task, _registry, _admitted, complete = self.complete_delivery(root)
            with mock.patch.object(
                controller.codeagent.cloud,
                "validate_authorization",
                side_effect=AssertionError("historical replay must not re-run the live consent gate"),
            ):
                replay = controller.revalidate_attempt(
                    complete["task_id"], complete["attempt_id"]
                )
            self.assertEqual(replay, complete)

    def test_terminal_revalidation_rejects_receipt_entrypoint_trigger_drift(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-entrypoint-drift-") as directory:
            root = Path(directory)
            controller, _task, _registry, admitted, complete = self.complete_delivery(root)
            receipt_path = root / complete["outputs"]["builder_receipt_path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["package_entrypoints"][0]["label"] = "Substituted launcher"
            self.rewrite_json(receipt_path, receipt)
            attempt_path = (
                root
                / "tasks"
                / admitted["task_id"]
                / "attempts"
                / admitted["attempt_id"]
                / "attempt.json"
            )
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["outputs"]["builder_receipt_sha256"] = delivery.builder_sha256_file(
                receipt_path, delivery.MAX_DOCUMENT_BYTES, "test receipt"
            )
            self.rewrite_json(attempt_path, attempt)
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.revalidate_attempt(complete["task_id"], complete["attempt_id"])
            self.assertEqual(caught.exception.code, "builder-receipt-invalid")

    def test_terminal_revalidation_rejects_rehashed_manifest_semantic_drift(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-manifest-drift-") as directory:
            root = Path(directory)
            controller, _task, _registry, admitted, complete = self.complete_delivery(root)
            receipt_path = root / complete["outputs"]["builder_receipt_path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            manifest_path = receipt_path.parent / "package/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["entrypoints"][0]["label"] = "Manifest-only launcher"
            self.rewrite_json(manifest_path, manifest)
            manifest_sha = delivery.builder_sha256_file(
                manifest_path, delivery.MAX_DOCUMENT_BYTES, "test manifest"
            )
            receipt["manifest_sha256"] = manifest_sha
            manifest_inventory = next(
                row for row in receipt["package_files"] if row["path"] == "manifest.json"
            )
            manifest_inventory["sha256"] = manifest_sha
            manifest_inventory["size_bytes"] = manifest_path.stat().st_size
            self.rewrite_json(receipt_path, receipt)
            attempt_path = (
                root
                / "tasks"
                / admitted["task_id"]
                / "attempts"
                / admitted["attempt_id"]
                / "attempt.json"
            )
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["outputs"]["builder_receipt_sha256"] = delivery.builder_sha256_file(
                receipt_path, delivery.MAX_DOCUMENT_BYTES, "test receipt"
            )
            attempt["outputs"]["manifest_sha256"] = manifest_sha
            self.rewrite_json(attempt_path, attempt)
            with self.assertRaises(delivery.DeliveryError) as caught:
                controller.revalidate_attempt(complete["task_id"], complete["attempt_id"])
            self.assertEqual(caught.exception.code, "manifest-binding-mismatch")

    def test_terminal_revalidation_requires_existing_private_appstore_record(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-appstore-missing-") as directory:
            root = Path(directory)
            controller, _task, _registry, admitted, _complete = self.complete_delivery(root)
            database = root / "appstore/registry.sqlite3"
            database.unlink()
            for suffix in ("-wal", "-shm"):
                companion = Path(str(database) + suffix)
                if companion.exists():
                    companion.unlink()
            with self.assertRaises(delivery.StoreError) as caught:
                controller.revalidate_attempt(admitted["task_id"], admitted["attempt_id"])
            self.assertEqual(caught.exception.code, "not-found")

    def test_injected_failure_then_same_task_retry_preserves_attempt_history(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-retry-") as directory:
            root = Path(directory)
            failing = self.controller(
                root, stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools
            )
            first_task = self.fresh_task(stage, 1)
            registry = self.registry(first_task, "first authoritative no-match")
            first = failing.submit(first_task, registry, explicit_user_submit=True)
            failed = failing.run_attempt(first["task_id"], first["attempt_id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["stage"], "builder")
            self.assertEqual(failed["error"]["code"], "builder-timeout")
            self.assertTrue(failed["error"]["retryable_with_new_attempt"])
            self.assertTrue(failed["error"]["same_task_retry_allowed"])

            repaired = self.controller(
                root,
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            second = repaired.submit(
                self.retry_task(stage, first_task, 2),
                registry,
                explicit_user_submit=True,
                retry_task_id=first["task_id"],
            )
            complete = repaired.run_attempt(first["task_id"], second["attempt_id"])
            self.assertEqual(complete["status"], "private-appstore-ready")
            history = repaired.history(first["task_id"])
            self.assertEqual([item["status"] for item in history["attempts"]], ["failed", "private-appstore-ready"])
            self.assertEqual(history["attempts"][1]["retry_of_attempt_id"], first["attempt_id"])
            self.assertEqual(history["attempts"][0]["error"]["code"], "builder-timeout")

    def test_explicit_source_resume_rebuilds_without_another_provider_or_consent(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-source-resume-") as directory:
            root = Path(directory)
            controller = self.controller(root, stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools)
            task = self.fresh_task(stage)
            admitted = controller.submit(task, self.registry(task), explicit_user_submit=True)
            with mock.patch.object(stage.provider, "execute", wraps=stage.provider.execute) as provider:
                failed = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
                self.assertEqual(failed["error"]["code"], "builder-timeout")
                attempt = root / "tasks" / admitted["task_id"] / "attempts" / admitted["attempt_id"]
                original_events = (attempt / "events.jsonl").read_bytes()
                original_task = (attempt / "input/task.json").read_bytes()
                controller.builder_runner = delivery.SafeFixtureRunner(self.component.read_bytes())
                # The old default remains a terminal replay, not an implicit retry.
                self.assertEqual(controller.run_attempt(admitted["task_id"]), failed)
                with mock.patch.object(stage, "run", side_effect=AssertionError("recovery must never invoke CodeAgent")):
                    complete = controller.run_attempt(admitted["task_id"], resume_source_handoff=True)
                self.assertEqual(provider.call_count, 1)
            self.assertEqual(complete["status"], "private-appstore-ready", complete.get("error"))
            self.assertTrue(complete["outputs"]["digest_equality_proven"])
            self.assertEqual((attempt / "input/task.json").read_bytes(), original_task)
            preserved = root / complete["outputs"]["builder_previous_failure_path"]
            self.assertEqual(delivery._load_json(preserved / "job.json", "preserved synthetic Builder failure")["error"]["code"], "builder-timeout")
            events_bytes = (attempt / "events.jsonl").read_bytes()
            self.assertTrue(events_bytes.startswith(original_events))
            events = [json.loads(line) for line in events_bytes.splitlines()]
            self.assertEqual(sum(event["event"] == "source-handoff-resumed" for event in events), 1)
            self.assertTrue(any(event["error"] == failed["error"] for event in events))
            self.assertEqual(len(controller.history(admitted["task_id"])["attempts"]), 1)
            with mock.patch.object(delivery, "build_handoff") as builder:
                with self.assertRaises(delivery.DeliveryError) as caught:
                    controller.run_attempt(admitted["task_id"], resume_source_handoff=True)
                self.assertEqual(caught.exception.code, "source-resume-not-allowed")
                builder.assert_not_called()

    def test_source_resume_rejects_unready_cancelled_stale_or_changed_retained_evidence(self):
        for mutation in (
            "missing-status", "not-source-ready", "missing-handoff", "handoff-bytes",
            "source-bytes", "status-digest", "output-digest", "missing-output", "task-bytes",
            "cancelled", "worker-cancelled", "worker-active", "stale-attempt",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="vibapp-delivery-source-resume-negative-") as directory:
                stage = self.make_stage()
                root = Path(directory)
                controller = self.controller(root, stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools)
                task = self.fresh_task(stage)
                registry = self.registry(task)
                admitted = controller.submit(task, registry, explicit_user_submit=True)
                with mock.patch.object(stage.provider, "execute", wraps=stage.provider.execute) as provider:
                    failed = controller.run_attempt(admitted["task_id"])
                    self.assertEqual(failed["error"]["code"], "builder-timeout")
                    attempt = root / "tasks" / admitted["task_id"] / "attempts" / admitted["attempt_id"]
                    status_path = attempt / "codeagent/status.json"
                    status = delivery._load_json(status_path, "synthetic status")
                    handoff = root / failed["outputs"]["source_handoff_path"]
                    if mutation == "missing-status":
                        status_path.unlink()
                    elif mutation in {"not-source-ready", "status-digest"}:
                        status["status" if mutation == "not-source-ready" else "handoff_sha256"] = "failed" if mutation == "not-source-ready" else "0" * 64
                        delivery._write_json(status_path, status)
                    elif mutation == "missing-handoff":
                        handoff.unlink()
                    elif mutation == "handoff-bytes":
                        document = delivery._load_json(handoff, "synthetic handoff")
                        document["package_intent"]["description"] = "Changed retained handoff"
                        delivery._write_json(handoff, document)
                    elif mutation == "source-bytes":
                        source = handoff.parent / "source/src/lib.rs"
                        source.chmod(0o600)
                        source.write_text("// tampered synthetic fixture\n", encoding="utf-8")
                    elif mutation in {"output-digest", "missing-output"}:
                        if mutation == "output-digest":
                            failed["outputs"]["source_handoff_sha256"] = "0" * 64
                        else:
                            del failed["outputs"]["source_handoff_path"]
                        delivery._write_json(attempt / "attempt.json", failed)
                    elif mutation == "task-bytes":
                        changed = delivery._load_json(attempt / "input/task.json", "synthetic task")
                        changed["need_spec"]["requirements"][0]["text"] += " Changed after admission."
                        delivery._write_json(attempt / "input/task.json", changed)
                    elif mutation == "cancelled":
                        controller._fail(attempt, "codeagent", delivery.DeliveryError("provider-cancelled", "synthetic cancellation"))
                    elif mutation.startswith("worker-"):
                        worker = {
                            "schema_version": "vibapp.desktop-delivery-worker.experimental-v2",
                            "task_id": admitted["task_id"], "attempt_id": admitted["attempt_id"],
                            "immutable_task_digest_sha256": task["immutable_task_digest_sha256"],
                            "status": "worker-cancelled" if mutation == "worker-cancelled" else "running",
                            "terminal": mutation == "worker-cancelled", "cleanup_confirmed": True,
                        }
                        delivery._write_json(root / "desktop-workers" / f"{admitted['task_id']}-{admitted['attempt_id']}.json", worker)
                    else:
                        controller.submit(self.retry_task(stage, task, 2), registry, explicit_user_submit=True, retry_task_id=admitted["task_id"])
                    before_record = (attempt / "attempt.json").read_bytes()
                    before_events = (attempt / "events.jsonl").read_bytes()
                    with mock.patch.object(delivery, "build_handoff") as builder, mock.patch.object(stage, "run") as author:
                        with self.assertRaises((delivery.DeliveryError, delivery.PipelineError, OSError)):
                            controller.run_attempt(admitted["task_id"], admitted["attempt_id"], resume_source_handoff=True)
                        builder.assert_not_called()
                        author.assert_not_called()
                    self.assertEqual(provider.call_count, 1)
                    self.assertEqual((attempt / "attempt.json").read_bytes(), before_record)
                    self.assertEqual((attempt / "events.jsonl").read_bytes(), before_events)

    def test_source_resume_is_explicit_boolean_and_only_a_run_cli_option(self):
        parser = delivery.build_parser()
        prefix = ["--root", "/synthetic", "--model", "fixture"]
        self.assertFalse(parser.parse_args(prefix + ["run", "development-" + "a" * 32]).resume_source_handoff)
        self.assertTrue(parser.parse_args(prefix + ["run", "development-" + "a" * 32, "--resume-source-handoff"]).resume_source_handoff)
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-source-resume-queued-") as directory:
            controller = self.controller(Path(directory), stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools)
            task = self.fresh_task(stage)
            admitted = controller.submit(task, self.registry(task), explicit_user_submit=True)
            for value in (True, "true", 1, None):
                with self.assertRaises(delivery.DeliveryError) as caught:
                    controller.run_attempt(admitted["task_id"], resume_source_handoff=value)
                self.assertEqual(caught.exception.code, "source-resume-not-allowed")

    def test_docker_source_resume_and_terminal_replay_keep_exact_receipt_binding(self):
        stage = self.make_stage()
        original_run = stage.run

        def synthetic_docker_run(task_path, output_root, status_path):
            # Synthetic metadata fixture only: the provider is still the local
            # static spy. No Docker container, model, or compiler executes.
            status = original_run(task_path, output_root, status_path)
            task = delivery._load_json(task_path, "synthetic task")
            receipt = {
                "schema_version": "codeagent-launcher-receipt.experimental-v0", "executor_kind": "docker",
                "job_id": task["job_id"], "attempt_id": task["execution_attempt"]["attempt_id"],
                "idempotency_key": task["consent"]["consent_id"], "provider_id": stage.provider_id,
                "provider_profile_id": "synthetic-docker", "model": task["model"],
                "provider_profile_digest_sha256": task["provider_execution_identity"]["identity_sha256"],
                "task_digest_sha256": task["immutable_task_digest_sha256"],
                "image_digest_sha256": "a" * 64, "network_policy_digest_sha256": "b" * 64,
                "backend_execution_id": "synthetic-docker-execution", "request_digest_sha256": "c" * 64,
                "network_policy_id": "synthetic-offline", "resource_policy_id": "synthetic-bounded",
                "resource_policy_digest_sha256": "d" * 64, "input_digest_sha256": "e" * 64,
                "prompt_digest_sha256": "f" * 64, "output_digest_sha256": "1" * 64,
                "output_size_bytes": 1, "output_media_type": "application/vnd.vibapp.source-files+json",
                "cleanup_confirmed": True, "whole_job_quiescent": True,
            }
            handoff = output_root / status["handoff_relative_path"]
            document = delivery._load_json(handoff, "synthetic handoff")
            document["provider_execution"].update({
                "mode": "docker-local-explicit-opt-in", "adapter": "docker-codeagent-launcher",
                "external_request_observed": True, "gateway_request_id": receipt["backend_execution_id"],
                "isolation_policy_version": "vibapp.docker-source-v1", "runner_id": receipt["backend_execution_id"],
                "runner_identity_sha256": receipt["image_digest_sha256"],
                "isolation_policy_sha256": receipt["network_policy_digest_sha256"],
                "receipt_digest_sha256": delivery.sha256_bytes(delivery.canonical_json(receipt)),
            })
            delivery._write_json(handoff, document)
            status.update({"external_request_observed": True, "gateway_request_id": receipt["backend_execution_id"],
                           "container_receipt": receipt, "handoff_sha256": delivery.sha256_bytes(handoff.read_bytes())})
            delivery._write_json(status_path, status)
            return status

        stage.run = synthetic_docker_run
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-docker-source-resume-") as directory:
            root = Path(directory)
            controller = self.controller(root, stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools)
            task = self.fresh_task(stage)
            admitted = controller.submit(task, self.registry(task), explicit_user_submit=True)
            with mock.patch.object(stage.provider, "execute", wraps=stage.provider.execute) as provider:
                failed = controller.run_attempt(admitted["task_id"])
                self.assertEqual(failed["error"]["code"], "builder-timeout")
                attempt = root / "tasks" / admitted["task_id"] / "attempts" / admitted["attempt_id"]
                status_path = attempt / "codeagent/status.json"
                original_status = delivery._load_json(status_path, "synthetic status")
                for key, value in (("cleanup_confirmed", False), ("whole_job_quiescent", False), ("attempt_id", "attempt-9999-" + "0" * 16)):
                    changed = json.loads(json.dumps(original_status))
                    changed["container_receipt"][key] = value
                    delivery._write_json(status_path, changed)
                    with mock.patch.object(delivery, "build_handoff") as builder:
                        with self.assertRaises(delivery.DeliveryError):
                            controller.run_attempt(admitted["task_id"], resume_source_handoff=True)
                        builder.assert_not_called()
                delivery._write_json(status_path, original_status)
                controller.builder_runner = delivery.SafeFixtureRunner(self.component.read_bytes())
                with mock.patch.object(stage, "run", side_effect=AssertionError("no CodeAgent replay")):
                    complete = controller.run_attempt(admitted["task_id"], resume_source_handoff=True)
                    self.assertEqual(complete["status"], "private-appstore-ready", complete.get("error"))
                    self.assertEqual(controller.run_attempt(admitted["task_id"]), complete)
                self.assertEqual(provider.call_count, 1)
                for key, value in (("cleanup_confirmed", False), ("task_digest_sha256", "0" * 64), ("image_digest_sha256", "0" * 64)):
                    changed = json.loads(json.dumps(original_status))
                    changed["container_receipt"][key] = value
                    delivery._write_json(status_path, changed)
                    with mock.patch.object(delivery, "build_handoff") as builder:
                        with self.assertRaises(delivery.DeliveryError):
                            controller.run_attempt(admitted["task_id"])
                        builder.assert_not_called()
                delivery._write_json(status_path, original_status)

    def test_edited_needspec_creates_a_new_linked_delivery_task(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-edited-task-") as directory:
            root = Path(directory)
            failing = self.controller(
                root, stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools
            )
            first_task = self.fresh_task(stage, 1)
            first = failing.submit(
                first_task,
                self.registry(first_task, "first authoritative no-match"),
                explicit_user_submit=True,
            )
            failed = failing.run_attempt(first["task_id"], first["attempt_id"])
            self.assertEqual(failed["status"], "failed")

            repaired = self.controller(
                root,
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            replacement_task = self.fresh_task(stage, 2)
            replacement = repaired.submit(
                replacement_task,
                self.registry(replacement_task, "edited authoritative no-match"),
                explicit_user_submit=True,
                edited_from_task_id=first["task_id"],
            )
            self.assertNotEqual(replacement["task_id"], first["task_id"])
            self.assertEqual(replacement["attempt_number"], 1)
            self.assertIsNone(replacement["retry_of_attempt_id"])
            complete = repaired.run_attempt(replacement["task_id"], replacement["attempt_id"])
            self.assertEqual(complete["status"], "private-appstore-ready")
            replacement_history = repaired.history(replacement["task_id"])
            self.assertEqual(
                replacement_history["task"]["supersedes_task_id"], first["task_id"]
            )
            self.assertEqual(len(replacement_history["attempts"]), 1)

    def test_same_task_retry_rejects_registry_substitution_and_consent_reuse(self):
        stage = self.make_stage()
        with tempfile.TemporaryDirectory(prefix="vibapp-delivery-retry-binding-") as directory:
            root = Path(directory)
            failing = self.controller(
                root, stage, FailingBuilderRunner(), wasm_tools=self.wasm_tools
            )
            first_task = self.fresh_task(stage, 1)
            registry = self.registry(first_task, "fixed authoritative no-match")
            first = failing.submit(first_task, registry, explicit_user_submit=True)
            failed = failing.run_attempt(first["task_id"], first["attempt_id"])
            self.assertTrue(failed["error"]["same_task_retry_allowed"])

            repaired = self.controller(
                root,
                stage,
                delivery.SafeFixtureRunner(self.component.read_bytes()),
                wasm_tools=self.wasm_tools,
            )
            retry = self.retry_task(stage, first_task, 2)
            with self.assertRaises(delivery.DeliveryError) as caught:
                repaired.submit(
                    retry,
                    self.registry(retry, "substituted authoritative no-match"),
                    explicit_user_submit=True,
                    retry_task_id=first["task_id"],
                )
            self.assertEqual(caught.exception.code, "retry-binding-conflict")

            reused = self.retry_task(stage, first_task, 2)
            reused["consent"]["consent_id"] = first_task["consent"]["consent_id"]
            with self.assertRaises(delivery.DeliveryError) as caught:
                repaired.submit(
                    reused,
                    registry,
                    explicit_user_submit=True,
                    retry_task_id=first["task_id"],
                )
            self.assertEqual(caught.exception.code, "retry-attempt-required")

            skipped = self.retry_task(stage, first_task, 3)
            with self.assertRaises(delivery.DeliveryError) as caught:
                repaired.submit(
                    skipped,
                    registry,
                    explicit_user_submit=True,
                    retry_task_id=first["task_id"],
                )
            self.assertEqual(caught.exception.code, "retry-attempt-required")


class RetryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-retry-diagnostic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.attempt = {
            "task_id": "development-" + "a" * 32,
            "attempt_id": "attempt-0001-" + "b" * 16,
            "status": "running", "stage": "codeagent-running",
        }
        self.execution = self.root / "tasks" / self.attempt["task_id"] / "attempts" / self.attempt["attempt_id"] / "codeagent/output/executions" / self.attempt["attempt_id"]
        self.execution.mkdir(parents=True)

    def write(self, name, value):
        (self.execution / name).write_text(json.dumps(value), encoding="utf8")

    def retry(self):
        return {
            "state": "model-retry", "model_requests": 1, "logical_model_requests": 1,
            "model_retries": 0, "compiler_checks": 0,
            "retry": {"attempt": 2, "max_attempts": 3, "http_status": 502, "delay_seconds": 1.25, "reason": "provider-upstream-unavailable"},
        }

    def test_live_retry_counts_only_started_network_attempts(self):
        self.write("docker-progress.json", self.retry())
        self.assertEqual(delivery._codeagent_diagnostics(self.root, self.attempt), self.retry())

    def test_bounded_later_transport_retries_remain_visible_without_compiler_repair(self):
        for retries in (5, 8):
            value = {**self.retry(), "state": "authoring", "model_requests": 14 + retries,
                     "logical_model_requests": 14, "model_retries": retries}
            del value["retry"]
            self.write("docker-progress.json", value)
            self.assertEqual(delivery._codeagent_diagnostics(self.root, self.attempt), value)
            terminal = {**value, "state": "failed", "failure_code": "provider-upstream-unavailable",
                        "upstream_status": 502}
            self.write("docker-terminal.json", terminal)
            self.assertEqual(delivery._codeagent_diagnostics(self.root, self.attempt), terminal)
            (self.execution / "docker-terminal.json").unlink()
        invalid = {**value, "model_requests": 23, "model_retries": 9}
        self.write("docker-progress.json", invalid)
        self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))

    def test_exhausted_502_terminal_wins_over_stale_progress_and_omits_raw_data(self):
        self.write("docker-progress.json", self.retry())
        self.write("docker-terminal.json", {
            "state": "failed", "model_requests": 3, "logical_model_requests": 1,
            "model_retries": 2, "compiler_checks": 0, "failure_code": "provider-upstream-unavailable",
            "upstream_status": 502, "upstream_error": "SECRET /private/path",
            "upstream_request": {"ordinal": 3, "phase": "http-rejected", "bytes": 0, "headers": "SECRET", "path": "/private/path"},
            "request_history": [{"text": "SECRET"}], "retry": self.retry()["retry"],
        })
        result = delivery._codeagent_diagnostics(self.root, self.attempt)
        self.assertEqual(result["failure_code"], "provider-upstream-unavailable")
        self.assertEqual(result["upstream_status"], 502)
        self.assertEqual(result["upstream_request"], {"ordinal": 3, "phase": "http-rejected", "bytes": 0})
        self.assertNotIn("retry", result)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("/private/path", json.dumps(result))

    def test_legacy_fields_are_absent_not_invented_and_terminal_suppresses_retry(self):
        self.write("docker-progress.json", {"state": "authoring", "model_requests": 8, "compiler_checks": 1})
        result = delivery._codeagent_diagnostics(self.root, self.attempt)
        self.assertNotIn("logical_model_requests", result)
        self.assertNotIn("model_retries", result)
        for status in ("failed", "private-appstore-ready"):
            self.assertIsNone(delivery._codeagent_diagnostics(self.root, {**self.attempt, "status": status}))
        self.write("docker-terminal.json", {"state": "succeeded", "model_requests": 64, "compiler_checks": 3})
        self.assertEqual(delivery._codeagent_diagnostics(self.root, self.attempt)["state"], "succeeded")

    def test_terminal_projects_only_complete_closed_failure_diagnostic_without_retry_authority(self):
        value = {
            "schema_version": "vibapp.docker-failure-diagnostic-v1",
            "failure_origin": "provider-process", "provider_error_category": "stream-decode",
            "child_exit_code": 1, "child_signal": None, "stdout_bytes": 100, "stderr_bytes": 200,
            "output_limit_exceeded": False, "frame_limit_exceeded": False,
            "bridge_stdout_bytes": 300, "bridge_stderr_bytes": 0,
            "container_exit_code": 1, "container_oom_killed": False, "container_running": False,
        }
        terminal = {"state": "failed", "model_requests": 2, "compiler_checks": 0,
                    "failure_code": "provider-failed", "failure_diagnostic": value}
        self.write("docker-terminal.json", terminal)
        result = delivery._codeagent_diagnostics(self.root, self.attempt)
        self.assertEqual(result["failure_diagnostic"], value)
        self.assertEqual(result["failure_code"], "provider-failed")
        self.assertFalse(delivery.failure_retry_policy(result["failure_code"])[0])
        for bad in ({**value, "stderr": "PRIVATE_HEADER_AND_PROMPT"},
                    {**value, "stdout_bytes": 8 * 1024**2 + 1},
                    {**value, "container_oom_killed": 1},
                    {**value, "provider_error_category": "PRIVATE_EXCEPTION"},
                    {"failure_origin": "provider-process"}):
            with self.subTest(bad=bad):
                self.write("docker-terminal.json", {**terminal, "failure_diagnostic": bad})
                result = delivery._codeagent_diagnostics(self.root, self.attempt)
                self.assertNotIn("failure_diagnostic", result)
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertEqual(result["failure_code"], "provider-failed")
        self.write("docker-progress.json", {"state": "authoring", "model_requests": 2,
                                             "compiler_checks": 0, "failure_diagnostic": value})
        (self.execution / "docker-terminal.json").unlink()
        self.assertNotIn("failure_diagnostic", delivery._codeagent_diagnostics(self.root, self.attempt))

    def test_budget_projection_requires_exact_actual_counts_and_keeps_exhaustion_nontransient(self):
        budget = {"schema_version": "vibapp.model-request-budget-v1", "phase": "authoring",
                  "total_limit": 48, "authoring_limit": 40, "repair_limit": 8,
                  "total_used": 40, "authoring_used": 40, "repair_used": 0,
                  "total_remaining": 8, "authoring_remaining": 0, "repair_remaining": 8}
        terminal = {"state": "failed", "model_requests": 40, "logical_model_requests": 38,
                    "model_retries": 2, "compiler_checks": 0,
                    "failure_code": "provider-request-budget-exhausted", "upstream_status": 200,
                    "model_request_budget": budget}
        self.write("docker-terminal.json", terminal)
        result = delivery._codeagent_diagnostics(self.root, self.attempt)
        self.assertEqual(result["model_request_budget"], budget)
        self.assertEqual(result["failure_code"], "provider-request-budget-exhausted")
        self.assertFalse(delivery.failure_retry_policy(result["failure_code"])[0])
        for mutation in ({"total_used": 41}, {"total_remaining": 9}, {"repair_used": 1},
                         {"total_limit": 65}, {"authoring_used": True}, {"raw_response": "PRIVATE"}):
            self.write("docker-terminal.json", {**terminal, "model_request_budget": {**budget, **mutation}})
            result = delivery._codeagent_diagnostics(self.root, self.attempt)
            self.assertNotIn("model_request_budget", result)
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertEqual(result["failure_code"], "provider-request-budget-exhausted")

    def test_invalid_terminal_never_falls_back_to_retry_or_changes_success(self):
        self.write("docker-progress.json", self.retry())
        for value in ({"state": []}, {"state": "failed", "model_requests": 2, "compiler_checks": 0}, {"state": "succeeded", "model_requests": True, "compiler_checks": 0}):
            self.write("docker-terminal.json", value)
            self.assertIsNone(delivery._codeagent_diagnostics(self.root, {**self.attempt, "status": "private-appstore-ready"}))
        (self.execution / "docker-terminal.json").write_text('{"state":"failed","state":"succeeded"}')
        self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))

    def test_forged_retry_and_unbounded_diagnostics_are_ignored(self):
        mutations = [
            {"state": []}, {"model_requests": 65}, {"compiler_checks": -1}, {"model_retries": 9},
            {"retry": {**self.retry()["retry"], "reason": "SECRET"}},
            {"retry": {**self.retry()["retry"], "delay_seconds": float("nan")}},
            {"retry": {**self.retry()["retry"], "delay_seconds": 31}},
            {"retry": {**self.retry()["retry"], "http_status": 401}},
            {"retry": {**self.retry()["retry"], "attempt": True}},
            {"retry": {**self.retry()["retry"], "path": "/private/path"}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.write("docker-progress.json", {**self.retry(), **mutation})
                self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))
        (self.execution / "docker-progress.json").write_bytes(b" " * (delivery.MAX_DIAGNOSTIC_BYTES + 1))
        self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))

    def test_symlink_hardlink_and_wrong_attempt_paths_are_ignored(self):
        target = self.root / "outside.json"
        target.write_text(json.dumps(self.retry()))
        progress = self.execution / "docker-progress.json"
        progress.symlink_to(target)
        self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))
        progress.unlink()
        progress.hardlink_to(target)
        self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))
        progress.unlink()
        self.execution.rmdir()
        outside = self.root / "outside-execution"
        outside.mkdir()
        (outside / "docker-progress.json").write_text(json.dumps(self.retry()))
        self.execution.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(delivery._codeagent_diagnostics(self.root, self.attempt))
        for attempt_id in ("../outside-execution", "attempt-0001-" + "c" * 16):
            self.assertIsNone(delivery._codeagent_diagnostics(self.root, {**self.attempt, "attempt_id": attempt_id}))

    def test_status_and_history_attach_only_sanitized_ephemeral_projection(self):
        self.write("docker-progress.json", self.retry())
        controller = object.__new__(delivery.DeliveryController)
        controller.root = self.root
        task = {"current_attempt_id": self.attempt["attempt_id"], "attempt_ids": [self.attempt["attempt_id"]]}
        with mock.patch.object(controller, "_task_dir", return_value=self.root), mock.patch.object(controller, "_task_record", return_value=task), mock.patch.object(delivery, "_load_json", side_effect=lambda *args: dict(self.attempt)), mock.patch.object(delivery, "_validate_attempt_record", side_effect=lambda value: value):
            self.assertEqual(controller.status(self.attempt["task_id"])["attempt"]["codeagent_diagnostics"], self.retry())
            self.assertEqual(controller.history(self.attempt["task_id"])["attempts"][0]["codeagent_diagnostics"], self.retry())
        self.assertNotIn("codeagent_diagnostics", self.attempt)


if __name__ == "__main__":
    unittest.main()
