# Public package locators

This directory accepts only locator sidecars emitted by the public Registry
publication pipeline. Each file must be named `<package-digest-sha256>.json` and
must match a `published`, `verified`, `not-revoked` Registry record with a trusted
`source.github_archive` receipt bound to its exact package/source and publisher/app
repository in `vib-app`. Shared `vib-app/sources` requires public source visibility;
legacy per-app source repositories may remain private. `source.visibility=public`
is not a substitute for successful source archival.

Do not place private candidates, development previews, user files, magnets copied
from an untrusted client, or self-certified artifacts here. A locator identifies
immutable bytes only; ordinary VibApp candidate verification and install policy still
apply after download.

`sync-web-gui.mjs` invokes `sync-public-package-locators.mjs` for every Website
development, production, and explicit-local build. Every run emits two complete data
roots: the requested Website projection at `website/public/data`, and an independent
production-public-only projection at `registry/generated/production-public-data` for
Desktop development and packaging. The Website root may contain a loopback-only
private preview; the stable Desktop source never does.

For each root, the synchronizer binds every sidecar to the exact public Registry
record using the record id, revision, canonical-record SHA-256, package digest, and
the SHA-256 of that root's exact Registry snapshot. It validates the snapshot and
locator index in a sibling staging directory, then switches the complete data root,
so consumers can fail briefly during a switch but cannot observe a new snapshot with
an old locator index. A browser-runtime flag is true only for a separately verified
public browser binding with explicit accepted product or Stage 0 activation policy.

An empty `index.json` remains valid when no public locators are present. Output is
regenerated on every build: the complete directory is staged and replaced as one
snapshot, so locators removed from the Registry source cannot survive as stale or
private Website files. Validation errors occur before replacement and retain the
previous complete data root.

The public LED release now supplies a real locator and independently rederived
`web-runtime` binding. Its explicit `product_activation_eligible=true` policy is
separate from historical Stage 0 acceptance, which remains false. The production
projection excludes synthetic fixtures and private previews even when these remain
in the developer Registry source snapshot.
