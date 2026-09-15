# Web authority and activation audit — 2026-08-27

Status: **product-only local proposal validated; current preview remains partial and not Stage 0 activation-eligible**

This document audits the implemented product preview against the accepted Stage 0
browser boundary without changing the accepted Stage 0 source. It is evidence and a
proposal, not formal acceptance, deployment, installation, or publication authority.

## Current authority path

```text
https://vibapp.ai (trusted parent UI)
  -> exact target-origin + fresh fragment nonce + one transferred MessagePort
https://preview.vibapp.ai/preview.html (trusted preview broker/banner)
  -> currently same-origin sandboxed launcher iframe
https://preview.vibapp.ai/launcher/index.html (shared Desktop GUI + bounded Web adapter)
  -> content-addressed static Jco entry and host adapter, both rehashed
dedicated Worker (no DOM; foreground-only; no install/permission authority)
```

The generated Website remains mechanically tied to the Desktop GUI. `app.js`,
`styles.css`, and `favicon.svg` are byte-identical. `index.html` has only the mode
label and versioned Web bridge injection described in `gui-sync-manifest.json`.

## CSP and same-origin findings

- Executable `blob:` and `data:` URLs are absent from Jco output, the host adapter,
  `script-src`, and `worker-src`.
- The Jco entry embeds core Wasm and currently requires `script-src 'wasm-unsafe-eval'`.
- The Launcher and Worker refetch and hash exact derivation files and therefore need
  `connect-src 'self'`. CSP cannot restrict that directive to the content-addressed
  path, so the origin server's exact static allowlist, credential-header rejection,
  and no-fallback checks are security controls, not conveniences.
- The preview shell uses `img-src 'self'` only for the bounded invalid-bootstrap
  audit beacon. The endpoint accepts three exact enum query fields and never stores
  the query or request headers.
- The current nested Launcher is same-origin with the preview shell and combines
  `sandbox="allow-scripts allow-same-origin allow-forms"`. `allow-forms` is required
  on both composed iframe layers so trusted user click/Enter reaches the shared GUI;
  every Website/preview/launcher CSP retains `form-action 'none'`, so a form cannot
  navigate or exfiltrate if JavaScript fails. The remaining scripts/same-origin combination is not accepted as a
  formal isolation boundary. The executable proposal requires the Launcher execution
  origin to be distinct from the preview-shell origin before activation can pass.
- The generated guest Component runs in a dedicated Worker with no DOM object. This
  reduces current exposure but does not erase the same-origin document-level blocker.

## Durable invalid-bootstrap audit

The preview host owns a local JSONL audit store with this canonical event shape:

```json
{"event_id":"audit-<uuid>","observed_at_utc":"YYYY-MM-DDTHH:MM:SSZ","reason":"<enum>","result":"rejected-before-authority","schema_version":"vibapp.invalid-bootstrap-audit.experimental-v1","source":"<enum>"}
```

Accepted reasons are `wrong-origin`, `wrong-nonce`, `extra-authority`, `tamper`, and
`replay`. Sources are `preview-frame`, `launcher-runtime`, and `app-worker`.

The store deliberately has no user, app, session, channel, nonce value, request
payload, NeedSpec, API key, cookie, credential, token, header, URL, or message body.
The server supplies the timestamp and event ID. Client reports are limited to 16 per
page, and the server is bounded to 256 events and 64 KiB per file, one 64 KiB rotated
file, and seven-day retention. A trailing incomplete record is removed on recovery.
A malformed complete record, malformed rotated record, noncanonical record, symlink,
or bound violation makes the store unhealthy and every preview route returns 503.

Production startup requires an explicit absolute `VIBAPP_PREVIEW_AUDIT_PATH`. This
proposal still needs deployment-specific ownership, backup, concurrent-process and
filesystem hardening review; no public deployment is claimed.

## Executable activation proposal

`activation-policy/web-activation-policy.proposal-v1.json` and
`activation-policy/validate-web-activation.mjs` define exact candidate checks for:

- Rust 1.93.0 `wasm32-wasip2` canonical input and offline reviewed Jco 1.15.4;
- an `experimental-v0` manifest with an explicit browser/wasm32 `web-preview`
  profile and matching `jco-esm` browser derivation;
- canonical/derived digest equality, complete content-addressed inventory,
  attestation, two-clean-run equality, and a fresh independent verifier;
- dedicated foreground-only Worker execution with no guest DOM/storage/service
  worker/credentials/install authority;
- exact CSP/static-server constraints, origin-separated Launcher execution, exact
  one-time MessagePort bootstrap, and a healthy durable audit.

Seven negative fixtures must reject missing browser profile, a wrong derived digest,
an executable data URL, live browser network/background authority, same-origin nested
Launcher execution, an unhealthy audit, and implementer self-certification. The
validator reports `local-product-proposal-validation-pass` while still reporting
`current_product_activation_eligible=false`, `formal_stage0_acceptance=not-claimed`,
and `public_activation=not-authorized`.

## Remaining formal blockers

1. The immutable promoted candidate manifest declares only `desktop` and no browser
   derivation.
2. Jco 1.15.4 and the browser runtime/derivation policy are not accepted Stage 0
   source or toolchain policy.
3. The browser derivation has local independent-process evidence but no fresh formal
   independent acceptance owner.
4. The current same-origin nested Launcher must move to a distinct execution origin
   or receive a separately accepted isolation design.
5. `'wasm-unsafe-eval'` and `connect-src 'self'` remain broader than the accepted
   `ISO-PREVIEW-01` minimum.
6. The audit file implementation is local product evidence; production filesystem
   ownership, multi-process serialization and operations have not been accepted.
