from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("validate_native_matrix.py")
SPEC = importlib.util.spec_from_file_location("validate_native_matrix", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NativeMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = SCRIPT.resolve().parents[3]
        cls.matrix_path = SCRIPT.with_name("native-platform-matrix.json")
        cls.matrix = MODULE.load_json(cls.matrix_path)

    def write_matrix(self, value: dict) -> Path:
        temporary = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        with temporary:
            json.dump(value, temporary)
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        return Path(temporary.name)

    def test_current_matrix_is_definition_only_and_valid(self) -> None:
        result = MODULE.validate(self.matrix_path, self.repository_root)
        self.assertEqual([target["current_evidence"]["support_claim"] for target in result["targets"]], ["not-verified", "not-verified"])

    def test_duplicate_json_key_is_rejected(self) -> None:
        temporary = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        with temporary:
            temporary.write('{"schema_version":"one","schema_version":"two"}')
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        with self.assertRaisesRegex(MODULE.MatrixError, "duplicate JSON key"):
            MODULE.load_json(Path(temporary.name))

    def test_release_eligibility_cannot_be_self_promoted(self) -> None:
        value = copy.deepcopy(self.matrix)
        value["targets"][0]["release_eligible"] = True
        with self.assertRaisesRegex(MODULE.MatrixError, "cannot be release eligible"):
            MODULE.validate(self.write_matrix(value), self.repository_root)

    def test_package_phase_cannot_skip_blockers(self) -> None:
        value = copy.deepcopy(self.matrix)
        value["targets"][1]["phases"]["package"] = {
            "state": "runnable-on-matching-native-runner",
            "emits_at_most": "package-candidate-produced",
            "blockers": [],
        }
        with self.assertRaisesRegex(MODULE.MatrixError, "package.*fail closed"):
            MODULE.validate(self.write_matrix(value), self.repository_root)

    def test_native_evidence_cannot_be_claimed_by_editing_the_plan(self) -> None:
        value = copy.deepcopy(self.matrix)
        value["targets"][0]["current_evidence"]["native_compile"] = "passed"
        with self.assertRaisesRegex(MODULE.MatrixError, "overstates current evidence"):
            MODULE.validate(self.write_matrix(value), self.repository_root)

    def test_lockfile_drift_is_rejected(self) -> None:
        value = copy.deepcopy(self.matrix)
        value["toolchain"]["lockfiles"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(MODULE.MatrixError, "lockfile digest drift"):
            MODULE.validate(self.write_matrix(value), self.repository_root)

    def test_workflow_has_no_soft_failure_escape(self) -> None:
        workflow = SCRIPT.with_name("native-platform-workflow.yml").read_text(encoding="utf-8")
        self.assertNotIn("continue-on-error", workflow)
        self.assertNotIn("|| true", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\n]+@(main|master|v\d+)\b")
        self.assertIn("--phase package", workflow)
        self.assertIn("--locked --offline --jobs 1", workflow)
        self.assertIn("vibapp-rust-1.93-offline", workflow)
        self.assertGreaterEqual(workflow.count("assemble_native_package.py --input"), 4)
        self.assertIn("test_assemble_native_package", workflow)
        self.assertGreaterEqual(workflow.count("native_platform::tests"), 2)

    def test_package_assembler_digest_is_bound(self) -> None:
        assembler = self.matrix["toolchain"]["package_assembler"]
        self.assertEqual(assembler["path"], "artifacts/desktop/ci/assemble_native_package.py")
        self.assertEqual(MODULE._sha256(self.repository_root / assembler["path"]), assembler["sha256"])

    def test_launcher_platform_policy_is_digest_bound_without_native_claim(self) -> None:
        policy = self.matrix["toolchain"]["launcher_platform_policy"]
        self.assertEqual(policy["path"], "artifacts/desktop/src-tauri/src/native_platform.rs")
        self.assertEqual(MODULE._sha256(self.repository_root / policy["path"]), policy["sha256"])
        self.assertEqual(policy["validation_scope"], "macos-hosted-policy-tests-only")
        self.assertEqual(policy["native_execution"], "not-run")

    def test_package_definition_is_not_native_package_evidence(self) -> None:
        for target in self.matrix["targets"]:
            self.assertEqual(target["current_evidence"]["package_definition"], "synthetic-fixture-reproduced-not-native")
            self.assertEqual(target["current_evidence"]["package"], "not-produced")
            self.assertEqual(target["current_evidence"]["native_smoke"], "not-run")
            self.assertFalse(target["release_eligible"])
            self.assertEqual(target["phases"]["package"]["state"], "blocked")

    def test_matching_native_host_still_gets_blocked_package_exit(self) -> None:
        with mock.patch.object(MODULE.platform, "system", return_value="Linux"):
            with mock.patch.object(MODULE.sys, "stderr"):
                result = MODULE.preflight(self.matrix_path, self.repository_root, "linux-x86_64", "package")
        self.assertEqual(result, 78)

    def test_launcher_helpers_use_the_platform_policy_boundary(self) -> None:
        source_root = self.repository_root / "artifacts/desktop/src-tauri/src"
        helper_modules = [
            "local_product.rs",
            "registry_integration.rs",
            "delivery_integration.rs",
            "local_orchestrator.rs",
            "local_codeagent.rs",
        ]
        for relative in helper_modules:
            contents = (source_root / relative).read_text(encoding="utf-8")
            self.assertIn("native_platform", contents, relative)
            self.assertNotIn('join("Resources")', contents, relative)
            self.assertNotIn('"/opt/homebrew/bin/python3"', contents, relative)
            self.assertNotIn('"/usr/bin/python3"', contents, relative)

    def test_windows_policy_blockers_remain_fail_closed(self) -> None:
        windows = next(target for target in self.matrix["targets"] if target["id"] == "windows-x86_64")
        blockers = {item["id"]: item for item in windows["blockers"]}
        self.assertIn("deliberately refuses execution", blockers["WINDOWS-IPC-001"]["condition"])
        self.assertIn("fails closed", blockers["WINDOWS-PYTHON-002"]["condition"])
        self.assertIn("refuses BYOM secret access", blockers["WINDOWS-SECRETS-004"]["condition"])


if __name__ == "__main__":
    unittest.main()
