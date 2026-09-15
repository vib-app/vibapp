#!/usr/bin/env python3
"""Dependency-free local server for the VibApp Client product platform."""

from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE = ROOT / "fixtures" / "client-state.json"
DEFAULT_STATIC = ROOT / "static"
MAX_REQUEST_BYTES = 32 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_COLLECTION_ITEMS = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        content = handle.read(MAX_STATE_BYTES + 1)
    if len(content) > MAX_STATE_BYTES:
        raise ValueError(f"{path}: JSON file exceeds 4 MiB")
    return json.loads(content.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)


class ClientState:
    """Owns only local presentation state and request outboxes."""

    def __init__(
        self,
        data_dir: Path,
        fixture_path: Path = DEFAULT_FIXTURE,
        builder_feed: Path | None = None,
        registry_feed: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.fixture_path = fixture_path
        self.builder_feed = builder_feed
        self.registry_feed = registry_feed
        self.state_path = data_dir / "state.json"
        self.need_outbox = data_dir / "outbox" / "need-requests.jsonl"
        self.install_outbox = data_dir / "outbox" / "install-intents.jsonl"
        self.lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "outbox").mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            initial = read_json(self.fixture_path)
            self._validate_state(initial)
            self._write_state(initial)

    @staticmethod
    def _validate_state(state: Any) -> None:
        if not isinstance(state, dict):
            raise ValueError("client state must be a JSON object")
        for key in ("meta", "needs", "jobs", "apps", "install_intents"):
            if key not in state:
                raise ValueError(f"client state missing {key}")
        for key in ("needs", "jobs", "apps", "install_intents"):
            if not isinstance(state[key], list):
                raise ValueError(f"client state {key} must be an array")

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.state_path)

    @staticmethod
    def _read_feed(path: Path, collection: str, id_field: str) -> list[dict[str, Any]]:
        raw = read_json(path)
        values = raw.get(collection) if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise ValueError(f"{path}: expected an array or an object containing {collection}")
        if len(values) > MAX_COLLECTION_ITEMS:
            raise ValueError(f"{path}: {collection} exceeds {MAX_COLLECTION_ITEMS} items")
        for index, value in enumerate(values):
            if not isinstance(value, dict) or not isinstance(value.get(id_field), str):
                raise ValueError(f"{path}: {collection}[{index}] is missing string {id_field}")
        return values

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = read_json(self.state_path)
            self._validate_state(state)
            result = copy.deepcopy(state)
            feed_errors: list[dict[str, str]] = []
            for feed, collection, id_field in (
                (self.builder_feed, "jobs", "job_id"),
                (self.registry_feed, "apps", "app_id"),
            ):
                if feed is None:
                    continue
                try:
                    result[collection] = self._read_feed(feed, collection, id_field)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    result[collection] = []
                    feed_errors.append({"feed": collection, "error": str(error)})
            result["feed_errors"] = feed_errors
            result["meta"]["runtime_mode"] = "product-platform-local"
            result["meta"]["installation_performed"] = False
            return result

    def submit_need(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_payload", "请求必须是 JSON 对象。")
        title = payload.get("title")
        description = payload.get("description")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_title", "需求名称需为 1–80 个字符。")
        if not isinstance(description, str) or len(description.strip()) < 10 or len(description.strip()) > 2000:
            raise ClientError(
                HTTPStatus.BAD_REQUEST,
                "invalid_description",
                "需求描述需为 10–2000 个字符。",
            )
        now = utc_now()
        need = {
            "need_id": f"need-{uuid.uuid4().hex[:10]}",
            "title": title.strip(),
            "description": description.strip(),
            "route": "local-private",
            "status": "ready-for-builder",
            "handoff": "need-request-outbox",
            "created_at_utc": now,
        }
        with self.lock:
            state = read_json(self.state_path)
            self._validate_state(state)
            state["needs"].insert(0, need)
            state["needs"] = state["needs"][:MAX_COLLECTION_ITEMS]
            self._write_state(state)
            self._append_jsonl(self.need_outbox, need)
        return need

    def submit_install_intent(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("app_id"), str):
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_payload", "缺少 app_id。")
        snapshot = self.snapshot()
        app = next((item for item in snapshot["apps"] if item.get("app_id") == payload["app_id"]), None)
        if app is None:
            raise ClientError(HTTPStatus.NOT_FOUND, "app_not_found", "没有找到这个应用。")
        if not app.get("install_eligible") or app.get("verification_state") != "verified":
            raise ClientError(
                HTTPStatus.CONFLICT,
                "not_install_eligible",
                "只有独立验收为 verified 的候选包才能记录安装意图。",
            )
        digest = payload.get("package_digest_sha256")
        if digest != app.get("package_digest_sha256"):
            raise ClientError(HTTPStatus.CONFLICT, "digest_mismatch", "应用摘要已变化，请刷新后重试。")
        intent = {
            "intent_id": f"intent-{uuid.uuid4().hex[:10]}",
            "app_id": app["app_id"],
            "package_digest_sha256": digest,
            "status": "intent-recorded",
            "installation_performed": False,
            "next_authority": "future-daemon",
            "created_at_utc": utc_now(),
        }
        with self.lock:
            state = read_json(self.state_path)
            self._validate_state(state)
            state["install_intents"].insert(0, intent)
            state["install_intents"] = state["install_intents"][:MAX_COLLECTION_ITEMS]
            self._write_state(state)
            self._append_jsonl(self.install_outbox, intent)
        return intent

    @staticmethod
    def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class ClientError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def make_handler(state: ClientState, static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "VibAppProductPlatform/0.1"

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send_json(self, status: HTTPStatus, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            try:
                body = path.read_bytes()
            except OSError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "页面不存在。"}})
                return
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_payload(self) -> Any:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError as error:
                raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_length", "Content-Length 无效。") from error
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ClientError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "请求为空或超过 32 KiB。")
            try:
                return json.loads(
                    self.rfile.read(length).decode("utf-8"),
                    object_pairs_hook=reject_duplicate_keys,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求不是有效的 JSON。") from error

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "mode": "product-platform-local",
                        "network": "disabled",
                        "real_installation": False,
                    },
                )
            elif route == "/api/state":
                try:
                    self._send_json(HTTPStatus.OK, state.snapshot())
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": {"code": "state_unavailable", "message": str(error)}},
                    )
            elif route in ("/", "/index.html"):
                self._send_file(static_dir / "index.html", "text/html; charset=utf-8")
            elif route == "/styles.css":
                self._send_file(static_dir / "styles.css", "text/css; charset=utf-8")
            elif route == "/app.js":
                self._send_file(static_dir / "app.js", "text/javascript; charset=utf-8")
            elif route == "/favicon.svg":
                self._send_file(static_dir / "favicon.svg", "image/svg+xml")
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "页面不存在。"}})

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            try:
                payload = self._read_payload()
                if route == "/api/needs":
                    self._send_json(HTTPStatus.CREATED, {"need": state.submit_need(payload)})
                elif route == "/api/install-intents":
                    self._send_json(HTTPStatus.CREATED, {"intent": state.submit_install_intent(payload)})
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "接口不存在。"}})
            except ClientError as error:
                self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

    return Handler


def build_server(
    host: str,
    port: int,
    data_dir: Path,
    fixture_path: Path = DEFAULT_FIXTURE,
    static_dir: Path = DEFAULT_STATIC,
    builder_feed: Path | None = None,
    registry_feed: Path | None = None,
) -> HTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the local client only binds to a loopback address")
    state = ClientState(data_dir, fixture_path, builder_feed, registry_feed)
    return HTTPServer((host, port), make_handler(state, static_dir))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local VibApp product client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "runtime")
    parser.add_argument("--builder-feed", type=Path)
    parser.add_argument("--registry-feed", type=Path)
    args = parser.parse_args()
    server = build_server(
        args.host,
        args.port,
        args.data_dir,
        builder_feed=args.builder_feed,
        registry_feed=args.registry_feed,
    )
    print(f"VibApp product client: http://{args.host}:{server.server_port}")
    print("Local-only. This prototype never installs, publishes, or calls a cloud service.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
