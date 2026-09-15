"""Synthetic orchestration tests; no model calls, containers or app acceptance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import opencode_apps_workflow as workflow


class ProviderSelectionTests(unittest.TestCase):
    def test_prepare_keeps_qwen_reception_and_explicit_codex_author_separate(self):
        calls = []
        def bridge(root, command, args):
            calls.append((command, args))
            if command == "health":
                return {"build_input_receipt": {"sha256": "synthetic-current"}}
            if command == "submit_need":
                return {"need": {"need_id": "synthetic-need"}, "need_spec": {
                    "analysis": {"status": "analyzed", "model": workflow.MODEL}}}
            return {}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(workflow, "bridge", side_effect=bridge), mock.patch.object(workflow, "current_desktop_build_inputs_sha256", return_value="synthetic-current"):
            workflow.prepare(Path(directory), "converter", provider="codex", code_model="selected-code-model")
        settings = dict(calls)
        self.assertEqual(settings["save_codeagent_settings"], {
            "selectedProvider": "codex", "modelByProvider": {"codex": "selected-code-model"}})
        self.assertEqual(settings["save_model_settings"]["generation"]["model"], workflow.MODEL)

    def test_completion_rejects_silent_provider_or_model_substitution(self):
        for task in ({"provider": "opencode", "model": "selected-code-model"},
                     {"provider": "openai-codex", "model": "wrong-model"}):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workflow.write_json(root / "converter/submit-need.json", {"need": {"need_id": "synthetic"}})
                completed = {"cloud_development": {"task_preparation": {"schema_preview": task}}}
                with mock.patch.object(workflow, "bridge", return_value=completed):
                    with self.assertRaisesRegex(RuntimeError, "Fresh selected CodeAgent"):
                        workflow.complete(root, "converter", provider="codex", code_model="selected-code-model")
                self.assertFalse((root / "converter/task.json").exists())

    def test_completion_preserves_legacy_opencode_default(self):
        task = {"provider": "opencode", "model": workflow.MODEL}
        completed = {"cloud_development": {
            "task_preparation": {"schema_preview": {**task, "job_id": "synthetic"}, "submission_available": True},
            "required_conditions": {"authoritative_registry_no_match": True}}, "registry": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow.write_json(root / "converter/submit-need.json", {"need": {"need_id": "synthetic"}})
            with mock.patch.object(workflow, "bridge", return_value=completed):
                workflow.complete(root, "converter")
            saved = json.loads((root / "converter/task.json").read_text())
            self.assertEqual(saved["provider"], "opencode")
            self.assertEqual(saved["model"], workflow.MODEL)


if __name__ == "__main__":
    unittest.main()
