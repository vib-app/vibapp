# Registry store validation record

Date: 2026-08-24 Asia/Shanghai  
State: independently accepted for local integration only, `experimental-product-hold`;
not deployed, published, production-connected, production-accepted, or promoted
through a Stage 0 Gate.

## Static contract checks

```bash
python3 -m unittest discover \
  -s artifacts/registry-store/tests -p 'test_*.py' -v
find artifacts/registry-store -type f -name '*.json' -print0 \
  | xargs -0 -n1 jq empty
```

Result: 10/10 tests passed and both JSON Schemas parsed. The tests check ordered,
transactional migrations; strict root schemas; 384-d model/version binding;
`MATERIALIZED` hard filtering before any cosine operator; separate private,
remote-processing, public-listing and public-install authorities; response-envelope
compatibility; no CodeAgent authority; stable tie-break/cursor binding; and explicit
dirty-metadata rejection rules. They also require the integration runner to wait for
the official image's final initialization marker before accepting `pg_isready`, which
prevents racing the entrypoint's temporary bootstrap server. The least-privilege
template also binds `ON_ERROR_STOP` and uses SQL exceptions for missing-role inputs.

## Ephemeral PostgreSQL/pgvector integration

The exact pre-existing local image `pgvector/pgvector:pg16` was available. No image
was pulled. The runner used one temporary container with `--pull=never`,
`--network none`, `--memory 1g`, `--cpus 2`, `--pids-limit 64`, no host port, a
512 MiB tmpfs data directory, and serial migration/test execution. The container
was removed by the runner's exact-name trap; no Registry test container remained.

```bash
bash artifacts/registry-store/scripts/run-integration.sh
```

Result: all four migrations applied under `ON_ERROR_STOP`; the final SQL assertion
was `registry-store integration PASS`. Exercised behavior included:

- first immutable release upsert plus exact idempotent replay;
- same idempotency key/different payload rejection;
- WIT/capability mismatch and zero-norm embedding rejection;
- 384-d embedding storage bound to exact model ID and version;
- verified publisher/evidence state kept separate from release metadata;
- remote-processing authority proven insufficient for publication;
- public-list and public-install authority both required before publication;
- hard-filter preflight stopping before embedding when the permission ceiling
  rejects every release;
- full-text plus pgvector recommendation and unrelated semantic refinement;
- deterministic keyset pagination across equal-score releases;
- revocation withdrawing a release and preventing ranking from reintroducing it;
- response truth labels retaining `codeagent_handoff.created=false` and
  `permitted=false`.

## Independent local acceptance

A fresh evaluator reran the current bytes. The exact two-role least-privilege template
succeeded, each missing-role path failed closed with exit code 3 and its exact message,
and the search/ingest/PUBLIC ACL positive-negative matrix passed. Static checks were
10/10, the full SQL integration passed, and no named test container remained. Verdict:
`PASS-for-local-integration`; production and deployment remain unaccepted.

## Limits and next verification

This is a two-record synthetic integration, not a relevance benchmark, scale test,
failover test, migration rollback rehearsal, moderation proof, trust-root proof,
tenant-isolation test, production security assessment, or deployment evidence.
Production work additionally needs authenticated service transport,
separate operational roles, backups, observability, labeled retrieval metrics,
capacity/latency testing, and real authority/evidence sources.

## 2026-08-27 local AppStore author validation

The private local intake was added after the independent PostgreSQL validation
above. This is author-run local validation, not an independent acceptance or a
public-Registry claim.

```bash
python3 -m py_compile \
  artifacts/registry-store/local_appstore.py \
  artifacts/registry-store/tests/test_local_appstore.py

python3 -m unittest discover \
  -s artifacts/registry-store/tests -p 'test_*.py' -v
```

Result: Python compilation passed and 20/20 Registry tests passed (the original
10 storage-contract tests plus 10 local intake tests). The new cases cover exact
candidate-v1 authority, private-only ingest, complete package rehashing,
candidate-record/quarantine-receipt/verification-block binding, idempotent replay, same-digest/different-
payload conflict, immutable app-version conflict, symlink/hardlink/path-escape
denial, source and stored-byte tamper detection, concurrent CLI ingest, bounded
stable-cursor listing, and evidence-preserving withdrawal. No candidate was
installed, published, signed, deployed, or sent to an external service.

The frozen WS02 verifier output was then consumed without editing:

```text
input candidate:
artifacts/app-builder/demo-output/pipeline/candidates/
dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674/
candidate.json

ingest: created=true, state=private, publication.state=not-published
package: dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674
component: d3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa
manifest: 4d65de0514c48953da5c40c782bf19eb3b8296845ce5e22f4bec8968debaf9be
quarantine receipt: 35f11d52b1238b7a88dce8e60e980edb1bc4a4a9fc66680f974a510e4e052de2
verifier-promoted candidate record: df6214d8135632fdc346f4898a57634467a79e5d93f0498c26d79ad73efff194
replay: created=false
list/detail: one private ai.vibapp.hello@0.1.0 record; four files rehashed
```

The smoke Store was a temporary local directory and was not a deployment,
installation, or durable project artifact.

## 2026-09-06 public source-archive gate (author-run)

Migration 0005 and `publication_service.py` were added under the explicit request
that every publicly listed app archive source in GitHub organization `vib-app`.
This is new local validation, not an extension of the old independent acceptance.

- Registry Python tests: **28 passed** (including 6 publication-service tests).
- Source archive Python tests: **42 passed**, including failure, retry, remote
  read-back mismatch, private ownership, scoped tokens, empty-repository recovery,
  forbidden source files and safe API error classification.
- Disposable PostgreSQL/pgvector integration: **PASS**, all five migrations.
  Missing archive blocks both normal publication and direct publication mutation;
  wrong organization/repository/source/package and conflicting replays fail.
  A publication-only principal cannot record/delete upload attestations; archived
  publication still succeeds through its approved function. Referenced archive
  deletion is rejected. The named temporary container was removed by the runner.
- Public projection JavaScript: **12 passed**; desktop public Registry sync:
  **7 passed**; shared web contract: **16 passed**. Private source with a matching
  receipt is allowed; a missing or mismatched receipt is not public-eligible.
- Website TypeScript: `tsc --noEmit --incremental false` **PASS**.
- Project `scripts/check.mjs --sync-web`: **19 checks passed**; subsequent targeted
  reruns cover the final extra archive/publication failure tests counted above.
- Candidate schemas: **43 fixtures passed**. The archival field is an additive
  product candidate extension; legacy/private/synthetic shape validation does not
  authorize a real public listing without a trusted bound receipt.

Real GitHub transport initially failed because the private test repo was absent
from App installation `159462341`'s selected repositories: 404 on read and
422/name-already-exists on creation. After the owner added that single repository,
**real upload/read-back and write-free replay passed** on 2026-09-06:

- Repository ID `1358981750`, still private; installation still `selected`.
- Source commit `70adfefe89d7435f97f634f8ecc9769ca8b16b1d`; exact source/package
  transport-fixture bindings are recorded in `../source-archive/README.md`.
- First successful upload: 14 API calls, 7 Git object/tag writes, no new repo or
  bootstrap. Replay: 6 calls, zero repository writes, identical receipt/commit.
- An intervening replay returned a network/response error; it was not reported as
  success. A subsequent bounded replay provided the successful evidence above.
- Source-archive offline tests rerun: 42 passed.

Only synthetic transport source was uploaded, using GitHub App credentials, not
personal-token source writes. This does not establish new-repository automatic
access assignment or real application acceptance/publication. No public test
application or accepted package evidence was fabricated.

No production database, public endpoint or public App Store was deployed/migrated.
Legacy public rows require archival backfill and explicit republishing when 0005
is applied in an approved deployment window.
