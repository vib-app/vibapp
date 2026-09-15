# ADR 0001: Stage 0 System Boundary

- Status: Accepted for pre-code review
- Date: 2026-08-11
- Decision: Run one unpublished, local, conformance-first contract spike

## Context

VibApp ultimately needs a desktop GUI, vibapp.ai, a local daemon, Registry retrieval and local/cloud code-agent development. The highest-risk dependency shared by every one of those surfaces is the app package and host capability contract. Stage 0 exists only to falsify or validate that boundary before product breadth creates reliance on it.

## Decision

The eventual desktop GUI is the launcher/shell and application manager for every installed VibApp. AppStore/search/chat/build are system surfaces inside that Launcher; an installed UI or hybrid module opens in a host-rendered app surface. Packages may be UI-only, headless services or hybrid. The Launcher manages install/uninstall, enable/disable, settings, permissions, status, logs and updates, while the daemon is the lifecycle authority. A lower-priority `vibapp` CLI will expose the same daemon/control semantics for Raspberry Pi, Ubuntu and headless Linux; it can manage UI apps but only render packages with an available UI surface. Stage 0 proves this surface-neutral lifecycle contract with a CLI test driver, but deliberately does not implement either production client.

Stage 0 proves one bounded vertical behavior on the current macOS ARM64 development host:

1. parse and validate an experimental package manifest and WebAssembly Component contract;
2. load a Rust `wasm32-wasip2` golden component through a local experimental host;
3. deny all undeclared imports and enforce bounded resources;
4. launch, focus, restore and close a host-issued alarm app surface and render its minimal declarative view data;
5. persist host-owned alarm schedules and app state revisions;
6. stage a new generation, migrate copied state, health-check, atomically activate and roll back under injected faults;
7. execute deterministic Registry fixtures where compatibility and permissions are hard filters and explanations cite fixture fields;
8. replay synthetic Codex-style events through a typed adapter without invoking a model or reading provider credentials;
9. build only inside a deterministic local isolation fixture and quarantine output until an independent verifier succeeds.

The contracts also describe `desktop`, `web-preview`, `web-runtime` and `headless` profiles. Actual browser execution is a post-Stage-0 POC, but Stage 0 fixtures must prevent an unavailable desktop/background/hardware capability from being advertised as real in a web preview.

The candidate contract is named `experimental-v0`. It is not a published ABI, SDK or compatibility promise.

## In-scope artifacts after pre-code acceptance

- protocol/manifest types and exact experimental WIT bindings;
- positive, negative and adversarial conformance fixtures;
- minimal local runtime/daemon test driver;
- golden alarm domain/component fixture;
- host-owned transactional state, schedule abstraction and fault injection;
- deterministic in-memory Registry fixture;
- replay-only agent provider adapter;
- local deterministic builder/verifier isolation fixture;
- tests, traces and acceptance evidence for those items.

## Explicit non-goals

Stage 0 does not include:

- production Launcher/GUI work, visual polish, tray/deep-link packaging or Tauri/Leptos source;
- production CLI packaging, terminal UX or remote administration;
- browser component execution, Service Worker/Web Push or desktop/browser parity;
- public or production Registry, vector database, account system or multi-user service;
- cloud workers, production signing, publisher identity, moderation or publication;
- live Codex/Claude Code/OpenCode/model calls, provider login, credential reads or token consumption;
- arbitrary external packages, repositories, commands or user data;
- deployment to `vibapp.ai`, DNS changes, production infrastructure or paid services;
- automatic sharing, licensing defaults, pricing, trademark or public “App Store” promises;
- a macOS/Windows/Linux support claim; non-current platforms remain later matrix entries.

## Trust and data ownership

```text
Synthetic fixtures / CLI test driver (untrusted input)
                  |
                  v
         local daemon boundary (trusted policy/state owner)
          |         |          |           |
          v         v          v           v
 component host   scheduler    KV       verifier
 (one Store per   (host-owned) (revisioned) (only promotion authority)
  generation)
          |
          v
 experimental alarm component (untrusted guest)

Replay adapter -> synthetic events only; no process/provider credentials
Builder fixture -> isolated workspace -> quarantined artifact -> verifier
Fixture Registry -> immutable test metadata; no remote content
```

Ownership rules:

- The daemon owns install state, permission grants, current generation, schedule definitions, event deduplication and committed state revisions.
- The daemon owns package enable/disable, service triggers/health, settings validation, uninstall data disposition and revocation of schedules/effects.
- The future Launcher owns trusted navigation/chrome and host-issued app surface/session IDs; guests cannot address other apps or impersonate system permissions/publisher state.
- The future CLI is a separate authenticated local principal with stable human/JSON output; it never invents GUI support or bypasses daemon permission and lifecycle checks.
- A component owns only its versioned domain data within the app namespace and its bounded response to an event.
- The scheduler never trusts a guest timer loop; it persists and triggers host events.
- The Registry fixture owns candidate facts; generated explanation text cannot create new facts.
- The replay adapter owns no product state and has no authority to build, sign, publish or install.
- The builder owns a disposable workspace only. The verifier alone may move a digest from quarantine to candidate-ready.

## Security boundary

The experimental guest begins with no filesystem, network, environment, socket, process, raw OS handle, credential or cross-app state authority. Host capabilities are named, scoped, quota-bound and checked again on every call. Wasm is not treated as the only boundary: the runtime service, builder isolation and verifier separation are independently tested.

## UI boundary

Stage 0 tests the launcher-neutral lifecycle and semantic controls needed by a hybrid alarm: install/enable/disable/configure/uninstall plus open/focus/blur/close/restore; text/headings, labelled settings, date/time/time-zone/recurrence choices, buttons, list rows, validation/status and confirmation. Surface close does not cancel host-owned alarms or disable the package. Disable stops service triggers/effects while retaining package/state. Uninstall requires an explicit retain/export/delete data disposition. If the golden module needs raw DOM, arbitrary HTML/JavaScript, native window APIs or framework objects, Stage 0 stops and the portability-versus-UI-freedom choice is escalated.

A service-only fixture models periodic temperature reporting through brokered `system-metrics`, scheduler and destination-scoped HTTP capabilities. It exists to test manifest/profile/permission semantics only; Stage 0 does not read or transmit real device temperature.

The future reversible Launcher-stack recommendation is Tauri v2 plus stable Leptos 0.8, but this ADR does not authorize or freeze that production UI choice.

## Stop or revise conditions

Stop breadth and revise the contract when any of these occurs:

1. required WIT changes cannot be expressed without a breaking ambiguity or engine-specific public type;
2. the alarm cannot be expressed by the bounded declarative UI without an escape hatch;
3. undeclared imports, cross-app namespaces or denied capabilities remain reachable;
4. a loop, blocked host call, output bomb or memory growth cannot be reliably stopped without killing/corrupting the daemon;
5. state migration or activation failure can lose committed data, duplicate a notification or leave no usable previous generation;
6. alarm semantics remain ambiguous for DST, sleep/resume, restart, duplicate delivery or clock correction;
7. the builder can see the host home, Docker socket, network, canary credential or mutable shared cache;
8. a similarity score can bypass ABI/platform/permission/trust constraints or an explanation can cite an absent fact;
9. UI-close, disable and uninstall semantics can leave orphan service/scheduler/network authority or silently delete data;
10. a `web-preview` can access or claim unavailable local hardware/background authority or be mistaken for installation;
11. any implementation step would require a live provider credential, external package/user data, deployment, publication or spending.

Failures in items 1–6 require revising RFC 0001 before any GUI, Registry or agent breadth. A failed spike is an acceptable outcome; silently weakening a gate is not.

## Escalation points

The user must decide before any of these become critical-path actions:

- declarative UI portability versus arbitrary UI freedom;
- public supported platforms, browser meaning and ABI support lifetime;
- live provider/account connection and what “local development” discloses;
- remote-processing consent, automatic/public sharing policy and default license;
- public Registry trust root, publisher verification, revocation and moderation;
- hosted compiler isolation/cost model and any “free” claim;
- deployment/domain access, public brand wording and trademark review.

These decisions are intentionally not requested during Stage 0 because no current pre-code or local conformance task depends on them.

## Consequences

Positive:

- the highest-blast-radius interfaces become falsifiable before product code relies on them;
- a failed experiment is cheap and recoverable from the clean Git baseline;
- the GUI/web and code-agent layers can later consume a measured contract rather than define it accidentally.

Costs:

- the first implementation milestone does not yet look like the requested end-user AppStore;
- UI, browser, cloud and multi-platform work wait for the runtime boundary to pass;
- exact alarm semantics and adversarial fixtures add up-front work.

## Acceptance

This ADR passes Gate 1 only when the Stage 0 index links it, all non-goals remain excluded from the tranche plan, and an independent reviewer confirms that no “later in Stage 0” clause reintroduces product breadth.
