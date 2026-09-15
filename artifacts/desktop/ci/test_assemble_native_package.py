from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("assemble_native_package.py")
SPEC = importlib.util.spec_from_file_location("assemble_native_package", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class NativePackageAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "bin").mkdir()
        (self.source / "resources").mkdir()
        self.payloads = {
            "bin/launcher": b"launcher-native-bytes\x00",
            "bin/runtime": b"runtime-native-bytes\x00",
            "bin/service-runtime": b"service-runtime-native-bytes\x00",
            "resources/config.json": b'{"offline":true}\n',
        }
        for relative, data in self.payloads.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def inventory(self, target: str) -> dict:
        windows = target == "windows-x86_64"
        root = "VibApp" if windows else "vibapp"
        suffix = ".exe" if windows else ""
        definitions = [
            ("launcher", "bin/launcher", f"{root}/{'vibapp-launcher' + suffix if windows else 'bin/vibapp-launcher'}", True),
            ("runtime", "bin/runtime", f"{root}/{'vibapp-runtime' + suffix if windows else 'bin/vibapp-runtime'}", True),
            ("service-runtime", "bin/service-runtime", f"{root}/{'libexec/vibapp-service-runtime.exe' if windows else 'libexec/vibapp-service-runtime'}", True),
            ("resource", "resources/config.json", f"{root}/resources/config.json", False),
        ]
        files = []
        for role, source_path, archive_path, executable in definitions:
            data = (self.source / source_path).read_bytes()
            files.append(
                {
                    "role": role,
                    "source_path": source_path,
                    "archive_path": archive_path,
                    "executable": executable,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return {
            "schema_version": MODULE.INPUT_SCHEMA,
            "target": target,
            "package_version": "0.1.0",
            "source_revision": "a" * 40,
            "source_date_epoch": 1700000000,
            "source_root": str(self.source),
            "files": files,
        }

    def write_inventory(self, value: dict, name: str = "input.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def assemble_twice(self, target: str):
        inventory = self.write_inventory(self.inventory(target), f"{target}.json")
        first = self.root / f"{target}-first"
        second = self.root / f"{target}-second"
        first.mkdir()
        second.mkdir()
        receipt_a = MODULE.assemble(inventory, str(first))
        receipt_b = MODULE.assemble(inventory, str(second))
        files_a = {path.name: path.read_bytes() for path in first.iterdir()}
        files_b = {path.name: path.read_bytes() for path in second.iterdir()}
        self.assertEqual(files_a, files_b)
        self.assertEqual(receipt_a, receipt_b)
        self.assertEqual(receipt_a["artifact_state"], "unverified-package-definition-output")
        self.assertEqual(receipt_a["evidence_label"], "definition-output-only")
        self.assertEqual(receipt_a["reproducibility"], "not-established-by-single-assembly")
        self.assertEqual(receipt_a["native_execution"], "not-attested")
        self.assertEqual(receipt_a["native_smoke"], "not-run")
        self.assertEqual(receipt_a["signing"], "not-applied")
        self.assertFalse(receipt_a["release_eligible"])
        self.assertEqual(receipt_a["support_claim"], "not-verified")
        return first, receipt_a

    def test_linux_tar_gz_is_byte_reproducible_and_metadata_fixed(self) -> None:
        output, receipt = self.assemble_twice("linux-x86_64")
        archive_path = output / receipt["archive"]
        raw = archive_path.read_bytes()
        self.assertEqual(raw[:4], b"\x1f\x8b\x08\x00")
        self.assertEqual(struct.unpack("<I", raw[4:8])[0], 1700000000)
        self.assertEqual(raw[9], 255)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            self.assertEqual(names, sorted(names, key=str.encode))
            self.assertTrue(all(member.isfile() for member in members))
            self.assertTrue(all((member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, "root", "root", 1700000000) for member in members))
            self.assertEqual(archive.getmember("vibapp/bin/vibapp-launcher").mode, 0o755)
            self.assertEqual(archive.getmember("vibapp/resources/config.json").mode, 0o644)
            embedded = archive.extractfile("vibapp/PACKAGE-MANIFEST.json")
            assert embedded
            self.assertEqual(embedded.read(), (output / receipt["manifest"]).read_bytes())

    def test_windows_zip_is_byte_reproducible_and_metadata_fixed(self) -> None:
        output, receipt = self.assemble_twice("windows-x86_64")
        archive_path = output / receipt["archive"]
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            self.assertEqual(names, sorted(names, key=str.encode))
            self.assertTrue(all(not name.endswith("/") for name in names))
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in infos))
            self.assertTrue(all(info.date_time == time_tuple(1700000000) for info in infos))
            launcher = archive.getinfo("VibApp/vibapp-launcher.exe")
            resource = archive.getinfo("VibApp/resources/config.json")
            self.assertEqual((launcher.external_attr >> 16) & 0o777, 0o755)
            self.assertEqual((resource.external_attr >> 16) & 0o777, 0o644)
            self.assertEqual(archive.read("VibApp/PACKAGE-MANIFEST.json"), (output / receipt["manifest"]).read_bytes())

    def test_tampered_input_fails_without_output(self) -> None:
        value = self.inventory("linux-x86_64")
        inventory = self.write_inventory(value)
        (self.source / "bin/launcher").write_bytes(b"tampered")
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(MODULE.PackageError, "declared size|digest mismatch"):
            MODULE.assemble(inventory, str(output))
        self.assertEqual(list(output.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_source_is_rejected(self) -> None:
        value = self.inventory("linux-x86_64")
        link = self.source / "resources/link.json"
        link.symlink_to(self.source / "resources/config.json")
        resource = value["files"][-1]
        resource["source_path"] = "resources/link.json"
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(MODULE.PackageError, "symlink or reparse"):
            MODULE.assemble(self.write_inventory(value), str(output))

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_hardlink_source_is_rejected(self) -> None:
        value = self.inventory("linux-x86_64")
        linked = self.source / "bin/linked-launcher"
        os.link(self.source / "bin/launcher", linked)
        value["files"][0]["source_path"] = "bin/linked-launcher"
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(MODULE.PackageError, "non-hardlinked"):
            MODULE.assemble(self.write_inventory(value), str(output))

    def test_path_escape_and_duplicate_archive_path_are_rejected(self) -> None:
        escaped = self.inventory("windows-x86_64")
        escaped["files"][-1]["source_path"] = "../outside"
        with self.assertRaisesRegex(MODULE.PackageError, "invalid source_path"):
            MODULE.validate_input(escaped)
        duplicate = self.inventory("windows-x86_64")
        duplicate["files"][-1]["archive_path"] = duplicate["files"][0]["archive_path"]
        with self.assertRaisesRegex(MODULE.PackageError, "archive paths must be unique"):
            MODULE.validate_input(duplicate)

    def test_declared_total_bound_fails_before_large_reads(self) -> None:
        value = self.inventory("linux-x86_64")
        for entry in value["files"][:3]:
            entry["size_bytes"] = 50 * 1024 * 1024
        with self.assertRaisesRegex(MODULE.PackageError, "total input bytes exceed"):
            MODULE.validate_input(value)

    def test_existing_output_is_never_overwritten(self) -> None:
        inventory = self.write_inventory(self.inventory("windows-x86_64"))
        output = self.root / "output"
        output.mkdir()
        archive = output / "vibapp-0.1.0-windows-x86_64.zip"
        archive.write_bytes(b"keep")
        with self.assertRaisesRegex(MODULE.PackageError, "refusing to overwrite"):
            MODULE.assemble(inventory, str(output))
        self.assertEqual(archive.read_bytes(), b"keep")

    def test_duplicate_input_key_is_rejected(self) -> None:
        path = self.root / "duplicate.json"
        path.write_text('{"schema_version":"one","schema_version":"two"}', encoding="utf-8")
        with self.assertRaisesRegex(MODULE.PackageError, "duplicate JSON key"):
            MODULE.load_json(path)


def time_tuple(epoch: int) -> tuple[int, int, int, int, int, int]:
    import time

    return time.gmtime(epoch)[:6]


if __name__ == "__main__":
    unittest.main()
