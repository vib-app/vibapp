#!/usr/bin/env python3
"""Bounded local Builder: NeedSpec -> Rust source -> quarantinable candidate."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[2]
RUNS = BASE / "runs"
LOCK_PATH = BASE / ".builder.lock"
CONTRACT = REPO / "wit/experimental-v0/contract.wit"
COMPILE_SCRIPT = BASE / "compile_inside.sh"
BUILDER_IMAGE = "sha256:ca24698517c86b3b433b6f62f2a02476048cc3b7251268d356a028b373e090dc"
BUILDER_BASE_IMAGE = "sha256:9a94ba32757132618f161731e0b93b8bbd5a34c10f256a1d883ca50ff4f422f2"
DOCKER = Path("/usr/local/bin/docker")
MAX_NEED_BYTES = 16 * 1024
MAX_LOG_BYTES = 512 * 1024
BUILD_TIMEOUT_SECONDS = 120

REQUIRED_IMPORTS = [
    "vibapp:experimental-v0/clock@0.0.1",
    "vibapp:experimental-v0/kv@0.0.1",
    "vibapp:experimental-v0/log@0.0.1",
    "vibapp:experimental-v0/host-info@0.0.1",
    "vibapp:experimental-v0/settings@0.0.1",
]


class BuildFailure(RuntimeError):
    pass


def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BuildFailure(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def load_need(path: Path) -> tuple[dict[str, str], bytes]:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_NEED_BYTES:
        raise BuildFailure("NeedSpec must be a regular file no larger than 16 KiB")
    raw = path.read_bytes()
    try:
        parsed = json.loads(raw, object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildFailure(f"invalid NeedSpec JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise BuildFailure("NeedSpec root must be an object")

    required = {
        "schema_version",
        "app_id",
        "version",
        "display_name",
        "description",
        "headline",
        "body",
    }
    if set(parsed) != required:
        missing = sorted(required - set(parsed))
        extra = sorted(set(parsed) - required)
        raise BuildFailure(f"NeedSpec fields mismatch missing={missing} extra={extra}")
    if parsed["schema_version"] != "vibapp.need.product-platform.v1":
        raise BuildFailure("unsupported NeedSpec schema_version")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", parsed["app_id"]):
        raise BuildFailure("app_id is not a Stage 0 identifier")
    if not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", parsed["version"]
    ):
        raise BuildFailure("version must be a simple semantic version")
    limits = {
        "display_name": 80,
        "description": 1000,
        "headline": 160,
        "body": 1000,
    }
    for key, limit in limits.items():
        value = parsed[key]
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
            raise BuildFailure(f"{key} must be a non-empty UTF-8 string <= {limit} bytes")
        if any(ord(character) < 0x20 and character not in "\t" for character in value):
            raise BuildFailure(f"{key} contains a control character")
    return parsed, canonical_json(parsed)


def rust_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def generated_rust(need: dict[str, str]) -> str:
    app_id = rust_literal(need["app_id"])
    version = rust_literal(need["version"])
    display_name = rust_literal(need["display_name"])
    headline = rust_literal(need["headline"])
    body = rust_literal(need["body"])
    return f'''#![no_std]
#![allow(unsafe_code)]

extern crate alloc;

extern crate self as wit_bindgen;

pub mod rt {{
        use alloc::alloc::{{alloc, dealloc, handle_alloc_error, realloc, Layout}};
        use core::ptr::{{self, NonNull}};

        pub fn maybe_link_cabi_realloc() {{}}
        pub fn run_ctors_once() {{}}

        #[unsafe(no_mangle)]
        pub unsafe extern "C" fn cabi_realloc(
            old_pointer: *mut u8,
            old_size: usize,
            align: usize,
            new_size: usize,
        ) -> *mut u8 {{
            if old_size == 0 {{
                if new_size == 0 {{
                    return align as *mut u8;
                }}
                let layout = unsafe {{ Layout::from_size_align_unchecked(new_size, align) }};
                let pointer = unsafe {{ alloc(layout) }};
                if pointer.is_null() {{
                    handle_alloc_error(layout);
                }}
                return pointer;
            }}
            if new_size == 0 {{
                let layout = unsafe {{ Layout::from_size_align_unchecked(old_size, align) }};
                unsafe {{ dealloc(old_pointer, layout) }};
                return ptr::null_mut();
            }}
            let layout = unsafe {{ Layout::from_size_align_unchecked(old_size, align) }};
            let pointer = unsafe {{ realloc(old_pointer, layout, new_size) }};
            if pointer.is_null() {{
                handle_alloc_error(layout);
            }}
            pointer
        }}

        pub struct Cleanup {{
            ptr: NonNull<u8>,
            layout: Layout,
        }}

        impl Cleanup {{
            pub fn new(layout: Layout) -> (*mut u8, Option<Self>) {{
                if layout.size() == 0 {{
                    return (ptr::null_mut(), None);
                }}
                let pointer = unsafe {{ alloc(layout) }};
                let pointer = NonNull::new(pointer).unwrap_or_else(|| handle_alloc_error(layout));
                (pointer.as_ptr(), Some(Self {{ ptr: pointer, layout }}))
            }}

            pub fn forget(self) {{
                core::mem::forget(self);
            }}
        }}

        impl Drop for Cleanup {{
            fn drop(&mut self) {{
                unsafe {{ dealloc(self.ptr.as_ptr(), self.layout) }};
            }}
        }}
}}

use core::alloc::{{GlobalAlloc, Layout}};
use core::ptr;

struct BoundedBumpAllocator;

static mut NEXT_ALLOCATION: usize = 0;

unsafe extern "C" {{
    static __heap_base: u8;
}}

unsafe impl GlobalAlloc for BoundedBumpAllocator {{
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {{
        let heap_base = ptr::addr_of!(__heap_base) as usize;
        let current = unsafe {{
            if NEXT_ALLOCATION == 0 {{
                heap_base
            }} else {{
                NEXT_ALLOCATION
            }}
        }};
        let aligned = match current.checked_add(layout.align() - 1) {{
            Some(value) => value & !(layout.align() - 1),
            None => return ptr::null_mut(),
        }};
        let end = match aligned.checked_add(layout.size()) {{
            Some(value) => value,
            None => return ptr::null_mut(),
        }};
        let current_bytes = core::arch::wasm32::memory_size(0) * 65_536;
        if end > current_bytes {{
            let additional = end - current_bytes;
            let pages = (additional + 65_535) / 65_536;
            if core::arch::wasm32::memory_grow(0, pages) == usize::MAX {{
                return ptr::null_mut();
            }}
        }}
        unsafe {{ NEXT_ALLOCATION = end }};
        aligned as *mut u8
    }}

    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {{}}
}}

#[global_allocator]
static GLOBAL_ALLOCATOR: BoundedBumpAllocator = BoundedBumpAllocator;

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo<'_>) -> ! {{
    core::arch::wasm32::unreachable()
}}

include!("ui_only_reference.rs");

use alloc::borrow::ToOwned;
use alloc::string::String;
use alloc::vec;
use alloc::vec::Vec;
use crate::exports::vibapp::experimental_v0::guest as guest;
use crate::vibapp::experimental_v0::common::{{
    AppError, AppKind, EntrypointKind, ErrorCode,
}};
use crate::vibapp::experimental_v0::settings;
use crate::vibapp::experimental_v0::ui;

// The frozen ui-only-reference world requires these five typed imports even
// though this inert local view does not use their results. Keeping the function
// exported prevents the linker from erasing the selected world's import set.
#[unsafe(export_name = "__vibapp_force_declared_imports")]
pub extern "C" fn force_declared_imports() {{
    use crate::vibapp::experimental_v0::{{clock, host_info, kv, log}};
    let _ = clock::monotonic_now();
    let _ = kv::get("");
    let _ = log::write(log::Level::Debug, "", &[], "product-platform-link");
    let _ = host_info::describe_host();
    let _ = settings::current();
}}

struct GeneratedApp;

fn app_error(message: &str) -> AppError {{
    AppError {{
        code: ErrorCode::InvalidArgument,
        message: message.to_owned(),
        retryable: false,
    }}
}}

fn view(session: String, surface: String, route: String) -> ui::SurfaceUpdate {{
    let root = "root".to_owned();
    ui::SurfaceUpdate {{
        session,
        surface,
        route,
        view: ui::View {{
            title: {display_name}.to_owned(),
            root: root.clone(),
            nodes: vec![
                ui::Node {{
                    id: root.clone(),
                    parent: None,
                    kind: ui::NodeKind::ListContainer(ui::ListNode {{ label: None }}),
                }},
                ui::Node {{
                    id: "headline".to_owned(),
                    parent: Some(root.clone()),
                    kind: ui::NodeKind::Text(ui::TextNode {{
                        text: {headline}.to_owned(),
                        style: ui::TextStyle::Title,
                    }}),
                }},
                ui::Node {{
                    id: "body".to_owned(),
                    parent: Some(root),
                    kind: ui::NodeKind::Text(ui::TextNode {{
                        text: {body}.to_owned(),
                        style: ui::TextStyle::Body,
                    }}),
                }},
            ],
        }},
    }}
}}

impl guest::Guest for GeneratedApp {{
    fn describe() -> Result<guest::AppDescriptor, AppError> {{
        Ok(guest::AppDescriptor {{
            id: {app_id}.to_owned(),
            version: {version}.to_owned(),
            kind: AppKind::Ui,
            display_name: {display_name}.to_owned(),
            entrypoints: vec![guest::EntrypointDescriptor {{
                id: "main".to_owned(),
                kind: EntrypointKind::LauncherUi,
                label: {display_name}.to_owned(),
                initial_route: Some("home".to_owned()),
            }}],
        }})
    }}

    fn get_settings_schema() -> Result<Option<guest::SettingsSchema>, AppError> {{
        Ok(None)
    }}

    fn validate_settings(
        _context: guest::CallContext,
        proposed: guest::SettingsSnapshot,
    ) -> Result<guest::SettingsValidation, AppError> {{
        if !proposed.values.is_empty() {{
            return Err(app_error("this UI-only local profile has no settings"));
        }}
        Ok(settings::SettingsValidation {{
            accepted: true,
            field_errors: Vec::new(),
            service_restart_required: false,
        }})
    }}

    fn handle_event(
        _context: guest::CallContext,
        event: guest::AppEvent,
    ) -> Result<guest::EventOutput, AppError> {{
        let surface = match event {{
            guest::AppEvent::Launcher(guest::LauncherEvent::Launch(event)) =>
                Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Open(event)) =>
                Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Restore(event)) =>
                Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Action(event)) =>
                Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Focus(event)) if event.focused =>
                Some((event.session, event.surface, "home".to_owned())),
            _ => None,
        }};
        Ok(guest::EventOutput {{
            surfaces: surface
                .map(|(session, surface, route)| vec![view(session, surface, route)])
                .unwrap_or_default(),
            diagnostic: None,
        }})
    }}

    fn health(
        _context: guest::CallContext,
        _request: guest::HealthRequest,
    ) -> Result<guest::HealthReport, AppError> {{
        Ok(guest::HealthReport {{
            status: guest::HealthStatus::Healthy,
            checks: vec![guest::HealthCheck {{
                name: "generated-ui".to_owned(),
                status: guest::HealthStatus::Healthy,
                message: "inert declarative UI is ready".to_owned(),
            }}],
        }})
    }}

    fn migrate(
        _context: guest::CallContext,
        request: guest::MigrationRequest,
    ) -> Result<guest::MigrationResult, AppError> {{
        if request.from_schema != 1 || request.to_schema != 1 {{
            return Err(app_error("only state schema 1 is supported"));
        }}
        Ok(guest::MigrationResult {{
            status: guest::MigrationStatus::Unchanged,
            target_state_revision: request.target_state_revision,
        }})
    }}
}}

export!(GeneratedApp);
'''


def cargo_toml(version: str) -> str:
    return f'''[package]
name = "vibapp-generated-ui"
version = "{version}"
edition = "2024"
publish = false

[lib]
crate-type = ["cdylib"]

[profile.release]
panic = "abort"
codegen-units = 1
lto = true
strip = true
'''


def cargo_lock(version: str) -> str:
    return f'''# This file is automatically @generated by Cargo.
# It is not intended for manual editing.
version = 4

[[package]]
name = "vibapp-generated-ui"
version = "{version}"
'''


def run_capped(command: list[str], timeout_seconds: int, output_limit: int) -> str:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    chunks: list[bytes] = []
    total = 0
    started = time.monotonic()
    try:
        while True:
            if time.monotonic() - started > timeout_seconds:
                os.killpg(process.pid, signal.SIGKILL)
                raise BuildFailure(f"bounded process exceeded {timeout_seconds}s")
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(process.stdout.fileno(), 8192)
                if chunk:
                    total += len(chunk)
                    if total > output_limit:
                        os.killpg(process.pid, signal.SIGKILL)
                        raise BuildFailure(f"bounded process exceeded {output_limit} output bytes")
                    chunks.append(chunk)
            if process.poll() is not None:
                remainder = process.stdout.read(output_limit - total + 1)
                total += len(remainder)
                if total > output_limit:
                    raise BuildFailure(f"bounded process exceeded {output_limit} output bytes")
                chunks.append(remainder)
                break
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    output = b"".join(chunks).decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise BuildFailure(f"bounded process failed rc={process.returncode}:\n{output[-8192:]}")
    return output


def source_tree_digest(source_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("Cargo.toml", "Cargo.lock", "lib.rs"):
        data = (source_dir / name).read_bytes()
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(2, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def artifact(path: str, media_type: str, file_path: Path) -> dict[str, object]:
    return {
        "path": path,
        "media_type": media_type,
        "sha256": sha256_file(file_path),
        "size_bytes": file_path.stat().st_size,
    }


def package_digest(manifest: dict[str, object]) -> str:
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    descriptors: list[dict[str, object]] = [
        artifacts["canonical_component"],
        *artifacts["assets"],
        artifacts["provenance"],
        artifacts["sbom"],
    ]
    for derivation in artifacts["browser_derivations"]:
        descriptors.extend(derivation["files"])
        descriptors.append(derivation["derivation_attestation"])
    descriptors.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    manifest_bytes = canonical_json(manifest)
    preimage = bytearray(b"VIBAPP-PACKAGE\x00experimental-v0\x00")
    preimage.extend(len(manifest_bytes).to_bytes(8, "big"))
    preimage.extend(manifest_bytes)
    for descriptor in descriptors:
        path_bytes = str(descriptor["path"]).encode("utf-8")
        preimage.extend(len(path_bytes).to_bytes(2, "big"))
        preimage.extend(path_bytes)
        preimage.extend(bytes.fromhex(str(descriptor["sha256"])))
        preimage.extend(int(descriptor["size_bytes"]).to_bytes(8, "big"))
    return sha256_bytes(bytes(preimage))


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json(value) + b"\n")


def make_archive(package_dir: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ("manifest.json", "component.wasm", "provenance.json", "sbom.cdx.json"):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, (package_dir / name).read_bytes())


def docker_build(source_dir: Path, output_dir: Path) -> tuple[list[str], str]:
    output_dir.chmod(0o777)
    command = [
        str(DOCKER),
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--cpus",
        "2",
        "--pids-limit",
        "64",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=536870912,mode=1777",
        "-e",
        "HOME=/tmp/home",
        "-e",
        "CARGO_HOME=/tmp/cargo-home",
        "-e",
        "CARGO_INCREMENTAL=0",
        "-e",
        "CARGO_NET_OFFLINE=true",
        "-e",
        "RUSTUP_AUTO_INSTALL=0",
        "-e",
        "SOURCE_DATE_EPOCH=1786440135",
        "-e",
        "TZ=UTC",
        "-e",
        "LANG=C.UTF-8",
        "-e",
        "LC_ALL=C.UTF-8",
        "-e",
        "RUSTFLAGS=--remap-path-prefix=/tmp/project=/workspace",
        "--mount",
        f"type=bind,source={source_dir},target=/input,readonly",
        "--mount",
        f"type=bind,source={output_dir},target=/output",
        "--mount",
        f"type=bind,source={CONTRACT},target=/contract/contract.wit,readonly",
        "--mount",
        f"type=bind,source={COMPILE_SCRIPT},target=/runner/compile_inside.sh,readonly",
        "--entrypoint",
        "/bin/sh",
        BUILDER_IMAGE,
        "/runner/compile_inside.sh",
    ]
    log = run_capped(command, BUILD_TIMEOUT_SECONDS, MAX_LOG_BYTES)
    for path in output_dir.iterdir():
        path.chmod(0o600)
    output_dir.chmod(0o700)
    return command, log


def build(need_path: Path) -> dict[str, object]:
    need, need_bytes = load_need(need_path)
    need_digest = sha256_bytes(need_bytes)
    run_id = f"{need_digest[:16]}-{time.time_ns()}"
    run_dir = RUNS / run_id
    source_dir = run_dir / "source"
    raw_output_dir = run_dir / "raw-output"
    candidate_dir = run_dir / "candidate"
    package_dir = candidate_dir / "package"
    for directory in (source_dir, raw_output_dir, package_dir):
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)

    job_state: dict[str, object] = {
        "schema_version": "vibapp.builder-job.product-platform.v1",
        "run_id": run_id,
        "need_sha256": need_digest,
        "status": "generated",
        "scope": "product-platform-local-hold",
        "candidate_status": "none",
    }
    write_json(run_dir / "job.json", job_state)
    (source_dir / "lib.rs").write_text(generated_rust(need), encoding="utf-8")
    (source_dir / "Cargo.toml").write_text(cargo_toml(need["version"]), encoding="utf-8")
    (source_dir / "Cargo.lock").write_text(cargo_lock(need["version"]), encoding="utf-8")
    for path in source_dir.iterdir():
        path.chmod(0o600)

    started_at = now_utc()
    command, compile_log = docker_build(source_dir, raw_output_dir)
    finished_at = now_utc()
    (run_dir / "compile.log").write_text(compile_log, encoding="utf-8")
    (run_dir / "compile.log").chmod(0o600)
    component = raw_output_dir / "component.wasm"
    if not component.is_file() or component.stat().st_size > 16 * 1024 * 1024:
        raise BuildFailure("compiler did not produce a bounded regular component.wasm")
    shutil.copyfile(component, package_dir / "component.wasm")

    provenance = {
        "schema_version": "vibapp.provenance.product-platform.v1",
        "scope": "product-platform-local-hold",
        "need_sha256": need_digest,
        "source_tree_sha256": source_tree_digest(source_dir),
        "cargo_lock_sha256": sha256_file(source_dir / "Cargo.lock"),
        "contract_sha256": sha256_file(CONTRACT),
        "component_wasm_sha256": sha256_file(component),
        "builder_image_digest": BUILDER_IMAGE,
        "builder_base_image_digest": BUILDER_BASE_IMAGE,
        "tool_versions": {
            "rustc": "1.93.0 (254b59607 2026-01-19)",
            "cargo": "1.93.0 (083ac5135 2025-12-15)",
            "wit_bindgen_cli": "0.60.0",
            "wasm_tools": "1.256.0",
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "network_phase": "none",
        "command": command,
        "limits": {
            "memory": "1g",
            "memory_swap": "1g",
            "cpus": 2,
            "pids": 64,
            "wall_seconds": BUILD_TIMEOUT_SECONDS,
            "log_bytes": MAX_LOG_BYTES,
            "tmpfs_bytes": 536870912,
        },
    }
    write_json(package_dir / "provenance.json", provenance)
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": need["app_id"],
                "version": need["version"],
            }
        },
        "components": [],
        "properties": [
            {"name": "ai.vibapp.scope", "value": "product-platform-local-hold"},
            {"name": "ai.vibapp.external-dependencies", "value": "none"},
        ],
    }
    write_json(package_dir / "sbom.cdx.json", sbom)

    capability_rows = []
    for interface in REQUIRED_IMPORTS:
        short_name = interface.split("/")[1].split("@")[0]
        scope: dict[str, object] = {}
        if short_name == "kv":
            scope = {"maximum_storage_bytes": 1_048_576}
        capability_rows.append(
            {
                "interface": interface,
                "necessity": "required",
                "grant": "automatic",
                "reason": f"Typed {short_name} interface required by the frozen ui-only-reference world; generated guest makes no live call in this local profile.",
                "scope": scope,
                "profiles": [
                    {
                        "profile": "desktop",
                        "availability": "native",
                        "behavior": "Linked by the local Stage 0 host contract; no ambient authority is granted.",
                    }
                ],
            }
        )

    manifest = {
        "schema_version": "vibapp.manifest.experimental-v0.0.1",
        "package_format": "vibapp.package.experimental-v0",
        "app": {
            "id": need["app_id"],
            "version": need["version"],
            "kind": "ui",
            "display_name": need["display_name"],
            "description": need["description"],
            "publisher": {"id": "ai.vibapp", "display_name": "VibApp Product Platform"},
        },
        "artifacts": {
            "canonical_component": artifact(
                "component.wasm", "application/wasm", package_dir / "component.wasm"
            ),
            "assets": [],
            "browser_derivations": [],
            "provenance": artifact(
                "provenance.json", "application/json", package_dir / "provenance.json"
            ),
            "sbom": artifact(
                "sbom.cdx.json",
                "application/vnd.cyclonedx+json",
                package_dir / "sbom.cdx.json",
            ),
        },
        "runtime": {
            "contract": "vibapp:experimental-v0@0.0.1",
            "wasi": "0.2",
            "world": "ui-only-reference",
            "required_imports": REQUIRED_IMPORTS,
            "profiles": [
                {
                    "profile": "desktop",
                    "mode": "full",
                    "background": "not-applicable",
                    "artifact_role": "canonical-component",
                    "degradation": "Local product profile only; no install, publication, or production runtime claim.",
                }
            ],
            "platforms": [
                {"os": "macos", "arch": "aarch64", "profiles": ["desktop"]},
                {"os": "linux", "arch": "aarch64", "profiles": ["desktop"]},
            ],
        },
        "entrypoints": [
            {
                "id": "main",
                "kind": "launcher-ui",
                "label": need["display_name"],
                "profiles": ["desktop"],
                "routes": {"initial": "home", "allowed": ["home"]},
                "restoration": "route-only",
            }
        ],
        "capabilities": capability_rows,
        "resources": {
            "linear_memory_bytes": 16_777_216,
            "event_wall_time_ms": 100,
            "health_migration_wall_time_ms": 1000,
            "output_bytes": 131_072,
            "stored_data_bytes": 1_048_576,
            "durable_schedules": 0,
            "log_bytes_per_day": 131_072,
        },
        "state": {"schema": 1, "migratable_from_min": 1, "migratable_from_max": 1},
        "lifecycle": {
            "disable": {
                "retains_package": True,
                "retains_state": True,
                "stops_services": True,
                "cancels_schedules": True,
            },
            "uninstall": {
                "allowed_data_dispositions": ["delete", "retain", "export-then-delete"],
                "default_data_disposition": "delete",
            },
        },
        "source": {
            "language": "rust",
            "revision": f"product-platform-{need_digest[:24]}",
            "cargo_lock_sha256": sha256_file(source_dir / "Cargo.lock"),
            "builder_image_digest": BUILDER_IMAGE,
        },
        "license": {"spdx_expression": "Apache-2.0"},
        "privacy": {"stores_personal_data": False, "network_access": "none"},
        "verification": {"status": "unverified", "revocation": "not-revoked"},
    }
    write_json(package_dir / "manifest.json", manifest)
    digest = package_digest(manifest)
    slug = need["app_id"].replace(".", "-")
    archive_name = f"{slug}-{need['version']}.vibapp"
    archive_path = candidate_dir / archive_name
    make_archive(package_dir, archive_path)
    handoff = {
        "schema_version": "vibapp.builder-handoff.product-platform.v1",
        "status": "builder-output-untrusted",
        "scope": "product-platform-local-hold",
        "need_sha256": need_digest,
        "package_digest_sha256": digest,
        "component_sha256": sha256_file(package_dir / "component.wasm"),
        "manifest_sha256": sha256_file(package_dir / "manifest.json"),
        "archive": archive_name,
        "archive_sha256": sha256_file(archive_path),
        "builder_image_digest": BUILDER_IMAGE,
        "network": "none",
        "promotion_authority": "none",
    }
    write_json(candidate_dir / "handoff.json", handoff)
    job_state.update(
        {
            "status": "candidate-created",
            "candidate_status": "untrusted-awaiting-verifier",
            "candidate_dir": str(candidate_dir),
            "package_digest_sha256": digest,
            "component_sha256": handoff["component_sha256"],
            "archive": str(archive_path),
        }
    )
    write_json(run_dir / "job.json", job_state)
    return job_state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("need", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not DOCKER.is_file() or not CONTRACT.is_file():
        print("required local Docker or contract file is unavailable", file=sys.stderr)
        return 69
    RUNS.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another Builder job is already running", file=sys.stderr)
            return 75
        try:
            result = build(args.need.resolve())
        except (BuildFailure, OSError, ValueError) as error:
            print(f"BUILD_FAILED: {error}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"CANDIDATE_CREATED {result['candidate_dir']}")
        print("status=untrusted-awaiting-verifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
