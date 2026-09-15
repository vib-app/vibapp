# VibApp persistent Registry store

Status: `experimental-product-hold`.

This directory contains a production-shaped PostgreSQL 16 + pgvector storage
adapter for VibApp Registry metadata. It is local implementation evidence only:
it is not deployed, connected to a production database, exposed publicly, or an
acceptance verdict.

It also contains a small, stdlib-only local AppStore intake used by the desktop
Launcher and the browser-delivered WASM GUI during product integration. This is a
private candidate catalog, not the public PostgreSQL Registry and not publication.

The adapter preserves the existing Registry request envelope (`need`,
`constraints`, `limit`) and the response branches (`recommendation` or
`refinement`). The database owns durable, normalized candidate facts; the caller
still owns user authentication, embedding consent, the embedding-provider call,
and transport-level rate limiting.

## Invariants

- Compatibility, platform/profile, app kind, capability, permission ceiling,
  negative constraint, publisher trust, verification, revocation, publication,
  privacy, and exact embedding-model availability are hard filters.
- The hard-filter CTE is `MATERIALIZED`; vector/full-text scoring consumes only
  its eligible output. A score cannot restore a rejected release.
- Every stored embedding is exactly 384 dimensions and is keyed by both model ID
  and model version. A query must name the same model/version.
- Package versions are immutable. Replaying an upsert with the same
  idempotency key and canonical payload returns the original result; changing the
  payload under that key fails closed.
- `private-store`, `remote-process`, `public-list`, and `public-install` are
  separate authority kinds. Public search eligibility requires active
  `public-list` and `public-install` grants; remote-processing authority never
  implies publication authority.
- Verification evidence and revocation history remain durable even when a
  release is withdrawn. Search explanations cite stored paths/facts only.
- Public publication requires a trusted, GitHub-read-back-verified source archive
  in `vib-app`, bound to the exact publisher/app/source/package. Missing or failed
  uploads never publish; public applications may have private source repositories.
- Stable ordering is final score descending, then app ID, version, and package
  digest ascending. Pagination cursors are bound to request digest and embedding
  model/version.
- Local candidate intake accepts only
  `vibapp.builder-candidate.experimental-v1` records promoted by
  `independent-verifier`, with `authority.install=daemon` and
  `authority.publish=none`. Every ingest is private; this CLI has no publish
  operation.

## Layout

- `migrations/`: ordered, transactional schema/function migrations.
- `api/`: strict request/upsert JSON Schemas and the service adapter contract.
- `tests/`: static contract tests and PostgreSQL integration assertions.
- `scripts/run-integration.sh`: bounded, offline-only ephemeral container runner.
- `local_appstore.py`: process-safe SQLite catalog plus content-addressed private
  candidate bytes for Launcher/WASM-GUI consumption.
- `publication_service.py`: trusted backend orchestration for source upload,
  receipt recording and public publication, with separate DB principals.

## Public publication and source archival

Migration `0005_github_source_archive.sql` enforces the source-archive requirement
inside the existing publication boundary, including direct publication table writes.
Only the dedicated archive-service principal may call
`record_github_source_archive_v1`; a publisher cannot assert upload success itself.
An exact receipt replay is idempotent; a conflicting commit/source/repository fails.

`publication_service.publish_with_source_archive` resolves ownership and immutable
source metadata from `source_archive_input_v1`, calls the trusted
`../source-archive/github_archive.py` worker, commits its receipt using the separate
archive DB connection, then invokes the existing `publish_release_v1` function.
Source/network/receipt errors propagate without publication. No DB transaction is
held during the GitHub upload. Existing verification/authority/revocation gates
remain mandatory. The local private SQLite catalog is unchanged.

Public metadata exporters join `github_source_archive_facts_v1.github_archive` to
the exact package/source before emitting `record.source.github_archive`. Desktop,
website shares/previews and public package-locator projection reject records missing
this bound receipt. Receipt shape alone is not authenticity: only trusted Registry
output may supply it. Historical synthetic fixtures have no real archival receipt
and cannot enter real public projection.

**Upgrade impact:** applying 0005 moves previously published rows without an
archive to private/review, preserving bytes and moderation evidence. Backfill their
verified source and explicitly republish before restoring public visibility. This
has been exercised only in a disposable database, not the running product or any
production store. The authenticated public service, deployment and historical
backfill remain separate work; no new public HTTP endpoint was added here.

## Local private AppStore CLI

The verifier output is passed as the exact adjacent `candidate.json` and
`package/` handoff. Ingest snapshots regular files without following links,
recomputes every file digest and size plus the RFC package digest, then stores a
daemon-ready copy at
`candidates/<package-sha256>/{candidate.json,package/}`.

```bash
python3 artifacts/registry-store/local_appstore.py \
  --store-root artifacts/local-appstore ingest \
  /absolute/path/to/candidate.json

python3 artifacts/registry-store/local_appstore.py \
  --store-root artifacts/local-appstore list --limit 50

python3 artifacts/registry-store/local_appstore.py \
  --store-root artifacts/local-appstore detail <package-sha256>

python3 artifacts/registry-store/local_appstore.py \
  --store-root artifacts/local-appstore withdraw <package-sha256> \
  --reason 'bounded local reason'
```

All commands emit one JSON document using
`vibapp.local-appstore-result.experimental-v1`. Item/detail records use
`vibapp.local-appstore-record.experimental-v1`; `paths.candidate` is relative to
the Store root and points to the exact verifier-promoted candidate record the
daemon can revalidate for install. Lists are capped at 100 records and return a
stable package-digest `next_cursor`. `withdraw` retains package and verifier
evidence, removes the item from the default list, and cannot make it public.

## Validation

Static tests require only Python 3:

```bash
python3 -m unittest discover -s artifacts/registry-store/tests -p 'test_*.py' -v
```

The integration runner uses only an already-present
`pgvector/pgvector:pg16` image (`--pull=never`). It creates one networkless,
ephemeral container limited to 1 GiB RAM, 2 CPUs, and 64 PIDs, applies migrations
serially, runs SQL assertions, then removes only that named test container:

```bash
bash artifacts/registry-store/scripts/run-integration.sh
```

No credential, production endpoint, production data, host port, or external
network is used.

## Deployment boundary

Before deployment, a separate owner must add authenticated service transport,
tenant/account authorization, migration backup/rollback practice, connection and
statement timeouts, metrics, moderation operations, a real trust root, revocation
feed ingestion, labeled relevance calibration, capacity testing, and fresh
independent security/release acceptance. Nothing here authorizes those actions.
