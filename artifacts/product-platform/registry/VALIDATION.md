# Validation record

Status: **PRODUCT PLATFORM - LOCAL / HOLD — locally exercised, not independently accepted**

Date: 2026-08-27 (Asia/Shanghai)

## Evidence

```text
python3 -m py_compile artifacts/product-platform/registry/registry_service.py
PASS (exit 0)

python3 -m unittest discover -s artifacts/product-platform/registry/tests -p 'test_*.py' -v
Ran 11 tests in 0.043s — OK

python3 artifacts/schema-contract-proposal/validate_schemas.py
PASS schemas=9 fixtures=43 positive=30 negative=13

python3 artifacts/product-platform/registry/registry_service.py search \
  --need artifacts/product-platform/registry/fixtures/needs/no-match.valid.json --limit 5
matched=0 rejected=5 empty_reason="No candidate passed every hard filter."
```

The test suite validates all five expanded Registry records, the emitted
`search-response`, and each `candidate-match` against the authored product schema
candidate using its dependency-free validator. It also covers deterministic ordering,
hard-filter non-reintroduction, empty results, duplicate keys, `NaN`, over-size input,
unknown fields, control characters and closed CLI errors. The eleventh test validates
the synthetic browser artifact's exact Registry record/package binding, Component
reference, artifact digest/size and explicit no-transformation-proof state.

## Snapshot

```text
path: snapshots/registry.snapshot.json
bytes: 63256
sha256: 57f62dd1efd570b203ab2e3e9adb1bf25f621d78a872c78a1aad9ddb4d3fbed0
records: 5
browser artifact bindings: 1
sample eligible matches: 2
sample hard-filtered records: 3
```

Re-running the snapshot command produced the same byte count and digest.

## Public transport locator boundary

The Website publication synchronizer now consumes only digest-named sidecars from
`package-locators/`. Each emitted entry binds the package digest to a published,
verified, non-revoked public record id/revision plus canonical-record SHA-256, and
the index binds to the exact public Website Registry snapshot SHA-256. Eight focused
Node tests pass for valid binding, private rejection with prior-snapshot retention,
canonical tracker enforcement, incomplete-record rejection, deterministic empty
output, stale-file removal, whole-data-root replacement/rollback, and Website
build-hook coverage. Website sync now also emits the separate stable
`generated/production-public-data` root on local and production builds, so Desktop
and packaging never consume the mode-dependent Website projection. The current source has
no locator sidecars, so production output is intentionally an empty index.

## Unexecuted / unavailable

- No production Registry, network, account, database, vector model or public API was
  contacted or tested.
- No real package, manifest, Component import set, evidence file or signature was
  independently verified; all records are explicit synthetic fixtures.
- The browser binding is a verified synthetic Registry fixture. It does not prove
  that a production trusted builder transformed the referenced canonical Component.
- No production retrieval corpus, relevance benchmark, moderation policy or trust root
  exists here.
- The product schema candidate has not passed promotion or fresh independent contract
  acceptance. This implementing agent cannot mark it accepted.
