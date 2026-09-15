"""Offline tests for approved-batch orchestration; no model or source execution."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import everyday_apps_workflow as workflow


class EverydayWorkflowTests(unittest.TestCase):
    def completion_fixture(self, root):
        directory = root / "calculator"
        directory.mkdir()
        (directory / "submit-need.json").write_text(json.dumps({"need": {"need_id": "need-test"}}))
        previous = {"provider": "openai-codex", "model": workflow.CODE_MODEL, "job_id": "previous-job",
                    "immutable_task_digest_sha256": "a" * 64,
                    "execution_attempt": {"attempt_id": "previous-attempt", "ordinal": 1},
                    "consent": {"consent_id": "previous-consent", "single_use": True,
                                "attempt_id": "previous-attempt", "expires_at_utc": "2026-01-01T00:00:00Z"}}
        (directory / "task.json").write_text(json.dumps(previous, separators=(",", ":")) + "\n")
        (directory / "registry.json").write_bytes(b'{"status":"previous-no-match"}\n')
        app = {"name": "Calculator", "capabilities": ["kv", "settings"], "acceptance": "1+2=3"}
        return directory, previous, app

    def completion_response(self, ordinal):
        return {"cloud_development": {
            "task_preparation": {"schema_preview": {
                "provider": "openai-codex", "model": workflow.CODE_MODEL,
                "job_id": f"fresh-job-{ordinal}", "consent": {"consent_id": f"fresh-consent-{ordinal}"},
            }, "submission_available": True},
            "required_conditions": {"authoritative_registry_no_match": True},
        }, "registry": {"status": "no-match", "confirmation": ordinal}}

    def test_settings_are_checked_without_writes_or_auth_copy(self):
        calls = []
        def observed(root, command, arguments):
            calls.append(command)
            return {
                "health": {"build_input_receipt": {"sha256": "current"}},
                "get_codeagent_settings": {"selectedProvider": "codex", "modelByProvider": {"codex": workflow.CODE_MODEL}},
                "get_model_settings": {"generation": {"enabled": True, "model": workflow.RECEPTION_MODEL}},
            }[command]
        with patch.object(workflow, "bridge", observed), patch.object(workflow, "current_desktop_build_inputs_sha256", return_value="current"):
            workflow.confirm_settings(Path("/synthetic"))
        self.assertEqual(calls, ["health", "get_codeagent_settings", "get_model_settings"])

    def test_stale_bridge_stops_before_settings_and_need_submission(self):
        with patch.object(workflow, "bridge", return_value={"build_input_receipt": {"sha256": "old"}}) as bridge, patch.object(workflow, "current_desktop_build_inputs_sha256", return_value="new"):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                workflow.confirm_settings(Path("/synthetic"))
        self.assertEqual(bridge.call_count, 1)

    def test_unsupported_capability_stops_before_any_bridge_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"apps": [{"key": "recorder", "generation_ready": False, "blocker": "microphone unavailable"}]}))
            with patch.object(workflow, "CATALOG", catalog), patch.object(workflow, "bridge") as bridge:
                with self.assertRaisesRegex(ValueError, "microphone unavailable"):
                    workflow.prepare(root / "batch", root, "recorder")
                bridge.assert_not_called()
                self.assertFalse((root / "batch").exists())

    def test_completion_has_no_publication_or_model_switch(self):
        app = {"name": "Calculator", "capabilities": ["kv", "settings"], "acceptance": "1+2=3"}
        response = {"cloud_development": {
            "task_preparation": {"schema_preview": {"provider": "openai-codex", "model": workflow.CODE_MODEL, "job_id": "job-test"}, "submission_available": True},
            "required_conditions": {"authoritative_registry_no_match": True},
        }, "registry": {"status": "no-match"}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calculator").mkdir()
            (root / "calculator/submit-need.json").write_text(json.dumps({"need": {"need_id": "need-test"}}))
            with patch.object(workflow, "app_requirements", return_value=app), patch.object(workflow, "confirm_settings"), patch.object(workflow, "bridge", return_value=response) as bridge:
                workflow.complete(root, root, "calculator")
            payload = bridge.call_args.args[2]
            self.assertIs(payload["public_publication_consent"], False)
            self.assertIs(payload["remote_processing_consent"], True)
            self.assertEqual(payload["package_id"], "ai.vibapp.everyday.calculator")
            self.assertEqual(bridge.call_args.args[1], "complete_need")
            self.assertEqual((root / "calculator/task.json").stat().st_mode & 0o777, 0o600)

    def test_admitted_delivery_cannot_be_overwritten_or_reconfirmed(self):
        for admission in ({"status": "queued"}, {"status": "running"}, {"status": "failed"}, {}):
            with self.subTest(admission=admission), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory, _, app = self.completion_fixture(root)
                (directory / "admission.json").write_text(json.dumps(admission))
                (directory / "complete-need.json").write_bytes(b'{"retained":"original-confirmation"}\n')
                before = {path.name: path.read_bytes() for path in directory.iterdir()}
                with patch.object(workflow, "app_requirements", return_value=app), patch.object(workflow, "confirm_settings"), patch.object(workflow, "bridge") as bridge, patch.object(workflow, "write_json", wraps=workflow.write_json) as write:
                    for _ in range(2):
                        with self.assertRaisesRegex(RuntimeError, "admitted.*explicit recovery"):
                            workflow.complete(root, root, "calculator")
                    bridge.assert_not_called()
                    write.assert_not_called()
                self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
                self.assertFalse((directory / "task-history").exists())

    def test_unadmitted_previous_tasks_are_preserved_before_each_fresh_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, previous, app = self.completion_fixture(root)
            originals = []
            for ordinal in (1, 2):
                original_bytes = (directory / "task.json").read_bytes()
                originals.append(previous)
                snapshot = directory / "task-history" / f"previous-{ordinal}.json"
                response = self.completion_response(ordinal)

                def confirm(product_root, command, payload):
                    self.assertEqual(product_root, root)
                    self.assertEqual(command, "complete_need")
                    self.assertEqual(payload["need_id"], "need-test")
                    self.assertEqual(json.loads(snapshot.read_bytes()), previous)
                    self.assertEqual((directory / "task.json").read_bytes(), original_bytes)
                    self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
                    return response

                with patch.object(workflow, "app_requirements", return_value=app), patch.object(workflow, "confirm_settings"), patch.object(workflow.time, "time_ns", return_value=ordinal), patch.object(workflow, "bridge", side_effect=confirm) as bridge, patch("builtins.print"):
                    workflow.complete(root, root, "calculator")
                    bridge.assert_called_once()
                previous = response["cloud_development"]["task_preparation"]["schema_preview"]
                self.assertEqual(json.loads((directory / "task.json").read_bytes()), previous)
                self.assertEqual(json.loads((directory / "registry.json").read_bytes()), response["registry"])
                self.assertEqual((directory / "task.json").stat().st_mode & 0o777, 0o600)
            history = sorted((directory / "task-history").glob("*.json"))
            self.assertEqual([path.name for path in history], ["previous-1.json", "previous-2.json"])
            self.assertEqual([json.loads(path.read_bytes()) for path in history], originals)
            self.assertFalse((directory / "admission.json").exists())

    def test_snapshot_failure_stops_before_fresh_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, _, app = self.completion_fixture(root)
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            with patch.object(workflow, "app_requirements", return_value=app), patch.object(workflow, "confirm_settings"), patch.object(workflow.time, "time_ns", return_value=7), patch.object(workflow, "write_json", side_effect=PermissionError("snapshot unavailable")) as write, patch.object(workflow, "bridge") as bridge:
                with self.assertRaisesRegex(PermissionError, "snapshot unavailable"):
                    workflow.complete(root, root, "calculator")
                self.assertEqual(write.call_args.args[0], directory / "task-history/previous-7.json")
                write.assert_called_once()
                bridge.assert_not_called()
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_failed_confirmation_retains_original_task_and_private_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, previous, app = self.completion_fixture(root)
            original_bytes = (directory / "task.json").read_bytes()
            registry_bytes = (directory / "registry.json").read_bytes()
            with patch.object(workflow, "app_requirements", return_value=app), patch.object(workflow, "confirm_settings"), patch.object(workflow.time, "time_ns", return_value=9), patch.object(workflow, "bridge", side_effect=RuntimeError("synthetic bridge failure")) as bridge:
                with self.assertRaisesRegex(RuntimeError, "synthetic bridge failure"):
                    workflow.complete(root, root, "calculator")
                bridge.assert_called_once()
            snapshot = directory / "task-history/previous-9.json"
            self.assertEqual(json.loads(snapshot.read_bytes()), previous)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "task.json").read_bytes(), original_bytes)
            self.assertEqual((directory / "registry.json").read_bytes(), registry_bytes)
            self.assertFalse((directory / "complete-need.json").exists())

    def test_rejected_fresh_task_never_replaces_previous_task_or_consent(self):
        for mutation in ("provider", "model", "registry", "submission"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory, previous, app = self.completion_fixture(root)
                original_bytes = (directory / "task.json").read_bytes()
                registry_bytes = (directory / "registry.json").read_bytes()
                response = self.completion_response(1)
                cloud = response["cloud_development"]
                if mutation in {"provider", "model"}:
                    cloud["task_preparation"]["schema_preview"][mutation] = "unapproved"
                elif mutation == "registry":
                    cloud["required_conditions"]["authoritative_registry_no_match"] = False
                else:
                    cloud["task_preparation"]["submission_available"] = False
                with patch.object(workflow, "app_requirements", return_value=app), patch.object(workflow, "confirm_settings"), patch.object(workflow.time, "time_ns", return_value=11), patch.object(workflow, "bridge", return_value=response) as bridge:
                    with self.assertRaisesRegex(RuntimeError, "task unavailable|did not authorize"):
                        workflow.complete(root, root, "calculator")
                    bridge.assert_called_once()
                self.assertEqual(json.loads((directory / "task-history/previous-11.json").read_bytes()), previous)
                self.assertEqual((directory / "task.json").read_bytes(), original_bytes)
                self.assertEqual((directory / "registry.json").read_bytes(), registry_bytes)
                self.assertEqual(json.loads((directory / "complete-need.json").read_bytes()), response)
                self.assertFalse((directory / "admission.json").exists())


if __name__ == "__main__":
    unittest.main()
