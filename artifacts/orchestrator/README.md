# VibApp local development orchestrator

Status: experimental/HOLD local product artifact. It is not Stage 0 acceptance. Live
source authoring is delegated to the separate local CodeAgent Adapter.

The orchestrator owns a private, persistent, digest-addressed queue between Desktop
and the separately held cloud CodeAgent boundary. Submission requires all four facts:

1. the NeedSpec is complete;
2. the fixed cloud-task schema preview passes `cloud_agent.py validate`;
3. remote processing consent is exact, granted, single-use, job-bound and immutable
   digest-bound;
4. Desktop sends the explicit user-submit command.

The queue accepts only `vibapp.cloud-codeagent-task.experimental-v3`. Historical v2
tasks remain schema-readable at the CloudAgent boundary, but cannot execute because
they do not bind a provider execution identity or an execution attempt.

The idempotency key binds both the immutable provider-request digest and the exact
`execution_attempt.attempt_id`. Repeating the same attempt returns its original
receipt; a separately consented attempt for the same immutable request gets a distinct
queue identity. Ready tasks are atomically claimed into `processing/`; crash recovery
restores an abandoned claim and moves incomplete attempts to `quarantine/`.

`process-one` invokes only `cloud_agent.py validate` and the reviewed cloud-agent
dry-run engine. A bounded local adapter binds the static UI fixture metadata to the
already validated dynamic job; it accepts only the fixed `ui-only-reference` world
and never changes generated source or authority. It never invokes `run`, an external
runner, a provider/model, Builder, installation, signing or publication. Both accepted
statuses—`queued-for-codeagent` and
`dry-run-complete`—require `external_request_attempted=false`,
`external_request_observed=false`, and a null gateway ID.

The queue ignores every legacy `outbox/` path. Its only operational tree is the
explicit `--root`, with stable `ready`, `processing`, `done`, `receipts`, `dry-runs`,
`attempts`, `quarantine`, `locks`, `slots`, `validation`, `runtime-home`, and `logs`
subdirectories.

Bounds are enforced for 128 KiB requests/records, 64 queue files, 256 MiB queue data,
two concurrent workers, 10-second validation, 30-second dry-run, 512 MiB child RSS,
64 KiB stdout/stderr, 8 KiB log records, and 2 MiB total structured logs. Desktop
requires a fixed Python 3.11+ executable containing `tomllib`; unsupported system
Python fails closed before queue execution.

```sh
python3 artifacts/orchestrator/orchestrator.py submit \
  --root /private/tmp/vibapp-local-queue \
  --cloud-agent artifacts/cloud-agent/cloud_agent.py \
  --task artifacts/cloud-agent/fixtures/valid-task.json \
  --explicit-submit --process-dry-run
```

## Automatic private delivery controller

`delivery_controller.py` is the restart-safe product controller for the next boundary:

```text
confirmed Registry no-match
  -> configured CodeAgent adapter
  -> Builder quarantine
  -> independent Verifier candidate
  -> private local AppStore
```

The controller does not replace those authorities. It records one immutable provider
request and up to 64 ordered execution attempts, appends a bounded event log before every durable summary
transition, and consumes only the existing source handoff, quarantine receipt,
Verifier candidate, and AppStore record. The terminal state
`private-appstore-ready` is accepted only when package, Component, manifest,
quarantine-receipt, and candidate-record digests agree across all three downstream
records. Restart replay also recomputes the immutable task/NeedSpec/consent binding,
revalidates the complete CodeAgent handoff and source tree, compares the receipt's
exact entrypoints and service triggers, and projects each quarantined, candidate, and
private-AppStore manifest back to that task. Consent expiry is not compared with the
replay clock: it gates a new provider call, not historical verification. It never
installs or publishes.

Terminal records created before `task_handoff_manifest_binding_proven=true` are kept
as history but fail closed in Desktop instead of being displayed as accepted apps.
They require a new explicitly authorized attempt; digest-only legacy success is not
silently upgraded.

Python integration API:

- `DeliveryController.submit(task, registry_no_match, explicit_user_submit=True)`
  validates exact CodeAgent consent plus a Registry `refinement` result with no
  recommendations and creates attempt 1 using the exact v3 task attempt ID.
- `run_attempt(task_id, attempt_id=None)` automatically advances the current attempt.
  Replaying a completed attempt is idempotent. A source-ready CodeAgent handoff,
  Builder receipt, Verifier candidate, or AppStore ingest already committed before a
  controller restart is consumed without rewriting its bytes.
- `status(task_id)` returns the current attempt; `history(task_id)` returns every
  retained attempt and its failure diagnostic.
- `submit(..., retry_task_id=task_id)` is only a same-immutable-task retry after a
  transient-class failure. It preserves the exact NeedSpec revision, job, immutable
  provider request, provider execution identity, and Registry evidence while requiring
  the next attempt ordinal, a new attempt ID, and a new single-use consent bound to it.
- `submit(..., edited_from_task_id=task_id)` is the separate edited-NeedSpec path. It
  creates a new delivery task at attempt ordinal 1, requires a newer NeedSpec revision,
  new job/request digest/consent, and records `supersedes_task_id`; it never fabricates
  revision history inside the old immutable task.

The configured adapter is provider-neutral at this boundary: the controller never
changes `task.provider` or consent fields. It expects the adapter's terminal status to
carry `status=source-ready`, `handoff_relative_path`, `handoff_sha256`, `provider_id`,
`external_request_attempted`, and `external_request_observed`. Builder configuration
remains explicit (`tool layer`, accepted offline Cargo home, cache acceptance); there
is no ambient Cargo fallback.

Known interface debt: the current adapter status/handoff schema does not yet carry an
`execution_attempt` object natively. The controller therefore gives every attempt its
own status/output directory and binds that path through the attempt record, exact task
bytes, consent ID, job, immutable digest, and provider-identity digest. Adding the
attempt object to the adapter status/handoff remains the cleaner next schema revision;
the current path isolation is explicit compensation, not a claim that the debt is gone.

CompleteNeed capability failures use a separate durable product-assessment record,
not a delivery task. The strict additive schemas are
`schemas/product-assessment-task.schema.json` and
`schemas/product-assessment-attempt.schema.json`. An assessment is terminal with
`reason_code=unsupported-capability-combination`, binds the exact draft NeedSpec
digest/revision plus a bounded request summary, and fixes
`codeagent_task_created=false`, `external_request_made=false`, and
`provider_process_started=false`. It appears in Launcher/Web history, but it is never
accepted by `DeliveryController.run_attempt` and cannot consume provider consent.

The CLI supports `submit`, `run`, `status`, and `history`. `submit`, `status`, and
`history` do not require Builder configuration. `run` (and `submit --run`) fail closed
unless all three Builder inputs and the external-cost acknowledgement required by the
configured CodeAgent are provided.
