#!/usr/bin/env python3
"""Corrected thin slice: real Qwen NeedSpec preprocessing to durable CodeAgent queue."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
ANALYZER = REPO / "artifacts/need-analyzer/need_analyzer.py"
REGISTRY = REPO / "artifacts/registry/registry_service.py"
CLOUD_AGENT = REPO / "artifacts/cloud-agent/cloud_agent.py"
ORCHESTRATOR_PATH = REPO / "artifacts/orchestrator/orchestrator.py"
PROFILE_TEMPLATE = REPO / "artifacts/provider-runner/deployment/accepted-profile.template.json"
REQUIRED_IMPORTS = [
    "vibapp:experimental-v0/clock@0.0.1",
    "vibapp:experimental-v0/kv@0.0.1",
    "vibapp:experimental-v0/log@0.0.1",
    "vibapp:experimental-v0/host-info@0.0.1",
    "vibapp:experimental-v0/settings@0.0.1",
]
CONTRACT_DIGEST = "ec94f06652886cf9eea0aba8afc028165b135b91103faccf62a2ccc6828d3c38"
DISCLOSED_CLASSES = [
    "need",
    "package-intent",
    "target-contract",
    "execution-limits",
    "generation-policy",
    "authoritative-contract",
]


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical(value) + b"\n")


def run_json(command: list[str], stdin: object | None, timeout: int) -> dict:
    completed = subprocess.run(
        command,
        input=None if stdin is None else canonical(stdin),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): "
            + completed.stderr.decode("utf-8", "replace")[:500]
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("command did not return a JSON object")
    return value


def load_orchestrator():
    spec = importlib.util.spec_from_file_location("corrected_e2e_orchestrator", ORCHESTRATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cloud_agent():
    spec = importlib.util.spec_from_file_location("corrected_e2e_cloud_agent", CLOUD_AGENT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    evidence = BASE / "evidence" / f"corrected-queue-{stamp}"
    evidence.mkdir(parents=True, mode=0o700)

    raw_need = {
        "title": "折纸配色助手",
        "description": "我需要一个完全离线的桌面界面，按纸张颜色给出折纸配色提示，并保存最近选择。",
    }
    analysis = run_json([sys.executable, str(ANALYZER), "analyze"], raw_need, 70)
    if analysis.get("status") != "analyzed" or analysis.get("provider") != "localai-lan":
        raise RuntimeError("Qwen NeedSpec preprocessing did not return a real analyzed result")
    write_json(evidence / "qwen-needspec-preprocessing.json", analysis)

    goal = str(analysis.get("goal_summary") or raw_need["description"])
    acceptance = str(analysis.get("acceptance_example") or "启动后可选择纸张颜色并看到离线配色提示。")
    need = {
        "schema_version": "vibapp.need-spec.product-v0.0.1",
        "document_type": "need-spec",
        "need_id": f"need.synthetic.paper-colors.{stamp.lower()}",
        "owner": {"principal_id": "desktop.local.synthetic-user", "principal_kind": "user"},
        "goal": goal,
        "requirements": [{
            "requirement_id": "requirement.primary",
            "text": goal,
            "priority": "must-have",
            "acceptance_examples": [acceptance],
        }],
        "negative_constraints": [{
            "constraint_id": "constraint.no-network",
            "kind": "forbidden-capability",
            "source": "user",
            "value": "vibapp:experimental-v0/http@0.0.1",
        }],
        "platforms": [{"os": "macos", "arch": "aarch64", "profile": "desktop"}],
        "profiles": ["desktop"],
        "permission_ceiling": {
            "allowed_interfaces": REQUIRED_IMPORTS,
            "forbidden_interfaces": ["vibapp:experimental-v0/http@0.0.1"],
            "maximum_scope_digests": [],
        },
        "privacy_requirement": "remote-private",
        "created_at_utc": now.isoformat().replace("+00:00", "Z"),
        "revision": 1,
    }
    write_json(evidence / "need-spec.json", need)

    registry = run_json([
        sys.executable,
        str(REGISTRY),
        "search",
        "--need",
        str(evidence / "need-spec.json"),
        "--kind",
        "ui",
        "--required-interface",
        "vibapp:experimental-v0/kv@0.0.1",
        "--required-interface",
        "vibapp:experimental-v0/settings@0.0.1",
        "--embedding-consent",
    ], None, 30)
    if (
        registry.get("route") != "refinement"
        or registry.get("recommendations") != []
        or registry.get("codeagent_handoff", {}).get("created") is not False
    ):
        raise RuntimeError("synthetic need did not produce a truthful Registry no-match")
    write_json(evidence / "registry-no-match.json", registry)

    package = {
        "app_id": "ai.vibapp.private.papercolors",
        "version": "0.1.0",
        "display_name": "折纸配色助手",
        "description": goal,
        "entrypoints": [{
            "id": "main",
            "kind": "launcher-ui",
            "label": "折纸配色助手",
            "initial_route": "home",
        }],
    }
    target = {
        "contract": "vibapp:experimental-v0@0.0.1",
        "wasi": "0.2",
        "rust_target": "wasm32-wasip2",
        "wit_world": "ui-only-reference",
        "app_kind": "ui",
        "profiles": ["desktop"],
        "required_imports": REQUIRED_IMPORTS,
        "required_capabilities": [
            "vibapp:experimental-v0/kv@0.0.1",
            "vibapp:experimental-v0/settings@0.0.1",
        ],
    }
    limits = {
        "wall_time_seconds": 300,
        "cpu_seconds": 240,
        "memory_bytes": 2147483648,
        "pids": 16,
        "workspace_bytes": 16777216,
        "stdout_bytes": 262144,
        "stderr_bytes": 262144,
        "source_bytes": 8388608,
        "source_files": 128,
    }
    provider = "openai-codex"
    model = "gpt-5.6-sol"
    seed = {
        "need_spec_digest_sha256": digest(need),
        "package_intent": package,
        "target": target,
        "provider": provider,
        "model": model,
    }
    job_id = f"job-cloud-{digest(seed)[:24]}"
    cloud_agent = load_cloud_agent()
    instructions_digest = cloud_agent.provider_instructions_digest(provider)
    task = {
        "schema_version": cloud_agent.TASK_SCHEMA_VERSION,
        "document_type": "cloud-codeagent-task",
        "job_id": job_id,
        "need_spec_complete": True,
        "need_spec_current_revision": 1,
        "need_spec_digest_sha256": digest(need),
        "immutable_task_digest_sha256": "0" * 64,
        "need_spec": need,
        "package_intent": package,
        "remote_processing_consent": True,
        "consent": {
            "consent_id": "consent-cloud-pending",
            "consent_type": "remote-processing",
            "decision": "granted",
            "subject": need["owner"],
            "job_id": job_id,
            "provider": provider,
            "model": model,
            "payload_digest_sha256": "0" * 64,
            "uploaded_data_classes": DISCLOSED_CLASSES,
            "single_use": True,
            "issued_at_utc": (now - dt.timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "expires_at_utc": (now + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "policy_version": cloud_agent.POLICY_VERSION,
            "contract_digest_sha256": CONTRACT_DIGEST,
            "instructions_digest_sha256": instructions_digest,
        },
        "provider": provider,
        "model": model,
        "target": target,
        "limits": limits,
    }
    immutable_digest = cloud_agent.immutable_task_digest(task, CONTRACT_DIGEST)
    task["immutable_task_digest_sha256"] = immutable_digest
    task["consent"]["payload_digest_sha256"] = immutable_digest
    task["consent"]["consent_id"] = f"consent-cloud-{immutable_digest[:24]}"
    write_json(evidence / "explicit-consented-codeagent-task.json", task)

    orchestrator = load_orchestrator()
    submitted = orchestrator.submit(
        evidence / "codeagent-queue",
        CLOUD_AGENT,
        canonical(task),
        explicit_submit=True,
    )
    receipt = submitted["receipt"]
    if (
        receipt.get("status") != "waiting-for-external-runner"
        or receipt.get("external_request_attempted") is not False
        or receipt.get("external_request_observed") is not False
    ):
        raise RuntimeError("durable queue receipt made an invalid execution claim")
    write_json(evidence / "durable-codeagent-queue-receipt.json", receipt)

    profile = json.loads(PROFILE_TEMPLATE.read_text("utf-8"))
    blocker = {
        "cloud_agent_default_runner": "UnavailableProviderRunner",
        "provider_runner_profile_status": profile.get("status"),
        "provider_runner_identity": profile.get("runner_id"),
        "production_backend_configured": False,
        "receipt_signer_configured": False,
        "gateway_verifier_configured": False,
        "credential_broker_configured": False,
        "real_codeagent_callable": False,
    }
    write_json(evidence / "external-codeagent-blocker.json", blocker)

    summary = {
        "status": "BLOCKED-at-durable-codeagent-queue",
        "evidence_root": str(evidence.relative_to(REPO)),
        "qwen_role": "needspec-preprocessing-only",
        "qwen_preprocessing_observed": True,
        "qwen_model": analysis["model"],
        "registry_route": "refinement",
        "registry_recommendation_count": 0,
        "explicit_remote_consent": True,
        "queue_status": receipt["status"],
        "queued_task_sha256": receipt["canonical_task_sha256"],
        "external_request_attempted": False,
        "external_request_observed": False,
        "codeagent_source_returned": False,
        "builder_invoked": False,
        "private_candidate_created": False,
        "desktop_launch_performed": False,
        "blocker": blocker,
        "papercolors_prior_component_role": "diagnostic-compiler-path-fixture-only",
    }
    write_json(evidence / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
