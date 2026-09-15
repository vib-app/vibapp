# VibApp local product integration smoke

This ignored prototype check connects the exact authority boundaries instead of
re-creating a fixture between modules:

`Verifier candidate -> private local AppStore -> daemon install/enable/UI close/disable`

Run it with a real `vibapp.builder-candidate.experimental-v1` promotion record:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  artifacts/product-integration/end_to_end_smoke.py \
  --candidate artifacts/app-builder/demo-output/pipeline/candidates/<digest>/candidate.json
```

The smoke requires private-by-default AppStore state, installs disabled, proves UI
surface close does not disable the app, then disables it. The daemon does not execute
guest bytes in this check, and the result says so explicitly. Component execution is
covered separately by the native Runtime and Web Worker evidence.

The full safe synthetic boundary smoke starts at a complete NeedSpec and explicit
single-use consent, then exercises the CodeAgent adapter with a provider-attempt test
double, Builder quarantine, independent verification, AppStore and daemon lifecycle:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  artifacts/product-integration/synthetic_vertical_smoke.py
```

It deliberately does not invoke a real CodeAgent, start a provider CLI, use the
network, or compile generated source: the prebuilt, digest-pinned Component is
injected only through the Builder's inert fixture runner. The machine-readable result
distinguishes the simulated adapter attempt from an actual external provider request.

The sequential, memory-conscious product regression entry point is:

```sh
artifacts/product-integration/run_full_regression.sh
```

It keeps Python suites sequential, caps Cargo parallelism at two jobs by default,
uses isolated `/tmp` Cargo targets, and never invokes the retired
`r52_diagnose_current.py`. Set `VIBAPP_INCLUDE_WEB_BUILD=1` to append the production
Website build after the unit, integration and synthetic-vertical checks.

To exercise the public Registry → RoomHash locator binding without changing any
production projection, pass an existing `seed-package` receipt (or normalized locator)
to the isolated acceptance generator:

```sh
node artifacts/product-integration/roomhash-public-flow-acceptance.mjs \
  --transport /absolute/path/to/seed-receipt-or-locator.json \
  --output artifacts/product-integration/output/roomhash-public-acceptance
```

The output directory must be absent or empty. The generator reuses the independently
verified demo Builder candidate, keeps its `authority.publish=none`, calls the same
public locator sync used by the Website, and self-checks the exact Registry snapshot,
canonical record, normalized locator, manifest, package, and source digests. It hard
rejects output paths overlapping the production Registry, locator source, or Website
`public/data`. The generated `public-data/` directory has the exact source-root shape
accepted by Desktop's strict Registry loader. Omitting `--output` creates an isolated
system temporary directory.

To join that exact fixture to the real Desktop Registry → P2P → verifier → Runtime
path, run the intentionally ignored two-process acceptance test with the generated
`public-data/` root:

```sh
cd artifacts/desktop/src-tauri
VIBAPP_ACCEPTANCE_PUBLIC_REGISTRY_ROOT=/absolute/path/to/public-data \
CARGO_TARGET_DIR=target-roomhash-1_93 CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 \
rustup run 1.93.0 cargo test --bin vibapp-launcher \
  real_public_p2p_candidate_reaches_disabled_runtime_install \
  --offline -- --ignored --nocapture --test-threads=1
```

The test must report `strict_registry_locator_sync_tested=true`,
`downloaded-and-verified`, and `installed-disabled`. It remains synthetic-public and
does not claim genuine Registry publication.

The separate real-browser cache acceptance is:

```sh
cd artifacts/product-platform/website
npm run accept:roomhash-public-cache
```

It uses two isolated Chrome profiles, real vendored WebTorrent/WebRTC, a strict
fixed-WSS locator followed by a test-only local tracker mapping, and verifies that
the committed IndexedDB receipt exposes no transport details or package bytes.

Before any real CodeAgent request, run the machine-readable no-request preflight:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  artifacts/product-integration/codeagent_vertical.py \
  readiness --model gpt-5.6-sol
```

It checks the reviewed CLI/auth metadata, exact secret-free Codex argv, immutable
Builder inputs, independent Verifier, fail-closed consent gates, and byte-identical
Desktop/Web GUI assets. It explicitly reports `provider_process_started: false`,
`external_request_attempted: false`, and `consent_consumed: false`.

The separate `run-live` subcommand is the only full real vertical. It requires the
exact task-bound job, consent, provider, model, external-cost acknowledgement,
explicit submit, and `RUN-ONE-LIVE-CODEAGENT-TASK` sentinel. Success is reported only
after that same task reaches the isolated Builder, independent Verifier, and private
AppStore with equal digests. Merely running `readiness` can never spend quota.
