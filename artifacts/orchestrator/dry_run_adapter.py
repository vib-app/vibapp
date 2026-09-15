#!/usr/bin/env python3
"""Bind the cloud-agent static UI fixture metadata to one validated local task.

This adapter is dry-run only. It invokes the reviewed cloud-agent's real validation,
source audit, and handoff implementation without calling a provider, consuming
consent, compiling, or running generated source.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


UI_WORLD = "ui-only-reference"
UI_IMPORTS = [
    "vibapp:experimental-v0/clock@0.0.1",
    "vibapp:experimental-v0/kv@0.0.1",
    "vibapp:experimental-v0/log@0.0.1",
    "vibapp:experimental-v0/host-info@0.0.1",
    "vibapp:experimental-v0/settings@0.0.1",
]


def load_cloud_agent(path: Path) -> Any:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError("reviewed cloud-agent entrypoint is not a regular file")
    name = "vibapp_reviewed_cloud_agent_dry_run"
    specification = importlib.util.spec_from_file_location(name, resolved)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load reviewed cloud-agent entrypoint")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task-bound local cloud-agent dry-run adapter")
    parser.add_argument("--cloud-agent", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        cloud = load_cloud_agent(arguments.cloud_agent)
        task = cloud.validate_task_file(arguments.task)
        if (
            task["target"]["app_kind"] != "ui"
            or task["target"]["wit_world"] != UI_WORLD
            or task["target"]["required_imports"] != UI_IMPORTS
        ):
            raise cloud.WorkerError(
                "dry-run-fixture-incompatible",
                "the bounded local source fixture only represents the accepted UI world",
            )
        fixture = cloud.load_json(
            cloud.DRY_RUN_RESULT,
            cloud.MAX_PROVIDER_RESULT_BYTES,
            "dry-run provider result fixture",
        )
        fixture = {
            **fixture,
            "job_id": task["job_id"],
            "wit_world": task["target"]["wit_world"],
            "app_kind": task["target"]["app_kind"],
            "declared_capabilities": task["target"]["required_imports"],
        }
        dynamic_fixture = arguments.output_root / "task-bound-provider-result.json"
        cloud.atomic_json(dynamic_fixture, fixture)
        cloud.DRY_RUN_RESULT = dynamic_fixture
        handoff = cloud.CloudAgentWorker().execute(
            arguments.task,
            arguments.output_root,
            dry_run=True,
        )
        created = cloud.load_json(handoff, cloud.MAX_PROVIDER_RESULT_BYTES, "source handoff")
        execution = created["provider_execution"]
        cloud.json_status(
            "source-handoff-created",
            handoff=str(handoff),
            external_request_attempted=execution["external_request_attempted"],
            external_request_observed=execution["external_request_observed"],
            gateway_request_id=execution["gateway_request_id"],
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "code": getattr(error, "code", "dry-run-adapter-failed"),
                    "message": str(error)[:1000],
                    "external_request_attempted": getattr(error, "external_request_attempted", False),
                    "external_request_observed": getattr(error, "external_request_observed", False),
                    "gateway_request_id": getattr(error, "gateway_request_id", None),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
