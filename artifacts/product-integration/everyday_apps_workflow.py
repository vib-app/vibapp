#!/usr/bin/env python3
"""Approved everyday-app batch using real product admission and Docker Codex.

This is host orchestration, never an app-source generator. Existing product
settings and host credentials are reused without copying or changing them.
Run workers as separate recorded processes; the shared controller owns capacity.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from retry_failed_samples import current_desktop_build_inputs_sha256

REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE = REPOSITORY / "target/release/vibapp-product-bridge"
CATALOG = Path(__file__).with_name("everyday_app_scenarios.json")
CODE_MODEL = "gpt-5.6-sol"
RECEPTION_MODEL = "qwen3.8-27b-uncensored-mtp-q4"


def write_json(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def app_requirements(key):
    catalog = json.loads(CATALOG.read_bytes())
    matches = [app for app in catalog["apps"] if app["key"] == key]
    if len(matches) != 1:
        raise ValueError("Unknown or duplicate application key")
    app = matches[0]
    if app["generation_ready"] is not True:
        raise ValueError(app["blocker"] or "Required host capability unavailable")
    return app


def bridge(product_root, command, arguments, *, timeout=120):
    request = json.dumps({"command": command, "args": arguments}, ensure_ascii=False).encode()
    result = subprocess.run([str(BRIDGE), str(product_root)], input=request,
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"Bridge {command} failed: {result.stderr.decode(errors='replace')[:700]}")
    value = json.loads(result.stdout)
    if value.get("ok") is not True:
        raise RuntimeError(f"Bridge {command} rejected: {value.get('error')}")
    return value["result"]


def confirm_settings(product_root):
    health = bridge(product_root, "health", {})
    if health["build_input_receipt"]["sha256"] != current_desktop_build_inputs_sha256():
        raise RuntimeError("Product bridge is stale; rebuild before fresh admission")
    settings = bridge(product_root, "get_codeagent_settings", {})
    if settings["selectedProvider"] != "codex" or settings["modelByProvider"].get("codex") != CODE_MODEL:
        raise RuntimeError("Existing selected CodeAgent is not the approved Codex/model; settings unchanged")
    models = bridge(product_root, "get_model_settings", {})
    generation = models["generation"]
    if generation.get("model") != RECEPTION_MODEL or generation.get("enabled") is not True:
        raise RuntimeError("Existing receptionist is not the approved Qwen; settings unchanged")
    return health


def prepare(root, product_root, key):
    app = app_requirements(key)
    health = confirm_settings(product_root)
    directory = root / key
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_json(directory / "requirements.json", app)
    write_json(directory / "bridge-health.json", health)
    submitted = bridge(product_root, "submit_need", {
        "title": app["name"],
        "description": app["description"] + f"建议独立窗口初始大小约{app['preferred_window']['width']}×{app['preferred_window']['height']}，内容随窗口响应式布局。",
        "embedding_consent": True,
    })
    write_json(directory / "submit-need.json", submitted)
    analysis = submitted["need_spec"]["analysis"]
    if analysis.get("status") != "analyzed" or analysis.get("model") != RECEPTION_MODEL:
        raise RuntimeError("Real requirement analysis incomplete; inspect retained result")
    print(json.dumps({"step": "requirement-analyzed", "app": key,
                      "need_id": submitted["need"]["need_id"]}), flush=True)


def complete(root, product_root, key):
    app = app_requirements(key)
    confirm_settings(product_root)
    directory = root / key
    submitted = json.loads((directory / "submit-need.json").read_bytes())
    # A fresh UI confirmation can supersede an unused, expired task. Retain the
    # exact old consent instead of losing its audit record; never overwrite an
    # already admitted delivery through this requirement-preparation helper.
    if (directory / "admission.json").exists():
        raise RuntimeError("Existing delivery is admitted; use its explicit recovery workflow")
    if (directory / "task.json").exists():
        previous = json.loads((directory / "task.json").read_bytes())
        write_json(directory / "task-history" / f"previous-{time.time_ns()}.json", previous)
    completed = bridge(product_root, "complete_need", {
        "need_id": submitted["need"]["need_id"], "app_kind": "ui", "capabilities": app["capabilities"],
        "allowed_permissions": ["clock", "kv", "log", "host-info", "settings"],
        "forbidden_permissions": ["http", "notification", "scheduler"],
        "permission_ceiling_confirmed": True,
        "negative_constraints": "不得联网或读取宿主文件\n不得创建后台服务和通知\n不得以假交互或静态示例代替功能",
        "negative_constraints_confirmed": True, "network_mode": "offline",
        "package_id": f"ai.vibapp.everyday.{key}", "package_name": app["name"], "package_version": "0.1.0",
        "acceptance_example": app["acceptance"], "proceed_after_recommendation": False,
        "registry_embedding_consent": False, "remote_processing_consent": True,
        "public_publication_consent": False,
    })
    write_json(directory / "complete-need.json", completed)
    cloud = completed["cloud_development"]
    task = cloud["task_preparation"].get("schema_preview")
    if not task or task["provider"] != "openai-codex" or task["model"] != CODE_MODEL:
        raise RuntimeError("Fresh approved Codex task unavailable")
    if cloud["required_conditions"]["authoritative_registry_no_match"] is not True or cloud["task_preparation"]["submission_available"] is not True:
        raise RuntimeError("Registry/admission did not authorize new development")
    write_json(directory / "task.json", task)
    write_json(directory / "registry.json", completed["registry"])
    print(json.dumps({"step": "task-confirmed", "app": key, "job_id": task["job_id"]}), flush=True)


def run(root, product_root, key):
    app_requirements(key)
    sys.path.insert(0, str(REPOSITORY / "artifacts/orchestrator"))
    from delivery_controller import CodeAgentStage, DeliveryController, DeliveryError
    from app_builder import MacSandboxCargoRunner
    from codeagent_vertical import TOOL_LAYER, DEFAULT_CARGO_HOME, DEFAULT_CACHE_ACCEPTANCE
    directory = root / key
    task = json.loads((directory / "task.json").read_bytes())
    if task["provider"] != "openai-codex" or task["model"] != CODE_MODEL:
        raise RuntimeError("Task provider/model differs from the approved batch")
    registry = json.loads((directory / "registry.json").read_bytes())
    runner = MacSandboxCargoRunner(TOOL_LAYER, DEFAULT_CARGO_HOME, DEFAULT_CACHE_ACCEPTANCE)
    stage = CodeAgentStage(provider_id="codex", model=CODE_MODEL, acknowledge_external_cost=True)
    controller = DeliveryController(product_root / "delivery-controller", stage, runner,
                                    appstore_root=product_root / "local-appstore")
    # submit is idempotent for the same exact task/consent; never manufactures a
    # replacement execution after an uncertain provider outcome.
    admitted = controller.submit(task, registry, explicit_user_submit=True)
    write_json(directory / "admission.json", admitted)
    task_id = admitted["task_id"]
    print(json.dumps({"step": "admitted", "app": key, "task_id": task_id}), flush=True)
    queue_deadline = time.monotonic() + 1800
    while True:
        try:
            controller.run_attempt(task_id, task["execution_attempt"]["attempt_id"])
            break
        except DeliveryError as error:
            status = controller.status(task_id)
            if error.code != "local-capacity-busy" or status.get("attempt", {}).get("status") != "queued" or time.monotonic() >= queue_deadline:
                raise
            write_json(directory / "worker-status.json", {"status": "queued", "task_id": task_id, "reason": "local-capacity-busy"})
            time.sleep(2)
    status = controller.status(task_id)
    write_json(directory / "delivery-status.json", status)
    terminal = status.get("attempt", {}).get("status")
    write_json(directory / "worker-status.json", {"status": terminal, "task_id": task_id})
    print(json.dumps({"step": "delivery-terminal", "app": key, "task_id": task_id, "status": terminal}), flush=True)
    if terminal != "private-appstore-ready":
        raise RuntimeError("Real delivery failed; retained status has the precise stage and cause")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "complete", "run"))
    parser.add_argument("--root", type=Path, required=True, help="Private batch evidence directory")
    parser.add_argument("--product-root", type=Path, required=True, help="Existing user product data directory")
    parser.add_argument("--app", required=True)
    arguments = parser.parse_args()
    globals()[arguments.command](arguments.root.resolve(), arguments.product_root.resolve(strict=True), arguments.app)


if __name__ == "__main__":
    main()
