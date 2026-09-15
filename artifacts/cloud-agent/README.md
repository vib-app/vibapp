# VibApp cloud CodeAgent source worker

Status: **experimental/HOLD**. This ignored artifact is outside the accepted Stage 0
Tranche 2 boundary. It is not a deployed cloud service, and this repair made no model
or provider request.

The worker has one narrow authority: convert a schema-valid, complete NeedSpec into
untrusted Rust/WASI source for a separate Builder. The task and consent contract
natively recognizes Codex, Claude Code, OpenCode and Gemini CLI. It binds the exact
provider/model, reviewed instruction digest, canonical endpoint identity, adapter
implementation/version, runtime package/executable, and a digest of non-secret
provider configuration. It never serializes a credential or secret configuration
value. It never compiles, tests, fetches, verifies, installs, signs, publishes,
deploys, or automatically hands source to the Builder.

```text
NeedSpec + package intent + target + limits + fixed policy/contract/instructions
        + explicit non-secret provider execution identity
        -> one immutable provider-request digest
        -> one execution attempt (separate from the immutable request)
        -> granted, current, one-hour, single-use consent for that digest + attempt
        -> task-derived, digest-bound Cargo.toml policy scaffold
        -> independently isolated provider runner (currently unavailable)
        -> fixed provider-result schema + signed, digest-bound quiescence receipt
        -> closed Cargo/source audit
        -> NeedSpec-to-source semantic acceptance gate
        -> source-only handoff awaiting the separate Builder
```

## Current operating modes

`schema-validate` loads the version-selected authoritative JSON Schema, recursively
rejects any unsupported assertion/applicator keyword, validates the instance, and
then applies the product semantic/integrity rules without authorizing or consuming a
consent. This built-in closed Draft 2020-12 subset is used because the optional
third-party `jsonschema` package is not a runtime dependency; unknown keywords fail
with `schema-validator-unavailable` rather than silently falling back.

`validate` additionally checks the current NeedSpec revision, complete profile
coverage, negative constraints, permission ceiling, immutable digest, and exact
consent. Consent timestamps are real security data, so the dated fixture eventually
expires by design.

The target distinguishes `required_imports` (the selected world's exact structural
link set) from `required_capabilities` (the consent-bound must-have authority subset).
Permission ceilings and forbidden-capability checks apply to the latter. A forbidden
or unavailable structural-only import can therefore remain linked as a typed stub
without fabricating application authority.

The current persisted contract is
`vibapp.cloud-codeagent-task.experimental-v3`; its immutable provider request and
consent policy are both `experimental-v7`. V3 raises only the wall-time ceiling from
900 to 1800 seconds; the CPU ceiling remains 900 seconds. The historical V2 schema is
preserved byte-for-byte and can still be schema-validated, but V2 consent/execution
fails closed with `legacy-provider-identity-unbound` because its provider execution
identity was not bound. Historical V1 records predate mandatory
`target.required_capabilities` and remain unsupported; no historical task is silently
upgraded or reinterpreted.

`execution_attempt` is mutable lifecycle state, not part of the immutable provider
request. A transient failure therefore creates a new strict
`attempt-NNNN-<16-lowercase-hex>` identity and new single-use consent while retaining
the same NeedSpec revision, job ID, and immutable task digest. Product retries must
never manufacture a NeedSpec revision just to satisfy the execution state machine.

`dry-run` pre-seeds the same task-derived Cargo/WIT inputs used by live execution,
copies only the checked-in Rust fixture source into a fresh temporary workspace,
audits it, creates a source handoff, consumes no consent, and reports both external
request truth fields as false. The fixture provider does not author `Cargo.toml`.

`run` is **fail closed in this artifact**. Its production runner interface remains
Codex-specific; the separate local CodeAgent adapter implements all four task
providers with provider-specific CLI controls. The default cloud runner is
unavailable, so even
with all three CLI confirmations it rejects before claiming consent or creating a job
workspace. A future production adapter must implement the provider-runner interface,
match an independently accepted runner identity/policy digest, and return a verified
detached receipt; there is no CLI switch that bypasses this requirement. This artifact
ships neither an accepted runner profile nor a production signature verifier.

```sh
python3 -m unittest discover -s artifacts/cloud-agent/tests -v

python3 artifacts/cloud-agent/cloud_agent.py dry-run \
  artifacts/cloud-agent/fixtures/valid-task.json \
  --output-root "$(mktemp -d)"
```

## Consent and identity boundary

- The provider-visible object contains the NeedSpec/current revision, package intent,
  target, limits, provider, mandatory exact model, fixed policy version, authoritative contract digest, and
  fixed instruction digest. It also contains the deterministic Cargo scaffold policy,
  task-derived package name/version, path, exact SHA-256, and the complete non-secret
  provider execution identity. Mutable attempt/consent/claim state is excluded.
- The identity digest covers every public identity field: endpoint kind/canonical
  endpoint/endpoint digest, adapter ID/version/implementation digest, runtime package
  ID/version/executable digest, and non-secret configuration digest. `provider-managed`
  is accepted only for providers whose adapter forbids custom endpoints; OpenCode
  requires an explicit canonical HTTPS endpoint. The adapter must separately preflight
  the actual CLI configuration and reject any redirect away from the consented value.
- Consent must disclose all seven transmitted data classes, match the complete
  immutable digest and provider identity digest, bind the exact execution attempt, be
  issued already, expire after issue within one hour, and be atomically claimed once
  with `O_EXCL` immediately before a future runner call.
- `/opt/homebrew/bin/codex` is allowed to be the reviewed Homebrew symlink. The worker
  resolves the bounded chain, requires the pinned `0.149.1` final path, owner/mode,
  and SHA-256, then requires the observed CLI version and bytes to match the
  consent-bound runtime identity before consent can be claimed. It passes the resolved
  path and requires a runner receipt for the digest actually executed.

## Required external provider runner

The interface is defined by
`schemas/provider-runner-interface.schema.json`. A production implementation must
provide a one-job VM/container that owns the entire job, with a read-only base,
non-root user, no inherited
environment or host home/repository/SSH/cloud credentials/container socket, brokered
credentials only, provider-gateway-only network, CPU/RSS/PID/disk ceilings, bounded
streams, and whole-job kill plus quiescence proof. Its request V2 and receipt V3 bind
the exact execution attempt and provider execution identity digest in addition to job
ID, immutable task, input/output bundles, runner identity, isolation policy,
executable, request truth fields, gateway ID, and quiescence. The request also carries
the complete non-secret identity so the trusted runner can compare actual CLI/runtime
configuration rather than merely echoing a digest. Runtime revalidates every field
and fails closed on an absent/unaccepted detached attestation. Workspace input/output
is copied as digest-bound bundles; the repository is never mounted.

`codex exec --ephemeral -s workspace-write` remains in the inner command for behavior
control. It is explicitly **not** treated as the host isolation boundary.

The local process supervisor exists only for cooperative synthetic regression tests;
it is not a security or live-execution boundary and cannot issue an accepted receipt.
It passes an inherited capability pipe and nonce. A fixture child must register its
PID, self-stop before fixture work, and wait for the supervisor's `SIGCONT`
acknowledgement. The supervisor records PID/UID/start-time handles before continuing
the child, then kills and re-observes the original process group plus every registered
handle after leader exit. It does not infer descendants from PPID snapshots, so an
immediate `setsid` plus `/dev/null` redirection cannot escape once registered.

## Source boundary

Before provider execution, policy v7 derives the Cargo package name solely from
`package_intent.app_id`, takes the version solely from `package_intent.version`, writes
the exact closed `Cargo.toml`, and marks it `0444`. The permission bits reduce
accidental edits but are not a security boundary: after provider quiescence the audit
requires a regular single-link `0444` file and recomputes both exact bytes and SHA-256
from the immutable task. A provider rewrite, replacement, chmod, removal, or even a
policy-valid extra comment fails closed. `provider-result.source_files` remains the
complete inventory and therefore still lists the worker-authored `Cargo.toml`.

The source audit permits only `Cargo.toml`, `README.md`, `manifest.intent.json`, the
byte-identical WIT copy, and Rust below `src/` or `tests/`. Cargo accepts exactly
`[package]`, `[lib]`, and `[dependencies]`: edition 2024, `publish=false`, all automatic
targets disabled, `cdylib`, and the fixed `wit-bindgen =0.60.0` feature set. Build
scripts, `lib.path`, workspaces, target-specific/dev/build dependencies, extra
sections, locks, executable/native/archive files, links, and out-of-tree paths fail
closed. Every reported and copied source path must be canonical slash-separated
segments: no empty, `.`, `..`, doubled separator, absolute path, backslash, or segment
outside the source root after resolution.

Before a live source handoff is persisted, the same audit compares positive
must-have NeedSpec prose with comment/string-stripped Rust tokens. It fails closed
with stable diagnostics when an interactive requirement lacks both a rendered
`Button` and `LauncherEvent::Action` handler, a service/hybrid package lacks explicit
`AppEvent::Service` lifecycle handling for its task-bound triggers (`Start` for
`on-enable`/`manual`, `Trigger` for `scheduler`), or a reminder lacks an alarm upsert,
`MissedPolicy::FireOnce`, requested time configuration, or requested pause/resume
controls. A multiline editing requirement is rejected during task validation because
the frozen `experimental-v0` `field-kind` has no multiline control; no provider call
is needed to discover that incompatibility.

The stable rejection codes are `semantic-contract-unsupported`,
`semantic-reminder-contract-mismatch`, `semantic-ui-action-unreachable`,
`semantic-reminder-policy-invalid`, `semantic-reminder-controls-missing`, and
`semantic-service-unimplemented`. These checks prove only deterministic structural
evidence at the source boundary. Builder/Verifier checks and real functional runtime
acceptance remain separate and mandatory.
