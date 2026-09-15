from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

import final_product_audit as audit


historical_acceptance = unittest.skipUnless(
    os.environ.get("VIBAPP_RUN_HISTORICAL_ACCEPTANCE") == "1",
    "retained August acceptance/package evidence is opt-in, not a hermetic unit test",
)


class FinalProductAuditTests(unittest.TestCase):
    def test_regression_requires_exact_terminal_pass_marker(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-final-audit-") as directory:
            path = Path(directory) / "regression.log"
            path.write_text("[vibapp regression] PASS\n", encoding="utf-8")
            with self.assertRaises(audit.AuditFailure):
                audit.require_regression(path)

    @historical_acceptance
    def test_current_regression_log_has_complete_ordered_suite_evidence(self):
        path = Path("/private/tmp/vibapp-round006-full-regression.log")
        if not path.exists():
            self.skipTest("current bounded regression log is not present")
        result = audit.require_regression(path)
        self.assertEqual(result["ordered_markers"], len(audit.REGRESSION_MARKERS))
        self.assertGreaterEqual(result["python_tests"], 200)
        self.assertGreaterEqual(result["rust_tests"], 190)
        self.assertGreaterEqual(result["tap_tests"], 35)

    @historical_acceptance
    def test_goal_gate_requires_fresh_goal_kind_and_zero_findings(self):
        exact_path = audit.GOAL_GATE_DIR / "result.json"
        current = json.loads(exact_path.read_text(encoding="utf-8"))
        if current.get("status") == "passed":
            result = audit.require_goal_gate(exact_path)
            self.assertEqual(result["gate_id"], audit.GOAL_GATE_ID)
            self.assertEqual(result["reviewer_identity"], "round006_goal_acceptance")
        else:
            with self.assertRaises(audit.AuditFailure):
                audit.require_goal_gate(exact_path)
        with tempfile.TemporaryDirectory(prefix="vibapp-final-audit-") as directory:
            wrong_path = Path(directory) / "result.json"
            wrong_path.write_text(exact_path.read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaises(audit.AuditFailure):
                audit.require_goal_gate(wrong_path)

    def test_goal_gate_metadata_guards_cover_exact_identity_and_independence(self):
        source = Path(audit.__file__).read_text(encoding="utf-8")
        for guard in (
            'result.get("gate_id") != GOAL_GATE_ID',
            'result.get("linked_round") != "round-006"',
            'result.get("reviewer_identity") != "round006_goal_acceptance"',
            'result.get("reviewer_identity") == result.get("owner_identity")',
        ):
            self.assertIn(guard, source)

    @historical_acceptance
    def test_current_shared_gui_is_exact(self):
        result = audit.require_shared_gui()
        self.assertEqual(set(result["shared_sha256"]), {"app.js", "styles.css", "favicon.svg"})

    @historical_acceptance
    def test_current_package_is_current_and_source_exact(self):
        result = audit.require_package_source_parity()
        self.assertEqual(result["authored_resource_count"], 31)
        self.assertEqual(set(result["binary_build_identity"]), {"launcher", "runtime", "service-runtime"})
        self.assertEqual(
            result["current_desktop_inputs"]["sha256"],
            result["packaged_build_provenance"]["sha256"],
        )
        self.assertEqual(result["codesign"]["returncode"], 0)
        self.assertTrue(result["service_runtime_not_older_than_inputs"])

    def test_current_health_does_not_depend_on_historical_job_pids(self):
        source = Path(audit.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DESKTOP_JOB", source)
        self.assertNotIn("WEBSITE_JOB", source)
        self.assertNotIn("require_recorded_process", source)
        self.assertIn("require_current_process", source)
        self.assertIn('"current-ps-snapshot"', source)
        self.assertIn("require_http(arguments.website_url)", source)
        self.assertIn("require_http(arguments.backend_health_url)", source)
        self.assertIn("require_http(arguments.preview_health_url)", source)

    def test_desktop_provenance_rejects_digest_drift_and_extra_keys(self):
        current = {
            "schema_version": "vibapp.desktop-build-inputs.v1",
            "sha256": "1" * 64,
            "file_count": 1,
        }
        provenance = {
            "schema_version": "vibapp.desktop-build-inputs.v1",
            "sha256": "1" * 64,
            "public_registry_source_sha256": "3" * 64,
            "public_registry_snapshot_sha256": "4" * 64,
            "public_registry_locator_index_sha256": "5" * 64,
        }
        with tempfile.TemporaryDirectory(prefix="vibapp-final-audit-") as directory:
            path = Path(directory) / "desktop-inputs.json"
            path.write_text(json.dumps(provenance), encoding="utf-8")
            self.assertEqual(audit.require_desktop_build_provenance(path, current), provenance)
            provenance["sha256"] = "2" * 64
            path.write_text(json.dumps(provenance), encoding="utf-8")
            with self.assertRaises(audit.AuditFailure):
                audit.require_desktop_build_provenance(path, current)
            provenance["sha256"] = "1" * 64
            provenance["unexpected"] = True
            path.write_text(json.dumps(provenance), encoding="utf-8")
            with self.assertRaises(audit.AuditFailure):
                audit.require_desktop_build_provenance(path, current)


if __name__ == "__main__":
    unittest.main()
