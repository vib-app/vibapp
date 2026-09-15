#!/usr/bin/env python3
"""Static RoomHash bundle-layout contract; performs no build or resource copy."""

from pathlib import Path
import re
import unittest


DESKTOP_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = DESKTOP_ROOT / "scripts" / "package-macos.sh"
HOST_SOURCE = DESKTOP_ROOT / "src-tauri" / "src" / "roomhash_host.rs"


class RoomHashMacPackageContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = PACKAGE_SCRIPT.read_text(encoding="utf-8")
        cls.host = HOST_SOURCE.read_text(encoding="utf-8")
        cls.package_words = " ".join(cls.package.split())

    def test_staged_resources_match_controller_primary_candidates(self):
        candidate_lists = re.findall(
            r"packaged_candidates\s*\(\s*&\[(.*?)\]\s*\)",
            self.host,
            flags=re.DOTALL,
        )
        self.assertGreaterEqual(len(candidate_lists), 4)
        candidates = [re.findall(r'"([^"]+)"', block) for block in candidate_lists[:4]]
        self.assertEqual(candidates[0][0], "roomhash/node")
        self.assertEqual(candidates[1][0], "roomhash/roomhash-host.mjs")
        self.assertEqual(candidates[2][0], "roomhash/current")
        self.assertEqual(candidates[3][0], "roomhash/collaboration")

        for staged_path in (
            '"$contents/Resources/roomhash/node"',
            '"$contents/Resources/roomhash/roomhash-host.mjs"',
            '"$contents/Resources/roomhash/current/headless/package.json"',
            '"$contents/Resources/roomhash/current/headless/package-lock.json"',
            '"$contents/Resources/roomhash/current/headless/src"',
            '"$contents/Resources/roomhash/current/headless/node_modules"',
            '"$contents/Resources/roomhash/collaboration/index.mjs"',
            '"$contents/Resources/roomhash/collaboration/roomhash-factory.mjs"',
        ):
            self.assertIn(staged_path, self.package)

    def test_current_headless_tree_is_checked_and_staged_without_fixture_copy(self):
        required_fragments = (
            '[ ! -d "$roomhash_headless" ]',
            '[ -L "$roomhash_headless" ]',
            '[ ! -f "$roomhash_headless/package.json" ]',
            '[ ! -f "$roomhash_headless/src/torrent-service.js" ]',
            '[ ! -d "$roomhash_headless/node_modules" ]',
            'cp "$roomhash_headless/package.json" "$contents/Resources/roomhash/current/headless/package.json"',
            'cp "$roomhash_headless/package-lock.json" "$contents/Resources/roomhash/current/headless/package-lock.json"',
            'cp -R "$roomhash_headless/src" "$contents/Resources/roomhash/current/headless/src"',
            'cp -R "$roomhash_headless/node_modules" "$contents/Resources/roomhash/current/headless/node_modules"',
        )
        for fragment in required_fragments:
            self.assertIn(fragment, self.package)

        # The contract test itself is intentionally static: importing this file
        # must not execute the packaging script or materialize its 86 MB tree.
        self.assertFalse((Path(__file__).resolve().parent / "node_modules").exists())

    def test_node_native_addons_and_bundle_are_signed_in_safe_order(self):
        chmod_node = 'chmod 755 "$contents/MacOS/vibapp-launcher" "$contents/MacOS/vibapp-runtime" "$contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime" "$contents/Resources/roomhash/node"'
        sign_node = '"$codesign_bin" --force --sign - --timestamp=none --options runtime --entitlements "$runtime_entitlements" "$contents/Resources/roomhash/node"'
        scan_addons = 'find "$contents/Resources/roomhash/current/headless/node_modules" -type f -name \'*.node\' -exec sh -c'
        inspect_macho = '/usr/bin/file "$binary" | /usr/bin/grep -q "Mach-O.*arm64"'
        sign_addon = '/usr/bin/codesign --force --sign - --timestamp=none "$binary"'
        sign_bundle = '"$codesign_bin" --force --sign - --timestamp=none --options runtime "$staged_bundle"'
        verify_staged = '"$codesign_bin" --verify --deep --strict --verbose=2 "$staged_bundle"'
        verify_committed = '"$codesign_bin" --verify --deep --strict --verbose=2 "$bundle"'

        for step in (chmod_node, sign_node, scan_addons, inspect_macho, sign_addon, sign_bundle, verify_staged, verify_committed):
            self.assertIn(step, self.package_words)
        positions = [
            self.package_words.index(step)
            for step in (chmod_node, sign_node, scan_addons, sign_bundle, verify_staged, verify_committed)
        ]
        self.assertEqual(positions, sorted(positions))

    def test_host_script_is_non_executable_data_and_node_is_executable(self):
        self.assertIn(
            'chmod 644 "$contents/Resources/roomhash/roomhash-host.mjs" "$contents/Resources/roomhash/collaboration/index.mjs" "$contents/Resources/roomhash/collaboration/roomhash-factory.mjs"',
            self.package,
        )
        self.assertIn(
            'chmod 755 "$contents/MacOS/vibapp-launcher" "$contents/MacOS/vibapp-runtime" "$contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime" "$contents/Resources/roomhash/node"',
            self.package_words,
        )

    def test_node_must_be_arm64_relocatable_and_executable_after_staging(self):
        required_fragments = (
            "otool_bin=/usr/bin/otool",
            '/usr/bin/file -L "$node_path" | /usr/bin/grep -q "Mach-O.*arm64"',
            'if ! node_dependency_report=$("$otool_bin" -L "$node_path" 2>&1); then',
            "could not be inspected with otool:",
            "/usr/bin/awk 'NR > 1 && $1 !~ /^\\/System\\/Library\\// && $1 !~ /^\\/usr\\/lib\\// { print $1 }'",
            "has non-relocatable dynamic dependencies:",
            "set VIBAPP_ROOMHASH_NODE_BIN to a standalone arm64 Node executable",
            '"$node_path" --version >/dev/null 2>&1',
            'validate_relocatable_node "$roomhash_node" "RoomHash Node" 65',
            'validate_relocatable_node "$contents/Resources/roomhash/node" "packaged RoomHash Node" 70',
        )
        for fragment in required_fragments:
            self.assertIn(fragment, self.package)
        self.assertNotIn('"$otool_bin" -L "$roomhash_node" \\\n', self.package)

        sign_node = '"$codesign_bin" --force --sign - --timestamp=none --options runtime --entitlements "$runtime_entitlements" "$contents/Resources/roomhash/node"'
        staged_validation = 'validate_relocatable_node "$contents/Resources/roomhash/node" "packaged RoomHash Node" 70'
        self.assertLess(self.package_words.index(sign_node), self.package_words.index(staged_validation))

    def test_production_public_registry_is_bounded_and_staged_without_fixture_fallback(self):
        required_fragments = (
            'artifacts_root=$(CDPATH= cd -- "$desktop_root/.." && pwd)',
            'public_registry_data="$artifacts_root/product-platform/registry/generated/production-public-data"',
            'public_registry_snapshot="$public_registry_data/registry.snapshot.json"',
            'public_registry_locators="$public_registry_data/package-locators"',
            'public_registry_index="$public_registry_locators/index.json"',
            'public_registry_source_snapshot="$desktop_root/../product-platform/registry/snapshots/registry.snapshot.json"',
            'public_registry_source_locators="$desktop_root/../product-platform/registry/package-locators"',
            'public_registry_projection_sync="$desktop_root/../web-client-core/sync-web-gui.mjs"',
            'public_registry_data_verifier="$desktop_root/../web-client-core/sync-public-package-locators.mjs"',
            '[ -L "$public_registry_data" ]',
            '[ -L "$public_registry_snapshot" ]',
            '[ -L "$public_registry_locators" ]',
            '[ -L "$public_registry_index" ]',
            '"$public_registry_projection_sync" --production-public-registry-only',
            '"$public_registry_data_verifier" --verify-production-data-root "$public_registry_data"',
            '"$public_registry_data_verifier" --verify-production-data-root "$packaged_public_registry_data"',
            'packaged public Registry snapshot/index binding changed before signing',
            'final_packaged_public_registry_snapshot_sha256',
            'final_packaged_public_registry_index_sha256',
            'public_registry_source_sha256_after_sync=$(compute_public_registry_source_sha256)',
            'public Registry source changed during deterministic projection sync',
            'public Registry source or generated projection changed while the package was staged',
            '"$contents/Resources/public-registry/data/package-locators"',
            'cp "$public_registry_snapshot" "$contents/Resources/public-registry/data/registry.snapshot.json"',
            'cp "$public_registry_entry" "$contents/Resources/public-registry/data/package-locators/"',
            '"public_registry_source_sha256":"%s"',
            '"public_registry_snapshot_sha256":"%s"',
            '"public_registry_locator_index_sha256":"%s"',
        )
        for fragment in required_fragments:
            self.assertIn(fragment, self.package)
        self.assertNotIn('product-platform/website/public/data', self.package)
        self.assertNotIn("acceptance", " ".join(
            line for line in self.package.splitlines()
            if "public_registry" in line or "public-registry" in line
        ))

    def test_failed_or_interrupted_promotion_recovers_or_removes_official_bundle(self):
        required_fragments = (
            "bundle_promoted=0",
            'if [ "$bundle_promoted" -eq 1 ] && { [ -e "$bundle" ] || [ -L "$bundle" ]; }; then',
            'mv "$bundle" "$stage_root/aborted.app"',
            'mv "$previous_bundle" "$bundle"',
            "trap cleanup EXIT",
            "trap 'exit 129' HUP",
            "trap 'exit 130' INT",
            "trap 'exit 143' TERM",
            "bundle_promoted=1",
        )
        for fragment in required_fragments:
            self.assertIn(fragment, self.package)
        self.assertNotIn("trap cleanup EXIT HUP INT TERM", self.package)

        promote = 'mv "$staged_bundle" "$bundle"'
        committed = "package_committed=1"
        self.assertLess(self.package.rindex("bundle_promoted=1"), self.package.rindex(promote))
        self.assertLess(self.package.rindex(promote), self.package.rindex(committed))


if __name__ == "__main__":
    unittest.main()
