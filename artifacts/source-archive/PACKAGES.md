# Experimental GitHub package distribution

## Public Store catalog · 2026-09-15

The real LED clock `ai.vibapp.custom.1a04442610c` 0.1.1 now has a public ZIP and
torrent with independently checked package bytes. Package digest:
`ef40927893f11a379cb036aa33e4f379e7ddd5a29d6aa36b71ec915621f9555d`.
`vib-app/packages/main/registry.json` is the mutable discovery catalog. Source and
runtime download addresses are immutable. Catalog commit:
`d65a6c158ddb450d2483f39e39dcdac9d7941ee5`.

Use `release_pipeline.py publish --list-in-store --output ABSOLUTE_JOB_DIR` for
explicit public package distribution plus listing, or `store_catalog.py --output
ABSOLUTE_JOB_DIR` after a completed release. Neither runs implicitly in CodeAgent
or untrusted CI. Catalog publication requires public-source consent, an exact
post-build source receipt, verified candidate/run bindings and a matching live
public ZIP. Updates preserve other apps and files and never force-push main.

The website reads this fixed public catalog without credentials. A catalog row
grants discovery only, not installation or browser execution; `package-verified`
does not claim Stage 0 activation, publisher certification or browser acceptance.
The LED remains desktop-only. Its browser derivation and ticking acceptance are
still outstanding. The archive-check fixture is deliberately not listed.

## Shared-source routing update · 2026-09-15

New public jobs start with `github_build.py start --public-source --handoff ...
--publisher-id ... --output ...`. The operator must have the app owner's approval
to disclose its source. They stage in the fixed public `vib-app/sources` repository,
run the trusted workflow there, then publish verified runtime packages to
`vib-app/packages`. The source archive is not the runtime download package.
Legacy private jobs/receipts are retained; they are not silently reinterpreted as
having approved public source. `release_pipeline.py` preserves a legacy job's
original archive/run binding, while new shared-source jobs use `upload_public`.

The owner has authorized copying previous test apps into `sources`; this is a
separate source migration, not mutation of already released package provenance.
Source installation access and three source migrations are now verified. The real
LED Actions run `34958597298` passed, as did its separate native artifact verifier.
Browser
derivations and the Store catalog are not added by this routing change; the
five-file runtime payload limitation described below remains in force.

## Live checkpoint · 2026-09-06

The explicitly authorized synthetic `ai.vibapp.archive-check` completed private
source upload, real [Actions compilation](https://github.com/vib-app/app-460d7bbfd320207054c626331f5819d88f1dfd47b96f3ed38e16a1e47636ee20/actions/runs/34038928625),
artifact verification, separate full candidate verification, post-build source
archive, and [public package release](https://github.com/vib-app/packages/releases/tag/vibapp-package-626ab221e217671bc9b1b74a98ae9406e8e75c412b44e3abef4a8ac0d065d31e).
All eight advertised Raw/Release downloads matched exact bytes. A subsequent
full controller retry passed with zero repository-write attempts and unchanged
release receipt (new short-lived token issuance is not a repository write).

Operational evidence is under
`runs/github-release-20260906/build-exec-fixed/`: `remote-evidence.json`,
`source-archive-receipt.json`, `release-receipt.json`, `replay-evidence.json`.
The public payload is only the five files below; source stays private.

Browser testing initially found the upstream WebTorrent bundle's explicit
`Cache-Control` request header triggered an unsupported GitHub Raw CORS
preflight. The real shared browser bundle now has one reproducible, hash-pinned
header correction (`patch-roomhash-webseed-headers.mjs`); upstream proof and MIT
license are retained. Chrome then completed a real zero-peer, tracker-disabled
WebTorrent transfer: 68,643 bytes, all five exact file hashes, one `webSeed` wire,
and HTTP 206 range requests. No proxy, intercepted response, disabled CORS or
global-fetch override was used. Direct cross-origin Range fetch returned 64
matching bytes with `Access-Control-Allow-Origin: *`. Probe code:
`webseed_probe.py`; before/after evidence and screenshot: `output/playwright/`.

The current AppStore locator still consumes a magnet URI: a cold client must
also be wired to load the published `.torrent` metadata to use a webseed with
zero peers. This CLI slice does not update that catalog/GUI flow or deploy an
always-on trusted controller. It does not test other peers or render the app UI.

`package_release.publish_candidate(candidate_path, source_receipt, run_binding,
output_dir, api=None)` is a trusted-host operation for an explicitly authorized
public release to `vib-app/packages` (repository ID `1359065065`). It is not called
by generated source or the compiler, and it does not publish to the AppStore,
update Registry records, install an app, or grant runtime capabilities.

The caller supplies an independently promoted `candidate.json`, the exact
six-field **post-build** private-source archive receipt, and
`{run_id, run_url, workflow_commit, source_commit}` from its trusted job ledger.
`output_dir` must be a private, ignored operational directory. Run the controller
with the existing protected GitHub App setup; this module never falls back to
personal `gh`, shell Git, or owner credentials. The App token is scoped to the
fixed packages repository with `contents:write` only.

## Explicit trusted CLI

After the existing GitHub Builder job has been dispatched, an authorized operator
can explicitly request public sharing with:

```sh
python3 artifacts/source-archive/release_pipeline.py publish --output /absolute/path/to/existing-job
```

This command is the public-sharing action. `github_build start` and `resume` do
not invoke it implicitly; it is not a CI or generated-code entrypoint. The
operator must have authority to disclose the candidate's contents publicly.
The job must retain its trusted `state.json`, frozen handoff/source and pipeline
evidence. A source-staging job is rejected instead of dispatching a new build
through the publication command. An already dispatched but unfinished Actions
run returns `waiting-for-actions` with exit code 2 and performs no publication;
call the same explicit command again after completion.

On every invocation, including retries, the connector calls `github_build.resume`
for complete source/quarantine/candidate reverification. It checks the ledger's
handoff hash, app/publisher identity, staged source receipt, canonical candidate
path, package digest, and run identity. Then it calls the trusted private-source
archiver with the **actual package digest** and exact handoff source digest/root,
obtains GitHub readback, and records `source-archive-receipt.json`. This is a new
post-build six-field receipt, not the pre-build staging receipt with a guessed
package identity. It must agree with the staged repository ID and candidate.

Only then does it invoke the public package publisher at `<job>/publication`.
The connector owns `<job>/release-state.json`, `<job>/release-receipt.json`, and
a per-job `.release.lock`; the Builder retains ownership of `state.json`.
Retries reread the source archive, compare existing receipts without replacing
conflicting data, and rerun the publisher's independent checks and downloads.
Successful completion returns exit code 0, while validation/network/publication
failure returns 1 with a bounded error code. Exceptions, credential contents,
source contents and raw server error bodies are not printed. A publication
failure can retain the verified private-source receipt and partial public
objects, but cannot create a successful release receipt.

## Payload and verification

Only this existing product Builder subset is supported:

```text
candidate.json
package/manifest.json
package/component.wasm
package/provenance.json
package/sbom.cdx.json
```

The bytes are copied unchanged. Extra files/directories, traversal descriptors,
symlinks, hardlinks, non-regular files, duplicate JSON keys, oversized data,
known secret markers, source attachments and mismatched source/run/package
bindings fail closed. The limit is five files, 16 MiB for the Component, 1 MiB
per JSON document, and 20 MiB total. Manifest semantics, artifact sizes and
SHA-256 values, the canonical package digest, exact cloud-run provenance, and
the minimal SBOM are checked against the existing Builder/verifier contract.
The provenance's `builder_image` is the derived Docker image ID, not the
bootstrap-image reference.

`candidate-ready` and the recorded check strings are not accepted as proof:
before constructing the authenticated transport, a separate credential-cleared
process reruns the pinned verifier's Wasm validation/import/metadata checks and
runtime guest-descriptor reconciliation on a fresh five-file copy. Its bytes
are compared again afterward. The caller remains responsible for the full
quarantine/source/consent verification and the trustworthiness of its run ledger;
the publisher does not independently query the private source repository or
reconstruct its archive receipt.

This remains an experimental desktop/headless product subset. It does not add
browser derivations, extra assets, platform claims, production isolation,
reproducibility, signing, license closure, or production supply-chain acceptance.
Public provenance contains existing run identifiers/URLs and digest metadata,
not raw private source. Secret-pattern scanning is a guardrail, not a general
data-loss-prevention proof; an operator must review intentionally embedded app
content before public sharing.

## Immutable distribution layout

The publisher creates a data commit with the existing `main` tree as its base,
preserves every existing file, adds only
`packages/<digest>.vibapp-candidate/{candidate.json,package/*}`, and creates the
new lightweight tag `vibapp-package-<digest>`. It never force-updates a reference
or changes `main`. An empty repository gets one create-only bootstrap file; an
existing conflicting file is not overwritten. Tree readback is bounded at
4,096 entries and must preserve all pre-existing non-directory entries.

The multi-file BitTorrent v1 `info.name` is `<digest>.vibapp-candidate`, matching
the current RoomHash locator display-name contract. Ordered file paths remain
`candidate.json` and `package/*`, and pieces are SHA-1 over their concatenated
bytes with a fixed 256 KiB piece size. The webseed is:

```text
https://raw.githubusercontent.com/vib-app/packages/<data-commit>/packages/
```

A client appends the torrent name and relative file path, as required by
[BEP 19](https://www.bittorrent.org/beps/bep_0019.html). The ZIP is a separate
deterministic, uncompressed archive of those same five paths; it is **not** used
as the multi-file webseed.

A second immutable commit, based on the data commit, adds
`metadata/<digest>.torrent`, tagged `vibapp-torrent-<digest>`. This avoids a hash
cycle: the torrent contains the already fixed data-commit webseed URL, while the
metadata has its own commit-pinned Raw URL. `torrent_url` in the receipt is that
Raw URL; `torrent_release_url` is the duplicate Release asset URL. Clients should
load the published `.torrent`; creating a new torrent from the same directory
with different piece-size options creates a different swarm identity.

The Release is a non-latest prerelease under the data tag. It contains
`<digest>.zip` and `<digest>.torrent`. Both assets and all six Raw objects are
downloaded anonymously and compared byte-for-byte before success is recorded.
Existing asset names, sizes, states, URLs and downloaded bytes are checked on
retry; the publisher never deletes or overwrites an existing Release asset.
The Git tree and Release APIs follow GitHub's documented
[tree operations](https://docs.github.com/en/rest/git/trees) and
[asset operations](https://docs.github.com/en/rest/releases/assets).

The torrent advertises `wss://tracker.webtorrent.dev`; generating this metadata
does not contact the tracker. The receipt also provides a canonical
`magnet_uri`, `tracker_urls`, SHA-1 `info_hash`, SHA-256 download evidence, and
separate data/metadata commit IDs. A magnet alone does not provide torrent
metadata to a zero-peer client. No catalog entry, running seeder, browser CORS,
HTTP Range behavior, tracker reachability, or peer download is implied by the
full-file HTTP checks. Those need their own live acceptance tests.

## Retry and failure states

The private output directory records `<digest>/binding.json`, a per-digest
lock, and, only after all eight anonymous downloads pass, `receipt.json` with
`state=public-downloads-verified` and `appstore_publication=not-performed`.
The binding includes exact candidate, source-receipt, and run-binding hashes;
it is also part of the immutable Git commit and Release body. Conflicting
retries fail rather than replacing content. The same successful request reruns
verification and downloads and performs no remote write.

A network failure can leave an immutable Git tag, Raw objects, or a partially
populated public Release. It is never reported as completed; retry the same
request to inspect and finish the missing assets. No automatic rollback or
deletion is attempted. After an ambiguous ref/asset creation, the next invocation
reads existing state before deciding whether another create is needed. A
historical successful receipt is not a promise of future GitHub availability;
every new invocation checks downloads again. A locally interrupted/incomplete
binding write fails closed and needs operator review.

## Local tests

```sh
python3 -m unittest discover -s artifacts/source-archive/tests -p test_package_release.py -v
python3 -m unittest discover -s artifacts/source-archive/tests -p test_release_pipeline.py -v
```

Tests use synthetic candidate metadata, the repository's existing safe Component,
a mocked GitHub API/download store, and a mocked independent-process seam. They
cover correct ZIP/torrent wire bytes, Raw path resolution, unchanged existing
paths, idempotency, tampering, private attachments, links/traversal/FIFO state,
duplicate JSON, limits, failed verification/downloads, and conflicting releases.
They perform no credential read, source compilation, or remote write. Passing
these protocol tests is not a live publication or a completed peer-transfer test.
The connector tests additionally mock resume, source archive and publication to
verify the exact package/source arguments, mandatory retry reverification,
identity/path changes, waiting/failure states, conflicting receipts and safe CLI
error output.
