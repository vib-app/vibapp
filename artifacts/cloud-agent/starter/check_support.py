#!/usr/bin/env python3
"""One isolated infrastructure-only compile; no model, network, install or promotion."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[2]
sys.path.insert(0, str(REPO / "artifacts/app-builder"))
from app_builder import MacSandboxCargoRunner, _validate_cargo_manifest  # noqa: E402
from common import ProcessLimits, run_bounded  # noqa: E402
from descriptor_reconciliation import run_guest_descriptor_reconciliation  # noqa: E402
from verifier import TYPE_ONLY_IMPORTS, _run_wasm_checks  # noqa: E402

TOOL_LAYER = REPO / "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980"
CACHE = REPO / "generated/builder-cargo-cache-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d"
CARGO = '''[package]
name = "vibapp-support-smoke"
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

def write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)

def run(output: Path, tool_layer: Path, cache: Path, builder_input_root: Path) -> dict:
    if not output.is_absolute() or REPO / "artifacts" not in output.parents:
        raise ValueError("--output must be a new absolute directory below repository artifacts/")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output = output.resolve(strict=True)
    source = output / "source"
    write(source / "Cargo.toml", CARGO.encode())
    write(source / "src/lib.rs", (BASE / "tests/lib.rs").read_bytes())
    write(source / "src/vibapp_support.rs", (BASE / "vibapp_support.rs").read_bytes())
    write(source / "wit/contract.wit", (REPO / "wit/experimental-v0/contract.wit").read_bytes())
    _validate_cargo_manifest(source / "Cargo.toml")
    limits = ProcessLimits(wall_seconds=180, cpu_seconds=120, memory_bytes=2 * 1024**3, pids=32, disk_bytes=512 * 1024**2)
    runner = MacSandboxCargoRunner(tool_layer, cache / "cargo-home", cache / "acceptance.json", builder_input_root=builder_input_root)
    built = runner.execute(source, output, limits)
    component = output / "component.wasm"
    write(component, built.component_bytes)
    write(output / "Cargo.lock", built.cargo_lock_bytes)
    write(output / "build.stdout", built.stdout)
    write(output / "build.stderr", built.stderr)
    write(output / "build-observation.json", json.dumps({
        "tool_versions": built.tool_versions, "isolation": built.isolation,
        "limits": limits.as_dict(), "resource_observations": built.resource_observations,
    }, indent=2).encode())
    environment = {"PATH": "/usr/bin:/bin", "TZ": "UTC", "LANG": "C", "LC_ALL": "C"}
    for name, args in (("validate", ["validate"]), ("wit", ["component", "wit"]), ("metadata", ["metadata", "show"])):
        result = run_bounded([str(tool_layer / "bin/wasm-tools"), *args, str(component)], cwd=output, environment=environment, limits=limits, disk_root=output)
        write(output / f"{name}.txt", result.stdout)
    return inspect(output, tool_layer)

def inspect(output: Path, tool_layer: Path) -> dict:
    output = output.resolve(strict=True)
    if REPO / "artifacts" not in output.parents:
        raise ValueError("inspection output must be below repository artifacts/")
    for name, original in (("lib.rs", BASE / "tests/lib.rs"), ("vibapp_support.rs", BASE / "vibapp_support.rs")):
        if (output / "source/src" / name).read_bytes() != original.read_bytes():
            raise AssertionError("compiled source snapshot differs from current support fixture")
    component = output / "component.wasm"
    component_bytes = component.read_bytes()
    wit = (output / "wit.txt").read_text()
    imports = sorted(set(re.findall(r"^\s*import\s+([^;]+);\s*$", wit, re.MULTILINE)))
    expected = sorted(f"vibapp:experimental-v0/{name}@0.0.1" for name in ("clock", "host-info", "kv", "log", "settings"))
    if sorted(set(imports) - TYPE_ONLY_IMPORTS) != expected or "export vibapp:experimental-v0/guest@0.0.1;" not in wit:
        raise AssertionError(f"unexpected Component imports/exports: {imports}")
    metadata = (output / "metadata.txt").read_text()
    if "rustc [1.93.0" not in metadata or "wit-bindgen-rust [0.60.0]" not in metadata:
        raise AssertionError("wrong producer metadata")
    digest = hashlib.sha256(component_bytes).hexdigest()
    manifest_projection = {
        "app": {"id": "example.vibapp.support-smoke", "version": "0.1.0", "kind": "ui", "display_name": "Support Smoke"},
        "entrypoints": [{"id": "main", "kind": "launcher-ui", "label": "Support Smoke", "routes": {"initial": "home"}}],
        "runtime": {"world": "ui-only-reference", "required_imports": expected},
        "artifacts": {"canonical_component": {"sha256": digest}},
    }
    wasm_checks = _run_wasm_checks(component, manifest_projection, tool_layer / "bin/wasm-tools")
    descriptor = run_guest_descriptor_reconciliation(component, manifest_projection)
    observation_path = output / "build-observation.json"
    evidence = {
        "state": "local-support-smoke-passed-not-release-acceptance",
        "scope": "synthetic-infrastructure-only-non-public",
        "install": False, "publish": False, "promotion": False,
        "component_sha256": digest, "component_bytes": len(component_bytes),
        "support_sha256": hashlib.sha256((BASE / "vibapp_support.rs").read_bytes()).hexdigest(),
        "component_imports": imports, "capability_imports": expected,
        "wasm_checks": wasm_checks, "descriptor_check": descriptor,
        "guest_checks": "describe executed allocator alignment/grow/shrink/prefix/ceiling, memcmp, Vec/String realloc, field typing/duplicates, surface IDs/close, settings and migration assertions",
        "cargo_jobs": 1,
        "build_observation": json.loads(observation_path.read_text()) if observation_path.exists() else None,
        "build_observation_note": "original successful compile preceded wrapper accounting repair; see build.stderr" if not observation_path.exists() else None,
        "reproducibility": "not checked; one build only",
    }
    write(output / "evidence.json", (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode())
    return evidence

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tool-layer", type=Path, default=TOOL_LAYER)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--builder-input-root", type=Path, default=REPO / "generated")
    parser.add_argument("--inspect-existing", action="store_true", help="Recheck existing exact compiled support bytes without another build")
    args = parser.parse_args()
    result = inspect(args.output, args.tool_layer) if args.inspect_existing else run(args.output, args.tool_layer, args.cache, args.builder_input_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
