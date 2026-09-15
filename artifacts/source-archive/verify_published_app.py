#!/usr/bin/env python3
"""Anonymous exact-byte download -> independent verifier -> real guest actions.

Operational acceptance helper; no GitHub credentials, publication or app source
generation. Uses a new private directory and a required reviewer-authored plan.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from github_transport import public_download
from package_release import (MAX_FILE, MAX_JSON, PAYLOAD_PATHS, _run_independent_checks,
                             _write_new, parse_json, require, sha256, snapshot, validate_record)


def verify(receipt_path, output, plan):
    require(receipt_path.stat().st_size <= MAX_JSON, "receipt_size")
    receipt = parse_json(receipt_path.read_bytes())
    require(receipt.get("state") == "public-downloads-verified", "release_not_published")
    records = [item for item in receipt.get("verified_downloads", []) if item.get("path") in PAYLOAD_PATHS]
    require(len(records) == 5 and {item["path"] for item in records} == PAYLOAD_PATHS, "public_payload_boundary")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    package = output / "download"
    (package / "package").mkdir(mode=0o700, parents=True)
    downloads = []
    for record in records:
        maximum = MAX_FILE if record["path"].endswith(".wasm") else MAX_JSON
        require(type(record["size_bytes"]) is int and 0 < record["size_bytes"] <= maximum, "public_payload_size")
        data = public_download(record["url"], limit=record["size_bytes"])
        require(len(data) == record["size_bytes"] and sha256(data) == record["sha256"], "public_payload_mismatch")
        _write_new(package / record["path"], data)
        downloads.append({"path": record["path"], "sha256": sha256(data), "bytes": len(data)})
    candidate = package / "candidate.json"
    snapshot(candidate)
    record = parse_json(candidate.read_bytes())
    validate_record(record)
    require(record["package_digest_sha256"] == receipt["package_digest_sha256"], "public_digest_mismatch")
    _run_independent_checks(candidate)
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "product-integration/review_runtime_acceptance.py"),
               "--candidate", str(candidate), "--output", str(output / "runtime-review"), "--plan", str(plan.resolve(strict=True))]
    completed = subprocess.run(command, timeout=120, capture_output=True, check=False)
    require(completed.returncode == 0, "public_runtime_review_failed")
    review = parse_json((output / "runtime-review/runtime-review.json").read_bytes())
    require(review["status"] == "PASS" and review["real_guest_execution"] is True, "public_guest_not_executed")
    result = {"status": "PASS", "package_digest_sha256": receipt["package_digest_sha256"],
              "anonymous_download": True, "independent_verifier": "PASS", "runtime": review, "files": downloads}
    _write_new(output / "download-review.json", json.dumps(result, ensure_ascii=False, indent=2).encode())
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    verify(args.receipt.resolve(strict=True), args.output.resolve(), args.plan)
