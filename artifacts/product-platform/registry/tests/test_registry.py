from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
REPO = BASE.parents[2]
MODULE_PATH = BASE / "registry_service.py"

SPEC = importlib.util.spec_from_file_location("registry_service", MODULE_PATH)
assert SPEC and SPEC.loader
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


def load_validator_module():
    path = REPO / "artifacts" / "schema-contract-proposal" / "validate_schemas.py"
    spec = importlib.util.spec_from_file_location("candidate_schema_validator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RegistryProductTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = registry.load_records()
        cls.focus_need = registry.validate_need(
            registry.load_json(BASE / "fixtures" / "needs" / "focus.valid.json", registry.MAX_NEED_BYTES)
        )
        cls.no_match_need = registry.validate_need(
            registry.load_json(BASE / "fixtures" / "needs" / "no-match.valid.json", registry.MAX_NEED_BYTES)
        )

    def test_catalog_has_five_deterministic_records(self) -> None:
        self.assertEqual(len(self.records), 5)
        self.assertEqual(
            registry.canonical_bytes(self.records),
            registry.canonical_bytes(registry.load_records()),
        )

    def test_browser_artifact_is_bound_to_record_and_honestly_synthetic(self) -> None:
        bindings = registry.browser_artifact_bindings(self.records)
        self.assertEqual(len(bindings), 1)
        binding = bindings[0]
        record = next(item for item in self.records if item["app"]["id"] == binding["app_id"])
        self.assertEqual(binding["profile"], "web-preview")
        self.assertEqual(
            binding["canonical_package_digest_sha256"],
            record["package"]["package_digest_sha256"],
        )
        self.assertEqual(
            binding["derived_from_sha256"],
            binding["canonical_component_reference_sha256"],
        )
        self.assertEqual(binding["artifact"]["size_bytes"], 44)
        self.assertEqual(
            binding["artifact"]["sha256"],
            "a5cd7802b1e10dde41d64bd0c1921ab6f775032c76dcad590d33b1b39ace1012",
        )
        self.assertEqual(
            binding["attestation"]["binding_payload_sha256"],
            registry.digest({key: value for key, value in binding.items() if key not in {"schema_version", "attestation"}}),
        )
        self.assertEqual(binding["attestation"]["verification_state"], "verified-synthetic-fixture")
        self.assertFalse(binding["attestation"]["canonical_component_transformation_proven"])

    def test_valid_need_returns_explained_stable_matches(self) -> None:
        first = registry.search(self.focus_need, self.records, 5)
        second = registry.search(self.focus_need, self.records, 5)
        self.assertEqual(registry.canonical_bytes(first), registry.canonical_bytes(second))
        self.assertEqual(first["platform_status"], "product-platform-local-hold")
        self.assertGreaterEqual(len(first["matched"]), 1)
        winner = first["matched"][0]
        self.assertEqual(winner["record"]["app"]["id"], "ai.vibapp.fixture.focus-board")
        self.assertIn("requirement.focus-plan", winner["matched"])
        self.assertIn("requirement.private-notes", winner["unmet"])
        self.assertTrue(winner["explanation"])
        self.assertTrue(all(item["evidence_fields"] for item in winner["explanation"]))
        self.assertEqual(
            [item["match"]["stable_order"] for item in first["matched"]],
            list(range(len(first["matched"]))),
        )

    def test_no_match_is_an_allowed_result(self) -> None:
        result = registry.search(self.no_match_need, self.records, 5)
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["search_response"]["matches"], [])
        self.assertEqual(result["empty_reason"], "No candidate passed every hard filter.")
        self.assertEqual(len(result["rejected"]), len(self.records))
        self.assertTrue(all("platform" in item["rejected_reasons"] for item in result["rejected"]))

    def test_hard_rejection_cannot_be_reintroduced_by_score(self) -> None:
        need = copy.deepcopy(self.focus_need)
        need["negative_constraints"].append({
            "constraint_id": "constraint.reject-focus",
            "kind": "forbidden-package",
            "value": "ai.vibapp.fixture.focus-board",
            "source": "rejection-feedback",
        })
        result = registry.search(need, self.records, 5)
        matched_ids = {item["record"]["app"]["id"] for item in result["matched"]}
        self.assertNotIn("ai.vibapp.fixture.focus-board", matched_ids)
        rejected = next(
            item for item in result["rejected"]
            if item["candidate"]["app_id"] == "ai.vibapp.fixture.focus-board"
        )
        self.assertIn("negative-constraint", rejected["rejected_reasons"])

    def test_unknown_field_and_control_character_are_rejected(self) -> None:
        malicious = registry.load_json(
            BASE / "fixtures" / "needs" / "malicious.invalid.json", registry.MAX_NEED_BYTES
        )
        with self.assertRaises(registry.InputError):
            registry.validate_need(malicious)
        without_extra = copy.deepcopy(malicious)
        without_extra.pop("debug_command")
        with self.assertRaisesRegex(registry.InputError, "control character"):
            registry.validate_need(without_extra)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":"one","schema_version":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(registry.InputError, "duplicate JSON key"):
                registry.load_json(path, registry.MAX_NEED_BYTES)

    def test_non_standard_nan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nan.json"
            path.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(registry.InputError, "non-standard JSON constant"):
                registry.load_json(path, registry.MAX_NEED_BYTES)

    def test_oversized_input_is_rejected_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.json"
            path.write_bytes(b" " * (registry.MAX_NEED_BYTES + 1))
            with self.assertRaisesRegex(registry.InputError, "input exceeds"):
                registry.load_json(path, registry.MAX_NEED_BYTES)

    def test_product_output_validates_against_candidate_schemas(self) -> None:
        validator_module = load_validator_module()
        schema_dir = REPO / "artifacts" / "schema-contract-proposal" / "schemas"
        validator = validator_module.CandidateValidator(sorted(schema_dir.glob("*.json")))
        registry_schema = validator.file_documents["registry-record.product-v0.schema.json"]
        registry_base = registry_schema["$id"]
        for record in self.records:
            self.assertEqual(validator.validate(record, registry_schema, registry_base), [])

        search_schema = validator.file_documents["search.product-v0.schema.json"]
        search_base = search_schema["$id"]
        result = registry.search(self.focus_need, self.records, 5)
        self.assertEqual(
            validator.validate(result["search_response"], search_schema, search_base),
            [],
        )
        for item in result["matched"]:
            self.assertEqual(validator.validate(item["match"], search_schema, search_base), [])

    def test_cli_invalid_input_has_closed_error_and_no_traceback(self) -> None:
        command = [
            sys.executable,
            str(MODULE_PATH),
            "validate-need",
            "--need",
            str(BASE / "fixtures" / "needs" / "malicious.invalid.json"),
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"]["code"], "invalid-argument")
        self.assertNotIn("Traceback", completed.stdout)


if __name__ == "__main__":
    unittest.main()
