# VibApp 0.1.0 Preview 2

Find it. Vibe it.

## Choose your client

| Platform | Download | Requirements |
| --- | --- | --- |
| macOS Apple Silicon | VibApp-macOS-Apple-Silicon.dmg | macOS 13+, M-series |
| macOS Intel | VibApp-macOS-Intel.dmg | macOS 13+, Intel |
| Windows | VibApp-Windows-x64-Setup.exe | Windows 10/11, x64, Microsoft WebView2 Runtime |
| Linux | VibApp-Linux-x64.deb | Ubuntu 22.04/24.04 or compatible, amd64 |
| Android | VibApp-Android-ARM64.apk | Android 9+, ARM64; foreground Web/Wasm preview |

The website at https://www.vibapp.ai recommends the appropriate download. A
verified browser runtime takes priority over the native-client link. Desktop-only
apps use vibapp:// to request explicit installation, never a URL permission grant.

## Installation

- **Mac:** open the DMG and drag VibApp into Applications. This preview is ad-hoc
  signed, **not Apple Developer ID signed or notarized**. If macOS blocks it and
  you trust this release, use System Settings → Privacy & Security → Open Anyway.
  Do not disable Gatekeeper globally.
- **Windows:** run Setup. It installs for the current user, creates a Start Menu
  entry and registers vibapp://. This preview is **not Authenticode signed**;
  SmartScreen may warn. The installer checks WebView2 and directs you to
  Microsoft's download page if it is missing.
- **Linux:** open the DEB with your software installer, or use
  `sudo apt install ./VibApp-Linux-x64.deb`. System WebKit/GTK dependencies must
  be available. The package registers an application menu entry and URL handler.
- **Android:** allow your browser to install the APK. It is signed with a stable
  VibApp preview key, not distributed through Google Play. The native Rust Android
  host bundles the shared GUI and verified LED Web/Wasm runtime for offline use.

## Scope and verification

- Desktop bundles include Python, Node and RoomHash. Creating new apps still
  needs separately configured model/code-agent and build services. Windows local
  source builds use Docker/remote; local agent auto-discovery is not advertised.
- Android currently supports **foreground Web/Wasm apps only**. The Store snapshot
  is bundled. Dynamic catalog updates, desktop background services, local
  CodeAgent, connected remote development, vibapp:// deep links and mobile P2P/RTC
  are not included. Installing this Android preview does not enable desktop-only
  applications; use their verified browser version where available.
- Client platform support does not change an app's verified manifest. The LED
  browser package is separately qualified; its legacy native manifest retains
  its original macOS ARM64 declaration.
- Per-platform manifests and SHA256SUMS identify actual source commits, pinned
  dependencies and artifact bytes. Runtime digest, ownership, permission and
  process limits remain enforced. Host diagnostics are labeled separately from
  Store app eligibility and GUI acceptance.
- Desktop CI stages a draft only. Signed Android output and its source/acceptance
  receipts must be checked and added before the combined release is public.

## 中文

提供 Mac（Apple Silicon / Intel）、Windows x64、Linux amd64 和 Android ARM64
预览版。网页优先运行已验证的 Web/Wasm 应用；没有网页运行包才唤醒客户端。

桌面版已内置 Python、Node 和 RoomHash，生成新应用仍需配置编程智能体与构建服务。
Android 这次只支持前台 Web/Wasm 应用，内置 LED 时钟可离线运行；不代表桌面版的
后台服务、CodeAgent 和 P2P 功能已经全部移植。

Mac 尚未做 Apple 开发者签名和公证；Windows 安装程序尚未做代码签名。请核对校验和，
只在信任此预览版时确认系统提示。
