"""Immutable generic support is not a substitute for generated app behavior."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cloud_agent as worker
from test_cloud_agent import (
    FIXED_CLOCK, legacy_v2_task_fixture, rebind, rebind_provider_identity,
    synthetic_provider_identity, task_fixture,
)


class UiSupportTests(unittest.TestCase):
    def test_current_ui_support_providers_receive_semantic_action_uniqueness_lesson(self):
        for provider in ("openai-codex", "opencode"):
            with self.subTest(provider=provider):
                instructions = worker.provider_instructions(provider, self.task(provider))
                self.assertIn("node IDs AND button action IDs must each be unique", instructions)
                self.assertIn("toolbar and empty-state", instructions)
                self.assertIn("Successful compilation alone does not validate the first screen", instructions)
                self.assertEqual(worker.provider_instructions_digest(provider, self.task(provider)),
                                 worker.sha256_bytes(instructions.encode()))

    def task(self, provider="opencode"):
        task = task_fixture()
        task["provider"] = provider
        task["provider_execution_identity"] = synthetic_provider_identity(provider)
        if provider == "openai-codex":
            task["provider_execution_identity"]["runtime"]["package_id"] = "openai-codex-cli-docker"
            rebind_provider_identity(task)
        # A fresh preview chooses today's instructions before minting consent;
        # an explicit original Codex digest denotes an archived no-support task.
        task["consent"].pop("instructions_digest_sha256", None)
        task["consent"]["provider"] = provider
        task["consent"]["instructions_digest_sha256"] = worker.provider_instructions_digest(provider, task)
        return rebind(task)

    def test_only_opencode_or_v3_docker_codex_ui_gets_starter(self):
        for provider in ("opencode", "openai-codex"):
            task = self.task(provider)
            self.assertTrue(worker.uses_ui_support(task))
            for world in ("service-only-reference", "hybrid-reference"):
                task["target"]["wit_world"] = world
                self.assertFalse(worker.uses_ui_support(task))
                if provider == "openai-codex":
                    self.assertEqual(worker.provider_instructions_digest(provider, task), worker.provider_instructions_digest(provider))
        for task in (task_fixture(), legacy_v2_task_fixture()):
            self.assertFalse(worker.uses_ui_support(task))
            self.assertEqual(worker.provider_instructions_digest(task["provider"], task), worker.INSTRUCTIONS_DIGEST)
            with tempfile.TemporaryDirectory() as temporary:
                worker.prepare_workspace(Path(temporary), task)
                self.assertFalse((Path(temporary) / "source/src/vibapp_support.rs").exists())
        for provider in ("anthropic-claude-code", "google-gemini-cli"):
            task = self.task(provider)
            self.assertFalse(worker.uses_ui_support(task))
            self.assertEqual(worker.provider_instructions_digest(provider, task), worker.provider_instructions_digest(provider))

    def test_generic_support_does_not_satisfy_app_action_wiring(self):
        for provider in ("opencode", "openai-codex"):
            task = self.task(provider)
            task["need_spec"]["requirements"][0]["text"] = "User clicks a button to change the displayed result"
            sources = {"src/lib.rs": "fn handle_event() {}", "src/vibapp_support.rs": worker.ui_support_inputs(provider)["vibapp_support.rs"].decode()}
            with self.assertRaises(worker.WorkerError):
                worker.validate_generated_semantics(task, sources)
            sources["src/app.rs"] = "use crate::vibapp_support as s; fn render() { s::button(); s::action_id(); }"
            worker.validate_generated_semantics(task, sources)
            sources["src/app.rs"] = '// s::button(); s::action_id();\nfn render() {}'
            with self.assertRaises(worker.WorkerError):
                worker.validate_generated_semantics(task, sources)

    def test_instructions_bind_exact_support_and_guide_bytes(self):
        for provider in ("opencode", "openai-codex"):
            task = self.task(provider)
            inputs = worker.ui_support_inputs(provider)
            original = worker.provider_instructions_digest(provider, task)
            text = worker.provider_instructions(provider, task)
            self.assertIn(worker.sha256_bytes(inputs["vibapp_support.rs"]), text)
            self.assertIn(worker.sha256_bytes(inputs["README.md"]), text)
            for key in inputs:
                changed = {**inputs, key: inputs[key] + b" changed"}
                with self.subTest(provider=provider, input=key), mock.patch.object(worker, "ui_support_inputs", return_value=changed):
                    self.assertNotEqual(original, worker.provider_instructions_digest(provider, task))
                    self.assertNotEqual(task["immutable_task_digest_sha256"], worker.immutable_task_digest(task))
            self.assertNotEqual(worker.provider_instructions_digest("openai-codex"), worker.INSTRUCTIONS_DIGEST)

    def test_docker_codex_prompt_request_consent_and_claim_bind_same_inputs(self):
        task = self.task("openai-codex")
        worker.validate_task_schema(task)
        worker.validate_authorization(task, FIXED_CLOCK)
        instructions = worker.provider_instructions(task["provider"], task)
        digest = worker.sha256_bytes(instructions.encode())
        self.assertEqual(digest, task["consent"]["instructions_digest_sha256"])
        self.assertEqual(digest, worker.provider_request(task)["instructions_digest_sha256"])
        prompt = worker.create_prompt(task, worker.CONTRACT_DIGEST_PIN).decode()
        self.assertTrue(prompt.startswith(instructions))
        self.assertIn("small apply_patch edits", prompt)
        self.assertIn("Do not write another allocator", prompt)
        self.assertNotIn("Implement the allocator over wasm linear memory", prompt)
        self.assertIn("Do not create provider-last-message.json", instructions)
        self.assertIn("derives that control record from the actual exported files", instructions)
        self.assertIn("trusted host model-request budget notice", instructions)
        with tempfile.TemporaryDirectory() as temporary:
            claim = worker.claim_consent(Path(temporary), task)
            self.assertEqual(json.loads(claim.read_bytes())["instructions_digest_sha256"], digest)

    def test_docker_completion_guidance_does_not_replace_native_or_opencode_policy(self):
        task = self.task("openai-codex")
        instructions = worker.provider_instructions(task["provider"], task)
        self.assertNotIn("write provider-last-message.json at the workspace root", instructions)
        self.assertNotIn("Then return that structured result", instructions)
        self.assertIn("Physically create source/src/lib.rs", instructions)
        self.assertIn("never resets between repair rounds", instructions)
        self.assertIn("does not extend the task deadline", instructions)
        self.assertIn("write the exact", worker.provider_instructions("openai-codex"))
        opencode = worker.provider_instructions("opencode", self.task("opencode"))
        self.assertNotIn("trusted host model-request budget notice", opencode)

    def test_historical_docker_codex_digest_stays_readable_without_support(self):
        task = self.task("openai-codex")
        task["consent"]["instructions_digest_sha256"] = worker.INSTRUCTIONS_DIGEST
        # Reconstruct a historical digest independently using the frozen original
        # policy, not the current support-aware provider_request implementation.
        with mock.patch.object(worker, "provider_instructions_digest", return_value=worker.INSTRUCTIONS_DIGEST):
            historical_request = worker.provider_request(task)
        historical_digest = worker.sha256_bytes(worker.canonical_json(historical_request))
        task["immutable_task_digest_sha256"] = historical_digest
        task["consent"]["payload_digest_sha256"] = historical_digest
        self.assertFalse(worker.uses_ui_support(task))
        worker.validate_task_schema(task)
        worker.validate_authorization(task, FIXED_CLOCK)
        self.assertEqual(worker.immutable_task_digest(task), historical_digest)
        self.assertTrue(worker.create_prompt(task, worker.CONTRACT_DIGEST_PIN).decode().startswith(worker.PROVIDER_INSTRUCTIONS))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker.prepare_workspace(root, task)
            self.assertFalse((root / "source/src/vibapp_support.rs").exists())
            self.assertFalse((root / "contracts/rust-support.md").exists())
        task["consent"]["instructions_digest_sha256"] = "f" * 64
        self.assertTrue(worker.uses_ui_support(task))
        with self.assertRaises(worker.WorkerError):
            worker.validate_authorization(task, FIXED_CLOCK)

    def test_changed_trusted_inputs_fail_before_prompt_preparation_and_handoff(self):
        for provider in ("opencode", "openai-codex"):
            task = self.task(provider)
            inputs = worker.ui_support_inputs(provider)
            for key in inputs:
                with self.subTest(provider=provider, input=key), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    worker.prepare_workspace(root, task)
                    changed = {**inputs, key: inputs[key] + b" changed"}
                    with mock.patch.object(worker, "ui_support_inputs", return_value=changed):
                        with self.assertRaisesRegex(worker.WorkerError, "consent"):
                            worker.validate_authorization(task, FIXED_CLOCK)
                        with self.assertRaisesRegex(worker.WorkerError, "consent"):
                            worker.create_prompt(task, worker.CONTRACT_DIGEST_PIN)
                        with tempfile.TemporaryDirectory() as fresh, self.assertRaisesRegex(worker.WorkerError, "consent"):
                            worker.prepare_workspace(Path(fresh), task)
                        with self.assertRaisesRegex(worker.WorkerError, "consent"):
                            worker.audit_workspace(root, task, {}, worker.CONTRACT_DIGEST_PIN)

    def test_ui_inputs_survive_audit_and_cannot_be_modified_or_omitted(self):
        for provider, mutation in (
            (provider, mutation)
            for provider in ("opencode", "openai-codex")
            for mutation in (None, "bytes", "mode", "missing", "guide", "inventory")
        ):
            task = self.task(provider)
            inputs = worker.ui_support_inputs(provider)
            with self.subTest(provider=provider, mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                worker.prepare_workspace(root, task)
                support = root / "source/src/vibapp_support.rs"
                self.assertEqual(support.read_bytes(), inputs["vibapp_support.rs"])
                self.assertEqual(support.stat().st_mode & 0o777, 0o444)
                self.assertFalse((root / "source/src/lib.rs").exists())
                (root / "source/src/lib.rs").write_bytes((worker.DRY_RUN_SOURCE / "src/lib.rs").read_bytes())
                result = json.loads(worker.DRY_RUN_RESULT.read_bytes())
                result["source_files"].append("src/vibapp_support.rs")
                if mutation == "bytes":
                    support.chmod(0o644)
                    support.write_bytes(b"// replaced")
                    support.chmod(0o444)
                elif mutation == "mode":
                    support.chmod(0o644)
                elif mutation == "missing":
                    support.unlink()
                elif mutation == "guide":
                    guide = root / "contracts/rust-support.md"
                    guide.chmod(0o644)
                    guide.write_text("edited")
                    guide.chmod(0o444)
                elif mutation == "inventory":
                    result["source_files"].remove("src/vibapp_support.rs")
                if mutation is None:
                    records = worker.audit_workspace(root, task, result, worker.CONTRACT_DIGEST_PIN)
                    self.assertIn("src/vibapp_support.rs", [record["path"] for record in records])
                else:
                    with self.assertRaises(worker.WorkerError):
                        worker.audit_workspace(root, task, result, worker.CONTRACT_DIGEST_PIN)

    def test_support_input_rejects_linked_and_oversized_files(self):
        for mutation in ("link", "large"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                starter = root / "starter"
                starter.mkdir()
                for name in ("opencode_instructions.txt", "codex_instructions.txt", "vibapp_support.rs", "README.md"):
                    (starter / name).write_text("trusted test input")
                path = starter / "vibapp_support.rs"
                if mutation == "large":
                    path.write_bytes(b"x" * (64 * 1024 + 1))
                else:
                    path.unlink()
                    path.symlink_to(starter / "README.md")
                for provider in ("opencode", "openai-codex"):
                    with mock.patch.object(worker, "BASE", root), self.assertRaises(worker.WorkerError):
                        worker.ui_support_inputs(provider)


if __name__ == "__main__":
    unittest.main()
