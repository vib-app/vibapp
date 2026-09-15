# External one-job provider runner deployment runbook

Status: **procedure only; nothing in this repository is deployed or accepted live**.

## 1. Freeze identities and bytes

1. Independently review `provider_runner.py`, `cloud_agent_adapter.py`, the existing
   cloud-agent ProviderRunner Request v2 / Receipt v3 schema, the local semantic
   schema mirror, and every deployment file.
2. Canonicalize and hash `deployment/runner-policy.json` and
   `deployment/gateway-egress-policy.json` using the runner's `canonical_json`.
3. Build the runner image in a separate approved build system. Record its immutable
   image digest, SBOM, provenance, scanner evidence, entrypoint executable SHA-256,
   owner, mode, and exact version. A tag is not a pin.
   The image must contain provider-result schema bytes with SHA-256
   `fe7c40611e940e3a86fa16f63fc45d6b592384d23b8fa5614c6418c44cff75d9` and the
   policy-bound `vibapp.cloud-codeagent-provider-result.experimental-v1` /
   `cloud-codeagent-provider-result` discriminator constants.
4. Replace every `REPLACE_...` value in `oci-runtime-template.json` with a reviewed
   digest. Do not weaken `read-only`, namespaces, no-new-privileges, empty
   capabilities, empty host mounts, or whole-cgroup lifecycle.
5. Create an accepted profile only after an independent evaluator signs the exact
   runner identity, canonical runner-policy digest, executable digest, destination
   policy digest, receipt verification key ID, and gateway verification key ID.
   `status=independently-accepted` is an evaluator decision, never a deployment script
   default.

Fail/stop: mutable image, tag-only executable, missing SBOM/provenance, profile made by
the runner implementer, or any pin mismatch.

## 2. Provision per-job isolation

1. Allocate a new user namespace and never-reused UID/GID plus a new PID, IPC, UTS,
   mount, network and cgroup namespace for each job.
2. Start from the read-only reviewed image. Drop all capabilities, enable
   no-new-privileges and the reviewed seccomp/LSM policy, expose no devices, and never
   use privileged or host PID/IPC/network modes.
3. Create a descriptor-bound snapshot from nofollow file/directory descriptors;
   hash the exact copied bytes, recheck source inode/metadata/membership, seal and
   rehash the snapshot, and require it to equal the request input digest. Mount this
   exact snapshot read-only. Mount only input, fresh noexec output quarantine, and bounded noexec tmpfs. Do not
   mount the host repository, home, SSH directory/agent, cloud config, provider auth,
   Docker/container socket, package cache, or another job.
4. Create the child environment from the allowlist; do not inherit it. In particular,
   prove the absence of `HOME`, `CODEX_HOME`, every upper/lowercase proxy variable,
   SSH agent, Docker/container host, Kubernetes service variables, and cloud-provider
   credential variables.
5. Only after the durable `(job_id, execution_attempt)` replay claim and input
   snapshot succeed, deliver a random, single-use, short-lived opaque credential
   handle through the declared protected FD. Broker issuance must bind job, attempt,
   immutable task digest and provider execution identity digest. The workload must
   never receive reusable credential bytes. Only the trusted provider gateway may
   redeem the handle for this exact binding. Every failure after issuance must
   synchronously request broker revocation/consumption; a known attempt replay must
   issue no handle. A retry uses a new valid higher-ordinal attempt, never a fabricated
   NeedSpec revision.
6. Set cgroup/VM ceilings from the signed request, never above the reviewed envelope:
   CPU seconds, RSS/memory (no swap), PIDs, workspace/output/tmpfs bytes, wall time,
   stdout and stderr. Enforce a maximum output file count in the trusted supervisor.

Fail/stop: reused identity/volume, ambient env, any host mount/socket, plaintext or
reusable credential, unbounded logs, swap, or a limit configured only inside the
untrusted workload.

## 3. Enforce gateway-only egress

1. Give the job namespace a default-deny egress policy. It may connect only to the
   independently pinned private gateway address and port over authenticated mTLS.
2. Deny workload DNS, loopback service discovery, cloud metadata, link-local,
   multicast and all private/host routes other than the exact gateway route. Do not
   use proxy environment variables as the network boundary.
3. The gateway separately allowlists the intended provider origin/method/size/rate,
   redeems the opaque handle, and never returns credential bytes to the workload.
4. The gateway signs a canonical `GatewayReceipt` binding job, immutable task, input
   bundle, executable, destination policy, opaque-handle digest, true attempted and
   observed values, and its gateway request ID.
5. The runner must derive `external_request_attempted`,
   `external_request_observed`, and `gateway_request_id` only from a gateway receipt
   accepted by the independently configured gateway verifier. Workload stdout,
   provider prose, self-reported JSON and runner inference are never truth sources.

Fail/stop: generic internet route, DNS, metadata reachability, proxy-only enforcement,
unsigned/stale/wrong-key gateway receipt, or any observation copied from workload
output.

## 4. Execute, stop and return

1. Recheck request schema/document, execution attempt, immutable task, complete
   non-secret provider execution identity and its digest, input, policy and executable
   pins before process start. Create the durable job-attempt claim with exclusive
   creation before any credential broker side effect; a repeat or conflicting attempt
   fails without consuming a new handle.
2. Run exactly the reviewed executable and bounded command in the per-job isolate.
   Validate the CloudAgent command shape first; rewrite the executable, result-schema,
   workspace and last-message host paths to the pinned image and `/job/work` paths.
   Seed `/job/work` from the digest-bound input copy. Never mount the source repository
   or forward a host path into the isolate.
3. On success, error, cancellation, leader exit or deadline: stop dispatch, terminate
   the whole cgroup/VM, escalate to forced destruction, and observe that the isolate
   has no tasks. A leader PID exit is not quiescence.
4. Record trusted supervisor maxima for wall/CPU/RSS/PIDs/disk and exact bounded stream
   byte counts. Any missing observation or over-limit value fails closed.
5. Only after quiescence, reject links/special files/over-limit output and create a
   descriptor-bound snapshot in a fresh quarantine. Hash the bytes actually copied,
   recheck the source, seal and rehash that exact quarantine. Do not execute output.
6. Canonicalize Receipt v3 excluding only `receipt_digest_sha256` and
   `attestation_signature`. The signed body must return the exact execution attempt
   and provider execution identity digest from Request v2 in addition to all existing
   task/input/output/policy/runner/executable bindings. Hash it and sign that digest
   with the provisioned runner attestation key. Rehash the sealed quarantine after
   signing and after its atomic workspace transfer; either mismatch fails and restores
   the original workspace. Verify again in the consuming CloudAgent using an
   independently configured verifier/profile.

Fail/stop: unobserved descendant, output before quiescence, stale/wrong binding,
unsigned receipt, signer/verifier sharing unreviewed mutable key configuration, or an
output digest differing after transfer.

## 5. Independent acceptance matrix

Before live enablement, a fresh evaluator must run at least:

- wrong job/attempt/task/provider-identity/input/output/policy/runner/executable/
  destination/handle binding;
- job replay and conflicting replay after success, error, timeout and crash;
- stdout, stderr, CPU, RSS, PID, disk, file-count and wall-time overrun;
- symlink, hard-link, archive/path traversal, rootfs write and host mount probes;
- `HOME`, `CODEX_HOME`, proxy, SSH, cloud env/config and container-socket probes;
- direct DNS, provider, metadata, loopback, private-route and non-gateway egress;
- opaque handle reuse, cross-job redemption, expiry and plaintext-secret probes;
- immediate leader exit, fork, double-fork, `setsid`, FD inheritance and daemonized
  child with all stdio redirected;
- forged workload `observed=true`, absent gateway receipt, wrong gateway key, bad
  runner signature, stale receipt and freshly signed nonquiescence;
- input and output bundle mutation before and after transfer.

The evaluator must retain exact request/receipt/config/image/executable digests,
network/cgroup/namespace observations, PIDs and negative outcomes. Passing the local
synthetic suite is supporting evidence only. Live remains HOLD until this independent
deployment matrix passes and Chief-of-Staff records the accepted profile/trust roots.

## 6. Rollback and incident handling

- Disable queue dispatch first; do not rotate into an unreviewed runner automatically.
- Revoke outstanding opaque handles at the gateway and destroy every active isolate.
- Preserve bounded trusted supervisor and gateway receipts; do not preserve provider
  credentials or unbounded prompts/logs.
- Mark affected runner/profile/image/key/destination digests revoked. A new profile
  requires a new independent acceptance pass.
- Never reinterpret a missing gateway or quiescence receipt as a retryable success.
