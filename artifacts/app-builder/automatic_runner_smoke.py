#!/usr/bin/env python3
"""Real, bounded, reproducibility smoke for the automatic macOS Cargo runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
sys.path.insert(0, str(BASE))

from app_builder import MacSandboxCargoRunner, build_handoff  # noqa: E402
from common import (  # noqa: E402
    ProcessLimits,
    canonical_json,
    run_bounded,
    source_tree_digest,
)
from verifier import verify_and_promote  # noqa: E402


CONTRACT = REPO / "wit/experimental-v0/contract.wit"
DEFAULT_TOOL_LAYER = (
    REPO
    / "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980"
)
SOURCE_DATE_EPOCH = 1787796000
MEMORY_LIMIT = 2 * 1024 * 1024 * 1024


CARGO_TOML = b'''[package]
name = "vibapp-automatic-builder-smoke"
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


LIB_RS = br'''#![no_std]

extern crate alloc;

use alloc::string::{String, ToString};
use alloc::vec;
use alloc::vec::Vec;
use core::alloc::{GlobalAlloc, Layout};
use core::panic::PanicInfo;
use core::sync::atomic::{AtomicUsize, Ordering};

struct BoundedBumpAllocator;
static NEXT: AtomicUsize = AtomicUsize::new(0);

unsafe extern "C" {
    static __heap_base: u8;
}

unsafe impl GlobalAlloc for BoundedBumpAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        loop {
            let observed = NEXT.load(Ordering::Relaxed);
            let current = if observed == 0 {
                core::ptr::addr_of!(__heap_base) as usize
            } else {
                observed
            };
            let aligned = current.saturating_add(layout.align() - 1) & !(layout.align() - 1);
            let Some(end) = aligned.checked_add(layout.size()) else {
                return core::ptr::null_mut();
            };
            let available = core::arch::wasm32::memory_size(0) * 65_536;
            if end > available {
                let pages = (end - available).div_ceil(65_536);
                if core::arch::wasm32::memory_grow(0, pages) == usize::MAX {
                    return core::ptr::null_mut();
                }
            }
            match NEXT.compare_exchange(observed, end, Ordering::Relaxed, Ordering::Relaxed) {
                Ok(_) => return aligned as *mut u8,
                Err(_) => continue,
            }
        }
    }

    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {}
}

#[global_allocator]
static ALLOCATOR: BoundedBumpAllocator = BoundedBumpAllocator;

#[unsafe(no_mangle)]
unsafe extern "C" fn cabi_realloc(
    _old_pointer: *mut u8,
    _old_size: usize,
    alignment: usize,
    new_size: usize,
) -> *mut u8 {
    let Ok(layout) = Layout::from_size_align(new_size.max(1), alignment.max(1)) else {
        return core::ptr::null_mut();
    };
    unsafe { ALLOCATOR.alloc(layout) }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop {
        core::hint::spin_loop();
    }
}

wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" });

use exports::vibapp::experimental_v0::guest::{
    AppDescriptor, AppError, AppEvent, EntrypointDescriptor, EventOutput, Guest, HealthCheck,
    HealthReport, HealthRequest, HealthStatus, MigrationRequest, MigrationResult, MigrationStatus,
};
use vibapp::experimental_v0::common::{AppKind, CallContext, EntrypointKind, ErrorCode};
use vibapp::experimental_v0::settings::{SettingsSchema, SettingsSnapshot, SettingsValidation};
use vibapp::experimental_v0::ui::{
    ListNode, Node, NodeKind, SurfaceUpdate, TextNode, TextStyle, View,
};

struct SmokeApp;

fn app_error(message: &str) -> AppError {
    AppError {
        code: ErrorCode::InvalidArgument,
        message: message.to_string(),
        retryable: false,
    }
}

fn surface(session: String, surface: String, route: String) -> SurfaceUpdate {
    SurfaceUpdate {
        session,
        surface,
        route,
        view: View {
            title: "Automatic Builder Smoke".to_string(),
            root: "root".to_string(),
            nodes: vec![
                Node {
                    id: "root".to_string(),
                    parent: None,
                    kind: NodeKind::ListContainer(ListNode { label: None }),
                },
                Node {
                    id: "message".to_string(),
                    parent: Some("root".to_string()),
                    kind: NodeKind::Text(TextNode {
                        text: "Compiled by the bounded offline automatic Builder".to_string(),
                        style: TextStyle::Title,
                    }),
                },
            ],
        },
    }
}

#[unsafe(export_name = "__vibapp_force_declared_imports")]
pub extern "C" fn force_declared_imports() {
    use vibapp::experimental_v0::{clock, host_info, kv, log, settings};
    let _ = clock::monotonic_now();
    let _ = kv::get("");
    let _ = log::write(log::Level::Debug, "", &[], "automatic-builder-smoke");
    let _ = host_info::describe_host();
    let _ = settings::current();
}

impl Guest for SmokeApp {
    fn describe() -> Result<AppDescriptor, AppError> {
        Ok(AppDescriptor {
            id: "ai.vibapp.automatic-builder-smoke".to_string(),
            version: "0.1.0".to_string(),
            kind: AppKind::Ui,
            display_name: "Automatic Builder Smoke".to_string(),
            entrypoints: vec![EntrypointDescriptor {
                id: "main".to_string(),
                kind: EntrypointKind::LauncherUi,
                label: "Automatic Builder Smoke".to_string(),
                initial_route: Some("home".to_string()),
            }],
        })
    }

    fn get_settings_schema() -> Result<Option<SettingsSchema>, AppError> {
        Ok(None)
    }

    fn validate_settings(
        _context: CallContext,
        proposed: SettingsSnapshot,
    ) -> Result<SettingsValidation, AppError> {
        if !proposed.values.is_empty() {
            return Err(app_error("this smoke app has no settings"));
        }
        Ok(SettingsValidation {
            accepted: true,
            field_errors: Vec::new(),
            service_restart_required: false,
        })
    }

    fn handle_event(_context: CallContext, event: AppEvent) -> Result<EventOutput, AppError> {
        let identifiers = match event {
            AppEvent::Launcher(event) => match event {
                exports::vibapp::experimental_v0::guest::LauncherEvent::Launch(event) => {
                    Some((event.session, event.surface, event.route))
                }
                exports::vibapp::experimental_v0::guest::LauncherEvent::Open(event) => {
                    Some((event.session, event.surface, event.route))
                }
                exports::vibapp::experimental_v0::guest::LauncherEvent::Restore(event) => {
                    Some((event.session, event.surface, event.route))
                }
                exports::vibapp::experimental_v0::guest::LauncherEvent::Action(event) => {
                    Some((event.session, event.surface, event.route))
                }
                _ => None,
            },
            _ => None,
        };
        Ok(EventOutput {
            surfaces: identifiers
                .map(|(session, surface_id, route)| surface(session, surface_id, route))
                .into_iter()
                .collect(),
            diagnostic: None,
        })
    }

    fn health(_context: CallContext, _request: HealthRequest) -> Result<HealthReport, AppError> {
        Ok(HealthReport {
            status: HealthStatus::Healthy,
            checks: vec![HealthCheck {
                name: "offline-build".to_string(),
                status: HealthStatus::Healthy,
                message: "component exports are ready".to_string(),
            }],
        })
    }

    fn migrate(
        _context: CallContext,
        request: MigrationRequest,
    ) -> Result<MigrationResult, AppError> {
        if request.from_schema != 1 || request.to_schema != 1 {
            return Err(app_error("only state schema 1 is supported"));
        }
        Ok(MigrationResult {
            status: MigrationStatus::Unchanged,
            target_state_revision: request.target_state_revision,
        })
    }
}

export!(SmokeApp);
'''


def write_regular(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def make_handoff(root: Path, job_id: str) -> Path:
    source = root / "source"
    source.mkdir(parents=True, mode=0o700)
    files = [
        ("Cargo.toml", CARGO_TOML, "generated-source"),
        ("src/lib.rs", LIB_RS, "generated-source"),
        ("wit/contract.wit", CONTRACT.read_bytes(), "authoritative-contract-copy"),
    ]
    records = []
    for relative, payload, role in files:
        write_regular(source / relative, payload)
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "role": role,
            }
        )
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    handoff = {
        "schema_version": "vibapp.codeagent-source-handoff.experimental-v2",
        "document_type": "codeagent-source-handoff",
        "status": "untrusted-source-awaiting-builder",
        "job_id": job_id,
        "need_spec_digest_sha256": "9" * 64,
        "source_tree_sha256": source_tree_digest(records),
        "source_directory": "source",
        "files": records,
        "package_intent": {
            "app_id": "ai.vibapp.automatic-builder-smoke",
            "version": "0.1.0",
            "display_name": "Automatic Builder Smoke",
            "description": "A bounded real-compilation acceptance fixture for the automatic Builder.",
            "entrypoints": [
                {
                    "id": "main",
                    "kind": "launcher-ui",
                    "label": "Automatic Builder Smoke",
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
        "created_at_utc": "2026-08-27T00:00:00Z",
    }
    handoff_path = root / "handoff.json"
    write_regular(handoff_path, canonical_json(handoff) + b"\n")
    return handoff_path


def sha256_file(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"unsafe smoke evidence file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cache_root: Path, tool_layer: Path, output_root: Path) -> Path:
    cache_root = cache_root.resolve(strict=True)
    tool_layer = tool_layer.resolve(strict=True)
    cargo_home = cache_root / "cargo-home"
    acceptance = cache_root / "acceptance.json"
    if output_root.exists() or output_root.is_symlink():
        raise RuntimeError(f"refusing to replace existing smoke root: {output_root}")
    output_root.mkdir(parents=True, mode=0o700)
    limits = ProcessLimits(memory_bytes=MEMORY_LIMIT)
    wasm_tools = tool_layer / "bin/wasm-tools"
    results = []
    for ordinal in (1, 2):
        run_root = output_root / f"run-{ordinal}"
        input_root = run_root / "input"
        pipeline_root = run_root / "pipeline"
        input_root.mkdir(parents=True, mode=0o700)
        pipeline_root.mkdir(mode=0o700)
        handoff = make_handoff(input_root, f"automatic-builder-cache-smoke-{ordinal}")
        runner = MacSandboxCargoRunner(tool_layer, cargo_home, acceptance)
        receipt = build_handoff(handoff, pipeline_root, runner, limits=limits)
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
        component = receipt.parent / receipt_value["package_directory"] / "component.wasm"
        environment = {"PATH": "/usr/bin:/bin", "TZ": "UTC", "LANG": "C", "LC_ALL": "C"}
        run_bounded(
            [str(wasm_tools), "validate", str(component)],
            cwd=component.parent,
            environment=environment,
            limits=limits,
            disk_root=component.parent,
        )
        wit_result = run_bounded(
            [str(wasm_tools), "component", "wit", str(component)],
            cwd=component.parent,
            environment=environment,
            limits=limits,
            disk_root=component.parent,
        )
        metadata_result = run_bounded(
            [str(wasm_tools), "metadata", "show", str(component)],
            cwd=component.parent,
            environment=environment,
            limits=limits,
            disk_root=component.parent,
        )
        wit = wit_result.stdout.decode("utf-8")
        metadata = metadata_result.stdout.decode("utf-8")
        imports = sorted(set(re.findall(r"^\s*import\s+([^;]+);\s*$", wit, re.MULTILINE)))
        if "vibapp:experimental-v0/guest@0.0.1" not in wit:
            raise RuntimeError("compiled Component does not export the selected guest world")
        if "rustc [1.93.0" not in metadata or "wit-bindgen-rust [0.60.0]" not in metadata:
            raise RuntimeError("compiled Component lacks exact Rust/wit-bindgen producer metadata")
        candidate = verify_and_promote(receipt, pipeline_root, wasm_tools=wasm_tools)
        candidate_value = json.loads(candidate.read_text(encoding="utf-8"))
        verifier_result = {
            "outcome": "pass",
            "candidate": str(candidate),
            "candidate_state": candidate_value["state"],
            "checks": candidate_value["verification"]["checks"],
        }
        provenance = json.loads((component.parent / "provenance.json").read_text(encoding="utf-8"))
        results.append(
            {
                "ordinal": ordinal,
                "handoff": str(handoff),
                "quarantine_receipt": str(receipt),
                "quarantine_state": receipt_value["state"],
                "component": str(component),
                "component_sha256": sha256_file(component),
                "component_size_bytes": component.stat().st_size,
                "package_digest_sha256": receipt_value["package_digest_sha256"],
                "component_imports": imports,
                "metadata_observation": {
                    "rustc_1_93_0": True,
                    "wit_bindgen_rust_0_60_0": True,
                    "wit_bindgen_rust_0_45_0": False,
                },
                "wasm_tools_checks": [
                    "wasm-tools validate",
                    "wasm-tools component wit",
                    "wasm-tools metadata show",
                ],
                "resource_observations": provenance["resource_observations"],
                "verifier_result": verifier_result,
            }
        )
    if results[0]["component_sha256"] != results[1]["component_sha256"]:
        raise RuntimeError("two fresh automatic Builder runs produced different Component bytes")
    evidence = {
        "schema_version": "vibapp.automatic-builder-smoke.experimental-v1",
        "state": "independently-verified-local-macos-runner-smoke",
        "accepted_by": "independent-component-verifier",
        "scope": "local-product-prototype-not-stage0-production-isolation",
        "authority": {"install": "none", "publish": "none"},
        "network": "sandbox-denied-and-cargo-offline",
        "memory_limit_bytes": MEMORY_LIMIT,
        "cargo_jobs": 1,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "cache_root": str(cache_root),
        "cache_acceptance": str(acceptance),
        "cache_acceptance_sha256": sha256_file(acceptance),
        "tool_layer": str(tool_layer),
        "tool_layer_identity_sha256": tool_layer.name.removeprefix("sha256-"),
        "fresh_build_count": 2,
        "reproducible_component_sha256": results[0]["component_sha256"],
        "runs": results,
        "claims": {
            "real_rust_source_compiled": True,
            "real_wasi_0_2_component_wasm_tools_validated": True,
            "component_bytes_reproducible": True,
            "independent_component_verifier_passed": True,
            "production_isolation": False,
            "stage0_activation_or_publication": False,
        },
    }
    path = output_root / "runner-smoke-evidence.json"
    write_regular(path, canonical_json(evidence) + b"\n")
    path.chmod(0o444)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache_root", type=Path)
    parser.add_argument("--tool-layer", type=Path, default=DEFAULT_TOOL_LAYER)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        evidence = run(arguments.cache_root, arguments.tool_layer, arguments.output_root)
    except Exception as error:
        print(f"SMOKE_FAILED {str(error)[:4096]}", file=sys.stderr)
        return 1
    print(json.dumps({"state": "independently-verified-local-macos-runner-smoke", "evidence": str(evidence)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
