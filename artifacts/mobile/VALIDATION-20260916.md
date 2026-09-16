# Android foreground LED acceptance evidence — 2026-09-16

State: implementer-observed local acceptance; final public source binding and
independent release acceptance remain the release owner's responsibility.

- Native Android ARM64 release built with Rust 1.98.0, Tauri 2.11.5 and NDK
  28.2.13676358; Gradle 8.14.3 distribution checksum is pinned.
- Signed APK installed on a read-only Android 36.1 ARM64 emulator. A connected
  physical USB device was explicitly excluded using `adb -e` for every operation.
- Wi-Fi and mobile-data settings both observed as `0` before app execution.
- Store showed `ai.vibapp.custom.1a04442610c` version 0.1.1, from public package
  `f3768d912af30054392f7a8e254ac87283ca02c4fc82476ddcafe3fd00906dce`.
- Store Open launched the bundled, integrity-checked Worker/Component derivation.
  Two captured screens showed real clock progression **11:39:46 → 11:39:58** while
  the emulator was offline; no page reload or external service was used.
- `chromium:E` and `AndroidRuntime:E` log query returned no errors.
- APK signature and 16 KiB ZIP alignment verification passed. Native ELF LOAD
  segments were also observed with alignment `0x4000`.
- Three mobile protocol tests passed, covering disconnected capability reporting,
  bounded app-scoped storage, and malformed/extra-field product envelopes.

Operational evidence is intentionally excluded from source export:

- `output/android-led-store.png`
- `output/android-led-running-1.{png,json,log}`
- `output/android-led-running-2.{png,json,log}`
- `output/android-signature-verification.txt`
- `output/android-build.log`

The acceptance APK had SHA-256
`972fddb13e21755db8f22e6b2ec54bdd179d148e8c25bd89e10c33a4c8bfa0fc`.
This hash describes this local tested build, not a claim that it was built from an
already published Git commit. The final source-attested rebuild may differ.

Limitations remain explicit: foreground Web/Wasm only; the Store snapshot is
bundled; no connected remote development, local CodeAgent, desktop background
daemon, or mobile P2P/RTC authority is claimed.
