#!/usr/bin/env python3
"""Bounded LocalAI-backed proposer for an experimental VibApp NeedSpec draft."""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.parse
from typing import Any


DEFAULT_BASE_URL = "http://192.168.199.170:8081"
DEFAULT_MODEL = "qwen3.8-27b-uncensored-mtp-q4"
MAX_INPUT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_STREAM_EVENTS = 4096
SUPPORTED_CAPABILITIES = (
    "clock",
    "scheduler",
    "notification",
    "kv",
    "log",
    "host-info",
    "settings",
    "system-metrics",
    "http",
)
MISSING_FIELDS = (
    "app-kind",
    "capabilities",
    "acceptance-criteria",
    "negative-constraints",
    "network-mode",
    "package-intent",
)
ANALYSIS_KEYS = {
    "g", "k", "c", "e", "d", "n", "m", "s", "z", "q",
    "goal_summary", "app_kind",
    "capabilities", "acceptance_example", "negative_constraints", "network_mode",
    "package_name", "assumptions", "missing_fields", "questions",
}
FIELD_ALIASES = {
    "g": "goal_summary",
    "k": "app_kind",
    "c": "capabilities",
    "e": "acceptance_example",
    "d": "negative_constraints",
    "n": "network_mode",
    "m": "package_name",
    "s": "assumptions",
    "z": "missing_fields",
    "q": "questions",
}
NORMALIZED_FIELDS = set(FIELD_ALIASES.values())
MODEL_FIELDS = set(FIELD_ALIASES) | NORMALIZED_FIELDS


class AnalyzerError(RuntimeError):
    pass


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise AnalyzerError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise AnalyzerError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as error:
        raise AnalyzerError(f"{name} must be numeric") from error
    if not minimum <= value <= maximum:
        raise AnalyzerError(f"{name} must be between {minimum} and {maximum}")
    return value


def _valid_private_http_host(host: str | None) -> bool:
    if host in {"localhost", "127.0.0.1", "::1", "192.168.199.170"}:
        return True
    if not host:
        return False
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".", 2)[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


def _validate_base_url(value: Any, *, legacy_environment: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or any(char.isspace() for char in value):
        raise AnalyzerError("model base_url is invalid")
    base_url = value.rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise AnalyzerError("model base_url must use http or https without embedded credentials")
    if parsed.query or parsed.fragment:
        raise AnalyzerError("model base_url cannot contain a query or fragment")
    if parsed.scheme == "http" and not _valid_private_http_host(parsed.hostname):
        raise AnalyzerError("remote model endpoints must use HTTPS; HTTP is limited to private LAN or loopback")
    if legacy_environment:
        approved = parsed.hostname == "192.168.199.170" and parsed.port == 8081 and not parsed.path
        test_loopback = (
            os.environ.get("VIBAPP_LLM_ALLOW_TEST_LOOPBACK") == "1"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port is not None
            and not parsed.path
        )
        if not (approved or test_loopback):
            raise AnalyzerError("VIBAPP_LLM_BASE_URL is not an approved LocalAI endpoint")
    return base_url


def _configuration(override: Any = None) -> dict[str, Any]:
    if override is not None:
        if not isinstance(override, dict):
            raise AnalyzerError("model_config must be an object")
        expected = {
            "enabled", "base_url", "model", "protocol", "timeout_seconds",
            "max_output_tokens", "temperature", "api_key",
        }
        if set(override) != expected:
            raise AnalyzerError("model_config fields are invalid")
        if override["enabled"] is not True:
            raise AnalyzerError("configured generation model is disabled")
        base_url = _validate_base_url(override["base_url"])
        model = override["model"]
        if not isinstance(model, str) or not model or len(model) > 256 or any(ord(char) < 32 for char in model):
            raise AnalyzerError("configured model name is invalid")
        protocol = override["protocol"]
        if protocol not in {"chat-completions", "responses"}:
            raise AnalyzerError("configured model protocol is invalid")
        timeout = override["timeout_seconds"]
        max_tokens = override["max_output_tokens"]
        temperature = override["temperature"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 5 <= timeout <= 120:
            raise AnalyzerError("configured timeout is invalid")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 128 <= max_tokens <= 4096:
            raise AnalyzerError("configured max_output_tokens is invalid")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not 0 <= temperature <= 2:
            raise AnalyzerError("configured temperature is invalid")
        api_key = override["api_key"]
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key or len(api_key) > 8192 or any(char.isspace() for char in api_key)
        ):
            raise AnalyzerError("configured API key is invalid")
        return {
            "base_url": base_url,
            "model": model,
            "protocol": protocol,
            "timeout": timeout,
            "max_tokens": max_tokens,
            "temperature": float(temperature),
            "api_key": api_key,
            "provider": "localai-lan" if base_url == DEFAULT_BASE_URL and model == DEFAULT_MODEL else "openai-compatible-byom",
        }
    enabled = os.environ.get("VIBAPP_LLM_ENABLED", "1").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise AnalyzerError("VIBAPP_LLM_ENABLED is disabled")
    base_url = _validate_base_url(
        os.environ.get("VIBAPP_LLM_BASE_URL", DEFAULT_BASE_URL),
        legacy_environment=True,
    )
    model = os.environ.get("VIBAPP_LLM_MODEL", DEFAULT_MODEL).strip()
    if not model or len(model) > 128 or any(ord(char) < 32 for char in model):
        raise AnalyzerError("VIBAPP_LLM_MODEL is invalid")
    return {
        "base_url": base_url,
        "model": model,
        "protocol": "chat-completions",
        "timeout": _bounded_int("VIBAPP_LLM_TIMEOUT_SECONDS", 45, 5, 60),
        "max_tokens": _bounded_int("VIBAPP_LLM_MAX_TOKENS", 512, 256, 1024),
        "temperature": _bounded_float("VIBAPP_LLM_TEMPERATURE", 0.0, 0.0, 0.3),
        "api_key": None,
        "provider": "localai-lan",
    }


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise AnalyzerError("request exceeds 16 KiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnalyzerError("request is not valid JSON") from error
    if not isinstance(value, dict):
        raise AnalyzerError("request must be an object")
    title = value.get("title")
    description = value.get("description")
    if not isinstance(title, str) or not 1 <= len(title) <= 80:
        raise AnalyzerError("title must contain 1-80 characters")
    if not isinstance(description, str) or not 10 <= len(description) <= 2000:
        raise AnalyzerError("description must contain 10-2000 characters")
    extra = set(value) - {"title", "description", "model_config"}
    if extra:
        raise AnalyzerError("request contains unknown fields")
    return {"title": title, "description": description, "model_config": value.get("model_config")}


def _prompt(request: dict[str, str]) -> list[dict[str, str]]:
    system = """你是 VibApp 需求预处理器，只输出单行 JSON，不要 Markdown 或解释，不作授权决定。
所有生成物都是由 VibApp Client 拉起的 VibApp 应用；不要询问、推断或输出 macOS、Windows、Linux、浏览器、CPU 架构或 profile，宿主兼容由 Client 自动处理。
使用短字段：g目标摘要；k=ui|service|hybrid|unknown；
c能力数组，仅限clock,scheduler,notification,kv,log,host-info,settings,system-metrics,http；
e非技术用户可验证的一句验收标准；d禁止事项数组；n=offline|scoped-network|unknown；m应用名；s假设数组；
z缺失字段数组，仅限app-kind,capabilities,acceptance-criteria,negative-constraints,network-mode,package-intent；q只问真正缺失或会改变实现的问题数组。
合理推断并预填，不为形式提问。明确离线时 n=offline、c不能含http、d包含禁止联网。不要加入凭据、个人数据或授权结论。"""
    user = f"应用名建议：{request['title']}\n原始需求：{request['description']}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _response_output_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = value.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for output in value.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _stream_text(response: http.client.HTTPResponse, timeout: int) -> str:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    total_bytes = 0
    event_count = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = response.readline(MAX_RESPONSE_BYTES + 1)
        if not line:
            break
        total_bytes += len(line)
        if total_bytes > MAX_STREAM_BYTES or len(line) > MAX_RESPONSE_BYTES:
            raise AnalyzerError("LocalAI stream exceeds its bounded wire budget")
        stripped = line.strip()
        if not stripped or stripped.startswith(b":"):
            continue
        if not stripped.startswith(b"data:"):
            continue
        payload = stripped[5:].strip()
        if payload == b"[DONE]":
            break
        event_count += 1
        if event_count > MAX_STREAM_EVENTS:
            raise AnalyzerError("LocalAI stream exceeds its event budget")
        try:
            event = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AnalyzerError("LocalAI returned an invalid stream event") from error
        content = None
        reasoning = None
        if isinstance(event, dict) and event.get("type") == "response.output_text.delta":
            content = event.get("delta")
        elif isinstance(event, dict) and event.get("type") in {"response.completed", "response.done"}:
            completed = _response_output_text(event.get("response"))
            if completed:
                content_parts.append(completed)
            continue
        else:
            try:
                delta = event["choices"][0].get("delta") or event["choices"][0].get("message") or {}
                content = delta.get("content")
                reasoning = delta.get("reasoning")
            except (KeyError, IndexError, TypeError, AttributeError) as error:
                raise AnalyzerError("model returned an invalid stream event") from error
        if isinstance(content, str):
            content_parts.append(content)
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
    else:
        raise AnalyzerError("LocalAI stream exceeded its total time budget")
    content_text = "".join(content_parts).strip()
    reasoning_text = "".join(reasoning_parts).strip()
    candidates = [text for text in (reasoning_text, content_text) if text]
    return max(candidates, key=_analysis_score) if candidates else ""


def _endpoint_path(base_url: str, suffix: str) -> tuple[urllib.parse.SplitResult, str]:
    parsed = urllib.parse.urlsplit(base_url)
    prefix = parsed.path.rstrip("/")
    if prefix.endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    return parsed, f"{prefix}{suffix}" or "/"


def _post_chat(config: dict[str, Any], request: dict[str, str]) -> str:
    protocol = config.get("protocol", "chat-completions")
    messages = _prompt(request)
    if protocol == "responses":
        request_body = {
            "model": config["model"],
            "instructions": messages[0]["content"],
            "input": messages[1]["content"],
            "temperature": config["temperature"],
            "max_output_tokens": config["max_tokens"],
            "stream": True,
        }
        parsed, endpoint = _endpoint_path(config["base_url"], "/v1/responses")
    else:
        request_body = {
            "model": config["model"],
            "messages": messages,
            "temperature": config["temperature"],
            "max_tokens": config["max_tokens"],
            "stream": True,
            "response_format": {"type": "json_object"},
        }
        parsed, endpoint = _endpoint_path(config["base_url"], "/v1/chat/completions")
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=config["timeout"])
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if config.get("api_key"):
        headers["Authorization"] = f"Bearer {config['api_key']}"
    try:
        connection.request(
            "POST",
            endpoint,
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        if response.status != 200:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AnalyzerError("LocalAI error response exceeds 256 KiB")
            raise AnalyzerError(f"LocalAI returned HTTP {response.status}")
        content_type = response.getheader("Content-Type", "").lower()
        if "text/event-stream" in content_type:
            text = _stream_text(response, config["timeout"])
            if not text:
                raise AnalyzerError("LocalAI stream returned no analyzable text")
            return text
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except OSError as error:
        raise AnalyzerError(f"LocalAI request failed: {type(error).__name__}") from error
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AnalyzerError("LocalAI response exceeds 256 KiB")
    try:
        value = json.loads(raw)
        if protocol == "responses":
            text = _response_output_text(value)
        else:
            message = value["choices"][0]["message"]
            candidates = [text for text in (message.get("reasoning"), message.get("content")) if isinstance(text, str) and text.strip()]
            text = max(candidates, key=_analysis_score) if candidates else ""
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise AnalyzerError("model response has an invalid schema") from error
    if not isinstance(text, str) or not text.strip():
        raise AnalyzerError("LocalAI returned no analyzable text")
    return text.strip()


def _expand_short_fields(raw: dict[str, Any]) -> dict[str, Any]:
    extra = sorted(set(raw) - MODEL_FIELDS)
    if extra:
        raise AnalyzerError("model output contains unknown fields: " + ",".join(extra))
    expanded: dict[str, Any] = {}
    for key, value in raw.items():
        normalized = FIELD_ALIASES.get(key, key)
        if normalized in expanded:
            raise AnalyzerError(f"model output aliases the field twice: {normalized}")
        expanded[normalized] = value
    return expanded


def _extract_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_newline < 0 or last_fence <= first_newline:
            raise AnalyzerError("model output has an invalid code fence")
        candidate = candidate[first_newline + 1:last_fence].strip()
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    # Some Qwen3.8 deployments stream internal reasoning followed by the final
    # object in the same field. Locate objects without ever returning or logging
    # the surrounding reasoning; the selected object is schema-validated next.
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, character in enumerate(candidate):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(candidate, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    if not objects:
        raise AnalyzerError("model output contains no JSON object")
    return max(
        enumerate(objects),
        key=lambda item: (len(ANALYSIS_KEYS.intersection(item[1])), item[0]),
    )[1]


def _analysis_score(text: str) -> int:
    try:
        return len(ANALYSIS_KEYS.intersection(_extract_object(text)))
    except AnalyzerError:
        return -1


def _text(value: Any, maximum: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or any(ord(char) < 32 and char not in "\n\t" for char in cleaned):
        return fallback
    return cleaned


def _string_list(value: Any, maximum_items: int, maximum_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:maximum_items]:
        cleaned = _text(item, maximum_chars)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _normalize(raw: dict[str, Any], request: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    app_kind = raw.get("app_kind") if raw.get("app_kind") in {"ui", "service", "hybrid"} else None
    capabilities = [item for item in _string_list(raw.get("capabilities"), 9, 32) if item in SUPPORTED_CAPABILITIES]
    network_mode = raw.get("network_mode") if raw.get("network_mode") in {"offline", "scoped-network"} else None
    if network_mode == "offline":
        capabilities = [item for item in capabilities if item != "http"]
    requested_missing = [item for item in _string_list(raw.get("missing_fields"), 7, 40) if item in MISSING_FIELDS]
    inferred_missing = {
        "app-kind": app_kind is None,
        "capabilities": not capabilities,
        "acceptance-criteria": not _text(raw.get("acceptance_example"), 1000),
        "negative-constraints": not isinstance(raw.get("negative_constraints"), list),
        "network-mode": network_mode is None,
        "package-intent": not _text(raw.get("package_name"), 80),
    }
    missing = [field for field in requested_missing if inferred_missing.get(field, True)]
    for field in MISSING_FIELDS:
        if inferred_missing.get(field, False) and field not in missing:
            missing.append(field)
    questions = _string_list(raw.get("questions"), 7, 240)
    if missing and not questions:
        questions = ["请补充这些会改变实现方式的内容：" + "、".join(missing)]
    return {
        "schema_version": "vibapp.need-analysis.experimental-v1",
        "status": "analyzed",
        "provider": config.get("provider", "openai-compatible-byom"),
        "model": config["model"],
        "endpoint": config["base_url"],
        "goal_summary": _text(raw.get("goal_summary"), 2000, request["description"]),
        "app_kind": app_kind,
        "target_runtime": "vibapp-client",
        "profile": None,
        "platform_os": None,
        "platform_arch": None,
        "capabilities": capabilities,
        "acceptance_example": _text(raw.get("acceptance_example"), 1000),
        "negative_constraints": _string_list(raw.get("negative_constraints"), 12, 500),
        "network_mode": network_mode,
        "package_name": _text(raw.get("package_name"), 80, request["title"]),
        "assumptions": _string_list(raw.get("assumptions"), 10, 300),
        "missing_fields": missing,
        "questions": questions,
    }


def analyze() -> dict[str, Any]:
    request = _read_request()
    config = _configuration(request.pop("model_config", None))
    raw = _expand_short_fields(_extract_object(_post_chat(config, request)))
    return _normalize(raw, request, config)


def main() -> int:
    if sys.argv[1:] != ["analyze"]:
        print("usage: need_analyzer.py analyze", file=sys.stderr)
        return 64
    try:
        result = analyze()
    except AnalyzerError as error:
        print(str(error), file=sys.stderr)
        return 2
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        print("normalized analysis exceeds 64 KiB", file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
