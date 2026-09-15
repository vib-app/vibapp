#!/usr/bin/env python3
"""Synthetic reclaiming-allocator proposal: one pinned compile, persistent-Store checks."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

BASE = Path(__file__).resolve().parent
STARTER = BASE.parent
sys.path.insert(0, str(STARTER))
from check_support import REPO, TOOL_LAYER, CACHE, CARGO, write  # noqa: E402
from app_builder import MacSandboxCargoRunner, _validate_cargo_manifest  # noqa: E402
from common import ProcessLimits, run_bounded  # noqa: E402
from descriptor_reconciliation import validated_component_inspector, run_guest_descriptor_reconciliation  # noqa: E402
from verifier import WORLD_IMPORTS, _run_wasm_checks  # noqa: E402

FROZEN_SUPPORT = "1f100bd5e8da98fcd304ed1955bc8c31299ef6f9b58164cf13986bea0f2dc2a5"
FROZEN_GUIDE = "766c9b6e0996cd4cb7c3b44d61fbdc55b88bf51d0f594aa32668327de9a60ec4"
APP_ID = "example.vibapp.reclaiming-smoke"
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "TZ": "UTC", "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}

def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

def check_frozen() -> bytes:
    support = (STARTER / "vibapp_support.rs").read_bytes()
    if digest(support) != FROZEN_SUPPORT or digest((STARTER / "README.md").read_bytes()) != FROZEN_GUIDE:
        raise AssertionError("frozen support/guide bytes changed")
    return support

def drive_runtime(output: Path) -> None:
    executable, _ = validated_component_inspector()
    component = output / "component.wasm"
    component_digest = digest(component.read_bytes())
    arguments = [str(executable), "--component", str(component), "--expected-sha256", component_digest,
        "--package-digest-sha256", component_digest, "--app-id", APP_ID, "--app-version", "0.1.0",
        "--entrypoint", "main", "--entrypoint-kind", "launcher-ui", "--instance", "synthetic-instance",
        "--generation", "synthetic-generation", "--world", "ui-only-reference",
        "--state-directory", str(output / "runtime-state"), "--state-revision", "1", "--kv-enabled", "false",
        "--kv-maximum-bytes", "4096", "--settings-snapshot", str(output / "settings-v1.json")]
    binding = {"session": "synthetic-session", "surface": "synthetic-surface", "route": "home"}
    commands = [{"request_id": "validate", "command": "validate"}, {"request_id": "launch", "command": "ui-launch", **binding}]
    for index in range(128):
        commands.append({"request_id": f"health-{index:04}", "command": "health"})
        for ordinal in range(4):
            commands.append({"request_id": f"refresh-{index:04}-{ordinal}", "command": "ui-refresh", "event_id": f"refresh-{index:04}-{ordinal}", **binding})
    result = subprocess.run(arguments, input=b"".join((json.dumps(command) + "\n").encode() for command in commands),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, env=ENVIRONMENT, check=False)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    if result.returncode != 0:
        sys.stderr.write("\nLast synthetic runtime output:\n" + result.stdout[-8192:].decode("utf-8", errors="replace"))
        raise RuntimeError(f"fixed runtime returned {result.returncode}")

def run(output: Path, inspect_existing: bool = False) -> dict:
    frozen = check_frozen()
    if not output.is_absolute() or REPO / "artifacts" not in output.parents:
        raise ValueError("new absolute --output must be below repository artifacts/")
    proposal = (BASE / "reclaiming_support.rs").read_bytes()
    marker = b"pub fn error(message: &str) -> common::AppError {"
    if frozen.count(marker) != 1:
        raise AssertionError("frozen helper split is ambiguous")
    assembled = proposal + b"\n" + frozen[frozen.index(marker):]
    limits = ProcessLimits(wall_seconds=180, cpu_seconds=120, memory_bytes=2 * 1024**3, pids=32, disk_bytes=512 * 1024**2)
    if not inspect_existing:
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        source = output / "source"
        write(source / "Cargo.toml", CARGO.replace("vibapp-support-smoke", "vibapp-reclaiming-smoke").encode())
        write(source / "src/lib.rs", (BASE / "reclaiming_lib.rs").read_bytes())
        write(source / "src/vibapp_support.rs", assembled)
        write(source / "wit/contract.wit", (REPO / "wit/experimental-v0/contract.wit").read_bytes())
        _validate_cargo_manifest(source / "Cargo.toml")
        runner = MacSandboxCargoRunner(TOOL_LAYER, CACHE / "cargo-home", CACHE / "acceptance.json")
        built = runner.execute(source, output, limits)
        write(output / "component.wasm", built.component_bytes)
        write(output / "Cargo.lock", built.cargo_lock_bytes)
        write(output / "build.stdout", built.stdout)
        write(output / "build.stderr", built.stderr)
        write(output / "build-observation.json", json.dumps({"tool_versions": built.tool_versions,
            "isolation": built.isolation, "limits": limits.as_dict(), "resource_observations": built.resource_observations}, indent=2).encode())
    else:
        if (output / "source/src/vibapp_support.rs").read_bytes() != assembled or (output / "source/src/lib.rs").read_bytes() != (BASE / "reclaiming_lib.rs").read_bytes():
            raise AssertionError("existing compiled source snapshot differs")
    component = output / "component.wasm"
    component_digest = digest(component.read_bytes())
    manifest = {
        "app": {"id": APP_ID, "version": "0.1.0", "kind": "ui", "display_name": "Reclaiming Smoke"},
        "entrypoints": [{"id": "main", "kind": "launcher-ui", "label": "Reclaiming Smoke", "routes": {"initial": "home"}}],
        "runtime": {"world": "ui-only-reference", "required_imports": WORLD_IMPORTS["ui-only-reference"]},
        "artifacts": {"canonical_component": {"sha256": component_digest}},
    }
    checks = _run_wasm_checks(component, manifest, TOOL_LAYER / "bin/wasm-tools")
    descriptor = run_guest_descriptor_reconciliation(component, manifest)
    (output / "runtime-state").mkdir(exist_ok=True)
    settings = {"schema_version": "vibapp.settings-snapshot.experimental-v1", "app_id": APP_ID,
        "package_digest_sha256": component_digest, "generation": "synthetic-generation", "state_revision": 1,
        "schema_revision": 1, "config_revision": 1, "values": []}
    if not (output / "settings-v1.json").exists():
        write(output / "settings-v1.json", json.dumps(settings).encode())
    runtime_limits = ProcessLimits(wall_seconds=60, cpu_seconds=45, memory_bytes=1024**3, pids=8,
        disk_bytes=64 * 1024**2, stdout_bytes=4 * 1024**2, stderr_bytes=128 * 1024, open_files=128)
    runtime = run_bounded([sys.executable, str(Path(__file__).resolve()), "--drive-runtime", str(output)],
        cwd=output, environment=ENVIRONMENT, limits=runtime_limits, disk_root=output)
    rows = [json.loads(line) for line in runtime.stdout.splitlines()]
    if len(rows) != 643 or rows[0].get("tag") != "ready":
        raise AssertionError(f"unexpected persistent runtime response count/framing: {len(rows)}")
    health = []
    rendered = 0
    for row in rows[1:]:
        outcome = row.get("outcome", {})
        tag = outcome.get("tag")
        if tag == "health":
            message = outcome["report"]["checks"][0]["message"]
            matched = re.fullmatch(r"cycles=(\d+);free=(\d+);blocks=(\d+);mapped=(\d+)", message)
            if matched is None or outcome["report"]["status"] != "healthy":
                raise AssertionError(f"unexpected health proof {outcome}")
            health.append(tuple(map(int, matched.groups())))
        elif tag == "ui-updated":
            rendered += 1
        elif tag != "validated":
            raise AssertionError(f"runtime check failed: {row}")
    if len(health) != 128 or rendered != 513:
        raise AssertionError("missing health or actual UI refresh calls")
    if [row[0] for row in health] != [1024 * (index + 1) for index in range(128)]:
        raise AssertionError("allocator cycles did not persist in one Store")
    if len({row[3] for row in health}) != 1 or max(row[1] for row in health) - min(row[1] for row in health) > 1024:
        raise AssertionError("heap grew or leaked across persistent canonical ABI calls")
    check_frozen()
    evidence = {"state": "proposal-local-smoke-passed-not-integrated-not-release-acceptance", "scope": "synthetic-infrastructure-only",
        "component_sha256": component_digest, "component_bytes": component.stat().st_size,
        "proposal_sha256": digest(proposal), "assembled_support_sha256": digest(assembled),
        "frozen_support_unchanged_sha256": FROZEN_SUPPORT, "frozen_guide_unchanged_sha256": FROZEN_GUIDE,
        "checks": checks, "descriptor_check": descriptor,
        "guest_edge_checks": ["1024-live-block fragmentation/coalescing", "alignments 1..4096", "bounded exhaustion and recovery",
            "failed realloc preserves old allocation", "near-whole-heap allocation after coalescing", "canonical grow/shrink/free", "Vec/String lifetime"],
        "persistent_store": {"health_calls": len(health), "allocation_reallocation_free_cycles": health[-1][0],
            "additional_canonical_allocate_grow_free_cycles": 128 * 64, "ui_launches": 1, "ui_refreshes": rendered - 1,
            "free_bytes_min": min(row[1] for row in health), "free_bytes_max": max(row[1] for row in health), "mapped_heap_bytes": health[0][3]},
        "build_observation": json.loads((output / "build-observation.json").read_text()),
        "runtime_observation": {"elapsed_ms": runtime.elapsed_ms, "peak_rss_bytes": runtime.peak_rss_bytes, "limits": runtime_limits.as_dict()},
        "install": False, "publish": False, "active_support_changed": False, "reproducibility": "one build only; not checked"}
    for name, payload in (("runtime.jsonl", runtime.stdout), ("runtime.stderr", runtime.stderr), ("evidence.json", (json.dumps(evidence, indent=2) + "\n").encode())):
        if not (output / name).exists():
            write(output / name, payload)
    return evidence

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect-existing", action="store_true")
    parser.add_argument("--drive-runtime", type=Path)
    args = parser.parse_args()
    if args.drive_runtime is not None:
        drive_runtime(args.drive_runtime)
    elif args.output is not None:
        print(json.dumps(run(args.output, args.inspect_existing), indent=2))
    else:
        parser.error("--output is required")
