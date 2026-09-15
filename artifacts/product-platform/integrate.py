#!/usr/bin/env python3
"""Create bounded, read-only-derived feeds between local product modules.

This adapter is intentionally product-v0 glue, not an accepted VibApp contract.
It never grants install or publication authority.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REGISTRY_SNAPSHOT = ROOT / "registry" / "snapshots" / "registry.snapshot.json"
BUILDER_LATEST = ROOT / "builder" / "latest.json"
INTEGRATION_DIR = ROOT / "integration"
CLIENT_REGISTRY_FEED = INTEGRATION_DIR / "registry-feed.json"
CLIENT_BUILDER_FEED = INTEGRATION_DIR / "builder-feed.json"
WEBSITE_REGISTRY_SNAPSHOT = ROOT / "website" / "public" / "data" / "registry.snapshot.json"
MAX_INPUT_BYTES = 512 * 1024
MAX_RECORDS = 50


def load_bounded_json(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry_to_client(snapshot: dict[str, Any]) -> dict[str, Any]:
    records = snapshot.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("registry records must be a bounded array")

    apps = []
    for record in records:
        if (record.get("app", {}).get("publisher", {}).get("publisher_id") == "fixture.vibapp"
                or record.get("app", {}).get("id", "").startswith("ai.vibapp.fixture.")
                or record.get("record_id", "").startswith("registry.fixture.")):
            continue
        app = record["app"]
        package = record["package"]
        verification = record["verification"]
        compatibility = record["compatibility"]
        permissions = record.get("permissions", [])
        publisher = app["publisher"]

        apps.append(
            {
                "app_id": app["id"],
                "display_name": app["display_name"],
                "version": app["version"],
                "kind": app["kind"],
                "summary": app["summary"],
                "publisher": publisher["display_name"],
                "profiles": compatibility.get("profiles", []),
                "permissions": [item["interface"] for item in permissions],
                "verification_state": verification["status"],
                "publication_state": record["publication"]["state"],
                "install_eligible": False,
                "package_digest_sha256": package["package_digest_sha256"],
                "verification_summary": (
                    "Synthetic fixture only; local preview cannot install or publish."
                ),
            }
        )

    return {
        "platform_status": "product-platform-local-hold",
        "source_snapshot_sha256": digest(REGISTRY_SNAPSHOT),
        "install_authority": False,
        "publication_authority": False,
        "apps": apps,
    }


def builder_to_client(latest: dict[str, Any]) -> dict[str, Any]:
    if latest.get("scope") != "product-platform-local-hold":
        raise ValueError("Builder feed escaped the local product-platform scope")
    if latest.get("status") != "quarantined":
        raise ValueError("only a quarantined Builder result may enter the client feed")
    if any(latest.get(flag) is not False for flag in ("accepted", "published", "installed")):
        raise ValueError("Builder feed claims authority it does not have")

    builder_job = Path(str(latest["builder_job"]))
    if ROOT not in builder_job.resolve().parents:
        raise ValueError("Builder job path is outside the product platform")
    job = load_bounded_json(builder_job)
    verifier_path = Path(str(latest["verifier_record"]))
    if ROOT not in verifier_path.resolve().parents:
        raise ValueError("verifier record path is outside the product platform")
    verifier = load_bounded_json(verifier_path)
    if verifier.get("status") != "quarantined":
        raise ValueError("verifier record is not quarantined")
    job_id = str(job["run_id"])

    return {
        "platform_status": "product-platform-local-hold",
        "accepted": False,
        "published": False,
        "installed": False,
        "jobs": [
            {
                "job_id": job_id,
                "need_id": str(job["need_sha256"])[:16],
                "title": "Hello VibApp — 本地闭环样例",
                "route": "local-builder",
                "status": "quarantined",
                "progress_percent": 100,
                "current_stage": "独立质检完成，已进入隔离区",
                "updated_at_utc": verifier["verified_at"],
                "stages": [
                    {"kind": "need", "status": "complete", "label": "需求已收好"},
                    {"kind": "generate", "status": "complete", "label": "Rust 源码已生成"},
                    {"kind": "compile", "status": "complete", "label": "受限离线编译完成"},
                    {"kind": "verify", "status": "complete", "label": "独立质检通过"},
                    {"kind": "install", "status": "blocked", "label": "真实安装未授权"},
                ],
                "verification": {
                    "status": "quarantined",
                    "independent": True,
                    "summary": "Component、manifest、摘要与文件边界通过；仅隔离，未接受、发布或安装。",
                },
                "package_digest_sha256": latest["package_digest_sha256"],
                "component_sha256": latest["component_sha256"],
            }
        ],
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"output exceeds {MAX_INPUT_BYTES} bytes: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")


def main() -> None:
    snapshot = load_bounded_json(REGISTRY_SNAPSHOT)
    registry_feed = registry_to_client(snapshot)
    write_json(CLIENT_REGISTRY_FEED, registry_feed)

    builder_latest = load_bounded_json(BUILDER_LATEST)
    builder_feed = builder_to_client(builder_latest)
    write_json(CLIENT_BUILDER_FEED, builder_feed)

    WEBSITE_REGISTRY_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REGISTRY_SNAPSHOT, WEBSITE_REGISTRY_SNAPSHOT)
    if digest(REGISTRY_SNAPSHOT) != digest(WEBSITE_REGISTRY_SNAPSHOT):
        raise RuntimeError("website Registry snapshot copy mismatch")

    print(
        json.dumps(
            {
                "status": "ok",
                "apps": len(registry_feed["apps"]),
                "builder_jobs": len(builder_feed["jobs"]),
                "registry_feed": str(CLIENT_REGISTRY_FEED),
                "builder_feed": str(CLIENT_BUILDER_FEED),
                "website_snapshot": str(WEBSITE_REGISTRY_SNAPSHOT),
                "registry_source_sha256": registry_feed["source_snapshot_sha256"],
                "builder_package_sha256": builder_latest["package_digest_sha256"],
                "install_authority": False,
                "publication_authority": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
