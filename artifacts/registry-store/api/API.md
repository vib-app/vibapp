# Registry store service adapter contract

Status: `experimental-product-hold`; no network service is deployed by this
artifact.

## Search adapter

The service endpoint remains `POST /v1/registry/search`. Its JSON body is exactly
the existing envelope:

```json
{
  "need": { "...": "vibapp.need-spec.product-v0.0.1" },
  "constraints": { "...": "existing Registry constraints" },
  "limit": 5
}
```

The service layer must:

1. reject duplicate keys, unknown fields, bodies over 96 KiB, or a limit outside
   `1..20` before opening a transaction;
2. call `registry.search_preflight_v1` with the exact model ID/version; return its
   response immediately when hard filters find no eligible candidate or embedding
   consent is absent;
3. only when preflight returns `needs_embedding=true`, obtain one 384-d query
   vector plus one vector per requirement from the configured provider;
4. call `registry.search_v1` with the exact model ID/version and optional opaque
   cursor;
5. return the function's existing-compatible Registry route envelope without
   inventing explanation facts;
6. preserve `codeagent_handoff.created=false` and
   `codeagent_handoff.permitted=false` on every branch.

Pagination is transport metadata, not a new body field. The caller may send an
opaque `X-VibApp-Registry-Cursor` header (maximum 2 KiB); the next opaque cursor is
returned at `retrieval.pagination.next_cursor`. Existing clients that omit the
header continue to use the exact body contract.

Database call shape:

```sql
SELECT registry.search_v1(
  request_jsonb             => $1::jsonb,
  embedding_model_id        => $2::text,
  embedding_model_version   => $3::text,
  query_embedding           => $4::vector(384),
  requirement_embeddings    => $5::jsonb,
  cursor_token              => $6::text
);
```

`requirement_embeddings` is an object keyed by every `requirement_id`; each value
is an array of exactly 384 finite numbers. A missing, extra, wrong-model, stale
cursor, or malformed vector fails closed. The database never calls an embedding
provider.

The response keeps these compatibility anchors:

- `schema_version=vibapp.registry-route.experimental.2026-08-24.1`
- `status=experimental-product-hold`
- `route=recommendation|refinement`
- `request_id`, `need_id`, `recommendations`, `refinement`, `retrieval`,
  `rejected`, and `codeagent_handoff`

The additive `retrieval.pagination` object contains `next_cursor` and
`stable_order`. No cursor grants access or authority; it only resumes the same
bounded query.

## Release ingestion adapter

Only a trusted metadata-ingestion service may call
`registry.upsert_release_v1(record_jsonb, idempotency_key, actor_principal)`.
The input must validate against `release-upsert-v1.schema.json` before the call.
The function repeats critical validation transactionally and records an audit
event.

An immutable version is inserted once. An exact replay is returned as
`deduplicated=true`. Reuse of the idempotency key with another payload, or reuse of
an app/version/package identity with changed immutable metadata, fails with SQLSTATE
`23514`/`23505`; callers must not translate either into success.

Publisher verification, verification evidence, revocation, publication state,
and authority grants are separate operations/tables. The initial upsert can create
only private, unpublished metadata unless independently issued evidence/authority
events are supplied by their respective trusted owners. Direct table ownership is
not an application-service permission.

## Public publication adapter

The trusted service uses `publication_service.publish_with_source_archive` with an
authenticated publisher ID and verifier-owned immutable source root. Never accept
a destination repository, source digest, archive receipt or GitHub token from the
browser or CodeAgent. Resolve source metadata through `source_archive_input_v1`.

Order: GitHub upload/read-back → `record_github_source_archive_v1` on an independent
archive-service connection → `publish_release_v1` on the publication connection.
The archive function is not granted to the publication or ingestion role. Its JSON
receipt has exactly `organization`, `repository`, `repository_id`, `commit_sha`,
`source_digest_sha256`, `package_digest_sha256`. The database resolves the owning
release and validates all bindings. Pending/failed uploads have no receipt row.

Publication without the matching receipt fails with SQLSTATE `23514` and
`source-archive-required`. Do not map upload errors, missing consent, rejected
verification or failed publication commits to success. Record the upload reason for
retry; an already archived source is not itself permission to publish. No public
network endpoint is provided by this local helper.

## Transaction and resource rules

- Search runs read-only with a service statement timeout at or below 2 seconds.
- Ingestion runs in one transaction with a bounded payload (256 KiB) and at most
  16 platforms, 4 profiles, 32 capabilities, 64 tags, and 64 evidence entries.
- The service role receives `EXECUTE` on approved functions and `SELECT` on safe
  views only, never table ownership or arbitrary write access.
- Audit payloads contain digests and bounded metadata, never prompts, secrets,
  credentials, plaintext sensitive settings, or generated source.
