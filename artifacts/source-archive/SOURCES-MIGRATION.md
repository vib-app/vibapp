# Shared source migration — 2026-09-15

Owner decision: two fixed public repositories, `vib-app/sources` and
`vib-app/packages`; migrate previous test applications. Private local apps stay
local unless separately submitted with public-source consent. Preserve old repos.

## Current external state

- `sources`: public, ID `1371257005`, default branch `main`, newly created.
- `packages`: public, ID `1359065065`, existing installation access retained.
- Owner added `sources` to vibapp-source-bot's selected repositories. Real scoped
  writes and GitHub readback now succeed; no all-repository permission was needed.
- Three application source trees are migrated: LED clock, Qwen unit converter,
  and GitHub Release Check. No old source repository or package was deleted.
- Do not recreate the repo, select all organization repositories, use owner/PAT
  credentials, or change old repository visibility to work around this.

## Completed live migration and build

- Original LED source: `ab0bdd7d86c3fb027b773b95c8e3d5d447c83add`, receipt
  `runs/led-clock-20260915/public-source-archive-receipt.json`.
- Unit converter source: `4b67895cbdfe3634d059d3acc88cf193f1306a2f`, receipt
  `runs/opencode-qwen-20260907-current/converter/github-build/shared-source-staging-receipt.json`.
  Source staging only; this is not a verified package or Store app.
- GitHub Release Check source: `c126f17f2baf8f192089cbd6121cdc6e9b9c62c6`, receipt
  `runs/github-release-20260906/build-exec-fixed/shared-source-archive-receipt.json`.
  An interrupted upload was reconciled by the same immutable-source retry.
- Original LED handoff was v1 and used Cargo package name `vibapp-led-clock`.
  New `import_legacy_source.py` creates a separate validated v2 input, preserving
  original Rust/provider facts, renaming only Cargo's package name, and preserving
  v1's conservative all-imports-required policy. It does not weaken the v2 validator
  or claim another CodeAgent run. The migration record is in
  `runs/led-clock-20260915/migrated-input/migration.json`.
- Migrated source digest: `5c55ee5156ed3c03456ba063db53a54d7989b477b6506c60462b0d7c1caef8e3`.
  Staged immutable commit: `5612c99f854cfb48945c4e4a22be91da79dab1e4`.
- Real [LED Actions run](https://github.com/vib-app/sources/actions/runs/34958597298)
  completed successfully. Downloaded output passed the separate pinned local
  Wasm/import/descriptor verifier; controller state is `independently-verified`.
- New native candidate:
  `runs/led-clock-20260915/github-build/pipeline/candidates/ef40927893f11a379cb036aa33e4f379e7ddd5a29d6aa36b71ec915621f9555d/candidate.json`.
  Published to packages and listed in `packages/main/registry.json` on 2026-09-15.
  See `github-build/release-receipt.json` and `store-publication-receipt.json`.
  ZIP SHA256 `b94a97131b67009b402c61119c30eef3d8c41b0752eb648722ac9532dbbb111d`;
  source commit `5a63543855df9200a99d07323f61cb76d4d58197`;
  catalog commit `d65a6c158ddb450d2483f39e39dcdac9d7941ee5`.
  **No browser derivation or online ticking acceptance yet.**

## Remaining work

1. New public CI builds must record `public_source_approved: true`; do not mutate
   old successful release provenance/receipts to pretend their CI ran in sources.
2. Complete the real LED browser derivation and online ticking test. Package and
   catalog publication now have separate receipts; they do not prove browser use.
   Existing `derive-browser-component.mjs` is still tied to the hello fixture;
   its host adapter has no wall clock. Hosted runtime activation is also disabled.
   Do not fake Stage 0 activation flags or treat that fixture as the user's clock.
3. Before long-lived browser use, test/replace the old LED no-op deallocator using
   the reviewed reclaiming support in `artifacts/cloud-agent/starter/vibapp_support.rs`.
   The newly compiled source still contains the old allocator; no repair is claimed.

### Existing LED (real user application)

- Publisher `ai.vibapp.local`, app `ai.vibapp.custom.1a04442610c`, version `0.1.1`.
- Source digest `df242aec31811dde764da29b0abceff5e51871f75a2654b557e63c2def73f3f6`.
- Native package digest `8c8cd3f7d40291fc646f88b287018c54d05e1fb6529d8fb4682a742daffda4ba`.
- Original source:
  `/Users/zhuzhe/Library/Application Support/ai.vibapp.launcher/delivery-controller/tasks/development-7e9c132b299d598c2af296045b45a55c/attempts/attempt-0002-1728b404a8b5056b/codeagent/output/runs/job-cloud-45dae430f092dd82d86728cd/source`.
- Private receipt `runs/led-clock-20260915/native-source-archive-receipt.json`.
- Native-only package: no browser derivations. Its no-op allocator deallocation
  deserves a bounded long-running refresh test before claiming a reliable clock.

### Other local historical evidence

- `runs/github-release-20260906/build-exec-fixed/`: completed archive-check source
  and package receipt; staged source digest
  `263f48ab6722bf681a6c395c4f6acdad489b21d2b98b0f45cec33e0ecdda120a`, handoff in
  `runs/github-release-20260906/input/handoff.json`. Old failed/retried build
  ledgers refer to the same source; deduplicate, do not treat each as a new app.
- `runs/opencode-qwen-20260907-current/converter/github-build/state.json`: converter
  `ai.vibapp.qwen.converter20260907`, state source-staging, source receipt null.
  Local source exists under that ledger's handoff. Do not claim it was archived
  or compiled by GitHub, or publish its runtime as verified, without rechecking.
- Initial smoke transport fixture is described in README; it is not an accepted
  user application. No test fixture should become a Store app merely by migration.

## Local validation checkpoint

New coverage: public consent before network, fixed shared repository/visibility,
per-app tag namespace, scoped role checks, immutable directory replay, concurrency
reconciliation, preservation of other apps/workflow, and conflicting path refusal.
Source/Actions/package unit tests: 127 passed (including four legacy migration tests);
Registry publication tests: 7 passed;
Registry contract tests: 10 passed; public locator tests: 13 passed. Disposable
PostgreSQL integration passed all six migrations, shared/legacy source receipt
checks, publication gates, and least-privilege tests; the test container was
automatically removed afterward. No production database was migrated.
These are synthetic/local checks, not proof of
live migration, Actions compilation, public package download or browser operation.

Local Python's bundled default CA path is absent. The secure operator invocation
currently uses `SSL_CERT_FILE=/etc/ssl/cert.pem`; never disable TLS verification.
