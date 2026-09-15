# VibApp Registry + Need Intake

Status: **PRODUCT PLATFORM - LOCAL / HOLD**

This is a bounded, deterministic demonstration of requirement intake and Registry
matching. It is not the public VibApp Registry, does not publish or install apps,
and does not promote the product schema candidate into an accepted contract.

The local Registry consumes the candidate shapes in `artifacts/schema-contract-proposal` and
preserves the Stage 0 search invariant: package/contract/WASI/WIT, platform/profile,
permissions, trust, verification, revocation and publication checks run before any
score. Rejected records never enter ranking.

## Run

From the repository root:

```sh
python3 artifacts/product-platform/registry/registry_service.py validate-need \
  --need artifacts/product-platform/registry/fixtures/needs/focus.valid.json

python3 artifacts/product-platform/registry/registry_service.py search \
  --need artifacts/product-platform/registry/fixtures/needs/focus.valid.json \
  --limit 5

python3 artifacts/product-platform/registry/registry_service.py snapshot \
  --output artifacts/product-platform/registry/snapshots/registry.snapshot.json

python3 -m unittest discover \
  -s artifacts/product-platform/registry/tests -p 'test_*.py' -v
```

The snapshot is the shared local handoff for website and client prototypes. Its
`records` are synthetic examples; `sample_search` exposes `matched`, `unmet`, and
evidence-bound `explanation` fields. It makes no real verification or publication
claim even though fixture fields deliberately exercise those filter states.

`artifacts/web-client-core/sync-web-gui.mjs` filters this source into a stable,
generated `production-public-only` consumer root at
`generated/production-public-data/`. Desktop development and macOS packaging consume
that root, not the mode-dependent Website `public/data` directory. The generated root
contains `registry.snapshot.json` and its exact hash-bound `package-locators/` index;
both are staged and switched together on every Website sync, including local preview
syncs. It remains product-layer HOLD data and is not publication evidence.

`browser_artifact_bindings` is a snapshot-level product extension, kept outside
the strict `registry-record.product-v0` candidate. It content-addresses the runnable
browser fixture, binds it to the exact Registry record/package digest and a canonical
Component reference, and records that canonical Component transformation is **not**
proven. The only accepted policy value is `synthetic-fixture-only`; this is not a
production trusted-builder attestation.

## Local policy and bounds

- Need input: at most 64 KiB; duplicate keys, JSON constants such as `NaN`, unknown
  fields, control characters and invalid candidate-schema shapes are rejected.
- Corpus: at most 512 KiB and 50 records; search scans at most 50 records and returns
  at most 20.
- Output: at most 512 KiB; all ordering and digests are deterministic.
- `allowed_interfaces` is an actual ceiling: every candidate interface must be
  listed. A non-empty `maximum_scope_digests` additionally restricts scope digests.
- `local-private` rejects a candidate that declares the HTTP capability in this local profile.
  This is a deliberately conservative temporary policy, not a finalized privacy
  contract.
- “Vector” is a deterministic character-bigram similarity proxy, not an embedding
  model or production retrieval metric.
- No network, live provider, account, credential, install, deploy or publish path is
  present.

## Known holds

The schema packet is an unaccepted candidate; public Registry trust, moderation,
publisher verification, authentication, transport, corpus quality and quantitative
retrieval evaluation remain unresolved. A separate verifier and the applicable later
product gate are required before any production or acceptance claim.
