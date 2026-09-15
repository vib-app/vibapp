# VibApp external one-job provider runner

Status: **experimental / deployment-unprovisioned / live HOLD**.

This ignored artifact fills the interface and lifecycle gap identified by the final
independent `artifacts/cloud-agent` review. It provides a production-shaped,
fail-closed boundary for one external CodeAgent job and a bounded local synthetic
oracle. It is not a deployed cloud runner, does not contain an accepted runner
profile or trust key, and has never called Codex, a model, a provider, or a network.

## Boundary

```text
CloudAgent ProviderRunnerRequest v2
  -> exact schema/document, execution-attempt and provider-identity validation
  -> canonical provider-execution-identity digest + executable digest binding
  -> exact per-job policy validation
  -> accepted runner/policy/executable pins (absent by default)
  -> one durable (job, execution-attempt) claim, before credential-handle issuance
  -> fresh isolate ID + never-reused UID/GID allocation
  -> descriptor-bound, sealed input snapshot matching the request digest
  -> exact command-shape validation and host-path rewrite to pinned image paths
  -> external rootless OCI/microVM supervisor (absent by default)
       read-only base / non-root / empty inherited environment
       opaque single-use credential handle on FD, not credential bytes
       provider-gateway-only egress / no DNS / no metadata/private routes
       CPU, RSS, PID, disk, wall-time and stream ceilings
       whole-isolate kill and supervisor-observed quiescence
  -> gateway attempted/observed truth only from a signed gateway receipt
  -> descriptor-bound, sealed returned-output snapshot + post-transfer rehash
  -> canonical ProviderRunnerReceipt v3 + detached runner signature
```

`provider_runner.py` contains the strict policy, bundle, identity, gateway-receipt,
Receipt v3, detached-verifier, production-backend, and synthetic-lifecycle seams.
`cloud_agent_adapter.py` maps this seam to the existing `artifacts/cloud-agent`
`ProviderRunner` protocol. The adapter also has no default credential broker.
`schemas/provider-runner-interface.schema.json` is a local semantic mirror of the
current CloudAgent Request v2 / Receipt v3 schema; the regression suite deep-compares
the parsed schemas so another one-sided contract change fails immediately.

The CloudAgent request contains host paths for its executable, workspace, result
schema, and last-message file. The runner never forwards those paths into the
isolate. It verifies the mandatory request model matches the command's single explicit
`--model` argument, the exact remaining command flags, and pinned provider-result schema bytes
(currently SHA-256 `fe7c40611e940e3a86fa16f63fc45d6b592384d23b8fa5614c6418c44cff75d9`),
including its schema-version and document-type constants,
then rewrites them to `/opt/vibapp/...` and `/job/work/...` paths supplied by the
reviewed image and per-job volume. Sandbox-mode or output-path drift rejects.

The production path fails before a job claim unless all of these are supplied:

- a separately provisioned backend that owns a whole one-job VM/container;
- an independently accepted profile pinning runner identity, reviewed policy,
  executable bytes, gateway destination policy, and both verifier key IDs;
- a runner receipt signer and trusted gateway receipt verifier;
- a broker that issues only a bounded opaque single-use credential handle.

The checked-in `accepted-profile.template.json` is intentionally unprovisioned and is
rejected by `AcceptedRunnerProfile.from_dict`. Changing only its status and current
policy/executable/destination pins is still rejected: template/sentinel identifiers,
their punctuation/case variants, zero-looking key IDs, and all-zero digest fields
cannot cross `from_dict` or `preflight`.

`begin` exclusively persists the replay claim for the exact `(job_id,
execution_attempt)` and creates the input snapshot before the adapter asks the broker
for a handle. A failed attempt remains consumed, while a valid higher-ordinal attempt
can retry the same immutable task/NeedSpec. The broker request is bound to job,
attempt, immutable task digest and provider execution identity digest. The snapshot
copy reads already-open
`O_NOFOLLOW` descriptors, hashes the bytes actually copied, rechecks inode/metadata
and directory membership, seals the copy, and requires the sealed digest to equal the
request. The backend receives that snapshot path and the deployment contract requires
it to be mounted read-only. The same descriptor-bound procedure creates the returned
output quarantine after quiescence; Receipt v3 signs that quarantine's digest, the
exact execution attempt and provider execution identity digest, the
service rehashes it after signing, and the adapter rehashes it once more after the
atomic workspace rename. Any later failure asks the broker to revoke or consume the
issued handle; a known replay never asks for one.

## Synthetic oracle

The synthetic runner executes only the hashable inert fixture
`fixtures/inert_worker.py` with a closed mode enum. It clears the environment and
passes no `HOME`, `CODEX_HOME`, proxy variables, SSH agent, container host/socket, or
credential handle. It checks input/output trees, applies local rlimits where the host
supports them, monitors process-group PID/RSS/disk/stream/wall limits, kills the whole
fixture process group, and requires two quiet observations.

The allocated UID/GID in a synthetic report is an identity-allocation oracle only;
`identity_enforcement=synthetic-label-only` says explicitly that the local macOS
fixture did not create a user namespace. Synthetic output always records
`external_request_attempted=false`, `external_request_observed=false`, and
`gateway_request_id=null`; it cannot issue a live/accepted Receipt v3.

Run the local bounded suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning \
  -m unittest discover -s artifacts/provider-runner/tests -p 'test_*.py' -v
```

Run one inert lifecycle check:

```sh
state_root="$(mktemp -d)"
input_root="$(mktemp -d)"
printf '{}\n' > "$input_root/request.json"
PYTHONDONTWRITEBYTECODE=1 python3 artifacts/provider-runner/provider_runner.py synthetic \
  synthetic-job-1 "$input_root" --state-root "$state_root"
```

No command in this README pulls an image, starts a container, reads a credential, or
uses the network.

## Evidence state

- Authored and locally regression-checked only; the security repair awaits a fresh
  independent review and is not self-accepted.
- Request v2 / Receipt v3 attempt and provider-identity bindings plus detached
  verifier interfaces are exercised with
  synthetic test-only signatures; no production trust root exists.
- Deployment files are reviewable templates with explicit placeholder pins. They are
  not evidence that a network policy, user namespace, cgroup, seccomp policy, image,
  gateway, key, or credential broker is running.
- The existing cloud worker remains live HOLD until provisioning and a fresh
  independent acceptance pass complete the runbook gates.

See [RUNBOOK.md](RUNBOOK.md) for the deployment and independent-acceptance sequence.
