from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


BASE = Path(__file__).resolve().parents[1]
REPOSITORY = BASE.parents[1]
CLOUD_AGENT = REPOSITORY / "artifacts/cloud-agent/cloud_agent.py"
CLOUD_FIXTURE = REPOSITORY / "artifacts/cloud-agent/fixtures/valid-task.json"

SPEC = importlib.util.spec_from_file_location("vibapp_local_orchestrator", BASE / "orchestrator.py")
assert SPEC is not None and SPEC.loader is not None
orchestrator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(orchestrator)

CLOUD_SPEC = importlib.util.spec_from_file_location(
    "vibapp_orchestrator_test_cloud_agent", CLOUD_AGENT
)
assert CLOUD_SPEC is not None and CLOUD_SPEC.loader is not None
cloud_agent = importlib.util.module_from_spec(CLOUD_SPEC)
sys.modules[CLOUD_SPEC.name] = cloud_agent
CLOUD_SPEC.loader.exec_module(cloud_agent)


def current_task() -> dict:
    task = json.loads(CLOUD_FIXTURE.read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    task["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    task["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    return task


def task_bytes(task: dict | None = None) -> bytes:
    return orchestrator.canonical_json(task or current_task())


def task_with_job(job_id: str) -> dict:
    task = current_task()
    task["job_id"] = job_id
    task["consent"]["job_id"] = job_id
    digest = cloud_agent.immutable_task_digest(
        task, task["consent"]["contract_digest_sha256"]
    )
    task["immutable_task_digest_sha256"] = digest
    task["consent"]["payload_digest_sha256"] = digest
    return task


def retry_attempt(task: dict, ordinal: int) -> dict:
    retried = json.loads(json.dumps(task))
    suffix = cloud_agent.sha256_bytes(
        f"{retried['job_id']}:{ordinal}".encode("utf-8")
    )[:16]
    attempt_id = f"attempt-{ordinal:04d}-{suffix}"
    retried["execution_attempt"] = {"attempt_id": attempt_id, "ordinal": ordinal}
    retried["consent"]["consent_id"] = f"consent-queue-retry-{ordinal}"
    retried["consent"]["attempt_id"] = attempt_id
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    retried["consent"]["issued_at_utc"] = (now - dt.timedelta(seconds=30)).isoformat().replace(
        "+00:00", "Z"
    )
    retried["consent"]["expires_at_utc"] = (now + dt.timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    return retried


class OrchestratorTests(unittest.TestCase):
    def test_current_cloud_fixture_is_bound_to_v7_policy_and_instructions(self) -> None:
        task = current_task()
        consent = task["consent"]
        self.assertEqual(consent["policy_version"], cloud_agent.POLICY_VERSION)
        self.assertEqual(
            consent["instructions_digest_sha256"],
            cloud_agent.provider_instructions_digest(task["provider"]),
        )
        self.assertEqual(
            task["immutable_task_digest_sha256"],
            cloud_agent.immutable_task_digest(task, consent["contract_digest_sha256"]),
        )

    def test_receipt_schema_is_strict_json(self) -> None:
        schema = BASE / "schemas/local-development-receipt.schema.json"
        parsed = json.loads(schema.read_text(encoding="utf-8"), object_pairs_hook=orchestrator.strict_object)
        self.assertEqual(parsed["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(parsed["additionalProperties"])

    def test_real_validate_and_dry_run_create_truthful_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
            root = Path(temporary) / "queue"
            submitted = orchestrator.submit(root, CLOUD_AGENT, task_bytes(), explicit_submit=True)
            self.assertFalse(submitted["duplicate"])
            queued = submitted["receipt"]
            self.assertEqual(queued["status"], "queued-for-codeagent")
            self.assertFalse(queued["external_request_attempted"])
            self.assertFalse(queued["external_request_observed"])
            completed = orchestrator.process_one(root, CLOUD_AGENT, queued["idempotency_key"])
            self.assertEqual(completed["status"], "dry-run-complete")
            self.assertEqual(completed["dry_run"]["status"], "source-handoff-created")
            self.assertFalse(completed["external_request_attempted"])
            self.assertFalse(completed["external_request_observed"])
            self.assertIsNone(completed["gateway_request_id"])
            receipt_schema = json.loads(
                (BASE / "schemas/local-development-receipt.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            cloud_agent.validate_json_schema_instance(
                completed, receipt_schema, context="local-development-receipt-v2"
            )
            handoff = root / completed["dry_run"]["handoff_relative_path"]
            self.assertTrue(handoff.is_file())
            self.assertFalse((root / "consents").exists())

    def test_task_bound_dry_run_accepts_a_nonfixture_job_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
            root = Path(temporary) / "queue"
            submitted = orchestrator.submit(
                root,
                CLOUD_AGENT,
                task_bytes(task_with_job("job-cloud-ui-desktop-001")),
                explicit_submit=True,
            )
            completed = orchestrator.process_one(
                root,
                CLOUD_AGENT,
                submitted["receipt"]["idempotency_key"],
            )
            handoff = json.loads(
                (root / completed["dry_run"]["handoff_relative_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["job_id"], "job-cloud-ui-desktop-001")
            self.assertEqual(handoff["provider_execution"]["mode"], "dry-run-fixture")
            self.assertFalse(handoff["provider_execution"]["external_request_attempted"])

    def test_duplicate_submission_is_idempotent_and_does_not_repeat_dry_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
            root = Path(temporary) / "queue"
            raw = task_bytes()
            first = orchestrator.submit(root, CLOUD_AGENT, raw, explicit_submit=True)
            completed = orchestrator.process_one(root, CLOUD_AGENT, first["receipt"]["idempotency_key"])
            second = orchestrator.submit(root, CLOUD_AGENT, raw, explicit_submit=True)
            self.assertTrue(second["duplicate"])
            self.assertEqual(second["receipt"], completed)
            self.assertEqual(len(list((root / "dry-runs").glob("run-*"))), 1)

    def test_same_immutable_request_new_attempt_has_a_distinct_queue_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-retry-") as temporary:
            root = Path(temporary) / "queue"
            first_task = current_task()
            second_task = retry_attempt(first_task, 2)
            self.assertEqual(
                first_task["immutable_task_digest_sha256"],
                second_task["immutable_task_digest_sha256"],
            )
            first = orchestrator.submit(
                root, CLOUD_AGENT, task_bytes(first_task), explicit_submit=True
            )
            second = orchestrator.submit(
                root, CLOUD_AGENT, task_bytes(second_task), explicit_submit=True
            )
            self.assertFalse(first["duplicate"])
            self.assertFalse(second["duplicate"])
            self.assertNotEqual(
                first["receipt"]["idempotency_key"],
                second["receipt"]["idempotency_key"],
            )
            self.assertEqual(
                first["receipt"]["provider_execution_identity_sha256"],
                second["receipt"]["provider_execution_identity_sha256"],
            )
            first_done = orchestrator.process_one(
                root, CLOUD_AGENT, first["receipt"]["idempotency_key"]
            )
            second_done = orchestrator.process_one(
                root, CLOUD_AGENT, second["receipt"]["idempotency_key"]
            )
            self.assertEqual(first_done["status"], "dry-run-complete")
            self.assertEqual(second_done["status"], "dry-run-complete")
            self.assertEqual(len(list((root / "dry-runs").glob("run-*"))), 2)

    def test_explicit_submit_and_exact_remote_consent_are_required(self) -> None:
        cases = []
        cases.append((current_task(), False, "explicit-submit-required"))
        denied = current_task()
        denied["remote_processing_consent"] = False
        denied["consent"]["decision"] = "not-requested"
        denied["consent"]["single_use"] = False
        cases.append((denied, True, "consent-required"))
        incomplete = current_task()
        incomplete["need_spec_complete"] = False
        cases.append((incomplete, True, "need-incomplete"))
        attempt_mismatch = current_task()
        attempt_mismatch["execution_attempt"]["ordinal"] = 2
        cases.append((attempt_mismatch, True, "attempt-binding-required"))
        consent_attempt_mismatch = current_task()
        consent_attempt_mismatch["consent"]["attempt_id"] = "attempt-0001-fedcba9876543210"
        cases.append((consent_attempt_mismatch, True, "consent-required"))
        consent_identity_mismatch = current_task()
        consent_identity_mismatch["consent"]["provider_execution_identity_sha256"] = "f" * 64
        cases.append((consent_identity_mismatch, True, "consent-required"))
        legacy = current_task()
        del legacy["model"]
        del legacy["consent"]["model"]
        cases.append((legacy, True, "model-binding-required"))
        historical_v1 = current_task()
        historical_v1["schema_version"] = "vibapp.cloud-codeagent-task.experimental-v1"
        del historical_v1["target"]["required_capabilities"]
        cases.append((historical_v1, True, "schema-preview-invalid"))
        for value in (None, "", "   "):
            invalid_task = current_task()
            invalid_task["model"] = value
            invalid_task["consent"]["model"] = value
            cases.append((invalid_task, True, "model-binding-required"))
        for task, explicit, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
                root = Path(temporary) / "queue"
                with self.assertRaises(orchestrator.QueueError) as caught:
                    orchestrator.submit(root, CLOUD_AGENT, task_bytes(task), explicit_submit=explicit)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(list((root / "ready").iterdir()), [])
                self.assertEqual(len(list((root / "quarantine").glob("rejected-*.json"))), 1)

    def test_crash_recovery_restores_claimed_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
            root = Path(temporary) / "queue"
            submitted = orchestrator.submit(root, CLOUD_AGENT, task_bytes(), explicit_submit=True)
            key = submitted["receipt"]["idempotency_key"]
            filename = orchestrator.task_filename(key)
            os.replace(root / "ready" / filename, root / "processing" / filename)
            recovery = orchestrator.recover(root)
            self.assertEqual(recovery["restored"], 1)
            self.assertTrue((root / "ready" / filename).is_file())
            receipt = orchestrator.process_one(root, CLOUD_AGENT, key)
            self.assertEqual(receipt["status"], "dry-run-complete")

    def test_corrupt_queued_task_and_legacy_outbox_are_quarantined_or_ignored(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
            root = Path(temporary) / "queue"
            legacy = root / "outbox"
            legacy.mkdir(parents=True)
            (legacy / "codeagent-tasks.jsonl").write_text('{"legacy":"must-not-run"}\n', encoding="utf-8")
            submitted = orchestrator.submit(root, CLOUD_AGENT, task_bytes(), explicit_submit=True)
            key = submitted["receipt"]["idempotency_key"]
            task_path = root / "ready" / orchestrator.task_filename(key)
            task_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(orchestrator.QueueError):
                orchestrator.process_one(root, CLOUD_AGENT, key)
            self.assertFalse(task_path.exists())
            self.assertIsNone(orchestrator.load_receipt(orchestrator.queue_layout(root), key))
            self.assertTrue((legacy / "codeagent-tasks.jsonl").is_file())
            self.assertGreaterEqual(len(list((root / "quarantine").iterdir())), 2)

    def test_request_concurrency_timeout_and_log_limits_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-orchestrator-") as temporary:
            root = Path(temporary) / "queue"
            layout = orchestrator.queue_layout(root)
            with self.assertRaises(orchestrator.QueueError) as caught:
                orchestrator.load_json_bytes(b"x" * (orchestrator.MAX_REQUEST_BYTES + 1), "oversize")
            self.assertEqual(caught.exception.code, "request-limit")
            slots = [orchestrator.acquire_slot(layout) for _ in range(orchestrator.MAX_CONCURRENCY)]
            try:
                with self.assertRaises(orchestrator.QueueError) as caught:
                    orchestrator.acquire_slot(layout)
                self.assertEqual(caught.exception.code, "concurrency-limit")
            finally:
                for slot in slots:
                    orchestrator.release_file(slot)
            with self.assertRaises(orchestrator.QueueError) as caught:
                orchestrator.run_bounded(
                    ["/usr/bin/python3", "-c", "import time; time.sleep(5)"],
                    timeout_seconds=1,
                    home=layout["runtime-home"],
                )
            self.assertEqual(caught.exception.code, "subprocess-timeout")
            with self.assertRaises(orchestrator.QueueError) as caught:
                orchestrator.append_event(layout, "x", value="y" * orchestrator.MAX_LOG_RECORD_BYTES)
            self.assertEqual(caught.exception.code, "log-limit")


if __name__ == "__main__":
    unittest.main()
