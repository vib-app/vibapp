"""Native Windows acceptance; POSIX runners explicitly skip these tests."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "registry-store/tests"))
from test_windows_store import WindowsStoreStorageTests  # native release CI discovers this TestCase too


class BinaryStorageTests(unittest.TestCase):
    def test_verifier_and_store_preserve_binary_crlf_and_ctrl_z(self):
        artifacts = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(artifacts / "app-builder"))
        sys.path.insert(0, str(artifacts / "registry-store"))
        from common import read_bounded, exclusive_write, exclusive_copy
        from local_appstore import _copy_regular, _hash_regular, _read_regular
        import hashlib
        payload = b"MZ\r\n\x1a\x00\xff\ncomponent\r\n"
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            if os.name == "nt":
                from vibapp_daemon.windows_security import protect
                protect(directory, directory=True)
            source = directory / "source.wasm"
            exclusive_write(source, payload)
            self.assertEqual(read_bounded(source, 1024, "binary fixture"), payload)
            copied = directory / "copied.wasm"
            exclusive_copy(source, copied, 1024)
            self.assertEqual(copied.read_bytes(), payload)
            staged = directory / "staged.wasm"
            _copy_regular(source, staged, 1024, "binary fixture")
            self.assertEqual(_read_regular(staged, 1024, "binary fixture"), payload)
            self.assertEqual(_hash_regular(staged, 1024, "binary fixture"), (hashlib.sha256(payload).hexdigest(), len(payload)))

    def test_component_bytes_are_never_crt_text_translated(self):
        from vibapp_daemon.core import _read_regular_nofollow
        payload = b"\x00asm\r\n\x1a\x00binary\r\n\xff"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "component.wasm"
            path.write_bytes(payload)
            self.assertEqual(_read_regular_nofollow(path, 1024), payload)


@unittest.skipUnless(os.name == "nt", "requires real Windows APIs")
class WindowsHostTests(unittest.TestCase):
    def test_production_cleared_helper_environment_supports_verified_tls(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop/scripts/tests"))
        from smoke_packaged_daemon import helper_environment
        environment = helper_environment()
        self.assertEqual(set(environment), {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONDONTWRITEBYTECODE", "SystemRoot", "WINDIR"})
        result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c",
                                 "import ssl;context=ssl.create_default_context();assert context.verify_mode==ssl.CERT_REQUIRED;assert context.check_hostname"],
                                env=environment, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_private_acl_rejects_world_access_and_repairs_explicitly(self):
        from vibapp_daemon import windows_security as security
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            security.protect(directory, directory=True)
            secret = directory / "secret.json"
            secret.write_text("synthetic-canary")
            security.protect(secret)
            security.verify(secret, directory=False)
            descriptor = security.P()
            security.checked(security._from_sddl("D:P(A;;FA;;;WD)", 1, security.ctypes.byref(descriptor), None))
            try:
                security.checked(security._set_file(str(secret), 4 | 0x80000000, descriptor))
                with self.assertRaises(PermissionError):
                    security.verify(secret, directory=False)
            finally:
                security._free(descriptor)
                security.protect(secret)

    def test_public_binary_may_be_world_readable_but_not_world_writable(self):
        from vibapp_daemon import windows_security as security
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "test.exe"
            binary.write_bytes(b"synthetic-non-executable-fixture")
            security.protect(binary)
            descriptor = security.P()
            sid = security.process_sid()
            security.checked(security._from_sddl(f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FR;;;WD)", 1, security.ctypes.byref(descriptor), None))
            try:
                security.checked(security._set_file(str(binary), 4 | 0x80000000, descriptor))
                security.verify(binary, private=False)
                with self.assertRaises(PermissionError):
                    security.verify(binary, private=True)
            finally:
                security._free(descriptor)
                security.protect(binary)

    def test_daemon_named_pipe_readiness_and_control(self):
        from vibapp_daemon.windows_transport import connect
        from vibapp_daemon.windows_security import protect
        from vibapp_daemon.cli import _read_one_line
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            promotion = root / "candidates"
            promotion.mkdir()
            protect(promotion, directory=True)
            runtime = root / "runtime"
            endpoint = runtime / "run/vibappd.sock"
            command = [sys.executable, "-I", "-B", "-c", "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')", str(Path(__file__).resolve().parents[1]), "serve", "--root", str(runtime), "--promotion-root", str(promotion), "--socket", str(endpoint)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        connection = connect(str(endpoint.resolve()), 0.1)
                        connection.close()
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail(f"daemon did not start: {process.poll()}")
                        time.sleep(0.02)
                # Probe/disconnect must not crash the lifecycle owner.
                envelope = {"request_id": "windows-smoke", "idempotency_key": "windows-smoke", "client": "launcher", "subject": "ai.vibapp.test.missing", "command": {"tag": "status", "value": None}}
                payload = {"schema_version": "vibapp.daemon-transport.experimental-v1", "envelope": envelope, "promotion_record": None}
                with connect(str(endpoint.resolve())) as connection:
                    connection.sendall(json.dumps(payload).encode() + b"\n")
                    response = json.loads(_read_one_line(connection))
                    connection.sendall(b"\0")
                self.assertEqual(response["error"]["code"], "not-found")
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=3)
                diagnostic = process.stderr.read(8192).decode(errors="replace")
                process.stderr.close()
                if "Traceback" in diagnostic:
                    self.fail(diagnostic)

if __name__ == "__main__":
    unittest.main()
