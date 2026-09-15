#!/usr/bin/env python3
"""Host-only orchestration for fresh, consented repair versions.

No old source is injected into an immutable task. The existing protocol has no
seed-source field; compatibility is an explicit requirement, not an upgrade claim.
This helper never installs, enables, updates, or edits a generated application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from everyday_apps_workflow import (CODE_MODEL, RECEPTION_MODEL, REPOSITORY,
                                   bridge, confirm_settings, write_json)

CATALOG = Path(__file__).with_name("repair_existing_app_requirements.json")
INVENTORY = REPOSITORY / "artifacts/product-integration/output/round2-existing-app-audit-20260909-1340/inventory.json"


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Unsafe or oversized repair evidence")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=pairs)


def requirements(key):
    catalog = read_json(CATALOG)
    rows = [row for row in catalog["apps"] if row["key"] == key]
    if len(rows) != 1 or catalog["automatic_user_upgrade"] is not False:
        raise ValueError("Unreviewed repair target")
    app = rows[0]
    if app["app_kind"] != "ui" or app["version"] != "0.1.1":
        raise ValueError("Only the two reviewed UI repair versions are ready")
    if not 1 <= len(app["description"]) <= 1000 or not 1 <= len(app["acceptance"]) <= 1000:
        raise ValueError("Repair requirements exceed the real Task schema limits")
    if app["state_contract"]["key"] not in app["description"]:
        raise ValueError("Persistent key must reach the actual author requirements")
    return app


def validate_task(app, completed):
    cloud = completed["cloud_development"]
    if cloud["required_conditions"].get("authoritative_registry_no_match") is not True:
        raise ValueError("Registry did not authorize development; no override or fabricated rejection")
    if cloud["task_preparation"].get("submission_available") is not True:
        raise ValueError("Fresh admission unavailable")
    task = cloud["task_preparation"]["schema_preview"]
    intent = task["package_intent"]
    if (intent["app_id"], intent["version"], task["target"]["app_kind"]) != (app["app_id"], "0.1.1", "ui"):
        raise ValueError("Repair identity changed during requirement analysis")
    if task["provider"] != "openai-codex" or task["model"] != CODE_MODEL:
        raise ValueError("Unapproved CodeAgent/model")
    if task["provider_execution_identity"]["runtime"]["package_id"] != "openai-codex-cli-docker":
        raise ValueError("Repair author must use the consent-bound Docker Codex runtime")
    if task["limits"]["memory_bytes"] > 2 * 1024 ** 3:
        raise ValueError("Author memory exceeds approved 2 GiB bound")
    expected_entry = [{"id": "launcher.main", "kind": "launcher-ui", "label": app["name"], "initial_route": "home"}]
    if intent["entrypoints"] != expected_entry:
        raise ValueError("Original entrypoint identity changed")
    primary = [r for r in task["need_spec"]["requirements"] if r["requirement_id"] == "requirement.primary"]
    if len(primary) != 1 or primary[0]["text"] != app["description"] or app["acceptance"] not in primary[0]["acceptance_examples"]:
        raise ValueError("Original compatibility requirements were lost in model preprocessing")
    expected_caps = {f"vibapp:experimental-v0/{cap}@0.0.1" for cap in app["capabilities"]}
    if set(task["target"]["required_capabilities"]) != expected_caps:
        raise ValueError("Actual requirements/capabilities differ from reviewed repair")
    return task


def baseline(app):
    inventory = read_json(INVENTORY)
    matches = [row for catalog in inventory["catalogs"] for row in catalog["apps"]
               if row["app_id"] == app["app_id"] and row["package_digest_sha256"] == app["baseline_digest"]]
    if len(matches) != 1 or matches[0]["version"] != "0.1.0" or matches[0]["app_kind"] != app["app_kind"]:
        raise ValueError("Frozen baseline identity unavailable")
    row = matches[0]
    candidate_path = Path(row["source_candidate"])
    read_json(candidate_path)
    if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != row["candidate_sha256"]:
        raise ValueError("Original candidate changed since failed audit")
    return row


def execute(output_root, product_root, key):
    app = requirements(key)
    original = baseline(app)
    # One directory per attempt; an existing directory is never silently retried.
    directory = output_root / key
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_json(directory / "requirements.json", app)
    write_json(directory / "baseline.json", original)
    write_json(directory / "worker.json", {"pid": os.getpid(), "phase": "preflight", "app_id": app["app_id"], "automatic_user_upgrade": False})
    try:
        write_json(directory / "bridge-health.json", confirm_settings(product_root))
        submitted = bridge(product_root, "submit_need", {"title": app["name"], "description": app["description"], "embedding_consent": True})
        write_json(directory / "submit-need.json", submitted)
        analysis = submitted["need_spec"]["analysis"]
        if analysis.get("status") != "analyzed" or analysis.get("model") != RECEPTION_MODEL:
            raise ValueError("Real receptionist analysis incomplete; retained without fake completion")
        confirm_settings(product_root)
        completed = bridge(product_root, "complete_need", {
            "need_id": submitted["need"]["need_id"], "app_kind": app["app_kind"], "capabilities": app["capabilities"],
            "allowed_permissions": ["clock", "kv", "log", "host-info", "settings"],
            "forbidden_permissions": ["http", "notification", "scheduler"],
            "permission_ceiling_confirmed": True, "negative_constraints": "不得联网或读取宿主文件\n不得创建后台服务和通知\n不得清空、改名或改编码旧持久状态\n不得把静态首屏或伪保存作为完整功能",
            "negative_constraints_confirmed": True, "network_mode": "offline",
            "package_id": app["app_id"], "package_name": app["name"], "package_version": app["version"],
            "acceptance_example": app["acceptance"], "proceed_after_recommendation": False,
            "registry_embedding_consent": False, "remote_processing_consent": True, "public_publication_consent": False,
        })
        write_json(directory / "complete-need.json", completed)
        task = validate_task(app, completed)
        write_json(directory / "task.json", task)
        write_json(directory / "registry.json", completed["registry"])
        run_delivery(directory, product_root, task, completed["registry"])
    except Exception as error:
        write_json(directory / "failure.json", {"type": type(error).__name__, "message": str(error)[:2000], "automatic_retry": False})
        raise


def run_delivery(directory, product_root, task, registry):
    sys.path.insert(0, str(REPOSITORY / "artifacts/orchestrator"))
    from delivery_controller import CodeAgentStage, DeliveryController, DeliveryError
    from app_builder import MacSandboxCargoRunner
    from codeagent_vertical import TOOL_LAYER, DEFAULT_CARGO_HOME, DEFAULT_CACHE_ACCEPTANCE
    stage = CodeAgentStage(provider_id="codex", model=CODE_MODEL, acknowledge_external_cost=True)
    runner = MacSandboxCargoRunner(TOOL_LAYER, DEFAULT_CARGO_HOME, DEFAULT_CACHE_ACCEPTANCE)
    controller = DeliveryController(product_root / "delivery-controller", stage, runner, appstore_root=product_root / "local-appstore")
    admitted = controller.submit(task, registry, explicit_user_submit=True)
    write_json(directory / "admission.json", admitted)
    task_id = admitted["task_id"]
    write_json(directory / "worker.json", {"pid": os.getpid(), "phase": "admitted", "task_id": task_id, "job_id": task["job_id"]})
    deadline = time.monotonic() + 1800
    while True:
        try:
            controller.run_attempt(task_id, task["execution_attempt"]["attempt_id"])
            break
        except DeliveryError as error:
            status = controller.status(task_id)
            if error.code != "local-capacity-busy" or status.get("attempt", {}).get("status") != "queued" or time.monotonic() >= deadline:
                raise
            time.sleep(2)
    status = controller.status(task_id)
    write_json(directory / "delivery-status.json", status)
    terminal = status.get("attempt", {}).get("status")
    write_json(directory / "worker.json", {"pid": os.getpid(), "phase": terminal, "task_id": task_id, "job_id": task["job_id"]})
    print(json.dumps({"app_id": task["package_intent"]["app_id"], "task_id": task_id, "status": terminal}), flush=True)
    if terminal != "private-appstore-ready":
        raise RuntimeError("Real repair delivery failed; original failure retained, no automatic new author")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "execute"))
    parser.add_argument("--app", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--product-root", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(requirements(args.app), ensure_ascii=False, indent=2))
        return
    if not args.root or not args.product_root:
        parser.error("execute requires --root and --product-root")
    execute(args.root.resolve(), args.product_root.resolve(strict=True), args.app)


if __name__ == "__main__":
    main()
