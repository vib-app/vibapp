# VibApp WebAssembly GUI

This is the browser edition of the VibApp Client GUI. It does not maintain a second
HTML/JavaScript product UI: every build regenerates `public/launcher` from
`artifacts/desktop/ui` and injects only the reviewed Web adapter.

## Sites deployment

The Sites deployment currently hosts the shared GUI as a trusted, same-origin
shell. It does not host the Node/Rust backend or a CodeAgent. Requirement analysis,
development submission, and service settings remain explicitly unavailable until
an authenticated remote backend is connected. No local credentials or private
tasks are published. The current production catalog has no eligible public apps.

Guest execution is disabled in this shell by both the bridge and asset CSP
(`worker-src 'none'`). This is not a relaxation of the separate-origin guest
runtime: enabling app execution requires connecting that boundary separately.
Language preferences still belong to the current browser.

Prepare an independent Site source checkout with `npm run prebuild`, then
`node scripts/prepare-sites-source.mjs <empty-absolute-directory> <site-origin>`.
The exporter copies an explicit source allowlist, requires a production-only
Registry snapshot, and hashes the exact shared GUI assets. Its standalone
`npm run build` verifies those assets and produces the usual Vinext Worker.
Never initialize or push the parent VibApp Git repository as the Sites source.
Keep `.openai/hosting.json`'s project ID for later releases; do not create another Site.

```sh
npm run build:local
npm run start:local
```

Open `http://127.0.0.1:3000/`. A shareable app URL is
`http://127.0.0.1:3000/apps/<app-id>`; unknown IDs return Not Found. The local Python
compatibility server in `artifacts/website` serves this same generated launcher and
no longer carries its former standalone UI files.

The runnable Hello preview is derived by Jco from the exact verifier-promoted WASI
0.2 Component, then its ESM and attestation are verified in both the page and a
dedicated foreground Worker. It has no install, publication, DOM, network, persistent
storage, service-worker, or reliable background authority.

NeedSpec preprocessing, Registry routing, CodeAgent settings, exact consent-bound
task creation, automatic CodeAgent → Builder → Verifier delivery history, and the
private AppStore catalog run through the trusted server-side product bridge. That
headless bridge directly reuses the Desktop Rust product modules; the credentialless
Launcher iframe never receives the backend token, provider credentials, or filesystem
paths. BYOM keys are stored only in the bridge's private data directory. No model or
CodeAgent call occurs at startup: the shared GUI must first receive the corresponding
user action and explicit consent.

`start:local` accepts the bridge only after a bounded `health` invocation matches the
SHA-256 of the current Desktop/UI/controller/adapter inputs and the current delivery
contracts. An executable file or a backend `/healthz` response alone is not readiness
evidence; a stale bridge fails before the Website starts.

The ordinary public Website is intentionally read-only at the product API for now.
Public Registry discovery and shareable verified-app routes work, while private jobs,
shared settings, consent creation, and development submission return
`authenticated-user-session-required`. Those mutations remain disabled until user
and session principals, CSRF/anti-replay protection, and principal-scoped bridge data
namespaces are implemented. Local loopback mode keeps the trusted single-user bridge.

Network settings are part of that same generated GUI and use the trusted product
bridge for bounded persistence. They describe the RoomHash-backed P2P/RTC adapter but
do not give the credentialless Launcher iframe or an untrusted application raw
network access.

The trusted parent now implements a foreground public-package cache route. It checks
that the same-origin locator index hashes the exact Registry snapshot and canonical
public record, verifies the digest-named locator and its fixed trackers, then streams
the declared candidate files through RoomHash into a bounded IndexedDB staging area.
Only a complete size/SHA-256-verified inventory is committed, and only a small cache
receipt crosses back to the Launcher frame. The browser tab remains a foreground-only
peer: there is no Service Worker/background promise, the cache may be evicted, and
durable seeding still requires a native/headless node.

This cache is not an application runtime. Website `submit_install_intent` still
returns `installation_performed=false`; cached bytes are not installed, activated,
published, or executable. The current strict preview runtime loads only pre-deployed,
independently content-bound browser artifacts. Connecting cached packages to an
accepted byte-loading Component runtime is separate future work.

The generated public locator index currently has **zero entries**, and the production
Registry snapshot has **zero real browser runtime bindings**. The cache route, store,
and transport adapter are locally build/test checked, but no genuine public package
exists in this repository with which to run a user-click end-to-end acceptance.
Synthetic Registry records and the local Hello preview are not substitutes for that
evidence.

The derivation is real but remains a local product implementation: the canonical manifest declares only
`desktop`, so `stage0_activation_eligible=false` until an independent verifier promotes
a manifest with the matching browser profile and derivation descriptor.

Checks:

```sh
npm run lint
npm run build
node --test ../../web-client-core/tests/*.test.mjs
```

## Local production preview

After a local-origin build, start the Website on loopback with the matching preview
origin and CSP policy:

```sh
npm run build:local
npm run start:local
```

Both commands set the exact `http://127.0.0.1:4174` preview origin, and
`start:local` binds the Website and its product backend to `127.0.0.1`. Build the
headless bridge first (or set `VIBAPP_PRODUCT_BRIDGE_BIN` to an accepted executable):

```sh
CARGO_BUILD_JOBS=2 cargo build --release \
  --manifest-path ../../desktop/src-tauri/Cargo.toml \
  --bin vibapp-product-bridge
```

The ordinary `npm run build` and
`npm run start` remain the production-strict entry points. Both start commands fail
closed when the existing build's CSP was produced for the other preview origin. A
server bearer token alone does not authorize a shared public product backend; public
mutations stay disabled until authenticated principal isolation exists.
