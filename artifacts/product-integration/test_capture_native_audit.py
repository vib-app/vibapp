"""Synthetic-only protection tests; all GUI, OS signals and control calls mocked."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

import capture_native_audit as capture


def chunk(kind, value):
    return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff)


def synthetic_png(width=16, height=16, raw=None):
    # Deliberately synthetic format fixture, never a captured app image.
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    if raw is None:
        raw = (b"\0" + b"\x10\x20\x30\xff" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def window(wid=77, pid=12345, title="Synthetic — VibApp"):
    return {"id": wid, "owner_pid": pid, "title": title, "owner": "VibApp", "width": 520, "height": 300}


class WindowBindingTests(unittest.TestCase):
    def test_same_title_foreign_pid_is_never_selected(self):
        with self.assertRaisesRegex(RuntimeError, "owned by"):
            capture.select_window({}, {77: window(pid=54321)}, "Synthetic — VibApp", 12345)
        self.assertEqual(capture.select_window({}, {77: window(pid=54321), 78: window(78)},
                                               "Synthetic — VibApp", 12345)["id"], 78)

    def test_old_or_ambiguous_owned_windows_are_rejected(self):
        for before, current in [({77: window()}, {77: window()}),
                                ({}, {77: window(), 78: window(78)})]:
            with self.assertRaises(RuntimeError):
                capture.select_window(before, current, "Synthetic — VibApp", 12345)

    def test_window_metadata_requires_typed_owner_pid_and_unique_ids(self):
        payload = {"schema_version": "vibapp.native-window-metadata-v1", "windows": [window()]}
        with patch.object(capture, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload).encode())) as run:
            self.assertEqual(capture.windows(Path("/synthetic-cache"))[77]["owner_pid"], 12345)
            self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/swift", "-module-cache-path", "/synthetic-cache"])
        invalid = []
        missing = window()
        missing.pop("owner_pid")
        invalid.append([missing])
        for pid in (True, "12345", 0, -1):
            invalid.append([window(pid=pid)])
        invalid.append([window(), window()])
        for rows in invalid:
            payload["windows"] = rows
            with patch.object(capture, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload).encode())):
                with self.assertRaises(ValueError):
                    capture.windows(Path("/synthetic-cache"))

    def test_window_metadata_rejects_duplicate_json_keys_and_oversize(self):
        for data in (b'{"schema_version":"a","schema_version":"b","windows":[]}', b"x" * (1024 * 1024 + 1)):
            with patch.object(capture, "run", return_value=subprocess.CompletedProcess([], 0, data)):
                with self.assertRaises(ValueError):
                    capture.windows(Path("/synthetic-cache"))

    def test_pid_start_time_and_window_owner_are_rechecked(self):
        item = {"pid": 12345, "process_identity": "exact start-time", "window_id": 77, "window_metadata": window()}
        with patch.object(capture, "process_rows", return_value={12345: "reused start-time"}), patch.object(capture, "windows") as windows:
            with self.assertRaisesRegex(RuntimeError, "PID/start-time"):
                capture.current_window(item, None)
            windows.assert_not_called()
        for current in ({}, {77: window(pid=54321)}, {77: window(title="Different")}):
            with patch.object(capture, "process_rows", return_value={12345: "exact start-time"}), \
                 patch.object(capture, "windows", return_value=current):
                with self.assertRaisesRegex(RuntimeError, "ownership"):
                    capture.current_window(item, None)

    def test_subprocess_environment_cannot_enable_synthetic_screenshots(self):
        with patch.dict(os.environ, {"CODEX_SCREENSHOT_TEST_MODE": "1", "PYTHONPATH": "/synthetic-untrusted"}), \
             patch.object(capture.subprocess, "run", return_value=object()) as run:
            capture.run(["/synthetic-command"])
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"})

    def test_local_swift_helper_has_only_read_only_os_window_access(self):
        source = capture.WINDOW_HELPER.read_text()
        for required in ("CGPreflightScreenCaptureAccess()", "CGWindowListCopyWindowInfo", "kCGWindowOwnerPID"):
            self.assertIn(required, source)
        for forbidden in ("CGRequestScreenCaptureAccess(", "CGEvent(", "AXUIElement", "NSWorkspace", "osascript"):
            self.assertNotIn(forbidden, source)


class PngEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-png-unit-")
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "synthetic-format-fixture.png"

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_png_binds_exact_bytes_dimensions_and_path(self):
        data = synthetic_png()
        self.path.write_bytes(data)
        self.assertEqual(capture.png_evidence(self.path), {
            "path": str(self.path), "width": 16, "height": 16,
            "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        })

    def test_missing_symlink_and_hardlink_images_are_rejected(self):
        with self.assertRaises(FileNotFoundError):
            capture.png_evidence(self.path)
        self.path.write_bytes(synthetic_png())
        alias = self.root / "alias.png"
        alias.symlink_to(self.path)
        with self.assertRaises(ValueError):
            capture.png_evidence(alias)
        alias.unlink()
        os.link(self.path, alias)
        with self.assertRaises(ValueError):
            capture.png_evidence(self.path)

    def test_non_png_truncated_crc_and_trailing_bytes_are_rejected(self):
        valid = synthetic_png()
        corrupt = bytearray(valid)
        corrupt[29] ^= 1
        for data in (b"not a PNG" * 20, valid[:-1], bytes(corrupt), valid + b"trailing"):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                capture.png_evidence(self.path)

    def test_invalid_deflate_size_and_scanline_filter_are_rejected(self):
        for raw in (b"", b"bad image", (b"\5" + b"\0" * 64) * 16, b"\0" * 2000):
            self.path.write_bytes(synthetic_png(raw=raw))
            with self.assertRaises(ValueError):
                capture.png_evidence(self.path)
        header = struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0)
        self.path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"broken zlib") + chunk(b"IEND", b""))
        with self.assertRaisesRegex(ValueError, "compressed image"):
            capture.png_evidence(self.path)

    def test_tiny_oversized_dimensions_or_decompression_bomb_are_rejected(self):
        for width, height in ((1, 1), (16385, 16), (16384, 16384)):
            self.path.write_bytes(synthetic_png(width, height, raw=b"\0"))
            with self.assertRaises(ValueError):
                capture.png_evidence(self.path)

    def test_unexpected_output_paths_fail_without_reading_them(self):
        for output in (b"", b"/somewhere-else.png\n", (str(self.path) + "\n/extra.png\n").encode()):
            with patch.object(capture, "run", return_value=subprocess.CompletedProcess([], 0, output)), \
                 patch.object(capture, "png_evidence") as png:
                with self.assertRaisesRegex(ValueError, "expected output path"):
                    capture.capture_png(77, self.path)
                png.assert_not_called()

    def test_matching_output_path_requires_actual_file_and_never_overwrites(self):
        with patch.object(capture, "run", return_value=subprocess.CompletedProcess([], 0, (str(self.path) + "\n").encode())):
            with self.assertRaises(FileNotFoundError):
                capture.capture_png(77, self.path)
        self.path.write_bytes(synthetic_png())
        before = self.path.read_bytes()
        with patch.object(capture, "run") as run:
            with self.assertRaisesRegex(ValueError, "overwrite"):
                capture.capture_png(77, self.path)
            run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)


@unittest.skipUnless(Path("/private/tmp").is_dir(), "macOS private-root contract")
class CaptureFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-capture-unit-", dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        (self.root / "settings").mkdir()
        (self.root / "settings/p2p-network.json").write_text(json.dumps({"p2p_enabled": False, "rtc_enabled": False, "turn_enabled": False}))
        self.bundle = self.root / "Synthetic.app"
        self.launcher = self.bundle / "Contents/MacOS/vibapp-launcher"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"synthetic nonexecutable test data")
        self.row = {"app_id": "ai.vibapp.synthetic-unit", "display_name": "Synthetic", "package_digest_sha256": "a" * 64}
        self.preparation = self.root / "preparation.json"
        self.preparation.write_text(json.dumps({"product_root": str(self.root), "status": "PASS", "user_data_copied": False,
                                                "model_or_auth_configured": False, "apps": [self.row]}))
        self.evidence = self.root / "new-evidence"
        self.identity = f"12345 exact start-time {self.launcher} --data-dir {self.root} --open-app {self.row['app_id']}"
        self.alive = False
        self.launched = False
        self.surface_open = False
        self.owner_pid = 12345
        self.mode = "valid"
        self.calls = []
        self.signals = []

    def tearDown(self):
        self.temporary.cleanup()

    def fake_run(self, arguments, **kwargs):
        self.calls.append(arguments)
        output = b""
        if arguments[0] == "/usr/bin/open":
            self.alive = self.launched = self.surface_open = True
        if "--window-id" in arguments:
            destination = Path(arguments[arguments.index("--path") + 1])
            if self.mode != "missing":
                destination.write_bytes(synthetic_png())
            output = (str(destination) + "\n").encode()
            if self.mode == "owner-changed-after-capture":
                self.owner_pid = 54321
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    def fake_control(self, _bundle, _root, app_id, tag, value=None):
        self.assertEqual(app_id, self.row["app_id"])
        self.assertEqual(_root, self.root)
        if tag == "surface-close":
            self.surface_open = False
        return {"outcome": {"value": {"app": app_id, "enabled": True, "package_digest_sha256": "a" * 64,
                "surfaces": [{"session": "synthetic-session", "surface": "synthetic-surface",
                              "state": "open" if self.surface_open else "closed"}]}}}

    def fake_kill(self, pid, signal):
        self.signals.append((pid, signal))
        self.assertEqual(pid, 12345)
        self.alive = False

    def invoke(self):
        with patch.object(capture, "run", side_effect=self.fake_run), \
             patch.object(capture, "process_rows", side_effect=lambda: {12345: self.identity} if self.alive else {}), \
             patch.object(capture, "windows", side_effect=lambda _: {77: window(pid=self.owner_pid)} if self.launched else {}), \
             patch.object(capture, "control", side_effect=self.fake_control), \
             patch.object(capture.os, "kill", side_effect=self.fake_kill), \
             patch.object(capture.time, "sleep"), patch("builtins.print"):
            capture.capture(self.preparation, self.bundle, self.evidence, [])

    def test_mock_capture_records_owner_before_after_and_exact_png_bytes(self):
        self.invoke()
        report = json.loads((self.evidence / "report.json").read_bytes())
        item = report["apps"][0]
        self.assertEqual(report["status"], "CAPTURED_AWAITING_VISUAL_REVIEW")
        self.assertEqual(report["accepted_screenshots"], 0)
        self.assertFalse(item["visually_accepted"])
        self.assertEqual(item["window_before_capture"]["owner_pid"], item["pid"])
        self.assertEqual(item["window_after_capture"]["owner_pid"], item["pid"])
        self.assertEqual(item["screenshot_evidence"]["sha256"], hashlib.sha256(Path(item["screenshot"]).read_bytes()).hexdigest())
        self.assertTrue(item["cleanup"])
        self.assertEqual(len(self.signals), 1)  # mocked, never a real OS signal

    def test_foreign_owner_fails_before_any_screenshot_call(self):
        self.owner_pid = 54321
        with self.assertRaisesRegex(RuntimeError, "owned by"):
            self.invoke()
        self.assertFalse(any("--window-id" in call for call in self.calls))
        self.assertEqual(json.loads((self.evidence / "report.json").read_bytes())["status"], "FAIL")

    def test_missing_image_never_becomes_captured(self):
        self.mode = "missing"
        with self.assertRaises(FileNotFoundError):
            self.invoke()
        report = json.loads((self.evidence / "report.json").read_bytes())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["apps"][0]["status"], "FAIL")
        self.assertIsNone(report["apps"][0]["screenshot"])

    def test_owner_change_during_capture_keeps_file_but_fails_evidence(self):
        self.mode = "owner-changed-after-capture"
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            self.invoke()
        report = json.loads((self.evidence / "report.json").read_bytes())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["apps"][0]["status"], "FAIL")
        self.assertIsNone(report["apps"][0]["screenshot"])
        self.assertEqual(len(list(self.evidence.glob("*.png"))), 1)

    def test_existing_evidence_is_never_overwritten(self):
        self.evidence.mkdir()
        marker = self.evidence / "report.json"
        marker.write_text("old synthetic evidence")
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(marker.read_text(), "old synthetic evidence")
        self.assertFalse(any(call[0] == "/usr/bin/open" for call in self.calls))


if __name__ == "__main__":
    unittest.main()
