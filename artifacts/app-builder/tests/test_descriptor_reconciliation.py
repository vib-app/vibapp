from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from common import PipelineError  # noqa: E402
from descriptor_reconciliation import (  # noqa: E402
    _strict_inspector_output,
    expected_guest_descriptor,
    reconcile_guest_descriptor,
    validated_component_inspector,
)


def manifest_fixture() -> dict:
    return {
        "app": {
            "id": "ai.vibapp.fixture",
            "version": "1.2.3",
            "kind": "hybrid",
            "display_name": "Descriptor Fixture",
        },
        "entrypoints": [
            {
                "id": "main",
                "kind": "launcher-ui",
                "label": "Main window",
                "routes": {"initial": "home", "allowed": ["home"]},
            },
            {
                "id": "worker",
                "kind": "service",
                "label": "Background worker",
                "triggers": ["manual"],
                "health_check_interval_seconds": 60,
            },
            {
                "id": "preferences",
                "kind": "settings",
                "label": "Preferences",
                "schema_export": "get-settings-schema",
            },
        ],
    }


class DescriptorReconciliationTests(unittest.TestCase):
    def test_exact_descriptor_accepts_entrypoint_order_difference(self) -> None:
        manifest = manifest_fixture()
        descriptor = expected_guest_descriptor(manifest)
        descriptor["entrypoints"].reverse()

        reconciled = reconcile_guest_descriptor(manifest, descriptor)

        self.assertEqual(reconciled["id"], manifest["app"]["id"])
        self.assertEqual(len(reconciled["entrypoints"]), 3)

    def test_every_descriptor_identity_field_is_manifest_bound(self) -> None:
        cases = {
            "id": "ai.vibapp.other",
            "version": "9.9.9",
            "kind": "ui",
            "display_name": "Different name",
        }
        for field, replacement in cases.items():
            with self.subTest(field=field):
                manifest = manifest_fixture()
                descriptor = expected_guest_descriptor(manifest)
                descriptor[field] = replacement
                with self.assertRaisesRegex(PipelineError, field):
                    reconcile_guest_descriptor(manifest, descriptor)

    def test_every_entrypoint_descriptor_field_is_manifest_bound(self) -> None:
        cases = {
            "kind": "settings",
            "label": "Different label",
            "initial_route": "other",
        }
        for field, replacement in cases.items():
            with self.subTest(field=field):
                manifest = manifest_fixture()
                descriptor = expected_guest_descriptor(manifest)
                descriptor["entrypoints"][0][field] = replacement
                with self.assertRaisesRegex(PipelineError, "entrypoint main"):
                    reconcile_guest_descriptor(manifest, descriptor)

    def test_missing_extra_and_duplicate_entrypoints_are_rejected(self) -> None:
        manifest = manifest_fixture()
        descriptor = expected_guest_descriptor(manifest)
        descriptor["entrypoints"].pop()
        with self.assertRaisesRegex(PipelineError, "missing"):
            reconcile_guest_descriptor(manifest, descriptor)

        descriptor = expected_guest_descriptor(manifest)
        descriptor["entrypoints"].append(
            {
                "id": "extra",
                "kind": "service",
                "label": "Extra",
                "initial_route": None,
            }
        )
        with self.assertRaisesRegex(PipelineError, "extra"):
            reconcile_guest_descriptor(manifest, descriptor)

        descriptor = expected_guest_descriptor(manifest)
        descriptor["entrypoints"].append(copy.deepcopy(descriptor["entrypoints"][0]))
        with self.assertRaisesRegex(PipelineError, "duplicated"):
            reconcile_guest_descriptor(manifest, descriptor)

    def test_non_ui_entrypoints_require_no_initial_route(self) -> None:
        manifest = manifest_fixture()
        descriptor = expected_guest_descriptor(manifest)
        worker = next(item for item in descriptor["entrypoints"] if item["id"] == "worker")
        worker["initial_route"] = "home"
        with self.assertRaisesRegex(PipelineError, "entrypoint worker"):
            reconcile_guest_descriptor(manifest, descriptor)

    def test_inspector_protocol_rejects_extra_and_duplicate_fields(self) -> None:
        value = {
            "schema_version": "vibapp.component-inspector.protocol.experimental-v1",
            "component_sha256": "0" * 64,
            "world": "ui-only-reference",
            "descriptor": {},
            "isolation": {},
            "extra": True,
        }
        with self.assertRaises(PipelineError):
            _strict_inspector_output(
                json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        duplicate = (
            b'{"schema_version":"a","schema_version":"b",'
            b'"component_sha256":"0","world":"w","descriptor":{},"isolation":{}}\n'
        )
        with self.assertRaisesRegex(PipelineError, "duplicate"):
            _strict_inspector_output(duplicate)

    def test_untrusted_inspector_bytes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-inspector-negative-") as temporary:
            path = Path(temporary) / "inspector"
            path.write_bytes(b"not the fixed inspector")
            path.chmod(0o700)
            with self.assertRaisesRegex(PipelineError, "accepted fixed executable"):
                validated_component_inspector(path)


if __name__ == "__main__":
    unittest.main()
