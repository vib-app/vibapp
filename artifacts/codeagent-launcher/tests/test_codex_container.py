"""Opt-in real Codex CLI / offline Responses fixture; NOT model or app acceptance.

The synthetic custom/function-tool stream follows the Responses tool-call envelope:
https://developers.openai.com/api/docs/guides/function-calling
Only the real CLI writes the sibling source file. No compiler, gateway connection,
credential discovery, generated application logic, or publication is involved.
"""
import base64
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codeagent_launcher as launcher
import docker_executor as backend


MODEL = "gpt-5.6-sol"
PROMPT = (
    "Offline tool-protocol fixture only, not a VibApp application. Use an editing tool "
    "to add source/src/lib.rs beside the immutable source/src/vibapp_support.rs. "
    "The new file contains only this comment: // offline sibling-write fixture only\n"
    "Do not modify support or contracts, compile, or access network. A fixed offline "
    "exec_command filesystem diagnostic is allowed if apply_patch is unavailable."
)
SOURCE = b"// offline sibling-write fixture only\n"
SUPPORT = b"// immutable synthetic support input\n"
CONTRACT = b"Offline protocol fixture only; no application acceptance.\n"
CALL_ID = "call_offline_sibling_write"
PATCH = "*** Begin Patch\n*** Add File: source/src/lib.rs\n+" + SOURCE.decode() + "*** End Patch"
WRITE_SENTINEL = "OFFLINE_SIBLING_WRITE_OK"
# This fixed tool command is a filesystem/UID oracle, not application source.
WRITE_DIAGNOSTIC = "node -e " + shlex.quote(
    "const fs=require('node:fs');"
    "const s=fs.statSync('source/src/vibapp_support.rs');"
    "if(s.uid!==0||(s.mode&0o222)!==0)throw Error('immutable support mode changed');"
    "if(fs.statSync('source/src').uid!==1000)throw Error('source directory owner mismatch');"
    "if(process.env.NODE_OPTIONS!=='--v8-pool-size=2'||process.env.UV_THREADPOOL_SIZE!=='2'||process.env.RAYON_NUM_THREADS!=='2'||process.env.TOKIO_WORKER_THREADS!=='2')throw Error('tool thread defaults missing');"
    "fs.writeFileSync('source/src/lib.rs'," + json.dumps(SOURCE.decode()) + ",{flag:'wx'});"
    "process.stdout.write('" + WRITE_SENTINEL + "');"
)


def stream_events(item, ordinal):
    """Emit bounded synthetic SSE, including actual custom-tool input events."""
    response_id = f"resp_offline_{ordinal}"
    events = [{"type": "response.created", "response": {
        "id": response_id, "object": "response", "status": "in_progress", "output": [],
    }}]
    initial = {**item}
    if item["type"] == "custom_tool_call":
        initial["input"] = ""
    elif item["type"] == "function_call":
        initial["arguments"] = ""
    else:
        initial["content"] = []
    events.append({"type": "response.output_item.added", "output_index": 0, "item": initial})
    if item["type"] == "custom_tool_call":
        events.extend([
            {"type": "response.custom_tool_call_input.delta", "output_index": 0, "item_id": item["id"], "delta": item["input"]},
            {"type": "response.custom_tool_call_input.done", "output_index": 0, "item_id": item["id"], "input": item["input"]},
        ])
    elif item["type"] == "function_call":
        events.extend([
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"], "delta": item["arguments"]},
            {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": item["id"], "arguments": item["arguments"]},
        ])
    else:
        content = item["content"][0]
        events.extend([
            {"type": "response.content_part.added", "output_index": 0, "item_id": item["id"], "content_index": 0, "part": {**content, "text": ""}},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": item["id"], "content_index": 0, "delta": content["text"]},
            {"type": "response.output_text.done", "output_index": 0, "item_id": item["id"], "content_index": 0, "text": content["text"]},
            {"type": "response.content_part.done", "output_index": 0, "item_id": item["id"], "content_index": 0, "part": content},
        ])
    events.extend([
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {
            "id": response_id, "object": "response", "status": "completed", "output": [item],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }},
    ])
    return b"".join(
        b"event: " + event["type"].encode() + b"\ndata: "
        + backend.canonical({**event, "sequence_number": sequence}) + b"\n\n"
        for sequence, event in enumerate(events)
    )


class SyntheticResponsesGateway:
    """No OpenAIGateway inheritance: its constructor discovers host credentials."""
    def __init__(self):
        self.requests = 0
        self.observed = False
        self.last_status = None
        self.last_error = None
        self.last_request = None
        self.tool_result_verified = False
        self.advertised_tools = []
        self.output_type = "custom_tool_call_output"
        self.used_exec_fallback = False
        self.closed = False

    def close(self):
        self.closed = True

    def relay(self, message, emit, cancelled):
        if cancelled.is_set():
            raise backend.DockerError("provider-cancelled")
        body = json.loads(base64.b64decode(message["body"], validate=True))
        if body.get("model") != MODEL or body.get("stream") is not True:
            raise backend.DockerError("synthetic-probe-request-mismatch")
        if self.requests == 0:
            tools = body.get("tools", [])
            self.advertised_tools = [
                {"type": str(tool.get("type", ""))[:40], "name": str(tool.get("name", ""))[:80]}
                for tool in tools[:32]
            ]
            patch_tools = [tool for tool in tools if tool.get("name") == "apply_patch"]
            if not patch_tools and (not tools or any(tool.get("name") == "exec_command" for tool in tools)):
                # Some CLI/model configurations omit schemas but retain the
                # registered exec_command tool. Its returned call result and
                # independently exported bytes remain mandatory, not assumed.
                self.output_type = "function_call_output"
                self.used_exec_fallback = True
                item = {"type": "function_call", "id": "fc_offline_write", "call_id": CALL_ID,
                        "name": "exec_command", "arguments": json.dumps({"cmd": WRITE_DIAGNOSTIC, "workdir": "/work", "yield_time_ms": 10000, "max_output_tokens": 1000})}
            elif len(patch_tools) != 1:
                raise backend.DockerError("synthetic-probe-apply-patch-tool-missing")
            elif patch_tools[0].get("type") == "custom":
                item = {"type": "custom_tool_call", "id": "ctc_offline_write", "call_id": CALL_ID,
                        "name": "apply_patch", "input": PATCH}
            elif patch_tools[0].get("type") == "function" and "input" in patch_tools[0].get("parameters", {}).get("properties", {}):
                self.output_type = "function_call_output"
                item = {"type": "function_call", "id": "fc_offline_write", "call_id": CALL_ID,
                        "name": "apply_patch", "arguments": json.dumps({"input": PATCH})}
            else:
                raise backend.DockerError("synthetic-probe-apply-patch-schema-mismatch")
        elif self.requests == 1:
            results = [item for item in body.get("input", [])
                       if item.get("type") == self.output_type and item.get("call_id") == CALL_ID]
            if len(results) != 1:
                raise backend.DockerError("synthetic-probe-tool-result-missing")
            output = results[0].get("output")
            output_text = output if isinstance(output, str) else json.dumps(output)
            if (self.used_exec_fallback and WRITE_SENTINEL not in output_text) or (not self.used_exec_fallback and ("Success." not in output_text or "source/src/lib.rs" not in output_text)):
                raise backend.DockerError("synthetic-probe-sibling-write-failed")
            self.tool_result_verified = True
            item = {"type": "message", "id": "msg_offline_done", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "OFFLINE_SIBLING_WRITE_PROTOCOL_COMPLETE", "annotations": []}]}
        else:
            raise backend.DockerError("synthetic-probe-request-limit")
        self.requests += 1
        self.last_status = 200
        # This means a synthetic response was observed, never external model I/O.
        self.observed = True
        self.last_request = {"ordinal": self.requests, "phase": "complete", "bytes": 0}
        payload = stream_events(item, self.requests)
        emit({"type": "response-head", "id": message["id"], "status": 200, "content_type": "text/event-stream"})
        emit({"type": "response-chunk", "id": message["id"], "body": base64.b64encode(payload).decode()})
        emit({"type": "response-end", "id": message["id"]})


class CodexOfflineFixtureTests(unittest.TestCase):
    def message(self, **fields):
        return {"id": 1, "body": base64.b64encode(backend.canonical({"model": MODEL, "stream": True, **fields})).decode()}

    def test_fixture_emits_patch_not_source_result_and_requires_actual_tool_success(self):
        gateway = SyntheticResponsesGateway()
        emitted = []
        gateway.relay(self.message(tools=[{"type": "custom", "name": "apply_patch"}]), emitted.append, threading.Event())
        self.assertEqual([item["type"] for item in emitted], ["response-head", "response-chunk", "response-end"])
        stream = base64.b64decode(emitted[1]["body"])
        self.assertIn(b"response.custom_tool_call_input.done", stream)
        self.assertIn(b"source/src/lib.rs", stream)
        self.assertNotIn(b'"type":"result"', stream)
        with self.assertRaisesRegex(backend.DockerError, "synthetic-probe-sibling-write-failed"):
            gateway.relay(self.message(input=[{"type": "custom_tool_call_output", "call_id": CALL_ID, "output": "Failed to write file: permission denied"}]), emitted.append, threading.Event())
        self.assertFalse(gateway.tool_result_verified)
        self.assertEqual(gateway.requests, 1)

    def test_fixture_never_reads_credentials_or_opens_http_connections(self):
        with mock.patch.object(backend, "codex_connection", side_effect=AssertionError("credentials forbidden")) as auth, mock.patch.object(backend.http.client, "HTTPConnection", side_effect=AssertionError("network forbidden")) as http, mock.patch.object(backend.http.client, "HTTPSConnection", side_effect=AssertionError("network forbidden")) as https:
            gateway = SyntheticResponsesGateway()
            gateway.relay(self.message(tools=[{"type": "custom", "name": "apply_patch"}]), lambda value: None, threading.Event())
            gateway.close()
        auth.assert_not_called()
        http.assert_not_called()
        https.assert_not_called()

    def test_schema_omission_fallback_requires_real_matching_tool_result_and_stays_bounded(self):
        gateway = SyntheticResponsesGateway()
        emitted = []
        gateway.relay(self.message(), emitted.append, threading.Event())
        self.assertTrue(gateway.used_exec_fallback)
        stream = base64.b64decode(emitted[1]["body"])
        self.assertIn(b"response.function_call_arguments.done", stream)
        self.assertIn(b"exec_command", stream)
        with self.assertRaisesRegex(backend.DockerError, "synthetic-probe-tool-result-missing"):
            gateway.relay(self.message(input=[{"type": "function_call_output", "call_id": "wrong-call", "output": WRITE_SENTINEL}]), emitted.append, threading.Event())
        with self.assertRaisesRegex(backend.DockerError, "synthetic-probe-sibling-write-failed"):
            gateway.relay(self.message(input=[{"type": "function_call_output", "call_id": CALL_ID, "output": "Permission denied"}]), emitted.append, threading.Event())
        self.assertFalse(gateway.tool_result_verified)
        gateway.relay(self.message(input=[{"type": "function_call_output", "call_id": CALL_ID, "output": WRITE_SENTINEL}]), emitted.append, threading.Event())
        self.assertTrue(gateway.tool_result_verified)
        self.assertEqual(gateway.requests, 2)
        with self.assertRaisesRegex(backend.DockerError, "synthetic-probe-request-limit"):
            gateway.relay(self.message(), emitted.append, threading.Event())


@unittest.skipUnless(os.environ.get("VIBAPP_TEST_CODEX_IMAGE") == "1", "explicit offline Docker integration opt-in required")
class CodexContainerTests(unittest.TestCase):
    def test_actual_cli_bad_stream_preserves_closed_failure_before_cleanup(self):
        class BadStreamGateway(SyntheticResponsesGateway):
            def relay(self, message, emit, cancelled):
                if cancelled.is_set():
                    raise backend.DockerError("provider-cancelled")
                if self.requests:
                    raise backend.DockerError("synthetic-probe-request-limit")
                self.requests += 1
                self.observed = True
                self.last_status = 200
                payload = b'data: {"type":"response.created","response":PRIVATE_INVALID_JSON}\n\n'
                self.last_request = {"ordinal": 1, "phase": "complete", "bytes": len(payload)}
                emit({"type": "response-head", "id": message["id"], "status": 200, "content_type": "text/event-stream"})
                emit({"type": "response-chunk", "id": message["id"], "body": base64.b64encode(payload).decode()})
                emit({"type": "response-end", "id": message["id"]})

        observed = backend.image_identity(backend.IMAGE, provider="codex")
        evidence = os.environ.get("VIBAPP_CODEX_TEST_EVIDENCE_ROOT")
        if evidence:
            self.assertTrue(Path(evidence).is_absolute())
            context = nullcontext(tempfile.mkdtemp(prefix="failure-offline-", dir=evidence))
        else:
            context = tempfile.TemporaryDirectory(prefix="vibapp-failure-offline-")
        with context as directory:
            root = Path(directory)
            inputs = {"contracts/diagnostic.txt": CONTRACT, "source/src/vibapp_support.rs": SUPPORT}
            files = [{"path": path, "base64": base64.b64encode(content).decode(), "sha256": backend.digest(content)} for path, content in inputs.items()]
            limits = {"memory_bytes": 2 * 1024**3, "pids": 64, "wall_time_seconds": 30,
                      "cpu_seconds": 20, "workspace_bytes": 16 * 1024**2}
            profile = launcher.ProviderProfile(
                provider_profile_id="codex-offline-failure-probe", provider_profile_digest_sha256=backend.digest(backend.canonical(observed)),
                provider_id="codex", allowed_models=frozenset({MODEL}), executor_kind=launcher.ExecutorKind.DOCKER,
                image_digest_sha256=observed["image_id"][7:], resource_policy_id="offline-probe", resource_policy_digest_sha256=backend.digest(backend.canonical(limits)),
                network_policy_id="synthetic-responses-only", network_policy_digest_sha256=observed["policy_sha256"], max_output_bytes=4 * 1024**2,
                output_media_type="application/vnd.vibapp.source-files+json")
            gateway = BadStreamGateway()
            executor = backend.DockerExecutor(image_id=observed["image_id"], input_payload={"files": files, "prompt": PROMPT, "model": MODEL}, gateway=gateway, limits=limits, state_root=root)
            service = launcher.LauncherService(profiles=[profile], executors=[executor])
            request = launcher.LaunchRequest.from_mapping({
                "schema_version": launcher.SCHEMA_VERSION, "job_id": "offline-codex-failure", "attempt_id": root.name, "idempotency_key": root.name,
                "provider_profile_id": profile.provider_profile_id, "provider_id": "codex", "model": MODEL,
                "task_digest_sha256": backend.digest(PROMPT.encode()), "input_digest_sha256": backend.digest(backend.canonical(files)), "prompt_digest_sha256": backend.digest(PROMPT.encode()),
                "resource_policy_id": profile.resource_policy_id, "network_policy_id": profile.network_policy_id,
            })
            with mock.patch.object(backend, "codex_connection", side_effect=AssertionError("credentials forbidden")), \
                 mock.patch.object(backend.http.client, "HTTPConnection", side_effect=AssertionError("network forbidden")), \
                 mock.patch.object(backend.http.client, "HTTPSConnection", side_effect=AssertionError("network forbidden")):
                try:
                    state = service.submit(request)
                    deadline = time.monotonic() + 40
                    while state["state"] not in {"succeeded", "failed", "cancelled", "held"} and time.monotonic() < deadline:
                        state = service.status(request.job_id, request.attempt_id)
                        time.sleep(.05)
                    self.assertEqual(state["state"], "failed", executor.failure_code)
                    terminal = json.loads((root / "docker-terminal.json").read_bytes())
                    value = backend.validate_failure_diagnostic(terminal["failure_diagnostic"])
                    self.assertEqual(terminal["failure_code"], "provider-failed")
                    self.assertEqual(value["failure_origin"], "provider-process")
                    self.assertEqual(value["child_exit_code"], 1)
                    self.assertIn(value["provider_error_category"], {"stream-decode", "stream-disconnected"})
                    self.assertGreater(value["stdout_bytes"], 0)
                    self.assertFalse(value["output_limit_exceeded"])
                    self.assertFalse(value["frame_limit_exceeded"])
                    self.assertNotIn("PRIVATE", json.dumps(terminal))
                    self.assertTrue(terminal["cleanup_confirmed"])
                    self.assertEqual(gateway.requests, 1)
                    self.assertFalse((root / "docker-receipt.json").exists())
                    self.assertEqual(list(root.glob("source-check-*")), [])
                finally:
                    for execution_id, job in executor.jobs.items():
                        if job["thread"].is_alive():
                            executor.cancel(execution_id)
                        job["thread"].join(timeout=20)
                        self.assertFalse(job["thread"].is_alive())
                        self.assertTrue(executor.status(execution_id).cleanup_confirmed)
                        self.assertIsNone(executor._inspect(job["name"]))

    def test_actual_cli_creates_sibling_beside_immutable_support_and_cleans_up(self):
        observed = backend.image_identity(backend.IMAGE, provider="codex")
        with tempfile.TemporaryDirectory(prefix="vibapp-codex-offline-") as directory:
            root = Path(directory)
            inputs = {"contracts/diagnostic.txt": CONTRACT, "source/src/vibapp_support.rs": SUPPORT}
            files = [{"path": path, "base64": base64.b64encode(content).decode(), "sha256": backend.digest(content)} for path, content in inputs.items()]
            limits = {"memory_bytes": 2 * 1024**3, "pids": 64, "wall_time_seconds": 60,
                      "cpu_seconds": 45, "workspace_bytes": 16 * 1024**2}
            profile = launcher.ProviderProfile(
                provider_profile_id="codex-offline-sibling-probe", provider_profile_digest_sha256=backend.digest(backend.canonical(observed)),
                provider_id="codex", allowed_models=frozenset({MODEL}), executor_kind=launcher.ExecutorKind.DOCKER,
                image_digest_sha256=observed["image_id"][7:], resource_policy_id="offline-probe", resource_policy_digest_sha256=backend.digest(backend.canonical(limits)),
                network_policy_id="synthetic-responses-only", network_policy_digest_sha256=observed["policy_sha256"], max_output_bytes=4 * 1024**2,
                output_media_type="application/vnd.vibapp.source-files+json")
            gateway = SyntheticResponsesGateway()
            executor = backend.DockerExecutor(image_id=observed["image_id"], input_payload={"files": files, "prompt": PROMPT, "model": MODEL}, gateway=gateway, limits=limits, state_root=root)
            service = launcher.LauncherService(profiles=[profile], executors=[executor])
            request = launcher.LaunchRequest.from_mapping({
                "schema_version": launcher.SCHEMA_VERSION, "job_id": "offline-codex-sibling", "attempt_id": root.name, "idempotency_key": root.name,
                "provider_profile_id": profile.provider_profile_id, "provider_id": "codex", "model": MODEL,
                "task_digest_sha256": backend.digest(PROMPT.encode()), "input_digest_sha256": backend.digest(backend.canonical(files)), "prompt_digest_sha256": backend.digest(PROMPT.encode()),
                "resource_policy_id": profile.resource_policy_id, "network_policy_id": profile.network_policy_id,
            })
            with mock.patch.object(backend, "codex_connection", side_effect=AssertionError("credentials forbidden")) as auth, mock.patch.object(backend.http.client, "HTTPConnection", side_effect=AssertionError("network forbidden")) as http, mock.patch.object(backend.http.client, "HTTPSConnection", side_effect=AssertionError("network forbidden")) as https:
                try:
                    state = service.submit(request)
                    deadline = time.monotonic() + 75
                    while state["state"] not in {"succeeded", "failed", "cancelled", "held"} and time.monotonic() < deadline:
                        state = service.status(request.job_id, request.attempt_id)
                        time.sleep(.05)
                    self.assertEqual(state["state"], "succeeded", (executor.failure_code, gateway.requests, gateway.advertised_tools))
                    result = executor.status(state["backend_execution_id"])
                    self.assertTrue(result.cleanup_confirmed)
                    self.assertTrue(result.whole_job_quiescent)
                    self.assertEqual(gateway.requests, 2)
                    self.assertTrue(gateway.tool_result_verified)
                    output = json.loads(result.success.output)
                    self.assertEqual(output["repair_rounds"], 0)
                    records = output["files"]
                    self.assertEqual(len(records), 3)
                    expected = {**inputs, "source/src/lib.rs": SOURCE}
                    self.assertEqual({item["path"] for item in records}, set(expected))
                    for item in records:
                        content = base64.b64decode(item["base64"], validate=True)
                        self.assertEqual(content, expected[item["path"]])
                        self.assertEqual(item["sha256"], backend.digest(content))
                    self.assertIsNone(executor.source_checker)
                finally:
                    # An assertion or timeout must not orphan the actual CLI/container.
                    for execution_id, job in executor.jobs.items():
                        if job["thread"].is_alive():
                            executor.cancel(execution_id)
                        job["thread"].join(timeout=20)
                        self.assertFalse(job["thread"].is_alive(), "offline probe worker did not stop")
                        final = executor.status(execution_id)
                        self.assertTrue(final.cleanup_confirmed, "offline probe container cleanup unconfirmed")
                        self.assertTrue(final.whole_job_quiescent)
                        self.assertIsNone(executor._inspect(job["name"]))
            auth.assert_not_called()
            http.assert_not_called()
            https.assert_not_called()


if __name__ == "__main__":
    unittest.main()
