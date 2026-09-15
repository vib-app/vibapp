# Stage 0 Conformance-First Implementation Tranches

- Status: Contract independently accepted at `e47f6cec78fe90923bcddde5dd1215fc1afe40de`; project-local skill independently accepted at `531dda060f6641dc05e8b3143bf6374e1ae056a3`; Tranche 1 at `393287f0af8e9c29ad3d8b44be895db0192b70e7` fully independently accepted; Tranche 2 authorized
- Constraint: Tranche 2 only; later tranches remain held until Tranche 2 evidence is independently accepted
- External actions: prohibited except the separately accepted, credential-free Gate 5 dependency-setup requests required by Tranche 2

## Global rules

- Each tranche starts only after the preceding tranche's tests and evidence are accepted.
- Builder output, model-like output and guest output are untrusted until independently verified.
- A builder/implementer may not mark its own foundational gate accepted.
- No live Codex/model call, provider credential, GUI, browser execution, cloud, public Registry, publication or deployment is part of this plan.
- A foundational failure stops later tranches; it is not offset by implementing more features.
- The exhaustive future path allowlist is [README.md](README.md). Path pre-authorization does not override this tranche order or the all-gates acceptance requirement.
- Synthetic replay events are committed only below `fixtures/**/*.jsonl`; operational model/build/runtime JSONL remains local and ignored.

## Post-gate skill bootstrap — VibApp application development

After independent pre-code acceptance and before Tranche 1 source, initialize the pre-authorized `.agents/skills/vibapp/` path with the approved skill-creator workflow. The directory must remain absent while any pre-code gate is failed.

Pass:

- `SKILL.md` routes application discovery, creation, review, validation and packaging to the accepted Stage 0 contracts;
- UI-only, service-only and hybrid examples plus desktop/web-preview/web-runtime/headless applicability rules are explicit;
- Launcher management lifecycle, settings, permissions, state migration, hot update, disable and uninstall data disposition are mandatory;
- CLI/headless applicability is explicit: the same control operations work without a GUI, while UI rendering is reported unsupported when no surface exists;
- Registry compatibility/metadata/explanation requirements and build/test/scan/package gates are mandatory;
- normative schemas/WIT/policies remain single-sourced in versioned references rather than copied inconsistently into prose;
- the skill folder passes the skill-creator validator;
- before deterministic CLI validators exist, the skill labels unavailable automation honestly and cannot claim a package passed.

Fail/stop:

- the skill weakens a gate, permits free-form native Rust plugins, hides web capability gaps, or lets an agent self-certify success;
- validation depends on live provider credentials or an external service.

## Tranche 1 — Protocol, WIT and conformance fixtures

Deliver:

- the root `Cargo.toml` workspace, exact `Cargo.lock`, and only the reviewed `.cargo/config.toml`, `deny.toml` and `supply-chain/{config.toml,audits.toml,imports.lock}` policy metadata required by the accepted toolchain contract;
- Rust protocol representation matching the accepted manifest JSON Schema;
- generated/checked bindings from accepted `experimental-v0` WIT;
- launcher-neutral app/surface/session lifecycle types for list/open/focus/blur/close/restore;
- package kinds and lifecycle for UI-only, service-only and hybrid apps: install/enable/disable/configure/uninstall with explicit data disposition;
- execution profiles for desktop, web-preview, web-runtime and headless, including capability/degradation declarations;
- parser/validator for package metadata and interface/capability reconciliation;
- positive and negative fixtures for current, missing import, unknown required import, permission denial, malformed output, unsupported version and resource-limit declarations.

Pass:

- every new tracked path is inside the exhaustive Stage 0 allowlist, and generated/vendor output stays in an ignored authorized generated path;
- every hand-authored fixture has one deterministic expected result;
- Rust serialization round-trips canonical positive manifests without changing meaning;
- actual component imports must match declared required capabilities;
- UI-only, service-only and hybrid fixtures cannot claim an entrypoint/profile/capability they do not implement, and web-preview limitations are explicit;
- unsupported package/WASI/WIT versions fail before instantiation with typed diagnostics.

Fail/stop:

- generated bindings or WIT tooling interpret the accepted contract inconsistently;
- a record/variant/error cannot evolve according to the RFC rules;
- validator results depend on hash-map ordering, mutable tags or engine-specific public types.

On failure: revise Gate 2 artifacts and repeat independent pre-code acceptance before continuing.

## Tranche 2 — Minimal host and no-privilege golden component

Deliver:

- one Rust-built WASI 0.2 Component with no privileged capability;
- a minimal host that validates and instantiates it in a disposable generation;
- health, event and first-render calls limited to inert declarative data.

Pass:

- the canonical component builds reproducibly under the pinned toolchain;
- the host links only declared interfaces and rejects ambient WASI filesystem/network/environment/process imports;
- the CLI driver can create one host-issued app surface, focus/restore/close it, and cannot route a forged surface/session ID across apps;
- health and render results conform to the accepted size/type limits.

Fail/stop:

- host setup requires granting broad WASI authority;
- package bytes depend on unpinned ambient tools;
- the guest must expose Rust-layout types or raw host/runtime handles.

## Tranche 3 — Limits, cancellation, state revisions and generation rollback

Deliver:

- per-generation Store/resource context;
- memory/table/instance/output/host-call quotas;
- fuel or epoch interruption plus outer timeout and blocked-call cancellation;
- host-owned transactional state revisions;
- shadow generation, migration, health gate, atomic routing, drain and rollback.

Pass:

- loops, memory growth, oversized values/UI trees, host-call flood and traps terminate the generation without corrupting the daemon;
- a blocked asynchronous host call obeys cancellation independently of guest fuel;
- injected failure at every swap step keeps or restores the previous digest/state;
- duplicated events/effects are deduplicated by host idempotency keys.

Fail/stop:

- a timed-out/trapped Store is reused;
- committed data is lost or a notification can be duplicated after failed activation;
- failure leaves neither old nor new generation usable.

On failure: revise the state/event/hot-swap portion of RFC 0001 before breadth.

## Tranche 4 — Alarm host capability and fault matrix

Deliver:

- host-owned clock/scheduler/notification abstractions matching Gate 4;
- golden alarm component/domain behavior;
- deterministic/fake clock and fault injection;
- a current-host adapter smoke where safe and non-credentialed.

Pass:

- every machine-readable Gate 4 case produces the expected persisted schedule/state, event ID, notification behavior and rollback result;
- GUI absence is simulated and has no effect on an accepted host schedule;
- close, disable and uninstall-with-retain/delete follow distinct accepted transitions; no scheduler lease survives disable/uninstall incorrectly;
- daemon restart, sleep/resume, DST gap/fold, time-zone/clock changes, duplicates, missed events, denial/failure and upgrade faults match the accepted semantics.

Fail/stop:

- behavior depends on a continuously running component or GUI timer;
- a case requires an undocumented platform promise;
- product semantics are changed in code rather than in Gate 4 artifacts.

## Tranche 5 — Deterministic Registry search and explanations

Deliver:

- typed `NeedSpec`, immutable candidate metadata and `CandidateMatch` evidence;
- deterministic in-memory lexical/vector fixture inputs;
- hard filters for package/WASI/WIT/platform/profile/permissions/trust/revocation;
- coverage and explanation fields that point to fixture facts.

Pass:

- no score can reintroduce a hard-filtered candidate;
- must-have conflicts can produce an empty result;
- every explanation capability/security/compatibility statement cites a candidate field or verification fixture;
- rejection feedback becomes a typed negative constraint and deterministically changes the next result.

Fail/stop:

- nearest-neighbor behavior always recommends something;
- an LLM/free-text explanation is used as the source of truth;
- stable input produces unstable ordering.

Quantitative production retrieval metrics are out of Stage 0 and require a labeled corpus later.

## Tranche 6 — Replay-only agent adapter and isolated builder fixture

Deliver:

- typed provider request/event/result contract;
- committed synthetic `fixtures/replay/**/*.jsonl` inputs resembling Codex execution progress and final structured output, with no real conversation, account or credential material;
- replay parser with cancellation/failure states;
- deterministic isolated builder and separate verifier fixture;
- canary evidence for home, Docker socket, credential, network and artifact-promotion boundaries.

Pass:

- every replay JSONL is visible to ordinary Git tracking without `git add -f`, while representative `.chief-of-staff/`, `logs/`, `artifacts/` and `tmp/` JSONL remains ignored;
- replay never launches a provider process or reads ambient provider configuration;
- agent success text cannot bypass compile/test/verifier results;
- builder cannot access the host home, Docker socket, network or canary credential;
- only verifier success can promote an exact quarantined digest;
- failure/cancel/timeout is durable and diagnostic.

Fail/stop:

- a live model/provider invocation or credential becomes necessary;
- generated/build dependency code can access an excluded boundary;
- builder can mark its own artifact verified or published.

Live Codex integration, cloud workers and publication require separate user authority and acceptance.

## Post-Stage-0 POC — vibapp.ai Web preview

This item is recorded but not authorized as Stage 0 source. After Stage 0 acceptance, a separate decision may authorize:

- trusted derivation of the canonical component into a browser Worker artifact;
- isolated preview origin/CSP/message broker;
- host-rendered preview UI with explicit real/mock/unavailable capability labels;
- weather-style web-safe execution and simulated preview of a temperature-reporting service;
- proof that preview cannot install, enable, obtain local hardware/background authority or access another preview/user state.

The project-local `.agents/skills/vibapp` must consume the accepted manifest/WIT/profile/security artifacts and block packaging when compatibility or applicability validation fails.

## Later product tranche — Linux/headless CLI

After desktop Launcher and Web preview priorities, a separate product tranche may turn the Stage 0 driver seam into `vibapp` CLI for Raspberry Pi and Ubuntu Server. It must reuse daemon semantics, authenticate over a local socket, sanitize terminal/log output, support stable `--json`, manage every package kind, fully operate headless services, and report UI rendering as unavailable rather than emulating it.

## Independent evidence owner

After the pre-code packet is complete, a fresh agent that did not author Gates 1–6 will check the repository, parse schemas/WIT/fixtures, verify pins and confirm `git status`/scope. After each implementation tranche, a fresh evaluator reads tests, logs and produced artifacts rather than accepting an implementer's summary.

The Chief-of-Staff merge owner records terminal gate/tranche state and preserves the thick acceptance report in the run inbox.

## Stage 0 completion

Stage 0 is complete only when all tranche pass statements have direct evidence and an independent final evaluator accepts the current-host alarm proof. Completion does not authorize GUI/web/cloud/publication work automatically; it activates the next product/architecture decision.
