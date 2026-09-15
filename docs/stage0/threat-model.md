# Stage 0 Threat Model and Minimum Isolation Profiles

Status: **Gate 3 contract candidate — normative for `experimental-v0`**

Scope: unpublished local Stage 0 only

Last reviewed: 2026-08-11
Failure action: do not add privileged host imports or execute generated packages when any required control lacks an executable oracle.

`MUST`, `MUST NOT`, `SHOULD`, and `MAY` are normative. Stable IDs in this document are inputs to conformance tests and the future `.agents/skills/vibapp` routing policy. Renaming an ID requires updating every fixture and gate report that references it.

## 1. Decision

VibApp treats all package code, generated Rust build inputs, package assets, declarative views, settings, self-reported health, and logs as untrusted. There are three package profiles:

- `ui`: event-driven component with a host-rendered view;
- `service`: headless component activated by host-owned schedules/events while enabled;
- `hybrid`: both surfaces, with one package identity and separate view/service generations if either can outlive the other.

The **daemon is authoritative** for install state, enable/disable state, settings revisions, capability grants, scheduler leases, health state, update generation, storage namespaces, and uninstall. The Launcher is the trusted user-facing broker for these daemon operations; it does not become the authority and it never executes package code. Closing a UI surface does not disable a service. Disabling or uninstalling a package revokes every generation and durable lease regardless of whether the Launcher is open.

Vibapp.ai MAY provide an isolated foreground preview. Preview is not installation, activation, permission grant, background execution, or evidence of local compatibility.

## 2. Security objectives and protected assets

| ID | Objective / asset |
| --- | --- |
| `OBJ-01` | Keep user files, home directory, environment, credentials, browser cookies, provider tokens, cloud metadata and Docker control sockets unavailable to packages and builds. |
| `OBJ-02` | Bind every host call, view, event, setting, log, state record and effect to the authenticated user, package, artifact digest and generation selected by trusted routing state. |
| `OBJ-03` | Bound CPU, memory, tables, instances, host-call rate, output, assets, logs, queues, persistent storage, build PIDs, disk and wall time. |
| `OBJ-04` | Make install, enable, UI open/close, service start/stop, disable, permission change, update, rollback and uninstall separate auditable transitions. |
| `OBJ-05` | Prevent one app from spoofing Launcher chrome, receiving another app's input, state, settings, events, permissions, status, logs or background leases. |
| `OBJ-06` | Prevent preview code from reaching local hardware/background capabilities or being confused with a signed, installed package. |
| `OBJ-07` | Keep builder output quarantined until independent validation; never trust generated tests or agent assertions as verification. |
| `OBJ-08` | Preserve an evidence trail without recording secrets, raw credentials, unbounded attacker strings or terminal/HTML control sequences. |

Stage 0 does not claim resistance to a malicious host administrator, kernel/hypervisor compromise, a Wasmtime zero-day, physical access, or a compromised release-signing root. Those risks require defense in depth and later operational controls; they are not permission to weaken the profiles below.

## 3. Trust boundaries

```text
user
  |
  +-- Launcher trusted chrome and typed actions ---- vibapp.ai trusted parent UI
  +-- local CLI over owner-authenticated UDS/named protocol
  |                         |                         |
  | authenticated IPC      | MessagePort envelope    | install intent only
  v                         v                         v
daemon authority       isolated preview broker   daemon refetch + verify
  |                         |
  | generation-bound        | no cookies / no network / mock capabilities
  v                         v
runtime service          dedicated Worker on preview origin
  |
  +-- Store(app A, digest, generation, grants, namespace)
  +-- Store(app B, digest, generation, grants, namespace)
  +-- host-owned scheduler / notifications / KV / logs

untrusted source + dependencies
  |
  v
disposable non-root builder -> quarantine -> independent verifier -> candidate
```

Boundary rules:

1. `CTRL-ID-01`: Every runtime/preview message envelope MUST contain trusted `user_id`, `app_id`, `artifact_digest`, `generation_id`, `session_id`, `view_revision` and unique `event_id` as applicable. Values are injected or checked by the broker; an app cannot select its own authority tuple.
2. `CTRL-ID-02`: Storage and settings namespaces MUST be derived from the authenticated authority tuple. Raw paths, arbitrary namespace IDs and another user's/app's identifiers MUST NOT cross the component ABI.
3. `CTRL-ID-03`: A capability context is created for one generation from persisted grants and inspected imports. It MUST NOT be cached in a reusable GUI slot or inherited during app switching, preview, update or rollback.
4. `CTRL-ID-04`: Stale generation, stale view revision, wrong-session, cross-user and cross-app messages MUST fail closed before application logic or host effects run.

## 4. Minimum isolation profiles

### 4.1 `ISO-RUNTIME-01` — untrusted Component generation

- The runtime service runs separately from Launcher/web processes with a low-privilege identity. It MUST NOT receive GUI cookies, browser storage, SSH/config directories, provider auth, cloud credentials or Docker access.
- Each app generation gets a new Wasmtime `Store`, limiter, fuel/epoch state, cancellation token, call budget and authority context. A trapped, timed-out or cancelled Store is dropped and never pooled for another app.
- No ambient WASI filesystem, environment, sockets, network, process spawn, home directory, terminal, raw clock device, hardware enumeration or OS handle is linked.
- Imports MUST equal the manifest's declared required interfaces. Unknown required imports, undeclared imports and unsupported versions are rejected before instantiation. A denied grant never silently disappears into a weaker behavior unless the interface declared it optional.
- All guest strings, lists, records, variants, asset references, UI trees, log fields and effect batches are validated after Canonical ABI decoding. WIT shape checking is not a semantic validator.

Stage 0 numeric budgets are deliberately conservative and testable:

| Resource | UI/service event | Migration/health exception |
| --- | ---: | ---: |
| Component bytes | 16 MiB per artifact | same |
| Assets | 16 MiB total; 4 MiB each | same |
| Linear memory | 64 MiB per Store | 64 MiB |
| Table elements | 10,000 | 10,000 |
| Instances / memories / tables | 32 / 4 / 8 | same |
| Fuel | 10,000,000 per event | 50,000,000 per migration |
| Outer wall deadline | 2 seconds | 5 seconds migration; 2 seconds health |
| One async host call | 500 ms unless the interface defines a smaller limit | 1 second |
| Concurrent host calls | 8 | 8 |
| Calls | 100/s, burst 200; failures count | same |
| Returned UI | 256 KiB encoded, 2,048 nodes, depth 32 | first view uses same limit |
| String / list | 16 KiB UTF-8 string; 4,096 scalar/list entries | same |
| Event queue | 256; explicit overflow error | migration receives no live events |
| Logs | 64 KiB/minute and 1 MiB/day per app | same |
| App-scoped KV | 16 MiB for Stage 0 | migration copy counts against quota |

- Fuel/epoch interruption MUST be combined with an outer timeout. Blocked async host calls MUST accept cancellation and release their lease by the outer deadline.
- Successful and rejected host calls consume rate budget. Effects use host transaction/event IDs and are idempotent. A trap between intent and acknowledgement MUST not duplicate a notification, schedule, setting mutation or KV commit.
- Raw HTML, script, SVG script/event attributes, terminal escape sequences, external URLs and active document formats are not valid declarative UI. Assets are content-addressed, MIME-sniffed, decoded with size/dimension limits and rendered through trusted host primitives.

### 4.2 `ISO-LAUNCHER-01` — GUI launcher and multi-app switching

The Launcher manages install/uninstall, enable/disable, settings, permissions, status, logs, update and app switching through typed daemon commands.

- Launcher chrome is host-owned, visually persistent and outside the app's clipped content surface. It displays the daemon-resolved app name, publisher/trust state, running/disabled state and active permission indicator. App output cannot draw over or rename this chrome.
- A view handle is bound to `(user, app, digest, generation, session, view_revision)`. Every pointer, keyboard, IME, accessibility and command event is checked against the currently focused handle.
- Switching apps revokes pointer capture, drag state, modal ownership, focus, pending paste/file chooser tokens and ephemeral capability handles from the old view before the new view is activated.
- Hidden/background UI surfaces receive no keyboard, clipboard, accessibility action or launcher-command events. Headless service events travel through a different queue and do not acquire UI focus.
- App-returned labels such as “System”, “Verified”, “Permission granted”, health badges or update prompts are rendered inside clearly marked app content and cannot use reserved host chrome roles/icons.
- Notification chrome and log/status headings are added by the host and include the authoritative app identity. Guest text is content, not source attribution.
- Accessibility trees are rooted per active view; node IDs are not global. Cross-view relationships and focus targets are rejected.

### 4.3 `ISO-SERVICE-01` — persistent headless/hybrid service

- `enabled` is the sole daemon-authoritative condition permitting new background dispatch. UI close/hide MUST NOT change it. Disable MUST atomically block dispatch, cancel in-flight calls, revoke scheduler/notification/network leases and drop all service Stores.
- Uninstall performs disable first, then revokes grants and leases, removes executable artifacts, and follows an explicit user choice for app data retention/deletion. An app cannot intercept or veto disable/uninstall.
- Each scheduler lease is keyed by `(user, app, generation, schedule_id)` and has an owner heartbeat/expiry. Activation, rollback, disable, uninstall and crash-loop quarantine reconcile and remove orphan leases.
- Crash-loop policy: 3 abnormal terminations/timeouts in 5 minutes disables automatic restart, marks `quarantined`, and requires a host-owned user action or verified update. Restart backoff cannot exceed the package's enabled-state decision.
- Settings are schema-validated, revisioned and size-limited. Secret values are stored in a host secret store and exposed, if ever authorized later, only through opaque handles and narrowly typed operations. Settings MUST NOT interpolate into commands, URLs, paths, SQL, HTML, logs or WIT names.
- The daemon computes `running`, `degraded`, `crash-loop`, `disabled`, `quarantined` and `update-pending`. Guest self-check results are labeled `app-reported` and cannot overwrite daemon health or fabricate test/verification status.
- Logs are structured, sanitized, bounded, source-labeled and generation-bound. Guests cannot backdate, choose severity above the host policy, inject new records through newline/control bytes, or write verification/audit streams.
- Host-info exposes only compatibility facts required by the ABI: coarse OS family, architecture, locale/time-zone capability and supported interface versions. Raw serials, MAC/IP addresses, CPU/GPU model, device name, battery history, memory totals, process list and high-resolution performance counters are denied. Repeated hardware-metric calls count against quotas to prevent fingerprinting.
- Stage 0 provides no **live** component HTTP authority. The experimental contract may link the exact HTTP interface as a degradable typed stub so an app can observe `capability-unavailable`; Web preview may return a labeled captured mock, but neither path performs network traffic. A future live HTTP broker requires a new threat review and MUST bind grants to scheme `https`, normalized host, exact/approved port, path/method class, DNS/IP re-check, redirect re-authorization, private/link-local/metadata IP denial, request/response byte limits, rate limits, replay-safe idempotency and visible periodic-use status. Periodic background egress is a separately disclosed permission, not implied by foreground HTTP.

### 4.4 `ISO-PREVIEW-01` — vibapp.ai isolated foreground preview

- Preview execution uses a dedicated Worker on a separate, cookie-less preview origin (candidate: `https://preview.vibapp.ai`) and host-rendered declarative UI. Package code receives no DOM, parent window, service worker, browser storage, credentials, local network, WebUSB, WebBluetooth, WebSerial, MIDI, clipboard, camera, microphone, geolocation, notifications or background APIs.
- Minimum CSP for the preview origin is `default-src 'none'; script-src 'self'; worker-src 'self'; connect-src 'none'; img-src 'self' data:; style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors https://vibapp.ai`. Any relaxation is a Gate 3 change.
- Parent/Worker communication uses one transferred `MessagePort`, a fresh nonce and the full `CTRL-ID-01` envelope. Global `postMessage` data, wrong origins, wrong sessions and cross-preview messages are ignored and audited.
- Preview state is scoped to `(authenticated user, artifact digest, preview session)`, encrypted by the service if persisted, and deleted at session expiry. It is never copied into installation state without an explicit import flow and schema validation.
- Clock/scheduler/notification/KV/hardware capabilities are mocks unless the UI explicitly states otherwise. Every simulated result carries `mode = preview-simulated`; package output cannot remove the persistent “Preview — no local/background access” banner.
- Preview cannot call install/enable/permission/update operations. A trusted parent action creates an install intent containing only an immutable digest and nonce; the local Launcher refetches metadata, verifies it and requests permissions again. Preview success is not a signature, scan, compatibility or reliability result.
- Multiple simultaneous previews use separate Worker, port, quota and state namespaces. Cross-user cache keys include user and digest; public artifact bytes may be shared only when content-addressed, while all mutable state remains isolated.

### 4.5 `ISO-CLI-01` — future local/headless CLI principal

The lower-priority CLI is a separate untrusted local client of the daemon, intended eventually for Ubuntu, Raspberry Pi and other headless Linux systems. It is not a direct runtime/component interface.

- Unix uses a per-user UDS in a private runtime directory owned by that user with mode `0600` (and a private parent directory). The daemon validates peer credentials/UID; file permissions alone are insufficient. Windows later uses an equivalently owner-bound named protocol. Stage 0 exposes no unauthenticated TCP listener.
- Every connection negotiates a protocol/schema version and obtains a bounded session identity. Commands use the same daemon-authoritative app/digest/generation/settings revision and lifecycle checks as Launcher; user-supplied IDs never select storage/capability namespaces directly.
- Human output escapes terminal/OSC/ANSI, bidi and control characters in all app names, logs, settings and errors. `--json` emits no ANSI or prose, uses a stable top-level `schema_version`, bounded arrays/strings and enumerated error/event objects; an untrusted string cannot become a JSON field or record boundary.
- The CLI cannot imitate a GUI permission dialog. Permission changes, enable, uninstall, destructive setting changes and secret operations are explicit subcommands with an authoritative diff. Interactive mode requires a distinct confirmation; non-interactive mode fails unless a separately designed preauthorization policy exists. Stage 0 has no such bypass token.
- Secret settings never appear in argv, environment, shell history, process listings, JSON output or logs. A future headless path accepts a secret through a protected file descriptor/stdin or OS secret store, immediately returns an opaque handle, and redacts subsequent reads. Ordinary settings remain typed, revisioned and schema-validated.
- Streaming status/log commands apply backpressure, line/byte limits and cancellation. Disconnect releases subscriptions and cannot stop/enable a service implicitly.

### 4.6 `ISO-BUILDER-01` — untrusted generated Rust build

This profile is a contract, not a builder implementation.

- The immutable bootstrap image and tool pins are in `config/contract-toolchain.toml`. Tag-only images are forbidden.
- Runtime identity is non-root UID/GID `65532:65532`; root filesystem read-only; `CAP_DROP=ALL`; `no-new-privileges`; default seccomp/LSM plus a future reviewed tighter profile; no privileged mode, devices, host PID/IPC/network namespace, cloud metadata route, Docker/container socket, host home, SSH agent, provider token or reusable credential.
- Writable mounts are only a fresh 4 GiB workspace, 1 GiB output quarantine and bounded tmpfs. Dependency/tool caches are content-addressed, verified and read-only during build. Symlinks and archive paths are resolved beneath their mount roots before use.
- Limits: 2 CPUs, 4 GiB RAM, no swap, 256 PIDs, 10 minute wall time, 4 GiB workspace, 1 GiB output, 1 GiB tmpfs. Cancellation terminates the process group and discards the worker.
- Fetch is a separate phase with only the allowlisted hosts/methods in `config/contract-toolchain.toml`, no source execution and no secrets. Agent, build and test phases have `network=none` and use `cargo ... --locked --offline`.
- `build.rs`, proc macros and native C/C++/assembly execute with build-process authority and are denied unless their exact crate/version/checksum is approved. Guest dependencies using native code are denied in Stage 0.
- Build/test exit codes, stdout, agent summaries and repository-supplied reports are untrusted evidence. The independent verifier re-hashes outputs, re-runs policy/format checks in a clean context and derives the candidate status itself.

### 4.7 `ISO-QUARANTINE-01` — output handoff

- Builder output lands in a non-executable quarantine mounted separately from Registry/runtime/install paths.
- The handoff manifest records source tree, `Cargo.lock`, toolchain config, builder image and output SHA-256. Any mismatch or post-build mutation invalidates the handoff.
- Registry/runtime never accept Wasmtime AOT/native artifacts, test result files or provenance claims as executable truth from the builder. Canonical Component bytes are validated/inspected afresh.
- Only a verifier-created candidate record can be offered to install. The agent and builder have no signing, publication, install or permission-grant authority.

## 5. Threat and control register

Every row has a planned adversarial oracle in [../../fixtures/adversarial/README.md](../../fixtures/adversarial/README.md). `Owner` names the future implementation/accountability role, not a currently running service.

| Threat ID | Attack / impact | Mandatory control IDs | Fixture IDs | Owner | Residual risk |
| --- | --- | --- | --- | --- | --- |
| `TH-C-001` | Infinite loop or recursion starves daemon | `ISO-RUNTIME-01`, fuel, epoch, outer timeout, disposable Store | `ADV-C-001`, `ADV-C-002` | runtime-owner | Engine/host scheduler bug |
| `TH-C-002` | Memory/table/instance growth exhausts process | Store limits and process memory ceiling | `ADV-C-003` | runtime-owner | Shared-process pressure before worker-per-app isolation |
| `TH-C-003` | Host-call flood or failure flood bypasses quota | Calls, failures and concurrency all consume budget | `ADV-C-004` | broker-owner | Legitimate burst tuning |
| `TH-C-004` | Oversized/deep UI, string or list exhausts host/rendering | Semantic validators and exact output limits | `ADV-C-005`, `ADV-C-006`, `ADV-C-007`, `ADV-C-008` | launcher-owner | Decoder implementation defects |
| `TH-C-005` | Malformed/undeclared imports gain authority or crash linker | Exact import/manifest reconciliation before instantiate | `ADV-C-009`, `ADV-C-010` | runtime-owner | Evolving Component parser bugs |
| `TH-C-006` | Trap/time-out during an effect duplicates or half-commits | Host transaction ID, idempotency, effect acknowledgement, Store disposal | `ADV-C-011`, `ADV-C-012` | daemon-owner | Incorrect effect-specific transaction design |
| `TH-C-007` | Blocked async host call survives guest deadline | Cancellable host calls, outer deadline, lease cleanup | `ADV-C-013` | broker-owner | OS APIs that do not support cancellation |
| `TH-C-008` | Malicious asset triggers parser, path, active-content or decompression issue | Content addressing, no guest paths, safe decoders, MIME/dimension/size caps | `ADV-C-014`, `ADV-C-015`, `ADV-C-016`, `ADV-C-017` | asset-owner | Third-party codec zero-days |
| `TH-C-009` | Guest log/notification injects terminal/HTML or impersonates another app | Sanitization, structured records, host-added source chrome | `ADV-C-018`, `ADV-L-007` | launcher-owner | Social engineering inside app content |
| `TH-C-010` | Guest submits native/AOT bytes to reach native execution | Canonical Component validation; AOT deserialize forbidden | `ADV-C-019` | verifier-owner | Wasmtime JIT zero-day |
| `TH-L-001` | Event/state from app A reaches app B during switching | `CTRL-ID-01`–`04`, per-view queues, trusted namespace derivation | `ADV-L-001`, `ADV-L-002` | launcher-owner | Broker implementation bug |
| `TH-L-002` | Stale generation/view event mutates current app | Generation, session and revision checks before effect | `ADV-L-003` | daemon-owner | Race in atomic route switch |
| `TH-L-003` | Permission/capability from previous app carries into next GUI slot | New generation capability context; revoke ephemeral handles on switch | `ADV-L-004` | broker-owner | OS chooser tokens with unclear lifetime |
| `TH-L-004` | App spoofs Launcher/system/verified/permission chrome | Host-owned clipped chrome and reserved semantic roles | `ADV-L-005` | launcher-owner | App can still phish within clearly marked content |
| `TH-L-005` | Hidden app captures focus, keyboard, IME, paste or accessibility actions | One active view handle; revoke focus/pointer/modal tokens | `ADV-L-006` | launcher-owner | Platform WebView focus bugs |
| `TH-S-001` | User closes UI believing headless service stopped | Distinct visible `UI closed` vs daemon `service enabled`; host status | `ADV-S-001` | product-owner | User misunderstanding despite labeling |
| `TH-S-002` | Disabled/uninstalled app keeps running, scheduling or notifying | Atomic revoke/cancel/drop/reconcile semantics | `ADV-S-002`, `ADV-S-003` | daemon-owner | Non-cancellable OS notification already delivered |
| `TH-S-003` | Crash loop causes resource/notification DoS | 3/5-minute quarantine policy and bounded restart | `ADV-S-004` | daemon-owner | Intentional low-rate failure below threshold |
| `TH-S-004` | Upgrade/rollback leaves orphan scheduler leases | Generation-owned leases and reconciliation on every transition | `ADV-S-005` | scheduler-owner | OS scheduler reconciliation failure |
| `TH-S-005` | Settings inject commands, URLs, paths, markup or secrets | Typed schema, no interpolation, opaque secret handles, revision checks | `ADV-S-006`, `ADV-S-007` | settings-owner | Unsafe future host binding |
| `TH-S-006` | App fabricates healthy/verified status or forges logs | Daemon-derived health; app-reported label; separate audit stream | `ADV-S-008`, `ADV-S-009` | daemon-owner | User may trust app text inside content |
| `TH-S-007` | Hardware metrics enable fingerprinting | Minimal host-info schema; no stable identifiers/high-resolution metrics | `ADV-S-010` | privacy-owner | Coarse OS/locale combination still adds entropy |
| `TH-S-008` | Periodic background HTTP quietly exfiltrates data | No Stage 0 live HTTP; typed unavailable/mock stubs only, then future exact destination + periodic-use grant, idempotency and rate budget | `ADV-S-011` | security-owner | Approved destination can itself be malicious |
| `TH-P-001` | Preview Worker reaches cookies, DOM, network, hardware or background APIs | `ISO-PREVIEW-01`, separate origin, Worker, CSP, no capabilities | `ADV-P-001`, `ADV-P-002` | web-security-owner | Browser zero-day |
| `TH-P-002` | Cross-preview or cross-user messages/state leak | Dedicated Worker/port/nonce and user+digest+session namespace | `ADV-P-003`, `ADV-P-004` | web-security-owner | Service-side cache-key bug |
| `TH-P-003` | Package spoofs capability result or removes preview/mock label | Host-owned persistent banner and typed `preview-simulated` result | `ADV-P-005` | preview-owner | User ignores warning |
| `TH-P-004` | Preview success is confused with install trust/compatibility | Separate trusted install action; refetch, digest/signature and permissions | `ADV-P-006` | launcher-owner | Phishing outside controlled UI |
| `TH-P-005` | Preview installs service worker or persists after session | No service-worker permission; isolated origin storage purge | `ADV-P-007` | web-security-owner | Browser persistence bugs |
| `TH-CLI-001` | Another local user/process controls daemon through socket/path replacement | Owner-private endpoint, peer-credential UID check, session/version handshake, no TCP | `ADV-CLI-001`, `ADV-CLI-002` | daemon-owner | Compromised same-user process remains in trust boundary |
| `TH-CLI-002` | App/log/settings text injects terminal controls or corrupts JSON consumers | Escape human mode; schema-bound JSON mode with no ANSI/prose and bounded values | `ADV-CLI-003`, `ADV-CLI-004` | cli-owner | Downstream consumer ignores schema/version |
| `TH-CLI-003` | Headless/non-interactive command spoofs permission/destructive confirmation | Explicit typed command/diff; interactive confirmation; no Stage 0 noninteractive bypass | `ADV-CLI-005` | cli-owner | Future automation policy can be overbroad |
| `TH-CLI-004` | Secret setting leaks through argv/env/history/output/logs | Protected FD/stdin or OS store, opaque handles, universal redaction | `ADV-CLI-006` | settings-owner | Same-user debugger/root can inspect process memory |
| `TH-B-001` | Symlink/archive traversal reads host or writes outside workspace | Bounded mounts, beneath-root resolution, no host home | `ADV-B-001`, `ADV-B-002` | builder-owner | Container/runtime filesystem bug |
| `TH-B-002` | `build.rs` or proc macro reads/exfiltrates canary secret | No secrets, deny/allowlist, build offline, canary oracle | `ADV-B-003`, `ADV-B-004` | dependency-owner | Approved macro compromise |
| `TH-B-003` | Native C/assembly or compiler plugin expands execution surface | Deny guest native code; explicit host review | `ADV-B-005` | dependency-owner | Wasmtime host needs reviewed native dependencies |
| `TH-B-004` | Dependency/git/network fetch is substituted or phones home | Locked crates.io checksums; no git; isolated allowlisted fetch; offline build | `ADV-B-006`, `ADV-B-007` | supply-chain-owner | Registry/account compromise |
| `TH-B-005` | Fork bomb, disk fill, memory/CPU loop or timeout abuses builder | UID/capabilities plus PID/CPU/RAM/disk/time limits | `ADV-B-008`, `ADV-B-009`, `ADV-B-010`, `ADV-B-011` | builder-owner | Kernel/container escape |
| `TH-B-006` | Repository reaches Docker socket, metadata or another tenant | No mounts/routes/credentials; canary probes; VM-grade later | `ADV-B-012`, `ADV-B-013` | builder-owner | Docker-only boundary insufficient for public multi-tenancy |
| `TH-B-007` | Dependency/tool cache is poisoned across jobs | Content-addressed verified read-only cache; tenant-safe keys | `ADV-B-014` | supply-chain-owner | Upstream signed malicious release |
| `TH-B-008` | Generated tests fabricate pass or output swapped after test | Independent verifier, digest handoff, quarantine, no publish authority | `ADV-B-015`, `ADV-B-016` | verifier-owner | Verifier shares vulnerable parser/tool |
| `TH-B-009` | Build logs leak synthetic/real secrets or inject audit records | No real secrets, redaction, structured bounded logs, separate audit | `ADV-B-017` | observability-owner | Novel encoding bypass |

## 6. Lifecycle and revocation acceptance

| Transition | Required authoritative result |
| --- | --- |
| Install | Artifact is present but disabled until grants/settings and activation succeed. No Store or lease exists merely because files were acquired. |
| Enable | Daemon checks current digest, grants, settings schema and quarantine state, then creates a fresh generation. |
| Open UI | Launcher creates/focuses a generation-bound view. It does not enable a disabled service without a separate typed confirmation. |
| Close UI | View/focus resources are revoked. A separately enabled service continues and remains visibly enabled in Launcher status. |
| Disable | New dispatch blocked first; calls cancelled; leases/grants/Stores revoked; daemon status becomes disabled. UI may show static settings/logs but no app code runs. |
| Permission change | New grants apply only to a new/restarted generation; removing a grant cancels affected calls and leases before the transition completes. |
| Update | Candidate uses a new digest/generation; no capability grant expands silently; old leases cannot be inherited by identity alone. |
| Rollback | Previous verified digest and state revision regain routing; candidate leases/handles are destroyed. |
| Uninstall | Disable completes first; artifacts/grants/leases removed; user chooses retain/export/delete app data under host control. |

## 7. Required audit events

The host writes bounded structured events for install source/digest, enable/disable, UI open/close, service start/stop, permission diff, settings revision, generation activate/drop, preview start/stop, scheduler lease create/revoke, trap/timeout/quarantine, update/rollback, uninstall and verifier decision. The guest cannot supply event type, authoritative timestamp, app identity, severity or result.

Logs and audit events are separate stores. Viewing guest logs must not grant the guest an execution opportunity. Export escapes control characters and includes the authoritative app/digest/generation fields.

## 8. Gate 3 acceptance statements

Gate 3 documentation is complete when an independent reviewer confirms:

1. Every council-required adversarial class appears in the threat register and inventory.
2. Every threat maps to at least one control, one fixture oracle and one owner.
3. UI, service, hybrid, Launcher switching, owner-authenticated headless CLI and web preview boundaries are represented without capability inheritance.
4. No profile grants ambient filesystem, environment, network, sockets, process, home, Docker, metadata, hardware identity or credential access.
5. Resource, cancellation, disable/uninstall, crash-loop, orphan-lease, output-quarantine and independent-verification behavior is numerically or observably testable.

This document does **not** assert that controls already exist. Gate 3 authorizes later conformance implementation only after Gates 0–6 pass together. Actual adversarial fixtures and generated-code execution remain prohibited in this pre-code tranche.
