#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("need_analyzer.py")
SPEC = importlib.util.spec_from_file_location("need_analyzer", MODULE_PATH)
assert SPEC and SPEC.loader
need_analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(need_analyzer)


class ChatHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        events = [
            {"choices": [{"delta": {"reasoning": '{"goal_'}}]},
            {"choices": [{"delta": {"reasoning": 'summary":"本地记事"}'}}]},
        ]
        body = b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


class ResponsesHandler(http.server.BaseHTTPRequestHandler):
    observed_authorization = None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).observed_authorization = self.headers.get("Authorization")
        self.assertions = payload
        events = [
            {"type": "response.output_text.delta", "delta": '{"g":"'},
            {"type": "response.output_text.delta", "delta": '响应接口"}'},
        ]
        body = b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@contextlib.contextmanager
def fake_server(handler=ChatHandler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class NeedAnalyzerTests(unittest.TestCase):
    def test_default_configuration_targets_requested_170_model(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            config = need_analyzer._configuration()
        self.assertEqual(config["base_url"], "http://192.168.199.170:8081")
        self.assertEqual(config["model"], "qwen3.8-27b-uncensored-mtp-q4")

    def test_unapproved_endpoint_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"VIBAPP_LLM_BASE_URL": "http://example.com:8081"}, clear=True):
            with self.assertRaises(need_analyzer.AnalyzerError):
                need_analyzer._configuration()

    def test_reasoning_field_is_accepted_but_only_json_is_returned(self) -> None:
        with fake_server() as port:
            config = {
                "base_url": f"http://127.0.0.1:{port}",
                "model": "test-model",
                "timeout": 5,
                "max_tokens": 256,
                "temperature": 0,
            }
            text = need_analyzer._post_chat(config, {"title": "记事", "description": "做一个本地保存的记事工具。"})
        self.assertEqual(text, '{"goal_summary":"本地记事"}')
        self.assertNotIn("reasoning", text)

    def test_responses_protocol_stream_and_bearer_key_are_supported(self) -> None:
        ResponsesHandler.observed_authorization = None
        with fake_server(ResponsesHandler) as port:
            config = {
                "base_url": f"http://127.0.0.1:{port}/v1",
                "model": "response-test-model",
                "protocol": "responses",
                "timeout": 5,
                "max_tokens": 256,
                "temperature": 0,
                "api_key": "test-key",
            }
            text = need_analyzer._post_chat(config, {"title": "测试", "description": "做一个用于接口测试的小工具。"})
        self.assertEqual(text, '{"g":"响应接口"}')
        self.assertEqual(ResponsesHandler.observed_authorization, "Bearer test-key")

    def test_byom_configuration_accepts_https_and_rejects_remote_http(self) -> None:
        config = need_analyzer._configuration({
            "enabled": True,
            "base_url": "https://models.example.test/v1",
            "model": "custom-model",
            "protocol": "chat-completions",
            "timeout_seconds": 30,
            "max_output_tokens": 512,
            "temperature": 0.2,
            "api_key": None,
        })
        self.assertEqual(config["provider"], "openai-compatible-byom")
        with self.assertRaises(need_analyzer.AnalyzerError):
            need_analyzer._configuration({**{
                "enabled": True,
                "base_url": "http://models.example.test/v1",
                "model": "custom-model",
                "protocol": "chat-completions",
                "timeout_seconds": 30,
                "max_output_tokens": 512,
                "temperature": 0.2,
                "api_key": None,
            }})

    def test_normalization_drops_unknown_capability_and_marks_absent_fields(self) -> None:
        raw = {
            "goal_summary": "离线浇水记录",
            "app_kind": "ui",
            "capabilities": ["kv", "invented-power", "http"],
            "acceptance_example": "重启后仍能看到浇水记录。",
            "negative_constraints": ["不得联网"],
            "network_mode": "offline",
            "package_name": "浇水记录",
            "missing_fields": ["invented-field"],
            "questions": [],
        }
        result = need_analyzer._normalize(
            raw,
            {"title": "浇水记录", "description": "做一个离线浇水记录工具。"},
            {"model": "test", "base_url": "http://127.0.0.1:1"},
        )
        self.assertEqual(result["capabilities"], ["kv"])
        self.assertNotIn("http", result["capabilities"])
        self.assertEqual(result["missing_fields"], [])
        self.assertNotIn("invented-field", result["missing_fields"])
        self.assertEqual(result["target_runtime"], "vibapp-client")
        self.assertIsNone(result["platform_os"])

    def test_prompt_never_asks_model_to_choose_a_host_platform(self) -> None:
        messages = need_analyzer._prompt({"title": "记事", "description": "做一个本地记事工具。"})
        system = messages[0]["content"]
        self.assertIn("宿主兼容由 Client 自动处理", system)
        self.assertNotIn("platform-profile", system)

    def test_malformed_model_output_is_rejected(self) -> None:
        with self.assertRaises(need_analyzer.AnalyzerError):
            need_analyzer._extract_object("Here is the answer without JSON")

    def test_wrapped_reasoning_returns_only_final_object(self) -> None:
        result = need_analyzer._extract_object('internal note {"draft":true} final {"g":"用户目标"}')
        self.assertEqual(result, {"g": "用户目标"})

    def test_model_extra_code_source_shell_fields_fail_closed(self) -> None:
        for field in ("code", "source", "shell", "unknown"):
            with self.subTest(field=field), self.assertRaises(need_analyzer.AnalyzerError):
                need_analyzer._expand_short_fields({"g": "用户目标", field: "untrusted"})

    def test_short_and_long_alias_collision_fails_closed(self) -> None:
        with self.assertRaises(need_analyzer.AnalyzerError):
            need_analyzer._expand_short_fields({"g": "短字段", "goal_summary": "长字段"})

    def test_only_declared_short_or_long_fields_are_accepted(self) -> None:
        self.assertEqual(
            need_analyzer._expand_short_fields({"g": "用户目标", "app_kind": "ui"}),
            {"goal_summary": "用户目标", "app_kind": "ui"},
        )


if __name__ == "__main__":
    unittest.main()
