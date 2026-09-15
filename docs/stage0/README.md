# VibApp Stage 0 Contract Index

Status: Exact contract commit `e47f6cec78fe90923bcddde5dd1215fc1afe40de` passed fresh independent pre-code acceptance on 2026-08-11. The project-local VibApp skill at `531dda060f6641dc05e8b3143bf6374e1ae056a3` passed fresh independent acceptance. Tranche 1 at `393287f0af8e9c29ad3d8b44be895db0192b70e7` passed its full independent evidence and semantics acceptance on 2026-08-12; Tranche 2 is now the only implementation tranche authorized to begin.

Stage 0 is an unpublished, local falsification spike. It may prove or reject a candidate module boundary; it does not publish an ABI, deploy a service, call a live model, or create an external compatibility promise.

## Gate artifacts

| Gate | Artifact | Status |
| --- | --- | --- |
| 0 — baseline/provenance | [provenance.md](provenance.md) | Independent PASS at `e47f6ce`; source/path boundary remains mandatory |
| 1 — charter/system boundary | [adr-0001-system-boundary.md](adr-0001-system-boundary.md) | Independent PASS at `e47f6ce`; Stage 0 scope remains bounded |
| 2 — package/WIT/compatibility | [../rfcs/0001-runtime-abi-v1.md](../rfcs/0001-runtime-abi-v1.md), schemas and vectors | Independent PASS at `e47f6ce`; 63 vectors are contract evidence, not implementation evidence |
| 3 — threat/isolation | [threat-model.md](threat-model.md) | Independent PASS at `e47f6ce`; 41 threats map to 67 future fixtures |
| 4 — alarm semantics | [alarm-semantics.md](alarm-semantics.md) | Independent PASS at `e47f6ce`; 45 cases remain unimplemented |
| 5 — toolchain/dependencies | [toolchain.md](toolchain.md) and machine-readable pins | Independent PASS at `e47f6ce`; missing tools/build graph remain execution holds |
| 6 — conformance-first tranches | [implementation-tranches.md](implementation-tranches.md) | Independent pre-code PASS at `e47f6ce`; independent skill PASS at `531dda0`; Tranche 1 full PASS at `393287f`; Tranche 2 only is authorized |

The Tranche 1 formal artifact contained 42 immutable evidence files with canonical inventory root `53e26f372bc9ec545e31fa079c8bef0c765e01d5bc1b729ebee687b76ea816b2`. Its fresh independent full-acceptance report has SHA-256 `f2329f520575db6b1e85c3c975ec62b643e637d2b8ebf7ea10ea270e3ee9a936`. This releases Tranche 2 minimal-host and no-privilege golden-component work only. Tranche 3+, GUI/browser execution, live providers, installation, deployment and publication remain prohibited. Dependency setup remains separately gated by [toolchain.md](toolchain.md).

After acceptance, the first contract-derived developer artifact is the project-local `.agents/skills/vibapp`. It must encode UI/service/hybrid applicability, Launcher and CLI/headless lifecycle, desktop/web-preview/web-runtime/headless profiles, capability degradation, validation and packaging gates; it cannot weaken the accepted RFCs.

## Authorized future source locations

After the gate acceptance, Stage 0 source is restricted to:

- `.agents/skills/vibapp/` — the post-acceptance, pre-Tranche-1 contract-derived skill only;
- `crates/protocol/` — manifest, compatibility, event and permission types;
- `crates/runtime/` — local experimental Component host and generation switching;
- `crates/registry/` — deterministic fixture search only;
- `crates/agent/` — replay/stub provider adapter only;
- `crates/builder/` — deterministic isolated builder contract/fixture;
- `apps/vibappd/` — local test daemon/driver only;
- `examples/alarm/` — golden alarm guest/domain logic;
- `wit/experimental-v0/` — unpublished candidate WIT;
- `fixtures/` — positive, negative, adversarial and replay fixtures;
- `infra/stage0-builder/` — local non-credentialed builder definition.

The only root/workspace policy metadata that later tranches may add or update is:

- `Cargo.toml` and `Cargo.lock` — the Stage 0 workspace declaration and exact resolved graph;
- `.cargo/config.toml` — reviewed offline/vendor configuration only; no credentials, wrappers, runners or ambient environment injection;
- `deny.toml` — the committed `cargo-deny` policy;
- `supply-chain/config.toml`, `supply-chain/audits.toml` and `supply-chain/imports.lock` — committed `cargo-vet` policy/audit metadata;
- `rust-toolchain.toml` and `config/contract-toolchain.toml` — the already accepted toolchain selections, changed only through Gate 5 change control.

This is an exhaustive future source/workspace path allowlist, not permission to create any item before the required phase. Existing contract documents, schemas, WIT and fixtures remain governed by their gates. Any other top-level source or workspace-policy path requires an ADR update and renewed gate review.

Synthetic replay inputs under `fixtures/**/*.jsonl` are reviewable, version-controlled contract data. Real model/provider transcripts and build/runtime logs must stay in ignored local operational locations such as `.chief-of-staff/`, `logs/`, `artifacts/`, `quarantine/` or `tmp/`; they may never be relabeled as fixtures.

Stage 0 processes may write generated output only under ignored `target/`, `generated/`, `artifacts/`, `quarantine/` or `tmp/`, and local logs only under ignored `logs/` or `.chief-of-staff/`. The hygiene ignores for `build/`, `dist/` and `node_modules/` do not authorize those paths for a Stage 0 tranche. GUI/web/cloud/public Registry/publication/payment/signing-root source is out of scope for Stage 0.
