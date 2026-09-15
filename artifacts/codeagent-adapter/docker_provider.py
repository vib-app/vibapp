"""Codex protocol adapter over the provider-neutral Docker launcher seam."""
from __future__ import annotations

import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import threading

import codeagent_launcher as launcher
import docker_executor as backend
import host_budget


class DockerCodexProvider:
    parallel_authoring = True
    provider_id = "codex"
    task_provider = "openai-codex"
    cli_name = "codex"
    profile_id = "codex-docker-v1"
    runtime_package_id = "openai-codex-cli-docker"
    credential_delivery = "host-only-stdio-responses-relay"

    def __init__(self, api, cloud_agent, *, model):
        self.api, self.cloud_agent = api, cloud_agent
        self.model = api.require_model(model)
        self._bound_execution_identity = None
        self._task = None
        self._builder = None
        self._builder_limits = None
        self._cancelled = threading.Event()
        self._execution_root = None
        self._last_source_error = None
        self._last_source_error_code = None

    def request_cancel(self):
        self._cancelled.set()

    def recover(self, task, output_root):
        backend.recover_job(output_root / "executions" / task["execution_attempt"]["attempt_id"])

    def configure_builder(self, runner, limits_type):
        self._builder, self._builder_limits = runner, limits_type

    def configuration(self):
        observed = backend.image_identity()
        connection, _ = backend.codex_connection()
        return observed, connection

    def gateway(self):
        return backend.OpenAIGateway(self.model)

    def _preflight(self):
        try:
            observed, connection = self.configuration()
        except backend.DockerError as error:
            raise self.api.AdapterError(error.code, str(error)) from error
        public_config = {
            "image_id": observed["image_id"], "policy_sha256": observed["policy_sha256"],
            "bridge_sha256": observed["bridge_sha256"],
            "executor_sha256": backend.digest(Path(backend.__file__).read_bytes()),
            "launcher_sha256": backend.digest(Path(launcher.__file__).read_bytes()),
            "provider_adapter_sha256": backend.digest(Path(__file__).read_bytes()),
            "host_budget_sha256": backend.digest(Path(host_budget.__file__).read_bytes()),
            "maximum_host_authors": host_budget.AUTHOR_LIMIT,
            "maximum_host_compilers": 1,
            "model": self.model,
            "connection": connection,
        }
        if self.provider_id == "codex":
            # Disclose the same closed policy used by the relay; these values
            # enter the execution identity before a task can consume consent.
            public_config["model_request_budget"] = dict(backend.POLICY["model_request_budget"])
        executable = "docker://" + observed["image_id"] + "/usr/local/bin/" + self.cli_name
        result = self.api.provider_preflight(
            provider_id=self.provider_id, task_provider=self.task_provider, model=self.model,
            identity={"version": observed["version"], "sha256": observed["bundle_sha256"],
                      "configured_path": executable, "resolved_path": executable},
            authentication=connection["auth_kind"] if connection["auth_kind"] == "not-required-fixed-lan" else connection["auth_kind"] + "-configured", credential_delivery=self.credential_delivery,
            endpoint_kind=connection["kind"], canonical_endpoint=connection["endpoint"],
            runtime_package_id=self.runtime_package_id,
            non_secret_config_sha256=backend.digest(backend.canonical(public_config)),
        )
        result.update(available=True, execution_available=True, execution_blocker=None,
                      containment_backend="docker-whole-container", macos_sandbox="not-applicable-container",
                      one_job_per_host_user=False, maximum_jobs_per_host_user=host_budget.AUTHOR_LIMIT,
                      maximum_compilers_per_host_user=1, provider_tool_network="none",
                      docker_image_id=observed["image_id"], docker_policy=public_config)
        return result

    def preflight(self):
        result = self._preflight()
        self._bound_execution_identity = result["provider_execution_identity"]
        return result

    def assert_execution_identity(self, expected):
        if expected != self._bound_execution_identity or self._preflight()["provider_execution_identity"] != expected:
            raise self.api.AdapterError("provider-execution-identity-mismatch", "Docker image, executor or provider changed")

    def bind_task(self, task):
        self._task = task

    def execute(self, *, workspace, prompt, limits, temporary_root, cancel_file):
        self.assert_execution_identity(self._bound_execution_identity)
        if self._task is None:
            raise self.api.AdapterError("task-binding-required", "Docker execution requires the admitted task")
        observed = self._preflight()
        if self.provider_id == "codex":
            # The task-v3 provider_request remains unchanged. This host-authored
            # notice is bound by the policy/adapter identity and actual prompt
            # digest; the relay supplies current remaining counts on each call.
            prompt = prompt + b"\n\n" + backend.format_budget_notice().encode("utf-8") + b"\n"
        files = []
        for file in sorted(workspace.rglob("*")):
            if file.is_dir():
                continue
            metadata = file.lstat()
            if not stat.S_ISREG(metadata.st_mode) or file.is_symlink() or metadata.st_size > 1024 * 1024:
                raise self.api.AdapterError("workspace-invalid", "Docker input must contain bounded regular files")
            content = file.read_bytes()
            files.append({"path": file.relative_to(workspace).as_posix(), "base64": base64.b64encode(content).decode(), "sha256": backend.digest(content)})
        profile = launcher.ProviderProfile(
            provider_profile_id=self.profile_id, provider_profile_digest_sha256=self._bound_execution_identity["identity_sha256"],
            provider_id=self.provider_id, allowed_models=frozenset({self.model}), executor_kind=launcher.ExecutorKind.DOCKER,
            image_digest_sha256=observed["docker_image_id"].removeprefix("sha256:"),
            resource_policy_id="bounded-source-v1", resource_policy_digest_sha256=backend.digest(backend.canonical(limits)),
            network_policy_id="no-network-host-relay-v1", network_policy_digest_sha256=observed["docker_policy"]["policy_sha256"],
            max_output_bytes=4 * 1024 * 1024, output_media_type="application/vnd.vibapp.source-files+json")
        self._execution_root = temporary_root.parent / "executions" / self._task["execution_attempt"]["attempt_id"]
        try:
            gateway = self.gateway()
        except backend.DockerError as error:
            raise self.api.AdapterError(error.code, str(error)) from error
        executor = backend.DockerExecutor(image_id=observed["docker_image_id"],
            input_payload={"files": files, "prompt": prompt.decode(), "model": self.model}, gateway=gateway,
            limits=limits, state_root=self._execution_root,
            source_checker=self.check_source if self._builder is not None else None)
        service = launcher.LauncherService(profiles=[profile], executors=[executor])
        task = self._task
        request = launcher.LaunchRequest.from_mapping({
            "schema_version": launcher.SCHEMA_VERSION, "job_id": task["job_id"],
            "attempt_id": task["execution_attempt"]["attempt_id"], "idempotency_key": task["consent"]["consent_id"],
            "provider_profile_id": profile.provider_profile_id, "provider_id": self.provider_id, "model": self.model,
            "task_digest_sha256": task["immutable_task_digest_sha256"],
            "input_digest_sha256": backend.digest(backend.canonical(files)), "prompt_digest_sha256": backend.digest(prompt),
            "resource_policy_id": profile.resource_policy_id, "network_policy_id": profile.network_policy_id})
        state = service.submit(request)
        try:
            while state["state"] not in {"succeeded", "failed", "cancelled", "held"}:
                if self._cancelled.is_set() or self.api.cancellation_requested(cancel_file):
                    state = service.cancel(request.job_id, request.attempt_id)
                else:
                    state = service.status(request.job_id, request.attempt_id)
                if state["state"] == "cleanup-pending" and executor.status(state["backend_execution_id"]).state != launcher.BackendState.RUNNING:
                    raise self.api.AdapterError("docker-cleanup-unconfirmed", "Docker cleanup requires recovery; no source was accepted")
                time.sleep(.05)
            if state["state"] != "succeeded":
                raise self.api.AdapterError(self._terminal_failure_code(executor.failure_code), self._last_source_error or "Docker CodeAgent did not produce an accepted source result",
                                           provider_process_started=True, external_request_attempted=gateway.requests > 0,
                                           external_request_observed=gateway.observed,
                                           failure_diagnostic=executor.failure_diagnostic)
            execution_id = state["backend_execution_id"]
            success = executor.status(execution_id).success
            exported = json.loads(success.output)
            self.import_source(workspace, exported, limits)
            return self.api.ProviderExecution(self.provider_id, True, gateway.requests > 0, gateway.observed,
                execution_id if gateway.observed else None, observed["executable_path"], observed["executable_version"],
                observed["executable_sha256"], exported.get("output_bytes", 0), 0, asdict(success.receipt))
        finally:
            if state["state"] not in {"succeeded", "failed", "cancelled", "held"}:
                service.cancel(request.job_id, request.attempt_id)

    def _terminal_failure_code(self, code):
        # The container deliberately emits only source-check-failed. Preserve
        # trusted host queue/cancellation failures instead of blaming the model.
        if code == "source-check-failed":
            host_failure = {"local-capacity-busy": "local-capacity-busy", "cancelled": "provider-cancelled",
                            "timeout": "builder-timeout", "provider-timeout": "provider-timeout"}
            if self._last_source_error_code in host_failure:
                return host_failure[self._last_source_error_code]
        return code or "provider-failed"

    def check_source(self, exported, cancellation, remaining_seconds):
        """Same admitted job/deadline; compiler stays outside the agent container.

        A successful compiler probe is NOT independent verification or install.
        Builder/Verifier still consume and verify the final immutable handoff.
        """
        deadline = time.monotonic() + min(120, max(0, remaining_seconds))
        root = self._execution_root / f"source-check-{exported['id']}"
        root.mkdir(mode=0o700)
        try:
            contract = self.cloud_agent.prepare_workspace(root, self._task)
            self.import_source(root, exported, self._task["limits"])
            control = self.api.derive_provider_control_record(self.cloud_agent, root, self._task)
            self.cloud_agent.validate_provider_result(control, self._task, root / "source")
            self.cloud_agent.audit_workspace(root, self._task, control, contract)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self.api.AdapterError("provider-timeout", "source check exhausted the authoring deadline")
            self._builder.execute(root / "source", root / "compiler", self._builder_limits(
                wall_seconds=remaining, cpu_seconds=120,
            ), cancellation=cancellation)
            self._last_source_error = self._last_source_error_code = None
            feedback = {"ok": True}
        except Exception as error:
            code = getattr(error, "code", "source-check-failed")
            # Configuration, credentials, limits or integrity are not model-repairable.
            repairable = code in {"process-failed", "source-invalid", "provider-schema-invalid", "semantic-reminder-policy-invalid", "semantic-reminder-controls-missing", "semantic-ui-action-unreachable", "semantic-service-unimplemented"}
            diagnostic = str(error)[:12000].replace(str(Path.home()), "/host")
            self._last_source_error = code + ": " + diagnostic
            self._last_source_error_code = code
            feedback = {"ok": False, "repairable": repairable, "code": code, "diagnostic": diagnostic}
        # Failed source remains private and quarantined, available for diagnosis;
        # it is never inserted into My Apps or the public AppStore.
        (root / "check.json").write_bytes(backend.canonical(feedback))
        return feedback

    def import_source(self, workspace, exported, limits):
        # Passive guidance is not generated source. Count only the exact trusted
        # files already seeded by prepare_workspace, not a hard-coded allowance.
        contract_root = workspace / "contracts"
        if contract_root.is_symlink():
            raise self.api.AdapterError("provider-output-invalid", "symlink in trusted workspace")
        trusted_contracts = {"contracts/" + path.name for path in contract_root.iterdir()} if contract_root.is_dir() else set()
        if len(trusted_contracts) > 64:
            raise self.api.AdapterError("provider-output-invalid", "too many trusted inputs")
        if not isinstance(exported, dict) or not isinstance(exported.get("files"), list) or len(exported["files"]) > min(512, limits["source_files"] + len(trusted_contracts)):
            raise self.api.AdapterError("provider-output-invalid", "invalid source export")
        seen = set()
        validated = []
        total = 0
        for record in exported["files"]:
            if not isinstance(record, dict) or set(record) != {"path", "base64", "sha256"}:
                raise self.api.AdapterError("provider-output-invalid", "invalid file record")
            relative = record["path"]
            if not isinstance(relative, str) or len(relative) > 256 or "\0" in relative or "\\" in relative or any(part in ("", ".", "..") for part in relative.split("/")) or not relative.startswith(("source/", "contracts/")) or relative in seen:
                raise self.api.AdapterError("provider-output-invalid", "unsafe or repeated source path")
            seen.add(relative)
            if relative.startswith("contracts/") and relative not in trusted_contracts:
                raise self.api.AdapterError("provider-output-invalid", "unexpected contract input")
            if sum(path.startswith("source/") for path in seen) > limits["source_files"]:
                raise self.api.AdapterError("provider-output-invalid", "source export exceeds file count")
            try:
                content = base64.b64decode(record["base64"], validate=True)
            except (ValueError, TypeError) as error:
                raise self.api.AdapterError("provider-output-invalid", "invalid file encoding") from error
            total += len(content)
            if total > min(2 * 1024 * 1024, limits["workspace_bytes"]):
                raise self.api.AdapterError("provider-output-invalid", "source export exceeds aggregate limit")
            if len(content) > limits["source_bytes"] or backend.digest(content) != record["sha256"]:
                raise self.api.AdapterError("provider-output-invalid", "source digest mismatch")
            validated.append((relative, content))
        # Validate the complete snapshot before writing any generated file. The
        # agent cannot omit/change a scaffold and have the host silently restore it.
        for directory in (workspace / "source", workspace / "contracts"):
            if directory.is_symlink():
                raise self.api.AdapterError("provider-output-invalid", "symlink in trusted workspace")
            if directory.exists():
                for existing in directory.rglob("*"):
                    metadata = existing.lstat()
                    if stat.S_ISDIR(metadata.st_mode):
                        continue
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 1024 * 1024:
                        raise self.api.AdapterError("provider-output-invalid", "unsafe trusted input")
                    if existing.relative_to(workspace).as_posix() not in seen:
                        raise self.api.AdapterError("provider-output-invalid", "provider omitted a trusted input")
        for relative, content in validated:
            destination = workspace / relative
            for parent in destination.parents:
                if parent == workspace:
                    break
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise self.api.AdapterError("provider-output-invalid", "unsafe source parent")
            if destination.is_symlink():
                raise self.api.AdapterError("provider-output-invalid", "symlink source output")
            if destination.exists() and destination.read_bytes() != content:
                raise self.api.AdapterError("provider-output-invalid", "provider changed a trusted input")
        for relative, content in validated:
            destination = workspace / relative
            if not destination.exists():
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)


class DockerOpenCodeProvider(DockerCodexProvider):
    """Same admission, supervisor, export and compiler loop; fixed LAN model."""
    provider_id = task_provider = "opencode"
    cli_name = "opencode"
    profile_id = "opencode-docker-v1"
    runtime_package_id = "opencode-ai-cli-docker"
    credential_delivery = "host-only-stdio-chat-relay"

    def configuration(self):
        # Registry constructs every provider; reject only when selected so the
        # unrelated Codex/Claude/Gemini model registry continues to work.
        if self.model not in {backend.LOCALAI_MODEL, "vibapp/" + backend.LOCALAI_MODEL}:
            raise backend.DockerError("provider-model-unsupported")
        observed = backend.image_identity(backend.OPENCODE_IMAGE, provider="opencode")
        return observed, {"kind": "fixed-lan-http", "endpoint": backend.LOCALAI_ENDPOINT,
                          "auth_kind": "not-required-fixed-lan", "model": backend.LOCALAI_MODEL,
                          "context_tokens": backend.OPENCODE_POLICY["context_tokens"],
                          "max_output_tokens": backend.OPENCODE_POLICY["max_output_tokens"],
                          "relay_policy": backend.LOCALAI_RELAY_POLICY}

    def gateway(self):
        return backend.LocalAIGateway(self.model, evidence_root=self._execution_root)
