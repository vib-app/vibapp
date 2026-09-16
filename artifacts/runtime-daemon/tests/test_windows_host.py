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

@unittest.skipUnless(os.name == "nt", "requires real Windows APIs")
class WindowsHostTests(unittest.TestCase):
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
