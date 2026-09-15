#!/usr/bin/env python3
"""Run a small, sequential, real-CodeAgent retry sample through the product bridge."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE = REPOSITORY / "artifacts/desktop/src-tauri/target/release/vibapp-product-bridge"
DATA_DIR = Path.home() / "Library/Application Support/ai.vibapp.launcher"
OUTPUT_ROOT = REPOSITORY / "artifacts/product-integration/output"
DESKTOP_INPUTS = REPOSITORY / "artifacts/desktop/scripts/desktop-build-inputs.mjs"
EXPECTED_BRIDGE_CONTRACTS = {
    "cloud_codeagent_task": "vibapp.cloud-codeagent-task.experimental-v3",
    "cloud_codeagent_task_schema_sha256": "fb2a49186349aa4542ab6bab9440fe2e5c48a9d7cc86085449a71e20ecdb73ab",
    "codeagent_adapter": "vibapp.codeagent-adapter.experimental-v1",
    "codeagent_status": "vibapp.codeagent-adapter-status.experimental-v2",
    "codex_compatibility_policy": "vibapp.codex-cli-compatibility.experimental-v1",
    "delivery_worker": "vibapp.desktop-delivery-worker.experimental-v2",
    "provider_execution_identity": "vibapp.provider-execution-identity.experimental-v1",
    "source_handoff": "vibapp.codeagent-source-handoff.experimental-v2",
}
TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "delivery-worker-failed",
    "builder-not-configured",
}
MODEL = "gpt-5.6-sol"


SAMPLES = [
    {
        "key": "simple-ui",
        "old_failure": "local Codex exited nonzero (1); stdout=887",
        "title": "CodeAgent 简单界面重试",
        "description": "做一个有界面的验收应用，只显示“CodeAgent 重试成功”、应用标题和当前日期；不读取用户文件，不访问网络。",
        "app_kind": "ui",
        "capabilities": ["clock", "kv", "settings"],
        "allowed_permissions": ["clock", "kv", "log", "host-info", "settings"],
        "forbidden_permissions": ["http"],
        "negative_constraints": "不得读取用户文件\n不得访问网络\n只显示验收要求中的必要内容",
        "package_id": "ai.vibapp.retry-proof-20260827",
        "package_name": "CodeAgent 重试验收",
        "acceptance_example": "启动后能看到应用标题、当前日期和“CodeAgent 重试成功”；运行期间没有网络请求或用户文件访问。",
    },
    {
        "key": "lunar-calendar",
        "old_failure": "local Codex exited nonzero (1); stdout=1021",
        "title": "农历日历重试",
        "description": "做一个离线农历日历，显示公历日期、农历日期和节气；可以按月切换，不访问网络，不读取用户文件。",
        "app_kind": "ui",
        "capabilities": ["clock", "kv", "settings"],
        "allowed_permissions": ["clock", "kv", "log", "host-info", "settings"],
        "forbidden_permissions": ["http"],
        "negative_constraints": "不得访问网络\n不得读取用户文件\n农历换算必须在本地完成",
        "package_id": "ai.vibapp.lunar-calendar-retry-20260827",
        "package_name": "离线农历日历重试",
        "acceptance_example": "启动后显示当前公历和农历日期，可切换月份并查看节气；整个运行过程无网络请求或用户文件访问。",
    },
    {
        "key": "alarm-clock",
        "old_failure": "local Codex exited nonzero (-25); stdout=0",
        "title": "时钟闹钟重试",
        "description": "做一个离线时钟和闹钟应用，可选择时区、创建多个闹钟；闹钟由宿主调度，即使关闭界面仍由 VibApp daemon 管理，不访问网络。",
        "app_kind": "hybrid",
        "capabilities": ["clock", "scheduler", "notification", "kv", "settings"],
        "allowed_permissions": [
            "clock",
            "scheduler",
            "notification",
            "kv",
            "log",
            "host-info",
            "settings",
        ],
        "forbidden_permissions": ["http"],
        "negative_constraints": "不得使用界面定时器代替宿主调度\n不得访问网络\n关闭界面不能取消已启用闹钟",
        "package_id": "ai.vibapp.alarm-clock-retry-20260827",
        "package_name": "离线时钟闹钟重试",
        "acceptance_example": "启动后可选择时区并创建多个闹钟；关闭应用界面后，已启用闹钟仍由 VibApp daemon 调度并产生通知。",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bridge_call(command: str, args: dict[str, Any], timeout: int = 90) -> dict[str, Any]:
    request = json.dumps({"command": command, "args": args}, ensure_ascii=False).encode("utf-8")
    completed = subprocess.run(
        [str(BRIDGE), str(DATA_DIR)],
        input=request,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"bridge {command} exited {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', errors='replace')[:1000]}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"bridge {command} returned malformed JSON") from error
    if response.get("ok") is not True:
        raise RuntimeError(f"bridge {command} rejected request: {response.get('error')}")
    return response


def current_desktop_build_inputs_sha256() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required to verify the product bridge build receipt")
    completed = subprocess.run(
        [node, str(DESKTOP_INPUTS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "desktop build-input receipt computation failed: "
            + completed.stderr.decode("utf-8", errors="replace")[:1000]
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("desktop build-input receipt computation returned malformed JSON") from error
    digest = value.get("sha256") if isinstance(value, dict) else None
    if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RuntimeError("desktop build-input receipt is invalid")
    return digest


def validate_bridge_health_response(response: dict[str, Any], expected_digest: str) -> None:
    result = response.get("result") if isinstance(response, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "vibapp.product-bridge.experimental-v1"
        or result.get("service") != "vibapp-product-bridge"
        or result.get("build_input_receipt") != {
            "schema_version": "vibapp.desktop-build-input-receipt.experimental-v1",
            "sha256": expected_digest,
        }
        or result.get("contracts") != EXPECTED_BRIDGE_CONTRACTS
        or result.get("pipeline_budget_seconds") != 2700
        or result.get("bridge_lifetime_seconds") != 2760
        or result.get("bridge_lifetime_margin_seconds") != 60
        or result.get("worker_cleanup_budget_seconds") != 20
        or result.get("lifecycle") != "cancel-join-confirm"
    ):
        raise RuntimeError("product-bridge-health-mismatch")


def require_current_bridge() -> None:
    try:
        metadata = BRIDGE.lstat()
    except OSError as error:
        raise RuntimeError(f"product bridge is missing: {BRIDGE}") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or not os.access(BRIDGE, os.X_OK):
        raise RuntimeError(f"product bridge must be an executable ordinary file: {BRIDGE}")
    expected_digest = current_desktop_build_inputs_sha256()
    validate_bridge_health_response(bridge_call("health", {}, timeout=30), expected_digest)


def start_delivery(task: dict[str, Any], registry: dict[str, Any]) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    request = json.dumps(
        {
            "command": "submit_development_task",
            "args": {
                "task": task,
                "registry": registry,
                "explicit_user_submit": True,
                "acknowledge_external_cost": True,
                "retry_task_id": None,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    process = subprocess.Popen(
        [str(BRIDGE), str(DATA_DIR)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(request)
    process.stdin.close()
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        raise RuntimeError(f"delivery bridge returned no receipt: {stderr[:1000]}")
    response = json.loads(line)
    if response.get("ok") is not True:
        raise RuntimeError(f"delivery admission rejected: {response.get('error')}")
    return process, response


def current_job(task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = bridge_call("get_state", {}, timeout=60)
    jobs = state["result"].get("jobs", [])
    for job in jobs:
        if job.get("task_id") == task_id:
            return job, state
    raise RuntimeError(f"admitted task {task_id} is absent from product state")


def wait_for_terminal(task_id: str, process: subprocess.Popen[bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + 20 * 60
    previous = None
    latest_state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        job, latest_state = current_job(task_id)
        marker = (job.get("status"), job.get("current_stage"), job.get("progress_percent"))
        if marker != previous:
            print(
                f"PROGRESS {task_id} status={marker[0]} stage={marker[1]} progress={marker[2]}",
                flush=True,
            )
            previous = marker
        if job.get("status") in TERMINAL_STATUSES:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise RuntimeError("delivery bridge did not stop after terminal task state")
            return job, latest_state
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"delivery bridge stopped before terminal state: {stderr[:1000]}")
        time.sleep(5)
    raise RuntimeError(f"task {task_id} exceeded the 20 minute product limit")


def run_sample(sample: dict[str, Any], directory: Path) -> dict[str, Any]:
    started_at = utc_now()
    print(f"SAMPLE_START {sample['key']} {sample['package_id']}", flush=True)
    submitted = bridge_call(
        "submit_need",
        {
            "title": sample["title"],
            "description": sample["description"],
            "embedding_consent": False,
        },
    )
    write_json(directory / "submit-need.json", submitted)
    need_id = submitted["result"]["need"]["need_id"]
    completed = bridge_call(
        "complete_need",
        {
            "need_id": need_id,
            "app_kind": sample["app_kind"],
            "capabilities": sample["capabilities"],
            "allowed_permissions": sample["allowed_permissions"],
            "forbidden_permissions": sample["forbidden_permissions"],
            "permission_ceiling_confirmed": True,
            "negative_constraints": sample["negative_constraints"],
            "negative_constraints_confirmed": True,
            "network_mode": "offline",
            "package_id": sample["package_id"],
            "package_name": sample["package_name"],
            "package_version": "0.1.0",
            "acceptance_example": sample["acceptance_example"],
            "proceed_after_recommendation": False,
            "registry_embedding_consent": False,
            "remote_processing_consent": True,
            "public_publication_consent": False,
        },
    )
    write_json(directory / "complete-need.json", completed)
    result = completed["result"]
    registry = result["registry"]
    route = registry.get("route")
    if route != "refinement" or registry.get("recommendations"):
        raise RuntimeError(f"Registry did not produce an honest no-match/refinement route: {route}")
    cloud = result["cloud_development"]
    if cloud.get("publication", {}).get("performed") is not False:
        raise RuntimeError("publication boundary is not false")
    task = cloud.get("task_preparation", {}).get("schema_preview")
    if not isinstance(task, dict):
        raise RuntimeError("fresh task-bound CodeAgent preview was not created")
    if (
        cloud.get("provider_execution", {}).get("execution_available") is not True
        or cloud.get("required_conditions", {}).get("authoritative_registry_no_match") is not True
        or cloud.get("required_conditions", {}).get("registry_development_evidence_bound") is not True
        or cloud.get("task_preparation", {}).get("submission_available") is not True
    ):
        raise RuntimeError("live CodeAgent submission is not currently available and safely remains closed")
    consent = task.get("consent", {})
    if (
        task.get("provider") != "openai-codex"
        or task.get("model") != MODEL
        or consent.get("single_use") is not True
        or consent.get("payload_digest_sha256") != task.get("immutable_task_digest_sha256")
    ):
        raise RuntimeError("task provider/model/consent binding is invalid")

    process, admission = start_delivery(task, registry)
    write_json(directory / "admission.json", admission)
    receipt = admission["result"]["receipt"]
    adapter = admission["result"]["codeagent_adapter"]
    if adapter.get("started") is not True:
        raise RuntimeError(f"real CodeAgent did not start: {adapter.get('error')}")
    task_id = receipt["task_id"]
    attempt_id = receipt["attempt_id"]
    print(f"ADMITTED {sample['key']} task={task_id} attempt={attempt_id}", flush=True)
    job, state = wait_for_terminal(task_id, process)
    write_json(directory / "final-state.json", state)
    outcome = {
        "sample": sample["key"],
        "old_failure": sample["old_failure"],
        "package_id": sample["package_id"],
        "need_id": need_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "provider": task["provider"],
        "model": task["model"],
        "real_codeagent_started": True,
        "status": job.get("status"),
        "current_stage": job.get("current_stage"),
        "progress_percent": job.get("progress_percent"),
        "error": job.get("error"),
        "outputs": job.get("outputs"),
        "verification": job.get("verification"),
        "publication_performed": False,
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
    }
    write_json(directory / "result.json", outcome)
    print(
        f"SAMPLE_DONE {sample['key']} status={outcome['status']} stage={outcome['current_stage']}",
        flush=True,
    )
    return outcome


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-real-provider",
        action="store_true",
        help="explicitly authorize this script to start the task-bound real CodeAgent provider",
    )
    parser.add_argument(
        "--acknowledge-external-cost",
        action="store_true",
        help="separately acknowledge that the real provider may consume paid quota",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.enable_real_provider or not args.acknowledge_external_cost:
        raise RuntimeError(
            "real retry sampling is disabled; both --enable-real-provider and "
            "--acknowledge-external-cost are required"
        )
    require_current_bridge()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = OUTPUT_ROOT / f"retry-failed-samples-{run_id}"
    summary: dict[str, Any] = {
        "schema_version": "vibapp.retry-failed-samples.experimental-v1",
        "run_id": run_id,
        "started_at_utc": utc_now(),
        "execution_policy": {
            "sequential": True,
            "maximum_concurrent_codeagents": 1,
            "provider_memory_limit_bytes": 2 * 1024 * 1024 * 1024,
            "provider_pid_limit": 16,
            "provider_wall_time_seconds": 300,
            "public_publication": False,
        },
        "results": [],
    }
    write_json(output_dir / "summary.json", summary)
    print(f"RUN_DIR {output_dir}", flush=True)
    for sample in SAMPLES:
        sample_dir = output_dir / sample["key"]
        try:
            result = run_sample(sample, sample_dir)
        except Exception as error:
            result = {
                "sample": sample["key"],
                "old_failure": sample["old_failure"],
                "package_id": sample["package_id"],
                "status": "harness-failed",
                "error": {"message": str(error)},
                "publication_performed": False,
                "finished_at_utc": utc_now(),
            }
            write_json(sample_dir / "result.json", result)
            print(f"SAMPLE_ERROR {sample['key']} {error}", flush=True)
        summary["results"].append(result)
        write_json(output_dir / "summary.json", summary)
    summary["finished_at_utc"] = utc_now()
    summary["counts"] = {
        "total": len(summary["results"]),
        "succeeded": sum(item.get("status") == "succeeded" for item in summary["results"]),
        "failed": sum(item.get("status") != "succeeded" for item in summary["results"]),
    }
    write_json(output_dir / "summary.json", summary)
    print(f"RUN_DONE succeeded={summary['counts']['succeeded']} failed={summary['counts']['failed']}", flush=True)
    return 0 if summary["counts"]["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("INTERRUPTED", file=sys.stderr)
        raise SystemExit(130)
