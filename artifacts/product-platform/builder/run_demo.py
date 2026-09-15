#!/usr/bin/env python3
"""Run the good end-to-end case plus a mandatory tamper rejection case."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from build_product import BASE, write_json


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)


def main() -> int:
    builder = BASE / "build_product.py"
    verifier = BASE / "verify_candidate.py"
    need = BASE / "needs/good.json"
    built = run([sys.executable, str(builder), str(need), "--json"])
    if built.returncode != 0:
        print(built.stderr, file=sys.stderr)
        return 1
    job = json.loads(built.stdout)
    candidate = Path(job["candidate_dir"])
    repeated = run([sys.executable, str(builder), str(need), "--json"])
    if repeated.returncode != 0:
        print(repeated.stderr, file=sys.stderr)
        return 1
    repeated_job = json.loads(repeated.stdout)
    if repeated_job["component_sha256"] != job["component_sha256"]:
        print("two clean component builds produced different digests", file=sys.stderr)
        return 1
    good = run([sys.executable, str(verifier), str(candidate), "--json"])
    if good.returncode != 0:
        print(good.stderr, file=sys.stderr)
        return 1
    good_record = json.loads(good.stdout)

    tampered = candidate.parent / "tampered-candidate"
    if tampered.exists():
        print("refusing to overwrite an existing tampered case", file=sys.stderr)
        return 1
    shutil.copytree(candidate, tampered)
    component = tampered / "package/component.wasm"
    data = bytearray(component.read_bytes())
    if not data:
        print("cannot tamper with an empty component", file=sys.stderr)
        return 1
    data[-1] ^= 0x01
    component.write_bytes(data)
    bad = run([sys.executable, str(verifier), str(tampered), "--json"])
    if bad.returncode == 0:
        print("tampered candidate was not rejected", file=sys.stderr)
        return 1

    report = {
        "schema_version": "vibapp.builder-demo.product-platform.v1",
        "scope": "product-platform-local-hold",
        "good_case": {
            "result": "quarantined",
            "candidate": str(candidate),
            "package_digest_sha256": good_record["package_digest_sha256"],
            "component_sha256": good_record["component_sha256"],
            "archive": good_record["archive"],
            "repeat_candidate": repeated_job["candidate_dir"],
            "two_clean_component_digests_match": True,
        },
        "tampered_case": {
            "result": "rejected",
            "candidate": str(tampered),
            "exit_code": bad.returncode,
            "diagnostic": bad.stderr.strip()[-4096:],
        },
        "formal_acceptance": False,
        "published": False,
        "installed": False,
    }
    report_path = candidate.parent / "demo-report.json"
    write_json(report_path, report)
    quarantine_dir = Path(good_record["archive"]).parent
    latest = {
        "schema_version": "vibapp.builder-feed.product-platform.v1",
        "scope": "product-platform-local-hold",
        "status": "quarantined",
        "builder_job": str(candidate.parent / "job.json"),
        "candidate_handoff": str(candidate / "handoff.json"),
        "verifier_record": str(quarantine_dir / "verifier-record.json"),
        "archive": good_record["archive"],
        "package_digest_sha256": good_record["package_digest_sha256"],
        "component_sha256": good_record["component_sha256"],
        "demo_report": str(report_path),
        "accepted": False,
        "published": False,
        "installed": False,
    }
    latest_path = BASE / "latest.json"
    write_json(latest_path, latest)
    print(
        json.dumps(
            {**report, "report": str(report_path), "latest": str(latest_path)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
