# Stage 0 Alarm Semantics

- Status: Repaired frozen candidate contract for Gate 4 re-review
- Contract version: `vibapp.alarm-semantics.v1`
- Date: 2026-08-11
- Test host: macOS 26.5.2 / Darwin 25.5.0, arm64
- Runtime scope: one trusted local `vibappd` daemon, a replaceable host-rendered launcher/app view and non-rendering control clients

## 1. Purpose and scope

This document freezes the observable alarm behavior that Stage 0 must test before scheduler or alarm guest source may be written. It defines product semantics, persistence and fault outcomes; it does not prescribe a timer library, database, macOS API, GUI framework, Wasm engine or implementation. Every typed boundary named below is representable by `vibapp:experimental-v0@0.0.1`; settings `configure` and alarm control are deliberately separate commands.

The key invariant is:

> The daemon is the only scheduler and notification owner. The alarm component authors definitions and receives durable events; the VibApp GUI launches and renders apps. Opening, focusing, closing or quitting an app view never creates, transfers or destroys scheduler ownership.

The words **MUST**, **MUST NOT**, **SHOULD** and **MAY** are normative.

Stage 0 proves only the declared macOS ARM64 host. Windows, Linux, mobile, browser execution, browser push, critical-alert entitlement, wake from powered-off state and cross-host parity are excluded. A page-local or closed-browser alarm is explicitly not claimed. This contract nevertheless keeps daemon commands independent of GUI rendering so a future `vibapp` CLI can use the same semantics; it does not claim that Raspberry Pi, Ubuntu or headless Linux has been implemented or tested.

## 2. Fixed Stage 0 constants

| Name | Value | Meaning |
| --- | --- | --- |
| `clock_resolution` | 1 second | Durable wall-clock fields use WIT `utc-instant`: exactly 20 characters `YYYY-MM-DDTHH:MM:SSZ`. |
| `awake_dispatch_tolerance` | 2 seconds | While the daemon is ready and the host is awake, the first notification-adapter attempt MUST start no earlier than the due instant and no later than due + 2 seconds. |
| `recovery_dispatch_tolerance` | 5 seconds | An eligible catch-up attempt MUST start within 5 seconds after the daemon declares recovery ready. |
| `catch_up_grace` | 900 seconds, inclusive | Lateness `0..=900` seconds is delivered as catch-up. Lateness `>900` seconds is terminally missed. |
| `notification_retry_offsets` | `[0, 2, 10]` seconds | At most three adapter attempts, measured from the first eligible processing instant and clipped by the catch-up deadline. |
| `guest_event_attempts` | 3 | A guest event that traps or times out three times is dead-lettered; this never reverses the host delivery. |
| `default_snooze` | 600 seconds | Default snooze duration. A request may specify an integer duration from 60 through 3,600 seconds. |

An OS adapter result of `accepted` proves only that macOS accepted the request. It does not prove that the user saw or heard it. Stage 0 requests the standard notification sound when permission allows, but it does **not** promise continuous ringing, critical-alert bypass, exact sound playback or wake from sleep/power-off.

`utc-instant` accepts only valid Gregorian date/time values, seconds `00..59`, uppercase `T`/`Z`, UTC and no fraction or whitespace. Offsets, fractional seconds, lowercase `z`, leap-second `:60` and invalid calendar values fail as `invalid-argument`. Monotonic deadlines and retry waits remain unsigned milliseconds and are never serialized as wall time.

## 3. Normative data and ownership

### 3.1 Host-owned authoritative state

The daemon MUST durably own and namespace by user and app ID:

- the stable `alarm_id`, enabled/disabled state and monotonically increasing `schedule_revision`;
- the civil-time definition, explicit IANA time-zone ID, recurrence and fixed DST policies;
- the exact WIT `notification-projection` (`title`, `body`, `standard-sound`) sufficient to deliver without executing the guest; no other projection fields are part of Stage 0;
- every materialized occurrence, its resolved UTC due instant, resolver time-zone-data label and terminal outcome;
- pending snoozes and notification-action deduplication records;
- the notification outbox, adapter attempt history and stable notification identifier;
- the durable guest-event inbox, attempts and acknowledgement/dead-letter state;
- command, occurrence, notification and action idempotency records;
- notification permission observations;
- the active component generation route and upgrade/rollback transaction state.

Raw Wasm memory, guest resource handles, GUI state and an in-process timer are never authoritative.

### 3.2 App-owned domain state

The component MAY own presentation preferences and other app-domain data in its transactional app-scoped KV revision. Once a schedule command commits, the host record above is authoritative for delivery. A component trap, unload or absence cannot invalidate it.

The GUI/launcher owns only ephemeral presentation state such as the selected route, focus and window visibility. It MUST query the daemon after opening or restoring an app view and MUST NOT infer a schedule from cached view state.

### 3.3 Stable identities

Canonical logical keys are:

```text
occurrence_id       = occ:{alarm_id}:r{schedule_revision}:{due_at_utc}
notification_id     = notify:{occurrence_id}
guest_event_id      = guest:{occurrence_id}
command_dedup_key   = cmd:{daemon_request.idempotency_key}
action_dedup_key    = action:{os_action_id}
snooze_occurrence   = snooze:{parent_occurrence_id}:{snooze_sequence}
```

The textual form in fixtures is normative. A future implementation may store a collision-resistant digest in addition to the canonical text, but MUST preserve the same identity relation.

## 4. Time and schedule semantics

### 4.1 Time representation

- Persisted due instants and event times use UTC.
- User schedules use civil date/time plus an explicit valid IANA time-zone ID. A launcher may default the field from the system zone, but `follow-system` is not a Stage 0 schedule mode.
- Changing the macOS system zone does not move an existing alarm. The user must edit the alarm to change its stored zone.
- Wall time determines alarm due instants. Monotonic time is used only for timeouts, retry waits and clock-correction detection.
- The host records the time-zone-data label used to resolve each materialized occurrence. An already materialized UTC due instant is never re-resolved after a time-zone database update. The next not-yet-materialized civil candidate uses the then-active database.

An invalid time zone or a one-shot instant that resolves at or before its create/edit commit time MUST be rejected without changing state.

### 4.2 Supported Stage 0 schedules

Stage 0 supports:

1. `one-shot`: one civil local date and time;
2. `daily`: one local time on every civil day from an inclusive start date;
3. `weekly`: one local time on a non-empty set of ISO weekdays from an inclusive start date.

Weekday semantics follow ISO order (`Monday=1` through `Sunday=7`), but the typed wire values are the WIT enum names `monday`, `tuesday`, `wednesday`, `thursday`, `friday`, `saturday` and `sunday`; numeric weekday values are not accepted on the normalized JSON wire.

The WIT `local-recurrence` always contains `kind`, `time`, inclusive `start-date`, `time-zone`, `weekdays`, `gap` and `overlap`. `daily` requires `weekdays=[]`; `weekly` requires a non-empty unique weekday list. The first candidate is on or after `start-date`, but it must resolve strictly after the create/edit/enable commit instant.

Intervals other than one, end dates, calendar exceptions, cron syntax and astronomical schedules are outside Stage 0 and MUST fail validation rather than be approximated.

Recurring candidates are derived from the civil schedule, never from actual delivery time, acknowledgement time or snooze time. Late delivery therefore causes no recurrence drift. At create or edit, a recurring schedule selects the earliest resolved candidate strictly after the transaction commit instant.

### 4.3 DST and civil-time discontinuities

The policies are fixed, not defaults that an implementation may change:

- **Ambiguous fold:** choose the earlier UTC occurrence (the pre-transition offset) and materialize exactly one occurrence.
- **Nonexistent gap:** shift the requested local time forward by the exact transition gap and materialize exactly one occurrence.

For example, under `America/New_York` in 2026:

- `2026-11-01 01:30` resolves to the earlier `2026-11-01T05:30:00Z`, not `06:30Z`;
- nonexistent `2026-03-08 02:30` shifts by the one-hour gap to local `03:30`, which is `2026-03-08T07:30:00Z`.

### 4.4 Wall-clock correction

The daemon MUST re-evaluate waits when wall time diverges from the monotonic baseline:

- a backward correction before a due instant delays processing until the persisted UTC instant is reached; it does not create another occurrence;
- a backward correction after a terminal occurrence never reopens it;
- a forward correction applies the same catch-up/missed rules as restart or resume.

No notification may be attempted before its persisted UTC due instant.

## 5. Occurrence and delivery state machine

### 5.1 Occurrence states

Each materialized regular or snooze occurrence moves through one path:

```text
scheduled
  -> cancelled
  -> missed
  -> permission-denied
  -> delivery-pending
       -> notification-accepted
       -> notification-failed
```

All states other than `scheduled` and `delivery-pending` are terminal. Terminal occurrences are immutable audit facts.

### 5.2 Due transaction and effect boundary

When an occurrence becomes due, the daemon MUST use one durable transaction to:

1. claim the unique `occurrence_id`;
2. re-check that its alarm/revision is enabled and not superseded;
3. classify it as on-time, catch-up or missed;
4. for an eligible delivery, set `delivery-pending` and create exactly one outbox row keyed by `notification_id`, or set `permission-denied` if authorization is absent;
5. enqueue exactly one guest event keyed by `guest_event_id`, containing one closed host delivery classification: `on-time`, `catch-up`, `missed` or `permission-denied`;
6. advance the regular recurrence cursor and materialize the next regular occurrence when applicable.

Committing this transaction is the linearization point against edit and cancel. Notification I/O and guest execution occur only after commit.

The notification worker performs the idempotent logical operation `put(notification_id, projection)`. Retries MUST use the same identifier and payload. Multiple adapter attempts are permitted after a crash; the expected logical/presented notification set contains at most one item for that identifier. Adapter acceptance changes `delivery-pending` to `notification-accepted`. Three failed attempts, or expiry of the catch-up deadline, changes it to `notification-failed`. Retry eligibility is based on the original first-eligible instant; after restart, an overdue remaining retry runs immediately within the recovery tolerance rather than rebasing the retry sequence.

The guest event is independent of the notification outbox. A guest trap cannot suppress, retract or duplicate the host notification. A successful guest handler applies its KV mutation and acknowledges `guest_event_id` atomically. After three traps/timeouts the event is dead-lettered and the occurrence/recurrence remains valid.

The typed WIT `delivery-event` contains `schedule`, `key`, `occurrence`, canonical `scheduled-for-utc`, canonical `dispatched-at-utc`, the closed `classification`, `attempt` and bounded opaque app payload. Classification is fixed by the host due/recovery transaction and cannot be supplied or overridden through payload. `on-time` means the continuously ready/awake path processed the occurrence; `catch-up` means a recovery path observed lateness `0..=900` seconds; `missed` means recovery lateness was greater than 900 seconds; `permission-denied` takes precedence when permission was `denied` or `not-determined` at an otherwise on-time or catch-up due transaction. Adapter failure does not rewrite the host delivery classification.

### 5.3 Permission behavior

Permission is checked per due occurrence:

- `authorized`: create the outbox row;
- `denied` or `not-determined`: create no notification outbox row, mark the occurrence `permission-denied`, enqueue the guest outcome event and advance recurrence.

The daemon MUST NOT display a permission prompt from background due processing. Granting permission later affects only future occurrences; it does not resurrect a terminal denied or missed occurrence. Typed alarm status reports only `authorized`, `denied` or `not-determined`; a UI may derive `ready` from `authorized` and `degraded` from either other value, but those derived labels never appear in the daemon contract.

## 6. Recovery and missed-event policy

The same rule applies after daemon restart, GUI absence, machine sleep/resume, user login recovery and forward clock correction:

- lateness `<=900` seconds: commit and attempt one logical catch-up notification immediately, with the first attempt within five seconds of recovery readiness;
- lateness `>900` seconds: mark `missed`, create no notification outbox row and enqueue a guest missed event;
- every elapsed recurring candidate is assigned a terminal result in order, and recurrence advances from its civil cursor rather than from recovery time.

Closing the GUI, closing only the alarm app view, navigating to the launcher home screen, or explicitly quitting the GUI process is not a recovery event and has no scheduler-state effect. Only daemon availability matters.

Crash recovery MUST distinguish these durable boundaries:

- crash before the due transaction commits: recovery may claim and commit the same occurrence;
- crash after outbox commit but before adapter call: recovery calls `put` with the existing `notification_id`;
- crash after adapter acceptance but before the acceptance marker: recovery may repeat `put` with the same identifier, never create a second logical notification;
- crash after the acceptance marker: recovery performs no adapter call.

## 7. Configure, enable, disable and snooze

Every mutating operation below carries an outer `daemon-request.idempotency-key`; it is the sole command-deduplication field. Replaying the same identifier with the same canonical payload returns the original committed result with `deduplicated=true` and without another revision or side effect. Reusing it with a different payload returns `host-error.code=conflict` with `message=idempotency-conflict` without changing state.

### 7.1 Configure and edit


`alarm-configure` is the control-surface-neutral operation behind both create and edit; settings `configure` is unrelated:

- for an `alarm_id` not yet present in host state, it validates and creates an enabled alarm at revision 1, then materializes its next occurrence;
- for an existing `alarm_id`, it performs the edit transition below;
- the same canonical payload and commit instant MUST produce the same semantic daemon state whether submitted by the GUI, CLI or another authorized local client.

An edit is an idempotent `alarm-configure` command for an existing `alarm-id`. On commit it:

- increments `schedule_revision` by one;
- cancels every still-`scheduled` regular occurrence and pending snooze from the previous revision;
- validates and stores the complete replacement definition/projection;
- materializes the replacement's next occurrence;
- leaves terminal occurrence history immutable.

If edit commits before the due transaction, the old occurrence is cancelled and cannot notify. If the due transaction commits first, its notification remains valid and the edit cannot recall it.

### 7.2 Disable and cancel

Typed `alarm-cancel` is a Stage 0 compatibility alias for `alarm-disable`; both execute the same daemon transition and return the same result shape.

Disabling an enabled alarm increments its revision, changes it to disabled, cancels every still-`scheduled` regular occurrence and pending snooze, retains the validated definition/projection, and leaves terminal history intact. Disabling an already-disabled alarm is a successful no-op and does not increment the revision. Disabling after the due transaction cannot recall an accepted or pending logical notification, but prevents later recurrence.

### 7.3 Enable

Enabling a disabled alarm is one transaction:

- a recurring alarm increments its revision, becomes enabled and materializes the earliest civil candidate strictly after the enable commit instant;
- a one-shot alarm does the same only when its stored civil instant resolves strictly after the enable commit instant;
- an expired one-shot fails as `expired-one-shot`, remains disabled, keeps its revision and materializes nothing;
- enabling an already-enabled alarm is a successful no-op and does not increment its revision or replace its pending occurrence.

No enable operation resurrects a cancelled, missed, denied, accepted or failed occurrence. Re-enabling always uses the new revision and, when valid, a new canonical occurrence ID.

### 7.4 Snooze

A snooze command references a delivered occurrence and a unique `os_action_id`:

- due time is `action_received_at_utc + duration_seconds`, using UTC elapsed time rather than civil-time recurrence rules;
- the first action creates `snooze:{parent}:{sequence}`; replay of the same action ID returns that same child and creates nothing;
- a snooze occurrence can be snoozed again, incrementing the sequence;
- snooze never shifts or consumes the next regular recurrence;
- if a snooze and regular recurrence are both due, both distinct occurrence IDs are delivered; Stage 0 does not coalesce them;
- edit or cancel of the parent alarm cancels any still-pending snooze.

## 8. Control surfaces, launcher and headless semantics

GUI and CLI are control surfaces, not scheduler owners. Both use the exact `control.daemon-request` fields `request-id`, `idempotency-key`, audit-only `client`, installed-app `subject`, and typed `command`. Their transport-specific requests MUST normalize to the same envelope. `client` MUST NOT change validation, authorization, revision transitions, occurrence or effect IDs, recurrence, notification behavior, idempotency, returned command result or normalized status.

The closed alarm commands are distinct from app lifecycle and settings commands:

- `alarm-configure {alarm-id, expected-revision, schedule, notification}` creates or atomically edits a complete alarm;
- `alarm-enable`, `alarm-disable` and typed alias `alarm-cancel` carry `{alarm-id, expected-revision}`;
- `alarm-snooze` carries `{parent-occurrence-id, os-action-id, action-received-at-utc, duration-seconds}`;
- `alarm-status` carries `{alarm-id, occurrence-limit, effect-limit}`.

Mutation responses use `alarm-command-result {alarm-id, outcome, revision, enabled, next-occurrence-id, snooze-occurrence-id, deduplicated}`. `outcome` is exactly `created`, `updated`, `enabled`, `disabled`, `unchanged` or `snoozed`. An expired one-shot enable returns `host-error.code=conflict` with `message=expired-one-shot`, not a successful outcome.

App `status` and `alarm-status` are different commands. `alarm-status` is a read-only daemon query returning `alarm-status-snapshot`: app ID; optional string `active-generation`; closed `rollback-state`; and the alarm's complete schedule/projection, enabled flag, revision, next occurrence ID, bounded occurrence records, bounded notification/guest/snooze effect-ledger records and permission health. Occurrence states are exactly `scheduled`, `cancelled`, `missed`, `permission-denied`, `delivery-pending`, `notification-accepted` or `notification-failed`. Notification effects are `pending|accepted|failed`; guest effects are `pending|acked|dead-lettered`.

`occurrence-limit` and `effect-limit` MUST be positive and within the host ceiling. Rows are newest-first with a deterministic ID tie-break; `occurrences-truncated` and `effects-truncated` distinguish complete results from bounded prefixes. Permission health is exactly `authorized|denied|not-determined`; rollback state is exactly `none|observation-window|rolling-back`; generation IDs are host-issued strings such as `gen-1`. `alarm-status` MUST NOT activate a generation, launch/render an app view, register a timer, create a command-dedup record or mutate durable state. Presentation state, transport details and audit-only `client` are excluded when comparing semantic status across clients.

For the machine fixtures, normalized Stage 0 CLI JSON maps WIT kebab-case fields to snake_case, records to objects, enums to their kebab-case strings, options to `null` or their value, variants to `{"tag":"<case>","value":<payload-or-null>}`, and `u32`/`u64` to in-range JSON integers. This is one deterministic serialization of the same typed envelope, not a second API.

The VibApp GUI is the launcher/shell for installed apps, including the alarm app.

- Launching the alarm app opens or restores its host-rendered view and reads current daemon state.
- If that app view already exists, launch focuses/restores it rather than creating another logical view owner.
- Multiple UI sessions or repeated launch intents MUST NOT create another daemon, timer loop, platform registration or scheduler lease.
- Navigating away, closing the app view, closing the launcher window, hiding to tray or quitting the GUI leaves every committed alarm unchanged.
- Reopening reconstructs the view from daemon state, including degraded permission, missed, delivery-failed and dead-letter indicators.
- If the daemon is unavailable, the launcher shows an unavailable/reconnect state. It MUST NOT run an in-process fallback scheduler or claim alarms are protected.

All alarm-configure/edit/enable/disable/cancel/snooze requests from a view are typed commands to the daemon and follow the same idempotency and transaction rules as any other client.

A headless deployment MAY omit the launcher and every app view. On the current Stage 0 host, the abstract CLI acceptance cases use alarm-configure, alarm-enable, alarm-disable and alarm-status with zero GUI processes and zero renders while the daemon remains the only scheduler. A request to launch/render an app is unsupported on such a surface; that has no effect on an alarm. These cases freeze control-surface semantics only and are not evidence of a Linux CLI or daemon port.

Disabling or uninstalling the alarm app is different from closing its view:

- App lifecycle `disable` is an idempotent daemon command distinct from `alarm-disable`. In one transaction it marks the app disabled and applies the alarm disable transition once to every enabled alarm owned by that app; already-disabled alarms do not gain another revision. It then rejects guest-originated schedule mutations as `app-disabled`. Re-enabling the app never implicitly re-enables its alarms.
- App lifecycle `uninstall` MUST first commit the same bulk alarm deactivation and block guest dispatch/mutation. Only that daemon acknowledgement authorizes the launcher/package manager to proceed with package and app-KV removal. The package/KV deletion mechanics are outside Gate 4.
- Neither operation recalls a notification whose due transaction already committed. The common transaction boundary decides a race: lifecycle deactivation first cancels a `scheduled` occurrence; due commit first preserves its logical notification.
- If the daemon is unavailable, the launcher MUST NOT claim the app's alarms are disabled or safely removed. It shows the launcher presentation state `daemon-unavailable` (not a `daemon-response` error code) and may not bypass the alarm-deactivation prerequisite.
- Terminal occurrence, notification and action ledgers remain immutable host audit facts. App disable/uninstall never transfers scheduling to the GUI, CLI or guest.

## 9. Upgrade, migration and rollback

- Host schedule definitions, occurrence/outbox/action ledgers and notification projections do not live in guest memory and are not guest-migration payloads.
- A shadow generation has no live schedule mutation or notification authority.
- Existing pending occurrences keep their IDs and due instants across a generation switch. The active generation at guest-event dispatch receives the event; notification delivery does not depend on it.
- Failed migration or pre-activation health leaves the old generation and host schedule state unchanged.
- During typed rollback state `observation-window`, alarm configure/edit/enable/disable/cancel and every guest-originated schedule mutation are rejected as `upgrade-in-progress`; host-owned notification actions such as snooze remain allowed and durable.
- A rollback restores the previous generation route and app KV revision. It does not roll back terminal occurrence, notification, action or snooze audit facts and MUST NOT resend an already committed occurrence.
- Requests carrying a shadow, old or otherwise inactive generation token are rejected without changing schedule state.

After the rollback window closes successfully, ordinary schedule mutations resume. The length of that runtime observation window is a Gate 2/runtime concern; Gate 4 requires only the frozen mutation behavior while it is active.

## 10. Machine-readable acceptance matrix

The normative cases are [../../fixtures/alarm/cases.json](../../fixtures/alarm/cases.json), validated structurally by [../../fixtures/alarm/cases.schema.json](../../fixtures/alarm/cases.schema.json).

Every case contains:

- exact initial facts and ordered input events;
- expected persisted-state path/value assertions;
- expected logical notification records and adapter attempts;
- expected deduplication keys;
- expected generation/rollback result and scheduler owner.

Cases that compare GUI and CLI clients use two isolated worlds with identical initial state, canonical command payload and commit time. Their semantic daemon projections MUST be deeply equal after excluding only the explicitly listed audit/presentation paths.

The top-level `wire_contract` and `wire_examples` are also normative. They enumerate exact WIT/normalized-JSON field sets and validate representative projection, daily/weekly recurrence, GUI/CLI daemon envelope, command result, full alarm status and all four delivery classifications. Snake-case JSON field names map mechanically to the WIT kebab-case names above.

`persisted_state[].path` is an RFC 6901 JSON Pointer into the implementation's normalized test-state projection, and `value` is an exact deep-equality assertion. `notifications` lists logical notification activity newly caused by the ordered case events; an empty list therefore means no new adapter activity, even when `given` already contains terminal history. Unless a case overrides it, permission is `authorized` and the guest acknowledges its durable event on the first attempt.

An implementation must pass every case without weakening an assertion. Adding implementation-specific state is allowed; changing a normative value requires a new contract version and Gate 4 review.

## 11. Gate decision

This contract resolves all behaviors required by Council Gate 4 for the current host. Gate 4 is **PASS at the pre-code contract level** when both fixture files validate and contain all required tags. It is not implementation evidence. Any scheduler or guest implementation that cannot provide the idempotent notification adapter, transaction boundary, host-owned fallback projection, fixed DST policy or launcher/daemon ownership above MUST stop and revise this contract rather than silently degrade it.
