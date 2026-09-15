"""Explicit offline real-CLI probe; synthetic stream, NEVER a model connection."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codeagent_launcher as launcher
import docker_executor as backend

PROMPT = "This is an offline protocol fixture. Create source/src/protocol.rs beside the immutable support using your write tool, then finish. No application logic, no shell or network is needed."
FEEDBACK = "OFFLINE_COMPILER_SENTINEL: expected repaired marker in protocol.rs, found initial marker."
INITIAL = b"// synthetic protocol only\n"
REPAIRED = b"// synthetic repaired protocol only\n"


class SyntheticGateway(backend.LocalAIGateway):
    def __init__(self, *, repair=False):
        super().__init__(backend.LOCALAI_MODEL)
        self.repair = repair
        self.history_verified = False
        self.bounded_read_verified = False

    def tool_result(self, body, call_id):
        results = [item for item in body["messages"] if item["role"] == "tool" and item.get("tool_call_id") == call_id]
        if len(results) != 1:
            raise backend.DockerError("synthetic-probe-tool-result-missing")
        return str(results[0]["content"])

    def reply(self, body):
        names = [tool["function"]["name"] for tool in body["tools"]]
        if "write" not in names:
            raise backend.DockerError("synthetic-probe-write-tool-missing")
        if self.requests == 0:
            # The old image's real grep returned 'ripgrep execution failed'.
            # Verify the scoped deny plus actual bounded read fallback, without
            # downloading a new executable or enabling network/shell authority.
            if "grep" in names or "read" not in names:
                raise backend.DockerError("synthetic-probe-read-fallback-not-enforced")
            name, call_id = "read", "call_offline_read"
            arguments = {"filePath": "/work/source/src/vibapp_support.rs", "offset": 1, "limit": 2}
        elif self.requests == 1:
            output = self.tool_result(body, "call_offline_read")
            if "immutable synthetic support" not in output:
                raise backend.DockerError("synthetic-probe-bounded-read-failed")
            self.bounded_read_verified = True
            name, call_id = "Write", "call_offline_probe"
            arguments = {"filePath": "/work/source/src/protocol.rs", "content": INITIAL.decode()}
        elif self.requests == 2:
            self.tool_result(body, "call_offline_probe")
            return {"role": "assistant", "content": "OFFLINE_PROTOCOL_OK"}, "stop"
        elif self.requests == 3 and self.repair:
            # This request must be a continuation of the actual previous CLI
            # session, not a new session with a copied task or tool summary.
            users = [str(item["content"]) for item in body["messages"] if item["role"] == "user"]
            if sum(text.count(PROMPT) for text in users) != 1 or len(users) != 2 or PROMPT in users[-1]:
                raise backend.DockerError("synthetic-probe-task-history-not-continued")
            expected = "<compiler-diagnostic>\n" + FEEDBACK + "\n</compiler-diagnostic>"
            if expected not in users[-1] or "bounded repair round 1/2" not in users[-1]:
                raise backend.DockerError("synthetic-probe-exact-feedback-missing")
            self.tool_result(body, "call_offline_probe")
            if not any(item["role"] == "assistant" and item.get("content") == "OFFLINE_PROTOCOL_OK" for item in body["messages"]):
                raise backend.DockerError("synthetic-probe-completed-history-missing")
            if not any(call["id"] == "call_offline_probe" for item in body["messages"] if item["role"] == "assistant" for call in item.get("tool_calls", [])):
                raise backend.DockerError("synthetic-probe-native-call-history-missing")
            self.history_verified = True
            name, call_id = "Write", "call_offline_repair"
            arguments = {"filePath": "/work/source/src/protocol.rs", "content": REPAIRED.decode()}
        elif self.requests == 4 and self.repair:
            self.tool_result(body, "call_offline_repair")
            return {"role": "assistant", "content": "OFFLINE_REPAIR_OK"}, "stop"
        else:
            raise backend.DockerError("synthetic-probe-request-limit")
        # Preserve native capitalization; the real SDK repairs Write→write.
        return {"role": "assistant", "content": None, "reasoning": "synthetic hidden reasoning must not cross the relay", "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls"

    def relay(self, message, emit, cancelled):
        # Exercise the production JSON relay; replace only the actual connection
        # with a synthetic response. No LAN socket can be opened by this test.
        body = self.validate_request(message)
        system = "\n".join(item["content"] for item in body["messages"] if item["role"] == "system")
        if "You are the source-authoring OpenCode agent" not in system or "MUST run the lint" in system or "prefer to use the Task tool" in system:
            raise backend.DockerError("synthetic-probe-system-prompt-mismatch")
        delta, finish = self.reply(body)
        response = mock.Mock(status=200)
        response.getheader.return_value = "application/json"
        response.read1.side_effect = [backend.canonical({"choices": [{"index": 0, "message": delta, "finish_reason": finish}]}), b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(backend.http.client, "HTTPConnection", return_value=connection) as factory:
            super().relay(message, emit, cancelled)
        factory.assert_called_once_with("192.168.199.170", port=8081, timeout=300)
        upstream = json.loads(connection.request.call_args.kwargs["body"])
        if upstream["stream"] is not False or "stream_options" in upstream:
            raise backend.DockerError("synthetic-probe-json-mode-mismatch")


@unittest.skipUnless(os.environ.get("VIBAPP_TEST_OPENCODE_IMAGE") == "1", "explicit offline Docker integration opt-in required")
class OpenCodeContainerTests(unittest.TestCase):
    def test_actual_cli_consumes_chat_tool_stream_and_exports_quiescent_source(self):
        self.run_probe(repair=False)

    def test_actual_cli_continues_tool_and_task_history_with_feedback_only_then_exports_repair(self):
        self.run_probe(repair=True)

    def run_probe(self, *, repair):
        observed = backend.image_identity(backend.OPENCODE_IMAGE, provider="opencode")
        with tempfile.TemporaryDirectory(prefix="vibapp-opencode-offline-") as directory:
            root = Path(directory)
            content = b"Offline protocol fixture only, not a VibApp application."
            files = [{"path": "contracts/diagnostic.txt", "base64": base64.b64encode(content).decode(), "sha256": backend.digest(content)}]
            support = b"// immutable synthetic support input\n"
            files.append({"path": "source/src/vibapp_support.rs", "base64": base64.b64encode(support).decode(), "sha256": backend.digest(support)})
            prompt = PROMPT
            limits = {"memory_bytes": 2 * 1024**3, "pids": 64, "wall_time_seconds": 60, "cpu_seconds": 45, "workspace_bytes": 16 * 1024**2}
            profile = launcher.ProviderProfile(
                provider_profile_id="opencode-offline-probe", provider_profile_digest_sha256=backend.digest(backend.canonical(observed)),
                provider_id="opencode", allowed_models=frozenset({backend.LOCALAI_MODEL}), executor_kind=launcher.ExecutorKind.DOCKER,
                image_digest_sha256=observed["image_id"][7:], resource_policy_id="offline-probe", resource_policy_digest_sha256=backend.digest(backend.canonical(limits)),
                network_policy_id="synthetic-stream-only", network_policy_digest_sha256=observed["policy_sha256"], max_output_bytes=4 * 1024**2,
                output_media_type="application/vnd.vibapp.source-files+json")
            gateway = SyntheticGateway(repair=repair)
            checks = []

            def check_source(message, cancelled, remaining):
                self.assertFalse(cancelled.is_set())
                self.assertGreater(remaining, 0)
                self.assertEqual(message["id"], len(checks))
                candidate = {record["path"]: base64.b64decode(record["base64"]) for record in message["files"]}
                self.assertEqual(candidate["source/src/protocol.rs"], INITIAL if not checks else REPAIRED)
                self.assertEqual(candidate["source/src/vibapp_support.rs"], support)
                checks.append(message["id"])
                # Synthetic compiler protocol fixture: no Rust compiler executes.
                return {"ok": len(checks) == 2, "repairable": True, "diagnostic": FEEDBACK}

            executor = backend.DockerExecutor(image_id=observed["image_id"], input_payload={"files": files, "prompt": prompt, "model": backend.LOCALAI_MODEL}, gateway=gateway, limits=limits, state_root=root, source_checker=check_source if repair else None)
            service = launcher.LauncherService(profiles=[profile], executors=[executor])
            request = launcher.LaunchRequest.from_mapping({"schema_version": launcher.SCHEMA_VERSION, "job_id": "offline-opencode", "attempt_id": root.name, "idempotency_key": root.name,
                "provider_profile_id": profile.provider_profile_id, "provider_id": "opencode", "model": backend.LOCALAI_MODEL,
                "task_digest_sha256": backend.digest(prompt.encode()), "input_digest_sha256": backend.digest(backend.canonical(files)), "prompt_digest_sha256": backend.digest(prompt.encode()),
                "resource_policy_id": profile.resource_policy_id, "network_policy_id": profile.network_policy_id})
            state = service.submit(request)
            deadline = time.monotonic() + 80
            while state["state"] not in {"succeeded", "failed", "cancelled", "held"} and time.monotonic() < deadline:
                state = service.status(request.job_id, request.attempt_id)
                time.sleep(.05)
            if state["state"] != "succeeded":
                service.cancel(request.job_id, request.attempt_id)
            self.assertEqual(state["state"], "succeeded", (executor.failure_code, gateway.requests))
            result = executor.status(state["backend_execution_id"])
            self.assertTrue(result.cleanup_confirmed)
            self.assertTrue(result.whole_job_quiescent)
            self.assertEqual(gateway.requests, 5 if repair else 3)
            self.assertTrue(gateway.bounded_read_verified)
            self.assertEqual(gateway.history_verified, repair)
            self.assertEqual(checks, [0, 1] if repair else [])
            output = json.loads(result.success.output)
            self.assertEqual(output["repair_rounds"], int(repair))
            records = {record["path"]: base64.b64decode(record["base64"]) for record in output["files"]}
            self.assertEqual(records["source/src/protocol.rs"], REPAIRED if repair else INITIAL)
            self.assertEqual(records["source/src/vibapp_support.rs"], support)


if __name__ == "__main__":
    unittest.main()
