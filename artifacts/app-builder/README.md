# VibApp product Builder and independent Verifier

This directory closes the local product handoff boundary:

`CodeAgent source handoff -> bounded Builder quarantine -> independent Verifier -> candidate-ready -> Runtime Daemon admission`

The Builder never installs, publishes, or creates `candidate-ready`. It accepts the
strict `vibapp.codeagent-source-handoff.experimental-v2` subset, copies only its
declared regular files into a private job, and places output in quarantine. The
Verifier independently reopens and rehashes the exact package bytes, validates the
Component and its WIT surface with the pinned local `wasm-tools`, then atomically
creates `candidates/<package_digest_sha256>/candidate.json`.

RFC 0001 section 4 invariant 9 is an executable gate, not a source-code guess. The
Verifier checks one fixed SHA-256-pinned `vibapp-service-runtime` executable, starts
its one-shot descriptor-inspection mode as a bounded child, and calls only the
Guest `describe` export. That Store links the selected world with no ambient WASI
and with every host import restricted to typed unavailable or static, non-effectful
behavior. The returned app id, version, kind, display name, and complete one-to-one
entrypoint id/kind/label/initial-route set must exactly match the manifest. Missing,
extra, duplicate, malformed, trapped, timed-out, or mismatched descriptors prevent
promotion and create a durable Verifier rejection.

Service trigger intent is carried explicitly from the consent-bound CodeAgent task
through the source handoff into the manifest. The Builder no longer invents
`on-enable`, `scheduler`, and `manual` for every service. Within the current v2
contract, an omitted service trigger hint is conservatively packaged as manual-only;
service/hybrid tasks may declare any canonical subset their accepted task requires.
Historical v1 handoffs are rejected wholesale and are not upgraded by this fallback.

Capability intent is likewise split explicitly. `target.required_imports` remains
the exact structural import set fixed by the selected WIT world, while
`target.required_capabilities` is the consent-bound subset the app actually needs.
The Builder emits every other fixed-world import as `degradable` with a typed denied
stub. Current host availability is a separate upper bound: a genuinely required
unavailable capability remains truthful in the package and is blocked by daemon
admission rather than being silently advertised as usable.
The quarantine receipt carries both that exact required subset and an exact
`package_entrypoints` projection (`id`, `kind`, `label`, initial route, and service
triggers). The Verifier reconciles both projections against the manifest, and the
delivery controller separately reconciles them against the original consent-bound
task and source handoff before promotion or terminal replay. Receipts predating the
entrypoint projection fail closed rather than silently regaining install authority.
The current receipt is `vibapp.builder-quarantine.experimental-v2`; v1 receipts that
lack either binding remain immutable history and cannot be promoted.

## Safety boundaries

- One Builder or Verifier job at a time per output root.
- Every child process has finite wall, CPU, RSS, PID, disk, stdout, stderr, and
  open-file ceilings. Aggregate RSS and PID counts are polled by the parent.
- Source paths must be canonical regular files with one link; symlinks, hard links,
  undeclared files, `Cargo.lock`, `build.rs`, and `.cargo` are rejected.
- Network, the user's home directory, Docker sockets, install, run, and publish are
  outside this component's authority.
- Browser derivation is rejected because no independently trusted derivation runner
  is available in this workstream.

The local macOS runner now has an independently checked, content-addressed offline
Cargo cache. Its fixed discovery paths are:

- Cargo home: `generated/builder-cargo-cache-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d/cargo-home`
- Acceptance: the sibling `acceptance.json`
- Cache evidence: the sibling `independent-evidence.json`

The cache contains the 34 exact crates in the reference lock, is sealed to
directories `0555` and files `0444`, and rejects symlinks, hardlinks, wrong owners,
special files, checksum drift, and unlisted native input. Its two proc macros are
recorded in `package-inventory.json`. Assembly uses only explicitly named local
`.crate` archive directories; it does not access the network.

This is still a bounded local product runner, not formal Stage 0 production
isolation. On macOS it uses the accepted Cargo/Rust layer plus the installed Xcode
clang and SDK, runs one Cargo job, and parent-supervises the build process group at
64 PIDs and 2 GiB aggregate RSS. Darwin's per-UID `RLIMIT_NPROC` is deliberately not
used because it would count unrelated processes owned by the logged-in user.

## Commands

Run the bounded test suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s artifacts/app-builder/tests -p 'test_*.py' -v
```

Build real untrusted source when an accepted offline cache and tool layer exist:

```sh
python3 artifacts/app-builder/app_builder.py build HANDOFF.json \
  --output-root OUTPUT \
  --tool-layer generated/TOOL_LAYER \
  --cargo-home generated/READ_ONLY_CARGO_HOME \
  --cache-acceptance generated/CACHE_ACCEPTANCE.json
```

Recheck the fixed cache without rebuilding or writing inside `cargo-home`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 artifacts/app-builder/offline_cache.py verify \
  generated/builder-cargo-cache-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d
```

The real runner smoke compiles two fresh CodeAgent-policy source trees in the
network-denied macOS sandbox, validates each Component with the accepted
`wasm-tools`, and requires identical bytes:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 artifacts/app-builder/automatic_runner_smoke.py \
  generated/builder-cargo-cache-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d \
  --output-root generated/builder-cargo-cache-runner-smoke-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d-v5
```

The CodeAgent/Builder source boundary requires `#![no_std]`, `alloc`, an explicit
global allocator and panic handler, and exact `wit-bindgen 0.60.0` features without
`std`. The smoke's two Components contain no ambient WASI CLI/I/O imports, report
the exact `wit-bindgen-rust 0.60.0` producer, and are both promoted to
`candidate-ready` by the independent Verifier. Candidate readiness does not grant
install, publish, Registry, or formal Stage 0 activation authority.

Verify a quarantine receipt independently:

```sh
python3 artifacts/app-builder/verifier.py verify \
  OUTPUT/jobs/JOB_ID/quarantine/quarantine-receipt.json \
  --output-root OUTPUT
```

Verifier preflight now requires both the accepted `wasm-tools` bytes and the fixed
descriptor inspector bytes. Supplying another executable path does not relax the
gate because its SHA-256 must still equal the recorded accepted digest.

For Builder-to-Daemon integration only, `safe_fixture_demo.py` exercises the whole
handoff/quarantine/verifier path with a pinned, already-built real Component. It
does not execute or compile the fixture source and says so in both provenance and
stdout:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 artifacts/app-builder/safe_fixture_demo.py \
  --job-id ws02-safe-hello-1
```

This product-layer prototype does not change the formal Stage 0 acceptance status.
