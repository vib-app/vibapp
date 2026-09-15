# Web Client validation — 2026-08-27

Status: **working local Component preview; local independent-process verifier PASS; formal browser activation still blocked**

## Exact evidence

- Canonical Component: `d3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa`, 46,761 bytes.
- Verifier-promoted package: `dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674`.
- Offline derivation tool: `@bytecodealliance/jco` 1.15.4.
- Deterministic Jco ESM entry: `e929c7497a8e9d4d1876b2c73db63d1fde4a5b6bdaadb7ae12208e08039effc9`, 168,664 bytes.
- Static host-adapter ESM: `da18a880b844cf8f31454d0b58324d34d6e2863e66e0336397343348f6445c1a`, 376 bytes.
- Canonical JSON attestation: `3f0101c741c770c86ed5416fdcaf414084e6258415da173e00bb89cd77f18d86`, 4,695 bytes.
- Browser binding payload: `07ddcc5003a60f048d95fb8f8b8ee78519702500795d0b65db8b701e940be0f7`.

## Passed checks

```text
node --test artifacts/web-client-core/tests/*.test.mjs
12 passed

node artifacts/web-client-core/verify-browser-derivation.mjs
local-independent-browser-derivation-verifier-pass

(cd artifacts/web-preview-host && npm test)
6 passed

/Users/zhuzhe/.rustup/toolchains/1.93.0-aarch64-apple-darwin/bin/cargo test \
  --manifest-path artifacts/web-client-core/Cargo.toml --offline
2 passed

python3 -m unittest discover -s artifacts/website/tests -p 'test_*.py' -v
13 passed

npm run lint
0 errors (generated/shared source warnings only)

npm run build
production and explicit-loopback local builds passed; / and /apps/:appId emitted
```

## Public package locator publication

```text
node --test artifacts/web-client-core/tests/public-package-locator-sync.test.mjs \
  artifacts/web-client-core/tests/contract.test.mjs
21 passed, 1 skipped, 0 failed

node artifacts/web-client-core/sync-public-package-locators.mjs
0 public package locator(s) synced
```

The locator tests prove exact published/verified/non-revoked/public Registry record
binding, canonical fixed-tracker magnets, private-sidecar rejection, rollback on
validation failure, deterministic empty output, and removal of stale target files.
They also verify that Website `predev`, `prebuild`, and `prebuild:local` all enter
`sync-web-gui.mjs`, which invokes the locator synchronizer with the exact public
Registry bytes. The current public locator source intentionally contains no
sidecars, so the generated index is empty and its `registry_snapshot_sha256` matches
the exact Website Registry snapshot; this is transport metadata only, not install or
publication authority.

Node tests rehash the canonical Component, binding, complete static ESM inventory,
and attestation; reject
artifact, attestation, package, and Stage 0 eligibility tampering; repeat the offline
derivation and obtain the same digests; import the exact Jco output and call the real
guest exports; prove executable Blob/data module URLs are absent; and prove exact
Desktop/Web GUI bytes plus bounded index adaptation. The preview host also rejects a
content-addressed filename whose bytes hash differently.

The preview tests also prove canonical privacy-minimal invalid-bootstrap records for
all five closed reasons, fixed count/byte/retention/rotation bounds, truncated-tail
recovery, and HTTP 503 fail-closed behavior for complete-record corruption. The
product-only activation-policy validator passes one positive policy shape and rejects
all seven required negative fixtures. It still reports current activation false and
claims neither formal Stage 0 acceptance nor public authority.

## Real-browser share-route smoke

Playwright opened `/apps/ai.vibapp.hello`, clicked **打开**, and observed the real
Component surface text “Hello from a generated VibApp” and the canonical Component
digest in the runtime toolbar. Network evidence showed HTTP 200 for both the
content-addressed Jco ESM and attestation. Console result: 0 errors, 0 warnings (one
React development info message). Screenshot:
`artifacts/product-platform/website/output/playwright/ws03-canonical-component-share-route.png`.

The legacy Python compatibility server was also exercised through its real HTTP
handler: `/apps/ai.vibapp.hello` redirected to the same generated launcher, both
content-addressed files returned HTTP 200, the Component surface rendered, and the
browser console reported 0 errors and 0 warnings. It did not serve the deleted legacy
`artifacts/website/app.js`, `styles.css`, or `index.html` implementation.

## Remaining blockers

The immutable candidate manifest declares only `desktop` and
`browser_derivations=[]`; Jco 1.15.4 and its browser execution policy are not in the
accepted Stage 0 toolchain; and this implementer-run local verifier is not a fresh
formal acceptance owner. CSP no longer permits executable Blob/data URLs, but Jco
compilation still needs `'wasm-unsafe-eval'` and page/Worker reverification still
needs `connect-src 'self'`, both broader than the exact `ISO-PREVIEW-01` minimum.
The preview shell and nested Launcher also currently share an origin while combining
`allow-scripts` and `allow-same-origin`; the executable proposal requires a distinct
Launcher execution origin before formal activation.
The local preview remains `stage0_activation_eligible=false`; no Stage 0 Web
activation, installation, or publication is claimed.
