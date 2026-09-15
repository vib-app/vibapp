"""Offline only: no LAN model, user configuration or credentials are accessed."""
import base64
import copy
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_executor as docker


class LocalAIGatewayTests(unittest.TestCase):
    def body(self):
        return {"model": docker.LOCALAI_MODEL, "stream": True, "messages": [{"role": "user", "content": "synthetic protocol input"}]}

    def frame(self, body):
        return {"id": 1, "body": base64.b64encode(json.dumps(body).encode()).decode()}

    def connection(self, status=200):
        response = mock.Mock(status=status)
        response.getheader.return_value = "application/json; charset=utf-8"
        response.read1.side_effect = [docker.canonical({"choices": [{"index": 0, "message": {"role": "assistant", "content": "synthetic"}, "finish_reason": "stop"}]}), b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def test_model_selection_does_not_discover_credentials(self):
        with mock.patch.object(docker, "codex_connection", side_effect=AssertionError("credential discovery")), mock.patch.object(Path, "home", side_effect=AssertionError("host home")):
            self.assertEqual(docker.LocalAIGateway(docker.LOCALAI_MODEL).model, docker.LOCALAI_MODEL)
            self.assertEqual(docker.LocalAIGateway("vibapp/" + docker.LOCALAI_MODEL).model, docker.LOCALAI_MODEL)
            for model in ["qwen3-4b", "qwen3.8-27b", "different/" + docker.LOCALAI_MODEL, ""]:
                with self.assertRaisesRegex(docker.DockerError, "model-unsupported"):
                    docker.LocalAIGateway(model)

    def test_invalid_requests_cannot_contact_any_destination(self):
        changes = [
            {"model": "other"}, {"stream": False}, {"stream": 1}, {"headers": {"Authorization": "untrusted"}},
            {"base_url": "https://attacker.invalid"}, {"n_ctx": 262144}, {"max_tokens": 16385}, {"max_tokens": True},
            {"messages": []}, {"messages": [{"role": "developer", "content": "no"}]},
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://attacker.invalid"}}]}]},
            {"messages": [{"role": "tool", "content": "x" * (97 * 1024)}]},
            {"tools": [{"type": "url", "function": {}}]}, {"tools": [{"type": "function", "function": {"name": "x", "parameters": {}, "url": "evil"}}]},
            {"stream_options": {"headers": {}}}, {"temperature": "high"}, {"temperature": float("nan")},
            {"tool_choice": {"type": "function", "function": {"name": "absent"}}},
        ]
        with mock.patch.object(docker.http.client, "HTTPConnection") as connection:
            for changed in changes:
                with self.subTest(changed=list(changed)):
                    with self.assertRaisesRegex(docker.DockerError, "request-invalid"):
                        docker.LocalAIGateway(docker.LOCALAI_MODEL).relay(self.frame({**self.body(), **changed}), lambda _: None, threading.Event())
            connection.assert_not_called()

    def test_fixed_destination_no_auth_stream_and_private_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL, evidence_root=root)
            connection, frames = self.connection(), []
            with mock.patch.object(docker.http.client, "HTTPConnection", return_value=connection) as factory:
                gateway.relay(self.frame(self.body()), frames.append, threading.Event())
            factory.assert_called_once_with("192.168.199.170", port=8081, timeout=15)
            connection.sock.settimeout.assert_called_once_with(300)
            call = connection.request.call_args
            self.assertEqual(call.args, ("POST", "/v1/chat/completions"))
            self.assertEqual(set(call.kwargs["headers"]), {"Content-Type", "Accept", "User-Agent"})
            self.assertEqual(json.loads(call.kwargs["body"])["max_tokens"], 16384)
            self.assertFalse(json.loads(call.kwargs["body"])["stream"])
            self.assertNotIn("stream_options", json.loads(call.kwargs["body"]))
            self.assertEqual(call.kwargs["headers"]["Accept"], "application/json")
            self.assertEqual([item["type"] for item in frames], ["response-head", "response-chunk", "response-end"])
            evidence = json.loads((root / "upstream-request-01.json").read_bytes())
            self.assertEqual(evidence["request_sha256"], docker.digest(call.kwargs["body"]))
            self.assertEqual(evidence["model"], docker.LOCALAI_MODEL)
            self.assertFalse(evidence["upstream_stream"])
            self.assertTrue(evidence["downstream_stream"])
            self.assertEqual((root / "upstream-request-01.json").stat().st_mode & 0o777, 0o600)
            response = json.loads((root / "upstream-response-01.json").read_bytes())
            self.assertGreater(response["bytes"], 0)
            self.assertFalse(response["upstream_stream"])
            self.assertTrue(response["downstream_stream"])
            self.assertEqual(response["finish_reason"], "stop")
            self.assertNotIn("content", response)
            self.assertNotIn("reasoning", response)
            self.assertTrue(gateway.observed)
            connection.close.assert_called_once()

    def test_redirect_and_errors_never_follow_or_forward_upstream_prose(self):
        for status in [302, 307, 401, 429, 500]:
            connection, frames = self.connection(status), []
            with mock.patch.object(docker.http.client, "HTTPConnection", return_value=connection) as factory:
                docker.LocalAIGateway(docker.LOCALAI_MODEL).relay(self.frame(self.body()), frames.append, threading.Event())
            factory.assert_called_once()
            connection.getresponse.return_value.read1.assert_not_called()
            self.assertEqual(frames[0]["status"], 502)

    def test_response_protocol_and_byte_limit_fail_closed(self):
        for content_type, chunks, code in [("text/html", [b"untrusted"], "protocol-invalid"), ("text/event-stream", [b"data: no-fallback"], "protocol-invalid"), ("application/json", [b"x" * (docker.MAX_RESPONSE + 1)], "response-limit")]:
            connection = self.connection()
            connection.getresponse.return_value.getheader.return_value = content_type
            connection.getresponse.return_value.read1.side_effect = chunks
            with mock.patch.object(docker.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaisesRegex(docker.DockerError, code):
                    docker.LocalAIGateway(docker.LOCALAI_MODEL).relay(self.frame(self.body()), lambda _: None, threading.Event())
            connection.close.assert_called_once()

    def test_tool_round_trip_is_bounded_and_preserved(self):
        body = self.body()
        body["tools"] = [{"type": "function", "function": {"name": "read", "description": "Read a local file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
        body["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path":"source/lib.rs"}'}}]}, {"role": "tool", "tool_call_id": "call_1", "content": "synthetic source"}]
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        self.assertEqual(gateway.validate_request(self.frame(body)), {**body, "max_tokens": 16384})
        invalid = copy.deepcopy(body)
        invalid["messages"][1]["tool_calls"][0]["function"]["headers"] = {}
        with self.assertRaises(docker.DockerError):
            gateway.validate_request(self.frame(invalid))

    def test_budget_cancellation_and_existing_evidence_are_fail_closed(self):
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        gateway.requests = docker.OPENCODE_POLICY["max_model_requests"]
        with mock.patch.object(docker.http.client, "HTTPConnection") as factory:
            with self.assertRaisesRegex(docker.DockerError, "request-limit"):
                gateway.relay(self.frame(self.body()), lambda _: None, threading.Event())
            factory.assert_not_called()
        cancelled = threading.Event()
        cancelled.set()
        connection = self.connection()
        with mock.patch.object(docker.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(docker.DockerError, "cancelled"):
                docker.LocalAIGateway(docker.LOCALAI_MODEL).relay(self.frame(self.body()), lambda _: None, cancelled)
        connection.request.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "upstream-request-01.json").write_bytes(b"preserved")
            with mock.patch.object(docker.http.client, "HTTPConnection") as factory:
                with self.assertRaises(FileExistsError):
                    docker.LocalAIGateway(docker.LOCALAI_MODEL, evidence_root=root).relay(self.frame(self.body()), lambda _: None, threading.Event())
                factory.assert_not_called()
            self.assertEqual((root / "upstream-request-01.json").read_bytes(), b"preserved")

    def native(self, *, name="Bash", arguments='{"command":"pwd"}', content=None):
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content, "reasoning": "never export this", "tool_calls": [{"id": "native-call", "type": "function", "function": {"name": name, "arguments": arguments}}]}, "finish_reason": "tool_calls"}]}

    def native_request(self):
        return {**self.body(), "tools": [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "required": ["command"], "properties": {"command": {"type": "string"}}}}}]}

    def test_native_json_preserves_case_argument_bytes_and_excludes_reasoning(self):
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        arguments = '{ "command" : "pwd" }'
        output, summary = gateway.native_response(docker.canonical(self.native(arguments=arguments)), self.native_request())
        chunk = json.loads(output.split(b"\n\n")[0].removeprefix(b"data: "))
        call = chunk["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"], {"name": "Bash", "arguments": arguments})
        self.assertNotIn(b"never export this", output)
        self.assertNotIn("reasoning", json.dumps(summary))
        self.assertEqual(summary["tool_calls"][0]["arguments_sha256"], docker.digest(arguments.encode()))
        self.assertTrue(output.endswith(b"data: [DONE]\n\n"))

    def test_recorded_localai49_nonstream_native_index_is_exact_and_optional(self):
        # Calls/finish reasons copied from main's sanitized real, non-executing
        # tool-probe-nonstream.json; reconstruct only the enclosing choice here.
        fixture = json.loads((Path(__file__).parent / "fixtures/localai49-native-tool-probe.json").read_bytes())
        body = self.native()
        body["choices"][0]["message"]["tool_calls"] = fixture["tool_calls"]
        body["choices"][0]["finish_reason"] = fixture["finish_reasons"][0]
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        output, _ = gateway.native_response(docker.canonical(body), self.native_request())
        emitted = json.loads(output.split(b"\n\n")[0].removeprefix(b"data: "))["choices"][0]["delta"]["tool_calls"]
        self.assertEqual(emitted, fixture["tool_calls"])
        for invalid_index in [-1, 1, False, "0", None]:
            invalid = copy.deepcopy(body)
            invalid["choices"][0]["message"]["tool_calls"][0]["index"] = invalid_index
            with self.assertRaisesRegex(docker.DockerError, "protocol-invalid"):
                gateway.native_response(docker.canonical(invalid), self.native_request())

    def test_invalid_unknown_ambiguous_or_incomplete_native_calls_fail_before_sse(self):
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        cases = [self.native(name="other"), self.native(arguments="{}"), self.native(arguments='{"command":3}'),
                 self.native(arguments='{"command":"one","command":"two"}'), self.native(arguments="not json"),
                 self.native(arguments='{"command":NaN}'), self.native(arguments="[]")]
        wrong_finish = self.native(); wrong_finish["choices"][0]["finish_reason"] = "stop"; cases.append(wrong_finish)
        many_choices = self.native(); many_choices["choices"] *= 2; cases.append(many_choices)
        bad_calls = self.native(); bad_calls["choices"][0]["message"]["tool_calls"] *= 33; cases.append(bad_calls)
        duplicate_ids = self.native(); duplicate_ids["choices"][0]["message"]["tool_calls"] *= 2; cases.append(duplicate_ids)
        for body in cases:
            with self.assertRaisesRegex(docker.DockerError, "protocol-invalid"):
                gateway.native_response(docker.canonical(body), self.native_request())
        ambiguous = self.native_request()
        ambiguous["tools"].append({"type": "function", "function": {"name": "Bash", "parameters": {}}})
        with self.assertRaisesRegex(docker.DockerError, "protocol-invalid"):
            gateway.native_response(docker.canonical(self.native()), ambiguous)

    def test_nonstream_default_zero_indices_become_distinct_sse_calls(self):
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        body = self.native()
        calls = body["choices"][0]["message"]["tool_calls"]
        first = calls[0]
        calls[:] = [{**first, "id": f"synthetic-{i}", "index": 0} for i in range(3)]
        stream, summary = gateway.native_response(docker.canonical(body), self.native_request())
        emitted = json.loads(stream.split(b"\n\n")[0].removeprefix(b"data: "))["choices"][0]["delta"]["tool_calls"]
        self.assertEqual([call["index"] for call in emitted], [0, 1, 2])
        self.assertEqual([call["id"] for call in emitted], [f"synthetic-{i}" for i in range(3)])
        self.assertEqual([call["function"] for call in emitted], [first["function"]] * 3)
        self.assertEqual(len(summary["tool_calls"]), 3)
        for invalid in [-1, 2, True, "0", None]:
            calls[1]["index"] = invalid
            with self.assertRaisesRegex(docker.DockerError, "protocol-invalid"):
                gateway.native_response(docker.canonical(body), self.native_request())

    def test_plain_xml_is_never_converted_into_a_tool_call(self):
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": '<tool_call>\n<function=Bash>\n<parameter=command>pwd</parameter>\n</function>\n</tool_call>'}, "finish_reason": "stop"}]}
        output, summary = gateway.native_response(docker.canonical(body), self.native_request())
        delta = json.loads(output.split(b"\n\n")[0].removeprefix(b"data: "))["choices"][0]["delta"]
        self.assertNotIn("tool_calls", delta)
        self.assertEqual(delta["content"], body["choices"][0]["message"]["content"])
        self.assertEqual(summary["tool_calls"], [])

    def test_invalid_response_records_only_closed_diagnostic_not_model_text(self):
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        body = self.native(arguments="synthetic private malformed input")
        with self.assertRaises(docker.DockerError):
            gateway.native_response(docker.canonical(body), self.native_request())
        self.assertEqual(gateway.protocol_diagnostic, {"validation_stage": "arguments-json", "finish_reason": "tool_calls"})
        body = self.native()
        body["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(docker.DockerError):
            gateway.native_response(docker.canonical(body), self.native_request())
        self.assertEqual(gateway.protocol_diagnostic, {"validation_stage": "finish-tool-consistency", "finish_reason": "length"})
        self.assertNotIn("synthetic private", json.dumps(gateway.protocol_diagnostic))
        gateway.native_response(docker.canonical(self.native()), self.native_request())
        self.assertIsNone(gateway.protocol_diagnostic)

    def test_new_image_requires_exact_explicit_relay_policy(self):
        bridge_root = Path(docker.__file__).resolve().parent / "docker"
        bridge = (bridge_root / "entry.mjs").read_bytes() + (bridge_root / "opencode-provider.mjs").read_bytes()
        identity = {"version": "1.18.27", "bundle_sha256": "a" * 64, "bridge_sha256": docker.digest(bridge), "relay_policy": docker.LOCALAI_RELAY_POLICY}
        def observe(value):
            with mock.patch.object(docker, "docker_command", side_effect=[mock.Mock(returncode=0, stdout=("sha256:" + "a" * 64).encode()), mock.Mock(returncode=0, stdout=docker.canonical(value))]):
                return docker.image_identity(docker.OPENCODE_IMAGE, provider="opencode")
        self.assertEqual(observe(identity)["relay_policy"], docker.LOCALAI_RELAY_POLICY)
        for value in [{key: item for key, item in identity.items() if key != "relay_policy"}, {**identity, "relay_policy": {**docker.LOCALAI_RELAY_POLICY, "upstream_stream": True}}, {**identity, "relay_policy": {**docker.LOCALAI_RELAY_POLICY, "upstream_stream": 0}}, []]:
            with self.assertRaisesRegex(docker.DockerError, "identity-invalid"):
                observe(value)

    def test_completion_timeout_is_explicit_in_identity_bound_policy(self):
        self.assertEqual(docker.OPENCODE_POLICY["completion_socket_timeout_seconds"], 300)
        self.assertNotIn("completion_socket_timeout_seconds", docker.POLICY)
        changed = {**docker.OPENCODE_POLICY, "completion_socket_timeout_seconds": 90}
        self.assertNotEqual(docker.digest(docker.canonical(changed)), docker.digest(docker.canonical(docker.OPENCODE_POLICY)))

    def test_cancel_shutdown_interrupts_real_blocked_getresponse_promptly(self):
        # Real loopback socket only, no LAN/model request. The server deliberately
        # receives the request without sending HTTP headers, exercising the exact
        # nonstream getresponse() blocking point and inherited socket shutdown.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        received, release = threading.Event(), threading.Event()
        accepted, failures = [], []
        def serve():
            try:
                peer, _ = listener.accept()
                accepted.append(peer)
                peer.settimeout(3)
                request_bytes = b""
                while b"\r\n\r\n" not in request_bytes:
                    chunk = peer.recv(8192)
                    if not chunk:
                        return
                    request_bytes += chunk
                received.set()
                release.wait(5)
            finally:
                for peer in accepted:
                    peer.close()
        server = threading.Thread(target=serve)
        server.start()
        gateway = docker.LocalAIGateway(docker.LOCALAI_MODEL)
        cancelled = threading.Event()
        original_connection = docker.http.client.HTTPConnection
        def loopback_connection(host, *, port, timeout):
            self.assertEqual((host, port, timeout), ("192.168.199.170", 8081, 15))
            return original_connection("127.0.0.1", port=listener.getsockname()[1], timeout=timeout)
        def request():
            try:
                gateway.relay(self.frame(self.body()), lambda _: None, cancelled)
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=request)
        try:
            with mock.patch.object(docker.http.client, "HTTPConnection", side_effect=loopback_connection):
                worker.start()
                self.assertTrue(received.wait(3), "loopback did not receive request")
                self.assertTrue(worker.is_alive(), "request was not waiting for response headers")
                started = time.monotonic()
                cancelled.set()
                gateway.close()
                worker.join(2)
                self.assertFalse(worker.is_alive(), "cancel left getresponse blocked")
                self.assertLess(time.monotonic() - started, 2)
                self.assertTrue(failures)
                self.assertFalse(gateway.observed)
                self.assertIsNone(gateway.connection)
        finally:
            cancelled.set()
            gateway.close()
            release.set()
            for peer in accepted:
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            listener.close()
            server.join(3)
            if worker.ident is not None:
                worker.join(3)


if __name__ == "__main__":
    unittest.main()
