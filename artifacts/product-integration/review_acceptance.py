#!/usr/bin/env python3
"""Explicit, serial review acceptance using the actual Registry-first product API.

Each invocation uses private test state. Never publishes or edits user settings.
Source/build success is recorded separately from subsequent runtime UX checks.
"""
import argparse
from pathlib import Path
import tempfile
import secrets

import retry_failed_samples as flow

SCENARIOS = {
    "shopping-list": ("购物清单", "离线购物清单：输入商品名称后添加，可以勾选和取消已购商品、删除商品；显示未买数量。使用 app-scoped KV 保存，关闭重开后保留。简洁精致的小窗口，原生标题栏之外不加宿主介绍头部。"),
    "clock": ("时钟与秒表", "离线数字时钟与秒表：每秒更新当前时间，秒表有开始、暂停、重置，暂停时数值保持不变。状态用 app-scoped KV 保存；使用宿主 clock 的时间差计算，不能依赖重复启动次数累加。精致紧凑的小窗口。"),
    "notes": ("随手记", "离线笔记：多行输入正文，点击保存并明确显示保存结果，重新启动后读回相同正文；支持清空。只使用 app-scoped KV，不读取用户文件。精致轻量的编辑器窗口，编辑时自动刷新不能清空输入。"),
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", choices=SCENARIOS, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--enable-real-provider", action="store_true")
    parser.add_argument("--acknowledge-external-cost", action="store_true")
    args = parser.parse_args()
    if not args.enable_real_provider or not args.acknowledge_external_cost:
        parser.error("real authoring requires both explicit execution and cost acknowledgements")
    flow.BRIDGE = flow.REPOSITORY / "artifacts/desktop/target/debug/vibapp-product-bridge"
    flow.MODEL = args.model
    parent = flow.REPOSITORY / "artifacts/product-integration/output"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"review-{args.sample}-", dir=parent))
    flow.DATA_DIR = root / "client-data"
    print(f"ACCEPTANCE_ROOT {root}", flush=True)
    flow.require_current_bridge()
    flow.bridge_call("save_codeagent_settings", {"selectedProvider": "codex", "modelByProvider": {"codex": args.model}})
    title, description = SCENARIOS[args.sample]
    sample = {
        "key": args.sample, "old_failure": "review regression acceptance",
        "title": title, "description": description, "app_kind": "ui",
        "capabilities": ["clock", "kv", "settings"],
        "allowed_permissions": ["clock", "kv", "log", "host-info", "settings"],
        "forbidden_permissions": ["http"], "negative_constraints": "不访问网络\n不读取用户文件\n不公开发布",
        "package_id": "ai.vibapp.review." + args.sample + ".r" + secrets.token_hex(4),
        "package_name": title, "acceptance_example": description,
    }
    result = flow.run_sample(sample, root)
    print(f"RESULT {result['status']} {result.get('error')}", flush=True)
    return 0 if result["status"] == "succeeded" else 1

if __name__ == "__main__":
    raise SystemExit(main())
