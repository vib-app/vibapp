from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))


class RegistryStoreContractTests(unittest.TestCase):
    def test_migrations_are_versioned_unique_and_transactional(self) -> None:
        self.assertEqual([p.name for p in MIGRATIONS], [
            "0001_extensions_and_types.sql",
            "0002_registry_core.sql",
            "0003_ingestion_and_authority.sql",
            "0004_search_api.sql",
            "0005_github_source_archive.sql",
            "0006_shared_public_sources.sql",
        ])
        for path in MIGRATIONS:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("BEGIN;"), path)
            self.assertTrue(text.rstrip().endswith("COMMIT;"), path)

    def test_json_schemas_parse_and_are_strict_at_root(self) -> None:
        for path in sorted((ROOT / "api").glob("*.schema.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertIs(value["additionalProperties"], False)

    def test_embedding_dimension_and_model_version_are_bound(self) -> None:
        sql = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS)
        self.assertGreaterEqual(sql.count("vector(384)"), 5)
        self.assertRegex(sql, r"PRIMARY KEY \(release_id, model_id, model_version\)")
        self.assertIn("dimensions = 384", sql)
        self.assertIn("model_version = embedding_model_version", sql)

    def test_hard_filter_is_materialized_before_vector_scoring(self) -> None:
        sql = (ROOT / "migrations" / "0004_search_api.sql").read_text(encoding="utf-8")
        eligible_at = sql.index("eligible AS MATERIALIZED")
        scoring_at = sql.index("base_scored AS MATERIALIZED", eligible_at)
        self.assertLess(eligible_at, scoring_at)
        hard_section = sql[eligible_at:scoring_at]
        self.assertIn("hard_filter_reasons_v1", hard_section)
        self.assertNotIn("<=>", hard_section)

    def test_public_remote_and_private_authorities_are_distinct(self) -> None:
        sql = (ROOT / "migrations" / "0001_extensions_and_types.sql").read_text(encoding="utf-8")
        for authority in ("private-store", "remote-process", "public-list", "public-install"):
            self.assertIn(authority, sql)
        search_sql = (ROOT / "migrations" / "0004_search_api.sql").read_text(encoding="utf-8")
        self.assertIn("authority_kind = 'public-list'", search_sql)
        self.assertIn("authority_kind = 'public-install'", search_sql)
        hard_filter = search_sql[search_sql.index("hard_filter_reasons_v1"):search_sql.index("safe_rejections_v1")]
        self.assertNotIn("authority_kind = 'remote-process'", hard_filter)

    def test_route_compatibility_and_no_codeagent_authority(self) -> None:
        sql = (ROOT / "migrations" / "0004_search_api.sql").read_text(encoding="utf-8")
        for field in (
            "schema_version", "status", "route", "request_id", "need_id",
            "recommendations", "refinement", "retrieval", "rejected", "codeagent_handoff",
        ):
            self.assertIn(f"'{field}'", sql)
        self.assertGreaterEqual(sql.count("'created',false,'permitted',false"), 3)
        self.assertNotRegex(sql.casefold(), re.compile(r"\b(insert|update)\s+.*codeagent"))

    def test_queries_have_stable_tie_break_and_cursor_binding(self) -> None:
        sql = (ROOT / "migrations" / "0004_search_api.sql").read_text(encoding="utf-8")
        self.assertIn("ORDER BY r.final_score DESC, r.app_id, r.version, r.package_digest_sha256", sql)
        for field in ("request_digest_sha256", "model_id", "model_version", "score", "app_id", "version", "package_digest_sha256"):
            self.assertIn(field, sql)

    def test_dirty_metadata_fail_closed_rules_exist(self) -> None:
        sql = (ROOT / "migrations" / "0003_ingestion_and_authority.sql").read_text(encoding="utf-8")
        for phrase in (
            "missing or unknown nested fields",
            "capability interfaces do not exactly match the selected WIT world",
            "immutable app version metadata cannot be changed",
            "idempotency key was reused with a different payload",
            "embedding content digest does not match the canonical search document",
        ):
            self.assertIn(phrase, sql)

    def test_integration_waits_for_final_postgres_startup(self) -> None:
        runner = (ROOT / "scripts" / "run-integration.sh").read_text(encoding="utf-8")
        marker = "PostgreSQL init process complete; ready for start up."
        self.assertIn(marker, runner)
        self.assertLess(runner.index(marker), runner.index('pg_isready -U postgres -d postgres'))

    def test_least_privilege_template_uses_supported_fail_closed_psql_commands(self) -> None:
        grants = (ROOT / "deployment" / "least-privilege-grants.sql.example").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\\error", grants)
        self.assertNotIn("\\quit", grants)
        self.assertIn("\\set ON_ERROR_STOP on", grants)
        self.assertEqual(grants.count("RAISE EXCEPTION"), 2)
        self.assertIn("registry_search_role must be supplied", grants)
        self.assertIn("registry_ingest_role must be supplied", grants)


if __name__ == "__main__":
    unittest.main()
