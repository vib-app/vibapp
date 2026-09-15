# VibApp Desktop

VibApp's cross-platform Client is built with Tauri v2. VibApp is the application
platform: generated output is one VibApp package, while macOS, Windows, Linux, and
the browser are hosts for a VibApp Client. The Client combines Launcher, AppStore,
trusted runtime chrome, and the requirement-to-development entry point.

See [ECOSYSTEM.md](ECOSYSTEM.md) for the product boundary and the exact current
implementation truth.

## Process boundary

- `vibapp-launcher`: native GUI, AppStore discovery, local need history, app controls,
  trusted chrome, and responsive host rendering.
- Registry-first intake: a draft NeedSpec goes through hard filters and real semantic
  retrieval before any development decision; recommendations and refinement remain
  separate closed routes.
- Builder and inspector: separate generation, compilation, and verification processes.
- `vibapp-runtime`: separately signed, digest-bound Component runtime. The Launcher can
  now use it for a real first-surface private preview without loading guest code into
  the GUI process.
- Future service daemon: owns enabled service and hybrid entrypoints independently
  from the GUI lifetime.

Closing an app surface is not the same as disabling its background service.

The Registry-first chat confirms behavior shape, capabilities, exact WIT-world
permission ceiling, negative constraints, network mode, acceptance example, and
package intent. It no longer asks the user to select OS, CPU architecture, or profile.
Those remain an internal current-Client admission context; the generated target is
always `vibapp-client`. A complete NeedSpec receives a deterministic canonical
SHA-256 digest.

When the user grants model-processing consent, an experimental streaming preprocessor
pre-fills the draft and the UI exposes only missing fields plus high-risk
confirmations. Settings now supports Bring Your Own Model for all current non-Code-Agent
scenarios: a Chat Completions or Responses generation profile for intake/NeedSpec work,
plus an independent Embeddings profile for Registry search. LocalAI at
`http://192.168.199.170:8081` remains the default. API keys are write-only, stored in a
permission-restricted local file, and passed to trusted helpers over bounded stdin.
CodeAgent configuration remains separate and is never overridden by BYOM. Invalid or
unavailable model output falls back to local rules.

The shared Settings GUI stores bounded controls for the active RoomHash-backed host:
P2P participation, upload/download rates, public verified-package and explicit
user-file seeding, cache and concurrency limits, temporary RTC channels, and optional
write-only TURN credentials. The trusted launcher owns the transport process and
applications never receive raw torrent, tracker, RTC, socket, or arbitrary-network
access. Local-private application data remains host `kv`; the compatibility nil UUID
is rejected by every network join. Automatic package seeding is restricted to
published, independently verified AppStore packages. Private candidates are not
seeded unless a future explicit encrypted-sharing flow is introduced.

The current RoomHash host is a bounded Node companion owned by the Launcher process.
It is not yet a separately installed always-on daemon: closing the Client ends this
peer, and automatic re-seeding after a later restart is not closed for packages that
are absent from the current local catalog projection.

When a Registry-bound locator is present in the Launcher's private locator store, the
implemented public install path can fetch the candidate through RoomHash, verify its
info hash and every declared file, confine it to the managed download directory,
ingest it through the ordinary local AppStore verifier, and only then install it in
the disabled state for the runtime daemon. A torrent infohash is only a byte locator
and never grants install or execution authority.

There is currently no real published package locator in this repository. Therefore
the public “click Install” product flow has not yet been accepted against a genuine
Registry package, even though the consumer path is implemented and the transport is
covered by a real two-host download plus independent byte re-verification. Private
and synthetic candidates do not receive fabricated public locators and continue to
use their existing local path.

Registry embedding, one-time remote private processing, and public publication are
three independent decisions. The UI shows the exact classes that would leave the
device and the job/provider/digest-bound consent object. A schema-valid cloud task
preview may be shown after complete NeedSpec plus explicit remote consent, but the
preview does not create a task by itself. A second, explicit Desktop confirmation
submits the exact preview to the local Orchestrator only after Registry no-match.
Qwen is only the NeedSpec preprocessor; it never authors Rust. The durable task is
now dispatched through a provider-neutral local CodeAgent Adapter. Its first provider
is the pinned local Codex CLI, which authors Rust in a bounded workspace and returns
an audited, untrusted source handoff. Codex success is shown as `source-ready`, not as
a built or verified application. Builder, verifier, installation, and publication
remain separate and are not implied by this adapter.

Normal `get_state` starts without demo needs, generated source, build jobs, or fake
installed apps. It may discover a digest-bound private UI candidate from the local
candidate store and expose only `isolated-preview`; this is not installation or formal
verification. The populated `fixtures/state.json` remains test-only. After explicit
consent and submission, Desktop may surface only the durable
`codeagent-running`, `source-ready`, or fail-closed CodeAgent task state.

Desktop displays `private-appstore-ready` as succeeded only when the controller wrote
an exact task/handoff/manifest binding proof plus the manifest and package digest
proofs. Older digest-only terminal records remain visible as failed historical
attempts (`delivery-binding-unproven`) and are never silently treated as accepted.

## Status

Experimental/HOLD. The Launcher/runtime seam and responsive surface are product-layer
work outside formal Stage 0 acceptance. The current launch is a bounded first-surface
private preview, not a persistent interactive session, daemon installation, enabled
service, or published AppStore package. Windows and Linux remain unverified.

## Local macOS bundle

### Open an installed application directly

The native launcher accepts `--open-app APP_ID`. It opens the same independent
window as the GUI's Run button, using the catalog's validated identity, window
metadata, and ordinary runtime binding. It does not install, enable, grant a
permission, or substitute a static preview for an application.

For isolated local acceptance, `--data-dir ABSOLUTE_PRIVATE_DIRECTORY` selects an
existing canonical directory (owned by the current user, mode `0700` on Unix).
It never copies the regular workspace's application data or model credentials.
Prepare this workspace using the normal verified-candidate import and lifecycle
commands; a disabled or incompatible application is still rejected. Network
settings belong to that selected workspace as well; disable P2P there when a
network-free visual check is intended.

```sh
target/release/vibapp-launcher --open-app ai.vibapp.everyday.clock
target/release/vibapp-launcher --data-dir /absolute/private/workspace --open-app ai.vibapp.everyday.clock
```

Build into the same target directory consumed by the package script, then package:

```sh
CARGO_TARGET_DIR="$PWD/artifacts/desktop/target" cargo build \
  --manifest-path artifacts/desktop/src-tauri/Cargo.toml --locked \
  --bin vibapp-launcher --bin vibapp-runtime
artifacts/desktop/scripts/package-macos.sh debug
```

For a release bundle, add `--release` to the Cargo command and pass `release` to the
package script. `package-macos.sh` uses `CARGO_TARGET_DIR` when set and otherwise uses
`artifacts/desktop/target`; it rejects either executable when it is older than any
desktop Rust source, `Cargo.toml`, `Cargo.lock`, or `build.rs`, and prints the exact
aligned rebuild command. The bundle is written to `dist/VibApp.app` and includes the separately signed runtime but no
default generated Component or launchable generated candidate. The experimental
Registry CLI and its bounded synthetic catalog are copied into bundle resources for
the retrieval-first path. The public AppStore projection is copied only from the
stable `product-platform/registry/generated/production-public-data` root produced by
the Website synchronizer; local Website preview mode never becomes a packaging input.
The local Orchestrator, pluggable CodeAgent Adapter, and
cloud-agent validation resources, task-bound dry-run adapter, authoritative WIT, Need Analyzer, and product
LLM environment example are also bundled. Diagnostic compiler fixtures are not
bundled into the product client.
The script ad-hoc signs both Mach-O executables with hardened-runtime options, seals
the outer app, and requires `codesign --verify --deep --strict` to pass before the
new bundle replaces the prior bundle. It also bundles the pinned current RoomHash
headless tree and a standalone arm64 Node runtime. A Node binary with Homebrew,
`@rpath`, or other non-system dynamic-library dependencies is rejected before and
after staging; set `VIBAPP_ROOMHASH_NODE_BIN` to a standalone binary when the default
`node` is not relocatable. After packaging, run
`node scripts/tests/smoke_packaged_roomhash.mjs dist/VibApp.app` to exercise the
bundle's real WebTorrent and collaboration startup/shutdown lifecycle.
Python 3.11+ with `tomllib` remains a current-host runtime dependency; an
older system Python fails closed. Packaging signs locally but does not install,
notarize, deploy, or publish.
