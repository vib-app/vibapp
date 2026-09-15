# VibApp client preview

Download **VibApp-macOS-Apple-Silicon.dmg**, open it, and drag VibApp into Applications.
Open VibApp once, then return to www.vibapp.ai and choose **Open in VibApp**.
The website can launch verified browser-enabled apps directly; native-only apps use the client.

## Requirements and limits

- macOS 13 or newer, Apple Silicon (M1 or newer). Intel Macs, Windows and Linux are not included in this preview.
- This preview is ad-hoc signed, **not Apple Developer ID signed or notarized**. macOS may block first launch. Only if you trust this release, use macOS System Settings → Privacy & Security → Open Anyway. Do not disable Gatekeeper globally.
- Python, Node and the RoomHash transport runtime are included. No Homebrew is needed to browse/install/run existing apps.
- Creating new apps still requires separately configured model/code-agent services and the development/build environment; those are not bundled here.
- Experimental software: some Store apps, including the current LED clock's automatic refresh, have known functional limitations. A successful install is not a guarantee that every app works correctly.
- SHA256SUMS and release-manifest.json identify the download and exact build dependencies. The build-time verifier pin is derived from the freshly compiled and signed trusted inspector before embedding the desktop source receipt; runtime hash checks stay enabled.

## 中文

下载 DMG，将 VibApp 拖入「应用程序」，先打开一次，然后回到网站点击「在 VibApp 中打开」。
当前只提供 Apple Silicon Mac 预览版，尚未做 Apple 开发者签名和公证，首次打开可能需要在「系统设置 → 隐私与安全性」中确认。
已内置运行已有应用所需的 Python、Node 与 RoomHash；生成新应用仍需自行配置编程智能体和构建服务。
