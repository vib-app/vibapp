# VibApp Web preview origin host

This ignored product slice serves the existing shared Web GUI from a distinct,
credential-rejecting origin. It does not copy or fork the GUI source: static files
come from `../product-platform/website/public` and remain covered by the existing
Desktop/Web exact-source tests.

The broker refuses every request containing `Cookie`, `Authorization`, or
`Proxy-Authorization`, emits no `Set-Cookie`, rejects an unexpected `Host`, exposes
only an allowlisted static tree, and binds its parent through an exact configured
origin, a fragment-carried fresh nonce, and one transferred `MessagePort`. The
Launcher is loaded only after that binding succeeds.

## Local run

Both origins are mandatory and local origins must be explicit loopback HTTP origins
with ports. For a website on port 4173 and preview host on port 8788:

```sh
NODE_ENV=development \
VIBAPP_PARENT_ORIGIN=http://127.0.0.1:4173 \
VIBAPP_PREVIEW_ORIGIN=http://127.0.0.1:8788 \
VIBAPP_PREVIEW_AUDIT_PATH=/absolute/private/path/invalid-bootstrap-audit.jsonl \
npm start
```

The website iframe must use the same exact `VIBAPP_PREVIEW_ORIGIN`, set the boolean
`credentialless` attribute, create a fresh nonce in the browser, put it only in the
preview URL fragment, and transfer one port with an exact target origin:

```js
frame.src = previewOrigin + '/preview.html#nonce=' + encodeURIComponent(nonce);
frame.contentWindow.postMessage({
  schema_version: 'vibapp.preview-frame-bind.experimental-v1',
  kind: 'bind-preview-frame',
  channel_id: channelId,
  nonce,
  preview_origin: previewOrigin,
}, previewOrigin, [channel.port2]);
```

Production configuration fails closed unless the origins are exactly
`https://preview.vibapp.ai` and `https://vibapp.ai`. The Node process is an HTTP
origin server intended to sit behind the TLS terminator for `preview.vibapp.ai`:

```sh
NODE_ENV=production \
VIBAPP_PARENT_ORIGIN=https://vibapp.ai \
VIBAPP_PREVIEW_ORIGIN=https://preview.vibapp.ai \
VIBAPP_PREVIEW_PORT=8788 \
VIBAPP_PREVIEW_AUDIT_PATH=/absolute/private/path/invalid-bootstrap-audit.jsonl \
npm start
```

## Honest boundary

`GET /preview-boundary.json` is the machine-readable source of truth. Its isolation
state is `partial` and `stage0_activation_eligible` is always `false`.

Invalid parent/Worker bootstrap attempts are recorded by the trusted host as a
privacy-minimal canonical JSONL event: host event ID/time, enum source, enum reason,
and rejection result only. The store never accepts or records a message payload,
NeedSpec, user/session/nonce value, API key, cookie, credential, token, URL, or request
header. It is capped at 64 KiB and 256 events per file, one rotated file, seven-day
retention, and 16 client reports per page. A truncated final record is recovered;
complete-record corruption fails the whole preview closed with HTTP 503. Production
requires the explicit absolute audit path shown above.

Executable Blob/data module URLs have been removed. Jco and its host adapter are
same-origin content-addressed modules, and the host rehashes each component response
against the digest in its filename. The CSP remains broader than the exact
`ISO-PREVIEW-01` minimum: Jco compilation needs `'wasm-unsafe-eval'`, and
launcher/Worker reverification needs `connect-src 'self'`. A separate local verifier
freshly rederives and byte-checks the artifact, but that implementer-run evidence is
not fresh formal acceptance. The nested preview shell and Launcher also currently
share one origin while using `allow-scripts allow-same-origin`; the product-only
activation proposal requires those execution origins to be distinct. This remains a runnable two-origin product preview boundary,
not completed Stage 0 preview isolation, installation, or publication evidence.

## Validation

```sh
npm test
```

The test starts two actual loopback HTTP origins and verifies exact target-origin
markup, credentialless iframe configuration, cookie/authorization rejection,
absence of `Set-Cookie`, host/path fail-closed behavior, response digest enforcement,
strict executable-source CSP, exact static bytes, partial boundary reporting, and
nonce/port/origin binding validation, bounded rotation/retention, tail recovery, and
complete-corruption fail-closed behavior. It does not substitute for a real-browser
isolation oracle or fresh formal acceptance.
