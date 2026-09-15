# WS02 validation record

Date: 2026-08-27

## Verified locally

- Strict source-handoff intake and source-tree digest reconciliation.
- Quarantine-only Builder authority; no candidate exists before Verifier promotion.
- Independent exact-byte package digest, artifact digest, manifest semantics, WIT
  import/export, ambient-WASI denial, and `wit-bindgen-rust 0.60.0` metadata checks.
- Durable rejection decision with install/publish authority set to `none`.
- Output-swap, fabricated-success text, path escape, symlink, duplicate JSON key,
  unavailable browser derivation, log flood, and timeout fail closed.
- Candidate schema uses lowercase check outcome `pass` and exact authority
  `{ "install": "daemon", "publish": "none" }` for Runtime Daemon intake.
- Service triggers are canonical task-bound subsets; Builder does not add scheduler
  or automatic-start claims absent from the handoff, and legacy handoffs default to
  manual-only.
- Fixed-world structural imports are reconciled independently from the app-specific
  `required_capabilities` subset. Structural-only imports are degradable denied
  stubs; required capabilities retain current host availability for admission.
- New quarantine receipts bind the exact app-required subset plus the full
  entrypoint/route/trigger projection. Verifier promotion rejects either
  manifest/receipt drift, while delivery independently rejects task/handoff/receipt/
  manifest drift both during the live attempt and terminal replay.
- The fixed descriptor inspector executes only Guest `describe` in a 64 MiB Store
  with 20,000,000 fuel, epoch interruption, a 2 s internal deadline, and a 15 s
  independently supervised child deadline. Its host imports have no live effects.
  Verifier promotion now rejects every app identity/display-name mismatch and every
  missing, extra, duplicate, or field-mismatched entrypoint before candidate creation.

Focused capability recheck on 2026-08-28:

```text
CloudAgent capability/task suite: 42 tests passed
Builder capability/necessity/receipt tests: 3 tests passed
Runtime Builder/daemon host-availability table check: 1 test passed
Builder/Verifier aggregate after descriptor and lifecycle-intent gates: 37 tests passed
Service runtime unit suite: 11 tests passed
```

Command and result:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s artifacts/app-builder/tests -p 'test_*.py' -v
Ran 16 tests in 1.123s
OK
```

## Offline cache and real runner evidence

- Cache root digest: `b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d`.
- Independent cache acceptance SHA-256:
  `c4c8ffef7fa72ffd6723f3f6615823dc83deb1742864f72c0067dfe215990188`.
- Two fresh sandboxed builds produced identical 59,315-byte Components with
  SHA-256 `acb6c56569815b83d0a584e2566c7c5c490d69a1d37d6e65475c81f2283163c9`.
- Peak observations were 574,177,280 bytes RSS, 4 PIDs, and 135,436,889 bytes
  workspace usage, below the 2 GiB / 64 PID / 512 MiB ceilings.
- The accepted `wasm-tools 1.256.0` completed `validate`, `component wit`, and
  `metadata show` on both exact Components.
- Evidence:
  `generated/builder-cargo-cache-runner-smoke-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d-v5/runner-smoke-evidence.json`.

## Deliberately not claimed

- The safe integration fixture still does not compile CodeAgent source; the
  separate automatic-runner smoke now does.
- A production-isolated compiler is not claimed; the available macOS runner is a
  bounded local product runner.
- Real offline compilation now uses the enforced no-std/alloc source contract. Both
  fresh Components passed all seven independent Verifier checks and reached
  `candidate-ready`; install, launch, publish, and Stage 0 activation remain outside
  this workstream's authority.
- Install, launch, publish, Registry admission, and formal Stage 0 acceptance belong
  to other authorities/workstreams.
