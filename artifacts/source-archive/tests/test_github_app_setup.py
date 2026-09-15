"""Offline tests only. Every credential and GitHub reply here is synthetic."""

import base64
import contextlib
from html.parser import HTMLParser
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode


SPEC = importlib.util.spec_from_file_location(
    "github_app_setup", Path(__file__).resolve().parents[1] / "github_app_setup.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def synthetic_record(pem=None):
    return {"id": 1234, "slug": "vibapp-source-test", "owner": {
        "login": "vib-app", "id": 4567, "type": "Organization"},
        "permissions": dict(app.PERMISSIONS), "events": [],
        "pem": pem or "-----BEGIN RSA PRIVATE KEY-----\nTESTCANARY\n-----END RSA PRIVATE KEY-----\n",
        "client_secret": "SYNTHETIC-OAUTH-CANARY", "webhook_secret": "SYNTHETIC-WEBHOOK-CANARY"}


def synthetic_installation():
    return {"id": 7890, "app_id": 1234, "account": {
        "login": "vib-app", "id": 4567, "type": "Organization"},
        "permissions": dict(app.PERMISSIONS), "events": [], "suspended_at": None,
        "repository_selection": "selected"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vibapp-github-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = app.CredentialStore(self.root / "credentials")

    def test_private_atomic_create_and_no_overwrite(self):
        self.store.create("credentials.json", {"synthetic": "CANARY"})
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.store.directory / "credentials.json").stat().st_mode), 0o600)
        with self.assertRaisesRegex(app.SetupError, "already_exists"):
            self.store.create("credentials.json", {"replacement": True})
        self.assertEqual(self.store.read("credentials.json"), {"synthetic": "CANARY"})
        self.assertEqual(list(self.store.directory.glob(".pending-*")), [])

    def test_rejects_credentials_in_checkout(self):
        with self.assertRaisesRegex(app.SetupError, "credentials_in_checkout"):
            app.CredentialStore(app.ROOT / ".forbidden-credential-test")

    def test_rejects_symlink_directory(self):
        link = self.root / "link"
        link.symlink_to(self.store.directory)
        with self.assertRaisesRegex(app.SetupError, "unsafe_store"):
            app.CredentialStore(link)

    def test_rejects_symlink_ancestor(self):
        link = self.root / "link"
        link.symlink_to(self.store.directory)
        with self.assertRaisesRegex(app.SetupError, "unsafe_store"):
            app.CredentialStore(link / "nested")

    def test_rejects_world_readable_directory(self):
        self.store.directory.chmod(0o755)
        with self.assertRaisesRegex(app.SetupError, "unsafe_store"):
            app.CredentialStore(self.store.directory)

    def test_rejects_unsafe_files_and_hardlinks(self):
        self.store.create("credentials.json", {"test": True})
        path = self.store.directory / "credentials.json"
        path.chmod(0o644)
        with self.assertRaisesRegex(app.SetupError, "unsafe_store"):
            self.store.read("credentials.json")
        path.chmod(0o600)
        os.link(path, self.store.directory / "hardlink")
        with self.assertRaisesRegex(app.SetupError, "unsafe_store"):
            self.store.read("credentials.json")

    def test_rejects_symlink_destination(self):
        (self.store.directory / "credentials.json").symlink_to(self.root / "missing")
        with self.assertRaises(app.SetupError):
            self.store.create("credentials.json", {"test": True})
        with self.assertRaises(app.SetupError):
            self.store.read("credentials.json")
        self.assertFalse((self.root / "missing").exists())

    def test_single_server_lock(self):
        first = self.store.lock()
        try:
            with self.assertRaisesRegex(app.SetupError, "already_running"):
                self.store.lock()
        finally:
            os.close(first)

    def test_restarts_resume_registration_without_recreating(self):
        setup = app.Setup(self.store, lambda *args: synthetic_record())
        setup.convert("synthetic-code")
        resumed = app.Setup(self.store)
        self.assertEqual(resumed.phase, "registered")
        self.assertNotIn("client_secret", resumed.record)
        self.assertNotIn("webhook_secret", resumed.record)
        with self.assertRaisesRegex(app.SetupError, "already_attempted"):
            resumed.convert("other-code")

    def test_ambiguous_exchange_is_not_retried_after_restart(self):
        calls = []

        def api(*args):
            calls.append(args)
            raise app.SetupError("github_unreachable")

        setup = app.Setup(self.store, api)
        with self.assertRaises(app.SetupError):
            setup.convert("synthetic-code")
        with self.assertRaisesRegex(app.SetupError, "already_attempted"):
            app.Setup(self.store, api).convert("synthetic-code")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.store.read("credentials.json"))
        self.assertNotIn("synthetic-code", json.dumps(self.store.read("registration-attempt.json")))

    def test_failed_storage_does_not_report_registered(self):
        setup = app.Setup(self.store, lambda *args: synthetic_record())
        original = self.store.create

        def create(name, value):
            if name == "credentials.json":
                raise app.SetupError("store_failed")
            return original(name, value)

        with patch.object(self.store, "create", create), self.assertRaises(app.SetupError):
            setup.convert("synthetic-code")
        self.assertEqual(setup.phase, "recovery")
        self.assertIsNone(setup.record)


class IdentityTests(unittest.TestCase):
    @patch.object(app, "app_jwt", return_value="SYNTHETIC-JWT")
    def test_explicit_build_upgrade_with_existing_key(self, signer):
        record = app.credential_record(synthetic_record())
        remote = {**record, "permissions": app.BUILD_PERMISSIONS}
        installation = {**synthetic_installation(), "permissions": app.BUILD_PERMISSIONS}
        result = app.verify_installation(record, lambda method, path, jwt: remote if path == "/app" else installation)
        self.assertEqual(result["installation_id"], 7890)
        extra = {**remote, "permissions": {**app.BUILD_PERMISSIONS, "secrets": "write"}}
        with self.assertRaises(app.SetupError):
            app.verify_installation(record, lambda *args: extra)

    def test_permissions_identity_and_pem_are_validated(self):
        mutations = [
            ("owner", {"id": 4567, "type": "Organization", "login": "wrong-org"}),
            ("owner", {"id": 4567, "type": "User", "login": "vib-app"}),
            ("id", True), ("id", 0), ("slug", "<script>"),
            ("permissions", {**app.PERMISSIONS, "workflows": "write"}),
            ("permissions", {"contents": "read"}), ("events", ["push"]),
            ("pem", "not-a-key"), ("pem", "x" * 8193),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                record = synthetic_record()
                record[field] = value
                with self.assertRaises(app.SetupError):
                    app.credential_record(record)

    def test_duplicate_json_is_rejected(self):
        with self.assertRaises(app.SetupError):
            json.loads('{"id":1,"id":2}', object_pairs_hook=app.unique_object)

    def test_transport_disallows_repository_writes_or_other_hosts(self):
        for method, path, jwt in [("DELETE", "/repos/vib-app/app", "test"),
                                  ("POST", "/orgs/vib-app/repos", "test"),
                                  ("GET", "https://evil.test/app", "test"),
                                  ("GET", "/app", None),
                                  ("POST", "/app-manifests/../conversions", None)]:
            with self.subTest(path=path), self.assertRaisesRegex(app.SetupError, "disallowed_endpoint"):
                app.github_request(method, path, jwt)

    def test_transport_is_bounded_and_uses_fixed_host(self):
        reply = MagicMock()
        reply.status = 200
        reply.read.return_value = b'{"id":1234}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = reply
        with patch.object(app, "build_opener", return_value=opener) as factory:
            self.assertEqual(app.github_request("GET", "/app", "SYNTHETIC-JWT"), {"id": 1234})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/app")
        self.assertEqual(request.get_header("Authorization"), "Bearer SYNTHETIC-JWT")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        reply.read.assert_called_once_with(app.MAX_BYTES + 1)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsNone(factory.call_args.args[1].redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_transport_rejects_oversized_and_duplicate_json(self):
        for raw in (b"x" * (app.MAX_BYTES + 1), b'{"id":1,"id":2}', b"invalid-json"):
            reply = MagicMock()
            reply.status = 200
            reply.read.return_value = raw
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = reply
            with patch.object(app, "build_opener", return_value=opener), self.assertRaises(app.SetupError):
                app.github_request("GET", "/app", "SYNTHETIC-JWT")

    def test_transport_errors_do_not_reflect_credentials_or_response_body(self):
        error = app.HTTPError("https://api.github.com/app-manifests/SECRET-CODE/conversions", 422,
                              "SYNTHETIC-SECRET-ERROR", {}, io.BytesIO(b"SYNTHETIC-SECRET-BODY"))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(app, "build_opener", return_value=opener):
            with self.assertRaises(app.SetupError) as caught:
                app.github_request("POST", "/app-manifests/SECRET-CODE/conversions")
        self.assertEqual(str(caught.exception), "github_rejected")

    @patch.object(app, "app_jwt", return_value="SYNTHETIC-JWT")
    def test_installed_identity_scope_and_revocation(self, signer):
        record = app.credential_record(synthetic_record())
        result = app.verify_installation(record, lambda method, path, jwt:
                                         record if path == "/app" else synthetic_installation())
        self.assertEqual(result["installation_id"], 7890)
        self.assertEqual(result["repository_selection"], "selected")
        for field, value in [("app_id", 999), ("id", True), ("account", {}),
                             ("suspended_at", "2026-01-01"), ("permissions", {}),
                             ("repository_selection", "nonsense"), ("events", ["push"])]:
            installation = synthetic_installation()
            installation[field] = value
            with self.subTest(field=field), self.assertRaises(app.SetupError):
                app.verify_installation(record, lambda method, path, jwt:
                                        record if path == "/app" else installation)

    def test_openssl_jwt_signature_and_claims(self):
        # Ephemeral test-only RSA key; no real GitHub key is read or printed.
        generated = subprocess.run(["/usr/bin/openssl", "genrsa", "2048"],
                                   capture_output=True, check=True, timeout=15)
        record = app.credential_record(synthetic_record(generated.stdout.decode()))
        jwt = app.app_jwt(record, now=1_800_000_000)
        header, body, signature = jwt.split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        self.assertEqual(claims, {"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "1234"})
        public = subprocess.run(["/usr/bin/openssl", "rsa", "-pubout"],
                                input=generated.stdout, capture_output=True, check=True, timeout=5)
        with tempfile.TemporaryFile() as public_fd, tempfile.TemporaryFile() as signature_fd:
            public_fd.write(public.stdout)
            public_fd.seek(0)
            signature_fd.write(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
            signature_fd.seek(0)
            checked = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify",
                                      f"/dev/fd/{public_fd.fileno()}", "-signature",
                                      f"/dev/fd/{signature_fd.fileno()}"], input=f"{header}.{body}".encode(),
                                     pass_fds=(public_fd.fileno(), signature_fd.fileno()),
                                     capture_output=True, timeout=5)
        self.assertEqual(checked.returncode, 0)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vibapp-github-http-")
        self.addCleanup(self.temp.cleanup)
        self.store = app.CredentialStore(Path(self.temp.name).resolve() / "credentials")
        self.calls = []

        def api(method, path, jwt=None):
            self.calls.append((method, path))
            return synthetic_installation() if path.endswith("/installation") else synthetic_record()

        self.setup = app.Setup(self.store, api)
        self.server = app.make_server(self.setup)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.cookie = f"{app.COOKIE}={self.setup.session}"

    def request(self, path, method="GET", headers=None, body=None, authenticated=True):
        values = {"Cookie": self.cookie} if authenticated else {}
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def callback(self, **overrides):
        params = {"state": self.setup.state, "code": "SYNTHETIC-CODE"}
        params.update(overrides)
        return "/callback?" + urlencode(params)

    def test_health_means_only_local_setup(self):
        code, headers, body = self.request("/health", authenticated=False)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "scope": "local-setup-only"})
        self.assertEqual(self.calls, [])

    def test_entry_capability_and_cookie(self):
        self.assertEqual(self.request("/", authenticated=False)[0], 403)
        self.assertEqual(self.request("/start?token=wrong", authenticated=False)[0], 403)
        code, headers, body = self.request(f"/start?token={self.setup.entry_token}", authenticated=False)
        self.assertEqual(code, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        self.assertEqual(headers["Location"], "/")

    def test_ready_page_and_manifest(self):
        code, headers, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("前往 GitHub 确认创建", body)
        manifest = self.setup.manifest()
        self.assertFalse(manifest["public"])
        self.assertFalse(manifest["request_oauth_on_install"])
        self.assertFalse(manifest["hook_attributes"]["active"])
        self.assertEqual(manifest["default_permissions"], app.PERMISSIONS)
        self.assertEqual(manifest["default_events"], [])
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_submitted_manifest_has_no_webhook_receiver_but_keeps_local_redirects(self):
        # Inspect the serialized HTML form submitted to GitHub, not just the
        # internal dict. A disabled loopback hook was still rejected by GitHub.
        class FormParser(HTMLParser):
            manifest = None
            action = None

            def handle_starttag(self, tag, attributes):
                values = dict(attributes)
                if tag == "form":
                    self.action = values.get("action")
                if tag == "input" and values.get("name") == "manifest":
                    self.manifest = json.loads(values["value"])

        parser = FormParser()
        parser.feed(self.request("/")[2])
        self.assertEqual(parser.manifest["hook_attributes"], {"url": "", "active": False})
        self.assertEqual(parser.manifest["default_events"], [])
        self.assertEqual(parser.manifest["redirect_url"], self.setup.origin + "/callback")
        self.assertEqual(parser.manifest["setup_url"], self.setup.origin + "/installed")
        self.assertEqual(parser.action,
                         f"https://github.com/organizations/vib-app/settings/apps/new?state={self.setup.state}")
        self.assertEqual(self.calls, [])

    def test_sensitive_redirects_keep_no_referrer(self):
        for path in (f"/start?token={self.setup.entry_token}",
                     self.callback(), "/installed?installation_id=123",
                     "/?code=SYNTHETIC-CODE&state=SYNTHETIC-STATE"):
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                self.assertEqual(code, 303)
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Location"], "/")
                self.assertNotIn("SYNTHETIC-CODE", body)

    def test_null_foreign_and_missing_origins_remain_rejected(self):
        self.request(self.callback())
        body = urlencode({"csrf": self.setup.csrf})
        for origin in (None, "null", "https://evil.test", "http://localhost:1234"):
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if origin is not None:
                headers["Origin"] = origin
            with self.subTest(origin=origin):
                self.assertEqual(self.request("/check", "POST", headers, body)[0], 403)
        self.assertEqual(len(self.calls), 1)

    def test_dns_rebinding_expiry_and_oversized_queries(self):
        self.assertEqual(self.request("/", headers={"Host": "evil.test"})[0], 403)
        self.assertEqual(self.request("/callback?code=a&code=b")[0], 400)
        self.assertEqual(self.request("/" + "a" * 4096)[0], 403)
        self.setup.deadline = time.monotonic() - 1
        self.assertEqual(self.request("/")[0], 410)
        self.assertEqual(self.calls, [])

    def test_rejects_invalid_state_missing_cookie_and_bad_codes(self):
        self.assertEqual(self.request(self.callback(state="wrong"))[0], 403)
        self.assertEqual(self.request(self.callback(), authenticated=False)[0], 403)
        self.assertEqual(self.request(self.callback(code="../other"))[0], 403)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.read("registration-attempt.json"))

    def test_valid_callback_captures_once_and_never_displays_secrets(self):
        log = io.StringIO()
        with contextlib.redirect_stderr(log), contextlib.redirect_stdout(log):
            code, headers, body = self.request(self.callback())
            self.assertEqual(code, 303)
            self.assertEqual(headers["Location"], "/")
            page = self.request("/")[2]
            self.request(self.callback())
        self.assertEqual(len(self.calls), 1)
        for canary in ("TESTCANARY", "SYNTHETIC-OAUTH-CANARY", "SYNTHETIC-WEBHOOK-CANARY", "SYNTHETIC-CODE"):
            self.assertNotIn(canary, body + page + log.getvalue())
        self.assertEqual(self.setup.phase, "registered")

    def test_install_callback_does_not_trust_browser_installation_id(self):
        self.request("/installed?installation_id=123&setup_action=install")
        self.assertIsNone(self.setup.result)
        self.assertEqual(self.calls, [])

    @patch.object(app, "app_jwt", return_value="SYNTHETIC-JWT")
    def test_check_requires_origin_csrf_and_queries_github(self, signer):
        self.request(self.callback())
        body = urlencode({"csrf": self.setup.csrf})
        headers = {"Origin": self.setup.origin, "Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.request("/check", "POST", body=body)[0], 403)
        self.assertEqual(self.request("/check", "POST", headers, "csrf=wrong")[0], 403)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.request("/check", "POST", headers, body)[0], 303)
        self.assertEqual(self.setup.phase, "verified")
        self.assertEqual(self.calls[-2:], [("GET", "/app"), ("GET", "/orgs/vib-app/installation")])
        self.assertIn("尚未自动上传任何源码", self.request("/")[2])

    @patch.object(app, "app_jwt", return_value="SYNTHETIC-JWT")
    def test_revoked_installation_clears_previous_success(self, signer):
        self.request(self.callback())
        self.setup.check()
        self.assertIsNotNone(self.setup.result)

        def failing_api(*args):
            raise app.SetupError("github_not_found")

        self.setup.api = failing_api
        with self.assertRaises(app.SetupError):
            self.setup.check()
        self.assertIsNone(self.setup.result)
        self.assertEqual(self.setup.phase, "registered")
        self.assertNotIn("GitHub 已确认", self.request("/")[2])


if __name__ == "__main__":
    unittest.main()
