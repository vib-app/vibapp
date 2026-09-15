# Review repair ledger — 2026-09-06

Authority: the user approved implementation following the project review and
previously authorized the desktop/web product, local CodeAgent testing and Docker
isolation. This ledger governs that product maintenance scope; it does not mark
historical Stage 0 gates accepted, authorize public publication, or alter WIT.

## Order and acceptance

1. Recover source visibility and a repeatable check entry point without moving
   the existing product paths. Keep local credentials, task data, logs and build
   output excluded. Document remaining clean-checkout bootstrap dependencies.
2. Repair installed launch, bounded process output, foreground refresh, draft
   preservation and collaboration expiry. Add negative and regression tests.
3. Connect a real Docker CodeAgent executor, including scoped credentials,
   immutable image identity, cancellation/whole-container cleanup and global
   resource limits. Then add bounded compiler-diagnostic repair attempts without
   reusing one-time consent or rewriting immutable task identity.
4. Test shopping list, clock/stopwatch and notes through real authoring, build,
   verification, private installation, launch, interaction and persisted reopen.
   Browser foreground interaction must be tested separately; native success is
   not proof of browser runtime parity.

## Implemented in this increment

- Source-only Git allowlist; no directory migration or deletion of user data.
- `scripts/check.mjs`: sequential, bounded regression; opt-in offline Rust and
  GUI regeneration, with portable exact-toolchain resolution.
- Installed launch consumes the daemon's bound surface; the stateless UI preview
  executable no longer re-executes/vetoes an already launched installed guest.
- Preview stdout and stderr are drained concurrently with 256 KiB per-stream
  bounds and a monotonic deadline.
- Unchanged refresh responses no longer stop clocks. Interactive surfaces can
  refresh; mutation and refresh are serialized, and hidden/closed views pause.
- Background polling preserves focused/dirty editors without copying credentials
  into persistent settings or draft storage.
- RoomHash expiry clears queue bytes and per-session deduplication. Late callbacks
  cannot revive a closed session. Deduplication retains the latest 1024 event IDs
  per session; domain-level durable idempotency remains the application's job.
- CodeAgent capacity lock is shared across task output directories and processes
  for the same host user. The stable private lock is released by the OS on exit.
- A separate host-user pipeline lease spans CodeAgent, Builder and Verifier, so
  different Desktop/Web data roots cannot fan out multiple delivery pipelines.
- Retry policy is allowlisted for transient errors; backend configuration,
  missing containment, integrity and unknown failures do not invite blind retries.
- Historical package/acceptance checks are explicit opt-ins, not unit tests that
  assume the developer's old `/tmp` logs and dated acceptance directory exist.

## Continued repair and actual execution (2026-09-05)

- Codex now executes inside the real provider-neutral Docker launcher seam. The
  admitted identity binds immutable image, CLI bundle, adapter, model, connection
  and policy. Other CLI adapters remain paused rather than claiming Docker parity.
- Workloads have no external network, host mounts, Docker socket or credentials.
  A bounded host-only streaming Responses relay uses the selected model connection.
  Limits are 2 GiB RAM, 64 PIDs, two CPUs and 64 MiB scratch, with whole-container
  CPU/deadline watchdogs. These limits were inspected on an actual authoring job.
- Source candidates go to the separate offline Builder, with at most two compiler
  repair rounds under the same job/deadline. An actual clock job needed and passed
  one compiler feedback repair. No new consent or immutable task was manufactured
  inside the repair loop.
- Durable exact-container recovery cancels, never resubmits. Cleanup checks owner,
  role, execution ID and immutable image. Receipt persistence failure becomes a
  terminal failure rather than leaving an exited worker marked running forever.
- Source import rejects omitted/modified scaffolds, path escapes, symlinks,
  hardlinks, duplicates and aggregate overflow before importing generated files.
- Browser foreground messages now bind the app, exact artifacts, generation,
  session, surface, route and sequence. Timeout, forged/replayed traffic, closed
  ports and page closure cannot keep a Worker alive or update another app.
- Synthetic conformance records were discovered in the visible public app list.
  Reserved fixture namespaces now cannot acquire public authority from test-only
  `verified` labels. The default Registry search no longer recommends nonexistent
  fixtures; fixture retrieval requires an explicit test-only switch.
- Shared GUI icons and progress bars no longer rely on CSP-blocked inline styles.
  Playwright observed zero browser console errors and a working real Hello
  Component preview after that repair (not native GUI or arbitrary-app parity).
- Text is already a WIT string: the shared GUI now renders non-sensitive Text
  fields as expanding textareas and preserves leading/embedded newlines. The
  mistaken multiline NeedSpec rejection was removed without changing frozen WIT.
  Sensitive inputs still require opaque host handles. Browser field transport
  matches native empty/false semantics rather than blocking unrelated buttons.
- Generation instructions now describe real foreground refresh, multiline Text,
  and KV revision behavior. New task/consent instruction hashes and test fixtures
  were updated; no historical user consent/task was rewritten.
- macOS packaging now preserves the accepted signed inspector bytes, and invokes
  the bundled Verifier preflight before promoting the staged bundle. The previous
  signing step changed the hash after the accepted pin was set; this could break
  verification only in the installed application. Release input is now signed
  before its reviewed pin and desktop build receipt are refreshed.
- Builder scratch accounting handles a directory disappearing between lstat and
  scandir (normal rustc cleanup). Permission errors, symlinks and byte overflow
  still fail, covered by three focused regressions.
- Website local startup resolves the actual `desktop/target/release` bridge by
  default; the legacy `src-tauri/target` path required an unnecessary override.

### Application evidence — build is not functional acceptance

All roots below are private under `artifacts/product-integration/output/`.

| Sample / evidence root | Observed result |
| --- | --- |
| `review-shopping-list-olwwypvu` | Real Codex, compiler, independent Verifier and private AppStore succeeded; 18 real daemon/UI checks passed, including add/toggle/delete and persisted reopen. |
| `review-clock-j56_hcq0` | Real build passed after one compiler repair. Runtime inspection launched, but interaction failed on `Some(0)` KV revision for a missing record. **Not functionally accepted.** |
| `review-notes-11clqh0k` | Rejected before provider execution by the old unsupported-multiline gate. This was a platform defect, not a model failure. |
| `review-clock-d1b6smaz` | Real authoring/build/Verifier passed; running refresh advanced. After daemon restart, Pause reset the elapsed duration because the guest persisted an instance-relative monotonic anchor. **Not functionally accepted.** Generation instructions now prohibit this pattern. |
| `review-notes-9mjrpa35` | Real authoring/build/Verifier passed after one compiler repair. The initial runtime rejected newlines in user text; host Text decoding was fixed without relaxing identifiers or secret handles. The exact unchanged candidate then passed 19 checks in `runtime-functional`, including leading/trailing newlines, saved reopen, cancel clear, confirm clear and empty reopen. |
| `review-clock-f6s5url0` | Authoring reached compiler feedback, but Builder scratch accounting failed on a disappearing rustc temporary directory. **Not accepted.** Platform race fixed with deterministic regression tests. |
| `review-clock-i2yr61df` | Fresh real authoring run after the scratch-race repair; functional result is pending. |

`review_runtime_acceptance.py` drives advertised controls on the actual compiled
Component, retains bound semantic surfaces, and closes/disables the isolated test
installation afterward. It does not rewrite the candidate, claim publication, or
turn a static screenshot into native runtime evidence.

## Still open — do not report as complete

- Complete repaired clock functional acceptance, not only
  compilation. Retain failed first attempts as failed evidence.
- Current packaged desktop visual checks: native UI automation could not connect
  because this session lacks its trusted Node/Sky service. CLI lifecycle checks
  and packaging signatures do not substitute for that visual check.
- Arbitrary generated applications in the browser, durable guest KV, and full
  interaction parity. The only derived web candidate is still the explicit local
  Hello preview; it must not be labeled a production or installed runtime.
- Fully reproducible clean-checkout fixture/tool-layer/registry bootstrap.

Unit or source-level checks are not independent package acceptance. Existing
generated apps, historical success reports and currently open desktop processes
are not relabeled as newly verified by this increment.

## Verification of this increment

Command: `VIBAPP_PYTHON_BIN=/opt/homebrew/bin/python3.11 node scripts/check.mjs --sync-web --rust`.
Initial increment: 11 checks completed; 183 tests passed, 5 skipped (one platform-dependent
credential test and four opt-in historical acceptance/package checks), zero
failures. Rust reported existing unused-code warnings. These counts include
synthetic launcher/pipeline tests, not real provider execution.

289 existing product source/input files were recovered into the Git index after
a bounded file-type/size and obvious-credential-pattern check (5.16 MB). This is
a source baseline, not 289 newly implemented files or a full secret audit. No
commit or remote push was made. Generated integration feeds and private state
were excluded. The baseline retains six pre-existing whitespace warnings.

Latest complete regression: 19 checks completed; 527 tests passed, 6 skipped
(five Python opt-ins/platform checks and one real P2P Rust acceptance). Includes
all 153 non-ignored desktop Rust tests, 12 service-runtime tests, 13 packaging
contract tests, Builder, Registry, Docker boundaries and two-origin web contracts.
The packaging suite was rerun after adding the final bundled-preflight assertion:
13 passed. The targeted website default-path regression separately passed once.

The latest release desktop bundle was compiled, packaged and ad-hoc
signature-checked at `artifacts/desktop/dist/VibApp.app`. Its bundled inspector
preflight reports ready with matching expected/actual signed hashes. Shopping
list (18 checks) and multiline notes (19 checks) also passed using the actual
packaged runtime executable, recorded in each root's `runtime-packaged` directory.
This does not constitute native GUI visual acceptance. Browser preview was
checked separately, with zero console errors and one known iframe-isolation
warning. Its screenshot is `output/playwright/review-browser-final.png`.

Local health: website 3000 and preview 4174 are available; backend
`http://127.0.0.1:3189/healthz` returns ready with the current desktop source receipt.
Only this turn's owned local processes were restarted; no public deployment,
publication, global model-setting change or unrelated container change occurred.
