#!/usr/bin/env python3
"""Fail-closed sparse-index broker for Stage 0 dependency lock resolution.

This is not a general proxy. A separately reviewed supervisor places Cargo on an
egress-free internal container network and gives it only this broker address. The
broker alone is dual-homed and performs at most one anonymous HTTPS GET for each
canonical crates.io sparse-index path. Crate archives are impossible in this phase.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import json
import os
import re
import signal
import ssl
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit


UPSTREAM_HOST = "index.crates.io"
UPSTREAM_PORT = 443
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_REGISTRY_HOST = "dependency-index-mirror"
DEFAULT_PORT = 18080
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_RETAINED_BYTES = 512 * 1024 * 1024
MAX_UNIQUE_PATHS = 1024
READ_CHUNK_BYTES = 64 * 1024
COMPLETION_BYTES = b"DEPENDENCY_INDEX_RESOLUTION_COMPLETED\n"
CRATE_NAME = re.compile(r"[a-z0-9_-]+\Z")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class MirrorFailure(RuntimeError):
    """A setup error that permanently fails this resolver attempt."""


@dataclass(frozen=True)
class RetainedResponse:
    path: str
    status: int
    headers: tuple[tuple[str, str], ...]
    upstream_body: bytes
    cargo_body: bytes
    upstream_body_sha256: str
    cargo_body_sha256: str


def canonical_sparse_path(raw_target: str) -> str:
    parsed = urlsplit(raw_target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise MirrorFailure("request target must be an origin-form path without query")
    path = parsed.path
    if not path.startswith("/") or "%" in path or "//" in path:
        raise MirrorFailure("request path is not canonical")
    if path == "/config.json":
        return path
    segments = path[1:].split("/")
    if not segments or any(not segment for segment in segments):
        raise MirrorFailure("empty sparse-index path segment")
    crate = segments[-1]
    if not CRATE_NAME.fullmatch(crate):
        raise MirrorFailure("crate name is outside the canonical lowercase alphabet")
    if len(crate) == 1:
        expected = ["1", crate]
    elif len(crate) == 2:
        expected = ["2", crate]
    elif len(crate) == 3:
        expected = ["3", crate[0], crate]
    else:
        expected = [crate[:2], crate[2:4], crate]
    if segments != expected:
        raise MirrorFailure("crate path does not match the sparse-index name mapping")
    return path


def exactly_one_header(headers: object, name: str) -> str:
    values = headers.get_all(name, [])  # type: ignore[attr-defined]
    if len(values) != 1:
        raise MirrorFailure(f"request must contain exactly one {name} header")
    return values[0]


def header_is_present(headers: object, name: str) -> bool:
    return bool(headers.get_all(name, []))  # type: ignore[attr-defined]


def content_length(headers: tuple[tuple[str, str], ...]) -> int | None:
    values = [value.strip() for name, value in headers if name.lower() == "content-length"]
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        raise MirrorFailure("invalid or duplicate Content-Length")
    return int(values[0])


def validate_transfer_encoding(
    headers: tuple[tuple[str, str], ...], expected_length: int | None
) -> None:
    values = [value.strip().lower() for name, value in headers if name.lower() == "transfer-encoding"]
    if not values:
        if expected_length is None:
            raise MirrorFailure("upstream response lacks accepted framing")
        return
    if values != ["chunked"] or expected_length is not None:
        raise MirrorFailure("ambiguous or unsupported Transfer-Encoding")


def strict_json_object(body: bytes) -> dict[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise MirrorFailure(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MirrorFailure(f"invalid upstream config.json: {error}") from error
    if not isinstance(value, dict):
        raise MirrorFailure("upstream config.json must be an object")
    return value


def sanitized_config(registry_host: str, port: int) -> bytes:
    value = {
        "auth-required": False,
        "dl": f"http://{registry_host}:{port}/__archives_forbidden__/{{crate}}/{{version}}",
    }
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def validate_config_and_derive(body: bytes, registry_host: str, port: int) -> bytes:
    value = strict_json_object(body)
    if set(value) != {"api", "dl"}:
        raise MirrorFailure("upstream config.json key set is not the accepted public set")
    if value["api"] != "https://crates.io":
        raise MirrorFailure("upstream config.json API authority changed")
    if value["dl"] != "https://static.crates.io/crates":
        raise MirrorFailure("upstream config.json download authority changed")
    return sanitized_config(registry_host, port)


def open_directory_chain_no_follow(path: Path) -> int:
    """Open an absolute directory path without traversing any symlink component."""
    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        raise MirrorFailure("directory no-follow primitives are unavailable")
    raw_path = os.fspath(path)
    if not os.path.isabs(raw_path) or os.path.normpath(raw_path) != raw_path:
        raise MirrorFailure("evidence root must be one canonical absolute path")
    components = Path(raw_path).parts
    if components == (os.path.sep,):
        raise MirrorFailure("filesystem root cannot be the evidence root")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(os.path.sep, flags)
    try:
        for component in components[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


class EvidenceStore:
    """Exclusive retained evidence beneath a supervisor-created private root."""

    def __init__(self, root: Path) -> None:
        try:
            root_descriptor = open_directory_chain_no_follow(root)
        except OSError as error:
            raise MirrorFailure("supervisor-created evidence root is required") from error
        try:
            root_stat = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise MirrorFailure("evidence root must be a real directory")
            if root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) != 0o700:
                raise MirrorFailure("evidence root owner/mode must match the mirror UID and 0700")
            if os.listdir(root_descriptor):
                raise MirrorFailure("evidence root must start empty")
            os.mkdir("responses", 0o700, dir_fd=root_descriptor)
            response_descriptor = os.open(
                "responses",
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_descriptor,
            )
        except Exception:
            os.close(root_descriptor)
            raise
        self.root = root
        self.responses = root / "responses"
        self._root_descriptor = root_descriptor
        self._response_descriptor = response_descriptor
        self._records: dict[str, dict[str, object]] = {}
        self._projections: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()
        self._temporary_counter = 0

    def close(self) -> None:
        for attribute in ("_response_descriptor", "_root_descriptor"):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, attribute, -1)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def _atomic_write(self, path: Path, body: bytes, mode: int = 0o400) -> None:
        if path.parent == self.root:
            directory_descriptor = self._root_descriptor
        elif path.parent == self.responses:
            directory_descriptor = self._response_descriptor
        else:
            raise MirrorFailure("evidence write escaped the accepted roots")
        name = path.name
        if not name or name in (".", "..") or os.path.sep in name:
            raise MirrorFailure("evidence filename is not one canonical component")
        self._temporary_counter += 1
        temporary = f".tmp-{os.getpid()}-{self._temporary_counter}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_descriptor)
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise MirrorFailure("short evidence write")
                view = view[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        finally:
            os.unlink(temporary, dir_fd=directory_descriptor)

    def write_control(self, name: str, value: dict[str, object]) -> None:
        if not re.fullmatch(r"[a-z0-9-]+\.json", name):
            raise MirrorFailure("invalid evidence control filename")
        self._atomic_write(
            self.root / name,
            (
                json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                + "\n"
            ).encode("utf-8"),
        )

    def retain(
        self,
        *,
        path: str,
        status: int,
        headers: tuple[tuple[str, str], ...],
        upstream_body: bytes,
        cargo_body: bytes,
        started_ns: int,
        finished_ns: int,
        read_error: str | None,
        response_complete: bool,
        received_bytes_at_least: int,
    ) -> RetainedResponse:
        upstream_digest = hashlib.sha256(upstream_body).hexdigest()
        cargo_digest = hashlib.sha256(cargo_body).hexdigest()
        key = hashlib.sha256(path.encode("utf-8")).hexdigest()
        body_path = self.responses / f"{key}.body"
        metadata_path = self.responses / f"{key}.json"
        record: dict[str, object] = {
            "schema_version": 2,
            "method": "GET",
            "request_path": path,
            "request_url": f"https://{UPSTREAM_HOST}{path}",
            "final_url": f"https://{UPSTREAM_HOST}{path}",
            "status": status,
            "response_headers": [[name, value] for name, value in headers],
            "retained_body_size": len(upstream_body),
            "received_bytes_at_least": received_bytes_at_least,
            "body_sha256": upstream_digest,
            "response_complete": response_complete,
            "body_truncated": received_bytes_at_least > len(upstream_body),
            "read_error": read_error,
            "started_monotonic_ns": started_ns,
            "finished_monotonic_ns": finished_ns,
        }
        with self._lock:
            if path in self._records:
                raise MirrorFailure("upstream path was retained more than once")
            self._atomic_write(body_path, upstream_body)
            self._atomic_write(
                metadata_path,
                (
                    json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                    + "\n"
                ).encode("utf-8"),
            )
            self._records[path] = record
        return RetainedResponse(
            path,
            status,
            headers,
            upstream_body,
            cargo_body,
            upstream_digest,
            cargo_digest,
        )

    def retain_projection(
        self, retained: RetainedResponse, cargo_body: bytes
    ) -> RetainedResponse:
        """Retain a derived Cargo response without altering raw upstream evidence."""
        cargo_digest = hashlib.sha256(cargo_body).hexdigest()
        key = hashlib.sha256(retained.path.encode("utf-8")).hexdigest()
        projection: dict[str, object] = {
            "schema_version": 2,
            "request_path": retained.path,
            "upstream_body_sha256": retained.upstream_body_sha256,
            "cargo_body_size": len(cargo_body),
            "cargo_body_sha256": cargo_digest,
            "derivation": "validated-config-with-archive-and-api-authority-removed",
        }
        with self._lock:
            if retained.path in self._projections:
                raise MirrorFailure("Cargo projection was retained more than once")
            self._atomic_write(self.responses / f"{key}.cargo-body", cargo_body)
            self._atomic_write(
                self.responses / f"{key}.cargo.json",
                (
                    json.dumps(
                        projection,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            self._projections[retained.path] = projection
        return RetainedResponse(
            retained.path,
            retained.status,
            retained.headers,
            retained.upstream_body,
            cargo_body,
            retained.upstream_body_sha256,
            cargo_digest,
        )

    def retain_failure(self, message: str) -> None:
        failure_path = self.root / "failure.json"
        try:
            os.stat("failure.json", dir_fd=self._root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return
        self._atomic_write(
            failure_path,
            (
                json.dumps(
                    {"schema_version": 2, "outcome": "fail-closed", "message": message},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )

    def finalize(self, outcome: str, completion_seen: bool) -> None:
        with self._lock:
            records = [self._records[path] for path in sorted(self._records)]
        jsonl = b"".join(
            (
                json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                + "\n"
            ).encode("utf-8")
            for record in records
        )
        self._atomic_write(self.root / "responses.jsonl", jsonl)
        projections_jsonl = b"".join(
            (
                json.dumps(
                    self._projections[path],
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            for path in sorted(self._projections)
        )
        self._atomic_write(self.root / "projections.jsonl", projections_jsonl)
        self.write_control(
            "summary.json",
            {
                "schema_version": 2,
                "outcome": outcome,
                "supervisor_completion_seen": completion_seen,
                "unique_upstream_gets": len(records),
                "paths": [record["request_path"] for record in records],
            },
        )
        try:
            os.fchmod(self._response_descriptor, 0o500)
            os.fchmod(self._root_descriptor, 0o500)
        finally:
            self.close()


class MirrorState:
    def __init__(
        self,
        store: EvidenceStore,
        registry_host: str,
        port: int,
        max_unique_paths: int = MAX_UNIQUE_PATHS,
        max_total_retained_bytes: int = MAX_TOTAL_RETAINED_BYTES,
    ) -> None:
        self.store = store
        self.registry_host = registry_host
        self.port = port
        self.max_unique_paths = max_unique_paths
        self.max_total_retained_bytes = max_total_retained_bytes
        self.cache: dict[str, RetainedResponse] = {}
        self.attempted_paths: set[str] = set()
        self.total_received_bytes_at_least = 0
        self.fatal: str | None = None
        self.shutdown_callback: object | None = None

    def mark_fatal(self, message: str) -> None:
        if self.fatal is None:
            self.fatal = message
            try:
                self.store.retain_failure(message)
            except Exception as evidence_error:  # the supervisor still observes nonzero exit
                self.fatal = f"{message}; failure-evidence-error:{type(evidence_error).__name__}"
            callback = self.shutdown_callback
            if callable(callback):
                threading.Thread(target=callback, daemon=True).start()

    def fetch(self, path: str) -> RetainedResponse:
        if self.fatal is not None:
            raise MirrorFailure(self.fatal)
        if path in self.cache:
            return self.cache[path]
        try:
            if path in self.attempted_paths:
                raise MirrorFailure("upstream path was attempted more than once")
            if len(self.attempted_paths) >= self.max_unique_paths:
                raise MirrorFailure("unique sparse-index path budget exhausted")
            if self.total_received_bytes_at_least >= self.max_total_retained_bytes:
                raise MirrorFailure("aggregate retained-body budget exhausted")
            self.attempted_paths.add(path)
            retained = self._fetch_once(path)
        except Exception as error:
            message = str(error) or type(error).__name__
            self.mark_fatal(message)
            if isinstance(error, MirrorFailure):
                raise
            raise MirrorFailure(message) from error
        self.cache[path] = retained
        return retained

    def _fetch_once(self, path: str) -> RetainedResponse:
        started_ns = time.monotonic_ns()
        connection: http.client.HTTPSConnection | None = None
        body = bytearray()
        received_bytes = 0
        read_error: str | None = None
        status = 0
        headers: tuple[tuple[str, str], ...] = ()
        try:
            context = ssl.create_default_context()
            connection = http.client.HTTPSConnection(
                UPSTREAM_HOST,
                UPSTREAM_PORT,
                timeout=30,
                context=context,
            )
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "text/plain, application/json;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": "vibapp-stage0-dependency-fetch/2",
                },
            )
            response = connection.getresponse()
            status = response.status
            headers = tuple(response.getheaders())
            while True:
                try:
                    chunk = response.read(READ_CHUNK_BYTES)
                except http.client.IncompleteRead as error:
                    chunk = error.partial
                    received_bytes += len(chunk)
                    remaining = max(
                        0,
                        min(
                            MAX_BODY_BYTES - len(body),
                            self.max_total_retained_bytes
                            - self.total_received_bytes_at_least
                            - len(body),
                        ),
                    )
                    body.extend(chunk[:remaining])
                    read_error = f"IncompleteRead:{error.expected}"
                    break
                if not chunk:
                    break
                received_bytes += len(chunk)
                remaining = max(
                    0,
                    min(
                        MAX_BODY_BYTES - len(body),
                        self.max_total_retained_bytes
                        - self.total_received_bytes_at_least
                        - len(body),
                    ),
                )
                body.extend(chunk[:remaining])
                if received_bytes > MAX_BODY_BYTES:
                    read_error = "body-size-limit-exceeded"
                    break
                if (
                    self.total_received_bytes_at_least + received_bytes
                    > self.max_total_retained_bytes
                ):
                    read_error = "aggregate-body-size-limit-exceeded"
                    break
        except Exception as error:
            read_error = f"{type(error).__name__}:{error}"
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as error:
                    suffix = f"close-{type(error).__name__}:{error}"
                    read_error = f"{read_error};{suffix}" if read_error else suffix

        upstream_body = bytes(body)
        cargo_body = upstream_body
        retained = self.store.retain(
            path=path,
            status=status,
            headers=headers,
            upstream_body=upstream_body,
            cargo_body=cargo_body,
            started_ns=started_ns,
            finished_ns=time.monotonic_ns(),
            read_error=read_error,
            response_complete=read_error is None,
            received_bytes_at_least=received_bytes,
        )
        self.total_received_bytes_at_least += received_bytes
        if read_error is not None:
            raise MirrorFailure(f"upstream response incomplete: {read_error}")
        if status != 200:
            raise MirrorFailure(f"upstream status is {status}, expected 200")
        if any(name.lower() == "location" for name, _ in headers):
            raise MirrorFailure("redirect Location header is forbidden")
        if any(name.lower() == "set-cookie" for name, _ in headers):
            raise MirrorFailure("upstream Set-Cookie is forbidden after retention")
        encodings = [
            value.strip().lower()
            for name, value in headers
            if name.lower() == "content-encoding"
        ]
        if encodings and encodings != ["identity"]:
            raise MirrorFailure("encoded upstream response is forbidden")
        expected_length = content_length(headers)
        validate_transfer_encoding(headers, expected_length)
        if expected_length is not None and expected_length != len(upstream_body):
            raise MirrorFailure("Content-Length does not match retained body")
        if path == "/config.json":
            cargo_body = validate_config_and_derive(
                upstream_body, self.registry_host, self.port
            )
            retained = self.store.retain_projection(retained, cargo_body)
        return retained


class SparseMirrorHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VibAppSparseMirror/2"
    sys_version = ""

    @property
    def state(self) -> MirrorState:
        return self.server.state  # type: ignore[attr-defined,no-any-return]

    def parse_request(self) -> bool:
        if not super().parse_request():
            self.state.mark_fatal("malformed local HTTP request")
            return False
        if self.command != "GET":
            self._reject_method()
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            host = exactly_one_header(self.headers, "Host")
            expected_host = f"{self.state.registry_host}:{self.state.port}"
            if host != expected_host:
                raise MirrorFailure("Host header does not match the internal mirror authority")
            for forbidden in ("Authorization", "Proxy-Authorization", "Cookie"):
                if header_is_present(self.headers, forbidden):
                    raise MirrorFailure(f"request header {forbidden} is forbidden")
            for framing in ("Content-Length", "Transfer-Encoding", "Expect"):
                if header_is_present(self.headers, framing):
                    raise MirrorFailure(f"GET request framing header {framing} is forbidden")
            path = canonical_sparse_path(self.path)
            retained = self.state.fetch(path)
            content_type = "application/json" if path == "/config.json" else "text/plain"
            self.send_response_only(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(retained.cargo_body)))
            self.send_header("X-VibApp-Body-SHA256", retained.cargo_body_sha256)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(retained.cargo_body)
        except Exception as error:
            message = str(error) or type(error).__name__
            self.state.mark_fatal(message)
            encoded = b"dependency sparse mirror failed closed\n"
            try:
                self.send_response_only(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)
            except Exception:
                pass

    def _reject_method(self) -> None:
        message = f"method {self.command!r} is forbidden"
        self.state.mark_fatal(message)
        try:
            self.send_response_only(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except Exception:
            pass

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        self.state.mark_fatal(f"local HTTP parser error {code}")
        super().send_error(code, message, explain)

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


class SparseMirrorServer(http.server.HTTPServer):
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], state: MirrorState) -> None:
        super().__init__(address, SparseMirrorHandler)
        self.state = state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--completion-file", required=True, type=Path)
    parser.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--registry-host", default=DEFAULT_REGISTRY_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-unique-paths", type=int, default=MAX_UNIQUE_PATHS)
    parser.add_argument(
        "--max-total-retained-bytes",
        type=int,
        default=MAX_TOTAL_RETAINED_BYTES,
    )
    return parser.parse_args()


def fail(message: str) -> NoReturn:
    print(f"dependency sparse mirror: {message}", file=sys.stderr)
    raise SystemExit(1)


def valid_completion_file(path: Path) -> bool:
    descriptor = -1
    parent_descriptor = -1
    try:
        required_flags = ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")
        if any(not hasattr(os, name) for name in required_flags):
            return False
        if path.name in ("", ".", ".."):
            return False
        parent_descriptor = open_directory_chain_no_follow(path.parent)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            return False
        if file_stat.st_uid != 0 or file_stat.st_gid != 0:
            return False
        if stat.S_IMODE(file_stat.st_mode) != 0o444:
            return False
        body = os.read(descriptor, len(COMPLETION_BYTES) + 1)
        return body == COMPLETION_BYTES
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def completion_file_is_absent(path: Path) -> bool:
    if path.name in ("", ".", ".."):
        raise MirrorFailure("completion file path is not canonical")
    try:
        parent_descriptor = open_directory_chain_no_follow(path.parent)
    except OSError as error:
        raise MirrorFailure("completion parent chain is not canonical") from error
    try:
        try:
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(parent_descriptor)


def main() -> int:
    args = parse_args()
    if os.geteuid() == 0:
        fail("mirror must run as the non-root setup UID")
    if args.listen_host != DEFAULT_LISTEN_HOST:
        fail("listen host is not the accepted internal-network address")
    if not DNS_LABEL.fullmatch(args.registry_host):
        fail("registry host is not one canonical DNS label")
    if not 1 <= args.port <= 65535:
        fail("port is outside the valid range")
    if args.max_unique_paths != MAX_UNIQUE_PATHS:
        fail("unique sparse-index path budget differs from the accepted value")
    if args.max_total_retained_bytes != MAX_TOTAL_RETAINED_BYTES:
        fail("aggregate retained-body budget differs from the accepted value")
    try:
        completion_absent = completion_file_is_absent(args.completion_file)
    except Exception as error:
        fail(str(error) or type(error).__name__)
    if not completion_absent:
        fail("completion file must be absent before the mirror starts")
    try:
        store = EvidenceStore(args.evidence_root)
        state = MirrorState(store, args.registry_host, args.port)
        server = SparseMirrorServer((args.listen_host, args.port), state)
    except Exception as error:
        fail(str(error) or type(error).__name__)

    state.shutdown_callback = server.shutdown
    completion_authorized = False

    def stop_server(signum: int, frame: object) -> None:
        nonlocal completion_authorized
        del frame
        if signum == signal.SIGTERM and valid_completion_file(args.completion_file):
            completion_authorized = True
        else:
            state.mark_fatal(f"unexpected signal {signum} without supervisor completion")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    try:
        store.write_control(
            "ready.json",
            {
                "schema_version": 2,
                "listen_host": args.listen_host,
                "registry_host": args.registry_host,
                "port": args.port,
                "registry": f"sparse+http://{args.registry_host}:{args.port}/",
                "archive_requests_enabled": False,
                "max_unique_paths": args.max_unique_paths,
                "max_total_retained_bytes": args.max_total_retained_bytes,
            },
        )
        server.serve_forever(poll_interval=0.1)
    except Exception as error:
        state.mark_fatal(str(error) or type(error).__name__)
    finally:
        server.server_close()
        if not completion_authorized and state.fatal is None:
            state.mark_fatal("mirror stopped without supervisor completion")
        outcome = "completed" if completion_authorized and state.fatal is None else "fail-closed"
        store.finalize(outcome, completion_authorized)
    return 0 if completion_authorized and state.fatal is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
