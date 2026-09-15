from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import app_builder  # noqa: E402
import common  # noqa: E402
from app_builder import MacSandboxCargoRunner  # noqa: E402
from common import ProcessLimits, bounded_tree_size  # noqa: E402
from offline_cache import CacheError, inventory_tree, normalized_member  # noqa: E402


class OfflineCacheSafetyTests(unittest.TestCase):
    @staticmethod
    def _builder_inputs(root: Path) -> tuple[Path, Path, Path, Path]:
        input_root = root / "external-builder-inputs"
        tool_layer = input_root / "tool-layers" / "accepted"
        cargo_home = input_root / "cargo-cache" / "cargo-home"
        cache_acceptance = input_root / "cargo-cache" / "acceptance.json"
        tool_layer.mkdir(parents=True)
        cargo_home.mkdir(parents=True)
        cache_acceptance.write_text(
            json.dumps(
                {
                    "schema_version": "vibapp.offline-cache-acceptance.experimental-v1",
                    "state": "independently-accepted",
                    "cargo_home": str(cargo_home.resolve(strict=True)),
                    "network": "none",
                    "read_only": True,
                    "allowed_proc_macros": [
                        {
                            "name": "wit-bindgen-rust-macro",
                            "version": "0.60.0",
                            "checksum": "a" * 64,
                        }
                    ],
                    "accepted_by": "independent-test-verifier",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return input_root, tool_layer, cargo_home, cache_acceptance

    def test_packaged_builder_uses_explicit_external_input_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-packaged-builder-root-") as raw:
            temporary = Path(raw)
            input_root, tool_layer, cargo_home, cache_acceptance = self._builder_inputs(temporary)
            runner = MacSandboxCargoRunner(
                tool_layer,
                cargo_home,
                cache_acceptance,
                input_root,
            )
            packaged_repo = temporary / "VibApp.app" / "Contents"
            original_is_file = Path.is_file

            def packaged_is_file(path: Path) -> bool:
                if path == Path("/usr/bin/sandbox-exec"):
                    return True
                return original_is_file(path)

            with mock.patch.object(app_builder, "REPO", packaged_repo), mock.patch.object(
                Path, "is_file", packaged_is_file
            ):
                acceptance, _ = runner._validate_inputs()

            self.assertEqual(runner.builder_input_root, input_root.resolve(strict=True))
            self.assertEqual(acceptance["cargo_home"], str(cargo_home.resolve(strict=True)))
            self.assertFalse((packaged_repo / "generated").exists())

    def test_builder_rejects_an_input_outside_the_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-builder-root-escape-") as raw:
            temporary = Path(raw)
            input_root, _, cargo_home, cache_acceptance = self._builder_inputs(temporary)
            escaped_tool_layer = temporary / "outside" / "tool-layer"
            escaped_tool_layer.mkdir(parents=True)
            runner = MacSandboxCargoRunner(
                escaped_tool_layer,
                cargo_home,
                cache_acceptance,
                input_root,
            )
            original_is_file = Path.is_file

            def sandbox_is_file(path: Path) -> bool:
                if path == Path("/usr/bin/sandbox-exec"):
                    return True
                return original_is_file(path)

            with mock.patch.object(Path, "is_file", sandbox_is_file), self.assertRaisesRegex(
                common.PipelineError, "explicit Builder input root"
            ):
                runner._validate_inputs()

    def test_archive_paths_must_remain_below_exact_package_root(self) -> None:
        self.assertEqual(normalized_member("crate-1.0/src/lib.rs", "crate-1.0"), "src/lib.rs")
        for unsafe in ("../escape", "crate-1.0/../escape", "/crate-1.0/file", "other/file"):
            with self.subTest(unsafe=unsafe), self.assertRaises(CacheError):
                normalized_member(unsafe, "crate-1.0")

    def test_cache_inventory_rejects_symlinks_and_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-cache-safety-test-") as raw:
            root = Path(raw)
            original = root / "original"
            original.write_bytes(b"locked bytes")
            symlink = root / "symlink"
            symlink.symlink_to(original)
            with self.assertRaisesRegex(CacheError, "link or special"):
                inventory_tree(root)
            symlink.unlink()
            os.link(original, root / "hardlink")
            with self.assertRaisesRegex(CacheError, "hard-linked"):
                inventory_tree(root)

    def test_disk_monitor_tolerates_compiler_scratch_file_disappearing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-disk-race-test-") as raw:
            root = Path(raw)
            stable = root / "stable"
            stable.write_bytes(b"1234")
            ephemeral = root / "ephemeral"
            ephemeral.write_bytes(b"gone")
            original_lstat = Path.lstat

            def flaky_lstat(path: Path):
                if path == ephemeral:
                    raise FileNotFoundError(path)
                return original_lstat(path)

            with mock.patch.object(Path, "lstat", flaky_lstat):
                self.assertGreaterEqual(bounded_tree_size(root, 1024), 4)

    def test_darwin_does_not_apply_per_uid_nproc_limit(self) -> None:
        calls = []
        with mock.patch.object(common.sys, "platform", "darwin"), mock.patch.object(
            common.resource, "setrlimit", side_effect=lambda kind, value: calls.append((kind, value))
        ):
            common._limit_child(ProcessLimits())
        if hasattr(common.resource, "RLIMIT_NPROC"):
            self.assertNotIn(common.resource.RLIMIT_NPROC, [kind for kind, _ in calls])

    def test_sandbox_reads_own_workspace_but_not_the_user_home(self) -> None:
        profile = MacSandboxCargoRunner._sandbox_profile(
            [Path("/accepted/tool-layer"), Path("/accepted/cache"), Path("/private/build")],
            Path("/private/build"),
        )
        self.assertIn("(deny network*)", profile)
        self.assertIn('(allow file-read* (subpath "/private/build"))', profile)
        self.assertIn('(allow file-write* (subpath "/private/build"))', profile)
        self.assertIn('/Applications/Xcode.app/Contents', profile)
        self.assertNotIn("(allow file-read*)", profile)
        self.assertNotIn(str(Path.home()), profile)


if __name__ == "__main__":
    unittest.main()
