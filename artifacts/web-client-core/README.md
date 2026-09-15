# VibApp Web Client core

The shared WebTorrent 3.0.16 browser bundle has a reproducible local webseed-header
patch; see `vendor-roomhash/PATCHES.md` and `patch-roomhash-webseed-headers.mjs`.
It preserves upstream identity/license, Range/cache/cancellation behavior and
avoids explicit request headers rejected by GitHub Raw CORS preflight. A real
Chrome zero-peer transfer from the public synthetic GitHub package passed on
2026-09-06 (`artifacts/source-archive/output/playwright/webseed-after.json`). This
is transport evidence only: the current product locator still needs a separately
integrated `.torrent` metadata URL for cold, zero-peer AppStore downloads.

This ignored product prototype is the WebAssembly control core and browser adapter
for the same GUI source used by the Desktop client. It is not formal Stage 0
acceptance.

`sync-web-gui.mjs` uses the absolute Cargo 1.93.0 toolchain, copies Desktop
`app.js`, `styles.css`, and `favicon.svg` byte-for-byte, and makes only two bounded
`index.html` changes: the Web mode label and Web bridge injection. Exact hashes are
recorded in `public/launcher/gui-sync-manifest.json` and tested for drift.

The same synchronizer also publishes two complete Registry data roots through
`sync-public-package-locators.mjs`: the requested Website projection in
`website/public/data`, plus the stable production-public-only Desktop/package source
in `registry/generated/production-public-data`. Snapshot and locator directory are
validated in staging and switched as one generation. A local Website build may add a
loopback-only private preview to its own root, but cannot mutate the Desktop source
away from production-public-only. Each index is hash-bound to its exact snapshot and
canonical Registry record bytes. With no public sidecars, a deterministic empty index
replaces stale output; validation failure retains the previous complete root.

## Browser public-package cache

The Website trusted parent resolves a requested app and package digest only through
that same-origin, Registry-bound locator index. It rechecks the Registry snapshot and
canonical record hashes, the exact locator bytes, package digest, complete file
inventory, sizes, SHA-256 values, info hash, and fixed WSS trackers before accepting a
transfer.

`roomhash-browser-node.mjs` streams verified candidate files into the bounded
`browser-package-store.mjs` IndexedDB staging store. A package becomes visible only
after an atomic complete-inventory commit; failures abort the hidden staging record,
and success returns a bounded receipt rather than package bytes, a magnet, or an
executable URL. The credentialless Launcher frame and application guests do not
receive the store or raw transport.

This is foreground byte acquisition, not installation or runtime admission. Browser
storage can be evicted, the page provides no reliable closed-tab service, and a cache
receipt never authorizes execution. The current production locator index contains
zero entries and its Registry projection contains zero real browser runtime bindings,
so the store/transport adapter has focused test evidence and the trusted-parent route
has build evidence, but there is no genuine public-package end-to-end acceptance yet.

## Canonical Component browser derivation

`derive-browser-component.mjs` fails closed unless it can rehash the exact
verifier-promoted candidate and observe all required independent candidate checks.
It resolves `@bytecodealliance/jco@1.15.4` from the offline npm cache, records the
exact tool entry/package hashes and command, and deterministically emits a
content-addressed Jco ESM entry plus a content-addressed same-origin host-adapter
module. Core Wasm remains embedded in the Jco entry. No executable `Blob` or `data:`
module URL is used.

The Registry-shaped browser binding ties together:

- canonical package and Component SHA-256/size;
- `derived_from_sha256` and `format=jco-esm`;
- exact Jco output inventory and host-adapter digest;
- a content-addressed canonical JSON derivation attestation.

The page verifies binding, complete file inventory, and attestation bytes. A dedicated
Worker independently refetches and rechecks the inventory, imports the
content-addressed same-origin entry, calls
the Component's real `guest.describe` and `guest.handleEvent` exports, bounds the
semantic UI output, and returns it to the shared host renderer. The old unrelated
44-byte Weather Core Wasm fixture has been removed.

`verify-browser-derivation.mjs` is a separate local verifier executable. It rehashes
the promoted candidate, launches a fresh derivation child process, byte-compares every
output and attestation, and executes the freshly derived guest exports. Its PASS is
local evidence only, not formal Stage 0 acceptance.

## Product-only activation policy proposal

`activation-policy/validate-web-activation.mjs` executes the strict product proposal
in `activation-policy/web-activation-policy.proposal-v1.json` against one positive
shape and seven negative fixtures. It pins the exact canonical/derivation toolchain,
manifest browser-profile relationship, derivation inventory, Worker authority, CSP,
origin separation, privacy-minimal audit, and independent-verifier requirements.
Passing this local proposal does not alter accepted Stage 0 source or activate a
browser package. See `WEB-AUTHORITY-AUDIT-20260827.md` for the authority analysis.

## Honest boundary

The browser Worker now retains a bounded foreground session after initial launch.
Refresh/action requests carry exact app/package/Component/generation/session/
surface/route identities and monotonic sequence numbers. One action is in flight;
invalid traffic, a deadline, page closure or explicit close terminates the Worker.
The shared desktop GUI preserves active editors during refresh. This is tested
separately from native lifecycle behavior.

This does **not** add durable guest KV storage, a closed-tab service, or automatic
Jco derivation of arbitrary generated packages. The local Hello preview still has
an explicit read-only test host adapter. Those application-level browser parity
checks remain open; a native app or a successful model request is not web-runtime
acceptance. Reserved synthetic Registry fixtures are excluded from both local
product/public projections, regardless of their test-only `verified` labels.

The immutable promoted candidate manifest declares only `desktop` and has no accepted
`browser_derivations` entry. Therefore the served binding remains intentionally labeled
`locally-derived-awaiting-independent-verifier` and
`stage0_activation_eligible=false`. This is a working local product preview adapter,
not accepted package activation, installation, publication, or proof that Jco 1.15.4
is part of the accepted Stage 0 toolchain.
