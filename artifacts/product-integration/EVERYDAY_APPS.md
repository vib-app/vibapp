# Everyday applications acceptance batch

The user approved twelve OS-style applications on 2026-09-09. The catalog in
`everyday_app_scenarios.json` is requirement input, not implemented functionality.
`everyday_apps_workflow.py` submits these requirements through the actual product:
Qwen analysis, Registry decision, fresh Codex task/consent, isolated authoring,
Builder and independent Verifier. It never authors application source itself.

Use the existing product data root so Development/history can show the real tasks.
Settings must already select Codex / gpt-5.6-sol and the existing Qwen receptionist;
the helper checks them without modifying settings or copying credentials.

Run `prepare`, then `complete`, then `run` for each application, each with
`--root <private-batch-evidence> --product-root <existing-product-data> --app <key>`.
Prepare/complete are sequential so the Qwen receptionist is not overloaded. After
fresh infrastructure acceptance, independent recorded `run` processes may overlap;
the controller limits Docker authors to two and all source/final compiles to one.
An admitted third worker stays queued and waits at most thirty minutes before
reporting contention. Failed/uncertain provider jobs are not automatically replaced.

Initial set: calculator, converter, clock, sticky-notes, todo, calendar and contacts.
Notepad additionally needs host edit-change/autosave dispatch and multiline input
verification; typing currently stays in a Client draft until an action is submitted.
Image-viewer, drawing, recorder and file-manager also remain capability-dependent;
the helper refuses to generate a fake implementation before the required host APIs
exist. Window dimensions in the catalog are desired design metadata; verify the
actual package/Launcher presentation separately, never claim prose means it shipped.

`private-appstore-ready` currently means platform delivery gates passed, not that
all requested product behavior passed acceptance. Follow every generated candidate
with `review_runtime_acceptance.py` using independently authored action/persistence
assertions derived from its actual semantic surface. Record desktop and web visual
checks separately. Public GitHub source, Actions build, release ZIP/torrent and
AppStore publication are separate accepted actions, never performed by this helper.

Current operational mission, task handles and per-app outcomes:
`.butler/runs/20260909-073618-everyday-apps/`.

Small runtime checkpoint set: bridge build-input receipt must match the current
source inventory; existing selected CodeAgent/model and receptionist settings must
match the task before admission; website backend `/healthz` must expose that same
bridge receipt (an older live bridge can otherwise outlast a binary rebuild).
During delivery inspect durable worker/container status, bounded model requests and
compiler progress. After delivery check exact-candidate runtime behavior and reopen
persistence, not only the process, HTTP200, compilation or package availability.
