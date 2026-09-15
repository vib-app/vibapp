#!/usr/bin/env python3
"""Safe readiness and explicitly authorized live CodeAgent vertical.

The default ``readiness`` command never starts a provider authoring process, never
consumes consent, and never compiles guest source.  ``run-live`` is deliberately a
separate, noisy command with exact task-bound confirmations.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any


BASE = Path(__file__).resolve().parent
ARTIFACTS = BASE.parent
REPOSITORY = ARTIFACTS.parent
CLOUD_AGENT = ARTIFACTS / "cloud-agent/cloud_agent.py"
ADAPTER = ARTIFACTS / "codeagent-adapter/codeagent_adapter.py"
CONTROLLER = ARTIFACTS / "orchestrator/delivery_controller.py"
VALID_TASK = ARTIFACTS / "cloud-agent/fixtures/valid-task.json"
TOOL_LAYER = REPOSITORY / "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980"
CACHE_ROOT = REPOSITORY / "generated/builder-cargo-cache-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d"
DEFAULT_CARGO_HOME = CACHE_ROOT / "cargo-home"
DEFAULT_CACHE_ACCEPTANCE = CACHE_ROOT / "acceptance.json"
DEFAULT_WASM_TOOLS = TOOL_LAYER / "bin/wasm-tools"
DESKTOP_UI = ARTIFACTS / "desktop/ui"
WEB_GUI = ARTIFACTS / "product-platform/website/public/launcher"
LIVE_SENTINEL = "RUN-ONE-LIVE-CODEAGENT-TASK"
MAX_JSON_BYTES = 512 * 1024


class VerticalError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path, maximum: int = 128 * 1024 * 1024) -> str:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not 1 <= metadata.st_size <= maximum
    ):
        raise VerticalError("unsafe-file", f"not a bounded regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_module(name: str, path: Path) -> Any:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise VerticalError("dependency-unavailable", f"reviewed dependency is unavailable: {path}")
    specification = importlib.util.spec_from_file_location(name, resolved)
    if specification is None or specification.loader is None:
        raise VerticalError("dependency-unavailable", f"cannot load dependency: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def load_json(path: Path, context: str) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not 1 <= metadata.st_size <= MAX_JSON_BYTES
    ):
        raise VerticalError("unsafe-json", f"{context} is not a bounded regular file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerticalError("invalid-json", f"{context} is not valid JSON") from error
    if not isinstance(value, dict):
        raise VerticalError("invalid-json", f"{context} must be an object")
    return value


def fresh_readiness_task(cloud: Any, model: str) -> dict[str, Any]:
    task = load_json(VALID_TASK, "readiness fixture")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    task["job_id"] = "job-codeagent-readiness-no-request"
    task["provider"] = "openai-codex"
    task["model"] = model
    task["consent"]["consent_id"] = "consent-codeagent-readiness-no-request"
    task["consent"]["job_id"] = task["job_id"]
    task["consent"]["provider"] = task["provider"]
    task["consent"]["model"] = model
    task["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    task["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    task["need_spec_digest_sha256"] = cloud.sha256_bytes(cloud.canonical_json(task["need_spec"]))
    digest = cloud.immutable_task_digest(task, cloud.CONTRACT_DIGEST_PIN)
    task["immutable_task_digest_sha256"] = digest
    task["consent"]["payload_digest_sha256"] = digest
    return task


def authority_oracle(adapter: Any, cloud: Any, model: str) -> dict[str, Any]:
    class ProbeProvider:
        provider_id = "codex"
        task_provider = "openai-codex"

        def __init__(self) -> None:
            self.model = model
            self.preflight_calls = 0
            self.execute_calls = 0

        def preflight(self) -> dict[str, Any]:
            self.preflight_calls += 1
            raise AssertionError("authority oracle must not reach provider preflight")

        def execute(self, **_: Any) -> Any:
            self.execute_calls += 1
            raise AssertionError("authority oracle must not reach provider execution")

    task = fresh_readiness_task(cloud, model)
    results: list[dict[str, str]] = []
    adapter_results: list[dict[str, str]] = []
    provider_calls = 0
    with tempfile.TemporaryDirectory(prefix="vibapp-codeagent-authority-") as directory:
        root = Path(directory)
        task_path = root / "task.json"
        task_path.write_text(canonical(task) + "\n", encoding="utf-8")
        valid = argparse.Namespace(
            execute_authorized_live_task=LIVE_SENTINEL,
            confirm_job=task["job_id"],
            confirm_consent=task["consent"]["consent_id"],
            confirm_task_provider=task["provider"],
            confirm_model=task["model"],
            acknowledge_external_cost=True,
            explicit_user_submit=True,
        )
        wrapper_cases = (
            ("sentinel", "execute_authorized_live_task", "wrong-sentinel"),
            ("job", "confirm_job", "wrong-job"),
            ("consent", "confirm_consent", "wrong-consent"),
            ("task-provider", "confirm_task_provider", "wrong-provider"),
            ("model", "confirm_model", "wrong-model"),
            ("external-cost", "acknowledge_external_cost", False),
            ("explicit-submit", "explicit_user_submit", False),
        )
        for label, field, value in wrapper_cases:
            arguments = argparse.Namespace(**vars(valid))
            setattr(arguments, field, value)
            try:
                require_live_bindings(arguments, task)
            except VerticalError as error:
                if error.code != "live-authorization-required":
                    raise VerticalError(
                        "authority-oracle-failed", f"{label} returned {error.code}"
                    ) from error
            else:
                raise VerticalError("authority-oracle-failed", f"{label} did not fail closed")
            results.append({"case": label, "status": "rejected-before-provider"})

        adapter_cases = (
            ("job-mismatch", "wrong-job", task["consent"]["consent_id"], True, "external-opt-in-required"),
            ("consent-mismatch", task["job_id"], "wrong-consent", True, "external-opt-in-required"),
            ("cost-not-acknowledged", task["job_id"], task["consent"]["consent_id"], False, "external-opt-in-required"),
        )
        for label, job, consent, cost, expected in adapter_cases:
            provider = ProbeProvider()
            output = root / f"output-{label}"
            status = root / f"status-{label}.json"
            try:
                adapter.execute_task(
                    cloud,
                    provider,
                    task_path,
                    output,
                    status,
                    confirm_job=job,
                    confirm_consent=consent,
                    acknowledge_external_cost=cost,
                )
            except adapter.AdapterError as error:
                if error.code != expected:
                    raise VerticalError("authority-oracle-failed", f"{label} returned {error.code}") from error
            else:
                raise VerticalError("authority-oracle-failed", f"{label} did not fail closed")
            if status.exists() or output.exists() or provider.preflight_calls or provider.execute_calls:
                raise VerticalError("authority-oracle-failed", f"{label} mutated state or reached provider")
            provider_calls += provider.preflight_calls + provider.execute_calls
            adapter_results.append({"case": label, "status": "rejected-before-provider"})
        durable_mutations = [path.name for path in root.iterdir() if path.name != "task.json"]
        if durable_mutations:
            raise VerticalError("authority-oracle-failed", "authority oracle left durable mutations")
    return {
        "status": "pass",
        "cases": results,
        "adapter_guards": adapter_results,
        "provider_calls": provider_calls,
        "consent_consumed": False,
        "durable_mutations": 0,
    }


def auth_status(provider: Any) -> str:
    if hasattr(provider, "_preflight") and not hasattr(provider, "_identity"):
        preflight = provider.preflight()
        if preflight.get("execution_available") is not True:
            raise VerticalError("codex-auth-unavailable", "Docker CodeAgent preflight is not ready")
        return preflight["docker_policy"]["connection"]["auth_kind"]
    identity = provider._identity()  # metadata-only identity already covered by provider preflight
    try:
        result = subprocess.run(
            [identity["resolved_path"], "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            env={"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise VerticalError("codex-auth-status-failed", "Codex login status could not be checked locally") from error
    combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")[:4096]
    if result.returncode != 0:
        raise VerticalError("codex-auth-unavailable", "Codex reports no usable local login")
    if "Logged in using ChatGPT" in combined:
        return "chatgpt"
    if "Logged in using" in combined:
        return "present-other"
    return "present-unclassified"


def verifier_preflight(path: Path, accepted_path: Path, accepted_sha256: str) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    accepted = accepted_path.resolve(strict=True)
    digest = sha256_file(path, 256 * 1024 * 1024)
    metadata = path.lstat()
    if not metadata.st_mode & 0o111:
        raise VerticalError("verifier-unavailable", "wasm-tools is not executable")
    if resolved != accepted or digest != accepted_sha256:
        raise VerticalError(
            "verifier-identity-mismatch",
            "wasm-tools does not match the exact independently accepted path and digest",
        )
    return {
        "status": "ready",
        "authority": "independent-verifier",
        "path": str(resolved),
        "sha256": digest,
        "accepted_path": str(accepted),
        "accepted_sha256": accepted_sha256,
        "process_started": False,
    }


def shared_gui_preflight() -> dict[str, Any]:
    manifest = load_json(WEB_GUI / "gui-sync-manifest.json", "GUI sync manifest")
    checks: dict[str, Any] = {}
    for name in ("app.js", "styles.css", "favicon.svg"):
        source = sha256_file(DESKTOP_UI / name, 16 * 1024 * 1024)
        generated = sha256_file(WEB_GUI / name, 16 * 1024 * 1024)
        row = manifest.get("files", {}).get(name, {})
        if source != generated or row != {
            "source_sha256": source,
            "generated_sha256": generated,
            "exact_match": True,
        }:
            raise VerticalError("shared-gui-drift", f"Web {name} differs from Desktop GUI source")
        checks[name] = {"sha256": source, "exact_match": True}
    source_index = sha256_file(DESKTOP_UI / "index.html", 4 * 1024 * 1024)
    generated_index = sha256_file(WEB_GUI / "index.html", 4 * 1024 * 1024)
    index = manifest.get("files", {}).get("index.html", {})
    if (
        index.get("source_sha256") != source_index
        or index.get("generated_sha256") != generated_index
        or index.get("exact_match") is not False
        or index.get("bounded_adapter_transform") is not True
        or manifest.get("adapter", {}).get("index_only") is not True
    ):
        raise VerticalError("shared-gui-drift", "Web index is not the recorded bounded adapter transform")
    return {
        "status": "ready",
        "source_of_truth": str(DESKTOP_UI.relative_to(REPOSITORY)),
        "web_derivation": str(WEB_GUI.relative_to(REPOSITORY)),
        "exact_shared_files": checks,
        "index_adapter_only": True,
        "cache_version": manifest["adapter"]["cache_version"],
    }


def run_readiness(arguments: argparse.Namespace) -> dict[str, Any]:
    delivery = load_module("vibapp_readiness_delivery", CONTROLLER)
    stage = delivery.CodeAgentStage(
        adapter_path=ADAPTER,
        cloud_agent_path=CLOUD_AGENT,
        provider_id=arguments.provider,
        model=arguments.model,
        acknowledge_external_cost=False,
    )
    provider = stage.provider
    provider_report = provider.preflight()
    if arguments.provider != "codex" or not hasattr(provider, "command_preview"):
        raise VerticalError("provider-not-supported", "the Round 005 readiness oracle currently requires Codex")
    with tempfile.TemporaryDirectory(prefix="vibapp-codex-command-preview-") as directory:
        command = provider.command_preview(Path(directory))
    required = {
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        "--json",
        "workspace-write",
        'shell_environment_policy.inherit="none"',
    }
    if not required.issubset(set(command)) or command.count("--model") != 1:
        raise VerticalError("command-hardening-missing", "Codex argv is missing a required isolation flag")
    if command[command.index("--model") + 1] != arguments.model:
        raise VerticalError("model-binding-failed", "Codex argv model differs from readiness selection")
    runner = delivery.MacSandboxCargoRunner(
        arguments.tool_layer, arguments.cargo_home, arguments.cache_acceptance
    )
    builder = runner.preflight()
    authority = authority_oracle(stage.adapter, stage.cloud, arguments.model)
    return {
        "schema_version": "vibapp.codeagent-vertical-readiness.experimental-v1",
        "status": "pass",
        "mode": "no-request-readiness",
        "provider": {
            **provider_report,
            "model": arguments.model,
            "auth_status": auth_status(provider),
            "command": command,
            "provider_process_started": False,
            "external_request_attempted": False,
        },
        "authority": authority,
        "builder": builder,
        "verifier": verifier_preflight(
            arguments.wasm_tools, delivery.DEFAULT_WASM_TOOLS, delivery.WASM_TOOLS_SHA256
        ),
        "shared_gui": shared_gui_preflight(),
        "chain": {
            "ready_for_one_explicit_live_task": True,
            "stages": ["registry-no-match", "codeagent", "isolated-builder", "independent-verifier", "private-appstore"],
            "live_task_executed": False,
            "publication_performed": False,
        },
        "next_action": {
            "requires_separate_user_authorization": True,
            "command": "run-live",
            "sentinel": LIVE_SENTINEL,
            "confirmations": [
                "sentinel", "job", "consent", "task-provider", "model", "external-cost", "explicit-submit"
            ],
        },
    }


def require_live_bindings(arguments: argparse.Namespace, task: dict[str, Any]) -> None:
    expected = {
        "sentinel": LIVE_SENTINEL,
        "job": task.get("job_id"),
        "consent": task.get("consent", {}).get("consent_id"),
        "provider": task.get("provider"),
        "model": task.get("model"),
    }
    actual = {
        "sentinel": arguments.execute_authorized_live_task,
        "job": arguments.confirm_job,
        "consent": arguments.confirm_consent,
        "provider": arguments.confirm_task_provider,
        "model": arguments.confirm_model,
    }
    if actual != expected or not arguments.acknowledge_external_cost or not arguments.explicit_user_submit:
        raise VerticalError(
            "live-authorization-required",
            "live execution requires exact task/job/consent/provider/model, cost, and submit confirmations",
        )


def require_fresh_live_attempt(admitted: dict[str, Any], terminal_statuses: set[str]) -> None:
    if admitted.get("status") in terminal_statuses:
        raise VerticalError(
            "live-task-already-terminal",
            "run-live cannot claim a prior terminal attempt as a new provider invocation; use status/revalidation",
        )


def assert_live_complete(
    controller: Any,
    root: Path,
    attempt: dict[str, Any],
    task: dict[str, Any],
    provider_id: str,
) -> dict[str, Any]:
    attempt = controller.revalidate_attempt(attempt["task_id"], attempt["attempt_id"])
    outputs = attempt.get("outputs", {})
    if (
        attempt.get("status") != "private-appstore-ready"
        or attempt.get("stage") != "private-appstore-ready"
        or attempt.get("progress_percent") != 100
        or outputs.get("external_request_attempted") is not True
        or outputs.get("digest_equality_proven") is not True
        or outputs.get("publication_performed") is not False
    ):
        raise VerticalError("live-chain-incomplete", "delivery did not reach digest-proven private AppStore readiness")
    attempt_root = root / "tasks" / attempt["task_id"] / "attempts" / attempt["attempt_id"]
    status = load_json(attempt_root / "codeagent/status.json", "CodeAgent status")
    if (
        status.get("status") != "source-ready"
        or status.get("job_id") != task.get("job_id")
        or status.get("need_id") != task.get("need_spec", {}).get("need_id")
        or status.get("need_spec_revision") != task.get("need_spec_current_revision")
        or status.get("immutable_task_digest_sha256") != task.get("immutable_task_digest_sha256")
        or status.get("consent_id") != task.get("consent", {}).get("consent_id")
        or status.get("provider_id") != provider_id
        or status.get("task_provider") != task.get("provider")
        or status.get("model") != task.get("model")
        or status.get("consent_consumed") is not True
        or status.get("provider_process_started") is not True
        or status.get("external_request_attempted") is not True
        or status.get("builder_invoked") is not False
    ):
        raise VerticalError("live-codeagent-evidence-invalid", "CodeAgent status is not a real source-only provider completion")
    handoff_path = root / outputs["source_handoff_path"]
    handoff = load_json(handoff_path, "CodeAgent handoff")
    if handoff.get("provider_execution", {}).get("mode") != "direct-local-explicit-opt-in":
        raise VerticalError("live-codeagent-evidence-invalid", "handoff is not bound to direct explicit provider execution")
    execution = handoff.get("provider_execution", {})
    if (
        execution.get("adapter") != "local-codeagent-adapter"
        or execution.get("provider_id") != provider_id
        or execution.get("external_request_attempted") is not True
        or execution.get("external_request_observed") is not False
        or execution.get("gateway_request_id") is not None
        or not isinstance(execution.get("executable_sha256"), str)
        or len(execution["executable_sha256"]) != 64
    ):
        raise VerticalError(
            "live-codeagent-evidence-invalid",
            "handoff provider execution does not match the explicit local CodeAgent invocation",
        )
    for field, context in (
        ("builder_receipt_path", "Builder receipt"),
        ("candidate_path", "Verifier candidate"),
    ):
        load_json(root / outputs[field], context)
    return {
        "provider_process_started": True,
        "external_request_attempted": True,
        "external_request_observed": status.get("external_request_observed"),
        "digest_equality_proven": True,
        "publication_performed": False,
        "visibility": "private",
    }


def run_live(arguments: argparse.Namespace) -> dict[str, Any]:
    delivery = load_module("vibapp_live_delivery", CONTROLLER)
    cloud = load_module("vibapp_live_cloud", CLOUD_AGENT)
    task = cloud.load_json(arguments.task.resolve(strict=True), cloud.MAX_TASK_BYTES, "CodeAgent task")
    registry = load_json(arguments.registry_no_match.resolve(strict=True), "Registry no-match evidence")
    require_live_bindings(arguments, task)
    delivery.DeliveryController._validate_registry_no_match(registry)
    runner = delivery.MacSandboxCargoRunner(
        arguments.tool_layer, arguments.cargo_home, arguments.cache_acceptance
    )
    builder = runner.preflight()
    verifier = verifier_preflight(
        arguments.wasm_tools, delivery.DEFAULT_WASM_TOOLS, delivery.WASM_TOOLS_SHA256
    )
    stage = delivery.CodeAgentStage(
        adapter_path=ADAPTER,
        cloud_agent_path=CLOUD_AGENT,
        provider_id=arguments.provider,
        model=arguments.model,
        acknowledge_external_cost=True,
    )
    if stage.provider.task_provider != task["provider"] or arguments.model != task["model"]:
        raise VerticalError("live-binding-mismatch", "adapter provider/model differs from the immutable task")
    stage.provider.preflight()
    root = arguments.root.resolve(strict=False)
    controller = delivery.DeliveryController(
        root,
        stage,
        runner,
        wasm_tools=arguments.wasm_tools,
        appstore_root=arguments.appstore_root,
    )
    admitted = controller.submit(
        task,
        registry,
        explicit_user_submit=True,
        retry_task_id=arguments.retry_task_id,
    )
    require_fresh_live_attempt(admitted, delivery.TERMINAL_STATUSES)
    attempt = controller.run_attempt(admitted["task_id"], admitted["attempt_id"])
    proof = assert_live_complete(controller, root, attempt, task, arguments.provider)
    return {
        "schema_version": "vibapp.codeagent-live-vertical-result.experimental-v1",
        "status": "pass",
        "mode": "one-explicitly-authorized-live-task",
        "task_id": attempt["task_id"],
        "attempt_id": attempt["attempt_id"],
        "provider": arguments.provider,
        "task_provider": task["provider"],
        "model": arguments.model,
        "builder": builder,
        "verifier": verifier,
        "proof": proof,
        "attempt": attempt,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="VibApp CodeAgent full vertical")
    commands = root.add_subparsers(dest="command", required=True)
    readiness = commands.add_parser("readiness", help="no request, no consent use, no guest compile")
    readiness.add_argument("--provider", default="codex")
    readiness.add_argument("--model", required=True)
    for command in (readiness,):
        command.add_argument("--tool-layer", type=Path, default=TOOL_LAYER)
        command.add_argument("--cargo-home", type=Path, default=DEFAULT_CARGO_HOME)
        command.add_argument("--cache-acceptance", type=Path, default=DEFAULT_CACHE_ACCEPTANCE)
        command.add_argument("--wasm-tools", type=Path, default=DEFAULT_WASM_TOOLS)
    live = commands.add_parser("run-live", help="spends quota; exact one-time task authority required")
    live.add_argument("--task", type=Path, required=True)
    live.add_argument("--registry-no-match", type=Path, required=True)
    live.add_argument("--root", type=Path, required=True)
    live.add_argument("--appstore-root", type=Path)
    live.add_argument("--retry-task-id")
    live.add_argument("--provider", required=True)
    live.add_argument("--model", required=True)
    live.add_argument("--confirm-job", required=True)
    live.add_argument("--confirm-consent", required=True)
    live.add_argument("--confirm-task-provider", required=True)
    live.add_argument("--confirm-model", required=True)
    live.add_argument("--execute-authorized-live-task", required=True)
    live.add_argument("--acknowledge-external-cost", action="store_true")
    live.add_argument("--explicit-user-submit", action="store_true")
    live.add_argument("--tool-layer", type=Path, default=TOOL_LAYER)
    live.add_argument("--cargo-home", type=Path, default=DEFAULT_CARGO_HOME)
    live.add_argument("--cache-acceptance", type=Path, default=DEFAULT_CACHE_ACCEPTANCE)
    live.add_argument("--wasm-tools", type=Path, default=DEFAULT_WASM_TOOLS)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result = run_readiness(arguments) if arguments.command == "readiness" else run_live(arguments)
    except Exception as error:
        code = getattr(error, "code", "internal")
        print(canonical({"ok": False, "code": code, "message": str(error)[:4096]}), file=sys.stderr)
        return 1
    print(canonical({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
