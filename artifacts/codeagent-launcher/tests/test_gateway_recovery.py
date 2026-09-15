"""Synthetic transport faults; no Docker, credentials or live model requests."""
import base64
import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_executor as docker


class GatewayRecoveryTests(unittest.TestCase):
    def gateway(self):
        with mock.patch.object(docker, "codex_connection", return_value=(
                {"kind": "https", "endpoint": "https://example.test/v1"}, "host-secret")):
            gateway = docker.OpenAIGateway("bound-model")
        gateway.progress_callback = mock.Mock()
        return gateway

    def message(self):
        return {"id": 7, "body": base64.b64encode(json.dumps({
            "model": "bound-model", "stream": True,
            "input": [{"role": "user", "content": "synthetic private requirement"}],
        }).encode()).decode()}

    def connection(self, status, retry_after=None):
        response = mock.Mock(status=status)
        response.getheader.return_value = retry_after
        response.read1.side_effect = [b'data: {"type":"response.completed"}\n\n', b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def cancellation(self):
        cancelled = mock.Mock()
        cancelled.is_set.return_value = cancelled.wait.return_value = False
        return cancelled

    def test_502_then_success_stays_in_same_logical_request_and_forwards_only_success(self):
        gateway, frames, cancel = self.gateway(), [], self.cancellation()
        connections = [self.connection(502), self.connection(200)]
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=connections):
            gateway.relay(self.message(), frames.append, cancel)
        self.assertEqual((gateway.requests, gateway.logical_requests, gateway.model_retries), (2, 1, 1))
        self.assertEqual([f["type"] for f in frames], ["response-head", "response-chunk", "response-end"])
        self.assertEqual(frames[0]["status"], 200)
        first, second = [connection.request.call_args for connection in connections]
        self.assertEqual(first.args, second.args)
        self.assertEqual(first.kwargs["headers"], second.kwargs["headers"])
        bodies = [json.loads(call.kwargs["body"]) for call in (first, second)]
        self.assertIn("used: 1 total", bodies[0].pop("instructions"))
        self.assertIn("used: 2 total", bodies[1].pop("instructions"))
        self.assertEqual(*bodies)  # Only the trusted remaining-budget notice changes.
        for connection in connections:
            connection.close.assert_called_once()
        self.assertEqual([r["http_status"] for r in gateway.request_history], [502, 200])
        self.assertIsNone(gateway.last_error)
        retry = gateway.progress_callback.call_args_list[1].args[0]
        self.assertEqual(retry["state"], "model-retry")
        self.assertEqual(retry["retry"]["attempt"], 2)
        self.assertEqual(gateway.progress_callback.call_args.args[0]["state"], "authoring")
        self.assertNotIn("secret", json.dumps(gateway.request_history))
        self.assertNotIn("private", json.dumps(gateway.request_history))

    def test_three_transient_rejections_stop_without_forwarding_or_fourth_request(self):
        gateway, frames, cancel = self.gateway(), [], self.cancellation()
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=[
                self.connection(502), self.connection(503), self.connection(504)]) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                gateway.relay(self.message(), frames.append, cancel)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(gateway.model_retries, 2)
        self.assertEqual(frames, [])
        self.assertEqual(cancel.wait.call_count, 2)
        self.assertEqual(gateway.last_status, 504)

    def test_rate_limit_respects_retry_after_and_recovers(self):
        gateway, cancel = self.gateway(), self.cancellation()
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=[
                self.connection(429, "7"), self.connection(200)]):
            gateway.relay(self.message(), lambda _: None, cancel)
        cancel.wait.assert_called_once_with(7)
        self.assertEqual(gateway.model_retries, 1)

    def test_long_retry_after_stops_instead_of_violating_server_delay(self):
        gateway, cancel = self.gateway(), self.cancellation()
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(429, "120")) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-rate-limited"):
                gateway.relay(self.message(), lambda _: None, cancel)
        factory.assert_called_once()
        cancel.wait.assert_not_called()

    def test_retry_after_date_and_malformed_header_are_bounded(self):
        with mock.patch.object(docker.time, "time", return_value=0), mock.patch.object(docker.random, "uniform", return_value=0):
            self.assertEqual(docker.OpenAIGateway.retry_delay("Thu, 01 Jan 1970 00:00:10 GMT", 1), 10)
            self.assertEqual(docker.OpenAIGateway.retry_delay("untrusted text", 2), 2)
            self.assertIsNone(docker.OpenAIGateway.retry_delay("999999", 1))

    def test_auth_invalid_and_unknown_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 409, 500, 501):
            with self.subTest(status=status):
                gateway, cancel, frames = self.gateway(), self.cancellation(), []
                with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(status)) as factory:
                    gateway.relay(self.message(), frames.append, cancel)
                factory.assert_called_once()
                cancel.wait.assert_not_called()
                self.assertEqual(frames[0]["status"], status)
                self.assertEqual(gateway.model_retries, 0)

    def test_cancellation_during_backoff_sends_no_second_request(self):
        gateway, cancel = self.gateway(), self.cancellation()
        cancel.wait.return_value = True
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(502)) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-cancelled"):
                gateway.relay(self.message(), lambda _: None, cancel)
        factory.assert_called_once()
        self.assertEqual(gateway.model_retries, 0)

    def test_existing_wire_budget_includes_automatic_retries(self):
        gateway, cancel = self.gateway(), self.cancellation()
        gateway.requests = docker.MAX_REQUESTS - 1
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(502)) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                gateway.relay(self.message(), lambda _: None, cancel)
        factory.assert_called_once()
        self.assertEqual(gateway.requests, docker.MAX_REQUESTS)
        cancel.wait.assert_not_called()

    def test_job_retry_budget_does_not_reset_for_a_new_logical_request(self):
        gateway, cancel = self.gateway(), self.cancellation()
        gateway.model_retries = docker.POLICY["transient_http_retry"]["max_retries_per_job"]
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(502)) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                gateway.relay(self.message(), lambda _: None, cancel)
        factory.assert_called_once()
        cancel.wait.assert_not_called()

    def test_repeated_isolated_502_sequence_recovers_fifth_retry_without_author_restart(self):
        # Only the HTTP status pattern reflects the observed failure. All request,
        # response and tool identifiers below are invented, never a saved transcript.
        # The old job cap stopped at POST 18 despite 22 authoring calls remaining.
        statuses = [200, 200, 200, 200, 200, 502, 200, 200, 200, 200,
                    200, 502, 200, 502, 502, 200, 200, 502, 200]
        for retry_cap, succeeds in ((4, False), (8, True)):
            with self.subTest(retry_cap=retry_cap):
                gateway, cancel, frames = self.gateway(), self.cancellation(), []
                connections = [self.connection(status) for status in statuses]
                logical = 0
                for status, connection in zip(statuses, connections):
                    if status == 200:
                        logical += 1
                        # A synthetic successful response has one unique tool ID.
                        connection.getresponse.return_value.read1.side_effect = [
                            f'data: {{"type":"response.completed","synthetic_tool_id":{logical}}}\n\n'.encode(), b""]
                with mock.patch.dict(docker.POLICY["transient_http_retry"], {"max_retries_per_job": retry_cap}), \
                        mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=connections) as factory, \
                        mock.patch.object(docker.subprocess, "Popen", side_effect=AssertionError("author restart forbidden")):
                    for logical_id in range(1, 15):
                        message = {**self.message(), "id": logical_id}
                        if logical_id == 14 and not succeeds:
                            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                                gateway.relay(message, frames.append, cancel)
                        else:
                            gateway.relay(message, frames.append, cancel)
                expected_posts = 19 if succeeds else 18
                self.assertEqual(factory.call_count, expected_posts)
                self.assertEqual((gateway.requests, gateway.logical_requests, gateway.model_retries),
                                 (expected_posts, 14, 5 if succeeds else 4))
                self.assertEqual([row["http_status"] for row in gateway.request_history], statuses[:expected_posts])
                delivered = [frame for frame in frames if frame["type"] == "response-end"]
                self.assertEqual([frame["id"] for frame in delivered], list(range(1, 15 if succeeds else 14)))
                tools = [json.loads(base64.b64decode(frame["body"]).removeprefix(b"data: "))["synthetic_tool_id"]
                         for frame in frames if frame["type"] == "response-chunk"]
                self.assertEqual(tools, list(range(1, 15 if succeeds else 14)))
                self.assertTrue(all(frame["status"] == 200 for frame in frames if frame["type"] == "response-head"))
                self.assertEqual(gateway.budget_snapshot()["repair_used"], 0)
                self.assertEqual(gateway.budget_snapshot()["repair_remaining"], 8)
                # Recovery stays in this gateway/session: only rejected requests
                # are sent again; no completed tool-output stream is emitted twice.
                self.assertEqual(cancel.wait.call_count, gateway.model_retries)
        self.assertEqual(docker.POLICY["transient_http_retry"]["max_retries_per_job"], 8)

    def test_eighth_job_retry_is_last_even_after_success_and_phase_change(self):
        gateway, cancel, frames = self.gateway(), self.cancellation(), []
        connections = [self.connection(status) for status in ([502, 200] * 8 + [502, 200])]
        with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=connections) as factory:
            for logical_id in range(1, 9):
                gateway.relay({**self.message(), "id": logical_id}, frames.append, cancel)
            self.assertEqual((gateway.requests, gateway.model_retries), (16, 8))
            gateway.set_phase("repair")
            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                gateway.relay({**self.message(), "id": 9}, frames.append, cancel)
        self.assertEqual(factory.call_count, 17)
        self.assertEqual((gateway.requests, gateway.logical_requests, gateway.model_retries), (17, 9, 8))
        self.assertEqual(cancel.wait.call_count, 8)
        self.assertEqual([frame["id"] for frame in frames if frame["type"] == "response-end"], list(range(1, 9)))
        self.assertEqual(gateway.budget_snapshot()["repair_used"], 1)
        connections[-1].request.assert_not_called()

    def test_authoring_budget_wins_over_remaining_transient_retry_allowance(self):
        gateway, cancel = self.gateway(), self.cancellation()
        gateway.requests = gateway.logical_requests = 39
        gateway.phase_requests["authoring"] = 39
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=self.connection(502)) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-upstream-unavailable"):
                gateway.relay(self.message(), lambda _: None, cancel)
        factory.assert_called_once()
        cancel.wait.assert_not_called()
        self.assertEqual(gateway.model_retries, 0)
        self.assertEqual(gateway.budget_snapshot()["authoring_remaining"], 0)
        self.assertEqual(gateway.budget_snapshot()["repair_remaining"], 8)

    def test_partial_stream_is_not_replayed(self):
        gateway, connection, frames = self.gateway(), self.connection(200), []
        connection.getresponse.return_value.read1.side_effect = [b"partial-tool-data", docker.http.client.IncompleteRead(b"private")]
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-network-error"):
                gateway.relay(self.message(), frames.append, threading.Event())
        factory.assert_called_once()
        self.assertEqual(gateway.model_retries, 0)
        self.assertEqual([f["type"] for f in frames], ["response-head", "response-chunk"])
        self.assertEqual(gateway.request_history[0]["transport_error"], "http-framing")

    def test_content_length_eof_is_not_a_success_or_replayed(self):
        gateway, connection, frames = self.gateway(), self.connection(200), []
        connection.getresponse.return_value.length = 12
        connection.getresponse.return_value.read1.side_effect = [b"partial", b""]
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-network-error"):
                gateway.relay(self.message(), frames.append, threading.Event())
        factory.assert_called_once()
        self.assertNotIn("response-end", [frame["type"] for frame in frames])
        self.assertEqual(gateway.last_request["transport_error"], "http-framing")

    def test_cancel_during_forwarding_never_emits_complete(self):
        gateway, connection, frames = self.gateway(), self.connection(200), []
        cancelled = threading.Event()
        def emit(frame):
            frames.append(frame)
            cancelled.set()
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaisesRegex(docker.DockerError, "provider-cancelled"):
                gateway.relay(self.message(), emit, cancelled)
        factory.assert_called_once()
        self.assertEqual([frame["type"] for frame in frames], ["response-head"])
        self.assertEqual(gateway.last_error, "provider-cancelled")

    def test_real_loopback_http_rejection_then_complete_stream(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                payload = b"synthetic rejected response must not reach author" if len(received) == 1 else b'data: {"type":"response.completed"}\n\n'
                self.send_response(502 if len(received) == 1 else 200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        gateway, frames = self.gateway(), []
        try:
            # Replace only the TLS connection factory with a test-owned loopback
            # HTTP socket. Production still uses exact origin TLS/certificate checks.
            with mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=lambda *a, **k:
                    docker.http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)), \
                    mock.patch.object(gateway, "retry_delay", return_value=0):
                gateway.relay(self.message(), frames.append, threading.Event())
            self.assertEqual(len(received), 2)
            bodies = [json.loads(body) for body in received]
            self.assertIn("used: 1 total", bodies[0].pop("instructions"))
            self.assertIn("used: 2 total", bodies[1].pop("instructions"))
            self.assertEqual(*bodies)
            self.assertEqual(gateway.requests, 2)
            self.assertEqual(gateway.logical_requests, 1)
            self.assertEqual(gateway.model_retries, 1)
            body = b"".join(base64.b64decode(frame["body"]) for frame in frames if frame["type"] == "response-chunk")
            self.assertNotIn(b"rejected", body)
            self.assertIn(b"response.completed", body)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
