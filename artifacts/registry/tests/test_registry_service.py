from __future__ import annotations

import copy
import http.client
import importlib.util
import json
import threading
import unittest
from unittest import mock
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
MODULE_PATH = BASE / "registry_service.py"
SPEC = importlib.util.spec_from_file_location("registry_service", MODULE_PATH)
assert SPEC and SPEC.loader
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


class FakeEmbedder:
    provider_name = "test-fixture-embedder"
    model = "test-fixture-model"
    dimensions = registry.EMBEDDING_DIMENSIONS

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise registry.EmbeddingError("synthetic provider outage")
        vectors = []
        for text in texts:
            folded = text.casefold()
            vector = [0.0] * registry.EMBEDDING_DIMENSIONS
            vector[0] = 1.0 if any(term in folded for term in ("focus", "task list", "progress", "专注", "任务")) else 0.0
            vector[1] = 1.0 if any(term in folded for term in ("notes", "checklist", "笔记", "清单")) else 0.0
            vector[2] = 1.0 if any(term in folded for term in ("inventory", "ledger", "库存")) else 0.0
            vector[3] = 1.0 if any(term in folded for term in ("alarm", "schedule", "reminder", "闹钟", "提醒")) else 0.0
            if not any(vector[:4]):
                vector[4] = 1.0
            vectors.append(vector)
        return vectors


class FakeResponse:
    def __init__(self, payload: bytes, content_length: str | None = None) -> None:
        self.payload = payload
        self.headers = {
            "Content-Length": content_length or str(len(payload)),
            "Content-Type": "application/json",
        }

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self.payload[:maximum]


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def open(self, request: object, timeout: float) -> FakeResponse:
        return self.response


def query(need: dict, **constraint_changes: object) -> tuple[dict, dict]:
    constraints = registry.default_constraints()
    constraints["embedding_consent"] = "granted"
    constraints.update(constraint_changes)
    return need, constraints


class RegistryServiceTests(unittest.TestCase):
    def test_default_product_search_never_recommends_synthetic_apps(self):
        embedder = FakeEmbedder()
        with mock.patch.object(registry, "cli_embedder", return_value=embedder), mock.patch.object(registry, "emit") as emit:
            result = registry.main(["search", "--need", str(BASE / "fixtures/needs/focus.json"), "--embedding-consent"])
        self.assertEqual(result, 0)
        self.assertEqual(emit.call_args.args[0]["route"], "refinement")
        self.assertEqual(emit.call_args.args[0]["recommendations"], [])
        self.assertEqual(embedder.calls, 0)

    @classmethod
    def setUpClass(cls) -> None:
        cls.records = registry.build_records()
        cls.focus_need = registry.validate_need(registry.load_json(BASE / "fixtures" / "needs" / "focus.json", registry.MAX_NEED_BYTES))
        cls.no_match_need = registry.validate_need(registry.load_json(BASE / "fixtures" / "needs" / "no-match.json", registry.MAX_NEED_BYTES))
        cls.unrelated_need = registry.validate_need(registry.load_json(BASE / "fixtures" / "needs" / "unrelated.json", registry.MAX_NEED_BYTES))

    def test_catalog_is_bounded_and_stable(self) -> None:
        self.assertEqual(len(self.records), 5)
        self.assertEqual(registry.canonical_bytes(self.records), registry.canonical_bytes(registry.build_records()))

    def test_hard_filters_run_before_embedding(self) -> None:
        embedder = FakeEmbedder()
        need, constraints = query(self.no_match_need)
        result = registry.search(need, constraints, self.records, embedder, 5)
        self.assertEqual(result["route"], "refinement")
        self.assertEqual(result["refinement"]["reason_code"], "no-hard-filter-match")
        self.assertEqual(embedder.calls, 0)
        self.assertEqual(result["retrieval"]["embedding"]["status"], "not-needed")
        self.assertFalse(result["codeagent_handoff"]["created"])

    def test_embedding_consent_is_fail_closed(self) -> None:
        embedder = FakeEmbedder()
        constraints = registry.default_constraints()
        result = registry.search(self.focus_need, constraints, self.records, embedder, 5)
        self.assertEqual(result["route"], "refinement")
        self.assertEqual(result["refinement"]["reason_code"], "embedding-consent-required")
        self.assertEqual(embedder.calls, 0)

    def test_keyword_and_embedding_can_recommend_existing_app(self) -> None:
        embedder = FakeEmbedder()
        need, constraints = query(self.focus_need)
        result = registry.search(need, constraints, self.records, embedder, 5)
        self.assertEqual(result["route"], "recommendation")
        self.assertEqual(result["recommendations"][0]["app"]["id"], "ai.vibapp.fixture.focus-board")
        self.assertGreater(result["recommendations"][0]["scores"]["embedding_cosine"], 0.7)
        self.assertEqual(result["retrieval"]["mode"], "hard-filter-then-keyword-plus-embedding")
        self.assertFalse(result["codeagent_handoff"]["permitted"])

    def test_forbidden_package_cannot_be_reintroduced_by_embedding(self) -> None:
        need = copy.deepcopy(self.focus_need)
        need["negative_constraints"].append({
            "constraint_id": "constraint.reject-focus",
            "kind": "forbidden-package",
            "value": "ai.vibapp.fixture.focus-board",
            "source": "rejection-feedback",
        })
        embedder = FakeEmbedder()
        need, constraints = query(need)
        result = registry.search(need, constraints, self.records, embedder, 5)
        recommended = {item["app"]["id"] for item in result["recommendations"]}
        self.assertNotIn("ai.vibapp.fixture.focus-board", recommended)
        rejection = next(item for item in result["rejected"] if item["app_id"] == "ai.vibapp.fixture.focus-board")
        self.assertIn("negative-constraint", rejection["rejected_reasons"])

    def test_kind_and_required_capability_are_hard_filters(self) -> None:
        embedder = FakeEmbedder()
        need, constraints = query(
            self.focus_need,
            acceptable_app_kinds=["service"],
            required_interfaces=[registry.interface_name("notification")],
        )
        result = registry.search(need, constraints, self.records, embedder, 5)
        self.assertEqual(result["route"], "refinement")
        self.assertEqual(embedder.calls, 0)
        reasons = {reason for item in result["rejected"] for reason in item["rejected_reasons"]}
        self.assertIn("app-kind", reasons)
        self.assertIn("required-capability", reasons)

    def test_embedding_failure_is_degraded_refinement_not_lexical_fallback(self) -> None:
        embedder = FakeEmbedder(fail=True)
        need, constraints = query(self.focus_need)
        result = registry.search(need, constraints, self.records, embedder, 5)
        self.assertEqual(result["route"], "refinement")
        self.assertEqual(result["refinement"]["reason_code"], "embedding-provider-unavailable")
        self.assertEqual(result["retrieval"]["embedding"]["status"], "degraded")
        self.assertEqual(result["recommendations"], [])
        self.assertFalse(result["codeagent_handoff"]["created"])

    def test_no_acceptable_semantic_match_routes_to_refinement(self) -> None:
        embedder = FakeEmbedder()
        need, constraints = query(self.unrelated_need)
        result = registry.search(need, constraints, self.records, embedder, 5)
        self.assertEqual(result["route"], "refinement")
        self.assertEqual(result["refinement"]["reason_code"], "no-acceptable-semantic-match")
        self.assertEqual(result["recommendations"], [])
        self.assertTrue(result["considered"])

    def test_invalid_embedding_dimensions_fail_closed_and_release_slot(self) -> None:
        payload = registry.canonical_bytes({
            "model": registry.EMBEDDING_MODEL,
            "data": [{"index": 0, "embedding": [0.0] * (registry.EMBEDDING_DIMENSIONS - 1)}],
        })
        embedder = registry.LocalAIEmbedder()
        embedder._opener = FakeOpener(FakeResponse(payload))
        with self.assertRaisesRegex(registry.EmbeddingError, "dimensions"):
            embedder.embed(["bounded test"])
        self.assertTrue(embedder._semaphore.acquire(blocking=False))
        embedder._semaphore.release()

    def test_embedding_response_size_header_is_rejected(self) -> None:
        embedder = registry.LocalAIEmbedder()
        embedder._opener = FakeOpener(FakeResponse(b"{}", str(registry.MAX_EMBEDDING_RESPONSE_BYTES + 1)))
        with self.assertRaisesRegex(registry.EmbeddingError, "response exceeds"):
            embedder.embed(["bounded test"])

    def test_byom_embedding_configuration_and_api_key_are_applied(self) -> None:
        config = registry.embedding_config({
            "enabled": True,
            "base_url": "https://models.example.test/v1",
            "model": "custom-embedding-model",
            "dimensions": 8,
            "timeout_seconds": 7,
            "api_key": "test-secret",
        })
        embedder = registry.LocalAIEmbedder(**config)
        self.assertEqual(embedder.url, "https://models.example.test/v1/embeddings")
        self.assertEqual(embedder.model, "custom-embedding-model")
        self.assertEqual(embedder.dimensions, 8)
        self.assertTrue(embedder.provider_name.startswith("openai-compatible-byom:"))

    def test_remote_cleartext_embedding_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(registry.RegistryError, "must use HTTPS"):
            registry.embedding_endpoint("http://models.example.test/v1")

    def test_duplicate_json_and_oversized_input_are_rejected(self) -> None:
        with self.assertRaisesRegex(registry.RegistryError, "duplicate JSON key"):
            registry.decode_json(b'{"a":1,"a":2}', registry.MAX_NEED_BYTES)
        with self.assertRaisesRegex(registry.RegistryError, "input exceeds"):
            registry.decode_json(b" " * (registry.MAX_NEED_BYTES + 1), registry.MAX_NEED_BYTES)

    def test_http_endpoint_returns_bounded_route(self) -> None:
        embedder = FakeEmbedder()
        server = registry.BoundedServer(("127.0.0.1", 0), self.records, embedder)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            constraints = registry.default_constraints()
            constraints["embedding_consent"] = "granted"
            payload = registry.canonical_bytes({"need": self.focus_need, "constraints": constraints, "limit": 3})
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("POST", "/v1/registry/search", body=payload, headers={"Content-Type": "application/json", "Content-Length": str(len(payload))})
            response = connection.getresponse()
            raw = response.read(registry.MAX_OUTPUT_BYTES + 1)
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertLessEqual(len(raw), registry.MAX_OUTPUT_BYTES)
            decoded = json.loads(raw)
            self.assertEqual(decoded["route"], "recommendation")
            self.assertFalse(decoded["codeagent_handoff"]["created"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_rejects_oversized_declared_body_without_reading_it(self) -> None:
        server = registry.BoundedServer(("127.0.0.1", 0), self.records, FakeEmbedder())
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("POST", "/v1/registry/search", body=b"{}", headers={"Content-Type": "application/json", "Content-Length": str(registry.MAX_REQUEST_BYTES + 1)})
            response = connection.getresponse()
            decoded = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 400)
            self.assertEqual(decoded["error"]["code"], "invalid-argument")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
