# Registry store operator runbook

Status: design/runbook only. Do not use these steps against production until a
separate deployment owner approves the environment, backups, identities, and
change window.

## Preflight

1. Freeze the five migration SHA-256 values and review any difference as a new
   change. Historical four-migration acceptance does not cover 0005.
2. Confirm PostgreSQL 16 and compatible `vector`, `pgcrypto`, and `pg_trgm`
   extensions already exist in the approved package/image source. Do not let a
   migration job pull arbitrary software.
3. Take and restore-test a database backup outside the target database. Capture
   schema owner, extension versions, database size, active sessions and replication
   health.
4. Create separate search, ingestion, trust, verification, revocation and
   publication and source-archive principals. None may own the database/schema or inherit another
   authority.
5. Set connection, lock and statement timeouts at the service/deployment layer.
   Recommended initial search statement timeout is at most 2 seconds.

## Apply

Apply each migration once, in filename order, with `ON_ERROR_STOP=1`. Every file is
transactional and stops on error. Record the database identity, operator, start/end
time, migration filename and digest, server/extension versions, result and backup
reference in the external change log.

Do not edit an applied migration. Add the next numbered migration. These are
forward-only migrations because destructive down scripts can silently discard
verification, revocation, authority or audit history.

After schema apply, use `deployment/least-privilege-grants.sql.example` as a reviewed
template. Do not grant application roles table ownership or arbitrary DML.

0005 withdraws legacy public visibility into private/review until source archival
is backfilled and publication authorized again. Inventory affected releases and
their verified source snapshots before an approved migration window. Do not
fabricate receipts to keep old rows public. Receipt deletion is blocked while a
publication references it; this is not a user-data removal workflow.

## Smoke checks

- Call `search_preflight_v1` with an incompatible synthetic NeedSpec and confirm
  `embedding.status=not-needed`.
- In a disposable tenant/test namespace, ingest one synthetic private release,
  replay its idempotency key, and confirm exactly one release/audit event exists.
- Confirm the private release is absent from public search.
- Independently add verification plus both public authorities; confirm missing
  GitHub archive still blocks publication (including direct table mutation).
- Archive/read back the exact verified source, record its receipt using only the
  archive-service role, then publish, search,
  revoke, and confirm the release becomes ineligible while revocation evidence
  remains queryable to the authorized operator.
- Confirm a remote-processing grant alone cannot publish.
- Confirm the publication role cannot record or delete archive receipts, and
  replay uses the existing Git tag without generating another source commit.
- Inspect `EXPLAIN (ANALYZE, BUFFERS)` only with bounded synthetic inputs; do not run
  unbounded diagnostics or dump vectors/metadata into logs.

## Failure/rollback

If an unapplied migration fails, its transaction rolls back. Fix only in a new
reviewed candidate and rerun on a restored disposable copy first.

If a successfully applied migration causes a release blocker, stop writers and
search traffic, preserve database/audit evidence, and restore the pre-change backup
to a separate database. Validate exact release/audit/revocation counts and service
queries before switching traffic. Do not manually delete authority, revocation,
verification, or audit rows to simulate rollback.

## Monitoring and incident facts

Monitor bounded query latency/timeouts, connection pool saturation, eligible and
safe-rejection counts, embedding model/version misses, idempotency conflicts,
publication/revocation transitions, index growth, autovacuum health and replication
lag. Logs may contain request/release/event digests and reason codes, but never
prompts, vectors, credentials, secrets, source code, sensitive settings or user data.
