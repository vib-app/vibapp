from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import build_server  # noqa: E402


class ClientProductPlatformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = build_server("127.0.0.1", 0, Path(self.temporary.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def get(self, path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.status, response.read()

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_health_and_static_client(self) -> None:
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["mode"], "product-platform-local")
        self.assertFalse(health["real_installation"])
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("VibApp Client", body.decode("utf-8"))

    def test_state_exposes_all_verification_states(self) -> None:
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        states = {job["verification"]["status"] for job in state["jobs"]}
        self.assertEqual(states, {"quarantined", "verified", "rejected"})
        self.assertFalse(state["meta"]["installation_performed"])

    def test_submit_need_writes_real_local_handoff(self) -> None:
        status, body = self.post(
            "/api/needs",
            {"title": "会议结论整理", "description": "把我粘贴的会议记录整理成结论和待办事项。"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["need"]["status"], "ready-for-builder")
        outbox = Path(self.temporary.name) / "outbox" / "need-requests.jsonl"
        row = json.loads(outbox.read_text(encoding="utf-8").strip())
        self.assertEqual(row["need_id"], body["need"]["need_id"])

    def test_invalid_need_is_an_error_state(self) -> None:
        status, body = self.post("/api/needs", {"title": "", "description": "太短"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_title")

    def test_verified_candidate_records_intent_without_installing(self) -> None:
        status, body = self.post(
            "/api/install-intents",
            {
                "app_id": "ai.vibapp.water-reminder",
                "package_digest_sha256": "a3f7c912fb6e70b8a7f36f5ed6bc5f354602d107bec3d69fd3dbde49681b52f0",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["intent"]["status"], "intent-recorded")
        self.assertFalse(body["intent"]["installation_performed"])
        self.assertEqual(body["intent"]["next_authority"], "future-daemon")

    def test_rejected_candidate_cannot_create_intent(self) -> None:
        status, body = self.post(
            "/api/install-intents",
            {
                "app_id": "ai.vibapp.weather-broadcast",
                "package_digest_sha256": "cc33c9c5e555845849de8d4dde9f82a4f868f37f159414ba3d30a9ee5a3d538b",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_install_eligible")


if __name__ == "__main__":
    unittest.main()
