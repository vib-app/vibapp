# VibApp Client validation

Date: 2026-08-24 Asia/Shanghai  
State: experimental/local-product. This evidence does not claim formal Stage 0
acceptance, production daemon packaging, background guest service execution, or
public AppStore publication.

## Product boundary under test

- VibApp Client is the Launcher, AppStore surface, trusted semantic renderer, and
  requirement-to-development entry point.
- Registry and CodeAgent outputs converge on one VibApp package model. The generated
  target is `vibapp-client`, never a user-selected macOS/Windows/Linux application.
- The current native Client may use OS, architecture, and `desktop` profile facts only
  as internal admission context. They are no longer requirement-form questions.
- A verifier-promoted private candidate can enter the local AppStore, be installed
  disabled by the daemon, explicitly enabled, launched, disabled, and uninstalled.
  Legacy private candidates can still be opened only as isolated previews.

## Verified implementation

- `launch_app` is a real Tauri command. For legacy preview candidates it verifies the
  bounded private record; for installed apps it reopens daemon state, manifest and
  Component bytes, verifies identity/path/size/SHA-256, executes the sibling
  `vibapp-runtime`, and records the UI surface with the daemon.
- The shared GUI now presents real local AppStore install, enable/disable,
  service-control start/stop, status refresh, launch, and retain-data uninstall
  actions. Install reopens the AppStore `detail` record and passes its exact verifier
  candidate to the owner-authenticated daemon; it no longer records a
  `future-daemon` intent.
- Runtime execution has a five-second deadline, 64 MiB linear-memory ceiling, fuel and
  epoch limits, cleared environment, bounded output, no ambient WASI, and descriptor /
  digest identity checks.
- The Launcher validates the returned semantic UI as one bounded, connected,
  cycle-free tree before rendering it. The guest receives no DOM, CSS, or native
  window handle.
- The host renderer supports text, buttons, fields, list containers, progress, and
  confirmations. Guest-surface controls remain deliberately disabled because a
  persistent interactive Runtime session is not implemented yet.
- Client size classes are `compact`, `regular`, and `wide`. The real Component output
  was rendered at 375, 820, and 1280 CSS pixels without horizontal page overflow.
- The requirement flow no longer asks users to choose an OS, CPU architecture, or
  runtime profile. Qwen remains a streaming NeedSpec preprocessor; it does not replace
  the real CodeAgent.

## Authoritative checks

Desktop Rust commands used the directly addressed Rust 1.98 toolchain with
auto-install disabled and `--locked`. The Web core uses the fixed Rust 1.93
toolchain offline.

```text
cargo fmt --all -- --check                         PASS
cargo test --locked                               PASS: 53 tests, 0 failed
cargo build --locked --bins                       PASS
node --check artifacts/desktop/ui/app.js           PASS
local AppStore -> daemon Rust integration           PASS
runtime-daemon suite                               PASS: 13 tests, 0 failed
Web shared-source / digest-binding suite            PASS: 4 tests, 0 failed
codesign --verify --deep --strict VibApp.app        PASS
packaged vibapp-runtime Component load              PASS
```

The packaged Runtime initially exited with status 137 under macOS Hardened Runtime.
The cause was a missing Wasmtime executable-memory entitlement. Packaging now signs
only the separate Runtime with `allow-jit` and `allow-unsigned-executable-memory`,
then seals and verifies the outer app. The packaged Runtime subsequently loaded the
digest-bound Component and returned the healthy three-node semantic surface.

Current packaged executable hashes:

```text
46294f7679ff0c2cd39a9b8548e1fae141ed6211220f639a6adefb51ce7e35da  vibapp-launcher
112ec2c232f58a7805972be7efd7bb086339d846a0fad16a6f42e8da93ab7d80  vibapp-runtime
```

Responsive evidence is under `evidence/launcher-responsive/`. The runtime screenshots
use the actual JSON returned by the built Rust/WASI Component and the same production
host renderer; only the browser transport is used for deterministic viewport QA.

## Remaining gaps

- The local Codex adapter is implemented and bounded, but no new live generation was
  run for this evidence because the existing one-time consent fixture had expired.
  A fresh authorized CodeAgent run still needs the independently accepted offline
  Cargo cache before its source can reach the Builder.
- The GUI can consume candidates already present in the local AppStore, and a bounded
  `import_verified_candidate` command validates explicit candidate paths. Automatic
  CodeAgent -> Builder -> verifier -> AppStore delivery and a GUI file picker are not
  yet connected.
- The daemon owns service lifecycle/control state, but does not itself execute service
  guest bytes. The GUI labels this boundary instead of reporting background execution.
- The Web/WASM form of this same GUI is verified for foreground Worker execution and
  shareable app routes. Browser service/hybrid background ownership is intentionally
  unsupported.
- Windows and Linux native GUI builds remain unverified; see
  `evidence/platform-matrix-20260827.md`.
- Updates/rollback, production public publication, production secret brokering and
  production daemon OS-service packaging remain unimplemented.
- The current Registry catalog is synthetic and Python 3.11+ remains a host dependency.

These gaps are presented in the Client as unavailable or preview-only states; none is
reported as installed, running in the background, or published.
