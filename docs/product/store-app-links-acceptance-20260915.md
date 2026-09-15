# Store app links and install confirmation — 2026-09-15

## Product behavior

- A verified, applicable browser binding opens inside the web runtime. A catalog WASM flag by itself cannot authorize execution.
- A published app without that binding offers `vibapp://<app-id>`, not a ZIP download as the primary action. The trusted web shell uses a user-clicked `_blank` link with `noopener`; `_self` was blocked by frame CSP and `_top` by iframe navigation restrictions. Untrusted guest sandbox rules are unchanged.
- macOS URL handling validates the app ID and downloads only from the fixed public `vib-app/packages` catalog and release location. Hashes, size limits, archive paths, source identity and existing snapshot validation are checked before local intake.
- Downloading is staging, not installation or enablement. A dedicated 480×480 native install window shows app identity, human-readable permissions, folded exact details, Cancel and Install and open. Main-window navigation and content are not replaced.
- Cancel/Escape/close dismisses consent without installation. A late download completion cannot reopen cancelled consent. Installation is tied to the current request and verified package digest; the confirmation window closes after successful installation and app-window opening.
- Already installed and enabled applications open without another installation prompt. This does not silently update an installed version.

## Observed acceptance

- Real LED package downloaded and verified from GitHub without user credentials.
- Fresh isolated data directory: cancelled confirmation closed; no daemon installation/state was created.
- Reopening the OS URL, then selecting Install and open, produced daemon install → enable → ui-open audit events. The confirmation window disappeared and a separate LED application window opened.
- Captured and inspected the actual native confirmation window, including icon, both actions and all five declared permissions. Shared UI tests cover English/Chinese and prohibit inline main-window consent.
- Browser click with `_blank` retained the Store page and avoided the previous CSP/sandbox errors. Browser policy can still require the user to approve opening an external app; installation detection is not inferred from timers.
- Shared UI/catalog/hosted-shell/projection tests: 64 passed. Native release build and macOS package validation passed. Earlier startup URL tests: 7 passed. Public intake/local store tests: 33 passed.

## Still not accepted

- The published LED package has no verified browser derivation; it currently routes to the native client. No claim that it runs in the browser.
- The LED surface opens, but live seconds did not advance during this acceptance run. No `ui-refresh` event reached the isolated daemon. Decoupling timer scheduling from cosmetic animation-frame sizing was insufficient; do not mark clock behavior as passed.
- A public client installer and Windows/Linux URL registration are not delivered by this change. The website states the installer limitation rather than supplying a nonexistent download.
- Service-only app URL activation is not covered by the UI-window path and needs separate service lifecycle handling.
- Pre-existing broad-suite failures remain: one packaged runtime contract text expectation and four web snapshot/text expectations. The targeted passing tests are not a claim that every project test passes.
