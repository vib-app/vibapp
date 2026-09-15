#!/usr/bin/env python3
"""Safe synthetic NeedSpec-to-daemon vertical smoke with no external provider/network use."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path


ARTIFACTS = Path(__file__).resolve().parents[1]
REPO = ARTIFACTS.parent
CODEAGENT = ARTIFACTS / "codeagent-adapter"
CLOUD = ARTIFACTS / "cloud-agent"
BUILDER = ARTIFACTS / "app-builder"
sys.path.insert(0, str(BUILDER))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


adapter = load_module("vertical_codeagent_adapter", CODEAGENT / "codeagent_adapter.py")
cloud_agent = adapter.load_cloud_agent(CLOUD / "cloud_agent.py")
from app_builder import SafeFixtureRunner, build_handoff  # noqa: E402
from common import sha256_file  # noqa: E402
from end_to_end_smoke import run as run_candidate  # noqa: E402
from verifier import verify_and_promote  # noqa: E402


COMPONENT = ARTIFACTS / "desktop/runtime-apps/hello/component.wasm"
EXPECTED_COMPONENT_SHA256 = "d3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa"


class LocalStaticProvider:
    """Test double: simulates one provider attempt without starting a CLI or network call."""

    provider_id = "codex"
    task_provider = "openai-codex"
    model = "gpt-5.6-sol"

    @staticmethod
    def preflight():
        return {"available": True, "mode": "local-static-fixture", "external": False}

    @staticmethod
    def execute(*, workspace, prompt, limits, temporary_root, cancel_file):
        del prompt, limits, temporary_root, cancel_file
        source = CLOUD / "fixtures/dry-run/source"
        shutil.copy2(source / "Cargo.toml", workspace / "source/Cargo.toml")
        (workspace / "source/src").mkdir(mode=0o700)
        shutil.copy2(source / "src/lib.rs", workspace / "source/src/lib.rs")
        shutil.copy2(
            CLOUD / "fixtures/dry-run/provider-result.json",
            workspace / "provider-last-message.json",
        )
        return adapter.ProviderExecution(
            provider_id="codex",
            provider_process_started=True,
            external_request_attempted=True,
            external_request_observed=False,
            gateway_request_id=None,
            executable_sha256="a" * 64,
            stdout_bytes=0,
            stderr_bytes=0,
        )


def main() -> int:
    if sha256_file(COMPONENT, 16 * 1024 * 1024, "safe fixture component") != EXPECTED_COMPONENT_SHA256:
        raise RuntimeError("safe fixture Component changed; review and repin it")
    with tempfile.TemporaryDirectory(prefix="vibapp-synthetic-vertical-") as directory:
        root = Path(directory)
        task = json.loads((CLOUD / "fixtures/valid-task.json").read_text(encoding="utf-8"))
        if task["model"] != LocalStaticProvider.model or task["consent"]["model"] != LocalStaticProvider.model:
            raise RuntimeError("synthetic provider model must exactly match task and consent")
        # The safe Builder runner substitutes the independently pinned Hello
        # Component instead of compiling untrusted source. Keep the synthetic
        # task's package identity exactly aligned with those executable bytes;
        # the smoke remains explicit that it proves wiring, not source
        # compilation or requirement satisfaction.
        task["package_intent"] = {
            "app_id": "ai.vibapp.hello",
            "version": "0.1.0",
            "display_name": "Hello VibApp",
            "description": "Identity-aligned inert Component for the bounded synthetic delivery smoke.",
            "entrypoints": [
                {
                    "id": "main",
                    "kind": "launcher-ui",
                    "label": "Hello VibApp",
                    "initial_route": "home",
                }
            ],
        }
        task_digest = cloud_agent.immutable_task_digest(
            task,
            cloud_agent.CONTRACT_DIGEST_PIN,
        )
        task["immutable_task_digest_sha256"] = task_digest
        task["consent"]["payload_digest_sha256"] = task_digest
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        task["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task_path = root / "task.json"
        task_path.write_text(json.dumps(task, sort_keys=True), encoding="utf-8")
        output = root / "codeagent-output"
        status = adapter.execute_task(
            cloud_agent,
            LocalStaticProvider(),
            task_path,
            output,
            root / "codeagent-status.json",
            confirm_job=task["job_id"],
            confirm_consent=task["consent"]["consent_id"],
            acknowledge_external_cost=True,
        )
        handoff = output / status["handoff_relative_path"]
        receipt = build_handoff(
            handoff,
            root / "builder-output",
            SafeFixtureRunner(COMPONENT.read_bytes()),
        )
        candidate = verify_and_promote(receipt, root / "builder-output")
        downstream = run_candidate(candidate)
        result = {
            "schema_version": "vibapp.synthetic-vertical-result.experimental-v1",
            "status": "PASS",
            "need_spec_complete": task["need_spec_complete"],
            "exact_consent_consumed": status["consent_consumed"],
            "provider_mode": "synthetic-simulated-attempt-no-network",
            "external_request_attempted": status["external_request_attempted"],
            "external_request_observed": status["external_request_observed"],
            "external_provider_request_performed": False,
            "source_handoff_created": handoff.is_file(),
            "builder_runner": "safe-fixture-no-source-execution",
            "codeagent_source_compilation_proven": False,
            "candidate_digest_sha256": downstream["package_digest_sha256"],
            "private_appstore_ingest": downstream["appstore_private"],
            "daemon_lifecycle_smoke": downstream["status"],
            "guest_execution_performed_by_daemon": downstream["guest_execution_performed"],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
