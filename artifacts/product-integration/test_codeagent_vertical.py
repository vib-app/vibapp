from __future__ import annotations

from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


BASE = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


vertical = load_module("vibapp_codeagent_vertical_test", BASE / "codeagent_vertical.py")


class CodeAgentVerticalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.delivery = vertical.load_module("vibapp_vertical_delivery_test", vertical.CONTROLLER)
        cls.stage = cls.delivery.CodeAgentStage(
            adapter_path=vertical.ADAPTER,
            cloud_agent_path=vertical.CLOUD_AGENT,
            provider_id="codex",
            model="gpt-5.6-sol",
            acknowledge_external_cost=False,
        )

    def test_no_request_authority_oracle_reaches_no_provider_or_consent(self):
        result = vertical.authority_oracle(self.stage.adapter, self.stage.cloud, "gpt-5.6-sol")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["consent_consumed"])
        self.assertEqual(result["durable_mutations"], 0)
        self.assertEqual(
            [item["case"] for item in result["cases"]],
            ["sentinel", "job", "consent", "task-provider", "model", "external-cost", "explicit-submit"],
        )
        self.assertEqual(len(result["adapter_guards"]), 3)

    def test_web_gui_is_an_exact_derivation_of_desktop_gui(self):
        result = vertical.shared_gui_preflight()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["index_adapter_only"])
        self.assertTrue(all(row["exact_match"] for row in result["exact_shared_files"].values()))

    def test_live_binding_requires_every_exact_confirmation(self):
        task = {
            "job_id": "job-exact",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "consent": {"consent_id": "consent-exact"},
        }
        valid = Namespace(
            execute_authorized_live_task=vertical.LIVE_SENTINEL,
            confirm_job="job-exact",
            confirm_consent="consent-exact",
            confirm_task_provider="openai-codex",
            confirm_model="gpt-5.6-sol",
            acknowledge_external_cost=True,
            explicit_user_submit=True,
        )
        vertical.require_live_bindings(valid, task)
        for field in (
            "execute_authorized_live_task",
            "confirm_job",
            "confirm_consent",
            "confirm_task_provider",
            "confirm_model",
            "acknowledge_external_cost",
            "explicit_user_submit",
        ):
            broken = Namespace(**vars(valid))
            setattr(broken, field, False if isinstance(getattr(broken, field), bool) else "wrong")
            with self.subTest(field=field), self.assertRaises(vertical.VerticalError) as caught:
                vertical.require_live_bindings(broken, task)
            self.assertEqual(caught.exception.code, "live-authorization-required")

    def test_run_live_rejects_reused_terminal_attempt(self):
        for status in ("private-appstore-ready", "failed"):
            with self.subTest(status=status), self.assertRaises(vertical.VerticalError) as caught:
                vertical.require_fresh_live_attempt({"status": status}, {"private-appstore-ready", "failed"})
            self.assertEqual(caught.exception.code, "live-task-already-terminal")
        vertical.require_fresh_live_attempt({"status": "queued"}, {"private-appstore-ready", "failed"})

    def test_live_parser_accepts_an_explicit_failed_task_retry_binding(self):
        arguments = vertical.parser().parse_args(
            [
                "run-live",
                "--task", "/tmp/task.json",
                "--registry-no-match", "/tmp/registry.json",
                "--root", "/tmp/root",
                "--retry-task-id", "development-" + "a" * 32,
                "--provider", "codex",
                "--model", "gpt-5.6-sol",
                "--confirm-job", "job",
                "--confirm-consent", "consent",
                "--confirm-task-provider", "openai-codex",
                "--confirm-model", "gpt-5.6-sol",
                "--execute-authorized-live-task", vertical.LIVE_SENTINEL,
                "--acknowledge-external-cost",
                "--explicit-user-submit",
            ]
        )
        self.assertEqual(arguments.retry_task_id, "development-" + "a" * 32)

    def test_live_success_oracle_rejects_synthetic_provider_status(self):
        with tempfile.TemporaryDirectory(prefix="vibapp-live-oracle-") as directory:
            root = Path(directory)
            task_id = "development-" + "a" * 32
            attempt_id = "attempt-0001-" + "b" * 16
            attempt_root = root / "tasks" / task_id / "attempts" / attempt_id
            (attempt_root / "codeagent").mkdir(parents=True)
            status = {
                "status": "source-ready",
                "provider_process_started": False,
                "external_request_attempted": True,
                "builder_invoked": False,
            }
            (attempt_root / "codeagent/status.json").write_text(json.dumps(status), encoding="utf-8")
            attempt = {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": "private-appstore-ready",
                "stage": "private-appstore-ready",
                "progress_percent": 100,
                "outputs": {
                    "external_request_attempted": True,
                    "digest_equality_proven": True,
                    "publication_performed": False,
                },
            }
            class RevalidatedFixture:
                @staticmethod
                def revalidate_attempt(_task_id, _attempt_id):
                    return attempt

            with self.assertRaises(vertical.VerticalError) as caught:
                vertical.assert_live_complete(RevalidatedFixture(), root, attempt, {}, "codex")
            self.assertEqual(caught.exception.code, "live-codeagent-evidence-invalid")

    def test_verifier_preflight_rejects_unaccepted_executable(self):
        with self.assertRaises(vertical.VerticalError) as caught:
            vertical.verifier_preflight(
                Path("/usr/bin/true"),
                self.delivery.DEFAULT_WASM_TOOLS,
                self.delivery.WASM_TOOLS_SHA256,
            )
        self.assertEqual(caught.exception.code, "verifier-identity-mismatch")

    def test_verifier_preflight_accepts_exact_reviewed_tool(self):
        result = vertical.verifier_preflight(
            self.delivery.DEFAULT_WASM_TOOLS,
            self.delivery.DEFAULT_WASM_TOOLS,
            self.delivery.WASM_TOOLS_SHA256,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["sha256"], self.delivery.WASM_TOOLS_SHA256)

    def test_builder_preflight_executes_no_source(self):
        runner = self.delivery.MacSandboxCargoRunner(
            vertical.TOOL_LAYER,
            vertical.DEFAULT_CARGO_HOME,
            vertical.DEFAULT_CACHE_ACCEPTANCE,
        )
        result = runner.preflight()
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["source_executed"])
        self.assertEqual(result["network"], "none")


if __name__ == "__main__":
    unittest.main()
