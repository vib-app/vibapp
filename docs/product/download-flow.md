# Public application acquisition

The Store has one primary Open action. A verified Web/Wasm app opens
`/apps/<app-id>?run=1` in a new top-level tab; a native-only public app uses the
existing validated `vibapp://<app-id>` handoff and offers the client installer.
Package acquisition is not an install, permission grant, or service enable.

## Browser

- Default HTTP threshold: 20 MiB, configurable in the top-right Downloads panel.
  The current package admission ceiling remains 64 MiB. Larger unsupported
  artifacts must not be described as supported merely by increasing a setting.
- Below the threshold, download pinned GitHub Raw files, check exact size and
  SHA-256 plus candidate/manifest authority, and commit atomically to IndexedDB.
- Above the threshold, offer a shared folder when the browser supports it.
  With explicit P2P consent, use RoomHash/WebTorrent with a pinned GitHub Raw
  webseed. Metadata wait is bounded at 10 seconds and transfer at 15 seconds;
  failed P2P acquisition falls back to verified HTTP (120-second deadline).
- P2P disabled is not an error and is never silently enabled. This browser can
  explicitly opt in/out through Downloads settings, without a remote backend.
- Cancel aborts acquisition; Retry creates a fresh top-level host/frame channel.
  No network retry, partial artifact, or cache receipt grants execution trust.
- Unsupported directory pickers use browser cache with a Chrome suggestion.
  Refused/cancelled directory selection offers the same usable fallback.
- Host-only Service Worker serves hash-addressed public runtime files. Every
  cache read is rehashed; Registry/attestation checks remain mandatory. Private
  data, APIs, HTML and directory handles are excluded; runtime cache is bounded
  separately at 256 MiB. Browsers without this facility use verified HTTP.
- Every app tab owns its own Worker, port, nonce, pending bootstrap and session.
  Closing/replacing the page cancels pending work and terminates the Worker.

## Shared cache and native intake

Recommended directory: the user's OS Downloads directory plus `VibApp/Packages`.
The browser must ask the user to select it; it cannot silently access an absolute
native path. Layout: `<package-sha256>.vibapp-candidate/candidate.json` and
`<package-sha256>.vibapp-candidate/package/**`. Write candidate.json last.
Keep application private data and credentials out of this directory.

Native deep links resolve the current live Store entry before choosing the
installed version. Native intake tries the shared folder first, copies into
owner-private staging with bounded no-follow reads, and rechecks all existing
candidate validation and current Store identity/digests. Missing, tampered or
partial cache falls back to the verified Release archive. The current safe
shared-folder reader uses POSIX directory descriptors (macOS/Linux); platforms
without that facility retain HTTP fallback. Non-default browser directories are
not automatically discoverable by the native client.

This native cache integration needs a new client release; preview.2 does not
contain it. Downloading newer application bytes still requires normal native
install/update and capability consent before execution.

## Acceptance (2026-09-16)

Local production build and TypeScript check; Rust 1.98 cargo check; UI,
foreground-session, package-store, HTTP/P2P unit regression; native shared-cache
reuse, tampering fallback and symlink rejection tests.

Real Chromium: LED starts in a separate tab with P2P off via HTTP; seconds
advance; 3 close/reopen cycles keep one Worker per running tab; cancelling
before launch leaves zero Workers, retry succeeds. Threshold lowered to 0.1
MiB to exercise the large-package path using the same real 0.3 MiB LED package:
directory prompt, browser-cache fallback and unavailable-metadata P2P→HTTP
fallback succeed. Directory-picker absence is simulated by disabling the API;
the real HTTP/verification/runtime path still runs. This is not a claim that
Safari/Firefox, an OS folder picker, Windows shared-folder reuse, or remote
peer-to-peer payload transfer has been independently accepted.
