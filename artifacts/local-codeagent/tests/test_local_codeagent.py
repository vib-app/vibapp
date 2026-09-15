from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "local_codeagent.py"
SPEC = importlib.util.spec_from_file_location("vibapp_local_codeagent", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def good_envelope() -> dict:
    need = {
        "schema_version": "vibapp.need-spec.product-v0.0.1",
        "document_type": "need-spec",
        "need_id": "need.synthetic.paper-colors",
        "owner": {"principal_id": "desktop.local.user", "principal_kind": "user"},
        "goal": "生成一个离线折纸配色提示界面，只显示标题和一条安全提示。",
        "requirements": [{
            "requirement_id": "requirement.primary",
            "text": "显示离线折纸配色提示。",
            "priority": "must-have",
            "acceptance_examples": ["启动后可看到标题和配色提示。"],
        }],
        "negative_constraints": [{
            "constraint_id": "constraint.no-network",
            "kind": "forbidden-capability",
            "source": "user",
            "value": "vibapp:experimental-v0/http@0.0.1",
        }],
        "platforms": [{"os": "macos", "arch": "aarch64", "profile": "desktop"}],
        "profiles": ["desktop"],
        "permission_ceiling": {
            "allowed_interfaces": MODULE.REQUIRED_IMPORTS,
            "forbidden_interfaces": ["vibapp:experimental-v0/http@0.0.1"],
            "maximum_scope_digests": [],
        },
        "privacy_requirement": "remote-private",
        "created_at_utc": "2026-08-24T06:00:00Z",
        "revision": 1,
    }
    package = {
        "app_id": "ai.vibapp.private.papercolors",
        "display_name": "折纸配色助手",
        "description": need["goal"],
        "version": "0.1.0",
        "entrypoints": [{"id": "main", "kind": "launcher-ui", "label": "折纸配色助手", "initial_route": "home"}],
    }
    target = {
        "app_kind": "ui",
        "contract": "vibapp:experimental-v0@0.0.1",
        "profiles": ["desktop"],
        "required_imports": MODULE.REQUIRED_IMPORTS,
        "required_capabilities": [
            "vibapp:experimental-v0/kv@0.0.1",
            "vibapp:experimental-v0/settings@0.0.1",
        ],
        "rust_target": "wasm32-wasip2",
        "wasi": "0.2",
        "wit_world": "ui-only-reference",
    }
    immutable = MODULE.digest_json({"need": need, "package": package, "target": target})
    job_id = f"job-local-preview-{immutable[:20]}"
    task = {
        "schema_version": "vibapp.cloud-codeagent-task.experimental-v2",
        "document_type": "cloud-codeagent-task",
        "job_id": job_id,
        "need_spec_complete": True,
        "need_spec_current_revision": 1,
        "need_spec_digest_sha256": MODULE.digest_json(need),
        "immutable_task_digest_sha256": immutable,
        "need_spec": need,
        "package_intent": package,
        "remote_processing_consent": True,
        "consent": {"decision": "granted", "single_use": True, "job_id": job_id, "payload_digest_sha256": immutable},
        "provider": "openai-codex",
        "target": target,
        "limits": {},
    }
    registry = {
        "request_id": "registry.synthetic.001",
        "route": "refinement",
        "recommendations": [],
        "refinement": {"reason_code": "no-acceptable-semantic-match"},
        "codeagent_handoff": {"created": False, "permitted": False, "reason": "Registry cannot launch CodeAgent."},
    }
    return {
        "task": task,
        "registry": registry,
        "explicit_user_submit": True,
        "diagnostic_compiler_fixture": True,
        "diagnostic_lan_preprocessing_consent": True,
    }


class ContractTests(unittest.TestCase):
    def test_positive_envelope_and_source(self) -> None:
        task, registry, fallback = MODULE.validate_envelope(good_envelope())
        self.assertEqual(registry["route"], "refinement")
        self.assertFalse(fallback)
        source = MODULE.render_source(task, {"display_name": "折纸配色助手", "headline": "今日配色", "body": "离线查看柔和配色建议。"})
        self.assertIn("const APP_ID: &str = \"ai.vibapp.private.papercolors\";".encode(), source)
        self.assertNotIn(b"__APP_ID__", source)

    def test_explicit_diagnostic_mode_and_lan_preprocessing_consent_are_required(self) -> None:
        for key in (
            "explicit_user_submit",
            "diagnostic_compiler_fixture",
            "diagnostic_lan_preprocessing_consent",
        ):
            envelope = good_envelope()
            envelope[key] = False
            with self.assertRaises(MODULE.ChainFailure):
                MODULE.validate_envelope(envelope)

    def test_historical_v1_task_without_required_capabilities_fails_closed(self) -> None:
        envelope = good_envelope()
        envelope["task"]["schema_version"] = "vibapp.cloud-codeagent-task.experimental-v1"
        del envelope["task"]["target"]["required_capabilities"]
        with self.assertRaisesRegex(MODULE.ChainFailure, "unsupported task schema"):
            MODULE.validate_envelope(envelope)

    def test_registry_recommendation_cannot_be_bypassed(self) -> None:
        envelope = good_envelope()
        envelope["registry"]["route"] = "recommendation"
        envelope["registry"]["recommendations"] = [{"app": {"id": "existing"}}]
        with self.assertRaises(MODULE.ChainFailure):
            MODULE.validate_envelope(envelope)

    def test_need_digest_and_consent_binding_mismatches_fail_closed(self) -> None:
        envelope = good_envelope()
        envelope["task"]["need_spec"]["goal"] = "tampered"
        with self.assertRaises(MODULE.ChainFailure):
            MODULE.validate_envelope(envelope)
        envelope = good_envelope()
        envelope["task"]["consent"]["payload_digest_sha256"] = "0" * 64
        with self.assertRaises(MODULE.ChainFailure):
            MODULE.validate_envelope(envelope)

    def test_world_or_permission_ceiling_cannot_expand(self) -> None:
        envelope = good_envelope()
        envelope["task"]["target"]["required_imports"].append("vibapp:experimental-v0/http@0.0.1")
        with self.assertRaises(MODULE.ChainFailure):
            MODULE.validate_envelope(envelope)

    def test_model_spec_rejects_extra_fields_and_oversize(self) -> None:
        with self.assertRaises(MODULE.ChainFailure):
            MODULE.parse_model_spec(
                '{"display_name":"A","headline":"B","body":"C","rust_source":"fn main() {}"}'
            )
        with self.assertRaises(MODULE.ChainFailure):
            MODULE.parse_model_spec('{"display_name":"A","headline":"B","body":"' + "x" * 241 + '"}')

    def test_qwen_fixture_output_cannot_become_a_product_candidate(self) -> None:
        task, _registry, _fallback = MODULE.validate_envelope(good_envelope())
        with tempfile.TemporaryDirectory() as directory:
            record = MODULE.persist_diagnostic_artifact(
                Path(directory),
                task,
                b"diagnostic-component-bytes",
                "1" * 64,
                {"component_sha256": MODULE.digest_bytes(b"diagnostic-component-bytes")},
                "receipt-diagnostic-only",
            )
            self.assertEqual(record["publication_state"], "diagnostic-only")
            self.assertEqual(record["verification_state"], "diagnostic-only")
            self.assertFalse(record["launch_eligible"])
            self.assertFalse(record["install_eligible"])
            self.assertEqual(MODULE.candidate_state(Path(directory))["apps"], [])

    def test_model_failure_is_durable_and_quiescent_without_candidate(self) -> None:
        calls = 0

        def failing_model(_prompt: str):
            nonlocal calls
            calls += 1
            raise MODULE.ChainFailure("synthetic model failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            candidates = Path(directory) / "candidates"
            with self.assertRaises(MODULE.ChainFailure):
                MODULE.process_envelope(good_envelope(), root, candidates, failing_model)
            failures = list((root / "queue" / "failed").glob("*.json"))
            self.assertEqual(len(failures), 1)
            self.assertFalse(list((root / "queue" / "processing").glob("*.json")))
            self.assertFalse(list(candidates.glob("*/candidate.json")))
            with self.assertRaises(MODULE.ChainFailure):
                MODULE.process_envelope(good_envelope(), root, candidates, failing_model)
            self.assertEqual(calls, 1, "known failed replay must not consume another model request")


if __name__ == "__main__":
    unittest.main()
