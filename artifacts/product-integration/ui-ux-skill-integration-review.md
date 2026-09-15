# Default application-experience skill integration

2026-09-09. Scope: trusted authoring inputs and new-task consent, not generated
application code, UI renderer, Docker image, WIT, or publication. No model,
container, credentials, user-data mutation or Cargo invocation in this workstream.

## Implemented

- The one source is `artifacts/cloud-agent/skills/vibapp-ui-ux/SKILL.md`.
  New tasks get its exact bytes at `contracts/vibapp-ui-ux.md`, mode 0444.
- All four supported providers receive explicit instructions to read the passive
  document. Its byte SHA256 is in the actual instruction text; the SHA256 of that
  whole text enters provider request, immutable task digest and single-use consent.
- UI support remains limited to UI-only OpenCode and modern Docker Codex tasks.
  Hybrid/service tasks get the same experience guidance without UI-only scaffolds.
  Fresh OpenCode non-UI tasks use the existing generic non-Codex base policy.
- Workspace preparation refuses unbound inputs before writing; audit requires
  exact input inventory, bytes, regular-file metadata and readonly permissions.
  Docker's existing source/contracts protocol transmits this file unchanged and
  rejects omission or modification before importing authored source. Only its
  export count allowance changes from source quota +1 to +3 trusted documents;
  exact paths, bytes and actual source quota still undergo independent checks.
- Rust preview computes matching new instructions, including provider-only
  preview. General frozen instruction text comes from the original Python literal
  and must match both old pins; there is no second copied policy body.
- Node/Rust/shell inventories share label
  `cloud-agent/skills/vibapp-ui-ux/SKILL.md`; packaging preflights and copies it to
  `Contents/Resources/cloud-agent/skills/vibapp-ui-ux/SKILL.md`.

## Historical compatibility

Existing constant bodies and starter instruction files were not edited here.
Only an exact recognized legacy digest chooses old instruction bytes and old
workspace contents. That path does not load the new skill. Unknown digests remain
rejected; no task or consumed consent is rewritten, resubmitted or replayed.

| Existing policy | SHA256 retained |
| --- | --- |
| Generic Codex | `49fe02f64ab181e3c9a1ce22729bd3ec1309a5afa7b8a4b39e14391d95650536` |
| Generic other-provider | `406373f344973f6eef09a2973bda945594fdb7a14cb5df5ac51fe5b78d216d2a` |
| Current pre-skill Codex UI support | `5664d7d20f255de784cbe2e7dfb1140b20e75f7bd8d2d93f57bda5999f1bbcdf` |
| Current pre-skill OpenCode support | `3717d8a8265849a9816a9a1a587b3347d582fbb5911069e4b773ac7177fbf36b` |

Pre-existing limitation: the older sticky-notes task used
`b4c48d65c828abbef67c845d0a6a4c9632990e21d46cce3a93334edc53225dda`,
before an earlier starter-policy change. It is not silently mapped to today's
instructions. A future explicit historical snapshot must recover its exact old
template/guide/support bytes from trustworthy saved evidence. Parent requested
this remain a separate task, not a blocker or a reason to accept arbitrary hashes.

## Evidence

- CloudAgent: 77 tests PASS, including four providers × three worlds, actual
  consent/request/prompt binding, known historical policies, replay rejection,
  byte drift, missing/linked/writable documents and complete workspace audits.
- Adapter: 67 tests, 66 PASS and one existing skip. Includes real export/import
  roundtrips for both Docker adapter implementations with all seven files, and
  missing/changed skill failure before any generated-file import.
- Desktop build-input Node suite: 5 PASS. Executes the actual package copy command
  in a temporary staging root and checks byte identity, receipt drift despite old
  mtimes, missing file and symlink rejection.
- Read-only history/archive/retry diagnostics: 44 PASS, preserving old rows,
  permissions and timestamps without granting execution authority.
- `sh -n artifacts/desktop/scripts/package-macos.sh` and `git diff --check`: PASS.

Pending parent build: Rust `cloud_task_preview::tests`, including 28 actual Python
policy parity cases and full Docker UI task/consent/request parity. Package and
live-author verification must follow that gate; they have not been claimed PASS.
