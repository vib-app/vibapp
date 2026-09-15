from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def test_artifact(environment: str, relative: str) -> Path:
    return Path(os.environ.get(environment, str(ROOT / relative)))


SERVICE_COMPONENT = test_artifact("VIBAPP_TEST_SERVICE_COMPONENT", "target-fixture-service-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
HYBRID_COMPONENT = test_artifact("VIBAPP_TEST_HYBRID_COMPONENT", "target-fixture-hybrid-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
HYBRID_V2_COMPONENT = test_artifact("VIBAPP_TEST_HYBRID_V2_COMPONENT", "target-fixture-hybrid-v2-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
HYBRID_V2_UNHEALTHY_COMPONENT = test_artifact("VIBAPP_TEST_HYBRID_V2_UNHEALTHY_COMPONENT", "target-fixture-hybrid-v2-unhealthy-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
HYBRID_V2_MIGRATION_FAIL_COMPONENT = test_artifact("VIBAPP_TEST_HYBRID_V2_MIGRATION_FAIL_COMPONENT", "target-fixture-hybrid-v2-migration-fail-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
UI_COMPONENT = test_artifact("VIBAPP_TEST_UI_COMPONENT", "target-fixture-ui-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
UI_V2_COMPONENT = test_artifact("VIBAPP_TEST_UI_V2_COMPONENT", "target-fixture-ui-v2-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
HYBRID_APP_B_COMPONENT = test_artifact("VIBAPP_TEST_HYBRID_APP_B_COMPONENT", "target-fixture-hybrid-app-b-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
HYBRID_V2_APP_B_COMPONENT = test_artifact("VIBAPP_TEST_HYBRID_V2_APP_B_COMPONENT", "target-fixture-hybrid-v2-app-b-1_93/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm")
SERVICE_RUNTIME = test_artifact("VIBAPP_TEST_SERVICE_RUNTIME", "target-service-1_98/release/vibapp-service-runtime")
KV_SCHEMA = "vibapp.kv-state.experimental-v1"
sys.path.insert(0, str(ROOT))

from vibapp_daemon.core import (  # noqa: E402
    CANDIDATE_SCHEMA,
    ENABLE_SCHEMA,
    DaemonError,
    LOCAL_HOST_CAPABILITY_AVAILABILITY,
    PRESENTATION_SCHEMA,
    TRANSPORT_SCHEMA,
    RuntimeDaemon,
    canonical_json,
)


class FrozenClock:
    def __init__(self) -> None:
        self.value = "2026-08-27T00:00:00Z"

    def __call__(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FrozenMonotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def descriptor(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "media_type": "application/octet-stream", "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def package_digest(manifest: dict[str, Any], descriptors: list[dict[str, Any]]) -> str:
    encoded = canonical_json(manifest)
    preimage = bytearray(b"VIBAPP-PACKAGE\x00experimental-v0\x00")
    preimage.extend(struct.pack(">Q", len(encoded)))
    preimage.extend(encoded)
    for item in sorted(descriptors, key=lambda row: row["path"].encode()):
        path = item["path"].encode()
        preimage.extend(struct.pack(">H", len(path)))
        preimage.extend(path)
        preimage.extend(bytes.fromhex(item["sha256"]))
        preimage.extend(struct.pack(">Q", item["size_bytes"]))
    return hashlib.sha256(preimage).hexdigest()


def write_profile_state(directory: Path, *, revision: int = 1) -> bytes:
    document = {
        "schema_version": KV_SCHEMA,
        "state_revision": revision,
        "entries": {
            "profile/name": {"value": list(b"alice"), "revision": revision},
        },
    }
    encoded = canonical_json(document) + b"\n"
    (directory / "kv-state.json").write_bytes(encoded)
    return encoded


def read_kv_state(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "kv-state.json").read_bytes())


def snapshot_state(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def create_candidate(
    root: Path,
    app_id: str = "ai.vibapp.runtime-service-fixture",
    *,
    kind: str = "hybrid",
    version: str = "0.1.0",
    state_schema: int = 1,
    migratable_from_min: int = 1,
    migratable_from_max: int | None = None,
    component_override: Path | None = None,
    display_name: str = "Daemon Test",
    description: str = "synthetic",
    presentation: dict[str, Any] | None = None,
    capabilities_override: list[dict[str, Any]] | None = None,
    service_triggers: dict[str, list[str]] | None = None,
) -> tuple[Path, str]:
    component_path = component_override or (
        UI_V2_COMPONENT
        if kind == "ui" and version == "0.2.0"
        else UI_COMPONENT
        if kind == "ui"
        else SERVICE_COMPONENT
        if kind == "service"
        else HYBRID_V2_APP_B_COMPONENT
        if app_id.endswith("fixture-b") and version == "0.2.0"
        else HYBRID_APP_B_COMPONENT
        if app_id.endswith("fixture-b")
        else HYBRID_V2_COMPONENT
        if version == "0.2.0"
        else HYBRID_COMPONENT
    )
    if not component_path.is_file():
        raise RuntimeError("build the bounded runtime fixtures before running daemon tests")
    component = component_path.read_bytes()
    provenance = b'{"source":"synthetic-test"}\n'
    sbom = b'{"bomFormat":"CycloneDX","specVersion":"1.5"}\n'
    component_descriptor = descriptor("component.wasm", component)
    provenance_descriptor = descriptor("provenance.json", provenance)
    sbom_descriptor = descriptor("sbom.cdx.json", sbom)
    entries: list[dict[str, Any]] = []
    if kind in ("ui", "hybrid"):
        entries.append({"id": "main-ui", "kind": "launcher-ui", "label": "Main", "profiles": ["desktop"], "routes": {"initial": "home", "allowed": ["home"]}, "restoration": "route-only"})
    if kind in ("service", "hybrid"):
        service_triggers = service_triggers or {}
        entries.extend([
            {"id": "main-service", "kind": "service", "label": "Service", "profiles": ["desktop"], "triggers": service_triggers.get("main-service", ["manual"]), "health_check_interval_seconds": 60},
            {"id": "other-service", "kind": "service", "label": "Other", "profiles": ["desktop"], "triggers": service_triggers.get("other-service", ["manual"]), "health_check_interval_seconds": 60},
        ])
    capabilities = capabilities_override or [{
        "interface": "vibapp:experimental-v0/kv@0.0.1",
        "necessity": "required",
        "grant": "automatic",
        "reason": "Exercise isolated transactional state migration.",
        "scope": {"maximum_storage_bytes": 1048576},
        "profiles": [{"profile": "desktop", "availability": "native", "behavior": "App-scoped daemon state."}],
    }]
    manifest = {
        "schema_version": "vibapp.manifest.experimental-v0.0.1",
        "package_format": "vibapp.package.experimental-v0",
        "app": {"id": app_id, "version": version, "kind": kind, "display_name": display_name, "description": description, "publisher": {"id": "ai.vibapp.tests", "display_name": "Tests"}},
        "artifacts": {"canonical_component": component_descriptor, "assets": [], "browser_derivations": [], "provenance": provenance_descriptor, "sbom": sbom_descriptor},
        "runtime": {"contract": "vibapp:experimental-v0@0.0.1", "wasi": "0.2", "world": {"ui": "ui-only-reference", "service": "service-only-reference", "hybrid": "hybrid-reference"}[kind], "required_imports": [capability["interface"] for capability in capabilities], "profiles": [{"profile": "desktop", "mode": "full", "background": "daemon", "artifact_role": "canonical-component", "degradation": ""}], "platforms": [{"os": "macos", "arch": "aarch64", "profiles": ["desktop"]}]},
        "entrypoints": entries,
        "capabilities": capabilities,
        "resources": {"stored_data_bytes": 1048576},
        "state": {
            "schema": state_schema,
            "migratable_from_min": migratable_from_min,
            "migratable_from_max": migratable_from_max if migratable_from_max is not None else state_schema,
        },
        "lifecycle": {"disable": {"retains_package": True, "retains_state": True, "stops_services": True, "cancels_schedules": True}, "uninstall": {"allowed_data_dispositions": ["delete", "retain", "export-then-delete"], "default_data_disposition": "retain"}},
        "source": {},
        "license": {},
        "privacy": {},
        "verification": {},
    }
    descriptors = [component_descriptor, provenance_descriptor, sbom_descriptor]
    digest = package_digest(manifest, descriptors)
    candidate_dir = root / digest
    package_dir = candidate_dir / "package"
    package_dir.mkdir(parents=True)
    manifest_bytes = canonical_json(manifest) + b"\n"
    (package_dir / "manifest.json").write_bytes(manifest_bytes)
    (package_dir / "component.wasm").write_bytes(component)
    (package_dir / "provenance.json").write_bytes(provenance)
    (package_dir / "sbom.cdx.json").write_bytes(sbom)
    for file in package_dir.iterdir():
        os.chmod(file, 0o600)
    candidate = {
        "schema_version": CANDIDATE_SCHEMA,
        "document_type": "verifier-promoted-candidate",
        "state": "candidate-ready",
        "job_id": "job-daemon-test",
        "source_tree_sha256": "a" * 64,
        "package_digest_sha256": digest,
        "package_directory": "package",
        "component": {"path": "component.wasm", "sha256": component_descriptor["sha256"], "size_bytes": len(component)},
        "manifest": {"path": "manifest.json", "sha256": hashlib.sha256(manifest_bytes).hexdigest(), "size_bytes": len(manifest_bytes)},
        "quarantine_receipt_sha256": "b" * 64,
        "verification": {"authority": "independent-verifier", "verifier_version": "app-verifier.experimental-v1", "verified_at_utc": "2026-08-27T00:00:00Z", "checks": [{"id": "package-digest", "outcome": "pass", "tool": "test", "detail": "recomputed"}]},
        "authority": {"install": "daemon", "publish": "none"},
    }
    if presentation is not None:
        candidate["presentation"] = presentation
    record = candidate_dir / "candidate.json"
    record.write_bytes(canonical_json(candidate) + b"\n")
    os.chmod(record, 0o600)
    return record, digest


def envelope(tag: str, value: Any, *, subject: str | None, key: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    return {"request_id": request_id or f"req-{tag}", "idempotency_key": key or f"key-{tag}", "client": "cli", "subject": subject, "command": {"tag": tag, "value": value}}


class RuntimeDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.promotions = self.base / "promotions"
        self.promotions.mkdir()
        self.clock = FrozenClock()
        self.monotonic = FrozenMonotonic()
        self.record, self.digest = create_candidate(self.promotions)
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            monotonic_clock=self.monotonic,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        self.app_id = "ai.vibapp.runtime-service-fixture"

    def tearDown(self) -> None:
        self.daemon.shutdown()
        self.temp.cleanup()

    def call(self, tag: str, value: Any, *, subject: str | None = None, key: str | None = None, promotion: Path | None = None, allowed: set[str] | None = None) -> dict[str, Any]:
        if subject is None and tag != "install":
            subject = self.app_id
        return self.daemon.execute(envelope(tag, value, subject=subject, key=key), principal="uid:test", allowed_apps=allowed, promotion_record=promotion)

    def install(self, *, key: str = "install-1") -> dict[str, Any]:
        return self.call("install", {"package_digest_sha256": self.digest, "enable_after_install": False}, subject=None, key=key, promotion=self.record)

    @staticmethod
    def capability(
        short: str,
        *,
        necessity: str,
        availability: str,
        grant: str = "user",
    ) -> dict[str, Any]:
        return {
            "interface": f"vibapp:experimental-v0/{short}@0.0.1",
            "necessity": necessity,
            "grant": grant,
            "reason": f"Exercise {short} activation policy.",
            "scope": {},
            "profiles": [
                {
                    "profile": "desktop",
                    "availability": availability,
                    "behavior": "Synthetic activation-policy fixture.",
                }
            ],
        }

    @classmethod
    def kv_capability(cls) -> dict[str, Any]:
        capability = cls.capability(
            "kv", necessity="required", availability="native", grant="automatic"
        )
        capability["scope"] = {"maximum_storage_bytes": 1048576}
        return capability

    def configure_scheduler_fixture(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            capabilities_override=[
                self.kv_capability(),
                self.capability(
                    "scheduler", necessity="degradable", availability="denied"
                ),
            ],
            service_triggers={
                "main-service": ["scheduler", "manual"],
                "other-service": ["scheduler", "manual"],
            },
        )

    def test_install_requires_independent_exact_candidate_and_stages_disabled(self) -> None:
        missing = self.call("install", {"package_digest_sha256": self.digest, "enable_after_install": False}, subject=None)
        self.assertEqual(missing["error"]["code"], "consent-required")
        self.assertEqual(self.install()["outcome"]["tag"], "accepted")
        state = self.daemon.inspect_state()
        app = state["apps"][self.app_id]
        self.assertFalse(app["enabled"])
        self.assertEqual(app["lifecycle_state"], "installed-disabled")
        self.assertFalse(app["guest_execution_performed"])
        self.assertEqual(app["publisher_display_name"], "Tests")
        self.assertEqual(app["permissions"], ["vibapp:experimental-v0/kv@0.0.1"])
        self.assertEqual(
            app["presentation"],
            {
                "schema_version": PRESENTATION_SCHEMA,
                "preferred_width": 1040,
                "preferred_height": 720,
                "minimum_width": 640,
                "minimum_height": 420,
                "resizable": True,
            },
        )
        self.assertTrue((self.runtime / app["package_path"] / "component.wasm").is_file())

        status = self.call("status", None)["outcome"]["value"]
        self.assertEqual(status["presentation"], app["presentation"])
        self.assertEqual(status["publisher_display_name"], "Tests")
        self.assertEqual(status["permissions"], app["permissions"])

        tampered_record, tampered_digest = create_candidate(self.promotions, "ai.vibapp.tampered")
        tampered = json.loads(tampered_record.read_text())
        tampered["verification"]["checks"][0]["outcome"] = "fail"
        tampered_record.write_bytes(canonical_json(tampered) + b"\n")
        rejected = self.daemon.execute(envelope("install", {"package_digest_sha256": tampered_digest, "enable_after_install": False}, subject=None, key="bad-install"), principal="uid:test", promotion_record=tampered_record)
        self.assertEqual(rejected["error"]["code"], "integrity-failure")
        self.assertNotIn("ai.vibapp.tampered", self.daemon.inspect_state()["apps"])

    def test_builder_and_runtime_host_availability_tables_match(self) -> None:
        builder_common = runpy.run_path(str(ROOT.parent / "app-builder/common.py"))
        self.assertEqual(
            builder_common["LOCAL_HOST_CAPABILITY_AVAILABILITY"],
            LOCAL_HOST_CAPABILITY_AVAILABILITY,
        )

    def test_required_unavailable_capabilities_block_enable_before_guest_execution(self) -> None:
        for index, short in enumerate(
            ("scheduler", "notification", "system-metrics", "http")
        ):
            with self.subTest(interface=short):
                app_id = f"ai.vibapp.required-unavailable-{index}"
                record, digest = create_candidate(
                    self.promotions,
                    app_id,
                    capabilities_override=[
                        self.kv_capability(),
                        self.capability(
                            short,
                            necessity="required",
                            availability="unavailable",
                        ),
                    ],
                )
                installed = self.daemon.execute(
                    envelope(
                        "install",
                        {
                            "package_digest_sha256": digest,
                            "enable_after_install": False,
                        },
                        subject=None,
                        key=f"install-required-unavailable-{index}",
                    ),
                    principal="uid:test",
                    promotion_record=record,
                )
                self.assertNotIn("error", installed)
                blocked = self.daemon.execute(
                    envelope(
                        "enable",
                        None,
                        subject=app_id,
                        key=f"enable-required-unavailable-{index}",
                    ),
                    principal="uid:test",
                )
                self.assertEqual(blocked["error"]["code"], "capability-unavailable")
                app = self.daemon.inspect_state()["apps"][app_id]
                self.assertFalse(app["enabled"])
                self.assertFalse(app["guest_execution_performed"])
                self.assertEqual(app["services"], {})

    def test_degradable_unavailable_stubs_do_not_block_enable(self) -> None:
        for index, short in enumerate(("notification", "http")):
            with self.subTest(interface=short):
                app_id = f"ai.vibapp.degradable-unavailable-{index}"
                record, digest = create_candidate(
                    self.promotions,
                    app_id,
                    capabilities_override=[
                        self.kv_capability(),
                        self.capability(
                            short,
                            necessity="degradable",
                            availability="unavailable",
                        ),
                    ],
                )
                installed = self.daemon.execute(
                    envelope(
                        "install",
                        {
                            "package_digest_sha256": digest,
                            "enable_after_install": False,
                        },
                        subject=None,
                        key=f"install-degradable-unavailable-{index}",
                    ),
                    principal="uid:test",
                    promotion_record=record,
                )
                self.assertNotIn("error", installed)
                enabled = self.daemon.execute(
                    envelope(
                        "enable",
                        None,
                        subject=app_id,
                        key=f"enable-degradable-unavailable-{index}",
                    ),
                    principal="uid:test",
                )
                self.assertNotIn("error", enabled)
                self.assertTrue(self.daemon.inspect_state()["apps"][app_id]["enabled"])

    def test_live_manifest_claim_for_unavailable_host_capability_fails_closed(self) -> None:
        app_id = "ai.vibapp.false-live-scheduler"
        record, digest = create_candidate(
            self.promotions,
            app_id,
            capabilities_override=[
                self.kv_capability(),
                self.capability(
                    "scheduler", necessity="required", availability="native"
                ),
            ],
        )
        installed = self.daemon.execute(
            envelope(
                "install",
                {"package_digest_sha256": digest, "enable_after_install": False},
                subject=None,
                key="install-false-live-scheduler",
            ),
            principal="uid:test",
            promotion_record=record,
        )
        self.assertNotIn("error", installed)
        blocked = self.daemon.execute(
            envelope(
                "enable", None, subject=app_id, key="enable-false-live-scheduler"
            ),
            principal="uid:test",
        )
        self.assertEqual(blocked["error"]["code"], "incompatible-contract")
        self.assertFalse(self.daemon.inspect_state()["apps"][app_id]["enabled"])

    def test_presentation_sidecar_is_manifest_derived_bounded_and_legacy_state_migrates(self) -> None:
        compact = {
            "schema_version": PRESENTATION_SCHEMA,
            "preferred_width": 520,
            "preferred_height": 300,
            "minimum_width": 420,
            "minimum_height": 240,
            "resizable": True,
        }
        clock_record, clock_digest = create_candidate(
            self.promotions,
            "ai.vibapp.clock",
            kind="ui",
            display_name="LED 数字时钟",
            description="本地时钟。",
            presentation=compact,
        )
        installed = self.daemon.execute(
            envelope(
                "install",
                {"package_digest_sha256": clock_digest, "enable_after_install": False},
                subject=None,
                key="install-clock",
            ),
            principal="uid:test",
            promotion_record=clock_record,
        )
        self.assertEqual(installed["outcome"]["tag"], "accepted")
        self.assertEqual(
            self.daemon.inspect_state()["apps"]["ai.vibapp.clock"]["presentation"],
            compact,
        )

        invalid_record, invalid_digest = create_candidate(
            self.promotions,
            "ai.vibapp.invalid-presentation",
            kind="ui",
            presentation={**compact, "preferred_width": 320},
        )
        rejected = self.daemon.execute(
            envelope(
                "install",
                {"package_digest_sha256": invalid_digest, "enable_after_install": False},
                subject=None,
                key="install-invalid-presentation",
            ),
            principal="uid:test",
            promotion_record=invalid_record,
        )
        self.assertEqual(rejected["error"]["code"], "incompatible-contract")

        self.install(key="install-legacy-state")
        self.daemon.shutdown()
        state = json.loads((self.runtime / "state.json").read_bytes())
        legacy = state["apps"][self.app_id]
        legacy.pop("presentation")
        legacy.pop("publisher_display_name")
        legacy.pop("permissions")
        legacy["service_entrypoints"] = [
            {
                "id": entrypoint["id"],
                "kind": entrypoint["kind"],
                "initial_route": entrypoint["initial_route"],
            }
            for entrypoint in legacy["service_entrypoints"]
        ]
        (self.runtime / "state.json").write_bytes(canonical_json(state) + b"\n")
        os.chmod(self.runtime / "state.json", 0o600)
        restarted = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            monotonic_clock=self.monotonic,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        try:
            migrated = restarted.inspect_state()["apps"][self.app_id]
            self.assertEqual(migrated["presentation"]["preferred_width"], 1040)
            self.assertEqual(migrated["publisher_display_name"], "Tests")
            self.assertEqual(
                migrated["permissions"],
                ["vibapp:experimental-v0/kv@0.0.1"],
            )
            self.assertEqual(
                [entrypoint["triggers"] for entrypoint in migrated["service_entrypoints"]],
                [["manual"], ["manual"]],
            )
        finally:
            restarted.shutdown()

    def test_extra_file_and_symlink_are_rejected_before_install(self) -> None:
        record, digest = create_candidate(self.promotions, "ai.vibapp.extra")
        (record.parent / "package" / "extra.txt").write_text("extra")
        result = self.daemon.execute(envelope("install", {"package_digest_sha256": digest, "enable_after_install": False}, subject=None, key="extra"), principal="uid:test", promotion_record=record)
        self.assertEqual(result["error"]["code"], "integrity-failure")

        record2, digest2 = create_candidate(self.promotions, "ai.vibapp.linked")
        component = record2.parent / "package" / "component.wasm"
        real = record2.parent / "package" / "real-component.wasm"
        component.rename(real)
        component.symlink_to(real.name)
        result2 = self.daemon.execute(envelope("install", {"package_digest_sha256": digest2, "enable_after_install": False}, subject=None, key="linked"), principal="uid:test", promotion_record=record2)
        self.assertEqual(result2["error"]["code"], "integrity-failure")

    def test_promotion_record_outside_trusted_root_is_never_opened(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        record, digest = create_candidate(outside, "ai.vibapp.outside")
        result = self.daemon.execute(
            envelope("install", {"package_digest_sha256": digest, "enable_after_install": False}, subject=None, key="outside"),
            principal="uid:test",
            promotion_record=record,
        )
        self.assertEqual(result["error"]["code"], "permission-denied")
        self.assertNotIn("ai.vibapp.outside", self.daemon.inspect_state()["apps"])

    def test_close_is_not_disable_but_disable_quiesces_service(self) -> None:
        self.install()
        self.assertEqual(self.call("enable", None)["outcome"]["tag"], "accepted")
        self.call("service-start", {"entrypoint": "main-service"})
        launched = self.call("launch", {"entrypoint": "main-ui", "route": None})
        surface = launched["outcome"]["value"]
        self.call("surface-close", {"session": surface["session"], "surface": surface["surface"]})
        status = self.call("status", None)
        self.assertTrue(status["outcome"]["value"]["enabled"])
        self.assertEqual(status["outcome"]["value"]["active_service_entrypoints"], ["main-service"])
        self.assertTrue(status["outcome"]["value"]["services"][0]["guest_execution"])
        self.assertEqual(status["outcome"]["value"]["services"][0]["health"]["status"], "healthy")
        self.assertEqual(status["outcome"]["value"]["services"][0]["started_at_utc"], self.clock.value)
        self.assertEqual(status["outcome"]["value"]["services"][0]["start_reason"], "manual")
        self.assertIsNone(status["outcome"]["value"]["services"][0]["recovered_at_utc"])
        self.call("disable", None)
        disabled = self.call("status", None)["outcome"]["value"]
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["active_service_entrypoints"], [])
        app = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertTrue((self.runtime / app["package_path"]).is_dir())

    def test_enable_starts_only_declared_on_enable_services(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            service_triggers={
                "main-service": ["on-enable"],
                "other-service": ["manual"],
            },
        )
        self.install()

        enabled = self.call("enable", None, key="enable-on-enable-service")
        self.assertNotIn("error", enabled)
        app = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertTrue(app["enabled"])
        self.assertEqual(list(app["services"]), ["main-service"])
        self.assertEqual(app["services"]["main-service"]["start_reason"], "enabled")
        first_generation = app["services"]["main-service"]["generation"]

        replay_as_new_command = self.call(
            "enable", None, key="enable-on-enable-service-again"
        )
        self.assertNotIn("error", replay_as_new_command)
        self.assertEqual(
            self.daemon.inspect_state()["apps"][self.app_id]["services"]["main-service"][
                "generation"
            ],
            first_generation,
        )

        unsupported_manual = self.call(
            "service-start",
            {"entrypoint": "main-service"},
            key="manual-start-not-declared",
        )
        self.assertEqual(unsupported_manual["error"]["code"], "capability-unavailable")
        manual = self.call(
            "service-start",
            {"entrypoint": "other-service"},
            key="manual-start-declared",
        )
        self.assertNotIn("error", manual)
        self.assertEqual(
            sorted(self.daemon.inspect_state()["apps"][self.app_id]["services"]),
            ["main-service", "other-service"],
        )

    def test_scheduler_trigger_gating_and_scheduler_only_cold_start(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            self.app_id,
            capabilities_override=[
                self.kv_capability(),
                self.capability(
                    "scheduler", necessity="degradable", availability="denied"
                ),
            ],
            service_triggers={
                "main-service": ["scheduler"],
                "other-service": ["manual"],
            },
        )
        self.install()
        self.assertNotIn("error", self.call("enable", None))
        self.assertEqual(
            self.call(
                "service-start", {"entrypoint": "main-service"}, key="manual-denied"
            )["error"]["code"],
            "capability-unavailable",
        )
        delivered = self.call(
            "service-trigger",
            {"entrypoint": "main-service", "trigger_id": "tick-1", "payload": []},
            key="scheduler-cold-start",
        )
        self.assertNotIn("error", delivered)
        service = self.daemon.inspect_state()["apps"][self.app_id]["services"][
            "main-service"
        ]
        self.assertEqual(service["start_reason"], "scheduler")
        # experimental-v0 has no scheduled start enum; only the daemon cause is
        # scheduler while the frozen Guest ABI receives manual compatibility.
        self.assertEqual(service["guest_start_reason"], "manual")
        self.assertEqual(service["last_trigger"]["id"], "tick-1")

        manual_only = self.call(
            "service-start", {"entrypoint": "other-service"}, key="manual-other"
        )
        self.assertNotIn("error", manual_only)
        rejected = self.call(
            "service-trigger",
            {"entrypoint": "other-service", "trigger_id": "tick-2", "payload": []},
            key="scheduler-not-declared",
        )
        self.assertEqual(rejected["error"]["code"], "capability-unavailable")

    def test_enable_recovery_discards_isolated_guest_start_state(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            service_triggers={
                "main-service": ["on-enable"],
                "other-service": ["manual"],
            },
        )
        self.install()
        active_state = self.runtime / "app-data" / self.app_id
        write_profile_state(active_state)
        before = snapshot_state(active_state)
        state = self.daemon.inspect_state()
        app = state["apps"][self.app_id]
        transaction_id = "1" * 32
        transaction_root = (
            self.runtime / "updates" / self.app_id / f"enable-{transaction_id}"
        )
        transaction_root.mkdir(parents=True)
        candidate_state = transaction_root / "candidate-state"
        previous_state = transaction_root / "previous-state"
        self.daemon._copy_state_tree(active_state, candidate_state)
        candidate_app = json.loads(json.dumps(app))
        service, worker = self.daemon._create_service_generation(
            self.app_id,
            candidate_app,
            "main-service",
            reason="enabled",
            state_directory=candidate_state,
            state_revision=app["state"]["revision"],
        )
        self.assertTrue(service["guest_execution"])
        worker.terminate()  # Abrupt process loss: no in-process rollback path.
        self.assertEqual(snapshot_state(active_state), before)
        app["enable_transaction"] = {
            "schema_version": ENABLE_SCHEMA,
            "transaction_id": transaction_id,
            "status": "preparing",
            "app_id": self.app_id,
            "package_digest_sha256": app["package_digest_sha256"],
            "transaction_root": str(transaction_root.relative_to(self.runtime)),
            "candidate_state_path": str(candidate_state.relative_to(self.runtime)),
            "previous_state_path": str(previous_state.relative_to(self.runtime)),
            "original_state_revision": app["state"]["revision"],
            "service_entrypoints": ["main-service"],
            "started_at_utc": self.clock.value,
        }
        self.daemon.shutdown()
        (self.runtime / "state.json").write_bytes(canonical_json(state) + b"\n")
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        recovered = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertFalse(recovered["enabled"])
        self.assertIsNone(recovered["enable_transaction"])
        self.assertEqual(snapshot_state(active_state), before)
        self.assertFalse(transaction_root.exists())

    def test_enable_recovery_rolls_back_an_uncommitted_state_swap(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            service_triggers={
                "main-service": ["on-enable"],
                "other-service": ["manual"],
            },
        )
        self.install()
        active_state = self.runtime / "app-data" / self.app_id
        write_profile_state(active_state)
        before = snapshot_state(active_state)
        state = self.daemon.inspect_state()
        app = state["apps"][self.app_id]
        transaction_id = "2" * 32
        transaction_root = (
            self.runtime / "updates" / self.app_id / f"enable-{transaction_id}"
        )
        transaction_root.mkdir(parents=True)
        candidate_state = transaction_root / "candidate-state"
        previous_state = transaction_root / "previous-state"
        self.daemon._copy_state_tree(active_state, candidate_state)
        write_profile_state(candidate_state, revision=2)
        os.replace(active_state, previous_state)
        os.replace(candidate_state, active_state)
        app["enable_transaction"] = {
            "schema_version": ENABLE_SCHEMA,
            "transaction_id": transaction_id,
            "status": "switching",
            "app_id": self.app_id,
            "package_digest_sha256": app["package_digest_sha256"],
            "transaction_root": str(transaction_root.relative_to(self.runtime)),
            "candidate_state_path": str(candidate_state.relative_to(self.runtime)),
            "previous_state_path": str(previous_state.relative_to(self.runtime)),
            "original_state_revision": app["state"]["revision"],
            "service_entrypoints": ["main-service"],
            "started_at_utc": self.clock.value,
        }
        self.daemon.shutdown()
        (self.runtime / "state.json").write_bytes(canonical_json(state) + b"\n")
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        recovered = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertFalse(recovered["enabled"])
        self.assertIsNone(recovered["enable_transaction"])
        self.assertEqual(snapshot_state(active_state), before)
        self.assertFalse(transaction_root.exists())

    def test_enable_recovery_keeps_a_durably_committed_state_swap(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            service_triggers={
                "main-service": ["on-enable"],
                "other-service": ["manual"],
            },
        )
        self.install()
        self.assertNotIn("error", self.call("enable", None))
        active_state = self.runtime / "app-data" / self.app_id
        committed_bytes = snapshot_state(active_state)
        state = self.daemon.inspect_state()
        app = state["apps"][self.app_id]
        transaction_id = "3" * 32
        transaction_root = (
            self.runtime / "updates" / self.app_id / f"enable-{transaction_id}"
        )
        previous_state = transaction_root / "previous-state"
        previous_state.mkdir(parents=True)
        candidate_state = transaction_root / "candidate-state"
        app["enable_transaction"] = {
            "schema_version": ENABLE_SCHEMA,
            "transaction_id": transaction_id,
            "status": "committed",
            "app_id": self.app_id,
            "package_digest_sha256": app["package_digest_sha256"],
            "transaction_root": str(transaction_root.relative_to(self.runtime)),
            "candidate_state_path": str(candidate_state.relative_to(self.runtime)),
            "previous_state_path": str(previous_state.relative_to(self.runtime)),
            "original_state_revision": app["state"]["revision"],
            "service_entrypoints": ["main-service"],
            "started_at_utc": self.clock.value,
        }
        self.daemon.shutdown()
        (self.runtime / "state.json").write_bytes(canonical_json(state) + b"\n")
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        recovered = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertTrue(recovered["enabled"])
        self.assertIsNone(recovered["enable_transaction"])
        self.assertEqual(snapshot_state(active_state), committed_bytes)
        self.assertFalse(transaction_root.exists())
        self.assertEqual(recovered["services"]["main-service"]["state"], "running")

    def test_recovery_rejects_tampered_installed_manifest_before_trigger_restore(self) -> None:
        self.install()
        state = self.daemon.inspect_state()
        package = self.runtime / state["apps"][self.app_id]["package_path"]
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        service = next(
            item for item in manifest["entrypoints"] if item["id"] == "main-service"
        )
        service["triggers"] = ["on-enable"]
        self.daemon.shutdown()
        manifest_path.write_bytes(canonical_json(manifest) + b"\n")
        with self.assertRaisesRegex(DaemonError, "manifest digest changed"):
            RuntimeDaemon(
                self.runtime,
                self.promotions,
                clock=self.clock,
                service_runtime_binary=SERVICE_RUNTIME,
            )

    def test_failed_on_enable_start_restores_disabled_state_and_app_data(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=2,
            migratable_from_max=2,
            component_override=HYBRID_V2_UNHEALTHY_COMPONENT,
            service_triggers={
                "main-service": ["on-enable"],
                "other-service": ["manual"],
            },
        )
        self.install()
        state_directory = self.runtime / "app-data" / self.app_id
        write_profile_state(state_directory)
        before = snapshot_state(state_directory)

        rejected = self.call("enable", None, key="enable-unhealthy-on-enable")
        self.assertEqual(rejected["error"]["code"], "internal")
        app = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertFalse(app["enabled"])
        self.assertEqual(app["lifecycle_state"], "installed-disabled")
        self.assertEqual(app["services"], {})
        self.assertFalse(app["guest_execution_performed"])
        self.assertEqual(snapshot_state(state_directory), before)

    def test_install_rejects_noncanonical_or_unknown_service_triggers(self) -> None:
        for index, triggers in enumerate(
            (["manual", "on-enable"], ["on-enable", "unknown"], ["scheduler"])
        ):
            with self.subTest(triggers=triggers):
                app_id = f"ai.vibapp.invalid-triggers-{index}"
                record, digest = create_candidate(
                    self.promotions,
                    app_id,
                    service_triggers={"main-service": triggers},
                )
                rejected = self.daemon.execute(
                    envelope(
                        "install",
                        {
                            "package_digest_sha256": digest,
                            "enable_after_install": False,
                        },
                        subject=None,
                        key=f"install-invalid-triggers-{index}",
                    ),
                    principal="uid:test",
                    promotion_record=record,
                )
                self.assertEqual(rejected["error"]["code"], "incompatible-contract")
                self.assertNotIn(app_id, self.daemon.inspect_state()["apps"])

    def test_idempotent_replay_and_conflicting_reuse_fail_closed(self) -> None:
        self.install()
        before = self.daemon.inspect_state()["sequence"]
        first = self.call("enable", None, key="same-enable")
        after_first = self.daemon.inspect_state()["sequence"]
        replay = self.call("enable", None, key="same-enable")
        after_replay = self.daemon.inspect_state()["sequence"]
        self.assertEqual(first["outcome"], replay["outcome"])
        self.assertGreater(after_first, before)
        self.assertEqual(after_replay, after_first)

        self.call("service-start", {"entrypoint": "main-service"}, key="service-key")
        conflict = self.call("service-start", {"entrypoint": "other-service"}, key="service-key")
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertNotIn("other-service", self.daemon.inspect_state()["apps"][self.app_id]["services"])

    def test_status_is_read_only_and_creates_no_dedup_or_audit_record(self) -> None:
        self.install()
        state_before = (self.runtime / "state.json").read_bytes()
        audit_before = (self.runtime / "audit.jsonl").read_bytes()
        first = self.call("status", None, key="status-read-only")
        second = self.call("status", None, key="status-read-only")
        self.assertEqual(first["outcome"], second["outcome"])
        self.assertEqual((self.runtime / "state.json").read_bytes(), state_before)
        self.assertEqual((self.runtime / "audit.jsonl").read_bytes(), audit_before)

    def test_forged_subject_is_rejected_without_mutation(self) -> None:
        self.install()
        before = self.daemon.inspect_state()["apps"][self.app_id]["enabled"]
        result = self.daemon.execute(envelope("enable", None, subject=self.app_id, key="forged"), principal="uid:test", allowed_apps={"ai.vibapp.other"})
        self.assertEqual(result["error"]["code"], "forged-identifier")
        self.assertEqual(self.daemon.inspect_state()["apps"][self.app_id]["enabled"], before)

    def test_restart_recovery_keeps_enabled_daemon_service(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        self.daemon.shutdown()
        restarted = RuntimeDaemon(self.runtime, self.promotions, clock=self.clock, service_runtime_binary=SERVICE_RUNTIME)
        response = restarted.execute(envelope("status", None, subject=self.app_id, key="status-after-restart"), principal="uid:test")
        self.assertEqual(response["outcome"]["value"]["active_service_entrypoints"], ["main-service"])
        public_service = response["outcome"]["value"]["services"][0]
        self.assertEqual(public_service["start_reason"], "host-restart")
        self.assertEqual(public_service["recovered_at_utc"], self.clock.value)
        service = restarted.inspect_state()["apps"][self.app_id]["services"]["main-service"]
        self.assertEqual(service["owner"], "daemon")
        self.assertTrue(service["guest_execution"])
        self.assertEqual(service["start_reason"], "host-restart")
        restarted.shutdown()

    def test_activated_service_generation_adopts_guest_kv_revision(self) -> None:
        app = {"state": {"revision": 1}}
        worker = object()
        service = {"state_revision": 2}
        original = self.daemon._create_service_generation
        self.daemon._create_service_generation = lambda *_args, **_kwargs: (service, worker)
        try:
            activated = self.daemon._activate_service_generation(
                self.app_id,
                app,
                "main-service",
                reason="manual",
            )
        finally:
            self.daemon._create_service_generation = original
            self.daemon._workers.pop((self.app_id, "main-service"), None)
        self.assertIs(activated, service)
        self.assertEqual(app["state"]["revision"], 2)

    def test_update_migrates_copied_state_and_restart_keeps_exact_candidate(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        state_directory = self.runtime / "app-data" / self.app_id
        state_file = state_directory / "state.bin"
        state_file.write_bytes(b"state-before-update")
        write_profile_state(state_directory)
        previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )
        response = self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="update-v2",
            promotion=update_record,
        )
        self.assertNotIn("error", response)
        status = self.call("status", None)["outcome"]["value"]
        self.assertEqual(status["version"], "0.2.0")
        self.assertEqual(status["package_digest_sha256"], update_digest)
        self.assertEqual(status["state"]["schema"], 2)
        self.assertEqual(status["state"]["revision"], 2)
        self.assertEqual(status["update"]["status"], "observation-window")
        transaction = status["update"]["last_transaction"]
        self.assertEqual(transaction["from_package_digest_sha256"], self.digest)
        self.assertEqual(transaction["to_package_digest_sha256"], update_digest)
        self.assertEqual(transaction["migration"]["target_state_revision"], 2)
        self.assertEqual(transaction["migration"]["status"], "migrated")
        self.assertEqual(transaction["migration"]["kv_state_revision"], 2)
        self.assertEqual(transaction["activation_health"]["status"], "healthy")
        self.assertEqual(status["services"][0]["package_digest_sha256"], update_digest)
        self.assertEqual(status["services"][0]["state_revision"], 2)
        self.assertEqual(state_file.read_bytes(), b"state-before-update")
        migrated_state = read_kv_state(state_directory)
        self.assertEqual(migrated_state["state_revision"], 2)
        self.assertNotIn("profile/name", migrated_state["entries"])
        self.assertEqual(
            bytes(migrated_state["entries"]["profile/display-name"]["value"]),
            b"migrated:alice",
        )
        self.assertEqual(
            migrated_state["entries"]["profile/display-name"]["revision"],
            2,
        )

        self.daemon.shutdown()
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        restarted = self.call("status", None)["outcome"]["value"]
        self.assertEqual(restarted["package_digest_sha256"], update_digest)
        self.assertEqual(restarted["active_service_entrypoints"], ["main-service"])
        self.assertEqual(state_file.read_bytes(), b"state-before-update")
        self.assertEqual(read_kv_state(state_directory)["state_revision"], 2)

        rolled_back = self.daemon.record_service_crash(
            self.app_id,
            "main-service",
            at_utc="2026-08-27T00:01:00Z",
        )
        self.assertEqual(rolled_back["result"], "rolled-back")
        restored = self.call("status", None)["outcome"]["value"]
        self.assertEqual(restored["version"], "0.1.0")
        self.assertEqual(restored["package_digest_sha256"], self.digest)
        self.assertEqual(restored["state"]["schema"], 1)
        self.assertEqual(restored["state"]["revision"], 1)
        self.assertEqual(restored["update"]["status"], "rolled-back")
        self.assertIn("terminated abnormally", restored["update"]["last_transaction"]["rollback_reason"])
        self.assertEqual(restored["active_service_entrypoints"], ["main-service"])
        self.assertEqual(restored["services"][0]["package_digest_sha256"], self.digest)
        self.assertEqual(restored["services"][0]["state_revision"], 1)
        self.assertEqual(state_file.read_bytes(), b"state-before-update")
        self.assertEqual(snapshot_state(state_directory), previous_state)

    def test_injected_swap_failure_restores_previous_digest_state_and_service(self) -> None:
        self.daemon.shutdown()
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
            update_fault="after-state-swap",
        )
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        state_directory = self.runtime / "app-data" / self.app_id
        state_file = state_directory / "state.bin"
        state_file.write_bytes(b"must-survive")
        write_profile_state(state_directory)
        previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )
        rejected = self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="update-injected-failure",
            promotion=update_record,
        )
        self.assertEqual(rejected["error"]["code"], "internal")
        status = self.call("status", None)["outcome"]["value"]
        self.assertEqual(status["version"], "0.1.0")
        self.assertEqual(status["package_digest_sha256"], self.digest)
        self.assertEqual(status["state"]["revision"], 1)
        self.assertEqual(status["update"]["status"], "rolled-back")
        self.assertEqual(status["update"]["last_transaction"]["status"], "rolled-back")
        self.assertIn("after-state-swap", status["update"]["last_transaction"]["rollback_reason"])
        self.assertEqual(status["active_service_entrypoints"], ["main-service"])
        self.assertEqual(state_file.read_bytes(), b"must-survive")
        self.assertEqual(snapshot_state(state_directory), previous_state)
        self.assertFalse((self.runtime / "packages" / self.app_id / update_digest).exists())

    def test_failure_after_real_migration_discards_target_and_never_runs_health(self) -> None:
        self.daemon.shutdown()
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
            update_fault="after-migration",
        )
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        state_directory = self.runtime / "app-data" / self.app_id
        write_profile_state(state_directory)
        previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )

        rejected = self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="update-fail-after-migration",
            promotion=update_record,
        )

        self.assertEqual(rejected["error"]["code"], "internal")
        status = self.call("status", None)["outcome"]["value"]
        transaction = status["update"]["last_transaction"]
        self.assertEqual(status["package_digest_sha256"], self.digest)
        self.assertEqual(status["state"]["revision"], 1)
        self.assertEqual(status["active_service_entrypoints"], ["main-service"])
        self.assertEqual(transaction["migration"]["status"], "migrated")
        self.assertEqual(transaction["migration"]["kv_state_revision"], 2)
        self.assertEqual(transaction["activation_health"]["status"], "not-run")
        self.assertEqual(snapshot_state(state_directory), previous_state)
        self.assertFalse((self.runtime / "packages" / self.app_id / update_digest).exists())

    def test_unhealthy_candidate_restores_previous_version_and_records_failed_health(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        state_directory = self.runtime / "app-data" / self.app_id
        state_file = state_directory / "state.bin"
        state_file.write_bytes(b"health-failure-must-not-touch-state")
        write_profile_state(state_directory)
        previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
            component_override=HYBRID_V2_UNHEALTHY_COMPONENT,
        )

        rejected = self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="update-unhealthy-candidate",
            promotion=update_record,
        )

        self.assertEqual(rejected["error"]["code"], "internal")
        status = self.call("status", None)["outcome"]["value"]
        transaction = status["update"]["last_transaction"]
        self.assertEqual(status["package_digest_sha256"], self.digest)
        self.assertEqual(status["active_service_entrypoints"], ["main-service"])
        self.assertEqual(transaction["activation_health"]["status"], "failed")
        self.assertEqual(transaction["migration"]["status"], "migrated")
        self.assertIn("unhealthy", transaction["rollback_reason"])
        self.assertEqual(state_file.read_bytes(), b"health-failure-must-not-touch-state")
        self.assertEqual(snapshot_state(state_directory), previous_state)
        self.assertFalse((self.runtime / "packages" / self.app_id / update_digest).exists())

    def test_restart_activation_failure_during_observation_rolls_back(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        state_directory = self.runtime / "app-data" / self.app_id
        state_file = state_directory / "state.bin"
        state_file.write_bytes(b"restart-failure-must-restore-this")
        write_profile_state(state_directory)
        previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )
        self.assertNotIn(
            "error",
            self.call(
                "update",
                {"package_digest_sha256": update_digest},
                key="update-before-restart-failure",
                promotion=update_record,
            ),
        )
        self.daemon.shutdown()
        installed_component = (
            self.runtime / "packages" / self.app_id / update_digest / "component.wasm"
        )
        installed_component.write_bytes(installed_component.read_bytes() + b"tamper")

        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )

        status = self.call("status", None)["outcome"]["value"]
        self.assertEqual(status["package_digest_sha256"], self.digest)
        self.assertEqual(status["state"]["schema"], 1)
        self.assertEqual(status["active_service_entrypoints"], ["main-service"])
        self.assertEqual(status["update"]["status"], "rolled-back")
        self.assertIn(
            "candidate recovery failed",
            status["update"]["last_transaction"]["rollback_reason"],
        )
        self.assertEqual(state_file.read_bytes(), b"restart-failure-must-restore-this")
        self.assertEqual(snapshot_state(state_directory), previous_state)
        self.assertFalse((self.runtime / "packages" / self.app_id / update_digest).exists())

    def test_migration_failure_restores_previous_version_and_records_failed_migration(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        state_directory = self.runtime / "app-data" / self.app_id
        state_file = state_directory / "state.bin"
        state_file.write_bytes(b"migration-failure-must-not-touch-state")
        write_profile_state(state_directory)
        previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
            component_override=HYBRID_V2_MIGRATION_FAIL_COMPONENT,
        )

        rejected = self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="update-migration-failure",
            promotion=update_record,
        )

        self.assertEqual(rejected["error"]["code"], "internal")
        status = self.call("status", None)["outcome"]["value"]
        transaction = status["update"]["last_transaction"]
        self.assertEqual(status["package_digest_sha256"], self.digest)
        self.assertEqual(status["active_service_entrypoints"], ["main-service"])
        self.assertEqual(transaction["activation_health"]["status"], "not-run")
        self.assertEqual(transaction["migration"]["status"], "failed")
        self.assertIn("migration", transaction["rollback_reason"])
        self.assertEqual(state_file.read_bytes(), b"migration-failure-must-not-touch-state")
        self.assertEqual(snapshot_state(state_directory), previous_state)
        self.assertFalse((self.runtime / "packages" / self.app_id / update_digest).exists())

    def test_restart_recovers_a_durable_switching_journal_to_previous_state(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        active_state = self.runtime / "app-data" / self.app_id
        state_file = active_state / "state.bin"
        state_file.write_bytes(b"old-durable-state")
        write_profile_state(active_state)
        durable_state = snapshot_state(active_state)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )
        candidate = self.daemon._verify_candidate(update_record, update_digest)
        candidate_document = json.loads(update_record.read_text())
        self.daemon._stage_package(candidate, candidate_document)
        transaction_id = "update-simulated-crash"
        transaction_root = self.runtime / "updates" / self.app_id / transaction_id
        candidate_state = transaction_root / "candidate-state"
        previous_state = transaction_root / "previous-state"
        self.daemon._copy_state_tree(active_state, candidate_state)
        (candidate_state / "state.bin").write_bytes(b"candidate-uncommitted-state")
        with self.daemon._locked_state() as state:
            app = state["apps"][self.app_id]
            previous = self.daemon._previous_generation(app, ["main-service"])
            os.replace(active_state, previous_state)
            os.replace(candidate_state, active_state)
            app["update"]["status"] = "switching"
            app["update"]["pending"] = {
                "schema_version": "vibapp.update-transaction.experimental-v1",
                "transaction_id": transaction_id,
                "status": "switching",
                "from_package_digest_sha256": self.digest,
                "to_package_digest_sha256": update_digest,
                "from_version": "0.1.0",
                "to_version": "0.2.0",
                "transaction_root": str(transaction_root.relative_to(self.runtime)),
                "candidate_state_path": str(candidate_state.relative_to(self.runtime)),
                "previous_state_path": str(previous_state.relative_to(self.runtime)),
                "previous": previous,
            }
            self.daemon._write_state(state)
        self.daemon.shutdown()

        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        recovered = self.call("status", None)["outcome"]["value"]
        self.assertEqual(recovered["package_digest_sha256"], self.digest)
        self.assertEqual(recovered["state"]["revision"], 1)
        self.assertEqual(recovered["update"]["status"], "rolled-back")
        self.assertEqual(
            recovered["update"]["last_transaction"]["rollback_reason"],
            "daemon-recovery-restored-previous-generation",
        )
        self.assertEqual(recovered["active_service_entrypoints"], ["main-service"])
        self.assertEqual(state_file.read_bytes(), b"old-durable-state")
        self.assertEqual(snapshot_state(active_state), durable_state)
        self.assertFalse((self.runtime / "packages" / self.app_id / update_digest).exists())

    def test_ui_update_preserves_surface_and_ui_failure_rolls_back_without_disabling(self) -> None:
        self.record, self.digest = create_candidate(
            self.promotions,
            kind="ui",
        )
        self.install()
        self.call("enable", None)
        launched = self.call("launch", {"entrypoint": "main-ui", "route": None})[
            "outcome"
        ]["value"]
        state_directory = self.runtime / "app-data" / self.app_id
        write_profile_state(state_directory)
        exact_previous_state = snapshot_state(state_directory)
        update_record, update_digest = create_candidate(
            self.promotions,
            kind="ui",
            version="0.2.0",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )

        updated = self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="ui-update-v2",
            promotion=update_record,
        )

        self.assertNotIn("error", updated)
        active = self.call("status", None)["outcome"]["value"]
        self.assertEqual(active["package_digest_sha256"], update_digest)
        self.assertEqual(active["state"]["revision"], 2)
        self.assertEqual(active["surfaces"][0]["state"], "open")
        self.assertEqual(active["surfaces"][0]["session"], launched["session"])
        self.assertEqual(
            bytes(read_kv_state(state_directory)["entries"]["profile/display-name"]["value"]),
            b"migrated:alice",
        )
        candidate_launch = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": "home"},
            key="ui-candidate-launch",
        )["outcome"]["value"]
        report = {
            key: candidate_launch[key]
            for key in (
                "entrypoint",
                "package_digest_sha256",
                "component_sha256",
                "generation",
                "session",
                "surface",
                "route",
            )
        }
        report.update(
            event_id="render-event-1",
            render_failure_token=candidate_launch["render_failure_token"],
            reason="launcher render failed after activation",
        )
        forged = self.call(
            "ui-render-failure",
            {**report, "render_failure_token": "0" * 64},
            key="ui-forged-render-report",
        )
        self.assertEqual(forged["error"]["code"], "forged-identifier")
        reported = self.call(
            "ui-render-failure",
            report,
            key="ui-valid-render-report",
        )
        self.assertEqual(reported["outcome"]["value"]["result"], "rolled-back")
        replay = self.call(
            "ui-render-failure",
            report,
            key="ui-replayed-render-report",
        )
        self.assertIn(replay["error"]["code"], {"conflict", "forged-identifier"})
        restored = self.call("status", None)["outcome"]["value"]
        self.assertTrue(restored["enabled"])
        self.assertEqual(restored["package_digest_sha256"], self.digest)
        self.assertEqual(restored["surfaces"][0]["state"], "closed")
        self.assertEqual(snapshot_state(state_directory), exact_previous_state)

        reopened = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": "home"},
            key="ui-reopen",
        )
        self.assertNotIn("error", reopened)
        self.call("disable", None, key="ui-disable")
        disabled = self.call("status", None)["outcome"]["value"]
        self.assertFalse(disabled["enabled"])
        self.assertIn(
            "disabled",
            {surface.get("close_reason") for surface in disabled["surfaces"]},
        )
        self.call("uninstall", {"disposition": "delete"}, key="ui-uninstall")
        self.assertNotIn(self.app_id, self.daemon.inspect_state()["apps"])

    def test_ui_action_is_exact_bound_and_persists_guest_kv(self) -> None:
        self.record, self.digest = create_candidate(self.promotions, kind="ui")
        self.install()
        self.call("enable", None)
        launched = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": None},
            key="ui-action-launch",
        )["outcome"]["value"]
        action = {
            key: launched[key]
            for key in (
                "entrypoint",
                "package_digest_sha256",
                "component_sha256",
                "generation",
                "session",
                "surface",
                "route",
            )
        }
        action.update(
            action="increment",
            event_id="ui-action-event-1",
            fields=[{"field": "name", "value": {"tag": "text", "value": "Grace"}}],
        )
        forged = self.call(
            "ui-action",
            {**action, "generation": f"{action['generation']}:forged"},
            key="ui-action-forged",
        )
        self.assertEqual(forged["error"]["code"], "forged-identifier")
        delivered = self.call("ui-action", action, key="ui-action-valid")
        self.assertEqual(delivered["outcome"]["tag"], "ui-updated")
        self.assertIn(
            "revision 2",
            delivered["outcome"]["value"]["semantic_surface"]["view"]["title"],
        )
        persisted = read_kv_state(self.runtime / "app-data" / self.app_id)
        self.assertEqual(persisted["state_revision"], 2)
        self.assertEqual(
            bytes(persisted["entries"]["ui/last-action"]["value"]),
            b"increment",
        )
        replay = self.call("ui-action", action, key="ui-action-valid")
        self.assertEqual(replay, delivered)
        self.assertEqual(
            read_kv_state(self.runtime / "app-data" / self.app_id)["state_revision"],
            2,
        )

    def test_hybrid_ui_and_service_share_app_kv_across_surface_close(self) -> None:
        self.configure_scheduler_fixture()
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        launched = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": None},
            key="shared-kv-launch",
        )["outcome"]["value"]
        action = {
            key: launched[key]
            for key in (
                "entrypoint",
                "package_digest_sha256",
                "component_sha256",
                "generation",
                "session",
                "surface",
                "route",
            )
        }
        action.update(
            action="increment",
            event_id="shared-kv-action-event",
            fields=[{"field": "name", "value": {"tag": "text", "value": "Grace"}}],
        )
        self.assertEqual(
            self.call("ui-action", action, key="shared-kv-action")["outcome"]["tag"],
            "ui-updated",
        )
        self.call(
            "surface-close",
            {"session": launched["session"], "surface": launched["surface"]},
            key="shared-kv-close",
        )
        self.call(
            "service-trigger",
            {"entrypoint": "main-service", "trigger_id": "read-ui-kv", "payload": []},
            key="shared-kv-service-read",
        )
        status = self.call("status", None)["outcome"]["value"]
        service = next(
            item for item in status["services"] if item["entrypoint"] == "main-service"
        )
        self.assertEqual(status["active_service_entrypoints"], ["main-service"])
        self.assertEqual(service["state_revision"], 2)
        self.assertEqual(service["last_trigger"]["diagnostic"], "shared-kv:2:9")
        self.assertEqual(status["surfaces"][0]["state"], "closed")
        reopened = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": "home"},
            key="shared-kv-reopen",
        )
        self.assertNotIn("error", reopened)

    def test_ui_action_rejects_field_kind_mismatch_before_guest(self) -> None:
        self.record, self.digest = create_candidate(self.promotions, kind="ui")
        self.install()
        self.call("enable", None)
        launched = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": None},
            key="ui-action-mismatch-launch",
        )["outcome"]["value"]
        action = {
            key: launched[key]
            for key in (
                "entrypoint",
                "package_digest_sha256",
                "component_sha256",
                "generation",
                "session",
                "surface",
                "route",
            )
        }
        action.update(
            action="increment",
            event_id="ui-action-mismatch-event-1",
            fields=[{"field": "name", "value": {"tag": "integer", "value": 7}}],
        )
        before = read_kv_state(self.runtime / "app-data" / self.app_id)

        rejected = self.call(
            "ui-action",
            action,
            key="ui-action-mismatch",
        )

        self.assertEqual(rejected["error"]["code"], "invalid-argument")
        self.assertIn("does not match", rejected["error"]["message"])
        after = read_kv_state(self.runtime / "app-data" / self.app_id)
        self.assertEqual(after, before)
        self.assertNotIn("ui/last-action", after["entries"])

    def test_ui_refresh_reuses_exact_binding_and_is_daemon_rate_limited(self) -> None:
        self.record, self.digest = create_candidate(self.promotions, kind="ui")
        self.install()
        self.call("enable", None)
        launched = self.call(
            "launch",
            {"entrypoint": "main-ui", "route": None},
            key="ui-refresh-launch",
        )["outcome"]["value"]
        refresh = {
            key: launched[key]
            for key in (
                "entrypoint",
                "package_digest_sha256",
                "component_sha256",
                "generation",
                "session",
                "surface",
                "route",
            )
        }
        refresh["event_id"] = "ui-refresh-event-1"
        pre_rejection_audit_bytes = self.daemon.audit_path.read_bytes()

        too_soon = self.call("ui-refresh", refresh, key="ui-refresh-too-soon")
        self.assertEqual(too_soon["error"]["code"], "resource-limit")
        self.assertTrue(too_soon["error"]["retryable"])
        forged = self.call(
            "ui-refresh",
            {**refresh, "generation": f"{refresh['generation']}:forged"},
            key="ui-refresh-forged",
        )
        self.assertEqual(forged["error"]["code"], "forged-identifier")
        baseline_state = self.daemon.inspect_state()
        baseline_ledger_size = len(baseline_state["idempotency"])
        baseline_state_bytes = self.daemon.state_path.read_bytes()
        baseline_audit_bytes = self.daemon.audit_path.read_bytes()
        self.assertGreater(len(baseline_audit_bytes), len(pre_rejection_audit_bytes))

        self.monotonic.advance(1.0)
        delivered = self.call("ui-refresh", refresh, key="ui-refresh-valid")
        self.assertEqual(delivered["outcome"]["tag"], "ui-updated")
        updated = delivered["outcome"]["value"]
        for key in (
            "entrypoint",
            "package_digest_sha256",
            "component_sha256",
            "generation",
            "session",
            "surface",
            "route",
        ):
            self.assertEqual(updated[key], launched[key])
        self.assertEqual(
            updated["semantic_surface"]["view"]["title"],
            "Fixture refreshed",
        )
        self.assertNotIn("render_failure_token", updated)
        self.assertEqual(self.daemon.state_path.read_bytes(), baseline_state_bytes)
        self.assertEqual(self.daemon.audit_path.read_bytes(), baseline_audit_bytes)

        rate_limited = self.call(
            "ui-refresh",
            {**refresh, "event_id": "ui-refresh-event-2"},
            key="ui-refresh-rate-limited",
        )
        self.assertEqual(rate_limited["error"]["code"], "resource-limit")

        # Visible clocks can refresh indefinitely without consuming the bounded
        # durable command ledger or appending a per-frame audit record/state fsync.
        # The worker still receives a unique event id for each bounded delivery.
        baseline_state_after_rejection = self.daemon.state_path.read_bytes()
        baseline_audit_after_rejection = self.daemon.audit_path.read_bytes()
        for index in range(101):
            self.monotonic.advance(1.0)
            repeated = self.call(
                "ui-refresh",
                {**refresh, "event_id": f"ui-refresh-event-{index + 3}"},
                key=f"ui-refresh-long-run-{index}",
            )
            self.assertEqual(repeated["outcome"]["tag"], "ui-updated")
        long_running_state = self.daemon.inspect_state()
        self.assertEqual(len(long_running_state["idempotency"]), baseline_ledger_size)
        self.assertEqual(
            self.daemon.state_path.read_bytes(),
            baseline_state_after_rejection,
        )
        self.assertEqual(self.daemon.audit_path.read_bytes(), baseline_audit_after_rejection)
        self.assertGreater(len(baseline_audit_after_rejection), len(baseline_audit_bytes))

        self.call(
            "surface-close",
            {"session": launched["session"], "surface": launched["surface"]},
            key="ui-refresh-close",
        )
        self.monotonic.advance(1.0)
        closed = self.call(
            "ui-refresh",
            {**refresh, "event_id": "ui-refresh-after-close"},
            key="ui-refresh-after-close",
        )
        self.assertIn(closed["error"]["code"], {"forged-identifier", "stale-revision"})

    def test_multiple_services_and_apps_update_with_independent_generation_ownership(self) -> None:
        self.install()
        app_b = "ai.vibapp.runtime-service-fixture-b"
        record_b, digest_b = create_candidate(self.promotions, app_b)
        installed_b = self.daemon.execute(
            envelope(
                "install",
                {"package_digest_sha256": digest_b, "enable_after_install": False},
                subject=None,
                key="install-b",
            ),
            principal="uid:test",
            promotion_record=record_b,
        )
        self.assertNotIn("error", installed_b)
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        self.call(
            "service-start",
            {"entrypoint": "other-service"},
            key="start-other-a",
        )
        for tag, value, key in (
            ("enable", None, "enable-b"),
            ("service-start", {"entrypoint": "main-service"}, "start-b"),
        ):
            result = self.daemon.execute(
                envelope(tag, value, subject=app_b, key=key),
                principal="uid:test",
            )
            self.assertNotIn("error", result)
        write_profile_state(self.runtime / "app-data" / self.app_id)
        write_profile_state(self.runtime / "app-data" / app_b)
        update_a, digest_a_v2 = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_max=1,
        )
        update_b, digest_b_v2 = create_candidate(
            self.promotions,
            app_b,
            version="0.2.0",
            state_schema=2,
            migratable_from_max=1,
        )

        self.assertNotIn(
            "error",
            self.call(
                "update",
                {"package_digest_sha256": digest_a_v2},
                key="update-a-v2",
                promotion=update_a,
            ),
        )
        before_b = self.daemon.inspect_state()["apps"][app_b]
        self.assertEqual(before_b["package_digest_sha256"], digest_b)
        self.assertEqual(before_b["services"]["main-service"]["state"], "running")
        result_b = self.daemon.execute(
            envelope(
                "update",
                {"package_digest_sha256": digest_b_v2},
                subject=app_b,
                key="update-b-v2",
            ),
            principal="uid:test",
            promotion_record=update_b,
        )
        self.assertNotIn("error", result_b)

        state = self.daemon.inspect_state()["apps"]
        self.assertEqual(state[self.app_id]["package_digest_sha256"], digest_a_v2)
        self.assertEqual(
            sorted(state[self.app_id]["services"]),
            ["main-service", "other-service"],
        )
        self.assertEqual(state[app_b]["package_digest_sha256"], digest_b_v2)
        self.assertEqual(state[app_b]["services"]["main-service"]["state"], "running")
        rolled_back_a = self.daemon.record_service_crash(
            self.app_id,
            "other-service",
            at_utc="2026-08-27T00:01:00Z",
        )
        self.assertEqual(rolled_back_a["result"], "rolled-back")
        state = self.daemon.inspect_state()["apps"]
        self.assertEqual(state[self.app_id]["package_digest_sha256"], self.digest)
        self.assertEqual(state[app_b]["package_digest_sha256"], digest_b_v2)
        self.assertEqual(state[app_b]["services"]["main-service"]["state"], "running")

    def test_observation_deadline_is_durable_and_backward_clock_does_not_expire_it(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        write_profile_state(self.runtime / "app-data" / self.app_id)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_max=1,
        )
        self.assertNotIn(
            "error",
            self.call(
                "update",
                {"package_digest_sha256": update_digest},
                key="deadline-update",
                promotion=update_record,
            ),
        )
        observation = self.call("status", None)["outcome"]["value"]["update"]
        self.assertEqual(observation["status"], "observation-window")
        self.assertEqual(
            observation["previous"]["observation_deadline_utc"],
            "2026-08-27T00:05:00Z",
        )

        self.clock.set("2026-08-26T23:59:00Z")
        self.assertEqual(
            self.call("status", None)["outcome"]["value"]["update"]["status"],
            "observation-window",
        )
        self.clock.set("not-a-time")
        invalid = self.call("status", None)
        self.assertEqual(invalid["error"]["code"], "invalid-argument")

        self.clock.set("2026-08-27T00:04:59Z")
        self.daemon.shutdown()
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        self.assertEqual(
            self.call("status", None)["outcome"]["value"]["update"]["status"],
            "observation-window",
        )
        self.clock.set("2026-08-27T00:05:00Z")
        committed = self.call("status", None)["outcome"]["value"]
        self.assertEqual(committed["update"]["status"], "committed")
        self.assertEqual(committed["update"]["previous"]["retention"], "last-rollback-generation")
        crash = self.daemon.record_service_crash(
            self.app_id,
            "main-service",
            at_utc="2026-08-27T00:06:00Z",
        )
        self.assertEqual(crash["result"], "restart-eligible")
        self.assertEqual(
            self.daemon.inspect_state()["apps"][self.app_id]["package_digest_sha256"],
            update_digest,
        )

    def test_generation_gc_journal_recovers_and_retains_active_and_rollback(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        write_profile_state(self.runtime / "app-data" / self.app_id)
        update_record, update_digest = create_candidate(
            self.promotions,
            version="0.2.0",
            state_schema=2,
            migratable_from_max=1,
        )
        self.call(
            "update",
            {"package_digest_sha256": update_digest},
            key="gc-update",
            promotion=update_record,
        )
        self.clock.set("2026-08-27T00:05:00Z")
        committed = self.call("status", None)["outcome"]["value"]
        rollback_root = Path(committed["update"]["previous"]["transaction_root"]).name
        package_root = self.runtime / "packages" / self.app_id
        update_root = self.runtime / "updates" / self.app_id
        for index in range(12):
            (package_root / f"obsolete-{index:02d}").mkdir()
        for index in range(6):
            (update_root / f"obsolete-{index:02d}").mkdir()

        self.daemon._update_fault = "during-gc"
        interrupted = self.call("status", None)
        self.assertEqual(interrupted["error"]["code"], "internal")
        interrupted_state = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertEqual(interrupted_state["gc"]["status"], "in-progress")
        self.assertTrue((package_root / update_digest).is_dir())
        self.assertTrue((package_root / self.digest).is_dir())
        self.assertTrue((update_root / rollback_root).is_dir())

        self.daemon.shutdown()
        self.daemon = RuntimeDaemon(
            self.runtime,
            self.promotions,
            clock=self.clock,
            service_runtime_binary=SERVICE_RUNTIME,
        )
        for index in range(4):
            self.call("status", None, key=f"gc-resume-{index}")
        self.assertEqual(
            sorted(path.name for path in package_root.iterdir()),
            sorted([self.digest, update_digest]),
        )
        self.assertEqual(
            sorted(path.name for path in update_root.iterdir()),
            [rollback_root],
        )
        gc = self.daemon.inspect_state()["apps"][self.app_id]["gc"]
        self.assertEqual(gc["status"], "idle")
        self.assertEqual(gc["pending"], [])

    def test_real_component_trigger_and_health_are_durable(self) -> None:
        self.configure_scheduler_fixture()
        self.install()
        self.call("enable", None)
        self.assertNotIn("error", self.call("service-start", {"entrypoint": "main-service"}))
        self.assertNotIn("error", self.call("service-trigger", {"entrypoint": "main-service", "trigger_id": "manual-1", "payload": [1, 2, 3]}))
        self.assertNotIn("error", self.call("service-health", {"entrypoint": "main-service"}))
        service = self.daemon.inspect_state()["apps"][self.app_id]["services"]["main-service"]
        self.assertEqual(service["state"], "running")
        self.assertEqual(service["event_count"], 4)
        self.assertEqual(service["last_trigger"]["payload_bytes"], 3)
        self.assertEqual(service["health"]["status"], "healthy")

    def test_guest_failure_is_typed_durable_and_does_not_kill_daemon(self) -> None:
        self.configure_scheduler_fixture()
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        failed = self.call("service-trigger", {"entrypoint": "main-service", "trigger_id": "fail", "payload": []})
        self.assertEqual(failed["error"]["code"], "internal")
        status = self.call("status", None)["outcome"]["value"]
        self.assertTrue(status["enabled"])
        self.assertEqual(status["active_service_entrypoints"], [])
        self.assertEqual(status["services"][0]["state"], "failed")
        self.assertEqual(status["services"][0]["last_error"]["operation"], "trigger")
        self.assertIn("fixture requested failure", status["services"][0]["last_error"]["message"])
        restarted = self.call("service-start", {"entrypoint": "main-service"}, key="restart-after-failure")
        self.assertNotIn("error", restarted)

    def test_disabled_and_uninstalled_apps_cannot_execute_guest(self) -> None:
        self.install()
        blocked = self.call("service-start", {"entrypoint": "main-service"})
        self.assertEqual(blocked["error"]["code"], "app-disabled")
        self.assertFalse(self.daemon.inspect_state()["apps"][self.app_id]["guest_execution_performed"])
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"}, key="enabled-start")
        self.call("disable", None)
        disabled_trigger = self.call("service-trigger", {"entrypoint": "main-service", "trigger_id": "after-disable", "payload": []})
        self.assertEqual(disabled_trigger["error"]["code"], "app-disabled")
        self.call("uninstall", {"disposition": "delete"})
        removed = self.call("service-start", {"entrypoint": "main-service"}, key="after-uninstall")
        self.assertEqual(removed["error"]["code"], "not-found")

    def test_three_crashes_in_five_minutes_quarantine_and_stop_restart(self) -> None:
        self.install()
        self.call("enable", None)
        for index, timestamp in enumerate(["2026-08-27T00:00:00Z", "2026-08-27T00:01:00Z", "2026-08-27T00:02:00Z"]):
            self.call("service-start", {"entrypoint": "main-service"}, key=f"start-{index}")
            result = self.daemon.record_service_crash(self.app_id, "main-service", at_utc=timestamp)
        self.assertEqual(result["result"], "quarantined")
        state = self.daemon.inspect_state()["apps"][self.app_id]
        self.assertTrue(state["quarantined"])
        self.assertFalse(state["enabled"])
        blocked = self.call("service-start", {"entrypoint": "main-service"}, key="fourth-start")
        self.assertEqual(blocked["error"]["code"], "app-disabled")

    def test_uninstall_retain_quiesces_and_seals_data(self) -> None:
        self.install()
        self.call("enable", None)
        self.call("service-start", {"entrypoint": "main-service"})
        data_file = self.runtime / "app-data" / self.app_id / "state.bin"
        data_file.write_bytes(b"app data")
        result = self.call("uninstall", {"disposition": "retain"})
        self.assertEqual(result["outcome"]["tag"], "accepted")
        state = self.daemon.inspect_state()
        self.assertNotIn(self.app_id, state["apps"])
        self.assertEqual(len(state["retained"]), 1)
        retained = next(iter(state["retained"].values()))
        self.assertTrue(retained["sealed"])
        self.assertFalse(retained["guest_accessible"])
        self.assertFalse((self.runtime / "packages" / self.app_id / self.digest).exists())

    def test_uninstall_delete_and_export_then_delete_are_distinct(self) -> None:
        for index, disposition in enumerate(("delete", "export-then-delete")):
            app_id = f"ai.vibapp.uninstall-{index}"
            record, digest = create_candidate(self.promotions, app_id)
            installed = self.daemon.execute(
                envelope("install", {"package_digest_sha256": digest, "enable_after_install": False}, subject=None, key=f"install-{index}"),
                principal="uid:test",
                promotion_record=record,
            )
            self.assertNotIn("error", installed)
            self.daemon.execute(envelope("enable", None, subject=app_id, key=f"enable-{index}"), principal="uid:test")
            self.daemon.execute(envelope("service-start", {"entrypoint": "main-service"}, subject=app_id, key=f"start-{index}"), principal="uid:test")
            data_file = self.runtime / "app-data" / app_id / "state.bin"
            data_file.write_bytes(b"to delete")
            removed = self.daemon.execute(envelope("uninstall", {"disposition": disposition}, subject=app_id, key=f"uninstall-{index}"), principal="uid:test")
            self.assertNotIn("error", removed)
            self.assertFalse(data_file.exists())
            self.assertNotIn(app_id, self.daemon.inspect_state()["apps"])
        exports = list((self.runtime / "exports").glob("*.json"))
        self.assertEqual(len(exports), 1)
        self.assertEqual(json.loads(exports[0].read_text())["app_id"], "ai.vibapp.uninstall-1")

    def test_invalid_uninstall_disposition_preserves_installed_app(self) -> None:
        self.install()
        rejected = self.call("uninstall", {"disposition": "invented"})
        self.assertEqual(rejected["error"]["code"], "invalid-argument")
        self.assertIn(self.app_id, self.daemon.inspect_state()["apps"])


class SocketRestartSmoke(unittest.TestCase):
    def test_owner_socket_process_restart_preserves_service_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime = base / "runtime"
            promotions = base / "promotions"
            promotions.mkdir()
            record, digest = create_candidate(promotions)
            socket_path = runtime / "run" / "vibappd.sock"
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = str(ROOT)
            env["VIBAPP_SERVICE_RUNTIME_BIN"] = str(SERVICE_RUNTIME)

            def start() -> subprocess.Popen[bytes]:
                process = subprocess.Popen([sys.executable, "-m", "vibapp_daemon", "serve", "--root", str(runtime), "--promotion-root", str(promotions), "--socket", str(socket_path)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not socket_path.exists() and process.poll() is None:
                    time.sleep(0.02)
                self.assertIsNone(process.poll(), process.stderr.read().decode() if process.poll() is not None else "")
                self.assertTrue(socket_path.exists())
                return process

            def send(request: dict[str, Any], promotion: Path | None = None) -> dict[str, Any]:
                transport = {"schema_version": TRANSPORT_SCHEMA, "envelope": request, "promotion_record": str(promotion) if promotion else None}
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(3)
                client.connect(str(socket_path))
                client.sendall(canonical_json(transport) + b"\n")
                chunks = bytearray()
                while not chunks.endswith(b"\n"):
                    chunks.extend(client.recv(8192))
                client.close()
                return json.loads(chunks)

            process = start()
            try:
                app = "ai.vibapp.runtime-service-fixture"
                self.assertNotIn("error", send(envelope("install", {"package_digest_sha256": digest, "enable_after_install": False}, subject=None, key="install"), record))
                self.assertNotIn("error", send(envelope("enable", None, subject=app, key="enable")))
                self.assertNotIn("error", send(envelope("service-start", {"entrypoint": "main-service"}, subject=app, key="start")))
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
            process = start()
            try:
                status = send(envelope("status", None, subject="ai.vibapp.runtime-service-fixture", key="status"))
                self.assertEqual(status["outcome"]["value"]["active_service_entrypoints"], ["main-service"])
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
