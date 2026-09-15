#!/usr/bin/env python3
"""Exact-byte Builder candidate -> local AppStore -> daemon lifecycle smoke."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ARTIFACTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ARTIFACTS / "registry-store"))
sys.path.insert(0, str(ARTIFACTS / "runtime-daemon"))

from local_appstore import LocalAppStore  # noqa: E402
from vibapp_daemon.core import RuntimeDaemon  # noqa: E402


def envelope(tag: str, value: Any, subject: str | None, sequence: int) -> dict[str, Any]:
    return {
        "request_id": f"integration-{sequence}-{tag}",
        "idempotency_key": f"integration-{sequence}-{tag}",
        "client": "cli",
        "subject": subject,
        "command": {"tag": tag, "value": value},
    }


def accepted(
    daemon: RuntimeDaemon,
    tag: str,
    value: Any,
    subject: str | None,
    sequence: int,
    promotion_record: Path | None = None,
) -> Any:
    response = daemon.execute(
        envelope(tag, value, subject, sequence),
        principal="uid:product-integration",
        allowed_apps={"*"},
        promotion_record=promotion_record,
    )
    if "error" in response:
        raise RuntimeError(f"daemon rejected {tag}: {response['error']}")
    return response["outcome"]["value"]


def run(candidate: Path) -> dict[str, Any]:
    candidate = candidate.resolve(strict=True)
    source_record = json.loads(candidate.read_text(encoding="utf-8"))
    digest = source_record["package_digest_sha256"]
    with tempfile.TemporaryDirectory(prefix="vibapp-product-integration-") as directory:
        scratch = Path(directory)
        store = LocalAppStore(scratch / "appstore")
        first = store.ingest(candidate)
        replay = store.ingest(candidate)
        if first["created"] is not True or replay["created"] is not False:
            raise RuntimeError("local AppStore ingest was not idempotent")
        record = store.detail(digest)["record"]
        if (
            record["state"] != "private"
            or record["publication"] != {"state": "not-published", "authority": "none"}
            or record["authority"] != {"install": "daemon", "publish": "none"}
        ):
            raise RuntimeError("local AppStore changed private install/publication authority")
        stored_candidate = (store.root / record["paths"]["candidate"]).resolve(strict=True)
        daemon = RuntimeDaemon(scratch / "runtime", store.candidates)
        app_id = record["app"]["id"]
        accepted(
            daemon,
            "install",
            {"package_digest_sha256": digest, "enable_after_install": False},
            None,
            1,
            stored_candidate,
        )
        staged = daemon.inspect_state()["apps"][app_id]
        if staged["enabled"] is not False or staged["lifecycle_state"] != "installed-disabled":
            raise RuntimeError("daemon did not stage the AppStore package disabled")
        accepted(daemon, "enable", None, app_id, 2)
        entrypoints = json.loads(
            (stored_candidate.parent / "package" / "manifest.json").read_text(encoding="utf-8")
        )["entrypoints"]
        ui = next((entry for entry in entrypoints if entry["kind"] == "launcher-ui"), None)
        if ui is not None:
            launched = accepted(
                daemon,
                "launch",
                {"entrypoint": ui["id"], "route": ui["routes"]["initial"]},
                app_id,
                3,
            )
            accepted(
                daemon,
                "surface-close",
                {"session": launched["session"], "surface": launched["surface"]},
                app_id,
                4,
            )
        after_close = accepted(daemon, "status", None, app_id, 5)
        if after_close["enabled"] is not True:
            raise RuntimeError("closing a UI surface disabled the installed app")
        accepted(daemon, "disable", None, app_id, 6)
        disabled = accepted(daemon, "status", None, app_id, 7)
        if disabled["enabled"] is not False or disabled["active_service_entrypoints"]:
            raise RuntimeError("disable did not quiesce daemon-owned lifecycle state")
        final_state = daemon.inspect_state()["apps"][app_id]
        return {
            "schema_version": "vibapp.product-integration-result.experimental-v1",
            "status": "PASS",
            "source_candidate_used_without_rewrite": str(candidate),
            "stored_candidate": record["paths"]["candidate"],
            "app_id": app_id,
            "package_digest_sha256": digest,
            "appstore_private": True,
            "publication_performed": False,
            "installed_disabled_before_enable": True,
            "surface_close_preserved_enablement": True,
            "disabled_after_smoke": True,
            "guest_execution_performed": final_state["guest_execution_performed"],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.candidate), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
