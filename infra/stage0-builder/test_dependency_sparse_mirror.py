#!/usr/bin/env python3
"""Offline adversarial checks for dependency_sparse_mirror.py."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import pathlib
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("dependency_sparse_mirror.py")
SPEC = importlib.util.spec_from_file_location("dependency_sparse_mirror", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load dependency sparse mirror")
MIRROR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MIRROR
SPEC.loader.exec_module(MIRROR)


class StaticResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: list[tuple[str, str]] | None = None,
        chunks: list[bytes | BaseException] | None = None,
    ) -> None:
        self.status = status
        self._headers = headers if headers is not None else [("Content-Length", "3")]
        self._chunks = list(chunks if chunks is not None else [b"abc", b""])

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers

    def read(self, amount: int) -> bytes:
        del amount
        value = self._chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class StaticConnection:
    responses: list[StaticResponse] = []
    calls: list[tuple[object, ...]] = []
    constructor_error: BaseException | None = None
    close_error: BaseException | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))
        if self.constructor_error is not None:
            raise self.constructor_error

    def request(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))

    def getresponse(self) -> StaticResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error


def new_store(directory: str) -> object:
    root = pathlib.Path(directory).resolve() / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return MIRROR.EvidenceStore(root)


def unseal(store: object) -> None:
    os.chmod(store.root, 0o700)
    os.chmod(store.responses, 0o700)


class SparseMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_connection = MIRROR.http.client.HTTPSConnection
        StaticConnection.calls = []
        StaticConnection.responses = []
        StaticConnection.constructor_error = None
        StaticConnection.close_error = None
        MIRROR.http.client.HTTPSConnection = StaticConnection

    def tearDown(self) -> None:
        MIRROR.http.client.HTTPSConnection = self.original_connection

    def test_canonical_sparse_paths(self) -> None:
        valid = [
            "/config.json",
            "/1/a",
            "/2/ab",
            "/3/a/abc",
            "/wa/sm/wasmtime",
            "/wi/t-/wit-bindgen",
        ]
        for value in valid:
            self.assertEqual(MIRROR.canonical_sparse_path(value), value)
        invalid = [
            "/",
            "//wa/sm/wasmtime",
            "/WA/SM/wasmtime",
            "/wa/sm/Wasmtime",
            "/wa/sm/other",
            "/wa/sm/wasmtime?q=1",
            "/%77a/sm/wasmtime",
            "/wa/sm/wasmtime/",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(MIRROR.MirrorFailure):
                    MIRROR.canonical_sparse_path(value)

    def test_one_upstream_get_per_unique_path(self) -> None:
        StaticConnection.responses = [StaticResponse()]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
            first = state.fetch("/wa/sm/wasmtime")
            second = state.fetch("/wa/sm/wasmtime")
            self.assertEqual(first, second)
            request_calls = [call for call in StaticConnection.calls if call[0][:1] == ("GET",)]
            self.assertEqual(len(request_calls), 1)
            request_headers = request_calls[0][1]["headers"]
            self.assertNotIn("Authorization", request_headers)
            self.assertNotIn("Cookie", request_headers)
            store.finalize("completed", True)
            summary = json.loads((store.root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["unique_upstream_gets"], 1)
            self.assertEqual(summary["paths"], ["/wa/sm/wasmtime"])
            self.assertEqual(stat.S_IMODE(os.lstat(store.root).st_mode), 0o500)
            self.assertEqual(stat.S_IMODE(os.lstat(store.responses).st_mode), 0o500)
            unseal(store)

    def test_config_is_strict_and_cargo_receives_no_external_authority(self) -> None:
        accepted = b'{"dl":"https://static.crates.io/crates","api":"https://crates.io"}\n'
        StaticConnection.responses = [
            StaticResponse(headers=[("Content-Length", str(len(accepted)))], chunks=[accepted, b""])
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
            retained = state.fetch("/config.json")
            value = json.loads(retained.cargo_body)
            self.assertEqual(set(value), {"auth-required", "dl"})
            self.assertFalse(value["auth-required"])
            self.assertEqual(
                value["dl"],
                "http://dependency-index-mirror:18080/__archives_forbidden__/{crate}/{version}",
            )
            self.assertNotIn(b"static.crates.io", retained.cargo_body)
            self.assertNotEqual(retained.upstream_body, retained.cargo_body)
            self.assertEqual(len(list(store.responses.glob("*.cargo-body"))), 1)
            self.assertEqual(len(list(store.responses.glob("*.cargo.json"))), 1)

        invalid = [
            b'{"dl":"https://evil.invalid/crates","api":"https://crates.io"}',
            b'{"dl":"https://static.crates.io/crates","api":"https://evil.invalid"}',
            b'{"dl":"https://static.crates.io/crates","api":"https://crates.io","auth-required":true}',
            b'{"dl":"https://static.crates.io/crates","dl":"https://evil.invalid","api":"https://crates.io"}',
        ]
        for body in invalid:
            with self.subTest(body=body):
                StaticConnection.responses = [
                    StaticResponse(headers=[("Content-Length", str(len(body)))], chunks=[body, b""])
                ]
                with tempfile.TemporaryDirectory() as directory:
                    store = new_store(directory)
                    state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
                    with self.assertRaises(MIRROR.MirrorFailure):
                        state.fetch("/config.json")
                    self.assertIsNotNone(state.fatal)
                    self.assertEqual(len(list(store.responses.glob("*.body"))), 1)

    def test_response_framing_cookie_and_encoding_fail_after_retention(self) -> None:
        cases = [
            StaticResponse(headers=[]),
            StaticResponse(headers=[("Content-Length", "3"), ("Content-Length", "3")]),
            StaticResponse(headers=[("Content-Length", "3"), ("Transfer-Encoding", "chunked")]),
            StaticResponse(headers=[("Transfer-Encoding", "gzip")]),
            StaticResponse(headers=[("Content-Length", "3"), ("Set-Cookie", "secret=x")]),
            StaticResponse(headers=[("Content-Length", "3"), ("Content-Encoding", "gzip")]),
            StaticResponse(status=302, headers=[("Content-Length", "3"), ("Location", "/elsewhere")]),
        ]
        for response in cases:
            with self.subTest(status=response.status, headers=response.getheaders()):
                StaticConnection.responses = [response]
                with tempfile.TemporaryDirectory() as directory:
                    store = new_store(directory)
                    state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
                    with self.assertRaises(MIRROR.MirrorFailure):
                        state.fetch("/wa/sm/wasmtime")
                    self.assertIsNotNone(state.fatal)
                    self.assertEqual(len(list(store.responses.glob("*.body"))), 1)

    def test_incomplete_and_oversize_bodies_retain_bounded_evidence(self) -> None:
        error = http.client.IncompleteRead(partial=b"partial", expected=12)
        StaticConnection.responses = [StaticResponse(headers=[], chunks=[b"prefix-", error])]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
            with self.assertRaisesRegex(MIRROR.MirrorFailure, "response incomplete"):
                state.fetch("/wa/sm/wasmtime")
            body = next(store.responses.glob("*.body")).read_bytes()
            self.assertEqual(body, b"prefix-partial")
            metadata = json.loads(next(store.responses.glob("*.json")).read_text())
            self.assertFalse(metadata["response_complete"])
            self.assertFalse(metadata["body_truncated"])

        StaticConnection.responses = [StaticResponse(headers=[], chunks=[b"123456789", b""])]
        with mock.patch.object(MIRROR, "MAX_BODY_BYTES", 8):
            with tempfile.TemporaryDirectory() as directory:
                store = new_store(directory)
                state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
                with self.assertRaisesRegex(MIRROR.MirrorFailure, "size-limit"):
                    state.fetch("/wa/sm/wasmtime")
                body = next(store.responses.glob("*.body")).read_bytes()
                metadata = json.loads(next(store.responses.glob("*.json")).read_text())
                self.assertEqual(body, b"12345678")
                self.assertTrue(metadata["body_truncated"])
                self.assertEqual(metadata["received_bytes_at_least"], 9)

    def test_constructor_and_close_errors_are_fatal_and_retained(self) -> None:
        StaticConnection.constructor_error = OSError("constructor")
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
            with self.assertRaises(MIRROR.MirrorFailure):
                state.fetch("/wa/sm/wasmtime")
            self.assertIsNotNone(state.fatal)
            self.assertTrue((store.root / "failure.json").is_file())
            self.assertEqual(len(list(store.responses.glob("*.body"))), 1)
            calls_after_failure = list(StaticConnection.calls)
            with self.assertRaises(MIRROR.MirrorFailure):
                state.fetch("/wa/sm/wasmtime")
            self.assertEqual(StaticConnection.calls, calls_after_failure)

        StaticConnection.constructor_error = None
        StaticConnection.close_error = OSError("close")
        StaticConnection.responses = [StaticResponse()]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(store, "dependency-index-mirror", 18080)
            with self.assertRaisesRegex(MIRROR.MirrorFailure, "close-OSError"):
                state.fetch("/wa/sm/wasmtime")
            self.assertIsNotNone(state.fatal)

    def test_attempt_wide_path_and_body_budgets_fail_closed(self) -> None:
        StaticConnection.responses = [StaticResponse()]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(
                store,
                "dependency-index-mirror",
                18080,
                max_unique_paths=1,
                max_total_retained_bytes=64,
            )
            state.fetch("/wa/sm/wasmtime")
            calls_before_budget = list(StaticConnection.calls)
            with self.assertRaisesRegex(MIRROR.MirrorFailure, "path budget"):
                state.fetch("/wi/t-/wit-bindgen")
            self.assertEqual(StaticConnection.calls, calls_before_budget)
            self.assertIsNotNone(state.fatal)

        StaticConnection.responses = [StaticResponse()]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(
                store,
                "dependency-index-mirror",
                18080,
                max_unique_paths=2,
                max_total_retained_bytes=3,
            )
            state.fetch("/wa/sm/wasmtime")
            calls_before_budget = list(StaticConnection.calls)
            with self.assertRaisesRegex(MIRROR.MirrorFailure, "retained-body budget"):
                state.fetch("/wi/t-/wit-bindgen")
            self.assertEqual(StaticConnection.calls, calls_before_budget)

        StaticConnection.responses = [StaticResponse()]
        with tempfile.TemporaryDirectory() as directory:
            store = new_store(directory)
            state = MIRROR.MirrorState(
                store,
                "dependency-index-mirror",
                18080,
                max_unique_paths=1,
                max_total_retained_bytes=2,
            )
            with self.assertRaisesRegex(MIRROR.MirrorFailure, "aggregate-body-size"):
                state.fetch("/wa/sm/wasmtime")
            body = next(store.responses.glob("*.body")).read_bytes()
            metadata = json.loads(next(store.responses.glob("*.json")).read_text())
            self.assertEqual(body, b"ab")
            self.assertEqual(metadata["received_bytes_at_least"], 3)
            self.assertTrue(metadata["body_truncated"])
            self.assertTrue((store.root / "failure.json").is_file())
            calls_after_failure = list(StaticConnection.calls)
            with self.assertRaises(MIRROR.MirrorFailure):
                state.fetch("/wa/sm/wasmtime")
            self.assertEqual(StaticConnection.calls, calls_after_failure)

    def test_all_non_get_methods_and_ambiguous_headers_are_globally_fatal(self) -> None:
        requests = [
            f"{method} /config.json HTTP/1.1\r\nHost: mirror:PORT\r\n\r\n"
            for method in ("HEAD", "POST", "PUT", "DELETE", "OPTIONS", "CONNECT", "PATCH", "TRACE", "CUSTOM")
        ]
        requests.extend(
            [
                "GET /config.json HTTP/1.1\r\nHost: mirror:PORT\r\nHost: mirror:PORT\r\n\r\n",
                "GET /config.json HTTP/1.1\r\nHost: mirror:PORT\r\nAuthorization:\r\n\r\n",
                "GET /config.json HTTP/1.1\r\nHost: mirror:PORT\r\nProxy-Authorization:\r\n\r\n",
                "GET /config.json HTTP/1.1\r\nHost: mirror:PORT\r\nCookie:\r\n\r\n",
                "GET /config.json HTTP/1.1\r\nHost: mirror:PORT\r\nContent-Length: 0\r\n\r\n",
                "GET /config.json HTTP/1.1\r\nHost: mirror:PORT\r\nTransfer-Encoding: chunked\r\n\r\n",
            ]
        )
        for template in requests:
            with self.subTest(request=template.split("\r\n", 1)[0]):
                with tempfile.TemporaryDirectory() as directory:
                    store = new_store(directory)
                    state = MIRROR.MirrorState(store, "mirror", 1)
                    server = MIRROR.SparseMirrorServer(("127.0.0.1", 0), state)
                    state.port = server.server_port
                    state.shutdown_callback = server.shutdown
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    request = template.replace("PORT", str(server.server_port)).encode("ascii")
                    with socket.create_connection(server.server_address, timeout=2) as connection:
                        connection.sendall(request)
                        connection.shutdown(socket.SHUT_WR)
                        connection.recv(4096)
                    thread.join(timeout=2)
                    server.server_close()
                    self.assertFalse(thread.is_alive())
                    self.assertIsNotNone(state.fatal)
                    self.assertTrue((store.root / "failure.json").is_file())
                    self.assertEqual(StaticConnection.calls, [])

    def test_completion_file_requires_root_owned_exact_read_only_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory).resolve() / "completion"
            path.write_bytes(MIRROR.COMPLETION_BYTES)
            real = os.lstat(path)
            accepted = types.SimpleNamespace(
                st_mode=stat.S_IFREG | 0o444,
                st_uid=0,
                st_gid=0,
            )
            with mock.patch.object(MIRROR.os, "fstat", return_value=accepted):
                self.assertTrue(MIRROR.valid_completion_file(path))
            for changed in (
                types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o444, st_uid=0, st_gid=0),
                types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0),
                types.SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=real.st_uid, st_gid=real.st_gid),
            ):
                with mock.patch.object(MIRROR.os, "fstat", return_value=changed):
                    self.assertFalse(MIRROR.valid_completion_file(path))
            path.write_bytes(b"wrong\n")
            with mock.patch.object(MIRROR.os, "fstat", return_value=accepted):
                self.assertFalse(MIRROR.valid_completion_file(path))

    def test_completion_special_nodes_never_block_and_descriptor_wins_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            fifo = root / "completion-fifo"
            os.mkfifo(fifo)
            started = time.monotonic()
            self.assertFalse(MIRROR.valid_completion_file(fifo))
            self.assertLess(time.monotonic() - started, 0.5)

            completion_directory = root / "completion-directory"
            completion_directory.mkdir()
            self.assertFalse(MIRROR.valid_completion_file(completion_directory))

            target = root / "completion-target"
            target.write_bytes(MIRROR.COMPLETION_BYTES)
            symlink = root / "completion-symlink"
            symlink.symlink_to(target)
            self.assertFalse(MIRROR.valid_completion_file(symlink))

            unix_socket_path = root / "completion-socket"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_socket:
                unix_socket.bind(os.fspath(unix_socket_path))
                self.assertFalse(MIRROR.valid_completion_file(unix_socket_path))

            if pathlib.Path("/dev/null").exists():
                self.assertFalse(MIRROR.valid_completion_file(pathlib.Path("/dev/null")))

            accepted = types.SimpleNamespace(
                st_mode=stat.S_IFREG | 0o444,
                st_uid=0,
                st_gid=0,
            )
            race_path = root / "completion-race"
            race_path.write_bytes(b"wrong\n")
            opened_path = root / "completion-race-opened"
            real_open = os.open

            def open_then_replace(path: object, flags: int, *args: object, **kwargs: object) -> int:
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == race_path.name:
                    race_path.rename(opened_path)
                    race_path.write_bytes(MIRROR.COMPLETION_BYTES)
                return descriptor

            with (
                mock.patch.object(MIRROR.os, "open", side_effect=open_then_replace),
                mock.patch.object(MIRROR.os, "fstat", return_value=accepted),
            ):
                self.assertFalse(MIRROR.valid_completion_file(race_path))

            for body in (b"short\n", MIRROR.COMPLETION_BYTES + b"extra"):
                target.write_bytes(body)
                with mock.patch.object(MIRROR.os, "fstat", return_value=accepted):
                    self.assertFalse(MIRROR.valid_completion_file(target))

    def test_fifo_sigterm_exits_nonzero_and_seals_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            evidence = root / "evidence"
            evidence.mkdir(mode=0o700)
            os.chmod(evidence, 0o700)
            completion = root / "completion"
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            process = subprocess.Popen(
                [
                    sys.executable,
                    os.fspath(MODULE_PATH),
                    "--evidence-root",
                    os.fspath(evidence),
                    "--completion-file",
                    os.fspath(completion),
                    "--registry-host",
                    "dependency-index-mirror",
                    "--port",
                    str(port),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 3
                while not (evidence / "ready.json").is_file():
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(f"mirror exited before readiness: {stdout!r} {stderr!r}")
                    if time.monotonic() >= deadline:
                        self.fail("mirror did not become ready")
                    time.sleep(0.01)
                os.mkfifo(completion)
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=3)
                stdout, stderr = process.communicate(timeout=1)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "")
                summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["outcome"], "fail-closed")
                self.assertFalse(summary["supervisor_completion_seen"])
                self.assertTrue((evidence / "failure.json").is_file())
                self.assertEqual(stat.S_IMODE(os.lstat(evidence).st_mode), 0o500)
                self.assertEqual(
                    stat.S_IMODE(os.lstat(evidence / "responses").st_mode), 0o500
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                if completion.exists():
                    completion.unlink()
                if (evidence / "responses").exists():
                    os.chmod(evidence / "responses", 0o700)
                if evidence.exists():
                    os.chmod(evidence, 0o700)

    def test_evidence_rejects_symlink_ancestors_and_uses_retained_fds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            real_parent = base / "real-parent"
            real_parent.mkdir()
            evidence = real_parent / "evidence"
            evidence.mkdir(mode=0o700)
            alias = base / "alias"
            alias.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(MIRROR.MirrorFailure):
                MIRROR.EvidenceStore(alias / "evidence")

            completion = real_parent / "completion"
            completion.write_bytes(MIRROR.COMPLETION_BYTES)
            self.assertFalse(MIRROR.valid_completion_file(alias / "completion"))
            with self.assertRaises(MIRROR.MirrorFailure):
                MIRROR.completion_file_is_absent(alias / "missing-completion")

        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            parent = base / "parent"
            parent.mkdir()
            evidence = parent / "evidence"
            evidence.mkdir(mode=0o700)
            store = MIRROR.EvidenceStore(evidence)
            moved_parent = base / "moved-parent"
            parent.rename(moved_parent)
            attacker_parent = base / "attacker-parent"
            attacker_evidence = attacker_parent / "evidence"
            attacker_evidence.mkdir(parents=True, mode=0o700)
            parent.symlink_to(attacker_parent, target_is_directory=True)
            store.write_control("race-proof.json", {"result": "retained-fd"})
            self.assertTrue((moved_parent / "evidence" / "race-proof.json").is_file())
            self.assertFalse((attacker_evidence / "race-proof.json").exists())
            store.finalize("fail-closed", False)
            os.chmod(moved_parent / "evidence" / "responses", 0o700)
            os.chmod(moved_parent / "evidence", 0o700)


if __name__ == "__main__":
    unittest.main()
