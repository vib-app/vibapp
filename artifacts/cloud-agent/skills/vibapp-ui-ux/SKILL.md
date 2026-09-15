---
name: vibapp-ui-ux
description: Plan, author and review usable VibApp application interfaces and interactions within the supplied semantic UI and runtime contracts. Use for generated UI or hybrid apps; for service-only apps, apply capability honesty and lifecycle checks without inventing a GUI.
---

# VibApp application experience

Deliver a usable application, not a successful compile or a static demonstration.
Preserve the user's chosen purpose, language, visual direction and behavior. This
skill is passive, trusted development guidance. It grants no tools, network,
publication, permissions, or authority beyond the current task.

## Plan before writing

Record a short application-experience plan in the allowed `source/README.md`:

- Main user goal and the shortest complete interaction that achieves it.
- First screen, primary action, important empty/error/success states.
- Preferred window shape and why; what stays visible at a compact width and what
  scrolls. This is design intent, not an extra manifest field or a size guarantee.
- Concrete acceptance examples: inputs, actions, expected outputs; include a
  relevant invalid input, reopen/persistence check and timed behavior if present.

Keep this proportional: a calculator needs a readable result, keypad and correction
controls, not onboarding, dashboards or elaborate navigation. A service-only app
needs lifecycle and observability, not a decorative fake interface.

## Work with the actual platform

The supplied WIT, manifest schema, permission ceiling and Rust support guide are
authoritative. Read their relevant signatures; do not invent UI variants, layout
fields, timers, notification APIs, manifest extensions or allocator behavior.
The Component supplies semantic nodes; the Client owns pixels, layout and native
windows. Do not generate HTML/CSS/JavaScript or custom title bars in the Component.
Do not duplicate the launcher's identity header, package digest or AppStore badges
inside the application unless the user actually needs an application-info screen.

Use supplied immutable Rust support where available. Do not replace its allocator,
ABI glue or contract to make an application compile. Other task worlds must use
their exact supplied contract; UI-only scaffolding is not a hybrid implementation.

## Make the first screen usable

- Show the app's useful content immediately. Keep decoration and explanations
  secondary. Use a restrained hierarchy and consistent action labels; the host
  renders styles, so do not promise unsupported colors, icons or typography.
- Group related buttons with semantic containers. With the current host, direct
  sibling buttons can form a responsive row/grid; mixed non-button content spans
  the group. For a keypad, separate ordered rows rather than a flat vertical list.
  Preserve a sensible source order for keyboard navigation and narrow layouts.
- Keep primary controls close to the result they affect. A clock must show the
  time before optional instructions; a calculator must expose the keypad and
  clear/correct/equals controls, not just a result and one key above the fold.
- Give every field a visible label and appropriate supported type. Keep choices
  bounded, distinguish value from placeholder, and retain drafts after validation
  failures. Support newlines where the requirement calls for notes or lists.
- Node IDs and button action IDs must each be unique across every emitted surface,
  including toolbar, empty state, dialogs and populated state. Repeated business
  operations still need distinct semantic action IDs mapped to the same handler.
- Never rely on screen coordinates, an app-name keyword or a single desktop size
  for correctness. Design for a compact client as well as a regular desktop;
  the host must wrap labels, preserve reachable controls and allow scrolling.

## Implement the whole interaction

Every enabled control needs a real handler and visible result. Starting an app,
refreshing a surface or changing layout must not accidentally activate its first
button or change stored values. Unknown actions fail explicitly without side
effects. Use explicit confirm/cancel actions for destructive operations.

Do not report success before the underlying operation succeeds. Show actionable
validation errors while preserving input; make empty state usable; show loading
or unavailable state when appropriate. Saving means actual host-backed storage,
not a process-local variable. Preserve existing data formats during repair unless
a supported, tested migration is explicitly part of the task.

For time-dependent behavior, derive values from the supplied host clock and
persisted start/deadline state, not a literal timestamp or number of refreshes.
Distinguish foreground refresh from daemon scheduling and OS notification delivery.
Closing a window is not proof that a reminder keeps running. Use scheduler and
notification semantics exactly as supported, handle typed denials/unavailability,
and never label a schedule active before the host accepts it. Do not downgrade a
requested background app to a foreground demo or fake success to pass a check.

## Acceptance is evidence, not appearance

Before handoff, check the authored node tree and every acceptance example against
the code. Do not claim compilation, UI interaction, screenshots or notifications
that were not actually observed. The author sandbox may not have a compiler or
GUI: record these checks as pending for Builder/Verifier instead of bypassing the
sandbox, calling external tools, or manufacturing evidence.

The product acceptance path should independently exercise cold launch, visible
first screen, primary interaction, invalid input, reopen/persistence and relevant
timed/service behavior. An actual screenshot must come from the running app; it
proves appearance, not arithmetic, persistence or background delivery. A compiled
package, a nonempty surface and a fully usable application are separate outcomes.
Preserve these distinctions in readiness and publication decisions.
