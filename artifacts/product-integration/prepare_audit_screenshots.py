#!/usr/bin/env python3
"""Prepare already-audited UI packages in a new short private product root."""
import argparse
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys

from audit_existing_apps import ARTIFACTS, bounded_bytes, strict_json, write_evidence


def prepare(audit_directory, product_root, bridge, runtime):
    if product_root != product_root.resolve(strict=True) or product_root.parent != Path("/private/tmp"):
        raise ValueError("Use an actual canonical /private/tmp directory, not a symlink")
    info = product_root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or any(product_root.iterdir()):
        raise ValueError("Screenshot root must be new, owner-private and empty")
    if len(str(product_root / "runtime-daemon/run/vibappd.sock").encode()) >= 100:
        raise ValueError("Screenshot root is too long for the native socket")
    inventory = strict_json(bounded_bytes(audit_directory / "inventory.json", 8 * 1024 * 1024))
    summary = strict_json(bounded_bytes(audit_directory / "summary.json", 8 * 1024 * 1024))
    frozen = {(r["app_id"], r["package_digest_sha256"]): r for c in inventory["catalogs"] for r in c["apps"]}
    selected = [r for r in summary["apps"] if r["status"] == "PASS" and r["catalog_state"] == "private" and r["ui"]]
    if not 1 <= len(selected) <= 32:
        raise ValueError("No bounded successful UI inventory")
    result = {"status": "FAIL", "product_root": str(product_root), "apps": [], "operations": [],
              "runtime_sha256": hashlib.sha256(bounded_bytes(runtime, 32 * 1024 * 1024)).hexdigest(),
              "bridge_sha256": hashlib.sha256(bounded_bytes(bridge, 32 * 1024 * 1024)).hexdigest(),
              "source_audit": str(audit_directory), "user_data_copied": False, "model_or_auth_configured": False,
              "gui_started": False, "cleanup": None}
    evidence = audit_directory / "screenshots-preparation.json"
    write_evidence(evidence, result)
    request = {"command": "save_network_settings", "args": {
        "p2pEnabled": False, "seedVerifiedApps": False, "allowUserFileSeeding": False,
        "uploadLimitKibPerSecond": 1024, "downloadLimitKibPerSecond": 4096,
        "cacheLimitMib": 128, "maxActiveTransfers": 1, "rtcEnabled": False,
        "maxActiveChannels": 1, "turnEnabled": False, "turnUrls": [], "turnUsername": "",
        "clearTurnCredential": True,
    }}
    import json
    saved = subprocess.run([str(bridge), str(product_root)], input=json.dumps(request).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                           env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"})
    response = strict_json(saved.stdout)
    result["network_settings"] = {"request": request, "response": response, "exit_code": saved.returncode}
    if saved.returncode != 0 or response.get("ok") is not True:
        write_evidence(evidence, result)
        raise ValueError("Production bridge did not save disabled networking")
    settings = strict_json(bounded_bytes(product_root / "settings/p2p-network.json"))
    if settings["p2p_enabled"] or settings["rtc_enabled"] or settings["turn_enabled"]:
        raise ValueError("Networking was not disabled")
    sys.path.insert(0, str(ARTIFACTS / "registry-store"))
    sys.path.insert(0, str(ARTIFACTS / "runtime-daemon"))
    from local_appstore import LocalAppStore
    from vibapp_daemon.core import RuntimeDaemon
    store = LocalAppStore(product_root / "local-appstore")
    daemon = RuntimeDaemon(product_root / "runtime-daemon", store.candidates, service_runtime_binary=runtime)
    try:
        for row in selected:
            bound = frozen[(row["app_id"], row["package_digest_sha256"])]
            source = Path(bound["source_candidate"])
            if bound["app_kind"] != "ui" or any(e["kind"] != "launcher-ui" for e in bound["entrypoints"]):
                raise ValueError("Screenshot preparation cannot activate background effects")
            if hashlib.sha256(bounded_bytes(source)).hexdigest() != bound["candidate_sha256"]:
                raise ValueError("Frozen candidate changed")
            store.ingest(source)
            candidate = store.candidates / bound["package_digest_sha256"] / "candidate.json"
            for tag, value, subject, promotion in (
                ("install", {"package_digest_sha256": bound["package_digest_sha256"], "enable_after_install": False}, None, candidate),
                ("enable", None, bound["app_id"], None),
                ("status", None, bound["app_id"], None),
            ):
                number = len(result["operations"]) + 1
                envelope = {"request_id": f"prepare-{number}", "idempotency_key": f"prepare-{number}",
                            "client": "cli", "subject": subject, "command": {"tag": tag, "value": value}}
                response = daemon.execute(envelope, principal="uid:screenshot-preparation", allowed_apps={bound["app_id"]}, promotion_record=promotion)
                result["operations"].append({"app_id": bound["app_id"], "envelope": envelope, "response": response})
                write_evidence(evidence, result)
                if "error" in response:
                    raise ValueError(f"Production daemon rejected {tag}: {response['error']}")
            result["apps"].append({"app_id": bound["app_id"], "display_name": bound["display_name"],
                                   "package_digest_sha256": bound["package_digest_sha256"],
                                   "candidate": str(candidate), "candidate_sha256": hashlib.sha256(bounded_bytes(candidate)).hexdigest(),
                                   "enabled": response["outcome"]["value"]["enabled"]})
        if daemon._workers or daemon._ui_workers:
            raise ValueError("Preparation unexpectedly created runtime workers")
        result["status"] = "PASS"
        return result
    finally:
        daemon.shutdown()
        result["cleanup"] = {"runtime_workers": len(daemon._workers), "ui_workers": len(daemon._ui_workers), "daemon_shutdown": True}
        write_evidence(evidence, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("audit-directory", "product-root", "bridge", "runtime"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    import json
    result = prepare(args.audit_directory.resolve(strict=True), args.product_root, args.bridge.resolve(strict=True), args.runtime.resolve(strict=True))
    print(json.dumps({"status": result["status"], "product_root": result["product_root"], "apps": result["apps"], "cleanup": result["cleanup"]}, ensure_ascii=False))
