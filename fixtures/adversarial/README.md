# Adversarial Fixture Inventory

Status: **inventory only; no executable payloads are authorized in the pre-code tranche**

Normative threat mapping: [../../docs/stage0/threat-model.md](../../docs/stage0/threat-model.md)
Profiles: `component`, `launcher`, `service`, `preview`, `cli`, `builder`

This file freezes names, setup, oracle and ownership for future hand-authored conformance fixtures. It intentionally contains no Rust source, Wasm binary, proc macro, build script, malware, external callback URL, secret or runnable generated package.

## 1. Fixture record contract

Every future fixture directory MUST include a non-executable metadata record with:

- `fixture_id`, `threat_ids`, `profile`, `input_kind`, `description`;
- `synthetic_canaries`, never real credentials or personal data;
- `preconditions` and exact resource/policy profile;
- `action`, bounded by the test harness;
- `expected_decision`: `reject-before-load`, `terminate-generation`, `deny-host-call`, `quarantine-build`, `drop-message`, or another enumerated result;
- `expected_observables`: exit/typed error, audit event IDs, unchanged namespaces/revisions, lease count and resource cleanup;
- `forbidden_observables`: host file/network access, cross-app/user output, duplicated effect, published/installed output, unbounded log, orphan process/lease;
- deterministic `cleanup`, timeout and owner.

An exit code, guest/build log or fixture-provided test report is not the oracle. The harness derives the result from trusted process state, authoritative storage, audit events, network sink and resource counters.

## 2. Safe handling rules

1. Fixture content is synthetic and minimal. It MUST use reserved example identifiers, loopback/black-hole test sinks and canary strings with no external value.
2. Builder fixtures run only after Gates 0–6 and the independent pre-execution review pass. They use the exact `ISO-BUILDER-01` profile; never a developer home or ambient Docker socket.
3. All fixtures have an outer harness timeout and cleanup assertion. A timeout without cleanup is a failed control, not a passed denial.
4. Malicious asset fixtures are inert byte patterns targeting the validation contract. No known weaponized exploit or active malware is stored.
5. Cross-user fixtures use two synthetic principals; cross-app fixtures use unrelated synthetic app IDs/digests/generations.
6. Network tests use a harness-owned local observer or RFC 2606/5737 reserved names/addresses. No public service receives test data.
7. Actual fixture payload additions require security-owner review and must remain under `fixtures/adversarial/`.

## 3. Component/runtime inventory

| Fixture ID | Threat | Future input | Trusted oracle |
| --- | --- | --- | --- |
| `ADV-C-001` | `TH-C-001` infinite loop | Minimal Component event handler that never returns | Fuel/epoch or 2 s outer limit terminates and drops only that generation; daemon responsive. |
| `ADV-C-002` | `TH-C-001` recursion/stack exhaustion | Bounded-size Component causing recursive call depth | Typed trap; Store dropped; no daemon crash/reuse. |
| `ADV-C-003` | `TH-C-002` memory/table/instance growth | Component requests beyond each exact Store budget separately | Limiter rejects at boundary; process RSS remains within ceiling; namespace unchanged. |
| `ADV-C-004` | `TH-C-003` host-call/failure flood | Repeated permitted and denied calls | Both consume 100/s burst-200 budget; later calls get rate error; logs bounded. |
| `ADV-C-005` | `TH-C-004` oversized UI | View over 256 KiB or 2,048 nodes | Whole response rejected; previous trusted view retained. |
| `ADV-C-006` | `TH-C-004` deep UI | View depth 33 and cyclic/repeated identifier variants | Validator rejects before render; no focus tree installed. |
| `ADV-C-007` | `TH-C-004` oversized string | 16 KiB+1 and invalid/edge UTF-8 representations | Typed size/decoding error; sanitized audit only. |
| `ADV-C-008` | `TH-C-004` oversized list | 4,097 scalar/list entries | Typed resource-limit error; no partial effect. |
| `ADV-C-009` | `TH-C-005` malformed import | Invalid Component/WIT import encoding or type | Reject before instantiation and before grants prompt. |
| `ADV-C-010` | `TH-C-005` undeclared/unknown required import | Valid unknown import absent from manifest/host | Exact incompatibility error; no best-effort link. |
| `ADV-C-011` | `TH-C-006` trap before effect acknowledgement | Effect intent followed by trap before guest ack | Host transaction commits at most once; retry does not duplicate. |
| `ADV-C-012` | `TH-C-006` trap after effect acknowledgement | Trap immediately after ack boundary | Authoritative ledger records exactly one effect and disposed generation. |
| `ADV-C-013` | `TH-C-007` blocked async host call | Harness host call blocks past cancellation | Call cancelled, lease released, Store gone by outer deadline. |
| `ADV-C-014` | `TH-C-008` active SVG/document | SVG/HTML-like asset with scripts/events/external refs | Rejected media type/content or rendered inert by trusted decoder; zero callbacks. |
| `ADV-C-015` | `TH-C-008` decompression/dimension bomb | Tiny encoded image declaring excessive dimensions/expansion | Decode rejected before allocation exceeds asset budget. |
| `ADV-C-016` | `TH-C-008` asset path traversal | `../`, absolute, encoded separator and symlink-like asset references | No path API reached; content digest lookup fails closed. |
| `ADV-C-017` | `TH-C-008` MIME mismatch | Declared safe image with active/unknown magic bytes | Sniff mismatch rejected; no browser/native parser dispatch. |
| `ADV-C-018` | `TH-C-009` log/control injection | Newlines, ANSI/OSC, bidi controls and HTML-like text | One structured record; controls escaped/marked; app source cannot change. |
| `ADV-C-019` | `TH-C-010` native/AOT submission | Native object or Wasmtime precompile bytes labeled Component | Canonical validation rejects; unsafe deserialize path has zero calls. |

## 4. Launcher and multi-app inventory

| Fixture ID | Threat | Future input | Trusted oracle |
| --- | --- | --- | --- |
| `ADV-L-001` | `TH-L-001` cross-app event | Event envelope for app A delivered on app B route | Dropped before component call; both state revisions unchanged. |
| `ADV-L-002` | `TH-L-001` cross-app state/settings ID | App A asks for app B/user B namespace | Broker derives A namespace and denies foreign identifier; no existence leak. |
| `ADV-L-003` | `TH-L-002` stale generation/view | Delayed event from prior digest/generation/revision | Dropped and audited; current generation gets no call. |
| `ADV-L-004` | `TH-L-003` capability carryover | Switch from permission-rich app A to app B reusing a visual slot | App B context contains only its persisted grants; every A ephemeral handle invalid. |
| `ADV-L-005` | `TH-L-004` chrome spoof | App view uses reserved “System/Verified/Permissions/Update” labels and overlay geometry | Content remains clipped below persistent authoritative header; reserved roles/icons unavailable. |
| `ADV-L-006` | `TH-L-005` focus/input capture | Hidden app retains modal, pointer capture, IME or accessibility node after switch | Capture/focus revoked before B activates; only B receives input. |
| `ADV-L-007` | `TH-C-009` notification/source spoof | App text names another app or VibApp system | Host-added source identity remains authoritative and visually separate. |

## 5. Headless/hybrid service inventory

| Fixture ID | Threat | Future input | Trusted oracle |
| --- | --- | --- | --- |
| `ADV-S-001` | `TH-S-001` UI-close/service-state confusion | Close hybrid app window while service enabled, then while disabled | Launcher/daemon status distinguishes both; close never mutates enabled flag. |
| `ADV-S-002` | `TH-S-002` disable revocation | Disable during scheduled/in-flight service event | New dispatch blocked; calls cancelled; Store/grants/leases reach zero. |
| `ADV-S-003` | `TH-S-002` uninstall persistence | Uninstall with UI closed and leases pending | Disable completes first; artifact/grants/leases removed; data follows host choice. |
| `ADV-S-004` | `TH-S-003` crash loop | Three abnormal exits/timeouts inside five minutes | No fourth automatic restart; daemon marks quarantined. |
| `ADV-S-005` | `TH-S-004` orphan scheduler lease | Update/rollback/crash with old generation lease | Reconciliation removes old lease; one authoritative schedule remains. |
| `ADV-S-006` | `TH-S-005` settings injection | Command/path/URL/markup control characters in typed settings | Schema/type/length policy rejects or stores inert data; no interpolation sink. |
| `ADV-S-007` | `TH-S-005` secret setting read/log | Synthetic opaque secret handle and canary | Raw value never enters guest/log; unauthorized operation denied. |
| `ADV-S-008` | `TH-S-006` spoofed health | Guest reports verified/healthy while host sees timeout/crash | Daemon state wins; app result labeled app-reported. |
| `ADV-S-009` | `TH-S-006` fabricated audit/log | Backdated high-severity record claiming scan/update success | Guest cannot set audit type/time/result; text remains bounded app log. |
| `ADV-S-010` | `TH-S-007` hardware fingerprint | Queries for CPU/GPU/serial/MAC/IP/memory/process/perf counters | Interfaces absent/denied; only coarse compatibility host-info returned. |
| `ADV-S-011` | `TH-S-008` periodic exfiltration | Background component repeatedly requests HTTP/metadata/private IP | Stage 0 import rejected; future broker oracle must enforce exact destination and periodic grant. |

## 6. Web-preview inventory

| Fixture ID | Threat | Future input | Trusted oracle |
| --- | --- | --- | --- |
| `ADV-P-001` | `TH-P-001` DOM/cookie/hardware access | Preview requests DOM, parent, cookies/storage and device APIs | Imports absent; Worker has no DOM; preview origin carries no auth cookie. |
| `ADV-P-002` | `TH-P-001` network/background access | Fetch/WebSocket/service-worker/notification/background request | CSP/broker denies; harness network observer sees zero request. |
| `ADV-P-003` | `TH-P-002` cross-preview message | Session A message sent through/global to session B | Wrong port/nonce/session dropped; B state unchanged. |
| `ADV-P-004` | `TH-P-002` cross-user/cache state | Same artifact previewed by synthetic users A/B | Artifact bytes may deduplicate by digest; all mutable state remains separate. |
| `ADV-P-005` | `TH-P-003` mock capability spoof | App claims real notification/scheduler/hardware and covers banner | Host banner persists; result envelope remains `preview-simulated`. |
| `ADV-P-006` | `TH-P-004` preview-to-install confusion | Preview sends install/permission command or altered digest | Broker has no operation; trusted parent refetches immutable digest and local Launcher re-prompts. |
| `ADV-P-007` | `TH-P-005` preview persistence | Attempt to register service worker/storage then close session | Registration impossible; Worker/port/state terminated and purged at expiry. |

## 7. CLI inventory

| Fixture ID | Threat | Future input | Trusted oracle |
| --- | --- | --- | --- |
| `ADV-CLI-001` | `TH-CLI-001` wrong local user | Synthetic second UID connects to per-user UDS | Peer credential rejected before command parsing; no existence/state leak. |
| `ADV-CLI-002` | `TH-CLI-001` socket replacement/version confusion | Symlink/replaced socket and unsupported protocol/schema versions | Daemon/client fail closed with trusted local error; no fallback TCP/legacy mode. |
| `ADV-CLI-003` | `TH-CLI-002` terminal escape | App/log/settings values contain ANSI/OSC, bidi, newline and control bytes | Human output escapes/marks content; terminal observer sees no control action. |
| `ADV-CLI-004` | `TH-CLI-002` JSON injection/evolution | Malicious strings, oversized arrays, unknown optional and required schema cases | One valid bounded JSON value/record per contract; no ANSI/prose; version behavior deterministic. |
| `ADV-CLI-005` | `TH-CLI-003` permission/destructive spoof | Noninteractive enable/grant/uninstall without future preauthorization | Command fails before daemon mutation and prints authoritative required diff in the chosen output mode. |
| `ADV-CLI-006` | `TH-CLI-004` headless secret leakage | Synthetic canary supplied by argv/env versus protected FD/stdin path | argv/env forms rejected; accepted secret never appears in process snapshot/output/log and returns only opaque handle. |

## 8. Builder/supply-chain inventory

| Fixture ID | Threat | Future input | Trusted oracle |
| --- | --- | --- | --- |
| `ADV-B-001` | `TH-B-001` symlink escape | Workspace link to synthetic host canary/outside output | Read/write fails; canary unchanged; no path disclosure. |
| `ADV-B-002` | `TH-B-001` archive traversal | Archive entries with absolute/parent/encoded separators | Extraction rejects atomically beneath workspace. |
| `ADV-B-003` | `TH-B-002` `build.rs` secret read | Synthetic package build script probes canary locations/env | Dependency policy rejects before build or isolated probe reads no canary. |
| `ADV-B-004` | `TH-B-002` proc-macro secret read | Synthetic proc macro probes canary locations/env | Policy rejects or isolated probe reads no canary; no egress. |
| `ADV-B-005` | `TH-B-003` native code | Guest dependency declares C/C++/assembly/`links` | Guest policy rejects with exact dependency path. |
| `ADV-B-006` | `TH-B-004` git/alternate dependency | Git URL, alternate registry, patch/replace | Manifest/lock policy rejects before fetch. |
| `ADV-B-007` | `TH-B-004` unapproved network fetch | Build/test attempts DNS/HTTP to harness sink/private/metadata IP | Network observer sees zero successful egress; job quarantined on attempt. |
| `ADV-B-008` | `TH-B-005` fork bomb | Process fan-out past 256 PIDs | PID limit terminates job/process group; worker cleaned. |
| `ADV-B-009` | `TH-B-005` memory/CPU loop | Process exceeds 4 GiB/2 CPU/10 min | Resource controller terminates; other job/control plane healthy. |
| `ADV-B-010` | `TH-B-005` disk fill | Writes past workspace/output/tmpfs quota | ENOSPC/quota failure; host disk remains bounded. |
| `ADV-B-011` | `TH-B-005` log flood | Unlimited stdout/stderr/control strings | Log cap/redaction engages; job cannot exhaust service storage. |
| `ADV-B-012` | `TH-B-006` Docker/host access | Probe common socket, home and host mount paths | Paths absent; no daemon response; no host identifier leak. |
| `ADV-B-013` | `TH-B-006` cloud metadata/tenant access | Probe link-local metadata and synthetic adjacent-tenant canary | No route/credential; zero read/egress. |
| `ADV-B-014` | `TH-B-007` cache poison | One job tries to mutate tool/dependency cache used by next | Cache read-only; next job digest unchanged. |
| `ADV-B-015` | `TH-B-008` fabricated test report | Repository emits fake success JSON/log/exit | Verifier ignores repository claim and derives failure from clean checks. |
| `ADV-B-016` | `TH-B-008` output swap | Artifact changes after test before handoff | Quarantine digest mismatch invalidates candidate. |
| `ADV-B-017` | `TH-B-009` encoded secret/log injection | Synthetic canary in plain/base64/split/control strings | Redactor/oracle reports no exported canary; audit stream remains separate. |

## 9. Cross-fixture acceptance

The future harness MUST prove after every case:

- daemon/Launcher/preview parent remains responsive;
- no unexpected network request, file read/write, capability grant, install, enable, publication or signature occurs;
- no foreign app/user/settings/KV/log/view/event namespace changes;
- no Store, Worker, process, timer, scheduler lease, focus/modal token or writable mount remains past cleanup deadline;
- audit records contain trusted fixture/app/digest/generation IDs without raw canary secrets;
- rerunning a case yields the same decision and authoritative state digest.

The future VibApp skill must route generated packages to the profile-specific subset of these fixture IDs and cannot waive a fixture because the code agent claims it tested equivalent behavior.
