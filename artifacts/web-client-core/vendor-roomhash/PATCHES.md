# Local WebTorrent browser delta

The reviewed WebTorrent 3.0.16 browser ESM is retained as upstream SHA-256
`50bc3160dadadac2306a41908aeec90335b6fad93847b55eb23b7ef906e55c5a`
(239,513 bytes). Existing MIT, package resolution and NPM integrity evidence in
`PROVENANCE.json` is preserved under the original fields; `upstream_bundle`
records the unchanged upstream distribution identity.

The local browser-only patch removes exactly the explicit `Cache-Control` and
`user-agent` request headers from one webseed fetch option block. The browser
still chooses its normal User-Agent. `fetch` retains `cache: "no-store"`, method
`GET`, the single byte `range` header, and `AbortSignal.timeout(60000)`.
The upstream Node package and every other byte of the browser bundle are
unchanged. There is no global fetch override, response substitution, routing
interception, permission expansion or change to torrent hashing.

This delta addresses the observed browser request preflight failure against
GitHub Raw while preserving normal byte-range webseed downloads. Passing the
mechanical tests is not a claim that a real browser transfer has passed; the
separate acceptance probe must exercise this actual vendored library.

Reproduce from the already present, read-only RoomHash upstream package and then
refresh the generated shared website copy:

```sh
node artifacts/web-client-core/patch-roomhash-webseed-headers.mjs
node artifacts/web-client-core/sync-roomhash-browser.mjs
node --test artifacts/web-client-core/tests/roomhash-webseed-patch.test.mjs artifacts/web-client-core/tests/roomhash-browser-sync.test.mjs
```

The patch rejects an unknown upstream hash, any unreviewed destination change,
incorrect upstream proof, links, or a different minified fetch block. Reapplying
it from the pinned upstream input is idempotent. The patched bundle is exactly
239,430 bytes with SHA-256
`1de6dcc5cba63ab802a02c4d1fa024f5768c8af47fa4a4dd96076aae5e4c098e`.
`local_patches` binds the patch script digest and exact input/output hashes.
The existing sync verifies that output against provenance before copying it and
records its hash in generated `SYNC.json`.
