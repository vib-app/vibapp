#!/usr/bin/env python3
"""Bounded read-only model protocol probe; never execute proposed tools."""
import argparse
import http.client
import json
from pathlib import Path
from retry_failed_samples import write_json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--stream", action="store_true")
parser.add_argument("--request", type=Path, help="Replay one recorded host request without executing returned tools")
parser.add_argument("--timeout", type=int, default=30, choices=range(1, 301))
args = parser.parse_args()
request = {
    "model": "qwen3.8-27b-uncensored-mtp-q4", "stream": args.stream,
    "max_tokens": 512, "temperature": 0,
    "messages": [{"role": "user", "content": "Call the bash tool exactly once with command pwd. Do not explain or answer in text. This is a tool protocol test."}],
    "tools": [{"type": "function", "function": {"name": "bash", "description": "Return output of one shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}}],
    "tool_choice": "auto",
}
if args.request:
    if args.request.stat().st_size > 256 * 1024:
        raise ValueError("Recorded request exceeds limit")
    request = json.loads(args.request.read_bytes())["request"]
    if request.get("model") != "qwen3.8-27b-uncensored-mtp-q4":
        raise ValueError("Probe cannot change model")
    request["stream"] = args.stream
connection = http.client.HTTPConnection("192.168.199.170", 8081, timeout=args.timeout)
connection.request("POST", "/v1/chat/completions", json.dumps(request), {"Content-Type": "application/json"})
response = connection.getresponse()
result = {"status": response.status, "stream": args.stream, "content": "", "tool_calls": [], "finish_reasons": [], "tools_executed": False}
try:
    if args.stream:
        size = 0
        while True:
            raw = response.readline(64 * 1024)
            size += len(raw)
            if size > 1024 * 1024:
                raise RuntimeError("probe response too large")
            if not raw or raw.strip() == b"data: [DONE]":
                break
            if not raw.startswith(b"data: "):
                continue
            value = json.loads(raw[6:])
            for choice in value.get("choices", []):
                delta = choice.get("delta", {})
                # Exclude all reasoning fields from recorded/output evidence.
                result["content"] += delta.get("content") or ""
                result["tool_calls"].extend(delta.get("tool_calls") or [])
                if choice.get("finish_reason"):
                    result["finish_reasons"].append(choice["finish_reason"])
    else:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("probe response too large")
        value = json.loads(raw)
        for choice in value.get("choices", []):
            message = choice.get("message", {})
            result["content"] += message.get("content") or ""
            result["tool_calls"].extend(message.get("tool_calls") or [])
            result["finish_reasons"].append(choice.get("finish_reason"))
finally:
    connection.close()
write_json(args.output, result)
if args.request:
    print(json.dumps({"status": result["status"], "finish_reasons": result["finish_reasons"],
                      "content_bytes": len(result["content"].encode()), "tools_executed": False,
                      "calls": [{"name": c.get("function", {}).get("name"), "index": c.get("index"),
                                 "argument_bytes": len(c.get("function", {}).get("arguments", "").encode())}
                                for c in result["tool_calls"]]}, ensure_ascii=False))
else:
    print(json.dumps(result, ensure_ascii=False))
