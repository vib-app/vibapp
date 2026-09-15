import base64
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from test_codeagent_adapter import adapter, cloud_agent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "codeagent-launcher"))
from docker_provider import DockerCodexProvider, DockerOpenCodeProvider
import docker_executor as backend
import codeagent_launcher as launcher


class DockerProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = DockerCodexProvider(adapter, cloud_agent, model="unit-model")
        self.limits = {"source_files": 128, "source_bytes": 1024 * 1024, "workspace_bytes": 2 * 1024 * 1024}

    def test_opencode_registry_routes_to_container_without_changing_host_gate(self):
        providers = adapter.provider_registry(cloud_agent, "unit-model")
        self.assertIsInstance(providers["codex"], DockerCodexProvider)
        self.assertIsInstance(providers["opencode"], DockerOpenCodeProvider)
        with mock.patch.object(backend, "codex_connection", side_effect=AssertionError("credential access")), mock.patch.object(backend, "image_identity") as identity:
            with self.assertRaisesRegex(adapter.AdapterError, "model-unsupported"):
                providers["opencode"].preflight()
            identity.assert_not_called()

    def test_opencode_preflight_binds_exact_image_relay_and_model(self):
        provider = DockerOpenCodeProvider(adapter, cloud_agent, model=backend.LOCALAI_MODEL)
        image = {"version": "1.18.27", "image_id": "sha256:" + "a" * 64, "bundle_sha256": "b" * 64, "bridge_sha256": "c" * 64, "policy_sha256": "d" * 64}
        with mock.patch.object(backend, "codex_connection", side_effect=AssertionError("credential access")), mock.patch.object(backend, "image_identity", return_value=image) as identity:
            result = provider.preflight()
        identity.assert_called_once_with(backend.OPENCODE_IMAGE, provider="opencode")
        self.assertTrue(result["execution_available"])
        self.assertEqual(result["credential_delivery"], "host-only-stdio-chat-relay")
        self.assertEqual(result["executable_path"], "docker://" + image["image_id"] + "/usr/local/bin/opencode")
        execution = result["provider_execution_identity"]
        self.assertEqual(execution["endpoint"]["kind"], "fixed-lan-http")
        self.assertEqual(execution["endpoint"]["canonical_endpoint"], backend.LOCALAI_ENDPOINT)
        self.assertEqual(execution["runtime"]["package_id"], "opencode-ai-cli-docker")
        self.assertNotIn("model_request_budget", result["docker_policy"])
        self.assertIsInstance(provider.gateway(), backend.LocalAIGateway)

    def record(self, path, content=b"code"):
        return {"path": path, "base64": base64.b64encode(content).decode(), "sha256": backend.digest(content)}

    def test_actual_skill_workspace_roundtrips_through_both_export_adapters(self):
        for provider_id, provider in (("openai-codex", self.provider), ("opencode", DockerOpenCodeProvider(adapter, cloud_agent, model=backend.LOCALAI_MODEL))):
            for mutation in (None, "skill-missing", "skill-changed", "apple-missing", "apple-changed"):
                with self.subTest(provider=provider_id, mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                    task = json.loads((cloud_agent.BASE / "fixtures/valid-task.json").read_bytes())
                    task["provider"] = provider_id
                    task["provider_execution_identity"]["runtime"]["package_id"] = "openai-codex-cli-docker" if provider_id == "openai-codex" else "opencode-ai-cli-docker"
                    task["consent"].pop("instructions_digest_sha256", None)
                    task["consent"]["instructions_digest_sha256"] = cloud_agent.provider_instructions_digest(provider_id, task)
                    root = Path(temporary)
                    source, destination = root / "source-workspace", root / "import-workspace"
                    cloud_agent.prepare_workspace(source, task)
                    cloud_agent.prepare_workspace(destination, task)
                    (source / "source/src/lib.rs").write_bytes((cloud_agent.DRY_RUN_SOURCE / "src/lib.rs").read_bytes())
                    files = [self.record(path.relative_to(source).as_posix(), path.read_bytes()) for path in source.rglob("*") if path.is_file()]
                    # Exactly four source files and three trusted contract files.
                    limits = {**self.limits, "source_files": 4}
                    self.assertEqual(len(files), 7 + len(cloud_agent.APPLE_DESIGN_FILES))
                    self.assertTrue(set(cloud_agent.apple_design_inputs()) <= {record["path"] for record in files})
                    if mutation == "apple-missing":
                        files = [record for record in files if record["path"] != "contracts/apple-design-SKILL.md"]
                    elif mutation == "apple-changed":
                        files = [self.record(record["path"], b"changed design guidance") if record["path"] == "contracts/apple-design-SKILL.md" else record for record in files]
                    if mutation == "skill-missing":
                        files = [record for record in files if record["path"] != "contracts/vibapp-ui-ux.md"]
                    elif mutation == "skill-changed":
                        files = [self.record(record["path"], b"changed passive guidance") if record["path"] == "contracts/vibapp-ui-ux.md" else record for record in files]
                    if mutation:
                        with self.assertRaises(adapter.AdapterError):
                            provider.import_source(destination, {"files": files}, limits)
                        self.assertFalse((destination / "source/src/lib.rs").exists())
                    else:
                        provider.import_source(destination, {"files": files}, limits)
                        self.assertEqual((destination / "contracts/vibapp-ui-ux.md").read_bytes(), cloud_agent.ui_ux_skill_bytes())
                        result = json.loads(cloud_agent.DRY_RUN_RESULT.read_bytes())
                        result["source_files"].append("src/vibapp_support.rs")
                        records = cloud_agent.audit_workspace(destination, task, result, cloud_agent.CONTRACT_DIGEST_PIN)
                        self.assertEqual(len(records), 4)

    def test_opencode_advertised_budgets_match_enforced_policy(self):
        provider = DockerOpenCodeProvider(adapter, cloud_agent, model=backend.LOCALAI_MODEL)
        with mock.patch.object(backend, "image_identity", return_value={"image_id": "test"}):
            _, configuration = provider.configuration()
        self.assertEqual(configuration["context_tokens"], backend.OPENCODE_POLICY["context_tokens"])
        self.assertEqual(configuration["max_output_tokens"], backend.OPENCODE_POLICY["max_output_tokens"])

    def test_codex_preflight_discloses_budget_and_rejects_changed_policy_identity(self):
        image = {"version": "codex-cli 0.153.4", "image_id": "sha256:" + "a" * 64,
                 "bundle_sha256": "b" * 64, "bridge_sha256": "c" * 64, "policy_sha256": "d" * 64}
        connection = {"kind": "https", "endpoint": "https://fixture.invalid/v1", "auth_kind": "api-key"}
        with mock.patch.object(self.provider, "configuration", return_value=(image, connection)), mock.patch.object(backend, "docker_command", side_effect=AssertionError("Docker access")), mock.patch.object(backend, "codex_connection", side_effect=AssertionError("credential access")):
            observed = self.provider.preflight()
            self.assertEqual(observed["docker_policy"]["model_request_budget"], {"total": 48, "authoring": 40, "repair": 8})
            self.assertEqual(observed["docker_policy"]["model_request_budget"], backend.POLICY["model_request_budget"])
            self.assertIsNot(observed["docker_policy"]["model_request_budget"], backend.POLICY["model_request_budget"])
            with mock.patch.dict(backend.POLICY, {"model_request_budget": {"total": 49, "authoring": 41, "repair": 8}}):
                with self.assertRaisesRegex(adapter.AdapterError, "provider-execution-identity-mismatch|changed"):
                    self.provider.assert_execution_identity(observed["provider_execution_identity"])

    def capture_failed_execution(self, provider):
        """Exercise adapter plumbing only; never start a container or relay."""
        prompt = b"Exact immutable provider_request fixture."
        provider._bound_execution_identity = {"identity_sha256": "b" * 64}
        task = {"job_id": "fixture-job", "execution_attempt": {"attempt_id": "fixture-attempt"},
                "consent": {"consent_id": "fixture-consent"}, "immutable_task_digest_sha256": "c" * 64}
        provider.bind_task(task)
        observed = {"docker_image_id": "sha256:" + "a" * 64, "docker_policy": {"policy_sha256": "d" * 64}}
        diagnostic = backend.empty_failure_diagnostic()
        gateway = types.SimpleNamespace(requests=40, observed=True)
        executor = types.SimpleNamespace(failure_code="provider-request-budget-exhausted", failure_diagnostic=diagnostic)
        service = mock.Mock()
        service.submit.return_value = {"state": "failed"}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            (workspace / "contracts").mkdir(parents=True)
            (workspace / "contracts/contract.wit").write_bytes(b"immutable fixture")
            with mock.patch.object(provider, "assert_execution_identity"), mock.patch.object(provider, "_preflight", return_value=observed), mock.patch.object(provider, "gateway", return_value=gateway), mock.patch.object(provider, "import_source") as import_source, mock.patch.object(backend, "DockerExecutor", return_value=executor) as factory, mock.patch.object(launcher, "LauncherService", return_value=service), mock.patch.object(backend, "docker_command", side_effect=AssertionError("Docker access")):
                with self.assertRaises(adapter.AdapterError) as failure:
                    provider.execute(workspace=workspace, prompt=prompt, limits=self.limits,
                                     temporary_root=Path(directory) / "temporary", cancel_file=None)
                import_source.assert_not_called()
                service.cancel.assert_not_called()
                payload = factory.call_args.kwargs["input_payload"]
                request = service.submit.call_args.args[0]
        self.assertEqual(task["immutable_task_digest_sha256"], request.task_digest_sha256)
        self.assertEqual(request.input_digest_sha256, backend.digest(backend.canonical(payload["files"])))
        self.assertEqual(request.prompt_digest_sha256, backend.digest(payload["prompt"].encode()))
        return prompt, payload, request, failure.exception, diagnostic

    def test_codex_actual_prompt_and_receipt_request_bind_initial_budget_notice(self):
        prompt, payload, request, failure, diagnostic = self.capture_failed_execution(self.provider)
        notice = backend.format_budget_notice()
        self.assertEqual(payload["prompt"], prompt.decode() + "\n\n" + notice + "\n")
        self.assertNotEqual(request.prompt_digest_sha256, backend.digest(prompt))
        self.assertEqual(failure.code, "provider-request-budget-exhausted")
        self.assertEqual(failure.failure_diagnostic, diagnostic)
        self.assertTrue(failure.provider_process_started)
        self.assertTrue(failure.external_request_attempted)
        self.assertTrue(failure.external_request_observed)

    def test_opencode_actual_prompt_does_not_inherit_codex_budget(self):
        provider = DockerOpenCodeProvider(adapter, cloud_agent, model=backend.LOCALAI_MODEL)
        with mock.patch.object(backend, "format_budget_notice", side_effect=AssertionError("Codex-only budget notice")):
            prompt, payload, request, _, _ = self.capture_failed_execution(provider)
        self.assertEqual(payload["prompt"], prompt.decode())
        self.assertEqual(request.prompt_digest_sha256, backend.digest(prompt))

    def test_export_rejects_traversal_nul_duplicates_and_bad_base64(self):
        records = [[self.record(path)] for path in ["source/../secret", "/source/lib.rs", "source/x\0.rs", "source//lib.rs", "contracts/../../secret"]]
        records += [[self.record("source/src/lib.rs")] * 2, [{**self.record("source/src/lib.rs"), "base64": "!invalid!"}]]
        for files in records:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(adapter.AdapterError):
                    self.provider.import_source(Path(directory), {"files": files}, self.limits)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_export_cannot_replace_trusted_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            cargo = root / "source/Cargo.toml"
            cargo.write_bytes(b"trusted")
            with self.assertRaisesRegex(adapter.AdapterError, "trusted input"):
                self.provider.import_source(root, {"files": [self.record("source/Cargo.toml", b"changed")]}, self.limits)
            self.assertEqual(cargo.read_bytes(), b"trusted")

    def test_export_has_aggregate_limit_before_writing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [self.record("source/src/a.rs", b"x" * 600), self.record("source/src/b.rs", b"x" * 600)]
            with self.assertRaisesRegex(adapter.AdapterError, "aggregate"):
                self.provider.import_source(Path(directory), {"files": files}, {**self.limits, "workspace_bytes": 1000})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_omitted_or_late_changed_scaffold_does_not_import_partial_source(self):
        for include_scaffold in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "source").mkdir()
                (root / "source/Cargo.toml").write_bytes(b"trusted")
                files = [self.record("source/src/lib.rs")]
                if include_scaffold:
                    files.append(self.record("source/Cargo.toml", b"changed"))
                with self.assertRaises(adapter.AdapterError):
                    self.provider.import_source(root, {"files": files}, self.limits)
                self.assertFalse((root / "source/src").exists())

    def test_symlink_parent_cannot_escape_private_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            workspace = root / "workspace"
            (workspace / "source").mkdir(parents=True)
            (workspace / "source/src").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(adapter.AdapterError):
                self.provider.import_source(workspace, {"files": [self.record("source/src/lib.rs")]}, self.limits)
            self.assertEqual(list(outside.iterdir()), [])

    def test_compile_failure_is_preserved_and_repairable_without_new_consent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.provider._execution_root = root
            self.provider._task = {"limits": self.limits}
            self.provider.cloud_agent = mock.Mock()
            self.provider.api = mock.Mock()
            self.provider._builder_limits = lambda **kwargs: kwargs
            error = types.SimpleNamespace()
            class CompileError(RuntimeError):
                code = "process-failed"
            builder = mock.Mock()
            builder.execute.side_effect = CompileError("error[E0308]: expected String, found &str")
            self.provider._builder = builder
            result = self.provider.check_source({"id": 0, "files": []}, mock.Mock(), 30)
            self.assertTrue(result["repairable"])
            self.assertFalse(result["ok"])
            self.assertEqual(json.loads((root / "source-check-0/check.json").read_bytes()), result)
            self.provider.cloud_agent.claim_consent.assert_not_called()
            builder.execute.assert_called_once()

    def test_integrity_failure_never_invites_model_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            self.provider._execution_root = Path(directory)
            self.provider._task = {"limits": self.limits}
            self.provider.cloud_agent = mock.Mock()
            self.provider.cloud_agent.prepare_workspace.side_effect = cloud_agent.WorkerError("integrity-failure", "contract changed")
            self.provider._builder = mock.Mock()
            result = self.provider.check_source({"id": 0, "files": []}, mock.Mock(), 30)
            self.assertFalse(result["repairable"])
            self.provider._builder.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
