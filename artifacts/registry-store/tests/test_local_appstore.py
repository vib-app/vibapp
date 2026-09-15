from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import local_appstore as appstore  # noqa: E402


FIXED_TIME = "2026-08-27T03:00:00Z"


def descriptor(path: str, data: bytes, media_type: str = "application/octet-stream") -> dict[str, Any]:
    return {
        "path": path,
        "media_type": media_type,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def write_candidate(
    root: Path,
    *,
    app_id: str = "ai.vibapp.local-store-test",
    version: str = "0.1.0",
    component: bytes = b"\x00asm\x0d\x00\x01\x00local-appstore-test",
    artifact_path: str | None = None,
    state_schema: int = 1,
    migratable_from_min: int = 1,
    migratable_from_max: int = 1,
    display_name: str = "Local Store Test",
    description: str = "Synthetic independently verified local AppStore fixture.",
    presentation: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    root.mkdir(parents=True)
    package = root / "package"
    package.mkdir()
    provenance = b'{"source":"synthetic-local-appstore-test"}\n'
    sbom = b'{"bomFormat":"CycloneDX","specVersion":"1.5"}\n'
    component_descriptor = descriptor("component.wasm", component, "application/wasm")
    provenance_descriptor = descriptor("provenance.json", provenance, "application/json")
    sbom_descriptor = descriptor("sbom.cdx.json", sbom, "application/vnd.cyclonedx+json")
    assets: list[dict[str, Any]] = []
    extra_descriptors: list[dict[str, Any]] = []
    if artifact_path is not None:
        extra = b"escape-test"
        extra_descriptor = descriptor(artifact_path, extra)
        assets.append(extra_descriptor)
        extra_descriptors.append(extra_descriptor)
    manifest = {
        "schema_version": appstore.MANIFEST_SCHEMA,
        "package_format": appstore.PACKAGE_FORMAT,
        "app": {
            "id": app_id,
            "version": version,
            "kind": "ui",
            "display_name": display_name,
            "description": description,
            "publisher": {"id": "ai.vibapp.tests", "display_name": "VibApp Tests"},
        },
        "artifacts": {
            "canonical_component": component_descriptor,
            "assets": assets,
            "browser_derivations": [],
            "provenance": provenance_descriptor,
            "sbom": sbom_descriptor,
        },
        "runtime": {
            "contract": "vibapp:experimental-v0@0.0.1",
            "wasi": "0.2",
            "world": "ui-only-reference",
            "required_imports": [],
            "profiles": [
                {
                    "profile": "desktop",
                    "mode": "full",
                    "background": "not-applicable",
                    "artifact_role": "canonical-component",
                    "degradation": "",
                }
            ],
            "platforms": [{"os": "macos", "arch": "aarch64", "profiles": ["desktop"]}],
        },
        "entrypoints": [
            {
                "id": "main",
                "kind": "launcher-ui",
                "label": "Main",
                "profiles": ["desktop"],
                "routes": {"initial": "home", "allowed": ["home"]},
                "restoration": "route-only",
            }
        ],
        "capabilities": [],
        "resources": {
            "linear_memory_bytes": 1048576,
            "event_wall_time_ms": 100,
            "health_migration_wall_time_ms": 1000,
            "output_bytes": 65536,
            "stored_data_bytes": 0,
            "durable_schedules": 0,
            "log_bytes_per_day": 0,
        },
        "state": {
            "schema": state_schema,
            "migratable_from_min": migratable_from_min,
            "migratable_from_max": migratable_from_max,
        },
        "lifecycle": {
            "disable": {
                "retains_package": True,
                "retains_state": True,
                "stops_services": True,
                "cancels_schedules": True,
            },
            "uninstall": {
                "allowed_data_dispositions": ["delete", "retain"],
                "default_data_disposition": "retain",
            },
        },
        "source": {
            "language": "rust",
            "revision": "synthetic-test",
            "cargo_lock_sha256": "a" * 64,
            "builder_image_digest": "sha256:" + "b" * 64,
        },
        "license": {"spdx_expression": "Apache-2.0"},
        "privacy": {"stores_personal_data": False, "network_access": "none"},
        "verification": {"status": "verified", "revocation": "not-revoked"},
    }
    descriptors = [component_descriptor, *extra_descriptors, provenance_descriptor, sbom_descriptor]
    digest = appstore._package_digest(manifest, descriptors)
    manifest_bytes = appstore.canonical_json(manifest) + b"\n"
    (package / "component.wasm").write_bytes(component)
    (package / "provenance.json").write_bytes(provenance)
    (package / "sbom.cdx.json").write_bytes(sbom)
    (package / "manifest.json").write_bytes(manifest_bytes)
    candidate = {
        "schema_version": appstore.CANDIDATE_SCHEMA,
        "document_type": "verifier-promoted-candidate",
        "state": "candidate-ready",
        "job_id": "job-local-appstore-test",
        "source_tree_sha256": "c" * 64,
        "package_digest_sha256": digest,
        "package_directory": "package",
        "component": {
            "path": "component.wasm",
            "sha256": component_descriptor["sha256"],
            "size_bytes": len(component),
        },
        "manifest": {
            "path": "manifest.json",
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
        },
        "quarantine_receipt_sha256": "d" * 64,
        "verification": {
            "authority": "independent-verifier",
            "verifier_version": "app-verifier.experimental-v1",
            "verified_at_utc": FIXED_TIME,
            "checks": [
                {
                    "id": "package-digest",
                    "outcome": "pass",
                    "tool": "synthetic-test",
                    "detail": "recomputed",
                }
            ],
        },
        "authority": {"install": "daemon", "publish": "none"},
    }
    if presentation is not None:
        candidate["presentation"] = presentation
    candidate_path = root / "candidate.json"
    candidate_path.write_bytes(appstore.canonical_json(candidate) + b"\n")
    return candidate_path, digest


class LocalAppStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.input = self.base / "input"
        self.store_root = self.base / "store"
        self.candidate, self.digest = write_candidate(self.input)
        self.store = appstore.LocalAppStore(self.store_root, clock=lambda: FIXED_TIME)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_code(self, expected: str, callback: Any) -> None:
        with self.assertRaises(appstore.StoreError) as raised:
            callback()
        self.assertEqual(raised.exception.code, expected)

    def test_ingest_is_private_content_addressed_and_idempotent(self) -> None:
        first = self.store.ingest(self.candidate)
        self.assertTrue(first["created"])
        record = first["record"]
        self.assertEqual(record["schema_version"], appstore.RECORD_SCHEMA)
        self.assertEqual(record["state"], "private")
        self.assertEqual(record["visibility"], "private")
        self.assertEqual(record["publication"], {"state": "not-published", "authority": "none"})
        self.assertEqual(record["authority"], {"install": "daemon", "publish": "none"})
        self.assertEqual(record["digests"]["package_sha256"], self.digest)
        self.assertEqual(
            record["app_state"],
            {"schema": 1, "migratable_from_min": 1, "migratable_from_max": 1},
        )
        self.assertEqual(record["verification"]["authority"], "independent-verifier")
        self.assertEqual(
            record["presentation"],
            {
                "schema_version": appstore.PRESENTATION_SCHEMA,
                "preferred_width": 900,
                "preferred_height": 640,
                "minimum_width": 480,
                "minimum_height": 360,
                "resizable": True,
            },
        )
        stored_candidate = self.store_root / record["paths"]["candidate"]
        self.assertTrue(stored_candidate.is_file())
        self.assertTrue((stored_candidate.parent / "package" / "component.wasm").is_file())

        replay = self.store.ingest(self.candidate)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["record"], record)
        listing = self.store.list()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["items"][0], record)
        self.assertEqual(self.store.detail(self.digest)["record"], record)

    def test_presentation_sidecar_is_bounded_and_legacy_records_migrate_on_read(self) -> None:
        explicit = {
            "schema_version": appstore.PRESENTATION_SCHEMA,
            "preferred_width": 520,
            "preferred_height": 300,
            "minimum_width": 420,
            "minimum_height": 240,
            "resizable": True,
        }
        clock_root = self.base / "clock"
        clock_candidate, clock_digest = write_candidate(
            clock_root,
            app_id="ai.vibapp.clock",
            display_name="LED 数字时钟",
            description="本地时钟。",
            presentation=explicit,
        )
        record = self.store.ingest(clock_candidate)["record"]
        self.assertEqual(record["presentation"], explicit)

        legacy = dict(record)
        legacy.pop("presentation")
        connection = sqlite3.connect(self.store.database)
        try:
            connection.execute(
                "UPDATE releases SET record_json = ? WHERE package_digest = ?",
                (appstore.canonical_json(legacy), clock_digest),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.store.detail(clock_digest)["record"]["presentation"], explicit)

        invalid_root = self.base / "invalid-presentation"
        invalid = dict(explicit)
        invalid["preferred_width"] = 320
        invalid["minimum_width"] = 420
        invalid_candidate, _ = write_candidate(
            invalid_root,
            app_id="ai.vibapp.invalid-presentation",
            presentation=invalid,
        )
        self.assert_code("incompatible-contract", lambda: self.store.ingest(invalid_candidate))

    def test_component_manifest_and_extra_file_tamper_fail_closed(self) -> None:
        component = self.input / "package/component.wasm"
        component.write_bytes(component.read_bytes() + b"tamper")
        self.assert_code("integrity-failure", lambda: self.store.ingest(self.candidate))

        second = self.base / "manifest-tamper"
        candidate, _ = write_candidate(second, app_id="ai.vibapp.manifest-tamper")
        document = json.loads(candidate.read_text())
        document["manifest"]["sha256"] = "0" * 64
        candidate.write_bytes(appstore.canonical_json(document) + b"\n")
        self.assert_code("integrity-failure", lambda: self.store.ingest(candidate))

        third = self.base / "extra"
        candidate3, _ = write_candidate(third, app_id="ai.vibapp.extra-file")
        (third / "package/extra.txt").write_text("not declared", encoding="utf-8")
        self.assert_code("integrity-failure", lambda: self.store.ingest(candidate3))

    def test_verifier_and_private_authority_are_mandatory(self) -> None:
        document = json.loads(self.candidate.read_text())
        document["authority"]["publish"] = "public"
        self.candidate.write_bytes(appstore.canonical_json(document) + b"\n")
        self.assert_code("permission-denied", lambda: self.store.ingest(self.candidate))

        other = self.base / "self-verified"
        candidate, _ = write_candidate(other, app_id="ai.vibapp.self-verified")
        document = json.loads(candidate.read_text())
        document["verification"]["authority"] = "builder"
        candidate.write_bytes(appstore.canonical_json(document) + b"\n")
        self.assert_code("integrity-failure", lambda: self.store.ingest(candidate))

    def test_symlink_hardlink_and_manifest_escape_are_rejected(self) -> None:
        component = self.input / "package/component.wasm"
        external = self.base / "outside.wasm"
        external.write_bytes(component.read_bytes())
        component.unlink()
        component.symlink_to(external)
        self.assert_code("permission-denied", lambda: self.store.ingest(self.candidate))

        hardlink_root = self.base / "hardlink"
        hard_candidate, _ = write_candidate(hardlink_root, app_id="ai.vibapp.hardlink")
        os.link(hardlink_root / "package/component.wasm", self.base / "component-copy.wasm")
        self.assert_code("permission-denied", lambda: self.store.ingest(hard_candidate))

        escape_root = self.base / "escape"
        escape_candidate, _ = write_candidate(
            escape_root, app_id="ai.vibapp.path-escape", artifact_path="../escape.bin"
        )
        self.assert_code("integrity-failure", lambda: self.store.ingest(escape_candidate))

    def test_duplicate_digest_with_different_candidate_payload_is_rejected(self) -> None:
        self.store.ingest(self.candidate)
        changed = json.loads(self.candidate.read_text())
        changed["verification"]["checks"][0]["detail"] = "same package, different candidate payload"
        self.candidate.write_bytes(appstore.canonical_json(changed) + b"\n")
        self.assert_code("conflict", lambda: self.store.ingest(self.candidate))

    def test_same_app_version_with_different_package_digest_is_rejected(self) -> None:
        self.store.ingest(self.candidate)
        other = self.base / "other-digest"
        candidate, _ = write_candidate(other, component=b"\x00asm\x0d\x00\x01\x00different")
        self.assert_code("conflict", lambda: self.store.ingest(candidate))

    def test_same_app_new_version_is_retained_as_an_exact_update_release(self) -> None:
        first = self.store.ingest(self.candidate)["record"]
        update_root = self.base / "update-release"
        update_candidate, update_digest = write_candidate(
            update_root,
            version="0.2.0",
            component=b"\x00asm\x0d\x00\x01\x00updated",
            state_schema=2,
            migratable_from_min=1,
            migratable_from_max=1,
        )
        update = self.store.ingest(update_candidate)["record"]

        self.assertNotEqual(first["digests"]["package_sha256"], update_digest)
        self.assertEqual(update["app"]["id"], first["app"]["id"])
        self.assertEqual(update["app"]["version"], "0.2.0")
        self.assertEqual(
            update["app_state"],
            {"schema": 2, "migratable_from_min": 1, "migratable_from_max": 1},
        )
        self.assertEqual(self.store.list()["count"], 2)
        self.assertEqual(self.store.detail(update_digest)["record"], update)

    def test_list_is_bounded_and_uses_a_stable_digest_cursor(self) -> None:
        first = self.store.ingest(self.candidate)["record"]
        other_root = self.base / "page-two"
        other_candidate, other_digest = write_candidate(
            other_root, app_id="ai.vibapp.z-page-two", version="0.2.0"
        )
        self.store.ingest(other_candidate)
        page_one = self.store.list(limit=1)
        self.assertEqual(page_one["count"], 1)
        self.assertEqual(page_one["items"][0], first)
        self.assertEqual(page_one["next_cursor"], self.digest)
        page_two = self.store.list(limit=1, cursor=page_one["next_cursor"])
        self.assertEqual(page_two["items"][0]["digests"]["package_sha256"], other_digest)
        self.assertIsNone(page_two["next_cursor"])
        self.assert_code("resource-limit", lambda: self.store.list(limit=101))
        self.assert_code("not-found", lambda: self.store.list(cursor="f" * 64))

    def test_withdraw_is_durable_idempotent_and_does_not_delete_bytes(self) -> None:
        record = self.store.ingest(self.candidate)["record"]
        result = self.store.withdraw(self.digest, "publisher requested local withdrawal")
        self.assertTrue(result["changed"])
        self.assertEqual(result["record"]["state"], "withdrawn")
        self.assertEqual(self.store.list()["count"], 0)
        self.assertEqual(self.store.list(include_withdrawn=True)["count"], 1)
        self.assertTrue((self.store_root / record["paths"]["candidate"]).is_file())
        replay = self.store.withdraw(self.digest, "publisher requested local withdrawal")
        self.assertFalse(replay["changed"])
        self.assert_code(
            "conflict", lambda: self.store.withdraw(self.digest, "a different immutable reason")
        )

    def test_detail_rehashes_stored_bytes(self) -> None:
        record = self.store.ingest(self.candidate)["record"]
        stored_component = self.store_root / record["paths"]["package"] / "component.wasm"
        os.chmod(stored_component, 0o600)
        stored_component.write_bytes(stored_component.read_bytes() + b"tamper")
        self.assert_code("integrity-failure", lambda: self.store.detail(self.digest))

    def test_concurrent_cli_ingest_has_one_creator_and_one_idempotent_replay(self) -> None:
        concurrent_store = self.base / "concurrent-store"
        command = [
            sys.executable,
            str(ROOT / "local_appstore.py"),
            "--store-root",
            str(concurrent_store),
            "ingest",
            str(self.candidate),
        ]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_out, first_err = first.communicate(timeout=20)
        second_out, second_err = second.communicate(timeout=20)
        self.assertEqual((first.returncode, second.returncode), (0, 0), first_err + second_err)
        results = [json.loads(first_out), json.loads(second_out)]
        self.assertEqual(sorted(item["created"] for item in results), [False, True])
        listing = appstore.LocalAppStore(concurrent_store).list()
        self.assertEqual(listing["count"], 1)


if __name__ == "__main__":
    unittest.main()
