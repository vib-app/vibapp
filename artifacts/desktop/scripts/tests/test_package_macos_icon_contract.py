#!/usr/bin/env python3
"""Regression coverage for the VibApp macOS bundle icon."""

from __future__ import annotations

from pathlib import Path
import plistlib
import struct
import subprocess
import tempfile
import unittest


DESKTOP_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = DESKTOP_ROOT / "scripts" / "package-macos.sh"
GENERATOR = DESKTOP_ROOT / "scripts" / "generate-macos-icon.sh"
INFO_PLIST = DESKTOP_ROOT / "packaging" / "macos" / "Info.plist"
ICON = DESKTOP_ROOT / "src-tauri" / "icons" / "VibApp.icns"
SOURCE_SVG = DESKTOP_ROOT / "ui" / "favicon.svg"


def png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise AssertionError(f"not a PNG: {path}")
    return struct.unpack(">II", payload[16:24])


class MacIconPackageContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = PACKAGE_SCRIPT.read_text(encoding="utf-8")

    def test_info_plist_declares_the_packaged_icon(self) -> None:
        with INFO_PLIST.open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIconFile"], "VibApp.icns")

    def test_package_requires_and_stages_the_icon_before_signing(self) -> None:
        required = 'macos_icon="$desktop_root/src-tauri/icons/VibApp.icns"'
        staged = 'cp "$macos_icon" "$contents/Resources/VibApp.icns"'
        self.assertIn(required, self.package)
        self.assertIn('"$runtime_entitlements" "$macos_icon"; do', self.package)
        self.assertIn(staged, self.package)
        self.assertLess(
            self.package.index(staged),
            self.package.index(
                '"$codesign_bin" --force --sign - --timestamp=none --options runtime "$staged_bundle"'
            ),
        )

    def test_generator_uses_the_existing_vector_brand_source(self) -> None:
        generator = GENERATOR.read_text(encoding="utf-8")
        self.assertTrue(SOURCE_SVG.is_file())
        self.assertIn('source_svg=${1:-"$desktop_root/ui/favicon.svg"}', generator)
        self.assertIn("<path", SOURCE_SVG.read_text(encoding="utf-8"))

    def test_checked_in_icon_contains_all_macos_representations(self) -> None:
        self.assertTrue(ICON.is_file())
        with tempfile.TemporaryDirectory(prefix="vibapp-icon-contract-") as temporary:
            iconset = Path(temporary) / "VibApp.iconset"
            subprocess.run(
                ["/usr/bin/iconutil", "-c", "iconset", str(ICON), "-o", str(iconset)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            expected = {
                "icon_16x16.png": (16, 16),
                "icon_16x16@2x.png": (32, 32),
                "icon_32x32.png": (32, 32),
                "icon_32x32@2x.png": (64, 64),
                "icon_128x128.png": (128, 128),
                "icon_128x128@2x.png": (256, 256),
                "icon_256x256.png": (256, 256),
                "icon_256x256@2x.png": (512, 512),
                "icon_512x512.png": (512, 512),
                "icon_512x512@2x.png": (1024, 1024),
            }
            for name, dimensions in expected.items():
                self.assertEqual(png_dimensions(iconset / name), dimensions)


if __name__ == "__main__":
    unittest.main()
