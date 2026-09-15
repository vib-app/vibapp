from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_executor as docker


class DockerBoundaryTests(unittest.TestCase):
    def test_codex_thread_defaults_cover_wrapper_tools_and_trusted_relay(self):
        root = Path(docker.__file__).resolve().parent / "docker"
        bridge = (root / "entry.mjs").read_text()
        defaults = re.search(r"const THREAD_ENV = Object.freeze\(\{(.*?)\}\);", bridge, re.S).group(1)
        self.assertEqual(dict(re.findall(r"([A-Z_]+): '([^']+)'", defaults)), {
            "NODE_OPTIONS": "--v8-pool-size=2", "UV_THREADPOOL_SIZE": "2",
            "TOKIO_WORKER_THREADS": "2", "RAYON_NUM_THREADS": "2"})
        self.assertIn("...THREAD_ENV", bridge)
        self.assertIn("Object.entries(CODEX_ENV)", bridge)
        self.assertIn("env: provider ? provider.env() : { ...CODEX_ENV", bridge)
        self.assertIn("shell_environment_policy.inherit=\"none\"", bridge)
        self.assertIn('ENV NODE_OPTIONS="--v8-pool-size=2" UV_THREADPOOL_SIZE="2"', (root / "Dockerfile").read_text())

    def image_probe(self, *, provider="codex", stale=False, version=None):
        bridge_root = Path(docker.__file__).resolve().parent / "docker"
        bridge = (bridge_root / "entry.mjs").read_bytes()
        if provider == "opencode":
            bridge += (bridge_root / "opencode-provider.mjs").read_bytes()
        identity = {"version": version or ("codex-cli 0.153.4" if provider == "codex" else "1.18.27"),
                    "bundle_sha256": "b" * 64, "bridge_sha256": "c" * 64 if stale else docker.digest(bridge)}
        if provider == "opencode":
            identity["relay_policy"] = docker.LOCALAI_RELAY_POLICY
        return [mock.Mock(returncode=0, stdout=("sha256:" + "a" * 64).encode()),
                mock.Mock(returncode=0, stdout=docker.canonical(identity))]

    def test_stale_image_bridge_rejects_before_model_or_credentials(self):
        for provider in ("codex", "opencode"):
            with self.subTest(provider=provider), mock.patch.object(docker, "docker_command", side_effect=self.image_probe(provider=provider, stale=True)) as command, mock.patch.object(docker, "codex_connection", side_effect=AssertionError("credential access")), mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=AssertionError("model access")):
                with self.assertRaises(docker.DockerError) as error:
                    docker.image_identity(provider=provider)
                self.assertEqual(error.exception.code, "docker-image-stale")
                self.assertEqual(command.call_count, 2)

    def test_current_bridge_accepts_codex_version_without_new_version_pin(self):
        for version in ("codex-cli 0.153.4", "codex-cli 0.199.0"):
            with mock.patch.object(docker, "docker_command", side_effect=self.image_probe(version=version)):
                self.assertEqual(docker.image_identity()["version"], version)

    def test_opencode_bridge_identity_covers_both_trusted_modules(self):
        with mock.patch.object(docker, "docker_command", side_effect=self.image_probe(provider="opencode")):
            identity = docker.image_identity(provider="opencode")
        self.assertEqual(identity["version"], "1.18.27")
        self.assertEqual(identity["policy_sha256"], docker.digest(docker.canonical(docker.OPENCODE_POLICY)))

    def test_transient_retry_policy_change_rebinds_provider_identity_without_credentials(self):
        identities = []
        for retry_cap in (4, 8):
            with mock.patch.dict(docker.POLICY["transient_http_retry"], {"max_retries_per_job": retry_cap}), \
                    mock.patch.object(docker, "docker_command", side_effect=self.image_probe()), \
                    mock.patch.object(docker, "codex_connection", side_effect=AssertionError("credential access")):
                identities.append(docker.image_identity())
        self.assertNotEqual(identities[0]["policy_sha256"], identities[1]["policy_sha256"])
        self.assertEqual({key: value for key, value in identities[0].items() if key != "policy_sha256"},
                         {key: value for key, value in identities[1].items() if key != "policy_sha256"})
        self.assertEqual(docker.POLICY["max_model_requests"], 48)
        self.assertEqual(docker.POLICY["model_request_budget"], {"total": 48, "authoring": 40, "repair": 8})
        self.assertEqual(docker.POLICY["transient_http_retry"]["max_attempts_per_request"], 3)
        self.assertFalse(docker.POLICY["transient_http_retry"]["uncertain_send_or_stream_replay"])

    def test_only_host_configured_loopback_unauthenticated_proxy_is_accepted(self):
        with mock.patch.object(docker, "getproxies", return_value={"https": "http://127.0.0.1:6152"}):
            self.assertEqual(docker.codex_https_proxy(), {"host": "127.0.0.1", "port": 6152})
        with mock.patch.object(docker, "getproxies", return_value={}):
            self.assertIsNone(docker.codex_https_proxy())
        for value in ("http://remote.example:6152", "http://u:secret@127.0.0.1:6152", "socks5://127.0.0.1:6152", "http://127.0.0.1:bad", "http://127.0.0.1:6152/path"):
            with mock.patch.object(docker, "getproxies", return_value={"https": value}):
                with self.assertRaisesRegex(docker.DockerError, "Only an existing"):
                    docker.codex_https_proxy()

    def test_custom_connection_metadata_never_returns_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('model_provider="custom"\n[model_providers.custom]\nbase_url="https://example.test/v1"\nwire_api="responses"\nexperimental_bearer_token="unit-secret-never-export"\n')
            config.chmod(0o600)
            with mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
                public, secret = docker.codex_connection()
                self.assertIsNone(secret)
                self.assertNotIn("unit-secret", json.dumps(public))
                self.assertEqual(public["endpoint"], "https://example.test/v1")
                self.assertEqual(docker.codex_connection(include_secret=True)[1], "unit-secret-never-export")
                config.chmod(0o666)
                with self.assertRaisesRegex(docker.DockerError, "codex-config-invalid"):
                    docker.codex_connection()

    def test_custom_connection_rejects_non_https_redirectable_or_inline_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            for endpoint in ["http://example.test/v1", "https://u:p@example.test/v1", "https://example.test/v1?q=secret", "https://example.test/v1#fragment"]:
                config.write_text(f'model_provider="custom"\n[model_providers.custom]\nbase_url="{endpoint}"\nexperimental_bearer_token="unit-test"\n')
                config.chmod(0o600)
                with mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
                    with self.assertRaisesRegex(docker.DockerError, "exact HTTPS"):
                        docker.codex_connection()

    def gateway(self):
        with mock.patch.object(docker, "codex_connection", return_value=({"kind": "https", "endpoint": "https://example.test/v1"}, "secret-only-on-host")):
            return docker.OpenAIGateway("bound-model")

    def message(self, body):
        return {"id": 1, "body": base64.b64encode(json.dumps(body).encode()).decode()}

    def test_wrong_model_or_non_streaming_request_never_reaches_upstream(self):
        for body in [{"model": "other", "stream": True}, {"model": "bound-model", "stream": False}]:
            gateway = self.gateway()
            with mock.patch.object(docker.http.client, "HTTPSConnection") as connection:
                with self.assertRaisesRegex(docker.DockerError, "provider-relay-request-invalid"):
                    gateway.relay(self.message(body), lambda _: None, threading.Event())
                connection.assert_not_called()

    def test_destination_and_headers_are_host_owned_and_response_is_streamed(self):
        gateway = self.gateway()
        response = mock.Mock(status=200)
        response.read1.side_effect = [b"data: test\n\n", b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        frames = []
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection) as factory:
            gateway.relay(self.message({"model": "bound-model", "stream": True, "max_output_tokens": 100000}), frames.append, threading.Event())
        factory.assert_called_once_with("example.test", port=None, timeout=15)
        connection.sock.settimeout.assert_called_once_with(300)
        self.assertEqual(connection.auto_open, 0)
        call = connection.request.call_args
        self.assertEqual(call.args, ("POST", "/v1/responses"))
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer secret-only-on-host")
        self.assertEqual(json.loads(call.kwargs["body"])["max_output_tokens"], 8192)
        self.assertNotIn("secret-only-on-host", json.dumps(frames))
        self.assertEqual([frame["type"] for frame in frames], ["response-head", "response-chunk", "response-end"])
        self.assertTrue(gateway.observed)
        connection.close.assert_called_once()

    def test_transport_diagnostics_are_bounded_and_never_keep_stale_http_status(self):
        for error, code, category in [
            (TimeoutError("secret-in-exception"), "provider-timeout", "timeout"),
            (ConnectionResetError("secret-in-exception"), "provider-network-error", "connection"),
            (docker.http.client.IncompleteRead(b"secret-in-exception"), "provider-network-error", "http-framing"),
        ]:
            with self.subTest(category=category):
                gateway = self.gateway()
                gateway.last_status = 200
                connection = mock.Mock()
                connection.getresponse.side_effect = error
                with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaisesRegex(docker.DockerError, code):
                        gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, threading.Event())
                self.assertIsNone(gateway.last_status)
                self.assertEqual(gateway.last_error, code)
                self.assertEqual(gateway.last_request["phase"], "response-headers")
                self.assertEqual(gateway.last_request["transport_error"], category)
                self.assertNotIn("secret", json.dumps(gateway.last_request))
                connection.close.assert_called_once()

    def test_body_timeout_records_received_bytes_without_response_content(self):
        gateway = self.gateway()
        response = mock.Mock(status=200)
        response.read1.side_effect = [b"private-output", TimeoutError("private-error")]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(docker.DockerError, "provider-timeout"):
                gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, threading.Event())
        self.assertEqual(gateway.last_request["bytes"], 14)
        self.assertEqual(gateway.last_request["phase"], "response-body")
        self.assertEqual(gateway.last_status, 200)
        self.assertNotIn("private", json.dumps(gateway.last_request))

    def test_cancel_shutdown_reaches_socket_detached_by_connection_close_response(self):
        gateway = self.gateway()
        cancelled = threading.Event()
        connection = mock.Mock()
        raw_socket = connection.sock
        response = mock.Mock(status=200)
        def detach():
            connection.sock = None
            return response
        def cancel_reader(_):
            cancelled.set()
            gateway.close()
            raise OSError("cancelled read")
        connection.getresponse.side_effect = detach
        response.read1.side_effect = cancel_reader
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(docker.DockerError, "provider-cancelled"):
                gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, cancelled)
        raw_socket.shutdown.assert_called_once_with(2)
        self.assertIsNone(gateway.response_socket)

    def test_only_connect_failures_retry_before_exactly_one_model_post(self):
        gateway = self.gateway()
        connection = mock.Mock()
        connection.connect.side_effect = [TimeoutError(), None]
        response = mock.Mock(status=200)
        response.read1.return_value = b""
        connection.getresponse.return_value = response
        cancelled = mock.Mock()
        cancelled.is_set.return_value = cancelled.wait.return_value = False
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, cancelled)
        self.assertEqual(connection.connect.call_count, 2)
        connection.request.assert_called_once()
        self.assertEqual(gateway.last_request["connect_attempts"], 2)

    def test_sent_request_failure_is_not_replayed(self):
        gateway = self.gateway()
        connection = mock.Mock()
        connection.request.side_effect = ConnectionResetError()
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(docker.DockerError, "provider-network-error"):
                gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, threading.Event())
        connection.connect.assert_called_once()
        connection.request.assert_called_once()
        self.assertEqual(gateway.last_request["phase"], "send-request")

    def test_cancel_after_explicit_connect_cannot_reopen_or_send_model_post(self):
        gateway = self.gateway()
        cancelled = threading.Event()
        connection = mock.Mock()
        original = gateway.connect
        def cancel_after_connect(*args):
            original(*args)
            cancelled.set()
            gateway.close()
            connection.sock = None
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection), mock.patch.object(gateway, "connect", side_effect=cancel_after_connect):
            with self.assertRaisesRegex(docker.DockerError, "provider-cancelled"):
                gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, cancelled)
        self.assertEqual(connection.auto_open, 0)
        connection.request.assert_not_called()
        connection.connect.assert_called_once()

    def test_request_budget_is_hard_limit(self):
        gateway = self.gateway()
        gateway.requests = docker.MAX_REQUESTS
        with self.assertRaisesRegex(docker.DockerError, "provider-request-budget-exhausted"):
            gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, threading.Event())

    def test_local_proxy_uses_origin_tls_tunnel_without_auth_in_connect(self):
        gateway = self.gateway()
        gateway.https_proxy = {"host": "127.0.0.1", "port": 6152}
        connection = mock.Mock()
        response = mock.Mock(status=200)
        response.read1.return_value = b""
        connection.getresponse.return_value = response
        with mock.patch.object(docker.http.client, "HTTPSConnection", return_value=connection) as factory:
            gateway.relay(self.message({"model": "bound-model", "stream": True}), lambda _: None, threading.Event())
        factory.assert_called_once_with("127.0.0.1", port=6152, timeout=15)
        connection.set_tunnel.assert_called_once_with("example.test", port=443)
        self.assertEqual(connection.request.call_args.args, ("POST", "/v1/responses"))

    def executor(self):
        return docker.DockerExecutor(image_id="sha256:" + "a" * 64, input_payload={}, gateway=mock.Mock(), limits={}, state_root=Path("/unused"))

    def test_mutable_image_tags_cannot_execute(self):
        with self.assertRaisesRegex(docker.ContractError, "immutable"):
            docker.DockerExecutor(image_id="codex:latest", input_payload={}, gateway=mock.Mock(), limits={}, state_root=Path("/unused"))

    def test_cleanup_never_removes_an_unrelated_container(self):
        executor = self.executor()
        with mock.patch.object(executor, "_inspect", return_value={"Id": "container-id", "Config": {"Labels": {"ai.vibapp.execution": "someone-else"}}}), mock.patch.object(docker, "docker_command") as command:
            with self.assertRaisesRegex(docker.DockerError, "identity-mismatch"):
                executor._cleanup("name", "our-execution")
            command.assert_not_called()

    def test_cleanup_requires_exact_id_and_confirmed_absence(self):
        executor = self.executor()
        container = {"Id": "exact-resolved-id", "Image": executor.image_id, "Config": {"Labels": {
            "ai.vibapp.execution": "ours", "ai.vibapp.owner": str(os.getuid()), "ai.vibapp.role": "codeagent"}}}
        with mock.patch.object(executor, "_inspect", side_effect=[container, None]), mock.patch.object(docker, "docker_command", return_value=mock.Mock(returncode=0)) as command:
            self.assertTrue(executor._cleanup("validated-name", "ours"))
            command.assert_called_once_with(["container", "rm", "--force", "exact-resolved-id"])

    def test_cleanup_requires_owner_role_and_immutable_image(self):
        executor = self.executor()
        for key, value in [("ai.vibapp.owner", "different-user"), ("ai.vibapp.role", "builder"), ("Image", "sha256:" + "b" * 64)]:
            container = {"Id": "exact-id", "Image": executor.image_id, "Config": {"Labels": {
                "ai.vibapp.execution": "ours", "ai.vibapp.owner": str(os.getuid()), "ai.vibapp.role": "codeagent"}}}
            if key == "Image":
                container[key] = value
            else:
                container["Config"]["Labels"][key] = value
            with mock.patch.object(executor, "_inspect", return_value=container), mock.patch.object(docker, "docker_command") as command:
                with self.assertRaisesRegex(docker.DockerError, "identity-mismatch"):
                    executor._cleanup("name", "ours")
                command.assert_not_called()

    def test_recovery_cancels_exact_reserved_job_and_never_relaunches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execution = "vibapp-" + "b" * 64
            name = "vibapp-ca-" + docker.digest(execution.encode())[:32]
            (root / "docker-job.json").write_text(json.dumps({"backend_execution_id": execution, "container_name": name, "image_id": "sha256:" + "a" * 64}))
            with mock.patch.object(docker.DockerExecutor, "_cleanup", return_value=True) as cleanup, mock.patch.object(docker.DockerExecutor, "launch") as launch:
                docker.recover_job(root)
            cleanup.assert_called_once_with(name, execution)
            launch.assert_not_called()
            self.assertFalse(json.loads((root / "docker-recovery.json").read_bytes())["resubmitted"])


if __name__ == "__main__":
    unittest.main()
