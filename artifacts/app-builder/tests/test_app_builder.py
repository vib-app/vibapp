from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


BASE = Path(__file__).resolve().parents[1]
REPO = BASE.parents[1]
sys.path.insert(0, str(BASE))

from app_builder import (  # noqa: E402
    SafeFixtureRunner,
    _capability,
    _entrypoint,
    _validate_cargo_manifest,
    build_handoff,
    validate_handoff,
)
from common import (  # noqa: E402
    PipelineError,
    ProcessLimits,
    canonical_json,
    derive_host_presentation,
    run_bounded,
    sha256_bytes,
    source_tree_digest,
)
from verifier import (  # noqa: E402
    WASM_TOOLS_SHA256,
    _validate_manifest_schema,
    verifier_preflight,
    verify_and_promote,
)


COMPONENT = REPO / "artifacts/desktop/runtime-apps/hello/component.wasm"
CONTRACT = REPO / "wit/experimental-v0/contract.wit"


def write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def make_handoff(
    root: Path,
    job_id: str = "builder-test-job",
    *,
    cargo_name: str = "ai_vibapp_hello",
    cargo_version: str = "0.1.0",
) -> Path:
    source = root / "source"
    source.mkdir(parents=True)
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
    cargo = cargo.replace(b'ai_vibapp_hello', cargo_name.encode("ascii"), 1)
    cargo = cargo.replace(b'0.1.0', cargo_version.encode("ascii"), 1)
    rust = b'''#![no_std]
extern crate alloc;
use core::alloc::{GlobalAlloc, Layout};
use core::panic::PanicInfo;
struct Allocator;
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, _layout: Layout) -> *mut u8 { core::ptr::null_mut() }
    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {}
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! { loop { core::hint::spin_loop(); } }
wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" });
pub const SAFE_FIXTURE: &str = "source is not executed by the safe fixture runner";
'''
    write(source / "Cargo.toml", cargo)
    write(source / "src/lib.rs", rust)
    write(source / "wit/contract.wit", CONTRACT.read_bytes())
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
    handoff = {
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
            "description": "A safe product Builder integration fixture.",
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
    path = root / "handoff.json"
    write(path, canonical_json(handoff) + b"\n")
    return path


class BuilderVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-app-builder-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, job_id: str = "builder-test-job", *, component: bytes | None = None, fabricated: bool = False):
        handoff = make_handoff(self.root / f"input-{job_id}", job_id)
        output = self.root / f"output-{job_id}"
        receipt = build_handoff(
            handoff,
            output,
            SafeFixtureRunner(component if component is not None else COMPONENT.read_bytes(), fabricated_success=fabricated),
        )
        return handoff, output, receipt

    def test_safe_handoff_reaches_quarantine_then_exact_candidate(self) -> None:
        handoff, output, receipt = self.build()
        handoff_value = json.loads(handoff.read_text(encoding="utf-8"))
        self.assertEqual(
            handoff_value["schema_version"],
            "vibapp.codeagent-source-handoff.experimental-v2",
        )
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt_value["schema_version"],
            "vibapp.builder-quarantine.experimental-v2",
        )
        self.assertEqual(receipt_value["state"], "quarantined-awaiting-verifier")
        self.assertEqual(receipt_value["authority"]["builder"], "quarantine-only")
        self.assertEqual(
            receipt_value["required_capabilities"],
            [
                "vibapp:experimental-v0/kv@0.0.1",
                "vibapp:experimental-v0/settings@0.0.1",
            ],
        )
        self.assertEqual(
            receipt_value["package_entrypoints"],
            [
                {
                    "id": "main",
                    "kind": "launcher-ui",
                    "label": "Hello VibApp",
                    "initial_route": "home",
                    "triggers": [],
                }
            ],
        )
        self.assertEqual(receipt_value["presentation"]["preferred_width"], 900)
        self.assertEqual(receipt_value["presentation"]["preferred_height"], 640)
        self.assertFalse((output / "candidates").exists())

        candidate = verify_and_promote(receipt, output)
        value = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "candidate-ready")
        self.assertEqual(value["authority"], {"install": "daemon", "publish": "none"})
        self.assertEqual(value["presentation"], receipt_value["presentation"])
        self.assertEqual(candidate.parent.name, value["package_digest_sha256"])
        component = candidate.parent / "package/component.wasm"
        self.assertEqual(hashlib.sha256(component.read_bytes()).hexdigest(), value["component"]["sha256"])
        self.assertTrue(all(item["outcome"] == "pass" for item in value["verification"]["checks"]))
        self.assertIn(
            "guest-descriptor-reconciliation",
            {item["id"] for item in value["verification"]["checks"]},
        )

    def test_historical_v1_handoff_without_required_capabilities_fails_closed(self) -> None:
        handoff = make_handoff(self.root / "legacy-v1-handoff", "legacy-v1-handoff-job")
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["schema_version"] = "vibapp.codeagent-source-handoff.experimental-v1"
        del value["target"]["required_capabilities"]
        write(handoff, canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "source handoff constants are unsupported"):
            validate_handoff(handoff)

    def test_docker_handoff_requires_observed_bound_container_receipt_metadata(self):
        handoff = make_handoff(self.root / "docker-handoff", "docker-handoff-job")
        value = json.loads(handoff.read_bytes())
        provider = {
            "mode": "docker-local-explicit-opt-in", "adapter": "docker-codeagent-launcher", "provider_id": "codex",
            "external_request_attempted": True, "external_request_observed": True,
            "gateway_request_id": "vibapp-test-container", "runner_id": "vibapp-test-container",
            "isolation_policy_version": "vibapp.docker-source-v1",
            **{key: "a" * 64 for key in ("executable_sha256", "runner_identity_sha256", "isolation_policy_sha256", "receipt_digest_sha256")},
        }
        for provider_id in ("codex", "opencode"):
            provider["provider_id"] = provider_id
            value["provider_execution"] = provider
            write(handoff, canonical_json(value) + b"\n")
            validate_handoff(handoff)
            for key, invalid in [("external_request_observed", False), ("gateway_request_id", "different-container"),
                                 ("receipt_digest_sha256", None), ("isolation_policy_version", "host-process-only"),
                                 ("provider_id", "claude-code"), ("provider_id", "unknown")]:
                with self.subTest(provider=provider_id, key=key, invalid=invalid):
                    value["provider_execution"] = {**provider, key: invalid}
                    write(handoff, canonical_json(value) + b"\n")
                    with self.assertRaises(PipelineError):
                        validate_handoff(handoff)

    def test_historical_v1_receipt_without_capability_bindings_fails_closed(self) -> None:
        _, output, receipt = self.build("legacy-v1-receipt-job")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["schema_version"] = "vibapp.builder-quarantine.experimental-v1"
        del value["required_capabilities"]
        del value["package_entrypoints"]
        receipt.chmod(0o600)
        write(receipt, canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "quarantine receipt fields mismatch"):
            verify_and_promote(receipt, output)

    def test_verifier_rejects_guest_descriptor_manifest_mismatch(self) -> None:
        handoff = make_handoff(
            self.root / "descriptor-mismatch-input", "descriptor-mismatch-job"
        )
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["package_intent"]["entrypoints"][0]["label"] = "Manifest-only label"
        write(handoff, canonical_json(value) + b"\n")
        output = self.root / "descriptor-mismatch-output"
        receipt = build_handoff(
            handoff,
            output,
            SafeFixtureRunner(COMPONENT.read_bytes()),
        )

        with self.assertRaisesRegex(PipelineError, "entrypoint main differs"):
            verify_and_promote(receipt, output)

        self.assertEqual(list((output / "candidates").iterdir()), [])
        decisions = list((output / "verifier-decisions").glob("*/decision.json"))
        self.assertEqual(len(decisions), 1)
        decision = json.loads(decisions[0].read_text(encoding="utf-8"))
        self.assertEqual(decision["error"]["code"], "incompatible-contract")

    def test_verifier_preflight_uses_the_same_fixed_wasm_tools_check(self) -> None:
        ready = verifier_preflight()
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(ready["source_executed"])
        self.assertEqual(ready["network"], "none")
        self.assertEqual(ready["wasm_tools_sha256"], WASM_TOOLS_SHA256)
        self.assertEqual(ready["expected_wasm_tools_sha256"], WASM_TOOLS_SHA256)
        self.assertEqual(
            ready["component_inspector_sha256"],
            ready["expected_component_inspector_sha256"],
        )
        self.assertFalse(ready["descriptor_live_host_effects"])

        wrong = self.root / "wrong-wasm-tools"
        wrong.write_bytes(b"not the accepted verifier executable")
        wrong.chmod(0o700)
        with self.assertRaisesRegex(PipelineError, "accepted local tool layer"):
            verifier_preflight(wrong)

    def test_compact_requirement_gets_bounded_host_presentation(self) -> None:
        presentation = derive_host_presentation(
            "ui", "LED 数字时钟", "实时显示小时、分钟、秒并响应窗口变化。"
        )
        self.assertEqual(
            presentation,
            {
                "schema_version": "vibapp.host-presentation.experimental-v1",
                "preferred_width": 520,
                "preferred_height": 300,
                "minimum_width": 420,
                "minimum_height": 240,
                "resizable": True,
            },
        )

    def test_builder_separates_app_necessity_from_current_host_truth(self) -> None:
        expected = {
            "scheduler": ("user", "unavailable"),
            "notification": ("user", "unavailable"),
            "system-metrics": ("user", "unavailable"),
            "http": ("user", "unavailable"),
        }
        for short, (grant, availability) in expected.items():
            with self.subTest(interface=short):
                interface = f"vibapp:experimental-v0/{short}@0.0.1"
                capability = _capability(
                    interface,
                    ["desktop", "headless"],
                    {interface},
                )
                self.assertEqual(capability["necessity"], "required")
                self.assertEqual(capability["grant"], grant)
                self.assertEqual(
                    [row["availability"] for row in capability["profiles"]],
                    [availability, availability],
                )
                structural_only = _capability(
                    interface,
                    ["desktop", "headless"],
                    set(),
                )
                self.assertEqual(structural_only["necessity"], "degradable")
                self.assertEqual(
                    [row["availability"] for row in structural_only["profiles"]],
                    ["denied", "denied"],
                )
        clock = _capability(
            "vibapp:experimental-v0/clock@0.0.1",
            ["desktop"],
            {"vibapp:experimental-v0/clock@0.0.1"},
        )
        self.assertEqual(clock["profiles"][0]["availability"], "brokered")

    def test_service_entrypoint_manifest_uses_bound_trigger_intent(self) -> None:
        explicit = _entrypoint(
            {
                "id": "probe",
                "kind": "service",
                "label": "Health Probe",
                "initial_route": None,
                "triggers": ["on-enable", "manual"],
            },
            ["desktop"],
        )
        self.assertEqual(explicit["triggers"], ["on-enable", "manual"])

        legacy = _entrypoint(
            {
                "id": "legacy",
                "kind": "service",
                "label": "Legacy Service",
                "initial_route": None,
            },
            ["desktop"],
        )
        self.assertEqual(legacy["triggers"], ["manual"])

        handoff = make_handoff(self.root / "ui-trigger-input", "ui-trigger-job")
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["package_intent"]["entrypoints"][0]["triggers"] = ["manual"]
        write(handoff, canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "only service entrypoints"):
            validate_handoff(handoff)

    def test_verifier_rejects_capability_claim_stronger_than_local_host(self) -> None:
        _, output, receipt = self.build("capability-truth-job")
        manifest = json.loads(
            (receipt.parent / "package/manifest.json").read_text(encoding="utf-8")
        )
        _validate_manifest_schema(manifest)
        clock = next(
            capability
            for capability in manifest["capabilities"]
            if capability["interface"]
            == "vibapp:experimental-v0/clock@0.0.1"
        )
        clock["profiles"][0]["availability"] = "native"
        with self.assertRaisesRegex(PipelineError, "contradicts"):
            _validate_manifest_schema(manifest)
        clock["profiles"][0]["availability"] = "denied"
        clock["necessity"] = "required"
        with self.assertRaisesRegex(PipelineError, "contradicts"):
            _validate_manifest_schema(manifest)

    def test_legacy_receipt_without_presentation_gets_safe_derived_candidate(self) -> None:
        _, output, receipt = self.build("legacy-presentation-job")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        del value["presentation"]
        receipt.chmod(0o600)
        receipt.write_bytes(canonical_json(value) + b"\n")
        candidate = verify_and_promote(receipt, output)
        promoted = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertEqual(promoted["presentation"]["preferred_width"], 900)
        self.assertEqual(promoted["presentation"]["minimum_height"], 360)

    def test_builder_cannot_forge_host_presentation_authority(self) -> None:
        _, output, receipt = self.build("forged-presentation-job")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["presentation"]["preferred_width"] = 1920
        receipt.chmod(0o600)
        receipt.write_bytes(canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "presentation differs"):
            verify_and_promote(receipt, output)

    def test_verifier_rejects_required_capability_drift_from_receipt(self) -> None:
        _, output, receipt = self.build("capability-binding-job")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["required_capabilities"].append(
            "vibapp:experimental-v0/clock@0.0.1"
        )
        receipt.chmod(0o600)
        write(receipt, canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "capability necessity differs"):
            verify_and_promote(receipt, output)

    def test_verifier_rejects_service_trigger_drift_from_receipt(self) -> None:
        handoff = make_handoff(self.root / "trigger-binding-input", "trigger-binding-job")
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["package_intent"]["entrypoints"] = [
            {
                "id": "worker",
                "kind": "service",
                "label": "Background worker",
                "initial_route": None,
                "triggers": ["scheduler", "manual"],
            }
        ]
        value["target"].update(
            {
                "wit_world": "service-only-reference",
                "app_kind": "service",
                "profiles": ["desktop", "headless"],
                "required_imports": [
                    "vibapp:experimental-v0/clock@0.0.1",
                    "vibapp:experimental-v0/scheduler@0.0.1",
                    "vibapp:experimental-v0/kv@0.0.1",
                    "vibapp:experimental-v0/log@0.0.1",
                    "vibapp:experimental-v0/host-info@0.0.1",
                    "vibapp:experimental-v0/settings@0.0.1",
                    "vibapp:experimental-v0/system-metrics@0.0.1",
                    "vibapp:experimental-v0/http@0.0.1",
                ],
            }
        )
        write(handoff, canonical_json(value) + b"\n")
        output = self.root / "trigger-binding-output"
        receipt = build_handoff(
            handoff,
            output,
            SafeFixtureRunner(COMPONENT.read_bytes()),
        )
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt_value["package_entrypoints"][0]["triggers"],
            ["scheduler", "manual"],
        )
        receipt_value["package_entrypoints"][0]["triggers"] = ["manual"]
        receipt.chmod(0o600)
        write(receipt, canonical_json(receipt_value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "entrypoints or triggers differ"):
            verify_and_promote(receipt, output)

    def test_output_swap_after_build_prevents_promotion(self) -> None:
        _, output, receipt = self.build("tamper-job")
        component = receipt.parent / "package/component.wasm"
        component.chmod(0o600)
        component.write_bytes(component.read_bytes() + b"tamper")
        with self.assertRaisesRegex(PipelineError, "quarantine bytes changed"):
            verify_and_promote(receipt, output)
        self.assertEqual(list((output / "candidates").iterdir()), [])
        decisions = list((output / "verifier-decisions").glob("*/decision.json"))
        self.assertEqual(len(decisions), 1)
        decision = json.loads(decisions[0].read_text(encoding="utf-8"))
        self.assertEqual(decision["state"], "rejected")
        self.assertFalse(decision["candidate_created"])
        self.assertEqual(decision["authority"]["install"], "none")

    def test_fabricated_success_text_cannot_promote_invalid_bytes(self) -> None:
        _, output, receipt = self.build(
            "fabricated-success-job", component=b"not a wasm component", fabricated=True
        )
        log = (receipt.parent.parent / "build.log.json").read_text(encoding="utf-8")
        self.assertIn("repository says tests passed", log)
        with self.assertRaises(PipelineError):
            verify_and_promote(receipt, output)
        self.assertEqual(list((output / "candidates").iterdir()), [])
        decisions = list((output / "verifier-decisions").glob("*/decision.json"))
        self.assertEqual(len(decisions), 1)
        self.assertIn(
            json.loads(decisions[0].read_text(encoding="utf-8"))["error"]["code"],
            {"process-failed", "malformed-output", "incompatible-contract"},
        )

    def test_path_escape_in_source_inventory_is_rejected(self) -> None:
        handoff = make_handoff(self.root / "escape-input", "escape-job")
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["files"][0]["path"] = "../escape"
        write(handoff, canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "unsafe segment"):
            validate_handoff(handoff)

    def test_symlink_source_is_rejected(self) -> None:
        handoff = make_handoff(self.root / "symlink-input", "symlink-job")
        source = handoff.parent / "source/src/lib.rs"
        outside = self.root / "outside.rs"
        write(outside, b"outside")
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(PipelineError):
            validate_handoff(handoff)

    def test_browser_profile_fails_closed_without_derivation(self) -> None:
        handoff = make_handoff(self.root / "browser-input", "browser-job")
        value = json.loads(handoff.read_text(encoding="utf-8"))
        value["target"]["profiles"] = ["desktop", "web-runtime"]
        write(handoff, canonical_json(value) + b"\n")
        with self.assertRaisesRegex(PipelineError, "trusted derivation"):
            validate_handoff(handoff)

    def test_provider_identity_must_match_the_recorded_adapter_mode(self) -> None:
        cases = [
            {"provider_id": "codex"},
            {"adapter": "local-codeagent-adapter", "provider_id": "claude-code"},
            {"external_request_attempted": True},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                handoff = make_handoff(
                    self.root / f"provider-evidence-{index}", f"provider-evidence-job-{index}"
                )
                value = json.loads(handoff.read_text(encoding="utf-8"))
                value["provider_execution"].update(changes)
                write(handoff, canonical_json(value) + b"\n")
                with self.assertRaisesRegex(PipelineError, "provider execution"):
                    validate_handoff(handoff)

    def test_codeagent_source_policy_requires_no_std_alloc_boundary(self) -> None:
        handoff = make_handoff(self.root / "no-std-input", "no-std-job")
        cargo = handoff.parent / "source/Cargo.toml"
        library = handoff.parent / "source/src/lib.rs"
        valid_cargo = cargo.read_text(encoding="utf-8")
        valid_library = library.read_text(encoding="utf-8")
        _validate_cargo_manifest(cargo)

        cargo.write_text(
            valid_cargo.replace('"realloc"]', '"realloc", "std"]'), encoding="utf-8"
        )
        with self.assertRaisesRegex(PipelineError, "wit-bindgen policy"):
            _validate_cargo_manifest(cargo)
        cargo.write_text(valid_cargo, encoding="utf-8")

        library.write_text(valid_library.replace("#![no_std]", ""), encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "no_std/alloc"):
            _validate_cargo_manifest(cargo)

    def test_cargo_package_identity_is_bound_to_package_intent(self) -> None:
        for label, options in (
            ("name", {"cargo_name": "unrelated_package"}),
            ("version", {"cargo_version": "9.9.9"}),
        ):
            with self.subTest(field=label):
                handoff = make_handoff(
                    self.root / f"cargo-{label}-input",
                    f"cargo-{label}-job",
                    **options,
                )
                with self.assertRaisesRegex(PipelineError, "do not match package_intent"):
                    validate_handoff(handoff)

    def test_duplicate_json_key_is_rejected(self) -> None:
        handoff = make_handoff(self.root / "duplicate-input", "duplicate-job")
        raw = handoff.read_text(encoding="utf-8")
        handoff.write_text(raw.replace('{"authority":', '{"job_id":"shadow","authority":', 1), encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "duplicate JSON key"):
            validate_handoff(handoff)

    def test_bounded_process_stops_log_flood(self) -> None:
        workspace = self.root / "log-workspace"
        workspace.mkdir()
        limits = ProcessLimits(
            wall_seconds=3,
            cpu_seconds=2,
            memory_bytes=256 * 1024 * 1024,
            pids=4,
            disk_bytes=1024 * 1024,
            stdout_bytes=1024,
            stderr_bytes=1024,
            open_files=32,
        )
        with self.assertRaisesRegex(PipelineError, "stdout exceeded"):
            run_bounded(
                ["/usr/bin/yes", "bounded"],
                cwd=workspace,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                limits=limits,
                disk_root=workspace,
            )

    def test_bounded_process_stops_timeout(self) -> None:
        workspace = self.root / "timeout-workspace"
        workspace.mkdir()
        limits = ProcessLimits(
            wall_seconds=1,
            cpu_seconds=1,
            memory_bytes=256 * 1024 * 1024,
            pids=4,
            disk_bytes=1024 * 1024,
            stdout_bytes=1024,
            stderr_bytes=1024,
            open_files=32,
        )
        with self.assertRaisesRegex(PipelineError, "process exceeded 1s"):
            run_bounded(
                ["/bin/sleep", "3"],
                cwd=workspace,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                limits=limits,
                disk_root=workspace,
            )

    def test_candidate_and_quarantine_schemas_parse(self) -> None:
        for path in sorted((BASE / "schemas").glob("*.json")):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()
