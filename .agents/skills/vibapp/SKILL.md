---
name: vibapp
description: Build, port, review, validate, or package VibApp applications against this repository's accepted Stage 0 contracts. Use for VibApp manifests, Rust-authored WASI 0.2 Components, experimental WIT, UI-only/service-only/hybrid app design, desktop/web-preview/web-runtime/headless applicability, Launcher or CLI lifecycle, permissions, settings, state migration, hot update, alarm behavior, Registry metadata/search explanations, conformance fixtures, deterministic builds, or package-readiness decisions.
---

# VibApp application development

Treat the repository contracts as the source of truth. Do not restate their complete type or policy definitions in this skill, generated docs, or implementation comments.

For application UI/interaction authoring or review, also read the product's
[VibApp UI/UX skill](../../../artifacts/cloud-agent/skills/vibapp-ui-ux/SKILL.md).
Its experience guidance complements, and does not replace, the exact contracts.

## Load the contract

Work from the Git repository root. Read these files before changing a VibApp artifact:

1. `docs/stage0/README.md` for gate status, allowed paths, and current authority.
2. `docs/stage0/implementation-tranches.md` for ordering and the current tranche's deliver/pass/fail-stop rules.
3. `docs/stage0/adr-0001-system-boundary.md` for product, trust, and non-goal boundaries.
4. `docs/rfcs/0001-runtime-abi-v1.md` and `wit/experimental-v0/contract.wit` for package, compatibility, lifecycle, capability, UI, and daemon-control semantics.

Load additional canonical artifacts when relevant:

- Manifest or compatibility work: `schemas/manifest.experimental-v0.schema.json`, `fixtures/conformance/experimental-v0/manifests/`, and `fixtures/conformance/experimental-v0/vectors.json`.
- Alarm or scheduler work: `docs/stage0/alarm-semantics.md`, `fixtures/alarm/cases.schema.json`, and `fixtures/alarm/cases.json`.
- Runtime, security, isolation, or builder work: `docs/stage0/threat-model.md` and `fixtures/adversarial/README.md`.
- Toolchain, dependencies, or packaging work: `rust-toolchain.toml`, `config/contract-toolchain.toml`, and `docs/stage0/toolchain.md`.
- Source provenance or new paths: `docs/stage0/provenance.md`.

If two normative artifacts disagree, stop implementation, identify the exact conflicting fields or rules, and route the issue back through the relevant gate. Never choose a convenient interpretation in code.

## Route the request

Classify the work before editing:

- Discovery: extract the user's complete requirements, negative constraints, profiles, permissions, and trust limits before searching.
- Create or port: select the app kind, profiles, WIT world, entrypoints, capabilities, state, and package evidence before writing guest logic.
- Review: compare the artifact directly with schemas, WIT, vectors, security controls, and tranche gates.
- Validate: run only deterministic checks that actually exist and report unavailable automation explicitly.
- Package: require exact artifact relationships, hashes, provenance, SBOM, policy evidence, quarantine, and independent verification; packaging is not publication.

Keep work inside the current accepted tranche. Path pre-authorization does not authorize a later tranche, GUI, browser runtime, public Registry, live provider, cloud worker, deployment, signing root, or publication.

## Choose app kind and profiles

Choose one manifest app kind from the user's actual behavior:

- `ui`: host-rendered Launcher surfaces and settings; no background service entrypoint.
- `service`: daemon-managed event-driven work; no renderable UI surface. Settings may still be managed by Launcher or CLI.
- `hybrid`: both host-rendered UI and daemon-managed service entrypoints, with lifecycle kept distinct.

Use the accepted valid manifests as concrete, non-implementation examples:

- `fixtures/conformance/experimental-v0/manifests/ui-only.valid.json` models a Launcher-only UI with settings and no background service.
- `fixtures/conformance/experimental-v0/manifests/service-only.valid.json` models a headless temperature-reporting service with settings and no UI surface.
- `fixtures/conformance/experimental-v0/manifests/hybrid.valid.json` models an alarm editor plus a daemon-owned alarm service whose schedule survives view closure.

These are contract fixtures, not working clients. Read their selected WIT worlds and exact fields from the files instead of copying the examples into a new definition.

Use the manifest schema and WIT worlds to determine exact fields and imports. Do not infer compatibility from a successful Rust compile.

Declare every applicable profile explicitly:

- `desktop`: local daemon plus available Launcher surfaces.
- `headless`: daemon/CLI management with no UI renderer; report UI launch as unsupported.
- `web-preview`: isolated, foreground-only preview using the exact per-profile availability enum: `native`, `brokered`, `mock`, `denied`, or `unavailable`. Background, hardware, scheduler, notification, system-metrics, and network authority in preview is only `mock`, `denied`, or `unavailable`, never live native or brokered authority.
- `web-runtime`: browser-hosted foreground behavior only when the accepted manifest/profile permits it.

Never claim browser-closed alarms, durable browser background work, local hardware access, native desktop authority, or installation from a preview. A profile may degrade only as declared. A required capability that is `denied` or `unavailable` blocks that profile's activation; a degradable import remains linked only through its declared typed stub behavior.

## Design against the host boundary

Author guest code as a Rust `wasm32-wasip2` Component using the exact accepted WIT. Never load community code as a native `dylib` or `cdylib` in the Launcher or daemon.

Preserve these ownership rules:

- The daemon owns installation, enablement, grants, typed settings, service lifecycle, schedules, notifications, state revisions, generation routing, updates, rollback, and uninstall data disposition.
- Launcher and CLI are authenticated control surfaces over the same typed daemon envelope. They do not duplicate lifecycle or scheduling logic.
- A UI surface owns only ephemeral presentation state. Closing, hiding, or quitting a view does not disable a hybrid app or cancel a host-owned schedule.
- A guest owns only its bounded app-domain response and app-scoped transactional state. It receives no ambient filesystem, network, environment, process, credential, device, or cross-app authority.
- Browser preview is a separate simulated execution state, never installation or permission grant.

Use host-rendered semantic UI types only. Do not introduce DOM objects, raw HTML or script, framework-native widgets, native window handles, Rust-layout types, pointers, descriptors, or executable markup across WIT.

Keep secrets in trusted host storage. Sensitive settings carry only opaque `secret-handle` values; never expose plaintext through guest state, daemon JSON, CLI arguments/environment, logs, fixtures, or generated reports.

## Define manifest and capability applicability

Start with the strict manifest and make these relationships exact:

1. Package format, contract version, WIT world, WASI version, app kind, profiles, platforms, and entrypoints.
2. `runtime.required_imports`, declared capability interfaces, and actual Component imports.
3. Capability necessity, grant mode, scope, per-profile availability, and degradation behavior.
4. Canonical component, assets, trusted browser derivations, provenance, SBOM, and digest relationships.
5. Resource limits, state schema/migration range, lifecycle behavior, privacy, source, license, and verification metadata.

Reject missing imports, unknown required imports, wrong-direction reconciliation, undeclared actual imports, capability expansion without new consent, unsupported versions, invalid app-kind/entrypoint combinations, unbounded resources, or a profile claim unsupported by its artifacts.

## Preserve lifecycle and update semantics

Keep install, enable, disable, configure, launch/surface operations, service start/stop, update, and uninstall as distinct typed transitions.

- Settings `configure` is not an app-specific domain command such as `alarm-configure`.
- Disable stops services, schedules, in-flight effects, grants, and leases as defined while retaining package/state.
- Uninstall first deactivates authority, then applies the explicit retain/export/delete data disposition.
- Closing a UI surface affects presentation only.
- Update stages an immutable generation, migrates copied state, runs health and compatibility gates, atomically switches routing, drains the old generation, and rolls back without rewinding host-owned ledgers.
- Replayed mutating commands use the accepted idempotency keys and return the original semantic result; conflicting payload reuse fails closed.

For alarm work, use the full accepted alarm document and 45-case matrix. Do not replace host scheduling with guest timers, GUI timers, or browser timers.

## Build Registry metadata and explanations

Apply hard filters before lexical/vector scoring:

- package/contract/WASI/WIT compatibility;
- platform and execution profile;
- required capabilities and availability;
- permission ceiling and negative constraints;
- trust, source, verification, revocation, and publication state.

Never let a similarity score reintroduce a rejected candidate. Match explanations may cite only actual manifest, candidate, or verification facts. Return requirement coverage, gaps, permissions, compatibility, source, digest, and evidence; allow an empty result when must-have constraints conflict.

## Validate honestly

Run checks in the current tranche's contract order. At minimum, when the relevant automation exists:

1. Parse all authored JSON/TOML with duplicate-key rejection where applicable.
2. Validate manifests and fixture instances against their strict schemas.
3. Parse the exact WIT world and compare actual imports with manifest imports/capabilities.
4. Run positive, negative, adversarial, lifecycle, profile, and alarm fixtures selected by the artifact's applicability.
5. Check deterministic ordering, idempotency, bounds, cancellation, state revision, generation, and rollback evidence.
6. Verify tracked paths and ensure generated output stays only in authorized ignored locations.
7. Use the exact toolchain, lock, offline build policy, dependency/security policies, quarantine, and separate verifier required by Gate 5.

Do not execute the Gate 5 Rustup observation sequence piecemeal. It requires caller-supplied absolute paths, `RUSTUP_AUTO_INSTALL=0`, the fully qualified installed toolchain, and a verifier-owned before/after state snapshot. Do not use bare repository Rust/Cargo proxies as a read-only probe.

Before deterministic project validators, bindings, the pinned tool layer, or a tranche harness exist, say exactly which checks are unavailable. Never turn schema validity, a successful compile, generated success prose, or an implementer's own report into package acceptance.

Independent verification is mandatory wherever the tranche contract assigns it. A builder or implementing agent cannot promote or self-certify its own output.

## Package without publishing

Package only after every applicable check passes. Keep the canonical component immutable, bind derivations and evidence to its digest, and quarantine output until a separate verifier succeeds.

Do not sign, publish, share, install, deploy, call a live provider, read credentials, spend resources, or change DNS unless the user separately authorizes that external action and the applicable later gate permits it. Remote processing consent and public sharing consent are separate decisions.

## Report the result

Return a compact evidence-backed handoff containing:

- selected app kind, profiles, world, entrypoints, capabilities, and degradation behavior;
- files changed and the tranche/path authority used;
- commands run and exact pass/fail evidence;
- checks that remain unavailable or unexecuted;
- blockers, failed gates, and the smallest next repair;
- whether the artifact is only authored, locally validated, independently accepted, packaged, installed, or published.

Use precise state labels. Never describe a contract, fixture, mock, preview, or quarantined candidate as a working client, production service, installed app, or published package.
