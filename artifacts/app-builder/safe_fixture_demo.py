#!/usr/bin/env python3
"""Create an inert WS02 integration candidate without executing generated source.

This exercises source-handoff intake, Builder quarantine, independent verification,
and exact-byte promotion. It is deliberately not evidence of CodeAgent compilation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app_builder import SafeFixtureRunner, build_handoff
from common import canonical_json, exclusive_write, sha256_file, source_tree_digest
from verifier import verify_and_promote


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
CONTRACT = REPO / "wit/experimental-v0/contract.wit"
COMPONENT = REPO / "artifacts/desktop/runtime-apps/hello/component.wasm"
EXPECTED_COMPONENT_SHA256 = "d3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa"


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    exclusive_write(path, payload, mode=0o600)


def _make_handoff(input_root: Path, job_id: str) -> Path:
    source = input_root / "source"
    source.mkdir(parents=True, mode=0o700)
    cargo = b'''[package]
name = "ai_vibapp_hello"
version = "0.1.0"
edition = "2024"
publish = false
autobins = false
autoexamples = false
autotests = false
autobenches = false

[lib]
crate-type = ["cdylib"]

[dependencies]
wit-bindgen = { version = "=0.60.0", default-features = false, features = ["bitflags", "macro-string", "macros", "realloc"] }
'''
    rust = b'''#![no_std]
extern crate alloc;
use core::alloc::{GlobalAlloc, Layout};
use core::panic::PanicInfo;
struct InertAllocator;
unsafe impl GlobalAlloc for InertAllocator {
    unsafe fn alloc(&self, _layout: Layout) -> *mut u8 { core::ptr::null_mut() }
    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {}
}
#[global_allocator]
static ALLOCATOR: InertAllocator = InertAllocator;
#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! { loop { core::hint::spin_loop(); } }
wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" });
pub const SAFE_FIXTURE: &str = "inert intake fixture; this source is never executed";
'''
    _write(source / "Cargo.toml", cargo)
    _write(source / "src/lib.rs", rust)
    _write(source / "wit/contract.wit", CONTRACT.read_bytes())
    records = []
    for relative, role in (
        ("Cargo.toml", "generated-source"),
        ("src/lib.rs", "generated-source"),
        ("wit/contract.wit", "authoritative-contract-copy"),
    ):
        payload = (source / relative).read_bytes()
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "role": role,
            }
        )
    records.sort(key=lambda row: row["path"].encode("utf-8"))
    document = {
        "schema_version": "vibapp.codeagent-source-handoff.experimental-v2",
        "document_type": "codeagent-source-handoff",
        "status": "untrusted-source-awaiting-builder",
        "job_id": job_id,
        "need_spec_digest_sha256": "1" * 64,
        "source_tree_sha256": source_tree_digest(records),
        "source_directory": "source",
        "files": records,
        "package_intent": {
            "app_id": "ai.vibapp.hello",
            "version": "0.1.0",
            "display_name": "Hello VibApp",
            "description": "A safe Builder-to-Runtime integration fixture.",
            "entrypoints": [
                {
                    "id": "main",
                    "kind": "launcher-ui",
                    "label": "Hello VibApp",
                    "initial_route": "home",
                }
            ],
        },
        "target": {
            "contract": "vibapp:experimental-v0@0.0.1",
            "wasi": "0.2",
            "rust_target": "wasm32-wasip2",
            "wit_world": "ui-only-reference",
            "app_kind": "ui",
            "profiles": ["desktop"],
            "required_imports": [
                "vibapp:experimental-v0/clock@0.0.1",
                "vibapp:experimental-v0/kv@0.0.1",
                "vibapp:experimental-v0/log@0.0.1",
                "vibapp:experimental-v0/host-info@0.0.1",
                "vibapp:experimental-v0/settings@0.0.1",
            ],
            "required_capabilities": [
                "vibapp:experimental-v0/kv@0.0.1",
                "vibapp:experimental-v0/settings@0.0.1",
            ],
        },
        "provider_execution": {
            "mode": "dry-run-fixture",
            "adapter": "local-static-fixture",
            "provider_id": "static-fixture",
            "external_request_attempted": False,
            "external_request_observed": False,
            "gateway_request_id": None,
            "executable_sha256": None,
            "isolation_policy_version": None,
            "runner_id": None,
            "runner_identity_sha256": None,
            "isolation_policy_sha256": None,
            "receipt_digest_sha256": None,
        },
        "authority": {
            "compile": "separate-builder",
            "verify": "separate-verifier",
            "install": "none",
            "publish": "none",
        },
        "created_at_utc": "2026-08-27T03:00:00Z",
    }
    handoff = input_root / "handoff.json"
    _write(handoff, canonical_json(document) + b"\n")
    return handoff


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the inert WS02 integration fixture")
    parser.add_argument("--workspace", type=Path, default=BASE / "demo-output")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    component_digest = sha256_file(COMPONENT, 16 * 1024 * 1024, "safe fixture component")
    if component_digest != EXPECTED_COMPONENT_SHA256:
        raise RuntimeError("safe fixture component changed; independently review and repin it")
    input_root = args.workspace / "inputs" / args.job_id
    input_root.mkdir(parents=True, mode=0o700)
    handoff = _make_handoff(input_root, args.job_id)
    pipeline_root = args.workspace / "pipeline"
    receipt = build_handoff(
        handoff,
        pipeline_root,
        SafeFixtureRunner(COMPONENT.read_bytes()),
    )
    candidate = verify_and_promote(receipt, pipeline_root)
    print(
        json.dumps(
            {
                "candidate": str(candidate.resolve()),
                "component_sha256": component_digest,
                "safe_fixture_no_source_execution": True,
                "codeagent_compilation_proven": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
