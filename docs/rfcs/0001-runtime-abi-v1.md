# RFC 0001: VibApp experimental-v0 package and component contract

- Status: Frozen for the Stage 0 conformance experiment
- Date: 2026-08-11
- Scope: Package, manifest, WIT, compatibility, lifecycle, and conformance semantics
- Decision owner: VibApp architecture council
- Support promise: None; this is unpublished `experimental-v0`, not ABI v1

## 1. Decision

VibApp packages contain a Rust-authored WebAssembly Component targeting
`wasm32-wasip2`. A trusted daemon, never the GUI process, instantiates the component,
owns durable state and background work, grants brokered capabilities, validates every
guest output, and routes host-rendered UI surfaces. Third-party native Rust dynamic
libraries are not accepted.

This RFC freezes one reversible experiment. A host accepts a package only when its
exact package format, manifest schema, WIT contract, world, imports, profile,
platform tuple, grants, and resource request all pass validation before activation.
Any mismatch fails closed with a typed error. No current or future VibApp release is
obligated to support `experimental-v0@0.0.1`.

The normative machine-readable artifacts are:

- [`contract.wit`](../../wit/experimental-v0/contract.wit), package
  `vibapp:experimental-v0@0.0.1`;
- [`manifest.experimental-v0.schema.json`](../../schemas/manifest.experimental-v0.schema.json);
- [`vectors.json`](../../fixtures/conformance/experimental-v0/vectors.json) and its
  referenced valid manifests.

If prose and a machine-readable artifact disagree, the gate is failed and the
contract must be revised before bindings or runtime crates are generated.

## 2. Goals and non-goals

Goals:

- one canonical component for desktop and headless hosts;
- trusted browser derivations tied to that canonical component;
- UI-only, service-only, and hybrid packages;
- a desktop Launcher that is the shell for every installed UI surface, not merely
  the Store/search/chat UI;
- the same daemon control envelope for Launcher and CLI;
- host-owned scheduling, notification delivery, state, permissions, lifecycle, and
  transactional generation switching;
- no GUI-framework, DOM, Rust-layout, allocator, pointer, descriptor, or device type
  crossing the boundary;
- deterministic validation inputs suitable for Registry filtering and a future
  project-local `.agents/skills/vibapp` authoring skill.

Non-goals:

- native `dylib`/`cdylib` plugins, arbitrary HTML/JavaScript, raw DOM access, shell,
  process spawning, raw filesystem paths, raw sockets, environment inheritance, or
  ambient hardware access;
- reliable background work after a browser page closes;
- a production signing, archive transport, public Registry, public ABI lifetime, or
  legacy-adapter policy;
- Stage 0 live HTTP egress. The WIT names a future scoped HTTP broker, but the Stage 0
  service fixture receives only an unavailable typed stub and the preview receives a
  mock; neither performs network traffic.

## 3. Logical package and canonical digest

### 3.1 Layout

A package is a logical directory tree, not an archive-format promise:

```text
manifest.json                         required
component.wasm                       required canonical component
provenance.json                      required
sbom.cdx.json                        required
assets/**                            optional, only when declared
web-preview/**                       optional trusted derivation files
web-runtime/**                       optional trusted derivation files
signature.json                       optional transport sidecar, excluded from digest
```

Every regular file other than `manifest.json` and optional `signature.json` must be
described exactly once by the artifact set below, and every descriptor must resolve
to exactly one regular file. Extra files, missing files, symlinks, hard links, device
nodes, sockets, and named pipes are rejected. Empty directories have no meaning.

The artifact set is:

1. `artifacts.canonical_component`;
2. every `artifacts.assets[]` item;
3. every browser derivation's `files[]` and `derivation_attestation`;
4. `artifacts.provenance` and `artifacts.sbom`.

A derivation's `entry` is a selector, not a second artifact: it must be byte-for-byte
equal as a JSON value to exactly one item in that derivation's `files[]`.

Artifact paths are relative UTF-8 NFC strings using `/`. The schema limits them to
ASCII, but the validator still applies NFC before comparison. A path must not be
absolute, empty, end in `/`, contain `\\`, `//`, a `.` or `..` segment, a NUL, or
collide with another normalized path. Comparison and ordering use normalized UTF-8
bytes. Artifact SHA-256 is 64 lowercase hexadecimal characters and `size_bytes` must
equal the file's byte length.

### 3.2 Digest algorithm

The parser must reject duplicate JSON object keys. It then validates the manifest
and serializes the parsed value with RFC 8785 JSON Canonicalization Scheme (JCS) to
UTF-8 bytes `M`.

Let `D` be the artifact set sorted by normalized path bytes. For each descriptor `d`,
let `path(d)` be its normalized UTF-8 path, `sha(d)` the 32 raw bytes decoded from its
hex digest, and `size(d)` its unsigned size. The package preimage is exactly:

```text
ASCII("VIBAPP-PACKAGE") || 0x00 || ASCII("experimental-v0") || 0x00 ||
U64BE(len(M)) || M ||
for d in D:
  U16BE(len(path(d))) || path(d) || sha(d) || U64BE(size(d))
```

Before computing the root, the validator hashes every artifact's actual bytes and
checks its size. The package digest is lowercase hexadecimal
`SHA-256(package-preimage)`. It is the external immutable package identity and is not
embedded in `manifest.json`, avoiding a self-reference. `signature.json`, if present,
signs this digest under a future policy; it is not an input to the digest. The Stage 0
gate does not treat the sidecar as production trust evidence.

Published fixture digests are immutable. Mutable names such as `latest` never enter
admission, consent, activation, or rollback decisions.

## 4. Manifest contract

The JSON Schema is strict Draft 2020-12 with `additionalProperties: false`. Required
domains are schema/package versions; app/publisher identity; canonical, asset,
browser-derived, provenance, and SBOM artifacts; WIT contract/WASI/world/imports;
profiles and platform tuples; entrypoints; capability grants/scopes/profile behavior;
resource bounds; state migration range; lifecycle/data disposition; Rust source and
builder identity; license; privacy; and verification/revocation state.

Schema validity is necessary but not sufficient. Admission must also enforce all of
these semantic invariants:

1. Artifact paths, entrypoint IDs, runtime profiles, platform tuples, capability
   interfaces, capability profile rows, and derivation profiles are unique.
2. Artifact coverage, entry selection, digest relation, and path rules satisfy
   section 3.
3. `state.migratable_from_min <= state.schema <= state.migratable_from_max`.
4. `lifecycle.uninstall.default_data_disposition` is a member of
   `allowed_data_dispositions`.
5. Every entrypoint and platform profile is present in `runtime.profiles`. Every
   capability has exactly one row for every runtime profile and no other row.
6. Browser profiles have a browser/`wasm32` platform and exactly one matching trusted
   derivation. Its `derived_from_sha256` equals the canonical component digest.
7. Non-browser execution uses `artifact_role=canonical-component`; browser execution
   uses `artifact_role=browser-derived`.
8. `app.kind=ui` has at least one Launcher entrypoint and no service entrypoint;
   `service` has at least one service entrypoint and no Launcher entrypoint; `hybrid`
   has both. A settings entrypoint is optional for all three.
9. The component's exported descriptor exactly matches manifest app identity,
   version, kind, entrypoint IDs/kinds, labels, and initial routes.
10. Capability scopes do not exceed resource requests; requested resources do not
    exceed schema maxima or host policy.
11. A revoked package, unknown contract/world, unsupported platform/profile, or
    malformed descriptor is rejected before instantiation.

The only app-importable host interfaces are the schema allowlist: `clock`,
`scheduler`, `notification`, `kv`, `log`, `host-info`, `settings`, `system-metrics`,
and `http` at `0.0.1`. `common`, `ui`, `guest`, and daemon-only `control` are not app
capabilities.

## 5. Profiles, variants, and platform eligibility

| Profile | Artifact | Authority and degradation | Background claim |
| --- | --- | --- | --- |
| `desktop` | canonical component | daemon-brokered native host abilities | daemon-owned where declared |
| `headless` | canonical component | surface-neutral daemon/process host; Raspberry Pi/Ubuntu ARM64 is an explicit eligibility tuple | daemon/process-owned where declared |
| `web-preview` | trusted browser derivation | sample/mock/denied capabilities; no real hardware or network side effects | `foreground-only` |
| `web-runtime` | trusted browser derivation | real foreground UI and only explicitly browser-brokered abilities | `foreground-only` |

`web-preview` is demonstrative. Scheduler, notification, system metrics, and HTTP are
`mock`, `denied`, or `unavailable`; they cannot be `native` or live-brokered. Mock
values carry the WIT `simulated` marker where defined. Preview state is isolated from
installed app state.

`web-runtime` may execute supported foreground behavior while the page is alive. A
service entrypoint does not turn a page into a reliable service, and neither browser
profile may claim closed-page alarms, reporting, health checks, or timers. Any future
push/server scheduler is a different product contract, not this profile.

Platform tuples are eligibility claims, not native ABI variants. Current fixtures
exercise macOS ARM64/x86-64, Windows x86-64, Linux x86-64/ARM64, browser/wasm32, and a
headless Linux ARM64 control path. Unsupported tuples fail closed. A headless host may
install, inspect, update, enable, disable, configure, and uninstall a UI package, but
launch returns `unsupported-surface` without changing app state.

## 6. WIT package, worlds, and import reconciliation

The exact WIT file defines:

- shared app/profile/lifecycle IDs, call context, cancellation correlation, and closed
  error vocabulary, including canonical whole-second UTC instants and string generation
  IDs matching the daemon's durable identifiers;
- semantic UI and typed host-rendered settings;
- clock, scheduler, notification, KV, log, host-info, system-metrics, and future HTTP
  capabilities;
- guest lifecycle, service events, health, and migration exports;
- a surface-neutral daemon `control` envelope;
- `ui-only-reference`, `service-only-reference`, `hybrid-reference`,
  `web-preview-reference`, and `daemon-control-reference` worlds.

For an app component, these three sets must be exactly equal after canonical WIT name
normalization:

```text
actual component imports
= manifest.runtime.required_imports
= manifest.capabilities[].interface
```

The selected world must also have exactly that import set. An extra actual import, a
declared import absent from the component, an unknown interface, wrong version, or
wrong-direction `guest`/`control` import is rejected before instantiation. Local
component-model type checking is the final authority; Registry negotiation is only a
prefilter.

Every listed import is structurally required because Component Model linking is
static. `necessity=required` means a denied or unavailable capability blocks that
profile's activation. `necessity=degradable` means the host must still link the exact
typed interface but may implement it as a denied/unavailable stub; calls return the
typed error and the app must remain healthy without the feature. Truly absent optional
imports require a separately described artifact variant; a host never guesses or
silently removes an import.

`grant=user` requires recorded consent before live authority. `grant=automatic` is
still bounded by host policy. An update requires new consent before activation if it
adds an interface, widens a scope/quota, changes a capability from unavailable/mock to
live authority, or changes publisher identity. The old generation remains active if
consent is absent.

## 7. App identity, Launcher surfaces, and restoration

The Launcher is the desktop shell for installed VibApps. Store, search, and AI chat
are Launcher surfaces, not the entire GUI. The guest descriptor and manifest identify
launcher UI, service, and settings entrypoints without naming Tauri, WebView, DOM,
React, native windows, or another framework.

The host creates every `session-id`, `surface-id`, `route-id`, and restore token. The
guest receives typed `launch`, `open`, `focus`, `close`, `restore`, and `action` events
and may update only the IDs carried by that call. A forged, stale, cross-app, or
cross-session identifier returns `forged-identifier`; no surface is changed.

Routes are limited to the manifest allowlist. `restore=none` restores nothing;
`route-only` restores only a validated route; `safe-fields` may additionally restore
host-validated non-sensitive typed fields. Raw guest memory, handles, callbacks,
secrets, file descriptors, and arbitrary serialized framework state are never
restored. The host may discard any restore token after update, logout, policy change,
or validation failure and perform a clean launch.

A UI close destroys only that surface. It does not disable a hybrid app, stop its
service, cancel host schedules, revoke grants, delete settings/state, or uninstall the
package.

Guest UI is a flat, bounded semantic node list. The host owns tree validation,
layout, theme, localization, accessibility, focus, keyboard behavior, escaping, and
trusted Launcher chrome. Node IDs must be unique; the root must exist; parent links
must form one acyclic tree; action/field IDs must be declared in the current view;
types must match; and all counts, depth, string, and payload limits are enforced before
render. Raw HTML, script, CSS, URLs, DOM objects, native pointers, and executable
markup are rejected as `malformed-output`.

A `sensitive=true` field or setting never carries `field-value.text`. The host captures
the secret through trusted input, stores it in the host secret store, and gives the
guest only `field-value.secret-handle`. Plaintext sensitive values in guest output,
settings snapshots, daemon responses, CLI argv/environment/JSON, or logs are rejected
and redacted; a handle is app/user/purpose scoped and is not itself secret material.

## 8. Surface-neutral daemon control and lifecycle

The `daemon-control-reference` world exports one `daemon-request`/
`daemon-response` contract. Desktop Launcher, CLI, and a future web adapter serialize
that same envelope. `client-kind` is audit metadata, never authorization. The daemon
binds an authenticated principal and allowed app scope outside the payload, validates
the envelope `subject`, applies the same quotas and state machine, and deduplicates
mutations by `idempotency-key`.

The envelope covers search, install, uninstall with explicit data disposition, app
enable/disable, settings read/configure, permission query/plan/commit/revoke, app
status, bounded logs, update, manual service start/stop, launch, and the distinct
`alarm-configure`, `alarm-enable`, `alarm-disable`, `alarm-cancel`, `alarm-snooze`, and
`alarm-status` operations. Search and install have no installed-app subject; every
other operation requires one. The outer `idempotency-key` is the Gate 4
`client_command_id`. CLI JSON is a deterministic serialization of the same typed
response, including the same error code; it is not a second business-logic API.

The Stage 0 normalized JSON projection is exact: a WIT kebab-case field becomes a
snake_case JSON key; a record becomes an object; an enum becomes its kebab-case string;
an option becomes `null` or its value; a variant becomes
`{"tag":"<case>","value":<payload-or-null>}`; and `u32`/`u64` become JSON integers
within their exact unsigned range. Unknown keys/cases, lossy numbers, alternate
variant encodings, and prose wrappers are rejected. Both GUI and CLI normalization
tests use this mapping for `daemon-request.command` and `daemon-response.outcome`.

Settings `configure` and `alarm-configure` are different closed commands. Settings
configure carries only a settings revision and typed values. Alarm configure carries
an alarm ID, optional expected alarm revision, complete typed civil schedule, and the
complete notification projection. When the supplied alarm ID does not yet exist in
host state, the command creates an enabled revision 1; an existing ID is an atomic
edit. Alarm enable/disable/cancel carry the alarm ID and
optional expected revision; cancel is an exact typed alias of disable. Snooze carries
the parent occurrence ID, OS action ID, canonical action-received UTC instant, and a
duration of 60 through 3,600 seconds.

`alarm-status` is a bounded read-only query for one alarm. Its typed result contains
the full definition and projection, enabled flag, revision, next occurrence ID,
occurrence state/classification, notification/guest/action effect ledgers, closed
notification-permission health, optional active generation, and rollback state. It
does not render or launch, acquire a timer, activate a generation, or mutate/deduplicate
a command. `occurrence-limit` and `effect-limit` must both be greater than zero and at
or below the host ceiling, otherwise the query returns `invalid-argument` or
`resource-limit`. Rows are newest-first with a deterministic ID tie-break;
`occurrences-truncated` and `effects-truncated` distinguish a complete result from a
bounded prefix. Permission health is exactly `authorized`, `denied`, or
`not-determined`; rollback is exactly `none`, `observation-window`, or `rolling-back`.

Permission control is two-phase and daemon-authoritative:

1. `permissions` returns the current bounded grant snapshot and `grants-revision`.
2. `permission-plan` resolves the installed/candidate manifest and profile itself and
   returns an authoritative grant/revoke/scope diff, the base grants revision, expiry,
   and an optional opaque host-confirmation token. Client-supplied interface names are
   requests, never an authority or scope source.
3. A trusted interactive host confirmation may mint the single-use token bound to
   principal, app, candidate digest, plan, exact diff, revision, and expiry. Merely
   reading a plan does not mint authority.
4. `permission-commit` applies only grant/widen changes; `permission-revoke` applies
   only revoke/narrow changes. Both recheck subject, plan, token, current manifest,
   grants revision, policy, and profile in one transaction and return the new snapshot
   plus applied diff.

A missing confirmation returns `consent-required`; a stale revision returns
`stale-revision`; a forged/cross-app/expired/replayed plan or token returns
`forged-identifier` or `consent-required` without mutation. A noninteractive CLI has
no confirmation bypass in Stage 0 and therefore cannot commit or revoke when the host
did not provide a valid confirmation token. Preview workers cannot call these control
operations.

The host is authoritative for lifecycle transitions and sends guest management events
as bounded notifications; a guest cannot veto or forge them:

- install validates and stages an immutable package but does not imply consent or
  enablement;
- enable permits declared entrypoints/effects after grants and health checks;
- settings configure asks the guest to validate typed proposed settings, then the host persists
  the accepted snapshot and emits `configured`; rejected settings do not partially
  commit;
- disable is idempotent, retains package/settings/state, stops service instances,
  disables host schedules while retaining their definitions/projections, revokes all
  new effects, and emits the disable event;
- uninstall first quiesces services/schedules/effects, then applies exactly one allowed
  `delete`, `retain`, or `export-then-delete` disposition. Retained state is sealed from
  guests and can be restored only to the same publisher/app/user after consent;
- security/audit ledgers may remain under host retention policy regardless of app-data
  disposition.

For a hybrid app, service lifecycle is independent from UI surface lifecycle. Service
triggers are host-issued, instance- and entrypoint-scoped, and bounded. Health checks
never grant capability authority. A disabled or uninstalled app receives
`app-disabled`/`app-uninstalled` on attempted effects.

## 9. Scheduler, notification projection, and occurrence channel

The daemon is the only durable scheduler. The guest never waits in a timer loop.
`schedule-request` has a typed purpose:

- `alarm` requires `notification=some(validated projection)`;
- `service-trigger` requires `notification=none` and can never create a notification
  outbox merely because it is scheduled.

The daemon validates and persists an alarm's complete definition and notification
projection in the same upsert transaction. The projection has exactly bounded `title`,
`body`, and `standard-sound`; urgency and action-route are not part of Stage 0. The
schedule record round-trips its revision, next occurrence ID, and persisted optional
projection. Scheduled delivery never depends on a later guest notification request.
`notification.show` remains available for explicit immediate foreground notifications
only.

Every durable wall-clock instant in clock, scheduler, metric, alarm control, status,
and permission-plan expiry uses `utc-instant`, whose sole accepted representation is
the 20-character canonical `YYYY-MM-DDTHH:MM:SSZ`: UTC `Z`, whole seconds, valid
Gregorian calendar/time values, and seconds `00..59`. Offsets, fractions, whitespace,
lowercase `z`, leap-second `:60`, and invalid calendar values return
`invalid-argument`. Monotonic deadlines remain unsigned milliseconds and are never
serialized as wall time. `generation-id` is a host-issued string such as `gen-1`.

Both one-shot local and recurring local schedules carry DST gap and overlap policies.
Recurring schedules explicitly carry `kind=daily|weekly` and an inclusive
`start-date`. Daily requires `weekdays=[]`; weekly requires a non-empty unique weekday
list. The first recurring candidate is on or after the inclusive start date but must
resolve strictly after the configure/edit/enable transaction commit. Intervals other
than one, end dates, exceptions, cron syntax, and approximate substitutions are
rejected.

An `alarm` purpose accepts only `at-local` or `recurring-local`; `at-instant` is
available only to non-alarm service triggers. The control `alarm-schedule` likewise
contains only civil one-shot and recurring variants. This prevents a timestamp-only
shortcut from bypassing the fixed time-zone, recurrence, and DST contract.

For Stage 0 alarms the only accepted combination is `gap=next-valid`,
`overlap=earlier`, `missed=fire-once`: nonexistent civil times shift forward by the
transition gap, ambiguous times select the earlier UTC occurrence, and catch-up follows
the separate alarm acceptance contract. Other WIT enum combinations may be used only
by a future non-alarm profile with its own conformance vectors; the Stage 0 alarm host
rejects them as `invalid-argument`.

When an alarm occurrence becomes due, one durable host transaction:

1. claims the unique occurrence ID and rechecks enabled revision and permission;
2. records exactly one closed delivery classification: `on-time`, `catch-up`,
   `missed`, or `permission-denied`;
3. if eligible, creates the notification outbox row from the already persisted
   projection before any guest call;
4. creates a separate durable guest occurrence event carrying that typed host
   classification, canonical scheduled/dispatched UTC instants, and occurrence ID;
5. advances/materializes recurrence as applicable.

Notification delivery, the guest occurrence event, and guest acknowledgement are
separate idempotent channels. A guest trap, timeout, cancellation, malformed result,
or generation disposal cannot suppress, retract, or duplicate a committed host
outbox. `acknowledge-occurrence` deduplicates by occurrence and idempotency key. A
pending occurrence keeps the same ID and due instant across generation switches and
is dispatched to the active generation; terminal ledgers are never guest migration
payloads. Opaque guest payload bytes cannot replace or override the classification.

`scheduler.disable` is the guest operation formerly described as cancel: it increments
an enabled schedule revision, cancels pending regular/snooze occurrences, and retains
the validated definition/projection. Repeating it on a disabled schedule is a no-op.
The daemon control layer exposes the typed `alarm-cancel` command as an exact alias of
`alarm-disable`; both return the same `alarm-command-result` and normalized durable
transition.

`scheduler.enable` gives a disabled recurring schedule a new revision and selects the
earliest civil candidate strictly after commit. It enables a one-shot only when its
resolved instant is strictly after commit. An expired one-shot returns
`conflict` with detail `expired-one-shot`, remains disabled, keeps its revision, and
materializes nothing. Re-enabling an already enabled schedule is a no-op and never
resurrects an old occurrence.

During the post-activation observation/rollback window, guest upsert/enable/disable
and daemon alarm-configure/alarm-enable/alarm-disable/alarm-cancel operations return
`upgrade-in-progress`. Host-owned notification actions such as snooze remain allowed
and durable. Rollback changes generation routing and app KV revision only; it never
rolls back occurrence, notification, acknowledgement, action, or snooze facts.

## 10. Capabilities and services

Default guest authority is empty. Each host call revalidates app/user/generation,
enabled state, current grant, declared scope, profile availability, quota, deadline,
and idempotency key. Linking an interface is not authorization.

`system-metrics` returns typed samples from a host broker and exposes no hardware
handle. The headless temperature fixture requests only CPU temperature. In
`web-preview` it receives a labeled simulated sample.

`http` exposes no socket or DNS API and carries origin, method, request/response bounds,
timeout, and a required idempotency key. A future live broker must deduplicate a replay
of the same normalized request/key within its declared ledger window and must not
silently repeat an uncertain non-idempotent remote effect. Stage 0 deliberately
provides no live broker: desktop/headless calls return `capability-unavailable` through
the degradable typed stub, and preview calls return captured mock data with zero
network requests.

KV is app-scoped and transactional. Logs are structured, sanitized, size/rate limited,
idempotent by their required key, and available through bounded status/log control
queries. Replaying the same log key/payload creates one record; key reuse with another
payload is `conflict`. Clock and host-info reveal
only the typed values in WIT. No capability conveys ambient filesystem, environment,
process, credential, raw network, or device authority.

## 11. Idempotency, cancellation, errors, traps, and bounds

Every guest event has an `event-id`, `idempotency-key`, generation, deadline, profile,
and cancellation ID. Every mutating host call has an idempotency key. The daemon
deduplication namespace includes authenticated user, app, operation, and key; reusing a
key with a different normalized payload returns `conflict`.

Guest KV mutations and guest-requested effects become visible only through a validated
host commit after the call returns. If the deadline or cancellation wins before that
commit, the result is `deadline-exceeded` or `cancelled`; partial guest results and
uncommitted effects are discarded. Host-owned work already committed independently,
including a due notification outbox, is not rolled back by guest cancellation.

All guest inputs and outputs are bounded by both the manifest request and host ceiling.
Current schema ceilings are 64 MiB linear memory, 250 ms event wall time, 2 s
health/migration time, 256 KiB output, 10 MiB stored data, 128 durable schedules, and
1 MiB logs/day. The implementation must also bound tables, instances, stack, host-call
rate, list/string/node counts, and asynchronous operations. An output over the app's
declared `output_bytes` is rejected whole; no partial UI is rendered.

Typed error codes are closed in WIT. Admission errors happen before instantiation;
permission/consent blocks preserve the previous active generation; call errors have no
partial commit. An unknown code or malformed result is itself `malformed-output`.

Every generation has a disposable Store, capability context, cancellation scope, and
resource limits. A trap, timeout, resource violation, cancellation race, or malformed
output disposes that Store; it is never reused. Host-owned state/outboxes remain
consistent, and a later event uses a fresh or already healthy generation according to
host policy.

## 12. State, migration, activation, and rollback

Durable state is namespaced by user, publisher, app, and schema revision. The host
owns settings, KV revisions, schedules/projections, occurrence/outbox/action ledgers,
permissions, lifecycle state, and generation routing. Raw guest memory is disposable.

Activation is transactional:

1. resolve an exact digest and stream into bounded staging;
2. validate package tree, artifact bytes, canonical digest, schema, semantic invariants,
   provenance/SBOM/revocation policy, imports, profile/platform, grants, and resources;
3. compile canonical component bytes locally and instantiate a shadow generation with
   no live-effect authority;
4. create a target state revision and run bounded typed migration when required;
5. validate descriptor, settings schema, health report, and initial view/output;
6. atomically switch event/service routing and active state revision;
7. drain old calls to a deadline, cancel the rest, and drop the old Store;
8. observe a bounded rollback window, reverting generation route and app KV revision
   on failure without reverting host-owned schedule/effect ledgers.

Migration is allowed only when the installed schema falls in the declared range and
the exact typed migration succeeds within limits. Failure leaves the old package and
state active. Install, consent, enable, activation, disable, and uninstall are separate
audited transitions.

## 13. Browser derivation

The canonical registry input remains `component.wasm`. A trusted builder may derive
Jco ES modules/core Wasm for one declared browser profile. Every derived file and the
derivation attestation is content-addressed; `derived_from_sha256` must equal the
canonical component digest, the profile must match, and admission must verify the
attestation under the configured trusted-builder policy before loading code.

Derived code runs in a dedicated Worker without DOM objects and communicates through a
narrow broker. The page owns rendering and applies CSP/origin isolation. A digest or
profile mismatch is `integrity-failure`; the host never falls back to unverified
browser code.

## 14. Evolution and rejected changes

Published experimental WIT and fixture files are immutable snapshots. Any edit during
this pre-code gate replaces the experiment before bindings exist. After a snapshot is
used to generate bindings, breaking type or semantic changes require a new package
version and new fixtures; hosts do not infer compatibility.

Rejected as breaking without a new version:

- removing or renaming an interface, function, record field, variant/enum case, world,
  entrypoint kind, error meaning, or lifecycle transition;
- changing a scalar width, option/result shape, ownership, unit, default, bound,
  idempotency scope, DST rule, notification transaction boundary, or data disposition;
- adding a required field/case that old peers cannot produce or safely reject;
- changing a degradable capability into activation-required authority;
- reinterpreting `close` as disable, browser foreground as background reliability, or
  guest acknowledgement as notification delivery;
- accepting a new package/contract version through silent fallback.

Safe candidates still require conformance proof: a new separate optional interface, a
new world/artifact variant, a higher host ceiling that does not change package requests,
or a new error/detail carried by a newly versioned interface. An unsupported exact
contract or package format returns `unsupported-version` before instantiation.

## 15. Conformance gate

The hand-authored vector set is the executable acceptance specification. It includes
positive UI-only, service-only, hybrid, desktop, headless Linux ARM64, web preview,
web runtime, shared Launcher/CLI control, restoration, lifecycle/data disposition,
degraded typed stubs, canonical GUI/CLI alarm configure/status JSON, permission
query/plan/commit/revoke, standard-sound projection, whole-second UTC, daily/weekly
inclusive recurrence, host delivery classification, schedule/notification separation,
generation switch, idempotent logs, and rollback cases.

It also includes negative missing/unknown/wrong-direction imports, both reconciliation
directions, required-permission denial, malformed/oversized output, unsupported
version, manifest/runtime resource limits, capability expansion without consent,
forged lifecycle/control IDs, disabled effects, invalid uninstall disposition,
app-kind violations, live preview authority, browser digest mismatch, cancellation,
expired one-shot, invalid recurrence/UTC/status bounds, missing/stale/forged permission
confirmation, sensitive-setting plaintext, and rollback-window guest mutation.

Schema validation alone does not execute mutations in vectors. A conformance runner
must load the referenced valid manifest, apply the named `given` mutation in isolation,
run the RFC semantic phase, and deep-compare the complete `expected` object. A runner
must reject duplicate JSON keys and duplicate vector IDs.

Gate 2 passes at the contract-artifact level only when WIT parsing, strict schema
compilation, all valid-manifest validation, semantic invariant checks, and fixture
inventory checks succeed. This is not runtime implementation evidence and does not
authorize generation of Rust bindings until the architecture council accepts the
gate.

## 16. Authoring-skill consumption

The future project-local `.agents/skills/vibapp` skill must consume the WIT, JSON
Schema, and conformance vectors directly as reference and validation inputs. It must
generate against an exact world/profile, declare every import and scope, choose an app
kind/entrypoint set, emit all artifact relationships, and run the same validations.
It must not copy a simplified type list from this prose or claim compatibility from a
successful Rust compile alone.
