# Four-gap product integration validation — 2026-08-27

Scope: ignored local product prototype. This is author/root integration evidence,
not formal Stage 0 acceptance or public deployment evidence.

## Safe vertical smoke

`synthetic_vertical_smoke.py` passed one complete, bounded synthetic flow:

1. complete NeedSpec plus exact single-use consent;
2. CodeAgent adapter producing a strict source-only handoff;
3. Builder quarantine using the explicitly inert fixture runner;
4. independent Verifier promotion of exact Component bytes;
5. private local AppStore ingest and idempotent replay;
6. daemon install-disabled, enable, UI open/close, status and disable.

Machine-readable terminal facts:

```json
{"builder_runner":"safe-fixture-no-source-execution","candidate_digest_sha256":"f4d6416b11205d49e82bb295f961e56bd3dd99e96b3f461338c4269ffccfa380","codeagent_source_compilation_proven":false,"daemon_lifecycle_smoke":"PASS","exact_consent_consumed":true,"external_provider_request_performed":false,"external_request_attempted":true,"external_request_observed":false,"guest_execution_performed_by_daemon":false,"need_spec_complete":true,"private_appstore_ingest":true,"provider_mode":"synthetic-simulated-attempt-no-network","schema_version":"vibapp.synthetic-vertical-result.experimental-v1","source_handoff_created":true,"status":"PASS"}
```

The test double simulates the adapter's provider-attempt evidence but does not start a
CLI, call an external model or compile fixture source. A prebuilt, digest-pinned real
WASI Component enters only through the Builder's no-source-execution fixture runner.
Those limitations are result fields, not prose-only caveats.

A separate real automatic Builder v7 smoke uses the independently accepted offline
cache and tool layer. It performs two fresh bounded sandbox builds, produces the same
59,315-byte Component both times
(`acb6c56569815b83d0a584e2566c7c5c490d69a1d37d6e65475c81f2283163c9`),
and the independent Verifier promotes both candidates with 7/7 checks. Evidence:
`generated/builder-cargo-cache-runner-smoke-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d-v7/runner-smoke-evidence.json`.

The frozen WS02 candidate also passed direct exact-byte AppStore-to-daemon integration:

```text
candidate: dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674
AppStore private / publication performed=false: PASS
install staged disabled -> enable -> UI close retained enablement -> disable: PASS
daemon guest execution performed=false: PASS
```

## Regressions

```text
Cloud Agent                               28/28 PASS
CodeAgent adapters                        23/23 PASS
Builder + independent Verifier            17/17 PASS
Delivery controller / orchestrator        13/13 PASS
Provider Runner                           24/24 PASS
Local diagnostic CodeAgent                  8/8 PASS
Need Analyzer                             12/12 PASS
Registry service                          15/15 PASS
Registry / local AppStore                 21/21 PASS
Runtime daemon                            27/27 PASS
Website contracts                         13/13 PASS
Legacy client/Registry POC                17/17 PASS
Native definition/package fixture checks  22/22 PASS
Desktop Rust                            119/119 PASS
Web GUI/Worker/single-source contracts    12/12 PASS
Web Rust core                               2/2 PASS
Split preview host                          6/6 PASS
Website production build                        PASS
Browser same-GUI root, app route, Worker        PASS (0 console errors)
Separate browser derivation re-verifier          PASS
macOS release bundle signature                  PASS
Synthetic complete vertical smoke               PASS
```

The Web single-source contract compares generated `app.js`, `styles.css` and
`favicon.svg` byte-for-byte with `desktop/ui`, and permits only the deterministic
Web mode label and bridge script injection in `index.html`. The fourth Web check
also verifies Registry package/component-reference binding and rejects package,
declared-size, and Wasm-byte tampering before execution.

## Unavailable / not claimed

- No fresh live external CodeAgent generation was consumed during acceptance; a real
  call requires a new user-bound consent and separate cost/quota acknowledgement.
- The accepted Builder/cache/Verifier evidence is local macOS product evidence, not a
  production multi-tenant isolation or cloud deployment claim.
- The synthetic complete vertical does not execute guest bytes, while separate daemon
  validation executes a real service Component and separate native/Web runtimes execute
  UI Component bytes.
- Browser activation stays `stage0_activation_eligible=false`; executable Blob/data
  imports are removed and local fresh-process rederivation passes, but canonical
  manifest/toolchain/profile and fresh formal acceptance remain open.
- No native Windows/Linux verification, public publication, hosting, DNS, signing
  root, or production OS-service packaging. UI-only and per-app multi-service updates
  are locally implemented and tested, but cross-process UI render-failure reporting
  and hardware power-loss GC testing remain later hardening.
