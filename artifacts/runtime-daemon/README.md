# VibApp local runtime daemon prototype

This ignored product artifact is a bounded lifecycle control plane and service
Component host. It owns install, update/rollback, enable/disable, service generations, UI
surface state, status and uninstall disposition. Service and hybrid packages
run their declared service entrypoint through a daemon-owned native worker;
the worker compiles and instantiates the exact installed Component digest in a
disposable Wasmtime Store.

The worker links only the accepted UI, service or hybrid WIT world. It links no
ambient WASI filesystem, network, environment or process authority. Each
generation runs in a separate process with a 64 MiB linear-memory ceiling,
fuel, epoch interruption, a 250 ms guest-event deadline, a 2 s health deadline
and an outer daemon timeout. Service generations are owned by `(app,
entrypoint)` and capped at eight live workers per app; one app does not consume
or ambiguously select another app's generations. A separate 64-worker daemon
resource ceiling remains fail-closed. Linux workers also receive a 768 MiB
address-space limit;
macOS calls are sampled every 50 ms and terminated above 512 MiB resident
memory because Darwin rejects lowering the JIT process address-space limit.
Workers receive a 64-file-descriptor ceiling and core dumps are disabled. An
automatically granted, manifest-bounded desktop KV capability is bound to the
generation's exact state revision. App-scoped transactional reads and writes
are shared by an app's UI and service workers through one locked, atomically
persisted broker. Scheduler, notification, system metric and live HTTP remain
typed unavailable; the worker does not silently grant them.

The same owner-controlled binary exposes a separate one-shot
`--inspect-descriptor` protocol for the independent Builder Verifier. This mode
does not load daemon KV, settings, clocks, logs, or effect brokers: every imported
host interface returns a typed unavailable result or a fixed non-authoritative
value. It calls only Guest `describe` under the same 64 MiB Component Store,
fuel/epoch interruption and a 2 s deadline, emits one bounded strict JSON record,
and exits. It does not install, enable, launch, or retain a generation.

Install and update require two independent inputs:

1. the canonical `daemon-request` JSON envelope with an immutable package digest;
2. an explicit verifier-created `vibapp.builder-candidate.experimental-v1`
   promotion record beneath the daemon's configured `--promotion-root`.

The daemon reopens every regular package file, rejects links/extra files,
recomputes manifest, artifact, component and package digests, copies the exact
bytes into owner-private storage and stages the app disabled. The installed record
retains the manifest digest as an additional binding. Restart recovery rechecks the
canonical app/digest package path, manifest digest, artifact bytes, and full package
digest before it may recover entrypoints or triggers; legacy records are backfilled
only after that complete recomputation succeeds. A CodeAgent or Builder success
message is never install authority.

Run:

```sh
cd artifacts/runtime-daemon
python3 -m vibapp_daemon serve \
  --root /absolute/runtime/root \
  --promotion-root /absolute/builder/output/candidates \
  --socket /absolute/runtime/root/run/vibappd.sock

python3 -m vibapp_daemon ctl \
  --socket /absolute/runtime/root/run/vibappd.sock \
  --envelope /absolute/request.json \
  --promotion-record /absolute/builder/output/candidates/<digest>/candidate.json
```

The daemon resolves `vibapp-service-runtime` from its local validated build
output by default. A packaged launcher should bundle the same ordinary native
binary and set `VIBAPP_SERVICE_RUNTIME_BIN` to its absolute path before starting
the Python daemon. Alternatively it can place the binary at
`runtime-daemon/service-runtime/vibapp-service-runtime` beside the packaged
Python module, which is the built-in lookup path. The binary must be an
executable regular file controlled by the current user or root, not writable by
group/other, and not a symbolic link.

For commands other than install/update omit `--promotion-record`. An update is
addressed by the candidate package's exact SHA-256 digest. The daemon verifies
the candidate against an independent promotion record, copies the bounded app
state (`state.schema` is the target schema; `migratable_from_min/max` bound the
accepted source schemas), runs the declared WIT state migration against the
isolated copied revision, then runs candidate validation/health (and starts
each service that was live before the update) and atomically switches package
and state routing. UI-only candidates are validated through the exact UI WIT
world while Launcher-owned surface/session state remains stable. A
pending switch is recovered to the previous generation after daemon restart,
and a candidate crash during the observation window automatically rolls back.
`status` exposes the durable update transaction, including from/to version and
digest, migration result, activation health, rollback reason, bounded history,
Launcher surfaces and generation-GC state.

The rollback observation interval is fixed at five minutes and persists its
start/deadline. A backward clock cannot shorten it, an invalid clock fails
closed, and restart resumes the same deadline. After expiry, bounded journaled
garbage collection removes obsolete package/update generations in batches of
at most eight while retaining the active package and last rollback generation;
an interrupted deletion is replay-safe. Event-time KV writes use the same
bounded idempotency ledger and atomic persistence as migration writes. Migrated
KV bytes and their target revision are atomically persisted and fsynced before
package/state routing can switch.

Enable starts only service entrypoints whose verified manifest explicitly declares
`on-enable`, delivers WIT start reason `enabled`, and requires bounded healthy or
degraded reports before committing the enabled state. Guest Start and KV operate on
an isolated candidate-state copy behind a durable enable journal. A crash while the
journal is `preparing` or `switching` restores the prior active directory and keeps
the app disabled; a durable `committed` record keeps the new active directory and
enabled state. A failed automatic start drops all newly created workers and restores
the disabled app-data snapshot. `service-start`
is available only to entrypoints that declare `manual`; it creates a fresh generation,
delivers the WIT start event and requires a bounded healthy or degraded report before
recording it running. `service-stop` delivers the
stop event and always drops the Store. The product transport extensions
`service-trigger` (`entrypoint`, `trigger_id`, byte-array `payload`) and
`service-health` (`entrypoint`) execute the corresponding bounded guest calls
and persist authoritative diagnostics. `service-trigger` is the daemon scheduler
delivery path: only an entrypoint declaring `scheduler` can receive it, and a
scheduler-only service cold-starts on its first delivery. The frozen
experimental-v0 `service-start-reason` has no scheduled variant, so this bridge
records daemon/audit cause `scheduler` while delivering Guest start reason `manual`.
That compatibility mapping is not a complete scheduler ABI; a real `scheduled`
reason requires a future versioned WIT contract. `status.value.services` exposes the
daemon-derived generation, health, event count and typed last error.

`launch` opens or refocuses a host surface. `surface-close` records the UI close
without disabling the app or stopping an independently running hybrid service.
Disable blocks new dispatch first and drops all service Stores; uninstall does
that before applying its separate retain/export/delete disposition. A stopped,
disabled or uninstalled service receives no further guest calls.

This is not launchd/systemd/Windows Service packaging and is not evidence of
Windows/Linux runtime support. A `running` service row now means a live
daemon-owned Component generation exists and passed its activation health
check; recovery after daemon restart creates a fresh generation and delivers
the WIT `host-restart` start reason.

Validation:

```sh
artifacts/runtime-daemon/tests/run-validation.sh
```

The smoke starts a real owner-authenticated Unix-domain socket process, stages a
fresh promoted Rust-authored WASI 0.2 Component, exercises actual service
start/health/trigger, UI open-close/status, restarts the daemon process, proves
a fresh guest generation is recovered, then tests manual stop, disable, typed
guest failure and retain/delete uninstall dispositions.

The 2026-08-28 fresh lifecycle-gate run rebuilt every native/WASM fixture in an
isolated `/tmp` target and passed all 45 daemon/socket tests plus the CLI smoke.
