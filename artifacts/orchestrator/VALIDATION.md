# Local validation record

State: implementer-tested experimental/HOLD; not independently accepted.

The focused Python suite has 29 passing tests (19 delivery-controller and 10 local-
queue tests). It exercises real local cloud-agent
validation/dry-run, a non-fixture dynamic job binding, exact truth labels, duplicate
idempotency, explicit-submit/consent gates, crash recovery, corrupt-task quarantine,
legacy outbox isolation, request/concurrency/timeout/log bounds, and handoff
confinement. The final combined run completed in 3.535 seconds. No live provider/model,
credential, Builder, installation, signing or publication action is used.

The packaged-resource smoke accepted a dynamic Desktop task, returned
`dry-run-complete`, kept attempted/observed false and the gateway ID null, and reported
Builder `not-invoked`. Repeating the exact task and root returned `duplicate=true`.

Exact commands and final results are recorded in the Chief-of-Staff handoff report.

## Durable automatic delivery controller

The focused controller suite covers:

- Registry recommendation/forged-handoff evidence is rejected before CodeAgent;
- a static local provider exercises the real CodeAgent handoff, Builder quarantine,
  independent Verifier, and private AppStore paths with exact package/Component/
  manifest/receipt/candidate digest equality, then proves a restarted controller is
  idempotent;
- an injected Builder failure persists a stage-specific diagnostic; a same immutable
  task retry succeeds only with the next exact attempt ID/ordinal, unchanged
  NeedSpec/job/provider identity/Registry evidence, and a new attempt-bound consent;
- an edited NeedSpec takes the separate new-task path with a newer revision, new
  job/request/consent, attempt ordinal 1, and an explicit `supersedes_task_id` link;
- same-request queue attempts receive distinct attempt-bound idempotency keys, while
  replaying the exact same attempt remains idempotent;
- restart replay recomputes the immutable task and rejects rehashed task,
  receipt-entrypoint/trigger, and manifest-semantic substitutions;
- the fixed SafeFixtureRunner Component is paired with the exact same app/version/
  display-name/entrypoint descriptor in the task and manifest.

The safe test runner injects a digest-pinned prebuilt Component and explicitly reports
that generated source was not compiled. This proves controller composition and digest
authority, not production Builder isolation or fresh source compilation.
