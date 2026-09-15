# RoomHash integration for VibApp

Status: trusted transport, Desktop public fetch/verify/install, Launcher-level
collaboration, and the Website trusted-parent foreground fetch-to-cache route are
implemented locally. Public Registry publication currently emits an empty locator
index, so neither public consumer has completed a product-level run against a genuine
published package. Generated-app collaboration ABI work remains explicitly pending.
This does not change the accepted Stage 0 app ABI or constitute formal Stage 0
acceptance.

## Decision

VibApp will reuse the current RoomHash WebTorrent, channel, and WebRTC implementation behind a host-owned adapter. A Rust rewrite is optional later work and must not block the first working network path. Applications never link RoomHash directly and never receive raw torrent, RTC, tracker, socket, or arbitrary network access.

The adapter boundary is deliberately stable:

- package distribution: content-addressed fetch and seed operations;
- collaboration: create, join, leave, publish event, receive event, and snapshot operations;
- local-private state: existing VibApp host `kv`, not a network room;
- settings/status: bounded resource controls and a user-visible network status.

The first implementation may use the existing browser JavaScript and headless Node code. A later native/Rust transport may replace it without changing VibApp application manifests or guest code.

## Three separate namespaces

1. A torrent swarm locates immutable bytes. It is global and independent of collaboration channels.
2. A temporary channel identifies a live collaboration session. It is explicitly created or joined, expires, and can be rotated or revoked.
3. Local-private storage identifies host-owned per-user/per-app state. It never joins or advertises on the network.

The compatibility UUID `00000000-0000-0000-0000-000000000000` may be accepted only as an internal marker for `LocalPrivate`. The host must reject it in every network join, advertisement, tracker, gossip, or invitation path. Storage keys must additionally bind the authenticated user, app id, package generation/digest, and namespace so two apps cannot collide.

## Package trust

A WebTorrent infohash is only a locator and piece-integrity mechanism. It is not publisher identity and is never sufficient to install or run a VibApp. Downloaded packages continue through VibApp's SHA-256 binding, signature/provenance checks, Builder/Verifier evidence, and private/public AppStore policy.

Public verified packages may be seeded independently of channels. Private artifacts and user files must be encrypted before seeding, with decryption grants delivered separately from the magnet. Arbitrary magnets are not cached automatically; the trusted node accepts only verified application artifacts or files the user explicitly selected.

## Collaboration trust

A RoomHash UUID is a high-entropy rendezvous secret, not a user identity or complete access-control system. Shared sessions therefore require:

- explicit create/join/share consent and a visible leave action;
- expiry, rotation, revocation, peer blocking, and rate limits;
- authenticated peer identity and signed/encrypted application messages where the scenario needs identity;
- deduplication, ordering/version rules, snapshots, and recovery owned by the application protocol;
- no shared-session UUID in channel-directory advertisements.

The host exposes typed application events through a broker. This preserves the Stage 0 rule that untrusted guests have no ambient network capability.

## Product topology

- Website: a foreground browser peer owned by the trusted parent. For a public
  candidate, the parent binds the locator index to the exact Registry snapshot and
  canonical record digest, verifies the locator, then stages the checked files into
  IndexedDB before returning a cache receipt. It may transfer while open but is not
  presented as an always-on seed, an installed app, or a guest capability.
- Desktop GUI: a foreground peer plus a trusted Node companion owned by the Launcher.
  The companion ends with the Client; it is not yet an independently installed
  always-on service.
- Separate headless node: a possible later deployment for durable seeding and
  cross-network bridging, not a completed product lifecycle today.
- Registry/HTTP delivery remains independent of a P2P locator and is still required
  for discovery, trust metadata, and fallback availability.

The isolated application preview origin keeps network access disabled. P2P and RTC live in the trusted launcher parent/backend and are reachable only through capability-checked messages.

## Implemented now

- Desktop owns a bounded JSONL Node companion that runs the current RoomHash
  WebTorrent 3.0.16 service. Process discovery, environment, paths, request sizes,
  timeouts, torrent counts, and shutdown are constrained by the Rust controller.
- Package seed/fetch verifies the independent-verifier candidate, the manifest and
  every declared file. A real second WebTorrent peer is exercised by the transport
  tests.
- Persisted locators are exact, digest-bound, limited to 64 MiB and 128 files, allow
  only WSS trackers, and reject links, unsafe directory chains, unknown fields and
  replacement races.
- Automatic desktop seeding is limited to `published + verified + public-appstore`.
  Private candidates are never automatically placed on the global swarm.
- The Desktop public install consumer is implemented: given a Registry-bound locator
  in its private store, it fetches into a confined managed directory, checks the
  locator receipt and every file, runs ordinary local AppStore ingestion again, and
  only then installs the app in the disabled state for the runtime daemon.
- The public locator build step accepts only exact digest-named sidecars bound to a
  `published + verified + not-revoked + public-source` Registry record. Its index is
  hash-bound to the exact public Registry snapshot and canonical record bytes, and a
  complete staged replacement removes stale/private output.
- The Website trusted parent implements the same-origin public locator lookup and a
  bounded RoomHash fetch into an IndexedDB package store. Files stay hidden in a
  staging record until the complete declared inventory, sizes and SHA-256 digests
  pass; success exposes only a small cache receipt to the credentialless Launcher
  frame. The store and transport adapter pass focused tests, and the trusted-parent
  route compiles in the successful Website local build.
- Desktop and Website expose app-bound temporary collaboration sessions with explicit
  confirmation, expiry, bounded messages, receive queues, deduplication and a visible
  leave operation. The nil UUID is rejected before transport access.
- The shared bilingual Settings UI controls bandwidth, cache/concurrency policy,
  public package seeding and RTC. It mentions RoomHash only as the underlying P2P/RTC
  network.

## Isolated end-to-end acceptance (2026-08-27)

The transport and trust mechanics now pass an isolated synthetic-public acceptance
without changing the production Registry or granting publication authority to the
source candidate:

- `roomhash-public-flow-acceptance.mjs` generated an exact public-data fixture from
  the independently verified `ai.vibapp.hello` candidate and a real seed locator.
  Ten snapshot/record/locator/package/source bindings passed. The source candidate
  remains `authority.publish=none`.
- The ignored Desktop acceptance test consumed that fixture through the strict
  Registry loader, persisted its locator, transferred all five files (54,778 bytes)
  between two real RoomHash processes, independently ingested the received candidate,
  and installed it as `installed-disabled` with `launch_eligible=false`.
- `npm run accept:roomhash-public-cache` used two isolated Chrome profiles and the
  vendored WebTorrent/WebRTC implementation. It transferred and independently hashed
  the same five files, committed an IndexedDB receipt, and proved that no candidate
  bytes were fetched over HTTP, executed as Wasm, installed, or returned to the
  untrusted GUI frame.
- Playwright drove the actual nested Website GUI, found the `AppStore 公开` candidate,
  clicked `下载并校验`, observed the busy state, and observed the trusted parent open
  WebSocket tracker connections. The deterministic byte-transfer gate remains the
  isolated local tracker; public WSS trackers are only a non-blocking network smoke.

This acceptance is deliberately labelled synthetic-public. It proves the complete
mechanism and its authority boundaries, not genuine production Registry publication.
The production projection still has an empty locator index.

During the browser run, a real adapter bug was found and fixed: product settings use
`0` for unlimited bandwidth, while WebTorrent interprets rate `0` as an enabled
zero-byte throttle. The browser adapter now maps `0` to WebTorrent's `-1` unlimited
sentinel, with a regression test.

## Honest remaining boundaries

- The current public locator index contains **zero entries**, and the production
  Website Registry projection contains **zero real public browser runtime bindings**.
  Synthetic Registry fixtures and the private/local Hello preview are not public
  distribution evidence. Consequently, the implemented Desktop install consumer has
  not yet completed a product-level acceptance run against a genuine public package.
- Browser download/cache is byte acquisition only. The Website
  `submit_install_intent` path still reports `installation_performed=false`; a cache
  receipt is not installation, activation, publication, or execution authority.
  Executing cached P2P bytes would require a separately accepted content-addressed
  asset resolver or byte-loading Component runtime; the current strict preview host
  runs only pre-deployed, independently bound artifacts.
- The current accepted Stage 0 guest WIT has no collaboration import/event. The broker
  is usable by the launcher product today, but generated Components need a future
  capability-gated ABI revision before they can directly consume live events.
- The Desktop Node companion cannot safely apply the stored custom TURN override and
  reports `turn-unsupported`. The Website broker also refuses that setting rather
  than exposing the write-only credential to browser code.
- Desktop restart-time re-seeding is not yet closed for a downloaded package absent
  from the current catalog projection, and there is no separately installed durable
  headless peer.

## User settings

The shared Desktop/Website GUI exposes product-level controls without making RoomHash a primary product concept:

- P2P participation;
- upload and download limits;
- verified-app seeding and explicit user-file seeding;
- cache size and concurrent-transfer limit;
- RTC participation and active-channel limit;
- optional TURN URLs, username, and write-only credential;
- later: live peer/transfer/channel status and per-transfer controls.

The UI description may state: “文件 P2P 传输与实时协作基于 RoomHash 网络。”

## Delivery progress

1. **Complete locally:** reuse and wrap the current RoomHash implementation with conformance
   and real-peer transport tests.
2. **Consumers implemented / public evidence pending:** locator publication, the
   Desktop fetch → verify → AppStore ingest → disabled-install path, and the Website
   trusted-parent fetch → verify → IndexedDB-cache path are implemented. The current
   index has no real locator, so neither product flow has genuine public-package
   acceptance evidence; Website caching also deliberately stops before execution.
3. **Launcher complete / guest pending:** explicit temporary sessions and typed broker
   events are implemented; a future WIT revision is needed for direct guest use.
4. **Launcher-lifetime only:** Desktop owns its bounded companion and Website is
   intentionally foreground-only; a durable always-on service remains future work.
5. **Deferred:** consider Rust only if measured portability, memory, lifecycle, or
   maintenance evidence justifies replacing the current implementation.
