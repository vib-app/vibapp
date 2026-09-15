import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "retry_failed_samples.py"
SPEC = importlib.util.spec_from_file_location("retry_failed_samples", SCRIPT)
assert SPEC and SPEC.loader
retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retry)


class RetryFailedSamplesSafetyTests(unittest.TestCase):
    def test_real_provider_requires_two_independent_cli_flags(self):
        with self.assertRaisesRegex(RuntimeError, "both --enable-real-provider"):
            retry.main([])
        with self.assertRaisesRegex(RuntimeError, "both --enable-real-provider"):
            retry.main(["--enable-real-provider"])
        with self.assertRaisesRegex(RuntimeError, "both --enable-real-provider"):
            retry.main(["--acknowledge-external-cost"])

    def test_bridge_health_requires_exact_current_contracts_and_lifecycle(self):
        digest = "a" * 64
        response = {
            "ok": True,
            "result": {
                "schema_version": "vibapp.product-bridge.experimental-v1",
                "service": "vibapp-product-bridge",
                "build_input_receipt": {
                    "schema_version": "vibapp.desktop-build-input-receipt.experimental-v1",
                    "sha256": digest,
                },
                "contracts": dict(retry.EXPECTED_BRIDGE_CONTRACTS),
                "pipeline_budget_seconds": 2700,
                "bridge_lifetime_seconds": 2760,
                "bridge_lifetime_margin_seconds": 60,
                "worker_cleanup_budget_seconds": 20,
                "lifecycle": "cancel-join-confirm",
            },
        }
        retry.validate_bridge_health_response(response, digest)
        response["result"]["contracts"]["cloud_codeagent_task"] = (
            "vibapp.cloud-codeagent-task.experimental-v1"
        )
        with self.assertRaisesRegex(RuntimeError, "product-bridge-health-mismatch"):
            retry.validate_bridge_health_response(response, digest)


if __name__ == "__main__":
    unittest.main()
