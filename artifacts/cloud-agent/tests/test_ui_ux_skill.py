"""Real policy/workspace boundaries; no provider, compiler, or user data."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cloud_agent as worker
from test_cloud_agent import (FIXED_CLOCK, rebind, rebind_provider_identity,
                             reminder_task_fixture, service_task_fixture,
                             synthetic_provider_identity, task_fixture)


def fresh_task(provider="openai-codex", world="ui-only-reference"):
    task = {"ui-only-reference": task_fixture, "service-only-reference": service_task_fixture,
            "hybrid-reference": reminder_task_fixture}[world]()
    task["provider"] = task["consent"]["provider"] = provider
    task["provider_execution_identity"] = synthetic_provider_identity(provider)
    if provider == "openai-codex":
        task["provider_execution_identity"]["runtime"]["package_id"] = "openai-codex-cli-docker"
        rebind_provider_identity(task)
    task["need_spec"]["permission_ceiling"]["allowed_interfaces"] = task["target"]["required_capabilities"]
    task["consent"].pop("instructions_digest_sha256", None)
    task["consent"]["instructions_digest_sha256"] = worker.provider_instructions_digest(provider, task)
    return rebind(task)


def completed_ui_workspace(root, task):
    worker.prepare_workspace(root, task)
    (root / "source/src").mkdir(exist_ok=True)
    (root / "source/src/lib.rs").write_bytes((worker.DRY_RUN_SOURCE / "src/lib.rs").read_bytes())
    result = json.loads(worker.DRY_RUN_RESULT.read_bytes())
    if worker.uses_ui_support(task):
        result["source_files"].append("src/vibapp_support.rs")
    return result


class UiUxSkillTests(unittest.TestCase):
    def test_existing_ui_ux_only_consent_keeps_its_exact_policy(self):
        task = fresh_task()
        instructions = worker.provider_instructions(task["provider"], task)
        prior = instructions.split("\nDefault Apple-inspired application design guidance:\n")[0]
        task["consent"]["instructions_digest_sha256"] = worker.sha256_bytes(prior.encode())
        rebind(task)
        with mock.patch.object(worker, "apple_design_inputs", side_effect=AssertionError("old task loads new policy")):
            self.assertEqual(worker.provider_instructions(task["provider"], task), prior)
            worker.validate_authorization(task, FIXED_CLOCK)
            with tempfile.TemporaryDirectory() as temporary:
                worker.prepare_workspace(Path(temporary), task)
                self.assertFalse((Path(temporary) / "contracts/apple-design-SKILL.md").exists())

    def test_changed_apple_guidance_requires_new_consent(self):
        task = fresh_task()
        changed = worker.apple_design_inputs()
        changed["contracts/apple-design-SKILL.md"] += b"changed"
        with mock.patch.object(worker, "apple_design_inputs", return_value=changed):
            with self.assertRaisesRegex(worker.WorkerError, "consent"):
                worker.validate_authorization(task, FIXED_CLOCK)

    def test_all_providers_and_worlds_receive_exact_consent_bound_readonly_skill(self):
        for provider in worker.SUPPORTED_TASK_PROVIDERS:
            for world in ("ui-only-reference", "hybrid-reference", "service-only-reference"):
                with self.subTest(provider=provider, world=world), tempfile.TemporaryDirectory() as temporary:
                    task = fresh_task(provider, world)
                    worker.validate_task_schema(task)
                    worker.validate_authorization(task, FIXED_CLOCK)
                    root = Path(temporary)
                    worker.prepare_workspace(root, task)
                    skill = root / "contracts/vibapp-ui-ux.md"
                    self.assertEqual(skill.read_bytes(), worker.ui_ux_skill_bytes())
                    self.assertEqual(skill.stat().st_mode & 0o777, 0o444)
                    instructions = worker.provider_instructions(provider, task)
                    digest = worker.sha256_bytes(instructions.encode())
                    self.assertIn(worker.sha256_bytes(skill.read_bytes()), instructions)
                    self.assertIn("read contracts/vibapp-ui-ux.md completely", instructions)
                    self.assertEqual(digest, task["consent"]["instructions_digest_sha256"])
                    self.assertEqual(digest, worker.provider_request(task)["instructions_digest_sha256"])
                    self.assertTrue(worker.create_prompt(task, worker.CONTRACT_DIGEST_PIN).startswith(instructions.encode()))
                    for name, payload in worker.apple_design_inputs().items():
                        self.assertEqual((root / name).read_bytes(), payload)
                        self.assertEqual((root / name).stat().st_mode & 0o777, 0o444)
                        self.assertIn(worker.sha256_bytes(payload), instructions)
                    self.assertEqual(task["immutable_task_digest_sha256"], worker.immutable_task_digest(task))
                    if world != "ui-only-reference":
                        self.assertFalse((root / "source/src/vibapp_support.rs").exists())
                        self.assertFalse((root / "contracts/rust-support.md").exists())
                        self.assertNotIn("one consent-bound UI-only VibApp task", instructions)

    def test_provider_only_preview_defaults_to_skill_without_consent(self):
        for provider in worker.SUPPORTED_TASK_PROVIDERS:
            instructions = worker.provider_instructions(provider)
            self.assertIn(worker.sha256_bytes(worker.ui_ux_skill_bytes()), instructions)
            self.assertNotEqual(worker.sha256_bytes(worker.legacy_provider_instructions(provider).encode()),
                                worker.provider_instructions_digest(provider))

    def test_exact_old_policies_read_without_loading_new_skill_or_rebinding(self):
        self.assertEqual(worker.INSTRUCTIONS_DIGEST, "49fe02f64ab181e3c9a1ce22729bd3ec1309a5afa7b8a4b39e14391d95650536")
        self.assertEqual(worker.OTHER_PROVIDER_INSTRUCTIONS_DIGEST, "406373f344973f6eef09a2973bda945594fdb7a14cb5df5ac51fe5b78d216d2a")
        for provider in worker.SUPPORTED_TASK_PROVIDERS:
            for world in ("ui-only-reference", "hybrid-reference", "service-only-reference"):
                task = fresh_task(provider, world)
                legacy = worker.legacy_provider_instructions(provider, task)
                task["consent"]["instructions_digest_sha256"] = worker.sha256_bytes(legacy.encode())
                rebind(task)
                before = copy.deepcopy(task)
                with self.subTest(provider=provider, world=world), mock.patch.object(worker, "ui_ux_skill_bytes", side_effect=AssertionError("History must not read new policy")), tempfile.TemporaryDirectory() as temporary:
                    self.assertEqual(worker.provider_instructions(provider, task), legacy)
                    self.assertEqual(worker.immutable_task_digest(task), before["immutable_task_digest_sha256"])
                    worker.validate_authorization(task, FIXED_CLOCK)
                    worker.prepare_workspace(Path(temporary), task)
                    self.assertFalse((Path(temporary) / "contracts/vibapp-ui-ux.md").exists())
                    worker.claim_consent(Path(temporary), task)
                    with self.assertRaisesRegex(worker.WorkerError, "already consumed"):
                        worker.claim_consent(Path(temporary), task)
                    self.assertEqual(task, before)

    def test_skill_authority_drift_fails_before_provider_and_handoff(self):
        task = fresh_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = completed_ui_workspace(root, task)
            with mock.patch.object(worker, "ui_ux_skill_bytes", return_value=worker.ui_ux_skill_bytes() + b"changed"):
                self.assertNotEqual(worker.immutable_task_digest(task), task["immutable_task_digest_sha256"])
                for operation in (lambda: worker.validate_authorization(task, FIXED_CLOCK),
                                  lambda: worker.create_prompt(task, worker.CONTRACT_DIGEST_PIN),
                                  lambda: worker.audit_workspace(root, task, result, worker.CONTRACT_DIGEST_PIN)):
                    with self.assertRaisesRegex(worker.WorkerError, "consent"):
                        operation()
                fresh = root / "unprepared"
                fresh.mkdir()
                with self.assertRaisesRegex(worker.WorkerError, "consent"):
                    worker.prepare_workspace(fresh, task)
                self.assertEqual(list(fresh.iterdir()), [])

    def test_exact_document_survives_audit_and_mutations_fail_closed(self):
        for provider in worker.SUPPORTED_TASK_PROVIDERS:
            for mutation in (None, "bytes", "mode", "missing", "symlink", "hardlink", "extra"):
                with self.subTest(provider=provider, mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = fresh_task(provider)
                    result = completed_ui_workspace(root, task)
                    path = root / "contracts/vibapp-ui-ux.md"
                    if mutation == "bytes":
                        path.chmod(0o644)
                        path.write_bytes(path.read_bytes().replace(b"VibApp", b"VibBad"))
                        path.chmod(0o444)
                    elif mutation == "mode":
                        path.chmod(0o644)
                    elif mutation == "missing":
                        path.unlink()
                    elif mutation == "symlink":
                        path.unlink()
                        path.symlink_to(worker.BASE / "skills/vibapp-ui-ux/SKILL.md")
                    elif mutation == "hardlink":
                        os.link(path, root / "contracts/second.md")
                    elif mutation == "extra":
                        (root / "contracts/injected.md").write_text("unexpected")
                    if mutation is None:
                        records = worker.audit_workspace(root, task, result, worker.CONTRACT_DIGEST_PIN)
                        self.assertNotIn("contracts/vibapp-ui-ux.md", [record["path"] for record in records])
                    else:
                        with self.assertRaises(worker.WorkerError):
                            worker.audit_workspace(root, task, result, worker.CONTRACT_DIGEST_PIN)

    def test_unknown_digest_never_selects_legacy_or_grants_authority(self):
        task = fresh_task()
        task["consent"]["instructions_digest_sha256"] = "f" * 64
        rebind(task)
        with self.assertRaisesRegex(worker.WorkerError, "consent"):
            worker.validate_authorization(task, FIXED_CLOCK)

    def test_skill_source_refuses_missing_linked_large_writable_or_non_utf8(self):
        for mutation in ("missing", "symlink", "parent-link", "hardlink", "large", "writable", "non-utf8"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / "skills/vibapp-ui-ux"
                directory.mkdir(parents=True)
                path = directory / "SKILL.md"
                path.write_bytes(b"trusted passive skill")
                if mutation == "missing":
                    path.unlink()
                elif mutation == "symlink":
                    path.unlink()
                    path.symlink_to(worker.BASE / "skills/vibapp-ui-ux/SKILL.md")
                elif mutation == "parent-link":
                    directory.rename(root / "real")
                    directory.symlink_to(root / "real")
                elif mutation == "hardlink":
                    os.link(path, directory / "extra")
                elif mutation == "large":
                    path.write_bytes(b"x" * (64 * 1024 + 1))
                elif mutation == "writable":
                    path.chmod(0o666)
                else:
                    path.write_bytes(b"\xff")
                with mock.patch.object(worker, "BASE", root), self.assertRaises(worker.WorkerError):
                    worker.ui_ux_skill_bytes()


if __name__ == "__main__":
    unittest.main()
