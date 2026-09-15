#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from test_runtime_daemon import create_candidate, envelope  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        runtime = base / "runtime"
        promotions = base / "promotions"
        promotions.mkdir()
        record, digest = create_candidate(promotions)
        delete_record, delete_digest = create_candidate(promotions, "ai.vibapp.cli-delete")
        socket_path = runtime / "run" / "vibappd.sock"
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(ROOT)

        def start() -> subprocess.Popen[bytes]:
            process = subprocess.Popen(
                [sys.executable, "-m", "vibapp_daemon", "serve", "--root", str(runtime), "--promotion-root", str(promotions), "--socket", str(socket_path)],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not socket_path.exists() and process.poll() is None:
                time.sleep(0.02)
            if process.poll() is not None or not socket_path.exists():
                diagnostic = process.stderr.read().decode() if process.stderr else ""
                raise RuntimeError(f"daemon failed to start: {diagnostic}")
            return process

        def stop(process: subprocess.Popen[bytes]) -> None:
            process.terminate()
            process.wait(timeout=5)
            if process.stderr:
                process.stderr.close()

        def ctl(request: dict[str, Any], promotion: Path | None = None) -> dict[str, Any]:
            command = [sys.executable, "-m", "vibapp_daemon", "ctl", "--socket", str(socket_path)]
            if promotion is not None:
                command.extend(["--promotion-record", str(promotion)])
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                input=json.dumps(request).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            response = json.loads(completed.stdout)
            if completed.returncode != 0 or "error" in response:
                raise RuntimeError(f"control failed: {response}; stderr={completed.stderr.decode()}")
            return response

        process = start()
        app = "ai.vibapp.runtime-service-fixture"
        try:
            ctl(envelope("install", {"package_digest_sha256": digest, "enable_after_install": False}, subject=None, key="install"), record)
            staged = ctl(envelope("status", None, subject=app, key="status-staged"))["outcome"]["value"]
            if staged["enabled"] or staged["active_service_entrypoints"]:
                raise RuntimeError("install did not stage disabled")
            ctl(envelope("enable", None, subject=app, key="enable"))
            ctl(envelope("service-start", {"entrypoint": "main-service"}, subject=app, key="service-start"))
            launch = ctl(envelope("launch", {"entrypoint": "main-ui", "route": None}, subject=app, key="launch"))["outcome"]["value"]
            ctl(envelope("surface-close", {"session": launch["session"], "surface": launch["surface"]}, subject=app, key="surface-close"))
            after_close = ctl(envelope("status", None, subject=app, key="status-after-close"))["outcome"]["value"]
            if after_close["active_service_entrypoints"] != ["main-service"]:
                raise RuntimeError("UI close changed daemon service ownership")
        finally:
            stop(process)

        process = start()
        try:
            after_restart = ctl(envelope("status", None, subject=app, key="status-after-restart"))["outcome"]["value"]
            if after_restart["active_service_entrypoints"] != ["main-service"]:
                raise RuntimeError("daemon restart lost service ownership")
            ctl(envelope("service-stop", {"entrypoint": "main-service"}, subject=app, key="service-stop"))
            ctl(envelope("disable", None, subject=app, key="disable"))
            disabled = ctl(envelope("status", None, subject=app, key="status-disabled"))["outcome"]["value"]
            if disabled["enabled"] or disabled["active_service_entrypoints"]:
                raise RuntimeError("disable did not quiesce service authority")
            ctl(envelope("uninstall", {"disposition": "retain"}, subject=app, key="uninstall-retain"))

            delete_app = "ai.vibapp.cli-delete"
            ctl(envelope("install", {"package_digest_sha256": delete_digest, "enable_after_install": False}, subject=None, key="install-delete"), delete_record)
            ctl(envelope("enable", None, subject=delete_app, key="enable-delete"))
            ctl(envelope("uninstall", {"disposition": "delete"}, subject=delete_app, key="uninstall-delete"))
        finally:
            stop(process)

        state = json.loads((runtime / "state.json").read_text())
        result = {
            "status": "PASS",
            "guest_execution_performed": True,
            "installed_apps": sorted(state["apps"]),
            "retained_records": len(state["retained"]),
            "audit_events": state["sequence"],
            "socket_owner_authentication": "peer-uid",
            "os_service_packaging_claimed": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
