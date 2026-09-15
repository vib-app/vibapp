# Local validation record

Status: **implementer regression green; fresh independent review pending; production/live HOLD**  
Date: 2026-08-30 (Asia/Shanghai)

## Executed check

```text
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning \
  -m unittest discover -s artifacts/provider-runner/tests -p 'test_*.py' -v

Ran 28 tests
OK

descriptor-mutation focused serial repeat: Ran 20 tests, OK
```

The serial suite directly covered:

- ProviderRunnerRequest v2 schema/document discriminators, strict execution-attempt
  shape and ordinal agreement, complete non-secret provider execution identity,
  embedded identity digest, request digest and executable byte/version binding;
- semantic equality between this artifact's provider-runner interface schema and the
  current CloudAgent interface schema;
- the current provider-result schema byte pin
  `fe7c40611e940e3a86fa16f63fc45d6b592384d23b8fa5614c6418c44cff75d9`
  plus its schema-version and document-type constants;
- a durable replay claim keyed by `(job_id, execution_attempt)`: a duplicate attempt
  is rejected before broker issuance, while a new valid attempt retries the same
  immutable task without fabricating a NeedSpec revision;
- credential-handle issuance bound to job, attempt, immutable task and provider
  execution identity digest, with success consumption and failure revocation paths;
- ProviderRunnerReceipt v3 returning and signing the exact attempt and provider
  execution identity digest along with task/input/output/policy/runner/executable
  bindings; stale bindings fail even when freshly re-signed by the synthetic key;
- default production preflight rejection without accepted profile, signer, gateway
  verifier and backend;
- unprovisioned, transformed-sentinel, zero-identity and zero-key-ID profiles rejected
  in parsing/preflight while a provisioned synthetic profile passes;
- descriptor-bound input mutation using `threading.Event` synchronization after FD
  binding (no file-size/timing race), request-digest/source drift before backend,
  output mutation during snapshot, and post-signature transfer mutation with original
  workspace restoration;
- post-claim execution-attempt, nested provider-execution-identity and isolation-policy
  mutation rejection before any backend dispatch;
- trusted gateway receipt as the only accepted source of attempted/observed/request-ID
  truth, including untyped self-report and bad-signature rejection;
- exact command model/sandbox/output-schema/output-path checks and rewrite to fixed
  isolate paths;
- unique synthetic isolate/UID/GID allocation, minimal child environment, symlink and
  hard-link rejection, stdout/wall/PID/disk caps, trusted CPU/RSS observations, and
  whole-process-group quiescence after leader exit;
- strict duplicate-aware parsing of all eight authored deployment/schema JSON files.

All provider-runner test backends/signatures/handles were inert synthetic fixtures.
No Docker, container, provider, model, credential, network or deployment action ran.

## Exact implementation identities

```text
598964a4719882b002b5957a7ced5836bdcb820acef9ef3bffb5ceb131923e80  provider_runner.py
3b540c14cc66589017edd321729e9f0b65598a31fd00d28730feb3ecd6d360cf  cloud_agent_adapter.py
c7c5a349924607971fd04f406899238edc38e65c0fc890ef3703a3c68aeaff9f  deployment/runner-policy.json
5637152817a37d53ba94fc6af2406660ece758ac64925c2d2a49ff4f14329c3c  schemas/provider-runner-interface.schema.json
39408a767f334a3f6e32c3dff32f5be5eee12ee841ac69ae4ace09276791c829  tests/test_provider_runner.py
81016fad08d946e91ab1ceb8c8503c6491f28bd6398e35f949394e220cdd1a6b  ../cloud-agent/schemas/provider-runner-interface.schema.json (read-only comparison input)
fe7c40611e940e3a86fa16f63fc45d6b592384d23b8fa5614c6418c44cff75d9  ../cloud-agent/schemas/provider-result.schema.json (read-only pin input)
```

## Unavailable or intentionally unexecuted

- No external one-job VM/container backend was provisioned; `UnavailableBackend`
  remains the production default.
- No image was built or pulled; placeholder image/seccomp pins remain deliberately
  unaccepted.
- No OS-enforced per-job namespace, cgroup, read-only rootfs or gateway network policy
  was exercised on this macOS host.
- No production runner/gateway keys, signer, verifier, credential broker or accepted
  profile exists.
- No Codex/model/provider request, credential access, network access, Docker command,
  compilation, deployment, installation, publication or paid action occurred.
- A general JSON Schema engine was not used. The suite performed strict JSON parsing,
  semantic schema equality and explicit exact-field validators; these checks are not
  an independent deployment acceptance.

This implementer-authored record supports repair handoff only. The artifact remains
experimental and deployment-unprovisioned; it is not a Stage 0 tranche PASS, a
working live provider runner, or production acceptance.
