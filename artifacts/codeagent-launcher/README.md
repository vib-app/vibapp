# CodeAgent Launcher Service contract skeleton

Status: **Experimental / HOLD**

## User-authorized product executor (September 2026)

The original contract-skeleton scope below is historical. `docker_executor.py`
now implements the separately user-authorized local Docker CodeAgent, without
releasing any frozen Stage 0 gate. Codex has no workload network, host mount,
provider credential or compiler; its host-only Responses relay supplies exactly
the consent-bound endpoint/model.

### Local parallel authoring and authentication

The product executor admits at most **two Docker authoring jobs per host user**.
Each keeps its own container, workspace, input/task identity, transcript, cancellation
and terminal receipt, with the existing 2 GiB memory, 2 CPU, 64 PID and wall-time
bounds. `host_budget.py` provides stable owner-private process locks; short Docker
admission counts reservations and existing containers, including leftovers after
worker failure. A full budget is contention, not permission to create another job.
The host compiler remains single-slot across intermediate source checks and final
Builder runs. Queuing is bounded and cancellable. Docker is not itself a scheduler
or an unlimited-concurrency guarantee; unrelated containers also consume VM memory.

All jobs reuse the current host-selected Codex connection through the existing
trusted Responses relay. Host credentials are not copied into images, mounted as
`auth.json`, passed as container environment variables, or put in source/artifacts.
Do not mount the host `.codex` directory: it also carries user configuration and
other material the authoring workload does not need. Each job has a separate relay
and per-job request/retry limits; all still share the upstream account's actual
quota/rate limits. This is not account pooling or an increase in plan capacity.

The currently exercised path uses the configured custom HTTPS provider credential.
Host-login/keyring/refresh variants must be tested separately before claiming them
equivalent. A future public multi-user cloud deployment must use an appropriately
scoped service credential or user-authorized account policy, not redistribute the
developer's personal login. See the [official Codex authentication documentation](https://developers.openai.com/codex/auth).

Codex transient HTTP recovery is owned by that relay, not stacked with CLI
retries: 429/502/503/504 permit at most three wire attempts per logical request,
and at most eight extra attempts across a job, still inside the 48-request total
(40 initial-authoring plus 8 shared compiler-repair) and wall-time budgets. The
job-wide retry count never resets after a successful request or a phase change.
Delays back off with jitter and honor a bounded Retry-After;
a requested delay above 30 seconds stops instead of retrying early. Cancellation
interrupts waiting. Only explicit HTTP rejection before downstream response bytes
is replayable. Unknown/auth/configuration errors, uncertain POST transmission and
partial streams are not replayed. The same Codex process, tool history and source
workspace survive these HTTP retries. This is not cross-process checkpoint/resume.

This policy is part of the execution identity; old consent never authorizes changed
policy bytes. More than one model request can incur more than one charge. A retry
does not authorize another model, tools, endpoint, new permissions or publication.
OpenCode keeps its existing separate policy; it does not inherit these HTTP retries.

Private terminal evidence records logical requests, wire attempts, retry count,
compiler checks and a bounded status/phase history, never request bodies, provider
error prose or credentials. Shared UI consumes only a closed diagnostic projection.
Retries must not be reported as first-pass app-generation success. A passing
transport fixture is not application/runtime or public-package acceptance.

Before reading provider credentials or sending model requests, image preflight
also matches the installed trusted workspace/relay bytes to this release's
`docker/entry.mjs` (plus `opencode-provider.mjs` for OpenCode). A stale image is
reported as `docker-image-stale`; refresh it using the existing Dockerfile, then
admit a fresh task. This does not impose a new Codex CLI version pin. Packaged
clients must ship these trusted bridge sources alongside the launcher Python
files. Immutable SDK input files remain read-only; their `source/src/` directory
must permit the author UID to create sibling application modules. Test that with
the opt-in offline real-CLI container regression before spending live model calls.

## Original contract skeleton

This directory is a source-controlled experimental contract. It is not an accepted VibApp
Stage 0 implementation, a durable service, a running Docker adapter, a Kubernetes
integration, or evidence that live CodeAgent execution is safe. The tests invoke
only in-memory fake executors. They do not start a process or container, contact a
cluster or provider, read credentials, or use the network.

## Boundary

VibApp talks to one transport-neutral Launcher Service contract:

```text
VibApp / orchestrator
        |
        | submit, status, events, cancel, result
        v
CodeAgent Launcher Service
        |
        +-- local Docker executor       (separate implementation/gate)
        +-- remote Kubernetes executor  (reserved; fail-closed here)
```

The submit request contains only stable identity and policy references:

- job, attempt and idempotency IDs;
- provider profile, provider and model IDs;
- SHA-256 digests for the normalized task, input and prompt;
- server-side resource-policy and network-policy IDs.

Model IDs reuse the adapter's deliberately broad safe envelope: 1–256 characters,
trimmed, not `-`-prefixed and free of C0/DEL control characters. Provider forms such
as `organization/model:variant@release` are therefore not accidentally excluded.

The request is a closed object. It has no field for an image, argv, shell text,
environment variable, mount, volume, host path, user/group, capability, device,
socket, credential, token, secret, or Kubernetes/Docker primitive. Those facts are
resolved from a trusted immutable `ProviderProfile` on the server. Unknown fields,
duplicate JSON keys, malformed UTF-8, unsupported versions and out-of-bound values
fail before executor selection.

## Execution and recovery contract

`Executor` has Docker and Kubernetes kinds but no transport assumption. The service
allocates and records the exact backend execution ID before crossing the executor
boundary, and passes that ID as the executor's idempotent create key. An executor
must never replace it with a new identity.

This sample core stores records only in memory. A real service must durably and
transactionally store the request digest, idempotency reservation, exact backend ID
and event sequence before launch. After a crash or launch-response loss it must
recover the same backend by that ID and drive it through status/cancel/cleanup. It
must not start a replacement attempt, and it must not call a job terminal merely
because the control plane lost contact. `cleanup-pending` is the fail-closed state
until cleanup and whole-job quiescence are positively confirmed.

`ExecutorUnavailable` has a narrow meaning: the executor has proved that no backend
was created for the assigned ID. An ambiguous launch failure is instead retained as
`launch-outcome-unknown` plus `cleanup-pending`, so it remains recoverable.

The included `KubernetesExecutor` is deliberately inert. Every operation returns
`kubernetes-executor-unavailable`; it imports no Kubernetes client and performs no
cluster or network access. A later remote implementation can replace that adapter
without changing the service contract.

## Progress events

`list_events(after_sequence, limit)` is the polling form of the same cursor contract
that a future HTTP/SSE/gRPC stream can expose. Sequences are per job-attempt,
strictly increasing and bounded. Phases are closed:

```text
queued -> starting -> running -> quiescing -> terminal
```

Events contain only `sequence`, a closed `phase` and a closed localization-friendly
`code`. They contain no free text, timestamps, prompt/input content, provider output,
exception message, log line, path, environment value or secret. A UI can translate
the codes itself without receiving untrusted diagnostics.

## Result release invariant

The service releases output only for `succeeded`, and only after a trusted executor
receipt proves both:

- `cleanup_confirmed = true`;
- `whole_job_quiescent = true` (the whole execution unit, not just its foreground
  process, has no remaining member).

The receipt must exactly bind all of the following to the stored job:

- job ID, attempt ID, idempotency key and canonical request digest;
- backend execution ID and Docker/Kubernetes kind;
- provider profile ID/digest, provider ID and model;
- immutable image digest;
- resource and network policy IDs/digests;
- task, input and prompt digests;
- output media type, exact size and SHA-256 digest.

The service recomputes output size and digest before release. A missing, mismatched or
non-quiescent receipt yields no output. `held` is used only after the exact backend
observation has already confirmed cleanup/quiescence but the success artifact binding
is invalid; it is not a substitute for cleanup.

CodeAgent output remains untrusted source. This launcher grants no build, verifier,
install, signing, publication or Registry authority. Builder execution must remain a
separate containment and trust domain.

## Files and local checks

- `codeagent_launcher.py` — contract types, executor protocol, in-memory state
  machine and cursor API.
- `schemas/submit-request.schema.json` — strict caller request.
- `schemas/status-response.schema.json` — bounded status projection.
- `schemas/event-page.schema.json` — bounded monotonic event page.
- `schemas/result-response.schema.json` — success-only output and receipt.
- `tests/test_launcher.py` — standard-library fake-executor tests.

Run the low-load offline checks from this directory:

```sh
python3 -m unittest discover -s tests -v
```

Passing these tests means only that this experimental contract skeleton behaves as
tested. It does not accept Stage 0, authorize a live provider, or establish Docker or
Kubernetes isolation.
