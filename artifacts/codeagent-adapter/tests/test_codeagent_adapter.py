from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


BASE = Path(__file__).resolve().parents[1]
CLOUD_BASE = BASE.parent / "cloud-agent"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


adapter = load_module("test_codeagent_adapter_module", BASE / "codeagent_adapter.py")
cloud_agent = adapter.load_cloud_agent(CLOUD_BASE / "cloud_agent.py")


class FixtureProvider:
    provider_id = "codex"
    task_provider = "openai-codex"

    def __init__(
        self,
        provider_id="codex",
        task_provider="openai-codex",
        model="gpt-fixture-explicit",
    ):
        self.provider_id = provider_id
        self.task_provider = task_provider
        self.model = model
        self.execute_called = False
        self.cargo_before_provider = None
        self.cargo_mode_before_provider = None

    def preflight(self):
        return {
            "available": True,
            "identity_observed": True,
            "execution_available": True,
            "synthetic_offline_test": True,
            "adapter_version": adapter.ADAPTER_VERSION,
            "adapter_sha256": "9" * 64,
            "executable_path": "/synthetic/reviewed/codex",
            "executable_version": "codex-cli 0.150.1",
            "executable_sha256": "a" * 64,
            "identity_policy_version": adapter.CODEX_COMPATIBILITY_POLICY_VERSION,
            "identity_basis": "synthetic-test-identity",
            "signing_team_identifier": "synthetic-team",
            "digest_reviewed": True,
            "protocol_version": adapter.CODEX_PROTOCOL_VERSION,
            "protocol_sha256": "f" * 64,
            "protocol_preflight_passed": True,
        }

    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        del prompt, limits, temporary_root, cancel_file
        self.execute_called = True
        source = CLOUD_BASE / "fixtures/dry-run/source"
        cargo = workspace / "source/Cargo.toml"
        self.cargo_before_provider = cargo.read_bytes()
        self.cargo_mode_before_provider = cargo.stat().st_mode & 0o777
        (workspace / "source/src").mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(source / "src/lib.rs", workspace / "source/src/lib.rs")
        # Deliberately bogus provider prose/control output.  The adapter must
        # discard it and derive the control record from task + source tree.
        result = {"status": "provider-self-claimed-success", "untrusted": True}
        (workspace / "provider-last-message.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return adapter.ProviderExecution(
            provider_id=self.provider_id,
            provider_process_started=True,
            external_request_attempted=True,
            external_request_observed=False,
            gateway_request_id=None,
            executable_path="/synthetic/reviewed/codex",
            executable_version="codex-cli 0.150.1",
            executable_sha256="a" * 64,
            stdout_bytes=128,
            stderr_bytes=0,
        )


class FailingProvider(FixtureProvider):
    def __init__(self, code, *, started=True):
        super().__init__()
        self.code = code
        self.started = started

    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        del workspace, prompt, limits, temporary_root, cancel_file
        self.execute_called = True
        raise adapter.AdapterError(
            self.code,
            f"synthetic {self.code}",
            provider_process_started=self.started,
            external_request_attempted=self.started,
        )


class FailingPreflightProvider(FixtureProvider):
    def preflight(self):
        raise adapter.AdapterError(
            "provider-protocol-incompatible",
            "synthetic incompatible provider protocol",
        )


class ContainmentBlockedProvider(FixtureProvider):
    def preflight(self):
        result = super().preflight()
        result["available"] = False
        result["execution_available"] = False
        result["execution_blocker"] = {
            "code": adapter.LIVE_CONTAINMENT_ERROR,
            "message": "synthetic containment backend unavailable",
        }
        return result


class IdentityMismatchProvider(ContainmentBlockedProvider):
    def preflight(self):
        result = super().preflight()
        result.pop("synthetic_offline_test", None)
        fixture = json.loads((CLOUD_BASE / "fixtures/valid-task.json").read_text(encoding="utf-8"))
        result["provider_execution_identity"] = fixture["provider_execution_identity"]
        return result


class ControlDirectoryProvider(FixtureProvider):
    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        execution = super().execute(
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
        )
        result_path = workspace / "provider-last-message.json"
        result_path.unlink()
        result_path.mkdir()
        return execution


class CapabilityMismatchProvider(FixtureProvider):
    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        execution = super().execute(
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
        )
        result_path = workspace / "provider-last-message.json"
        result = json.loads(
            (CLOUD_BASE / "fixtures/dry-run/provider-result.json").read_text(encoding="utf-8")
        )
        result["declared_capabilities"][-1] = "vibapp:experimental-v0/http@0.0.1"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return execution


class CargoMutatingProvider(FixtureProvider):
    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        execution = super().execute(
            workspace=workspace,
            prompt=prompt,
            limits=limits,
            temporary_root=temporary_root,
            cancel_file=cancel_file,
        )
        cargo = workspace / "source/Cargo.toml"
        cargo.chmod(0o644)
        cargo.write_bytes(cargo.read_bytes() + b"# provider rewrite\n")
        cargo.chmod(0o444)
        return execution


class WorkspaceBreachingProvider(FixtureProvider):
    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        del prompt, temporary_root, cancel_file
        self.execute_called = True
        with (workspace / "oversized.bin").open("wb") as handle:
            handle.truncate(limits["workspace_bytes"] + 1)
        return adapter.ProviderExecution(
            provider_id="codex",
            provider_process_started=True,
            external_request_attempted=True,
            external_request_observed=False,
            gateway_request_id=None,
            executable_path="/synthetic/reviewed/codex",
            executable_version="codex-cli 0.150.1",
            executable_sha256="a" * 64,
            stdout_bytes=0,
            stderr_bytes=0,
        )


class SyntheticCommandCloud:
    WorkerError = cloud_agent.WorkerError

    def __init__(self, script):
        self.script = script
        self.last_command = None

    def codex_command(self, codex_bin, workspace, model):
        del codex_bin, workspace
        self.last_command = ["/bin/sh", str(self.script), "--model", model]
        return self.last_command

    @staticmethod
    def workspace_usage(workspace, limits):
        return cloud_agent.workspace_usage(workspace, limits)


class ExactExecutableCommandCloud(SyntheticCommandCloud):
    def codex_command(self, codex_bin, workspace, model):
        self.last_command = [
            str(codex_bin),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            str(workspace),
            "-o",
            str(workspace / "provider-last-message.json"),
            "-m",
            model,
            "-",
        ]
        return self.last_command


class SyntheticCommandProvider(adapter.CodexProvider):
    sandbox_owner = adapter.ADAPTER_SEATBELT

    def _identity(self):
        return {
            "configured_path": str(self.codex_bin),
            "resolved_path": str(self.codex_bin),
            "version": "synthetic",
            "sha256": "b" * 64,
        }

    def _auth_environment(self, temporary_root):
        home = temporary_root / "home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        return {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "HOME": str(home),
            "TMPDIR": str(temporary_root),
        }


class SyntheticClaudeProvider(adapter.ClaudeCodeProvider):
    def _identity(self):
        return {
            "configured_path": str(self.executable),
            "resolved_path": str(self.executable),
            "version": "synthetic",
            "sha256": "c" * 64,
        }


class SyntheticOpenCodeProvider(adapter.OpenCodeProvider):
    def _identity(self):
        return {
            "configured_path": str(self.executable),
            "resolved_path": str(self.executable),
            "version": "synthetic",
            "sha256": "d" * 64,
        }


class SyntheticGeminiProvider(adapter.GeminiCliProvider):
    def _identity(self):
        return {
            "configured_path": str(self.executable),
            "resolved_path": str(self.executable),
            "version": "synthetic",
            "sha256": "e" * 64,
            "bundle_sha256": "f" * 64,
            "bundle_files": 1,
            "bundle_bytes": 1,
        }


class CodeAgentAdapterTests(unittest.TestCase):
    def fresh_task(
        self,
        task_provider="openai-codex",
        model="gpt-fixture-explicit",
    ):
        task = json.loads((CLOUD_BASE / "fixtures/valid-task.json").read_text(encoding="utf-8"))
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        task["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task["provider"] = task_provider
        task["consent"]["provider"] = task_provider
        task["model"] = model
        task["consent"]["model"] = model
        identity = task["provider_execution_identity"]
        if task_provider == "opencode":
            endpoint = {"kind": "https", "canonical_endpoint": "https://synthetic.invalid/v1"}
        else:
            endpoint = {"kind": "provider-managed", "canonical_endpoint": "provider-managed"}
        identity["endpoint"] = {
            **endpoint,
            "endpoint_sha256": cloud_agent.sha256_bytes(cloud_agent.canonical_json(endpoint)),
        }
        identity["runtime"].update(
            {
                "adapter_id": "local-codeagent-adapter",
                "adapter_version": adapter.ADAPTER_VERSION,
                "adapter_sha256": "9" * 64,
                "package_id": task_provider,
                "package_version": "synthetic-test",
                "executable_sha256": "a" * 64,
            }
        )
        identity["non_secret_config_sha256"] = "8" * 64
        identity_body = {key: value for key, value in identity.items() if key != "identity_sha256"}
        identity["identity_sha256"] = cloud_agent.sha256_bytes(cloud_agent.canonical_json(identity_body))
        task["consent"]["provider_execution_identity_sha256"] = identity["identity_sha256"]
        task["consent"]["instructions_digest_sha256"] = cloud_agent.provider_instructions_digest(task_provider)
        digest = cloud_agent.immutable_task_digest(task)
        task["immutable_task_digest_sha256"] = digest
        task["consent"]["payload_digest_sha256"] = digest
        return task

    def run_task(self, root, provider, *, cancel_file=None):
        task = self.fresh_task(provider.task_provider, provider.model)
        task_path = root / "task.json"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        status_path = root / "status/job.json"
        status = adapter.execute_task(
            cloud_agent,
            provider,
            task_path,
            root / "output",
            status_path,
            confirm_job=task["job_id"],
            confirm_consent=task["consent"]["consent_id"],
            acknowledge_external_cost=True,
            cancel_file=cancel_file,
        )
        return task, status, status_path

    def process_limits(self, **updates):
        limits = {
            # Seatbelt startup can exceed three seconds on a busy developer Mac;
            # timeout-specific tests override this with their own tight limit.
            "wall_time_seconds": 10,
            "cpu_seconds": 5,
            "memory_bytes": 256 * 1024 * 1024,
            "pids": 4,
            "workspace_bytes": 64 * 1024,
            "stdout_bytes": 1024,
            "stderr_bytes": 1024,
            "source_bytes": 32 * 1024,
            "source_files": 8,
        }
        limits.update(updates)
        return limits

    def test_every_real_adapter_requires_an_explicit_model_at_construction(self):
        for provider_type in (
            adapter.CodexProvider,
            adapter.ClaudeCodeProvider,
            adapter.OpenCodeProvider,
            adapter.GeminiCliProvider,
        ):
            for value in (None, "", "   "):
                with self.subTest(provider=provider_type.__name__, value=value), self.assertRaises(adapter.AdapterError) as caught:
                    provider_type(cloud_agent, model=value)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "model-invalid")

    def test_adapter_identity_binds_current_script_bytes(self):
        identity = adapter.adapter_identity()
        self.assertEqual(identity["adapter_id"], "local-codeagent-adapter")
        self.assertEqual(identity["adapter_version"], adapter.ADAPTER_VERSION)
        self.assertEqual(
            identity["adapter_sha256"],
            cloud_agent.sha256_file(BASE / "codeagent_adapter.py"),
        )

    def test_secret_free_preflight_identity_matches_cloud_v3_contract_for_all_providers(self):
        for provider_id, task_provider in adapter.TASK_PROVIDER_BY_ID.items():
            with self.subTest(provider_id=provider_id):
                kwargs = {}
                if provider_id == "opencode":
                    kwargs = {
                        "endpoint_kind": "https",
                        "canonical_endpoint": "https://synthetic.invalid/v1",
                    }
                observed = adapter.provider_preflight(
                    provider_id=provider_id,
                    task_provider=task_provider,
                    model="synthetic/model",
                    identity={
                        "configured_path": "/synthetic/provider",
                        "resolved_path": "/synthetic/provider",
                        "version": "synthetic-1",
                        "sha256": "7" * 64,
                    },
                    authentication="synthetic-metadata-only",
                    credential_delivery="not-delivered",
                    runtime_package_id=f"{provider_id}-package",
                    **kwargs,
                )
                validated = cloud_agent.validate_provider_execution_identity(
                    observed["provider_execution_identity"],
                    task_provider,
                    "test.provider_execution_identity",
                )
                self.assertEqual(
                    validated["identity_sha256"],
                    observed["provider_execution_identity"]["identity_sha256"],
                )
                self.assertFalse(observed["execution_available"])

    def test_provider_output_schema_is_accepted_by_strict_structured_output(self):
        schema = json.loads((CLOUD_BASE / "schemas/provider-result.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("uniqueItems", json.dumps(schema, sort_keys=True))

        def visit(value):
            if isinstance(value, dict):
                if "const" in value or "enum" in value:
                    self.assertIn("type", value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)

    def test_adapter_derives_control_record_from_task_and_source_not_provider_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            task = self.fresh_task()
            cloud_agent.prepare_workspace(workspace, task)
            (workspace / "source/src").mkdir()
            shutil.copy2(
                CLOUD_BASE / "fixtures/dry-run/source/src/lib.rs",
                workspace / "source/src/lib.rs",
            )
            (workspace / "provider-last-message.json").write_text(
                '{"status":"self-accepted","declared_capabilities":["ambient-network"]}',
                encoding="utf-8",
            )
            result = adapter.derive_provider_control_record(cloud_agent, workspace, task)
            self.assertEqual(result["job_id"], task["job_id"])
            self.assertEqual(result["declared_capabilities"], task["target"]["required_imports"])
            self.assertEqual(
                result["source_files"],
                ["Cargo.toml", "src/lib.rs", "wit/contract.wit"],
            )
            self.assertNotIn("self-accepted", json.dumps(result))
            persisted = json.loads(
                (workspace / "provider-last-message.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, result)

    @unittest.skipUnless(
        Path("/opt/homebrew/Caskroom/codex/0.150.1/bin/codex").is_file(),
        "reviewed Codex 0.150.1 cask is not installed",
    )
    def test_current_signed_codex_static_identity_does_not_execute_provider(self):
        identity = adapter.resolve_codex_observed_identity(
            Path("/opt/homebrew/Caskroom/codex/0.150.1/bin/codex")
        )
        self.assertEqual(identity["version"], "codex-cli 0.150.1")
        self.assertEqual(
            identity["sha256"],
            "a14f9a907c12c8812878b70e6b7d65f81c39ed795513e46a55817d7428c0ca6b",
        )
        self.assertEqual(identity["signing_team_identifier"], "2DC432GLL2")
        self.assertTrue(identity["signature_requirement_satisfied"])
        self.assertTrue(identity["digest_reviewed"])
        self.assertEqual(identity["identity_policy_version"], adapter.CODEX_COMPATIBILITY_POLICY_VERSION)
        self.assertEqual(identity["protocol_version"], adapter.CODEX_PROTOCOL_VERSION)
        self.assertFalse(identity["protocol_preflight_passed"])
        self.assertEqual(identity["protocol_observation"], "provider-not-executed-without-containment")

    @unittest.skipUnless(
        sys.platform == "darwin" and adapter.DEFAULT_CODEX_BIN.is_file(),
        "signed Homebrew Codex launcher is not installed",
    )
    def test_signed_codex_version_range_is_advisory_for_identity_observation(self):
        policy = dict(adapter.LOCAL_CODEX_COMPATIBILITY_POLICY)
        policy["minimum_version"] = (0, 0, 0)
        policy["maximum_version_exclusive"] = (0, 0, 1)
        identity = adapter.resolve_codex_observed_identity(adapter.DEFAULT_CODEX_BIN, policy)
        self.assertFalse(identity["version_within_exercised_range"])
        self.assertTrue(identity["signature_requirement_satisfied"])
        self.assertFalse(identity["protocol_preflight_passed"])

    @unittest.skipUnless(sys.platform == "darwin", "macOS Developer ID verification")
    def test_unknown_forged_codex_is_rejected_before_its_protocol_can_execute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cask_root = root / "Caskroom/codex"
            executable = cask_root / "0.150.1/bin/codex"
            executable.parent.mkdir(parents=True)
            marker = root / "forged-executed"
            executable.write_text(
                f"#!/bin/sh\ntouch {json.dumps(str(marker))}\nprintf 'codex-cli 0.150.1\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            policy = dict(adapter.LOCAL_CODEX_COMPATIBILITY_POLICY)
            policy["configured_paths"] = (str(executable),)
            policy["resolved_roots"] = (str(cask_root),)
            policy["reviewed_digests"] = {}
            with self.assertRaises(adapter.AdapterError) as caught:
                adapter.resolve_codex_observed_identity(executable, policy)
            self.assertEqual(caught.exception.code, "executable-identity-invalid")
            self.assertFalse(marker.exists(), "an untrusted binary must not reach version/protocol execution")

    def test_codex_protocol_probe_requires_every_safety_and_noninteractive_option(self):
        compatible = "\n".join(
            (
                "Run Codex non-interactively",
                "--config <key=value>",
                "--model <MODEL>",
                "--sandbox <SANDBOX_MODE> workspace-write",
                "--cd <DIR>",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--json",
                "--output-last-message <FILE>",
            )
        ).encode()
        self.assertRegex(adapter.validate_codex_protocol(compatible), r"^[0-9a-f]{64}$")
        with self.assertRaises(adapter.AdapterError) as caught:
            adapter.validate_codex_protocol(compatible.replace(b"--ignore-rules", b"--load-rules"))
        self.assertEqual(caught.exception.code, "provider-protocol-incompatible")

    def test_live_codex_protocol_probe_is_closed_without_containment(self):
        with self.assertRaises(adapter.AdapterError) as caught:
            adapter.resolve_codex_compatible_identity(Path("/synthetic/provider-must-not-run"))
        self.assertEqual(caught.exception.code, adapter.LIVE_CONTAINMENT_ERROR)
        self.assertFalse(caught.exception.provider_process_started)

    def test_codex_identity_change_after_preflight_fails_before_provider_start(self):
        first = {
            "configured_path": "/reviewed/codex",
            "resolved_path": "/reviewed/codex",
            "version": "codex-cli 0.151.0",
            "sha256": "a" * 64,
        }
        changed = {**first, "version": "codex-cli 0.152.0", "sha256": "b" * 64}
        provider = adapter.CodexProvider(
            SyntheticCommandCloud(Path("/never-run")),
            Path("/reviewed/codex"),
            {},
            model="gpt-5.6-sol",
        )
        with mock.patch.object(provider, "_auth_source", return_value=Path("/safe/auth.json")), \
             mock.patch.object(provider, "_identity", side_effect=[first, changed]):
            observed = provider.preflight()["provider_execution_identity"]
            with self.assertRaises(adapter.AdapterError) as caught:
                provider.assert_execution_identity(observed)
        self.assertEqual(caught.exception.code, "provider-execution-identity-mismatch")
        self.assertFalse(caught.exception.provider_process_started)
        self.assertFalse(caught.exception.external_request_attempted)

    def test_codex_execute_rechecks_bound_identity_before_supervision(self):
        first = {
            "configured_path": "/reviewed/codex",
            "resolved_path": "/reviewed/codex",
            "version": "codex-cli 0.151.0",
            "sha256": "a" * 64,
        }
        changed = {**first, "version": "codex-cli 0.152.0", "sha256": "b" * 64}
        provider = adapter.CodexProvider(
            SyntheticCommandCloud(Path("/never-run")),
            Path("/reviewed/codex"),
            {},
            model="gpt-5.6-sol",
        )
        provider._bound_execution_identity = provider._preflight_for_identity(first)[
            "provider_execution_identity"
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            with mock.patch.object(adapter, "require_live_containment_backend", return_value=None), \
                 mock.patch.object(provider, "_identity", return_value=changed), \
                 mock.patch.object(adapter, "supervise_provider") as supervise:
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider.execute(
                        workspace=workspace,
                        prompt=b"task",
                        limits=self.process_limits(),
                        temporary_root=root,
                        cancel_file=None,
                    )
        self.assertEqual(caught.exception.code, "provider-execution-identity-mismatch")
        supervise.assert_not_called()

    def test_codex_execute_runs_a_verified_snapshot_when_original_path_changes_after_auth_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            executable = root / "codex-a"
            replacement = root / "codex-b"
            marker_a = root / "verified-a-ran"
            marker_b = root / "replacement-b-ran"
            executable.write_text(
                f"#!/bin/sh\n/bin/cat >/dev/null\n: > {str(marker_a)!r}\n",
                encoding="utf-8",
            )
            replacement.write_text(
                f"#!/bin/sh\n/bin/cat >/dev/null\n: > {str(marker_b)!r}\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            replacement.chmod(0o700)
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            identity = {
                "configured_path": str(executable),
                "resolved_path": str(executable),
                "version": "codex-cli 0.151.0",
                "sha256": digest,
            }
            cloud = ExactExecutableCommandCloud(executable)
            provider = adapter.CodexProvider(cloud, executable, {}, model="gpt-5.6-sol")
            provider._bound_execution_identity = provider._preflight_for_identity(identity)[
                "provider_execution_identity"
            ]

            def replace_original_during_auth(_temporary_root):
                os.replace(replacement, executable)
                return {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(root),
                    "TMPDIR": str(root),
                }

            with mock.patch.object(adapter, "require_live_containment_backend", return_value=None), \
                 mock.patch.object(provider, "_identity", return_value=identity), \
                 mock.patch.object(provider, "_auth_environment", side_effect=replace_original_during_auth):
                execution = provider.execute(
                    workspace=workspace,
                    prompt=b"task",
                    limits=self.process_limits(),
                    temporary_root=root,
                    cancel_file=None,
                )

            snapshot = Path(cloud.last_command[0])
            self.assertNotEqual(snapshot, executable)
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), digest)
            self.assertTrue(marker_a.is_file())
            self.assertFalse(marker_b.exists())
            self.assertEqual(execution.executable_sha256, digest)
            self.assertEqual(execution.executable_path, str(executable))

    def test_codex_native_sandbox_is_exactly_workspace_bound_without_outer_seatbelt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            command = cloud_agent.codex_command(Path("/reviewed/codex"), workspace, "gpt-5.6-sol")
            command[2:2] = [
                "-c",
                'shell_environment_policy.inherit="none"',
                "-c",
                'shell_environment_policy.set={PATH="/usr/bin:/bin",LANG="C.UTF-8",LC_ALL="C.UTF-8",TZ="UTC"}',
            ]
            selected = adapter.provider_process_command(
                command,
                workspace,
                root / "temporary",
                provider_id="codex",
                sandbox_owner=adapter.CODEX_NATIVE_WORKSPACE_SANDBOX,
            )
            self.assertEqual(selected, command)
            self.assertNotEqual(selected[0], "/usr/bin/sandbox-exec")

            for mutation in (
                [*command[:-1], "--dangerously-bypass-approvals-and-sandbox", "-"],
                ["/reviewed/codex", *command[1:command.index("workspace-write")], "read-only", *command[command.index("workspace-write") + 1:]],
            ):
                with self.subTest(mutation=mutation), self.assertRaises(adapter.AdapterError) as caught:
                    adapter.provider_process_command(
                        mutation,
                        workspace,
                        root / "temporary",
                        provider_id="codex",
                        sandbox_owner=adapter.CODEX_NATIVE_WORKSPACE_SANDBOX,
                    )
                self.assertEqual(caught.exception.code, "sandbox-policy-invalid")

            with self.assertRaises(adapter.AdapterError) as caught:
                adapter.provider_process_command(
                    command,
                    workspace,
                    root / "temporary",
                    provider_id="opencode",
                    sandbox_owner=adapter.CODEX_NATIVE_WORKSPACE_SANDBOX,
                )
            self.assertEqual(caught.exception.code, "sandbox-policy-invalid")

    def test_fixture_provider_creates_only_untrusted_builder_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = FixtureProvider()
            task, status, status_path = self.run_task(root, provider)
            self.assertEqual(status["status"], "source-ready")
            self.assertEqual(status["current_stage"], "awaiting-separate-builder")
            self.assertTrue(status["external_request_attempted"])
            self.assertFalse(status["external_request_observed"])
            self.assertFalse(status["builder_invoked"])
            self.assertFalse(status["verifier_invoked"])
            self.assertFalse(status["installation_performed"])
            self.assertFalse(status["publication_performed"])
            self.assertTrue(status["consent_consumed"])
            self.assertEqual(status["executable_path"], "/synthetic/reviewed/codex")
            self.assertEqual(status["executable_version"], "codex-cli 0.150.1")
            self.assertEqual(status["executable_sha256"], "a" * 64)
            self.assertEqual(
                status["executable_identity_policy_version"],
                adapter.CODEX_COMPATIBILITY_POLICY_VERSION,
            )
            self.assertTrue(status["provider_protocol_preflight_passed"])
            self.assertEqual(status["events"][-1]["event"], "source-handoff-persisted")
            self.assertIn("adapter-control-record-derived", [event["event"] for event in status["events"]])
            self.assertEqual(adapter.read_status(status_path), status)
            handoff = json.loads((root / "output" / status["handoff_relative_path"]).read_text(encoding="utf-8"))
            self.assertEqual(handoff["status"], "untrusted-source-awaiting-builder")
            self.assertEqual(handoff["authority"]["compile"], "separate-builder")
            self.assertEqual(handoff["authority"]["verify"], "separate-verifier")
            self.assertEqual(handoff["authority"]["install"], "none")
            self.assertEqual(handoff["authority"]["publish"], "none")
            self.assertEqual(handoff["provider_execution"]["adapter"], "local-codeagent-adapter")
            self.assertEqual(handoff["provider_execution"]["provider_id"], "codex")
            self.assertEqual(provider.cargo_before_provider, cloud_agent.canonical_cargo_manifest(task))
            self.assertEqual(provider.cargo_mode_before_provider, 0o444)
            source = root / "output" / Path(status["handoff_relative_path"]).parent / handoff["source_directory"]
            self.assertEqual((source / "Cargo.toml").read_bytes(), cloud_agent.canonical_cargo_manifest(task))

    def test_every_registered_provider_is_bound_to_its_exact_task_provider(self):
        mappings = adapter.TASK_PROVIDER_BY_ID
        registry = adapter.provider_registry(cloud_agent, "provider-preflight-explicit")
        self.assertEqual(set(registry), set(mappings))
        for provider_id, task_provider in mappings.items():
            with self.subTest(provider_id=provider_id), tempfile.TemporaryDirectory() as temporary:
                provider = FixtureProvider(provider_id, task_provider)
                _, status, _ = self.run_task(Path(temporary), provider)
                self.assertEqual(status["provider_id"], provider_id)
                self.assertEqual(status["task_provider"], task_provider)
                self.assertEqual(status["current_stage"], "awaiting-separate-builder")
                handoff = json.loads(
                    (Path(temporary) / "output" / status["handoff_relative_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(handoff["provider_execution"]["provider_id"], provider_id)

    def test_provider_mismatch_fails_before_status_consent_or_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.fresh_task("openai-codex")
            task_path = root / "task.json"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            provider = FixtureProvider("claude-code", "anthropic-claude-code")
            status_path = root / "status/job.json"
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter.execute_task(
                    cloud_agent,
                    provider,
                    task_path,
                    root / "output",
                    status_path,
                    confirm_job=task["job_id"],
                    confirm_consent=task["consent"]["consent_id"],
                    acknowledge_external_cost=True,
                )
            self.assertEqual(raised.exception.code, "provider-mismatch")
            self.assertFalse(provider.execute_called)
            self.assertFalse(status_path.exists())

    def test_model_mismatch_fails_before_status_consent_or_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.fresh_task("openai-codex", "gpt-authorized")
            task_path = root / "task.json"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            provider = FixtureProvider(model="gpt-current-setting")
            status_path = root / "status/job.json"
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter.execute_task(
                    cloud_agent,
                    provider,
                    task_path,
                    root / "output",
                    status_path,
                    confirm_job=task["job_id"],
                    confirm_consent=task["consent"]["consent_id"],
                    acknowledge_external_cost=True,
                )
            self.assertEqual(raised.exception.code, "model-mismatch")
            self.assertFalse(provider.execute_called)
            self.assertFalse(status_path.exists())

    def test_legacy_task_without_model_fails_closed_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.fresh_task()
            del task["model"]
            del task["consent"]["model"]
            task_path = root / "task.json"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            provider = FixtureProvider()
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter.execute_task(
                    cloud_agent,
                    provider,
                    task_path,
                    root / "output",
                    root / "status/job.json",
                    confirm_job=task["job_id"],
                    confirm_consent=task["consent"]["consent_id"],
                    acknowledge_external_cost=True,
                )
            self.assertEqual(raised.exception.code, "model-binding-required")
            self.assertFalse(provider.execute_called)

    def test_null_and_blank_task_consent_or_adapter_models_fail_before_execution(self):
        cases = [
            (None, "gpt-fixture-explicit", "gpt-fixture-explicit"),
            ("", "gpt-fixture-explicit", "gpt-fixture-explicit"),
            ("   ", "gpt-fixture-explicit", "gpt-fixture-explicit"),
            ("gpt-fixture-explicit", None, "gpt-fixture-explicit"),
            ("gpt-fixture-explicit", "", "gpt-fixture-explicit"),
            ("gpt-fixture-explicit", "gpt-fixture-explicit", None),
            ("gpt-fixture-explicit", "gpt-fixture-explicit", "   "),
        ]
        for task_model, consent_model, adapter_model in cases:
            with self.subTest(
                task_model=task_model,
                consent_model=consent_model,
                adapter_model=adapter_model,
            ), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = self.fresh_task()
                task["model"] = task_model
                task["consent"]["model"] = consent_model
                task_path = root / "task.json"
                task_path.write_text(json.dumps(task), encoding="utf-8")
                provider = FixtureProvider(model=adapter_model)
                with self.assertRaises(adapter.AdapterError) as raised:
                    adapter.execute_task(
                        cloud_agent,
                        provider,
                        task_path,
                        root / "output",
                        root / "status/job.json",
                        confirm_job=task["job_id"],
                        confirm_consent=task["consent"]["consent_id"],
                        acknowledge_external_cost=True,
                    )
                self.assertEqual(raised.exception.code, "model-binding-required")
                self.assertFalse(provider.execute_called)
                self.assertFalse((root / "status/job.json").exists())

    def test_in_memory_provider_validation_preserves_original_digest_bound_value(self):
        for provider_id, task_provider in adapter.TASK_PROVIDER_BY_ID.items():
            with self.subTest(provider_id=provider_id):
                task = self.fresh_task(task_provider)
                original = json.loads(json.dumps(task))
                validated = adapter.validate_task_value_for_provider(
                    cloud_agent,
                    task,
                    FixtureProvider(provider_id, task_provider),
                )
                self.assertIs(validated, task)
                self.assertEqual(validated, original)
                self.assertEqual(validated["provider"], task_provider)
                self.assertEqual(validated["consent"]["provider"], task_provider)
                self.assertEqual(
                    validated["immutable_task_digest_sha256"],
                    cloud_agent.immutable_task_digest(validated),
                )

    def test_exact_submit_consent_and_cost_ack_are_required_before_status_or_provider(self):
        cases = [
            ("wrong-job", None, True),
            (None, "wrong-consent", True),
            (None, None, False),
        ]
        for wrong_job, wrong_consent, cost_ack in cases:
            with self.subTest(wrong_job=wrong_job, wrong_consent=wrong_consent, cost_ack=cost_ack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = self.fresh_task()
                    task_path = root / "task.json"
                    task_path.write_text(json.dumps(task), encoding="utf-8")
                    provider = FixtureProvider()
                    status_path = root / "status/job.json"
                    with self.assertRaises(adapter.AdapterError) as raised:
                        adapter.execute_task(
                            cloud_agent,
                            provider,
                            task_path,
                            root / "output",
                            status_path,
                            confirm_job=wrong_job or task["job_id"],
                            confirm_consent=wrong_consent or task["consent"]["consent_id"],
                            acknowledge_external_cost=cost_ack,
                        )
                    self.assertEqual(raised.exception.code, "external-opt-in-required")
                    self.assertFalse(provider.execute_called)
                    self.assertFalse(status_path.exists())

    def test_existing_status_must_match_the_immutable_task_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, status, status_path = self.run_task(root, FixtureProvider())
            status["job_id"] = "job-other"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            task_path = root / "task.json"
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter.execute_task(
                    cloud_agent,
                    FixtureProvider(),
                    task_path,
                    root / "output",
                    status_path,
                    confirm_job=task["job_id"],
                    confirm_consent=task["consent"]["consent_id"],
                    acknowledge_external_cost=True,
                )
            self.assertEqual(raised.exception.code, "status-binding-conflict")

    def assert_terminal_error(self, provider, expected_code, expected_stage="codeagent-failed-closed"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(adapter.AdapterError) as raised:
                _, _, status_path = self.run_task(root, provider)
            self.assertEqual(raised.exception.code, expected_code)
            status_path = root / "status/job.json"
            status = adapter.read_status(status_path)
            self.assertIsNotNone(status)
            self.assertEqual(status["current_stage"], expected_stage)
            self.assertEqual(status["error"]["code"], expected_code)
            self.assertEqual(status["events"][-1]["event"], "job-failed" if expected_stage == "codeagent-failed-closed" else "source-validation-failed")
            self.assertEqual(
                status["retry_policy"],
                "terminal-status-requires-new-job; new-consent-required-when-consumed",
            )
            self.assertFalse(status["builder_invoked"])
            self.assertFalse(status["installation_performed"])
            return status

    def test_provider_failure_is_durable_and_bounded(self):
        status = self.assert_terminal_error(FailingProvider("provider-failed"), "provider-failed")
        self.assertTrue(status["provider_process_started"])
        self.assertTrue(status["external_request_attempted"])

    def test_closed_docker_failure_diagnostic_survives_adapter_status_without_retry_authority(self):
        value = {
            "schema_version": "vibapp.docker-failure-diagnostic-v1",
            "failure_origin": "provider-process", "provider_error_category": "stream-decode",
            "child_exit_code": 1, "child_signal": None, "stdout_bytes": 10, "stderr_bytes": 20,
            "output_limit_exceeded": False, "frame_limit_exceeded": False,
            "bridge_stdout_bytes": 30, "bridge_stderr_bytes": 0,
            "container_exit_code": 1, "container_oom_killed": False, "container_running": False,
        }
        class DiagnosticProvider(FixtureProvider):
            def execute(self, **_):
                raise adapter.AdapterError("provider-failed", "Provider process failed",
                                           provider_process_started=True, external_request_attempted=True,
                                           external_request_observed=True, failure_diagnostic=value)
        status = self.assert_terminal_error(DiagnosticProvider(), "provider-failed")
        self.assertEqual(status["failure_diagnostic"], value)
        self.assertEqual(status["error"]["code"], "provider-failed")
        self.assertTrue(status["external_request_observed"])

    def test_adapter_drops_invalid_diagnostic_without_persisting_raw_fields(self):
        class DiagnosticProvider(FixtureProvider):
            def execute(self, **_):
                raise adapter.AdapterError("provider-failed", "Provider process failed",
                                           provider_process_started=True,
                                           failure_diagnostic={"raw_stderr": "PRIVATE_BEARER_AND_PROMPT"})
        status = self.assert_terminal_error(DiagnosticProvider(), "provider-failed")
        self.assertIsNone(status.get("failure_diagnostic"))
        self.assertNotIn("PRIVATE", json.dumps(status))

    def test_incompatible_preflight_is_durable_but_does_not_consume_consent_or_start_provider(self):
        provider = FailingPreflightProvider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(adapter.AdapterError) as caught:
                self.run_task(root, provider)
            self.assertEqual(caught.exception.code, "provider-protocol-incompatible")
            status = adapter.read_status(root / "status/job.json")
            self.assertEqual(status["error"]["code"], "provider-protocol-incompatible")
            self.assertFalse(status["consent_consumed"])
            self.assertFalse(status["provider_process_started"])
            self.assertFalse(status["external_request_attempted"])
            self.assertFalse(provider.execute_called)

    def test_identity_observation_records_exact_adapter_then_blocks_before_consent(self):
        provider = ContainmentBlockedProvider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(adapter.AdapterError) as caught:
                self.run_task(root, provider)
            self.assertEqual(caught.exception.code, adapter.LIVE_CONTAINMENT_ERROR)
            status = adapter.read_status(root / "status/job.json")
            self.assertTrue(status["identity_observed"])
            self.assertFalse(status["execution_available"])
            self.assertEqual(status["execution_blocker"]["code"], adapter.LIVE_CONTAINMENT_ERROR)
            self.assertEqual(status["adapter_version"], adapter.ADAPTER_VERSION)
            self.assertEqual(status["adapter_sha256"], "9" * 64)
            self.assertFalse(status["consent_consumed"])
            self.assertFalse(status["provider_process_started"])
            self.assertFalse(status["external_request_attempted"])
            self.assertFalse(provider.execute_called)

    def test_observed_execution_identity_mismatch_blocks_before_consent(self):
        provider = IdentityMismatchProvider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(adapter.AdapterError) as caught:
                self.run_task(root, provider)
            self.assertEqual(caught.exception.code, "provider-execution-identity-mismatch")
            status = adapter.read_status(root / "status/job.json")
            self.assertFalse(status["consent_consumed"])
            self.assertFalse(status["provider_process_started"])
            self.assertFalse(status["external_request_attempted"])
            self.assertFalse(provider.execute_called)

    def test_timeout_is_durable_and_bounded(self):
        status = self.assert_terminal_error(FailingProvider("provider-timeout"), "provider-timeout")
        self.assertTrue(status["external_request_attempted"])

    def test_cancel_before_consent_does_not_invoke_provider(self):
        provider = FixtureProvider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cancel = root / "cancel"
            cancel.touch(mode=0o600)
            cancel.chmod(0o600)
            with self.assertRaises(adapter.AdapterError) as raised:
                self.run_task(root, provider, cancel_file=cancel)
            self.assertEqual(raised.exception.code, "provider-cancelled")
            self.assertFalse(provider.execute_called)
            status = adapter.read_status(root / "status/job.json")
            self.assertEqual(status["status"], "codeagent-cancelled")
            self.assertFalse(status["consent_consumed"])
            self.assertFalse(status["external_request_attempted"])

    def test_provider_control_directory_fails_closed_without_recursive_cleanup(self):
        status = self.assert_terminal_error(
            ControlDirectoryProvider(),
            "source-invalid",
        )
        self.assertTrue(status["external_request_attempted"])

    def test_provider_capability_mismatch_cannot_replace_handoff_task_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, status, _ = self.run_task(root, CapabilityMismatchProvider())
            self.assertEqual(status["status"], "source-ready")
            self.assertEqual(status["current_stage"], "awaiting-separate-builder")
            self.assertFalse(status["builder_invoked"])
            handoff = json.loads(
                (root / "output" / status["handoff_relative_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["target"], task["target"])
            self.assertNotIn("declared_capabilities", handoff)

    def test_provider_cargo_rewrite_fails_closed_before_builder_handoff(self):
        status = self.assert_terminal_error(
            CargoMutatingProvider(),
            "source-invalid",
            expected_stage="source-validation-failed-closed",
        )
        self.assertTrue(status["external_request_attempted"])
        self.assertFalse(status["builder_invoked"])

    def test_workspace_resource_breach_is_durable(self):
        status = self.assert_terminal_error(
            WorkspaceBreachingProvider(),
            "workspace-output-limit",
            expected_stage="source-validation-failed-closed",
        )
        self.assertTrue(status["external_request_attempted"])

    def test_live_supervisor_blocks_fork_setsid_escape_before_process_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "escaped-descendant-wrote"
            script = root / "escape.py"
            script.write_text(
                "import os, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    os.setsid()\n"
                "    null = os.open('/dev/null', os.O_RDWR)\n"
                "    os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)\n"
                "    time.sleep(0.2)\n"
                f"    open({str(marker)!r}, 'w').write('escaped')\n"
                "    os._exit(0)\n"
                "os._exit(0)\n",
                encoding="utf-8",
            )
            workspace = root / "workspace"
            workspace.mkdir()
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter.supervise_provider(
                    cloud_agent=SyntheticCommandCloud(script),
                    provider_id="synthetic-live",
                    provider_label="synthetic escape provider",
                    identity={"resolved_path": sys.executable, "version": "synthetic", "sha256": "0" * 64},
                    command=[sys.executable, str(script)],
                    environment={"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root)},
                    workspace=workspace,
                    prompt=b"synthetic",
                    limits=self.process_limits(),
                    temporary_root=root / "temp",
                    cancel_file=None,
                )
            self.assertEqual(raised.exception.code, adapter.LIVE_CONTAINMENT_ERROR)
            self.assertFalse(raised.exception.provider_process_started)
            self.assertFalse(raised.exception.external_request_attempted)
            import time
            time.sleep(0.35)
            self.assertFalse(marker.exists())
            self.assertEqual(list(workspace.iterdir()), [])

    def test_explicit_model_is_forwarded_as_an_argv_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "exit.py"
            script.write_text("exit 0\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            cloud = SyntheticCommandCloud(script)
            provider = SyntheticCommandProvider(
                cloud,
                Path(sys.executable),
                {},
                model="gpt-5.6-sol",
            )
            provider.command_preview(workspace, provider._identity())
            self.assertIn(["--model", "gpt-5.6-sol"], [cloud.last_command[index:index + 2] for index in range(len(cloud.last_command) - 1)])

    def test_cancel_marker_prevents_any_provider_or_descendant_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "provider-started"
            script = root / "start.py"
            script.write_text(f"open({str(marker)!r}, 'w').write('started')\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            cancel = root / "cancel"
            cancel.touch(mode=0o600)
            cancel.chmod(0o600)
            with self.assertRaises(adapter.AdapterError) as raised:
                adapter.supervise_provider(
                    cloud_agent=SyntheticCommandCloud(script),
                    provider_id="synthetic-live",
                    provider_label="synthetic cancelled provider",
                    identity={"resolved_path": sys.executable, "version": "synthetic", "sha256": "0" * 64},
                    command=[sys.executable, str(script)],
                    environment={"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root)},
                    workspace=workspace,
                    prompt=b"synthetic",
                    limits=self.process_limits(),
                    temporary_root=root / "temp",
                    cancel_file=cancel,
                )
            self.assertEqual(raised.exception.code, "provider-cancelled")
            self.assertFalse(raised.exception.provider_process_started)
            self.assertFalse(raised.exception.external_request_attempted)
            self.assertFalse(marker.exists())

    def test_local_effective_limits_prevent_task_from_raising_adapter_ceilings(self):
        requested = {
            "memory_bytes": 4 * 1024 * 1024 * 1024,
            "pids": 64,
            "wall_time_seconds": 3600,
            "cpu_seconds": 3600,
            "workspace_bytes": 64 * 1024 * 1024,
            "stdout_bytes": 1024 * 1024,
            "stderr_bytes": 1024 * 1024,
            "source_bytes": 16 * 1024 * 1024,
            "source_files": 256,
        }
        effective = adapter.effective_local_limits(requested)
        self.assertEqual(effective["memory_bytes"], 2 * 1024 * 1024 * 1024)
        self.assertEqual(effective["pids"], adapter.MAX_LOCAL_PIDS)
        self.assertEqual(effective["wall_time_seconds"], 1800)
        self.assertEqual(effective["cpu_seconds"], 1800)

    def test_one_job_lease_rejects_local_parallel_fanout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with adapter.one_job_lease(root):
                with self.assertRaises(adapter.AdapterError) as raised:
                    with adapter.one_job_lease(root / "different-attempt-output"):
                        self.fail("second local job lease unexpectedly succeeded")
            self.assertEqual(raised.exception.code, "local-capacity-busy")

    @unittest.skipUnless(sys.platform == "darwin", "macOS clonefile credential broker")
    def test_credential_broker_clones_without_adapter_content_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic-auth.json"
            destination = root / "scoped-auth.json"
            source.write_text('{"synthetic":"no-real-secret"}', encoding="utf-8")
            source.chmod(0o600)
            adapter.CodexProvider._clone_auth(source, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertNotEqual(destination.stat().st_ino, source.stat().st_ino)

    def test_every_live_cli_execute_path_is_gated_before_script_start(self):
        cases = [
            (SyntheticClaudeProvider, "claude-test-model"),
            (SyntheticOpenCodeProvider, "synthetic/provider-model"),
            (SyntheticGeminiProvider, "gemini-test-model"),
        ]
        for provider_type, model in cases:
            with self.subTest(provider=provider_type.__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / "provider-started"
                script = root / "synthetic-provider.sh"
                script.write_text(f"#!/bin/sh\ntouch {marker!s}\n", encoding="utf-8")
                script.chmod(0o700)
                workspace = root / "workspace"
                workspace.mkdir(mode=0o700)
                provider = provider_type(cloud_agent, script, {}, model=model)
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider.execute(
                        workspace=workspace,
                        prompt=b"synthetic provider task",
                        limits=self.process_limits(),
                        temporary_root=root / "provider-temporary",
                        cancel_file=None,
                    )
                self.assertEqual(caught.exception.code, adapter.LIVE_CONTAINMENT_ERROR)
                self.assertFalse(caught.exception.provider_process_started)
                self.assertFalse(caught.exception.external_request_attempted)
                self.assertFalse(marker.exists())

    def test_opencode_model_without_provider_segment_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ambient_home = Path(temporary) / "ambient-home"
            (ambient_home / ".config/opencode").mkdir(mode=0o700, parents=True)
            (ambient_home / ".config/opencode/opencode.json").write_text(
                json.dumps({"provider": {"zhipu": {"npm": "@ai-sdk/openai-compatible"}}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(cloud_agent, "synthetic-opencode", {}, model="bare-model")
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider._strict_selected_provider_metadata()
                self.assertEqual(caught.exception.code, "model-invalid")

    def test_opencode_missing_provider_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            ambient_home = Path(temporary) / "ambient-home"
            (ambient_home / ".config/opencode").mkdir(mode=0o700, parents=True)
            (ambient_home / ".config/opencode/opencode.json").write_text(
                json.dumps({"provider": {"other": {"npm": "@ai-sdk/openai-compatible"}}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(cloud_agent, "synthetic-opencode", {}, model="zhipu/glm-5.3-flash")
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider._strict_selected_provider_metadata()
                self.assertEqual(caught.exception.code, "opencode-config-invalid")

    def test_opencode_jsonc_and_unterminated_comments_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ambient_home = Path(temporary) / "ambient-home"
            config = ambient_home / ".config/opencode/opencode.jsonc"
            config.parent.mkdir(mode=0o700, parents=True)
            config.write_text(
                '{\n'
                '  // provider map\n'
                '  "provider": {\n'
                '    /* zhipu entry */\n'
                '    "zhipu": {"npm": "@ai-sdk/openai-compatible", "options": {"apiKey": "k//not-comment"}}\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )
            config.chmod(0o600)
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(cloud_agent, "synthetic-opencode", {}, model="zhipu/glm-5.3-flash")
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider._strict_selected_provider_metadata()
                self.assertEqual(caught.exception.code, "opencode-config-invalid")

            strict = ambient_home / ".config/opencode/opencode.json"
            strict.write_text('{"provider":{"zhipu":{/* never closed', encoding="utf-8")
            strict.chmod(0o600)
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(cloud_agent, "synthetic-opencode", {}, model="zhipu/glm-5.3-flash")
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider._strict_selected_provider_metadata()
                self.assertEqual(caught.exception.code, "opencode-config-invalid")

    def test_opencode_secret_config_is_rejected_without_clone_or_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ambient_home = root / "ambient-home"
            ambient_config = ambient_home / ".config/opencode/opencode.json"
            ambient_config.parent.mkdir(mode=0o700, parents=True)
            ambient_config.write_text(
                json.dumps(
                    {
                        "provider": {
                            "synthetic": {
                                "npm": "@ai-sdk/openai-compatible",
                                "options": {"baseURL": "https://synthetic.invalid/v1", "apiKey": "synthetic-config-credential"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            ambient_config.chmod(0o600)
            script = root / "synthetic-opencode.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o700)
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(cloud_agent, script, {}, model="synthetic/provider-model")
                with mock.patch.object(
                    adapter,
                    "strict_json_bytes",
                    side_effect=AssertionError("secret-bearing config reached JSON deserialization"),
                ), self.assertRaises(adapter.AdapterError) as caught:
                    provider.preflight()
                self.assertEqual(caught.exception.code, adapter.OPENCODE_SCOPED_CREDENTIAL_ERROR)
                self.assertFalse((ambient_home / ".local/share/opencode/auth.json").exists())
                temporary_root = root / "provider-temporary"
                with self.assertRaises(adapter.AdapterError) as environment_error:
                    provider._environment(temporary_root)
                self.assertEqual(environment_error.exception.code, adapter.OPENCODE_SCOPED_CREDENTIAL_ERROR)
                self.assertFalse((temporary_root / "data/opencode/auth.json").exists())
                self.assertFalse((temporary_root / "opencode-config.json").exists())

    def test_opencode_duplicate_keys_are_rejected_by_strict_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            ambient_home = Path(temporary) / "ambient-home"
            config = ambient_home / ".config/opencode/opencode.json"
            config.parent.mkdir(mode=0o700, parents=True)
            config.write_text(
                '{"provider":{"synthetic":{"npm":"one"},"synthetic":{"npm":"two"}}}',
                encoding="utf-8",
            )
            config.chmod(0o600)
            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(
                    cloud_agent,
                    "synthetic-opencode",
                    {},
                    model="synthetic/provider-model",
                )
                with self.assertRaises(adapter.AdapterError) as caught:
                    provider._strict_selected_provider_metadata()
                self.assertEqual(caught.exception.code, "opencode-config-invalid")
                self.assertIn("unique object keys", str(caught.exception))

    def test_opencode_safe_identity_preflight_returns_secret_free_binding_and_closed_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ambient_home = root / "ambient-home"
            config = ambient_home / ".config/opencode/opencode.json"
            config.parent.mkdir(mode=0o700, parents=True)
            config.write_text(
                json.dumps(
                    {
                        "provider": {
                            "synthetic": {
                                "npm": "@ai-sdk/openai-compatible",
                                "options": {"baseURL": "https://synthetic.invalid/v1"},
                                "models": {"provider-model": {"name": "Synthetic Model"}},
                            }
                        }
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            config.chmod(0o600)
            auth = ambient_home / ".local/share/opencode/auth.json"
            auth.parent.mkdir(mode=0o700, parents=True)
            auth.write_text('{"synthetic":"opaque"}', encoding="utf-8")
            auth.chmod(0o600)
            original_os_open = os.open

            def reject_auth_content_open(path, flags, *args, **kwargs):
                if Path(path) == auth:
                    raise AssertionError("OpenCode shared auth store bytes must not be opened")
                return original_os_open(path, flags, *args, **kwargs)

            with mock.patch.dict(os.environ, {"HOME": str(ambient_home)}, clear=True):
                provider = SyntheticOpenCodeProvider(
                    cloud_agent,
                    "synthetic-opencode",
                    {},
                    model="synthetic/provider-model",
                )
                with mock.patch.object(os, "open", side_effect=reject_auth_content_open):
                    preflight = provider.preflight()
            self.assertTrue(preflight["identity_observed"])
            self.assertFalse(preflight["execution_available"])
            self.assertEqual(preflight["execution_blocker"]["code"], adapter.LIVE_CONTAINMENT_ERROR)
            self.assertEqual(preflight["adapter_id"], "local-codeagent-adapter")
            self.assertEqual(preflight["adapter_version"], adapter.ADAPTER_VERSION)
            self.assertRegex(preflight["adapter_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(preflight["provider_endpoint"], "https://synthetic.invalid/v1")
            self.assertRegex(preflight["provider_endpoint_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(preflight["provider_runtime_package"], "@ai-sdk/openai-compatible")
            validated_identity = cloud_agent.validate_provider_execution_identity(
                preflight["provider_execution_identity"],
                "opencode",
                "test.preflight.provider_execution_identity",
            )
            self.assertEqual(
                validated_identity["identity_sha256"],
                preflight["provider_execution_identity"]["identity_sha256"],
            )
            self.assertNotIn("opaque", json.dumps(preflight))
            self.assertFalse((root / "provider-temporary").exists())


if __name__ == "__main__":
    unittest.main()
