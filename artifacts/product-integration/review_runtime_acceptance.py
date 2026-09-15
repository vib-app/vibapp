#!/usr/bin/env python3
"""Real candidate -> private install -> advertised UI actions -> persisted reopen.

No model, source rewriting, synthetic guest or publication. Actions are selected
from the actual guest's semantic surface. Durable private evidence is retained.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time

from end_to_end_smoke import LocalAppStore, RuntimeDaemon, accepted, envelope


def duration_seconds(value):
    """Parse one visible nonnegative duration, never a label or hidden field."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{2,3}:[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,3})?", value) is None:
        raise ValueError("Expected bounded HH:MM:SS[.sss] rendered duration")
    hours, minutes, seconds = value.split(":")
    return Decimal(hours) * 3600 + Decimal(minutes) * 60 + Decimal(seconds)


class RuntimeReview:
    def __init__(self, candidate, output):
        self.output = output
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.store = LocalAppStore(output / "appstore")
        promotion = self.store.ingest(candidate)
        source = json.loads(candidate.read_bytes())
        self.digest = source["package_digest_sha256"]
        record = self.store.detail(self.digest)["record"]
        assert record["state"] == "private" and record["publication"]["state"] == "not-published"
        self.candidate = self.store.root / record["paths"]["candidate"]
        self.app_id = record["app"]["id"]
        manifest = json.loads((self.candidate.parent / "package/manifest.json").read_bytes())
        self.entry = next(item for item in manifest["entrypoints"] if item["kind"] == "launcher-ui")
        self.daemon = RuntimeDaemon(output / "runtime", self.store.candidates)
        self.sequence = 0
        self.binding = None
        self.saved = {}
        self.snapshots = []
        self.call("install", {"package_digest_sha256": self.digest, "enable_after_install": False}, subject=None, promotion=self.candidate)
        self.call("enable", None)
        self.launch()

    def call(self, tag, value, *, subject="app", promotion=None):
        self.sequence += 1
        return accepted(self.daemon, tag, value, self.app_id if subject == "app" else subject, self.sequence, promotion)

    def launch(self):
        self.binding = self.call("launch", {"entrypoint": self.entry["id"], "route": self.entry["routes"]["initial"]})
        self.snapshot("launch")

    def snapshot(self, label):
        surface = self.binding["semantic_surface"]
        self.snapshots.append({"label": label, "surface": surface})
        (self.output / "surfaces.json").write_text(json.dumps(self.snapshots, ensure_ascii=False, indent=2))
        return surface

    def event_binding(self):
        return {key: self.binding[key] for key in ("entrypoint", "package_digest_sha256", "component_sha256", "generation", "session", "surface", "route")}

    def step(self, action):
        operation = action["operation"]
        if operation in {"action", "assert-action-rejected"}:
            if operation == "assert-action-rejected":
                code, message = action.get("code"), action.get("message_contains")
                if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", code) is None:
                    raise ValueError("Expected rejection requires an explicit bounded error code")
                if not isinstance(message, str) or not 1 <= len(message) <= 512:
                    raise ValueError("Expected rejection requires a bounded nonempty message")
            nodes = self.binding["semantic_surface"]["view"]["nodes"]
            selector = action.get("node")
            if "node" in action and (not isinstance(selector, str) or not 1 <= len(selector) <= 256):
                raise ValueError("Button node selector must be a bounded nonempty ID")
            buttons = [(node["id"], node["kind"].get("button", node["kind"].get("Button"))) for node in nodes]
            buttons = [(node_id, button) for node_id, button in buttons if button is not None]
            for node in nodes:
                confirmation = node["kind"].get("confirmation", node["kind"].get("Confirmation"))
                if confirmation:
                    for prefix in ("confirm", "cancel"):
                        # WIT supplies actions, while Client owns these localized labels.
                        buttons.append((node["id"], {"label": "确认" if prefix == "confirm" else "取消", "action": confirmation.get(prefix + "_action"), "disabled": False}))
            matches = [button for node_id, button in buttons if button.get("label") == action["label"] and not button.get("disabled") and (selector is None or node_id == selector)]
            if len(matches) != 1:
                raise ValueError("Expected one advertised enabled button: " + action["label"])
            fields = action.get("fields", [])
            payload = {**self.event_binding(), "action": matches[0]["action"], "fields": fields, "event_id": f"review-event-{self.sequence + 1}"}
            if operation == "assert-action-rejected":
                self.sequence += 1
                response = self.daemon.execute(
                    envelope("ui-action", payload, self.app_id, self.sequence),
                    principal="uid:product-integration", allowed_apps={"*"},
                )
                error = response.get("error") if isinstance(response, dict) else None
                assert isinstance(error, dict) and set(error) == {"code", "message", "retryable"}, "Expected one structured daemon rejection"
                assert "outcome" not in response and error["code"] == code and error["retryable"] is False, "Unexpected daemon outcome or rejection code"
                assert isinstance(error["message"], str) and message in error["message"], "Daemon rejection message differs"
                self.snapshots.append({"label": "structured-action-rejection", "error": error})
            else:
                self.binding = self.call("ui-action", payload)
        elif operation == "refresh":
            delay = action.get("after_seconds", 1.1)
            if not isinstance(delay, (int, float)) or not 1.05 <= delay <= 3:
                raise ValueError("Refresh delay must be 1.05..3 seconds")
            time.sleep(delay)
            self.binding = self.call("ui-refresh", {**self.event_binding(), "event_id": f"review-event-{self.sequence + 1}"})
        elif operation == "reopen":
            self.call("surface-close", {"session": self.binding["session"], "surface": self.binding["surface"]})
            self.daemon.shutdown()
            self.daemon = RuntimeDaemon(self.output / "runtime", self.store.candidates)
            self.launch()
        elif operation == "assert-text":
            content = json.dumps(self.binding["semantic_surface"], ensure_ascii=False)
            assert action["contains"] in content, "Expected visible content missing: " + action["contains"]
        elif operation in {"assert-field", "assert-field-error"}:
            fields = [node["kind"].get("Field", node["kind"].get("field")) for node in self.binding["semantic_surface"]["view"]["nodes"]]
            matches = [field for field in fields if field and field["field"] == action["field"]]
            if len(matches) != 1:
                raise ValueError("Expected one actual field: " + action["field"])
            field = matches[0]
            if operation == "assert-field":
                variants = {"text": "Text", "choice": "Choice"}
                kind = action.get("value_kind", "text")
                if not isinstance(kind, str) or kind not in variants or not isinstance(action.get("value"), str):
                    raise ValueError("Field assertion requires an explicit text/choice string value")
                value = field["value"]
                if not isinstance(value, dict) or len(value) != 1 or not any(key in value for key in (kind, variants[kind])):
                    raise ValueError("Field value variant differs from the requested assertion kind")
                assert value.get(variants[kind], value.get(kind)) == action["value"], "Saved typed field value did not round-trip"
            else:
                if ("equals" in action) == ("contains" in action):
                    raise ValueError("Field error needs exactly one of equals/contains")
                expected = action.get("equals", action.get("contains"))
                if not isinstance(expected, str) or not expected or len(expected) > 4096:
                    raise ValueError("Field error expectation must be a bounded nonempty string")
                actual = field.get("validation_message")
                assert isinstance(actual, str) and actual, "Expected a visible field validation error"
                assert actual == expected if "equals" in action else expected in actual, "Field validation error differs"
        elif operation == "assert-node-count":
            prefix, expected = action.get("prefix"), action.get("expected")
            if not isinstance(prefix, str) or not 1 <= len(prefix) <= 256 or type(expected) is not int or not 0 <= expected <= 2048:
                raise ValueError("Node count requires a bounded nonempty ID prefix and count 0..2048")
            kinds = {"Text": "text", "Button": "button", "Field": "field", "ListContainer": "list-container", "Progress": "progress", "Confirmation": "confirmation"}
            kind = action.get("kind")
            if kind is not None and (not isinstance(kind, str) or kind not in kinds):
                raise ValueError("Node count kind must name one semantic node variant")
            nodes = [node for node in self.binding["semantic_surface"]["view"]["nodes"] if node["id"].startswith(prefix)]
            if kind is not None:
                nodes = [node for node in nodes if kind in node["kind"] or kinds[kind] in node["kind"]]
            assert len(nodes) == expected, f"Rendered node count differs for {prefix}: {len(nodes)} != {expected}"
        elif operation in {"assert-node-text", "assert-node-number", "assert-node-duration"}:
            nodes = [node for node in self.binding["semantic_surface"]["view"]["nodes"] if node["id"] == action["node"]]
            if len(nodes) != 1:
                raise ValueError("Expected one actual text node: " + action["node"])
            text = nodes[0]["kind"].get("Text", nodes[0]["kind"].get("text"))
            if not isinstance(text, dict) or not isinstance(text.get("text"), str):
                raise ValueError("Selected node is not rendered text")
            actual = text["text"]
            if operation == "assert-node-duration":
                number = duration_seconds(actual)
                if "relative_to" in action:
                    key = action["relative_to"]
                    if not isinstance(key, str) or key not in self.saved:
                        raise ValueError("Duration baseline must name a previously remembered node")
                    remembered = json.loads(self.saved[key])
                    text = remembered.get("Text", remembered.get("text"))
                    if not isinstance(text, dict):
                        raise ValueError("Duration baseline must be rendered text")
                    number -= duration_seconds(text.get("text"))
                try:
                    minimum = Decimal(str(action["minimum_seconds"]))
                    maximum = Decimal(str(action["maximum_seconds"]))
                except (InvalidOperation, ValueError, TypeError, KeyError):
                    raise ValueError("Duration assertion requires explicit finite bounds") from None
                if not minimum.is_finite() or not maximum.is_finite() or not -3600000 <= minimum <= maximum <= 3600000:
                    raise ValueError("Duration assertion bounds must be finite, ordered and within 1000 hours")
                assert minimum <= number <= maximum, f"Rendered duration outside [{minimum}, {maximum}] seconds: {number} ({actual})"
            elif operation == "assert-node-number":
                prefix, suffix = action.get("prefix", ""), action.get("suffix", "")
                if not isinstance(prefix, str) or not isinstance(suffix, str) or not actual.startswith(prefix) or not actual.endswith(suffix):
                    raise AssertionError("Rendered number unit/label differs: " + actual)
                core = actual[len(prefix):len(actual) - len(suffix) if suffix else len(actual)].strip()
                try:
                    number = Decimal(core)
                    expected = Decimal(str(action["expected"]))
                    tolerance = Decimal(str(action.get("absolute_tolerance", "0.00001")))
                except (InvalidOperation, ValueError, TypeError):
                    raise ValueError("Expected one finite rendered decimal") from None
                if not number.is_finite() or not expected.is_finite() or not tolerance.is_finite() or not Decimal(0) <= tolerance <= Decimal("0.0001"):
                    raise ValueError("Number assertion must be finite with tolerance at most 0.0001")
                assert abs(number - expected) <= tolerance, "Rendered number differs: " + actual
            elif ("equals" in action) == ("contains" in action):
                raise ValueError("Text assertion needs exactly one of equals/contains")
            elif "equals" in action:
                assert actual == action["equals"], "Rendered text differs: " + actual
            else:
                assert action["contains"] in actual, "Rendered text missing expected value: " + actual
        elif operation in {"remember-node", "assert-node-changed", "assert-node-unchanged"}:
            node = next(node for node in self.binding["semantic_surface"]["view"]["nodes"] if node["id"] == action["node"])
            value = json.dumps(node["kind"], ensure_ascii=False, sort_keys=True)
            key = action.get("key", action["node"])
            if operation == "remember-node":
                self.saved[key] = value
            else:
                changed = value != self.saved[key]
                assert changed == (operation == "assert-node-changed"), operation + " failed: " + action["node"]
        else:
            raise ValueError("Unknown review operation")
        self.snapshot(operation)

    def finish(self):
        self.call("disable", None)
        state = self.daemon.inspect_state()["apps"][self.app_id]
        assert state["enabled"] is False
        self.daemon.shutdown()
        return {"status": "PASS", "app_id": self.app_id, "package_digest_sha256": self.digest,
                "real_guest_execution": state["guest_execution_performed"], "publication_performed": False,
                "checks": len(self.snapshots), "disabled_after_review": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new private evidence directory")
    review = None
    try:
        review = RuntimeReview(args.candidate.resolve(strict=True), args.output.resolve())
        if args.plan and args.plan.stat().st_size > 1024 * 1024:
            raise ValueError("Review plan exceeds 1 MiB")
        plan = json.loads(args.plan.read_bytes()) if args.plan else [{"operation": "reopen"}]
        if not isinstance(plan, list) or len(plan) > 64:
            raise ValueError("Review plan must contain at most 64 operations")
        for action in plan:
            review.step(action)
        result = review.finish()
        result.update(plan_operations=len(plan), assertions=sum(str(item.get("operation", "")).startswith("assert-") for item in plan), surface_snapshots=result["checks"])
    except Exception as error:
        result = {"status": "FAIL", "error": str(error), "publication_performed": False}
        if review:
            try:
                review.call("disable", None)
            except Exception:
                pass
            review.daemon.shutdown()
        if not args.output.is_dir():
            raise
    (args.output / "runtime-review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
