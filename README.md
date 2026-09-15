# VibApp

VibApp is a Rust/Wasm application ecosystem: a shared desktop/web Launcher and
AppStore, with Registry-first requirement intake, provider-neutral CodeAgents,
an isolated Builder, a separate Verifier, and a host-owned runtime.

[Open the Store](https://www.vibapp.ai) · [Download the client](https://github.com/vib-app/vibapp/releases)

The client download is an experimental Apple Silicon macOS preview. Read the
release's signing, platform and setup limitations before installing.

## Client releases

`.github/workflows/client-release.yml` builds on a clean macOS arm64 runner,
includes pinned Python/Node/RoomHash dependencies, runs verification, and uploads
a DMG plus checksums and provenance. **Run workflow** builds an artifact without
publishing. Pushing a new `client-v*` tag builds and publishes a GitHub pre-release
only after checks pass. The build job is read-only; only the separate publish job
receives repository write permission. Never reuse or move a published tag.

Versioned dependency inputs are in `artifacts/desktop/ci/release-dependencies.json`.
The trusted host's inspector hash is stamped in the disposable CI checkout before
desktop compilation; this does not disable the runtime verifier or change the
historical Stage 0 toolchain. Apple Developer ID signing/notarization is not yet
configured. Windows/Linux release jobs are intentionally not enabled.

Current engineering status and acceptance boundaries are in
[the repair ledger](docs/product/repair-ledger.md). The
[Stage 0 contracts](docs/stage0/README.md) remain the ABI/security baseline;
their historical tranche status is not a current product completion report.

## Source and generated data

Product modules currently live in `artifacts/` for path compatibility. The root
`.gitignore` explicitly allows source, tests, schemas and packaging inputs there;
build directories, credentials, runtime data and generated apps stay ignored.
This increment does not move these paths or import private operational history.

| Responsibility | Source |
| --- | --- |
| Shared GUI and native host | `artifacts/desktop/ui`, `artifacts/desktop/src-tauri` |
| Web host and Wasm bridge | `artifacts/web-client-core`, `artifacts/web-product-backend` |
| Intake and delivery | `artifacts/need-analyzer`, `artifacts/orchestrator` |
| CodeAgent integration and launch boundary | `artifacts/codeagent-adapter`, `artifacts/codeagent-launcher` |
| Builder and Verifier | `artifacts/app-builder` |
| Runtime authority | `artifacts/runtime-daemon` |
| Registry and local store | `artifacts/registry`, `artifacts/registry-store` |
| P2P and collaboration | `artifacts/roomhash-transport`, `artifacts/roomhash-collaboration` |

## Local regression

Use Node and Python 3.11 or newer:

```sh
node scripts/check.mjs
node scripts/check.mjs --sync-web --rust
```

`VIBAPP_PYTHON_BIN` may select an explicit Python. Rust checks use an already
installed exact 1.98.0 host toolchain; the web core retains 1.93.0. Overrides are
`VIBAPP_DESKTOP_CARGO_BIN` and `VIBAPP_CARGO_BIN`, both absolute Cargo paths.
No toolchain downloads, live model requests or publication happen in this check.
`--sync-web` explicitly regenerates the loopback-only private preview for browser
contract checks; the separate desktop/public Registry projection stays public-only.
Builds are offline/locked with at most two compiler jobs and reuse existing targets.

Some product regression fixtures still require the previously prepared pinned
tool layer and seed Wasm. A fresh clone is **not yet a self-contained bootstrap**;
missing prerequisites must fail rather than be replaced by mock success.
The old full-regression/dated product audit remains available separately. Dated
package/acceptance tests require `VIBAPP_RUN_HISTORICAL_ACCEPTANCE=1`; skips are
not acceptance passes.

## Real CodeAgent testing

See [Docker setup and boundaries](artifacts/codeagent-adapter/README.md). Codex is
currently the executable Docker adapter; the other provider integrations remain
paused. The selected model connection must support streaming Responses and have
usable quota. No credentials are copied into the container.

After building the current desktop product bridge, explicitly run a private test:

```sh
python3 artifacts/product-integration/review_acceptance.py \
  --sample shopping-list --model YOUR_MODEL \
  --enable-real-provider --acknowledge-external-cost
```

Repeat with `clock` or `notes` sequentially. These commands consume model quota,
retain private attempt evidence, and never publish. Source/build success is not
runtime UX acceptance; `review_runtime_acceptance.py` separately drives a real
candidate's advertised controls and persisted reopen using an explicit test plan.

## macOS runtime packaging

The descriptor inspector digest includes the executable's signature. After a
reviewed runtime source change, build the fixed offline service-runtime release,
then sign that build output with `artifacts/desktop/packaging/macos/Runtime.entitlements.plist`
using `codesign --force --sign - --timestamp=none --options runtime --entitlements`.
Review and refresh `COMPONENT_INSPECTOR_SHA256` against those final signed bytes
before rebuilding the desktop binaries. Do not weaken this check to accept any
file selected by an environment variable. The packager now rejects signatures
that change the accepted bytes and runs the bundled inspector's real preflight
before promoting the staged application. Set `VIBAPP_PYTHON_BIN` to Python 3.11+
when the system `python3` is older.

Local runtime checkpoints: website `http://127.0.0.1:3000/`, private product
backend `http://127.0.0.1:3189/healthz`, and the isolated preview host on port 4174.
An HTTP success is only readiness: also open the shared GUI, verify a real
Component preview, and run candidate-specific persisted interaction checks.
