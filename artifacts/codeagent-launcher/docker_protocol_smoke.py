#!/usr/bin/env python3
"""Explicit paid-provider editing probe, NOT a VibApp application acceptance."""
import argparse
import base64
import json
from pathlib import Path
import tempfile
import time

import codeagent_launcher as launcher
import docker_executor as backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--execute-authorized-provider-probe", action="store_true")
    args = parser.parse_args()
    if not args.execute_authorized_provider_probe:
        parser.error("explicit provider-probe and external quota authorization required")
    parent = Path(__file__).resolve().parent / "output"
    parent.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="editing-probe-", dir=parent))
    print(f"PROBE_ROOT {root}", flush=True)
    observed = backend.image_identity()
    files = [{"path": "contracts/diagnostic.txt", "base64": base64.b64encode(b"Diagnostic only; no app acceptance.").decode(), "sha256": backend.digest(b"Diagnostic only; no app acceptance.")}]
    prompt = 'Use your actual file editing tool to create /work/source/src/lib.rs containing exactly: pub const EDITING_PROBE: bool = true; Then use a tool to read it back. Do not just return code in your answer. Do not compile or access network. The directory /work/source is writable. This is a tool protocol diagnostic, not an application.'
    limits = {"memory_bytes": 2 * 1024**3, "pids": 64, "wall_time_seconds": 120, "cpu_seconds": 60, "workspace_bytes": 64 * 1024**2}
    profile = launcher.ProviderProfile(
        provider_profile_id="codex-editing-probe", provider_profile_digest_sha256=backend.digest(backend.canonical(observed)),
        provider_id="codex", allowed_models=frozenset({args.model}), executor_kind=launcher.ExecutorKind.DOCKER,
        image_digest_sha256=observed["image_id"][7:], resource_policy_id="editing-probe", resource_policy_digest_sha256=backend.digest(backend.canonical(limits)),
        network_policy_id="host-only-relay", network_policy_digest_sha256=observed["policy_sha256"], max_output_bytes=4 * 1024**2,
        output_media_type="application/vnd.vibapp.source-files+json")
    gateway = backend.OpenAIGateway(args.model)
    executor = backend.DockerExecutor(image_id=observed["image_id"], input_payload={"files": files, "prompt": prompt, "model": args.model}, gateway=gateway, limits=limits, state_root=root)
    service = launcher.LauncherService(profiles=[profile], executors=[executor])
    request = launcher.LaunchRequest.from_mapping({"schema_version": launcher.SCHEMA_VERSION,
        "job_id": "editing-probe", "attempt_id": root.name, "idempotency_key": root.name,
        "provider_profile_id": profile.provider_profile_id, "provider_id": "codex", "model": args.model,
        "task_digest_sha256": backend.digest(prompt.encode()), "input_digest_sha256": backend.digest(backend.canonical(files)),
        "prompt_digest_sha256": backend.digest(prompt.encode()), "resource_policy_id": profile.resource_policy_id, "network_policy_id": profile.network_policy_id})
    state = service.submit(request)
    while state["state"] not in {"succeeded", "failed", "cancelled", "held"}:
        state = service.status(request.job_id, request.attempt_id)
        time.sleep(.1)
    success = executor.status(state["backend_execution_id"]).success
    if success:
        (root / "source-result.json").write_bytes(success.output)
    physical_source = bool(success and any(file["path"] == "source/src/lib.rs" and base64.b64decode(file["base64"]).strip() == b"pub const EDITING_PROBE: bool = true;" for file in json.loads(success.output)["files"]))
    print(f"RESULT {state['state']} {executor.failure_code} requests={gateway.requests} cleanup={executor.status(state['backend_execution_id']).cleanup_confirmed}", flush=True)
    print(f"PHYSICAL_SOURCE {physical_source}", flush=True)
    return 0 if physical_source else 1


if __name__ == "__main__":
    raise SystemExit(main())
