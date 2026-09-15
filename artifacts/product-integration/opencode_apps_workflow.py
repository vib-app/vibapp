#!/usr/bin/env python3
"""Explicit real product admission for the authorized application acceptance run.

Host orchestration only: no application source, generated responses or credentials.
Public publication is a separate command after exact-package functional acceptance.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time

from retry_failed_samples import write_json, current_desktop_build_inputs_sha256

# This repository's .cargo/config.toml directs builds to the workspace target.
# Do not borrow archived sample scripts' stale src-tauri/target binary paths.
BRIDGE = Path(__file__).resolve().parents[2] / "target/release/vibapp-product-bridge"

MODEL = "qwen3.8-27b-uncensored-mtp-q4"
TASK_PROVIDERS = {"opencode": "opencode", "codex": "openai-codex"}
APPS = {
    "converter": {
        "name": "轻换算",
        "description": "制作离线单位换算应用‘轻换算’。单个紧凑窗口约460×440，简洁中文语义界面、清晰标题层次和结果强调，不重复宿主head，不出现开发者ID等技术文本。提供数值输入和长度/重量/温度三类转换：米与英尺、千克与磅、摄氏与华氏；按钮可切换转换类别、交换方向、换算。负温度和小数可正确处理，非法、空白、NaN/Infinity和过长输入显示友好错误，不崩溃、不静默当0。结果保留合适小数；用应用自己的kv保存选择和上次有效输入，关闭重开仍保留。测试示例：1米约3.28084英尺，1千克约2.20462磅，0摄氏为32华氏，-40摄氏为-40华氏，212华氏为100摄氏。禁止联网、读取宿主文件、通知、后台服务。所有按钮必须真实可用，不能只显示静态结果。窗口会由宿主周期性刷新：结果和错误不能在下一次刷新突然消失，显示必须与当前类别、方向、输入一致；需要时从保存的输入重新计算，不要只在点击时临时显示。",
        "capabilities": ["kv", "settings"],
        "acceptance": "长度、重量和温度双向换算通过已知数值验证；空白、非法文本和非有限数被拒绝且保留可修正输入；结果和错误在宿主刷新后仍可见；类别、方向与有效输入在关闭并重新启动后恢复。",
    },
    "shopping": {
        "name": "随手购",
        "description": "制作离线购物清单‘随手购’，约440×560独立窗口，清爽中文界面，醒目的输入和添加按钮、已购/待购数量、友好空状态，无重复宿主head和技术说明。用户输入商品名称后点添加，支持中文和空格，自动去掉两端空格，拒绝空白及超过80字的输入；最多30项并提示容量。每项都有对应标记已购/恢复待购、删除按钮，标签应含商品名避免歧义；提供‘清空已购’，不会删除待购项；保留添加次序。列表和勾选状态用应用专属kv保存，关闭重开包括重启runtime仍存在。测试：添加牛奶与鸡蛋，标记牛奶已购，重启后状态保留，清空已购仅保留鸡蛋，再删除鸡蛋出现空状态。禁止联网、读取宿主文件、通知、后台任务。交互真实实现，不能只画静态列表。",
        "capabilities": ["kv", "settings"],
        "acceptance": "能增删和切换已购状态；拒绝空白及过长输入；清空已购保留待购；重启runtime后数据和勾选状态保留。",
    },
    "focus": {
        "name": "一刻专注",
        "description": "制作前台专注计时器‘一刻专注’，约400×400独立小窗口，简洁中文语义界面，大号剩余时分秒、明显状态和少量按钮，无重复宿主head。提供25分钟专注、5分钟休息以及10秒测试预设；开始、暂停、继续、重置必须真实有效。通过宿主clock和UI刷新计算经过时间，不能每次render固定减1，不阻塞不忙等。暂停不计时，结束显示完成且不出现负数，重置回当前预设。明确告知这是前台计时，关闭应用不会发送后台通知或声音；关闭后重新启动应恢复为暂停或未开始，不能宣称后台提醒。应用专属kv可保存预设，不存用户私密文件。测试10秒预设：开始后刷新数字减少，暂停后两次刷新不减少，继续后到0显示完成，重置显示00:10。禁止联网、通知、后台服务或宿主文件访问。",
        "capabilities": ["clock", "kv", "settings"],
        "acceptance": "10秒预设可开始、暂停、继续和重置；暂停数值不变，运行按真实时间减少，到零停止显示完成；不声称后台通知。",
    },
}

# Concrete acceptance feedback from the actual generated app, not replacement
# app source. A revised task must keep the original product need and fix these.
APPS["converter"]["description"] += (
    "上一版真实运行验收发现须修复：粘贴86个中文‘中’（258 UTF-8字节）提示过长后，下一次刷新因状态长度使用u8回绕导致数据损坏，无法再输入。"
    "状态编码必须无损且有界，不能把字符串字节数强转u8；超长及多字节文本拒绝后应仍能清空或改为1继续使用，刷新和重开不应卡死。"
    "GUI把彻底清空的字段传为FieldValue::Empty，必须按空文本处理，不能忽略后回显旧值。"
    "每次处理完字段、类别或方向变更后，再重新计算结果并渲染；切换类别或方向的同一帧就必须正确，不能等待下一次刷新。"
    "第二版又出现保存category/direction完整单词，读取却各取1字节的问题，导致输入1刷新后变成ngtha_to_b1。"
    "必须使用写入/读取完全对称、无歧义且检查边界的状态编码；不允许读取失败只声称重置却持续报错。"
    "需要可回收的内存分配，不能仅增长不释放导致长时间刷新耗尽堆。"
)
APPS["converter"]["acceptance"] += (
    "粘贴86个中文中后刷新不崩溃，随后改成1可正常换算；真正清空输入显示空值提示，不沿用旧值；"
    "当前输入1切换重量立即显示约2.20462，切温度立即显示33.8；不需要额外点击换算或等待刷新。"
)


def bridge(root, command, args, timeout=90):
    request = json.dumps({"command": command, "args": args}, ensure_ascii=False).encode()
    result = subprocess.run([str(BRIDGE), str(root / "product")], input=request,
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"bridge {command} failed: {result.stderr.decode(errors='replace')[:700]}")
    value = json.loads(result.stdout)
    if value.get("ok") is not True:
        raise RuntimeError(f"bridge {command} rejected: {value.get('error')}")
    return value["result"]


def prepare(root, key, *, provider="opencode", code_model=MODEL):
    health = bridge(root, "health", {})
    if health["build_input_receipt"]["sha256"] != current_desktop_build_inputs_sha256():
        raise RuntimeError("Product bridge is stale; rebuild current tracked inputs first")
    directory = root / key
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    app = APPS[key]
    write_json(directory / "requirements.json", app)
    write_json(directory / "bridge-health.json", health)
    bridge(root, "save_codeagent_settings", {"selectedProvider": provider, "modelByProvider": {provider: code_model}})
    bridge(root, "save_model_settings", {
        "generation": {"enabled": True, "baseUrl": "http://192.168.199.170:8081", "model": MODEL,
                       "protocol": "chat-completions", "timeoutSeconds": 55, "maxOutputTokens": 3072, "temperature": 0},
        "embedding": {"enabled": False, "baseUrl": "http://192.168.199.170:8081", "model": "text-embedding-ada-002",
                      "dimensions": 384, "timeoutSeconds": 5},
    })
    submitted = bridge(root, "submit_need", {"title": app["name"], "description": app["description"], "embedding_consent": True})
    write_json(directory / "submit-need.json", submitted)
    analysis = submitted["need_spec"]["analysis"]
    if analysis.get("status") != "analyzed" or analysis.get("model") != MODEL:
        raise RuntimeError("Actual Qwen analysis did not complete; inspect private submit-need.json")
    print(json.dumps({"step": "requirement-analyzed", "app": key, "need_id": submitted["need"]["need_id"], "model": analysis["model"]}), flush=True)


def complete(root, key, *, provider="opencode", code_model=MODEL):
    directory = root / key
    app = APPS[key]
    submitted = json.loads((directory / "submit-need.json").read_bytes())
    completed = bridge(root, "complete_need", {
        "need_id": submitted["need"]["need_id"], "app_kind": "ui", "capabilities": app["capabilities"],
        "allowed_permissions": ["clock", "kv", "log", "host-info", "settings"],
        "forbidden_permissions": ["http", "notification", "scheduler"], "permission_ceiling_confirmed": True,
        "negative_constraints": "不得联网或读取宿主文件\n不得创建后台服务和通知\n不得返回假交互或静态示例代替功能",
        "negative_constraints_confirmed": True, "network_mode": "offline",
        "package_id": f"ai.vibapp.qwen.{key}20260907", "package_name": app["name"], "package_version": "0.1.0",
        "acceptance_example": app["acceptance"], "proceed_after_recommendation": False,
        "registry_embedding_consent": False, "remote_processing_consent": True, "public_publication_consent": False,
    })
    write_json(directory / "complete-need.json", completed)
    cloud = completed["cloud_development"]
    task = cloud["task_preparation"].get("schema_preview")
    if not task or task["provider"] != TASK_PROVIDERS[provider] or task["model"] != code_model:
        raise RuntimeError("Fresh selected CodeAgent task unavailable; inspect completion")
    if cloud["required_conditions"]["authoritative_registry_no_match"] is not True or cloud["task_preparation"]["submission_available"] is not True:
        raise RuntimeError("Product did not authorize development admission")
    write_json(directory / "task.json", task)
    write_json(directory / "registry.json", completed["registry"])
    print(json.dumps({"step": "task-confirmed", "app": key, "job_id": task["job_id"]}), flush=True)


def revise(root, key, *, provider="opencode", code_model=MODEL):
    directory = root / key
    previous = json.loads((directory / "admission.json").read_bytes())
    app = APPS[key]
    submitted = bridge(root, "submit_need", {
        "title": app["name"], "description": app["description"], "embedding_consent": True,
        "retry_task_id": previous["task_id"], "need_id": previous["need_id"],
    })
    write_json(directory / "submit-need.json", submitted)
    if submitted["need_spec"]["analysis"].get("status") != "analyzed":
        raise RuntimeError("Actual Qwen requirement revision analysis failed")
    print(json.dumps({"step": "requirement-revised", "app": key, "previous_task": previous["task_id"]}), flush=True)


def run(root, key, *, provider="opencode", code_model=MODEL):
    # Controller owns the foreground worker and whole-container cancellation.
    # Same product admission, without a UI bridge process lifetime dependency.
    import sys
    repository = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repository / "artifacts/orchestrator")]
    from delivery_controller import CodeAgentStage, DeliveryController
    from app_builder import MacSandboxCargoRunner
    from codeagent_vertical import TOOL_LAYER, DEFAULT_CARGO_HOME, DEFAULT_CACHE_ACCEPTANCE
    directory = root / key
    task = json.loads((directory / "task.json").read_bytes())
    if task["provider"] != TASK_PROVIDERS[provider] or task["model"] != code_model:
        raise RuntimeError("Task provider/model differs from explicit run selection")
    registry = json.loads((directory / "registry.json").read_bytes())
    runner = MacSandboxCargoRunner(TOOL_LAYER, DEFAULT_CARGO_HOME, DEFAULT_CACHE_ACCEPTANCE)
    stage = CodeAgentStage(provider_id=provider, model=code_model, acknowledge_external_cost=True)
    controller = DeliveryController(root / "product/delivery-controller", stage, runner, appstore_root=root / "product/local-appstore")
    previous_path = directory / "admission.json"
    previous_task = json.loads(previous_path.read_bytes())["task_id"] if previous_path.exists() else None
    admitted = controller.submit(task, registry, explicit_user_submit=True, edited_from_task_id=previous_task)
    write_json(directory / "admission.json", admitted)
    task_id = admitted["task_id"]
    print(json.dumps({"step": "admitted", "app": key, "task": task_id}), flush=True)
    controller.run_attempt(task_id, task["execution_attempt"]["attempt_id"])
    status = controller.status(task_id)
    write_json(directory / "delivery-status.json", status)
    print(json.dumps(status, ensure_ascii=False), flush=True)
    if status.get("attempt", {}).get("status") != "private-appstore-ready":
        raise RuntimeError("Real delivery did not succeed; inspect recorded status")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "revise", "complete", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app", choices=tuple(APPS), required=True)
    parser.add_argument("--provider", choices=tuple(TASK_PROVIDERS), default="opencode")
    parser.add_argument("--code-model", help="Required when explicitly selecting Codex; Qwen remains the requirement-analysis model")
    args = parser.parse_args()
    if args.provider == "codex" and not args.code_model:
        parser.error("--provider codex requires an explicit --code-model")
    globals()[args.command](args.root.resolve(), args.app, provider=args.provider, code_model=args.code_model or MODEL)
