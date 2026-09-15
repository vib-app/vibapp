import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import repair_existing_apps as repair


def completion(app):
    task = {
        "package_intent": {"app_id": app["app_id"], "version": "0.1.1", "entrypoints": [
            {"id": "launcher.main", "kind": "launcher-ui", "label": app["name"], "initial_route": "home"}]},
        "target": {"app_kind": "ui", "required_capabilities": [f"vibapp:experimental-v0/{cap}@0.0.1" for cap in app["capabilities"]]},
        "provider": "openai-codex", "model": repair.CODE_MODEL,
        "provider_execution_identity": {"runtime": {"package_id": "openai-codex-cli-docker"}},
        "limits": {"memory_bytes": 2147483648},
        "need_spec": {"requirements": [{"requirement_id": "requirement.primary", "text": app["description"], "acceptance_examples": [app["acceptance"]]}]},
    }
    return {"cloud_development": {"required_conditions": {"authoritative_registry_no_match": True},
            "task_preparation": {"submission_available": True, "schema_preview": task}}}


class RepairPlanTests(unittest.TestCase):
    def test_only_reviewed_ui_versions_with_bound_compatibility(self):
        for key in ("multiline-notes", "sticky-notes"):
            app = repair.requirements(key)
            self.assertEqual(app["version"], "0.1.1")
            self.assertLessEqual(len(app["description"]), 1000)
            self.assertIn(app["state_contract"]["key"], app["description"])
            self.assertIn("launcher.main/home", app["description"])
            self.assertIsNotNone(repair.validate_task(app, completion(app)))
        with self.assertRaises(ValueError):
            repair.requirements("weekday-water-reminder")

    def test_no_registry_or_admission_override(self):
        app = repair.requirements("multiline-notes")
        for section, field in (("required_conditions", "authoritative_registry_no_match"), ("task_preparation", "submission_available")):
            value = completion(app)
            value["cloud_development"][section][field] = False
            with self.assertRaises(ValueError):
                repair.validate_task(app, value)

    def test_model_summary_cannot_drop_original_compatibility(self):
        app = repair.requirements("sticky-notes")
        value = completion(app)
        value["cloud_development"]["task_preparation"]["schema_preview"]["need_spec"]["requirements"][0]["text"] = "只做简单便签"
        with self.assertRaises(ValueError):
            repair.validate_task(app, value)

    def test_local_unisolated_author_refused(self):
        app = repair.requirements("multiline-notes")
        value = completion(app)
        value["cloud_development"]["task_preparation"]["schema_preview"]["provider_execution_identity"]["runtime"]["package_id"] = "openai-codex-cli"
        with self.assertRaises(ValueError):
            repair.validate_task(app, value)

    def test_identity_capability_resource_drift_rejected(self):
        app = repair.requirements("sticky-notes")
        changes = [("package_intent", "app_id", "ai.vibapp.other"), ("package_intent", "version", "0.1.0"),
                   ("target", "app_kind", "service"), ("target", "required_capabilities", []),
                   ("limits", "memory_bytes", 4294967296), ("package_intent", "entrypoints", [])]
        for section, key, replacement in changes:
            value = completion(app)
            value["cloud_development"]["task_preparation"]["schema_preview"][section][key] = replacement
            with self.assertRaises(ValueError):
                repair.validate_task(app, value)

    def test_duplicate_json_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"a":1,"a":2}')
            with self.assertRaises(ValueError):
                repair.read_json(path)

    def test_synthetic_legacy_codec_fixtures_are_exact(self):
        fixtures = repair.read_json(Path(repair.__file__).with_name("repair_existing_app_state_fixtures.json"))["fixtures"]
        for fixture in fixtures:
            value = bytes.fromhex(fixture["value_hex"])
            if fixture["key"] == "note/body":
                self.assertEqual(value.decode("utf-8"), fixture["expected_text"])
                continue
            # Independent fixture oracle only, never guest or product storage code.
            offset = 4
            self.assertEqual(value[:offset], b"SN01")
            def integer(size):
                nonlocal offset
                result = int.from_bytes(value[offset:offset + size], "little")
                offset += size
                return result
            def string():
                nonlocal offset
                size = integer(2)
                result = value[offset:offset + size].decode("utf-8")
                offset += size
                return result
            result = {"next_id": integer(4)}
            editor = integer(1)
            result["editor"] = "closed" if editor == 0 else "new" if editor == 1 else {"edit": integer(4)}
            result["pending_delete"] = integer(4) if integer(1) else None
            result["error"] = string() if integer(1) else None
            result["draft"] = string()
            result["notes"] = [{"id": integer(4), "pinned": bool(integer(1)), "body": string()} for _ in range(integer(1))]
            self.assertEqual(offset, len(value))
            self.assertEqual(result, fixture["expected"])

    def test_service_plans_remain_non_executable_and_keep_original_kinds(self):
        plans = repair.read_json(Path(repair.__file__).with_name("repair_service_app_requirements.json"))
        self.assertIs(plans["execution_enabled"], False)
        self.assertIs(plans["automatic_user_upgrade"], False)
        self.assertEqual([app["app_kind"] for app in plans["apps"]], ["hybrid", "service"])
        for app in plans["apps"]:
            self.assertEqual(app["capabilities"], ["clock", "kv"])
            with self.assertRaises(ValueError):
                repair.requirements(app["key"])

    def test_old_failure_retained_and_never_silently_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(repair, "baseline", return_value={}), patch.object(repair, "confirm_settings", side_effect=RuntimeError("precise-old-failure")) as confirm:
                with self.assertRaises(RuntimeError):
                    repair.execute(root, root, "multiline-notes")
                self.assertEqual(json.loads((root / "multiline-notes/failure.json").read_text())["message"], "precise-old-failure")
                with self.assertRaises(FileExistsError):
                    repair.execute(root, root, "multiline-notes")
                self.assertEqual(confirm.call_count, 1)


if __name__ == "__main__":
    unittest.main()
