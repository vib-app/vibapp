# Local validation record

State: **implementer-tested experimental/HOLD artifact; not independent acceptance**.
Validated on macOS ARM64 on 2026-08-30. This is a Stage 0-external product
integration. No Codex execution, model/network request, credential read,
generated-app compilation, external Builder handoff, installation, or publication
occurred; only the product host test target was compiled offline.

## Bounded commands and observed results

```text
python3 -m py_compile artifacts/cloud-agent/cloud_agent.py artifacts/cloud-agent/tests/test_cloud_agent.py
PASS

python3 -W error::ResourceWarning -m unittest discover -s artifacts/cloud-agent/tests -v
Ran 53 tests in 1.186s
OK

python3 -W error::ResourceWarning -m unittest discover -s artifacts/codeagent-adapter/tests -v
Ran 42 tests in 0.515s
OK (skipped=1: reviewed Codex 0.150.1 cask is not installed)

CARGO_BUILD_JOBS=1 CARGO_TARGET_DIR=artifacts/desktop/target cargo test --manifest-path artifacts/desktop/src-tauri/Cargo.toml --locked --offline --bin vibapp-product-bridge cloud_task_preview -- --nocapture
Ran 26 focused producer tests (module is included by two product-bridge paths)
OK

python3 artifacts/cloud-agent/cloud_agent.py schema-validate artifacts/cloud-agent/fixtures/valid-task.json
{"external_request_attempted":false,"external_request_observed":false,"gateway_request_id":null,"job_id":"job-cloud-ui-001","schema_version":"vibapp.cloud-codeagent-task.experimental-v3","status":"schema-valid-task"}

shasum -a 256 artifacts/cloud-agent/schemas/cloud-codeagent-task.schema.json artifacts/cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json
b23cb65c899ee5f533630783407aecd2b16032ad8f2bc941ef401ef153eda6ca  cloud-codeagent-task.schema.json
2f145a5ae581d301ba95f4bce35c9373eb983ead491dee0988388a55b067694d  cloud-codeagent-task.experimental-v2.schema.json
```

The optional `jsonschema` Python package was unavailable. The worker therefore loads
the version-selected authoritative schema through strict JSON parsing and runs a
fail-closed Draft 2020-12 subset validator. It recursively lints before every use;
only `$id`, `$schema`, `title`, `description`, and `$comment` are annotations. Any
unknown assertion/applicator rejects with `schema-validator-unavailable`, rather than
causing a command-wide semantic-validator fallback.

## Final boundary regression coverage

- a synthetic child registers through the inherited capability pipe, self-stops before
  fixture work, receives supervisor acknowledgement, immediately calls `setsid`,
  redirects all standard streams to `/dev/null`, and is killed/observed inactive after
  leader exit; no PPID sampling is used to infer it;
- nested local `$ref`, `additionalProperties=false`, `oneOf`, `anyOf`, numeric/array
  bounds, regex patterns, and `uniqueItems` are exercised in the built-in schema
  validator; an injected unknown nested keyword fails with
  `schema-validator-unavailable`;
- historical task V2 remains byte-for-byte schema-readable with its 900-second wall
  ceiling, but authorization rejects exactly with
  `legacy-provider-identity-unbound`; task V3 accepts wall=1800 while retaining
  CPU<=900 and rejects CPU=901;
- endpoint, adapter/runtime package/version/implementation, executable, and
  non-secret configuration fields are covered by the provider identity digest;
  tampering fails integrity checks, while a recomputed identity still requires a new
  immutable task/consent binding and changes the immutable digest; the locally
  resolved CLI bytes/version must also match that consent-bound runtime before a
  consent can be claimed;
- a retry keeps the same NeedSpec revision/digest, job, and immutable task digest but
  uses a new strict attempt and new single-use consent; mismatched attempt ordinal/ID
  fails before execution;
- runner request V2 carries the complete non-secret identity, and request V2/receipt
  V3 bind the exact execution attempt and provider identity digest plus
  job/task/input/output/runner/policy/executable/truth and quiescence fields into the
  attested digest; stale binding values fail;
- a synthetic self-report of observed egress with a rejecting verifier is not trusted,
  an oversized gateway ID fails, and a validly structured but nonquiescent receipt is
  rejected;
- package intent, target, limits, current revision, instruction/contract policy,
  disclosure set, and TTL mutations fail against the consent boundary;
- atomic single-use consent replay is rejected;
- missing profile platform coverage and conflicting negative constraints are rejected;
- `package.build`, out-of-tree `lib.path`, target-specific dependencies, workspace,
  features, and every non-allowlisted Cargo section are rejected;
- policy v7 pre-seeds exact task-derived `Cargo.toml` bytes before provider execution;
  the fixture provider authors only Rust, while a provider rewrite (including a
  semantically valid extra comment) fails the post-provider byte/digest/metadata gate;
- consent claims record the selected provider's reviewed instruction digest rather
  than a Codex-only constant;
- provider-declared capabilities remain bounded, unique informational metadata; a
  mismatch cannot replace consent-bound `task.target` in the handoff, while malformed
  capability metadata still fails closed;
- fixed-world `required_imports` are validated independently from the unique
  app-specific `required_capabilities` subset; only that subset consumes the NeedSpec
  permission ceiling or conflicts with a forbidden-capability constraint;
- policy v7 binds the wit-bindgen 0.60 owner rule into every reviewed provider prompt;
  a local common-owned `ErrorCode` smoke source passes while any literal
  `guest::ErrorCode` path fails closed before handoff;
- explicit multiline NeedSpecs fail before provider execution because the frozen UI
  contract has no multiline control;
- action requirements fail unless comment/string-stripped Rust contains both a
  rendered `NodeKind::Button` and `LauncherEvent::Action` handling;
- service/hybrid intents fail unless Rust explicitly handles `AppEvent::Service` and
  the task-bound event for each declared trigger (`Start` for `on-enable`/`manual`,
  `Trigger` for `scheduler`);
- reminder requirements fail on a missing alarm upsert, missing `FireOnce` policy,
  missing requested time field/setting, or missing requested pause/resume controls;
- a reviewed symlink-to-pinned-file identity succeeds, then content mutation or
  symlink retargeting fails;
- default live mode fails before claim/workspace when the external isolation runner is
  absent; a custom runner without an accepted identity/verifier also fails before its
  execute method, claim, or workspace;
- empty segments, `.`, `..`, doubled separators, absolute paths, backslashes, trailing
  separators, and symlink escapes are rejected by the source-handoff path boundary;
- dry-run produces source only, removes its workspace, consumes no consent, and records
  attempted=false, observed=false, gateway_request_id=null.

## Held boundaries

- There is no production provider-runner implementation, accepted runner profile, or
  production receipt-signature verifier. The verifier protocol and attestation fields
  are fail-closed placeholders for a separately accepted implementation.
- The provider-runner schema/interface is design input for a separately reviewed
  service; live mode remains unavailable until that service exists.
- The local capability handshake supervises cooperative synthetic fixtures only. It is
  not a kernel isolation primitive and cannot produce an accepted live receipt.
- The fixture source was not compiled or accepted by Builder/verifier.
- Windows/Linux and real VM/container behavior remain unverified.
