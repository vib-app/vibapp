#!/usr/bin/env python3
"""Diagnostic Qwen-copy -> compiler-path fixture; never a product CodeAgent."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
TEMPLATE = BASE / "guest_template.rs"
CONTRACT = REPO / "wit/experimental-v0/contract.wit"
COMPILE_SCRIPT = BASE / "compile_inside.sh"
CARGO_TOML = BASE / "Cargo.toml"
CARGO_LOCK = BASE / "Cargo.lock"
DOCKER = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
WASM_TOOLS = REPO / "generated/tool-layer-target/release/wasm-tools"
BUILDER_IMAGE = "sha256:ca24698517c86b3b433b6f62f2a02476048cc3b7251268d356a028b373e090dc"
MODEL_ENDPOINT = "http://192.168.199.170:8081"
MODEL_ID = "qwen3.8-27b-uncensored-mtp-q4"
POLICY_VERSION = "vibapp.diagnostic-compiler-fixture-policy.experimental-v1"
RECEIPT_VERSION = "vibapp.diagnostic-compiler-fixture-receipt.experimental-v1"
REQUIRED_IMPORTS = [
    "vibapp:experimental-v0/clock@0.0.1",
    "vibapp:experimental-v0/kv@0.0.1",
    "vibapp:experimental-v0/log@0.0.1",
    "vibapp:experimental-v0/host-info@0.0.1",
    "vibapp:experimental-v0/settings@0.0.1",
]
MAX_INPUT_BYTES = 256 * 1024
MAX_MODEL_BYTES = 64 * 1024
MAX_MODEL_WIRE_BYTES = 4 * 1024 * 1024
MAX_MODEL_EVENTS = 4096
MAX_SOURCE_BYTES = 32 * 1024
MAX_COMPONENT_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_CANDIDATES = 64


class ChainFailure(RuntimeError):
    pass


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChainFailure(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_strict_bytes(raw: bytes) -> Any:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ChainFailure("input envelope must be 1..262144 bytes")
    try:
        return json.loads(raw, object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChainFailure(f"invalid JSON envelope: {exc}") from exc


def safe_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ChainFailure(f"{name} must be a string")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ChainFailure(f"{name} is empty, oversized, or contains controls")
    return text


def valid_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_registry(registry: Any) -> dict[str, Any]:
    if not isinstance(registry, dict):
        raise ChainFailure("registry evidence must be an object")
    if registry.get("route") != "refinement" or registry.get("recommendations") != []:
        raise ChainFailure("local generation requires a truthful Registry no-match/refinement route")
    handoff = registry.get("codeagent_handoff")
    if not isinstance(handoff, dict) or handoff.get("created") is not False or handoff.get("permitted") is not False:
        raise ChainFailure("Registry must not claim CodeAgent handoff authority")
    request_id = safe_text(registry.get("request_id"), "registry.request_id", 128)
    reason = safe_text((registry.get("refinement") or {}).get("reason_code"), "registry.refinement.reason_code", 128)
    return {"request_id": request_id, "reason_code": reason, "route": "refinement"}


def validate_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise ChainFailure("task must be an object")
    if task.get("need_spec_complete") is not True or task.get("remote_processing_consent") is not True:
        raise ChainFailure("complete NeedSpec and existing consent-bound preview are required")
    if task.get("schema_version") != "vibapp.cloud-codeagent-task.experimental-v2":
        raise ChainFailure("unsupported task schema")
    job_id = safe_text(task.get("job_id"), "task.job_id", 128)
    task_digest = task.get("immutable_task_digest_sha256")
    if not valid_digest(task_digest):
        raise ChainFailure("task immutable digest is invalid")
    consent = task.get("consent")
    if not isinstance(consent, dict) or consent.get("decision") != "granted" or consent.get("single_use") is not True:
        raise ChainFailure("task's existing single-use consent binding is incomplete")
    if consent.get("job_id") != job_id or consent.get("payload_digest_sha256") != task_digest:
        raise ChainFailure("task and consent identity/digest binding mismatch")
    need = task.get("need_spec")
    if not isinstance(need, dict) or need.get("document_type") != "need-spec":
        raise ChainFailure("NeedSpec document is missing")
    need_digest = task.get("need_spec_digest_sha256")
    if not valid_digest(need_digest) or digest_json(need) != need_digest:
        raise ChainFailure("NeedSpec digest mismatch")
    if task.get("need_spec_current_revision") != need.get("revision"):
        raise ChainFailure("NeedSpec revision mismatch")
    target = task.get("target")
    if not isinstance(target, dict) or any([
        target.get("app_kind") != "ui",
        target.get("wit_world") != "ui-only-reference",
        target.get("wasi") != "0.2",
        target.get("rust_target") != "wasm32-wasip2",
        target.get("contract") != "vibapp:experimental-v0@0.0.1",
        target.get("required_imports") != REQUIRED_IMPORTS,
    ]):
        raise ChainFailure("thin slice only accepts the exact UI-only Stage 0 target/import ceiling")
    required_capabilities = target.get("required_capabilities")
    if (
        not isinstance(required_capabilities, list)
        or len(required_capabilities) > len(REQUIRED_IMPORTS)
        or any(
            not isinstance(interface, str) or interface not in REQUIRED_IMPORTS
            for interface in required_capabilities
        )
        or len(set(required_capabilities)) != len(required_capabilities)
    ):
        raise ChainFailure("task required capabilities must be a unique structural-import subset")
    ceiling = need.get("permission_ceiling")
    allowed = set(ceiling.get("allowed_interfaces", [])) if isinstance(ceiling, dict) else set()
    forbidden = set(ceiling.get("forbidden_interfaces", [])) if isinstance(ceiling, dict) else set()
    if set(required_capabilities) - allowed or set(required_capabilities) & forbidden:
        raise ChainFailure("task required capabilities exceed the NeedSpec permission ceiling")
    package = task.get("package_intent")
    if not isinstance(package, dict):
        raise ChainFailure("package intent is missing")
    app_id = safe_text(package.get("app_id"), "package_intent.app_id", 128)
    if re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+", app_id) is None:
        raise ChainFailure("package app_id is invalid")
    version = safe_text(package.get("version"), "package_intent.version", 32)
    if re.fullmatch(r"0\.[0-9]+\.[0-9]+", version) is None:
        raise ChainFailure("thin slice only accepts pre-1.0 simple semantic versions")
    display_name = safe_text(package.get("display_name"), "package_intent.display_name", 80)
    goal = safe_text(need.get("goal"), "need_spec.goal", 2000)
    return {
        "job_id": job_id,
        "task_digest": task_digest,
        "need_digest": need_digest,
        "need_id": safe_text(need.get("need_id"), "need_spec.need_id", 128),
        "goal": goal,
        "app_id": app_id,
        "version": version,
        "display_name": display_name,
        "task": task,
    }


def validate_envelope(value: Any) -> tuple[dict[str, Any], dict[str, Any], bool]:
    if not isinstance(value, dict):
        raise ChainFailure("envelope must be an object")
    if value.get("explicit_user_submit") is not True:
        raise ChainFailure("explicit user submit is required")
    if value.get("diagnostic_compiler_fixture") is not True:
        raise ChainFailure("diagnostic compiler fixture mode must be explicit")
    if value.get("diagnostic_lan_preprocessing_consent") is not True:
        raise ChainFailure("diagnostic LAN preprocessing consent is required")
    task = validate_task(value.get("task"))
    registry = validate_registry(value.get("registry"))
    return task, registry, bool(value.get("allow_deterministic_fallback", False))


def model_prompt(task: dict[str, Any]) -> str:
    goal = task["goal"]
    display = task["display_name"]
    return (
        "You are a bounded NeedSpec presentation-copy preprocessor for a diagnostic compiler fixture. "
        "Return one JSON object only, no markdown, no code, no extra keys: "
        '{"display_name":"1-40 chars","headline":"1-80 chars","body":"1-240 chars"}. '
        "You do not author code and this output is never a CodeAgent result. "
        "Do not mention hidden reasoning, credentials, networking, files, shell, or unsupported features. "
        f"NeedSpec goal: {goal}\nConfirmed diagnostic label: {display}"
    )


def parse_model_spec(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    value: Any = None
    decoder = json.JSONDecoder(object_pairs_hook=strict_object)
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and set(candidate) == {"display_name", "headline", "body"}:
            value = candidate
    if value is None:
        raise ChainFailure("model did not return the exact diagnostic copy JSON object")
    if not isinstance(value, dict) or set(value) != {"display_name", "headline", "body"}:
        raise ChainFailure("diagnostic copy has missing or extra fields")
    return {
        "display_name": safe_text(value["display_name"], "model.display_name", 40),
        "headline": safe_text(value["headline"], "model.headline", 80),
        "body": safe_text(value["body"], "model.body", 240),
    }


def stream_localai(prompt: str, endpoint: str = MODEL_ENDPOINT) -> tuple[dict[str, str], dict[str, Any]]:
    if endpoint != MODEL_ENDPOINT:
        raise ChainFailure("only the pinned LAN LocalAI endpoint is allowed")
    payload = canonical_bytes({
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 512,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    })
    connection = http.client.HTTPConnection("192.168.199.170", 8081, timeout=90)
    started = time.monotonic()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    chunks = 0
    total = 0
    try:
        connection.request("POST", "/v1/chat/completions", body=payload, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            detail = response.read(1024).decode("utf-8", "replace")
            raise ChainFailure(f"LocalAI returned HTTP {response.status}: {detail[:240]}")
        while True:
            line = response.readline(256 * 1024 + 1)
            if not line:
                break
            total += len(line)
            if total > MAX_MODEL_WIRE_BYTES:
                raise ChainFailure("LocalAI SSE wire stream exceeded 4 MiB")
            text = line.decode("utf-8", "strict").strip()
            if not text or text.startswith(":"):
                continue
            if not text.startswith("data:"):
                raise ChainFailure("LocalAI returned malformed SSE")
            data = text[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data, object_pairs_hook=strict_object)
            delta = ((event.get("choices") or [{}])[0].get("delta") or {})
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            if isinstance(delta.get("reasoning"), str):
                reasoning_parts.append(delta["reasoning"])
            chunks += 1
            if chunks > MAX_MODEL_EVENTS:
                raise ChainFailure("LocalAI SSE stream exceeded 4096 events")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChainFailure(f"LocalAI streaming failed: {exc}") from exc
    finally:
        connection.close()
    raw = "".join(content_parts) or "".join(reasoning_parts)
    if not raw:
        raise ChainFailure("LocalAI stream contained no usable diagnostic copy")
    if len(raw.encode("utf-8")) > MAX_MODEL_BYTES:
        raise ChainFailure("LocalAI diagnostic copy exceeded 64 KiB")
    spec = parse_model_spec(raw)
    return spec, {
        "provider": "localai-lan",
        "endpoint": MODEL_ENDPOINT,
        "model": MODEL_ID,
        "streaming": True,
        "stream_chunks": chunks,
        "response_bytes": len(raw.encode("utf-8")),
        "response_sha256": digest_bytes(raw.encode("utf-8")),
        "hidden_reasoning_persisted": False,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def fallback_spec(task: dict[str, Any]) -> dict[str, str]:
    return {
        "display_name": task["display_name"][:40],
        "headline": task["display_name"][:80],
        "body": "已从确认过的 NeedSpec 生成这个离线 UI 预览；当前仅作为本机私有候选。",
    }


def rust_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_source(task: dict[str, Any], spec: dict[str, str]) -> bytes:
    template = TEMPLATE.read_text("utf-8")
    replacements = {
        "__APP_ID__": rust_literal(task["app_id"]),
        "__APP_VERSION__": rust_literal(task["version"]),
        "__DISPLAY_NAME__": rust_literal(spec["display_name"]),
        "__HEADLINE__": rust_literal(spec["headline"]),
        "__BODY__": rust_literal(spec["body"]),
    }
    for token, replacement in replacements.items():
        if template.count(token) != 1:
            raise ChainFailure(f"trusted source template token invalid: {token}")
        template = template.replace(token, replacement)
    encoded = template.encode("utf-8")
    if not encoded or len(encoded) > MAX_SOURCE_BYTES:
        raise ChainFailure("rendered Rust source exceeded 32 KiB")
    if any(token.encode() in encoded for token in replacements):
        raise ChainFailure("rendered Rust source contains unresolved tokens")
    return encoded


def write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = canonical_bytes(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_new(temporary, encoded)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def run_bounded(command: list[str], timeout: int, label: str, env: dict[str, str] | None = None) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChainFailure(f"{label} exceeded {timeout} seconds and was killed") from exc
    if len(completed.stdout) > MAX_LOG_BYTES or len(completed.stderr) > MAX_LOG_BYTES:
        raise ChainFailure(f"{label} output exceeded 256 KiB")
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise ChainFailure(f"{label} failed: {(stderr or stdout)[-800:]}")
    return stdout, stderr


def compiler_preflight() -> None:
    for dependency in (DOCKER, WASM_TOOLS, CONTRACT, COMPILE_SCRIPT, CARGO_TOML, CARGO_LOCK):
        if not dependency.is_file() or dependency.is_symlink():
            raise ChainFailure(f"pinned compiler dependency unavailable: {dependency}")


def compile_component(source_dir: Path, output_dir: Path) -> tuple[bytes, dict[str, Any]]:
    compiler_preflight()
    source_dir.chmod(0o755)
    for path in source_dir.iterdir():
        path.chmod(0o644)
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    output_dir.chmod(0o777)
    command = [
        str(DOCKER), "run", "--pull=never", "--rm", "--network", "none",
        "--memory", "1g", "--memory-swap", "1g", "--cpus", "2", "--pids-limit", "64",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65532:65532", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=536870912,mode=1777",
        "-e", "HOME=/tmp/home", "-e", "CARGO_HOME=/tmp/cargo-home", "-e", "CARGO_NET_OFFLINE=true",
        "-e", "RUSTUP_AUTO_INSTALL=0", "-e", "CARGO_INCREMENTAL=0", "-e", "SOURCE_DATE_EPOCH=1786440135",
        "-e", "TZ=UTC", "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
        "-e", "RUSTFLAGS=--remap-path-prefix=/tmp/project=/workspace",
        "--mount", f"type=bind,source={source_dir},target=/input,readonly",
        "--mount", f"type=bind,source={output_dir},target=/output",
        "--mount", f"type=bind,source={CONTRACT},target=/contract/contract.wit,readonly",
        "--mount", f"type=bind,source={COMPILE_SCRIPT},target=/runner/compile_inside.sh,readonly",
        "--entrypoint", "/bin/sh", BUILDER_IMAGE, "/runner/compile_inside.sh",
    ]
    started = time.monotonic()
    stdout, stderr = run_bounded(command, 120, "experimental offline compiler", {"PATH": "/usr/local/bin:/usr/bin:/bin"})
    component_path = output_dir / "component.wasm"
    metadata = component_path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0 or metadata.st_size > MAX_COMPONENT_BYTES:
        raise ChainFailure("compiler output is not a bounded regular component")
    component = component_path.read_bytes()
    output_dir.chmod(0o700)
    component_path.chmod(0o600)
    return component, {
        "adapter": "post-stage0-experimental-local-compiler",
        "formal_builder_invoked": False,
        "formal_builder_accepted": False,
        "image_digest": BUILDER_IMAGE,
        "image_pull": False,
        "network": "none",
        "limits": {"memory_bytes": 1073741824, "cpus": 2, "pids": 64, "wall_time_seconds": 120},
        "stdout_sha256": digest_bytes(stdout.encode()),
        "stderr_sha256": digest_bytes(stderr.encode()),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def qa_component(component: bytes, path: Path) -> dict[str, Any]:
    digest = digest_bytes(component)
    if path.read_bytes() != component:
        raise ChainFailure("component changed between immutable digest snapshot and QA")
    run_bounded([str(WASM_TOOLS), "validate", str(path)], 20, "wasm-tools validate")
    wit, _ = run_bounded([str(WASM_TOOLS), "component", "wit", str(path)], 20, "wasm-tools component wit")
    for required in REQUIRED_IMPORTS:
        if f"import {required};" not in wit:
            raise ChainFailure(f"component WIT is missing required import {required}")
    if "export vibapp:experimental-v0/guest@0.0.1;" not in wit:
        raise ChainFailure("component WIT is missing guest export")
    forbidden = ["wasi:filesystem", "wasi:sockets", "wasi:cli", "vibapp:experimental-v0/http@"]
    if any(item in wit for item in forbidden):
        raise ChainFailure("component WIT contains forbidden ambient capability")
    return {
        "status": "experimental-qa-passed",
        "independent": False,
        "formal_verification_status": "unverified",
        "component_sha256": digest,
        "component_bytes": len(component),
        "wasm_tools_validate": True,
        "exact_runtime_contract": "vibapp:experimental-v0@0.0.1/ui-only-reference",
        "publication_authority": False,
        "install_authority": False,
        "launch_authority": "none-diagnostic-cli-inspection-only",
    }


def persist_diagnostic_artifact(candidate_root: Path, task: dict[str, Any], component: bytes, source_digest: str, qa: dict[str, Any], receipt_id: str) -> dict[str, Any]:
    component_digest = qa["component_sha256"]
    app_root = candidate_root / task["app_id"]
    version_root = app_root / component_digest
    version_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    component_path = version_root / "component.wasm"
    if component_path.exists():
        if component_path.read_bytes() != component:
            raise ChainFailure("existing private component digest path contains different bytes")
    else:
        write_new(component_path, component)
    relative = component_path.relative_to(candidate_root).as_posix()
    record = {
        "schema_version": "vibapp.private-candidate.experimental-v1",
        "app_id": task["app_id"],
        "display_name": task["display_name"],
        "version": task["version"],
        "kind": "ui",
        "summary": "仅用于验证离线编译与运行器通路的诊断 Component；不是 CodeAgent 产品产物。",
        "publisher": "VibApp diagnostic fixture",
        "profiles": ["desktop"],
        "permissions": ["clock", "kv", "log", "host-info", "settings"],
        "verification_state": "diagnostic-only",
        "publication_state": "diagnostic-only",
        "install_eligible": False,
        "launch_eligible": False,
        "launch_mode": "diagnostic-cli-only",
        "component_sha256": component_digest,
        "component_size_bytes": len(component),
        "component_relative_path": relative,
        "source_sha256": source_digest,
        "receipt_id": receipt_id,
        "public_publication_performed": False,
        "formal_builder_accepted": False,
        "verification_summary": "仅为编译/运行器诊断夹具；Desktop 不展示，不可启动、安装或发布。",
    }
    atomic_json(app_root / "candidate.json", record)
    return record


def mark_job_failed(root: Path, processing: Path, job_id: str, binding: dict[str, Any], stage: str, message: str) -> None:
    failed = {
        "schema_version": "vibapp.local-codeagent-failure.experimental-v1",
        "job_id": job_id,
        "binding": binding,
        "status": "failed-closed",
        "failure_stage": stage,
        "message": message[:500],
        "candidate_registered": False,
        "installation_performed": False,
        "public_publication_performed": False,
        "failed_at_utc": utc_now(),
    }
    atomic_json(root / "queue" / "failed" / f"{job_id}.json", failed)
    with contextlib.suppress(FileNotFoundError):
        processing.unlink()


def process_envelope(
    envelope: Any,
    root: Path,
    candidate_root: Path,
    model_client: Callable[[str], tuple[dict[str, str], dict[str, Any]]] = stream_localai,
) -> dict[str, Any]:
    task, registry, allow_fallback = validate_envelope(envelope)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = {
        "task_digest_sha256": task["task_digest"],
        "registry_request_id": registry["request_id"],
        "policy_version": POLICY_VERSION,
        "diagnostic_compiler_fixture": True,
    }
    job_digest = digest_json(binding)
    job_id = f"local-{job_digest[:24]}"
    receipt_path = root / "receipts" / f"{job_id}.json"
    failed_path = root / "queue" / "failed" / f"{job_id}.json"
    lock_path = root / ".queue.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt_path.exists():
            existing = load_strict_bytes(receipt_path.read_bytes())
            if existing.get("binding") != binding:
                raise ChainFailure("replay receipt binding mismatch")
            return existing
        if failed_path.exists():
            failure = load_strict_bytes(failed_path.read_bytes())
            raise ChainFailure(f"known job is failed closed at {failure.get('failure_stage', 'unknown-stage')}")
        ready = root / "queue" / "ready" / f"{job_id}.json"
        processing = root / "queue" / "processing" / f"{job_id}.json"
        done = root / "queue" / "done" / f"{job_id}.json"
        queue_record = {"job_id": job_id, "binding": binding, "status": "ready", "created_at_utc": utc_now()}
        atomic_json(ready, queue_record)
        processing.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(ready, processing)

    job_root = root / "jobs" / job_id
    source_dir = job_root / "source"
    source_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic_json(job_root / "input-envelope.json", envelope)
    model_failure: str | None = None
    try:
        compiler_preflight()
    except ChainFailure as exc:
        mark_job_failed(root, processing, job_id, binding, "compiler-preflight", str(exc))
        raise
    try:
        spec, model_observation = model_client(model_prompt(task))
        generation_mode = "localai-stream"
    except ChainFailure as exc:
        if not allow_fallback:
            mark_job_failed(root, processing, job_id, binding, "preflight-or-model", str(exc))
            raise
        model_failure = str(exc)[:500]
        spec = fallback_spec(task)
        model_observation = {
            "provider": "localai-lan",
            "endpoint": MODEL_ENDPOINT,
            "model": MODEL_ID,
            "streaming": True,
            "request_observed": False,
            "fallback_reason": model_failure,
            "hidden_reasoning_persisted": False,
        }
        generation_mode = "explicit-deterministic-fallback"
    try:
        source = render_source(task, spec)
        source_digest = digest_bytes(source)
        write_new(source_dir / "lib.rs", source)
        write_new(source_dir / "Cargo.toml", CARGO_TOML.read_bytes())
        write_new(source_dir / "Cargo.lock", CARGO_LOCK.read_bytes())
        component, compile_observation = compile_component(source_dir, job_root / "compiler-output")
        component_path = job_root / "compiler-output" / "component.wasm"
        qa = qa_component(component, component_path)
        receipt_id = f"receipt-{job_digest[:24]}"
        candidate = persist_diagnostic_artifact(candidate_root, task, component, source_digest, qa, receipt_id)
    except (ChainFailure, OSError, subprocess.SubprocessError) as exc:
        mark_job_failed(root, processing, job_id, binding, "source-compile-qa-register", str(exc))
        raise
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "status": "diagnostic-compiler-fixture-ready",
        "receipt_id": receipt_id,
        "job_id": job_id,
        "need_id": task["need_id"],
        "title": task["display_name"],
        "binding": binding,
        "registry": registry,
        "explicit_user_submit": True,
        "diagnostic_lan_preprocessing_consent": {"decision": "granted-single-use", "endpoint": MODEL_ENDPOINT, "task_digest_sha256": task["task_digest"]},
        "generation": {
            "mode": generation_mode,
            "source_shape": "diagnostic-trusted-rust-scaffold-plus-qwen-preprocessed-copy",
            "codeagent_output": False,
            "source_sha256": source_digest,
            "source_bytes": len(source),
            "model_observation": model_observation,
            "model_failure": model_failure,
        },
        "compiler": compile_observation,
        "qa": qa,
        "candidate": candidate,
        "authorities": {
            "formal_builder": "not-invoked",
            "formal_verifier": "not-invoked",
            "public_registry_publish": False,
            "installation": False,
            "private_preview_registration": False,
            "diagnostic_artifact_registration": True,
        },
        "completed_at_utc": utc_now(),
    }
    atomic_json(receipt_path, receipt)
    with lock_path.open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        finished = {"job_id": job_id, "binding": binding, "status": "diagnostic-compiler-fixture-ready", "receipt_id": receipt_id, "completed_at_utc": receipt["completed_at_utc"]}
        atomic_json(done, finished)
        with contextlib.suppress(FileNotFoundError):
            processing.unlink()
    return receipt


def candidate_state(candidate_root: Path) -> dict[str, Any]:
    apps: list[dict[str, Any]] = []
    if candidate_root.is_dir() and not candidate_root.is_symlink():
        for path in sorted(candidate_root.glob("*/candidate.json"))[:MAX_CANDIDATES]:
            try:
                if path.is_symlink() or path.stat().st_size > 128 * 1024:
                    continue
                record = load_strict_bytes(path.read_bytes())
                relative = record.get("component_relative_path")
                if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
                    continue
                component = candidate_root / relative
                if not component.is_file() or component.is_symlink():
                    continue
                data = component.read_bytes()
                if digest_bytes(data) != record.get("component_sha256") or len(data) != record.get("component_size_bytes"):
                    continue
                if record.get("publication_state") != "private" or record.get("install_eligible") is not False:
                    continue
                apps.append(record)
            except (OSError, ChainFailure):
                continue
    return {"schema_version": "vibapp.local-codeagent-state.experimental-v1", "apps": apps}


def emit(value: Any) -> None:
    encoded = canonical_bytes(value) + b"\n"
    if len(encoded) > MAX_INPUT_BYTES:
        raise ChainFailure("reply exceeded 256 KiB")
    sys.stdout.buffer.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("diagnostic-compile")
    run_parser.add_argument("--root", required=True)
    run_parser.add_argument("--candidate-root", required=True)
    state_parser = commands.add_parser("state")
    state_parser.add_argument("--candidate-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "state":
            emit({"ok": True, **candidate_state(Path(args.candidate_root).resolve())})
            return 0
        envelope = load_strict_bytes(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
        receipt = process_envelope(envelope, Path(args.root).resolve(), Path(args.candidate_root).resolve())
        emit({"ok": True, "receipt": receipt})
        return 0
    except (ChainFailure, OSError, subprocess.SubprocessError) as exc:
        emit({"ok": False, "code": "local-chain-failed", "message": str(exc)[:1000]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
