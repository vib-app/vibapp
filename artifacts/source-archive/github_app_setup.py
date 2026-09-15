#!/usr/bin/env python3
"""Owner-operated, loopback-only GitHub App setup. Not a source upload API.

No PAT, user OAuth, repository writes, installation tokens, or publication.
Credentials are stored outside the checkout; HTTP/log output is an allowlist.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import html
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ORG = "vib-app"
PERMISSIONS = {"administration": "write", "contents": "write", "metadata": "read"}
# Explicitly owner-approved 2026-09-06 expansion for the trusted cloud builder.
# Source-only installations remain supported; callers request only their role's
# repository-scoped permissions when minting installation tokens.
BUILD_PERMISSIONS = {**PERMISSIONS, "actions": "write", "workflows": "write"}
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = Path.home() / "Library/Application Support/VibApp/source-archive/github"
COOKIE = "vibapp_github_setup"
MAX_BYTES = 1024 * 1024


class SetupError(Exception):
    """Only fixed, non-secret error codes may cross the presentation boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("invalid_response")
        result[key] = value
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_request(method: str, path: str, jwt: str | None = None):
    # Fixed host and endpoint allowlist: credentials cannot follow redirects or
    # inherited HTTP(S)_PROXY settings. No API that writes repository data exists.
    conversion = re.fullmatch(r"/app-manifests/[A-Za-z0-9_-]{1,256}/conversions", path)
    if not ((method == "POST" and conversion and jwt is None) or
            (method == "GET" and path in ("/app", f"/orgs/{ORG}/installation") and jwt)):
        raise SetupError("disallowed_endpoint")
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "VibApp-GitHub-Setup"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    request = Request("https://api.github.com" + path, method=method, headers=headers,
                      data=b"" if method == "POST" else None)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            if response.status != (201 if method == "POST" else 200):
                raise SetupError("github_response")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise SetupError("response_too_large")
        return json.loads(raw, object_pairs_hook=unique_object)
    except HTTPError as exc:
        code = "github_not_found" if exc.code == 404 else "github_rejected"
        exc.close()
        raise SetupError(code) from None
    except (URLError, TimeoutError, OSError):
        raise SetupError("github_unreachable") from None
    except (ValueError, UnicodeError):
        raise SetupError("invalid_response") from None


def positive_id(value):
    return type(value) is int and 0 < value < 2 ** 63


def app_record(value):
    if not isinstance(value, dict):
        raise SetupError("invalid_response")
    owner = value.get("owner")
    if not isinstance(owner, dict) or owner.get("login", "").lower() != ORG or \
            owner.get("type") != "Organization" or not positive_id(owner.get("id")):
        raise SetupError("wrong_organization")
    if not positive_id(value.get("id")) or not isinstance(value.get("slug"), str) or \
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value["slug"]):
        raise SetupError("invalid_response")
    if value.get("permissions") not in (PERMISSIONS, BUILD_PERMISSIONS) or value.get("events", []) != []:
        raise SetupError("unexpected_permissions")
    return {"id": value["id"], "slug": value["slug"],
            "owner": {"login": ORG, "id": owner["id"], "type": "Organization"},
            "permissions": dict(value["permissions"]), "events": []}


def credential_record(value):
    record = app_record(value)
    pem = value.get("pem")
    if not isinstance(pem, str) or len(pem) > 8192 or not re.fullmatch(
            r"-----BEGIN (RSA )?PRIVATE KEY-----\n[A-Za-z0-9+/=\r\n]+"
            r"-----END (RSA )?PRIVATE KEY-----\n?", pem):
        raise SetupError("invalid_private_key")
    record["pem"] = pem
    return record


class CredentialStore:
    """Private, non-symlink storage with atomic, non-overwriting creation."""

    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        resolved = self.directory.resolve()
        if resolved == ROOT or ROOT in resolved.parents:
            raise SetupError("credentials_in_checkout")
        # Reject symlink ancestors (including the managed directory) before mkdir.
        for parent in (self.directory, *self.directory.parents):
            if parent.is_symlink():
                raise SetupError("unsafe_store")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or \
                stat.S_IMODE(info.st_mode) != 0o700:
            raise SetupError("unsafe_store")

    def read(self, name: str):
        try:
            fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise SetupError("unsafe_store") from None
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or \
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise SetupError("unsafe_store")
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SetupError("unsafe_store")
        try:
            return json.loads(data, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            raise SetupError("unsafe_store") from None

    def create(self, name: str, value):
        payload = json.dumps(value, ensure_ascii=True).encode()
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as sink:
                os.fchmod(sink.fileno(), 0o600)
                sink.write(payload)
                sink.flush()
                os.fsync(sink.fileno())
            # link fails if destination exists, even if it is a dangling symlink.
            os.link(temporary, self.directory / name, follow_symlinks=False)
        except FileExistsError:
            raise SetupError("already_exists") from None
        except OSError:
            raise SetupError("store_failed") from None
        finally:
            os.unlink(temporary)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def lock(self):
        fd = os.open(self.directory / "setup.lock",
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or \
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            os.close(fd)
            raise SetupError("unsafe_store")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SetupError("already_running") from None
        return fd


def app_jwt(record, now=None):
    now = int(time.time()) if now is None else now

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    message = encode(b'{"alg":"RS256","typ":"JWT"}') + b"." + encode(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": str(record["id"])},
        separators=(",", ":")).encode())
    # Unlinked temporary file / inherited FD: no PEM in argv, env, or a named file.
    with tempfile.TemporaryFile() as key:
        key.write(record["pem"].encode())
        key.seek(0)
        try:
            result = subprocess.run(
                ["/usr/bin/openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{key.fileno()}"],
                input=message, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(key.fileno(),), timeout=5, check=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError("signing_failed") from None
    if result.returncode != 0 or not 256 <= len(result.stdout) <= 1024:
        raise SetupError("signing_failed")
    return (message + b"." + encode(result.stdout)).decode("ascii")


def verify_installation(record, api=github_request):
    jwt = app_jwt(record)
    remote_app = app_record(api("GET", "/app", jwt))
    stored_app = app_record(record)
    # Stored key metadata can predate an explicitly supported permission upgrade.
    # Never weaken app/organization identity or accept arbitrary added scopes.
    if any(remote_app[k] != stored_app[k] for k in ("id", "slug", "owner", "events")):
        raise SetupError("app_identity_changed")
    value = api("GET", f"/orgs/{ORG}/installation", jwt)
    if not isinstance(value, dict):
        raise SetupError("invalid_response")
    account = value.get("account", {})
    if not isinstance(account, dict) or account.get("id") != record["owner"]["id"] or \
            account.get("login", "").lower() != ORG or account.get("type") != "Organization" or \
            value.get("app_id") != record["id"] or not positive_id(value.get("id")):
        raise SetupError("wrong_installation")
    if value.get("suspended_at") is not None:
        raise SetupError("installation_suspended")
    if value.get("permissions") != remote_app["permissions"] or value.get("events", []) != []:
        raise SetupError("unexpected_permissions")
    selection = value.get("repository_selection")
    if selection not in ("selected", "all"):
        raise SetupError("invalid_response")
    return {"app_id": record["id"], "installation_id": value["id"], "organization": ORG,
            "repository_selection": selection, "checked_at": int(time.time())}


ERRORS = {
    "github_not_found": "GitHub 尚未找到有效安装。请先确认已安装到 vib-app，然后再检查。",
    "github_unreachable": "暂时无法连接 GitHub。若已点击创建，请勿重复创建；查看下方恢复说明。",
    "github_rejected": "GitHub 拒绝了请求。请检查组织权限或现有 App 配置；不要重复创建。",
    "unexpected_permissions": "权限与预定配置不一致。请检查 GitHub App 权限后再继续。",
    "installation_suspended": "此 GitHub App 安装已暂停，需要组织管理员恢复。",
    "wrong_organization": "App 不属于 vib-app 组织，未连接。请检查 GitHub 中的所有者。",
}


class Setup:
    def __init__(self, store, api=github_request, lifetime=3600):
        self.store, self.api = store, api
        self.entry_token = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.state = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + lifetime
        self.record = store.read("credentials.json")
        if self.record:
            self.record = credential_record(self.record)
        self.phase = "registered" if self.record else (
            "recovery" if store.read("registration-attempt.json") else "ready")
        self.result = None
        self.error = None
        self.origin = ""

    def manifest(self):
        return {"name": "VibApp Source Bot", "url": "https://github.com/vib-app",
                "description": "Private source archive for VibApp-generated applications.",
                "public": False, "default_permissions": dict(PERMISSIONS),
                "default_events": [], "request_oauth_on_install": False,
                # GitHub validates a supplied webhook URL even when inactive.
                # This App needs no webhooks: leave it blank, not loopback and
                # not a made-up public receiver. Browser redirects remain local.
                "hook_attributes": {"url": "", "active": False},
                "redirect_url": self.origin + "/callback", "setup_url": self.origin + "/installed"}

    def convert(self, code):
        if self.phase != "ready":
            raise SetupError("already_attempted")
        # Durable marker prevents another conversion/registration after ambiguous
        # network failure or process crash. Never persist the one-time code.
        self.store.create("registration-attempt.json", {"started_at": int(time.time()), "org": ORG})
        self.phase = "recovery"
        record = credential_record(self.api("POST", f"/app-manifests/{code}/conversions"))
        self.store.create("credentials.json", record)
        self.record = record
        self.phase = "registered"

    def check(self):
        if not self.record:
            raise SetupError("not_registered")
        self.result = None
        self.error = None
        self.phase = "registered"
        # No trust in installation_id/setup_action provided by browser redirects.
        self.result = verify_installation(self.record, self.api)
        self.phase = "verified"


STYLE = """
:root{color-scheme:dark;font:16px/1.65 system-ui,sans-serif;background:#101713;color:#ecf3ee}
body{margin:0;padding:40px 20px}main{max-width:720px;margin:auto}h1{font-size:32px;line-height:1.25}
.tag{color:#78d9a9;font-size:13px;letter-spacing:2px}.box{background:#19251d;border:1px solid #304537;
border-radius:18px;padding:24px;margin:24px 0}h2{font-size:20px;margin-top:0}p{margin:12px 0}
button,.button{display:inline-block;background:#8be0b4;color:#10291b;padding:12px 20px;
border:0;border-radius:10px;font:inherit;font-weight:600;text-decoration:none;cursor:pointer}
button:focus-visible,a:focus-visible{outline:3px solid #fff;outline-offset:4px}
a{color:#a9e6c5}.muted{color:#a6b9ac;font-size:14px}.warning{color:#f0cc87}
code{overflow-wrap:anywhere}li{margin:8px 0}@media(max-width:500px){body{padding:24px 14px}.box{padding:18px}}
"""


def render(setup):
    esc = html.escape
    if setup.phase == "ready":
        manifest = esc(json.dumps(setup.manifest()), quote=True)
        content = f"""<h2>1 · 在 GitHub 创建专用身份</h2>
<p>名称预填为 <strong>VibApp Source Bot</strong>，所有者为 <strong>vib-app</strong>。
这是仅供本组织安装的私有 GitHub App，不是你的个人访问令牌。</p>
<form method="post" action="https://github.com/organizations/{ORG}/settings/apps/new?state={setup.state}">
<input type="hidden" name="manifest" value="{manifest}">
<button type="submit">前往 GitHub 确认创建</button></form>
<p class="muted">请使用本机浏览器，登录具有组织管理权限的账号。名称被占用时可在 GitHub 修改。</p>"""
    elif setup.record:
        slug = setup.record["slug"]
        content = f"""<h2>1 · GitHub App 已创建，密钥已安全接收</h2>
<p><code>{esc(slug)}</code> · App ID {setup.record['id']}</p>
<h2>2 · 安装到 vib-app 组织</h2>
<p>建议选 <strong>Only select repositories</strong>，只授权专用仓库。
若页面要求至少选一个，请选专用测试仓库，不要为方便改成全选。</p>
<a class="button" rel="noreferrer" href="https://github.com/apps/{slug}/installations/new">前往 GitHub 确认安装</a>
<h2>3 · 返回这里检查安装</h2>
<form method="post" action="/check"><input type="hidden" name="csrf" value="{setup.csrf}">
<button type="submit">检查组织安装和权限</button></form>"""
        if setup.result:
            content += "<p>✓ GitHub 已确认：组织安装、App 身份、签名密钥及权限有效。</p>"
            if setup.result["repository_selection"] == "all":
                content += '<p class="warning">当前授权了全部仓库，权限范围较大。建议在 GitHub 改为仅指定仓库。</p>'
            content += "<p>下一步才是接入归档服务并试推一个私有测试仓库；尚未自动上传任何源码。</p>"
    else:
        content = """<h2>创建流程需要人工核对</h2><p>上次密钥交换未确认完成，已停止自动重试，避免重复创建 App。</p>
<p>请检查 GitHub 组织设置中的现有 GitHub Apps。若已创建但密钥未保存，需要为该 App 重新生成密钥并安全导入；
不要将密钥粘贴到聊天中。本向导当前不提供手工密钥导入。</p>"""
    error = ""
    if setup.error:
        message = ERRORS.get(setup.error, "设置未完成，凭据未向页面或日志输出。请检查本机设置工具的恢复说明。")
        error = f'<p class="warning">{message}（{esc(setup.error)}）</p>'
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VibApp · 源码归档身份设置</title><link rel="stylesheet" href="/style.css">
<main><div class="tag">VIBAPP / LOCAL SETUP</div><h1>给应用源码一个专用的 GitHub 身份</h1>
<p>你在 GitHub 确认授权；VibApp 接收密钥。无需复制令牌，也不使用开发者的个人账号推送。</p>
<section class="box">{error}{content}</section>
<section class="box"><h2>这次授权什么？</h2><ul>
<li><strong>Contents · 读写</strong>：将来用于写入应用源码。</li>
<li><strong>Administration · 读写</strong>：将来用于创建仓库。GitHub 同时赋予仓库管理能力，
包括删除等高权限操作；本工具没有实现这些操作。</li>
<li><strong>Metadata · 只读</strong>：识别仓库。不开启 Webhooks，不申请用户 OAuth、Actions 或 Workflows 权限。</li>
</ul><p class="muted">默认方案是私有源码仓库。私有 GitHub App 不等于自动创建私有仓库；
后续归档服务仍须显式设置仓库为 private。源码归档不代表应用验收或 App Store 发布。</p></section>
<p class="muted">向导仅在本机运行，一小时后关闭。密钥保存在仓库外的受限目录，不进入客户端、
CodeAgent 或 Builder。浏览器完成创建后会自动回到这里；最终组织安装仍需你确认。</p></main></html>"""


class Server(HTTPServer):
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # Never emit a traceback containing request codes or credentials.
        pass

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address


class Handler(BaseHTTPRequestHandler):
    server_version = "VibAppSetup"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, "Request rejected.", "text/plain; charset=utf-8")

    def respond(self, status, body="", content_type="text/html; charset=utf-8", headers=None,
                referrer_policy="no-referrer"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", referrer_policy)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; "
                         "form-action 'self' https://github.com; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        value = cookie.get(COOKIE)
        return value is not None and secrets.compare_digest(value.value, self.server.setup.session)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        setup = self.server.setup
        try:
            if self.headers.get_all("Host") != [urlsplit(setup.origin).netloc] or len(self.path) > 4096:
                return self.respond(403, "Invalid host or request.")
            if time.monotonic() >= setup.deadline:
                return self.respond(410, "Setup expired. Restart the local setup tool.")
            target = urlsplit(self.path)
            if target.netloc or target.scheme:
                return self.respond(400, "Invalid request.")
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=8)
            if any(len(values) != 1 for values in query.values()):
                return self.respond(400, "Duplicate query fields.")
            if method == "GET" and target.path == "/health":
                return self.respond(200, '{"status":"ok","scope":"local-setup-only"}', "application/json")
            if method == "GET" and target.path == "/start":
                token = query.get("token", [""])[0]
                if not secrets.compare_digest(token, setup.entry_token):
                    return self.respond(403, "Open the setup link printed by the local tool.")
                return self.respond(303, headers={"Location": "/", "Set-Cookie":
                    f"{COOKIE}={setup.session}; HttpOnly; SameSite=Lax; Path=/; Max-Age=3600"})
            if not self.authorized():
                return self.respond(403, "Open the setup link printed by the local tool.")
            if method == "GET" and target.path == "/style.css":
                return self.respond(200, STYLE, "text/css; charset=utf-8")
            if method == "GET" and target.path == "/callback":
                state = query.get("state", [""])[0]
                code = query.get("code", [""])[0]
                if not secrets.compare_digest(state, setup.state) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", code):
                    return self.respond(403, "Invalid or expired registration callback.")
                try:
                    setup.convert(code)
                except SetupError as exc:
                    setup.error = exc.code
                return self.respond(303, headers={"Location": "/"})
            if method == "GET" and target.path == "/installed":
                return self.respond(303, headers={"Location": "/"})
            if method == "GET" and target.path == "/":
                # no-referrer suppresses Origin on browser form POSTs (Origin:
                # null), conflicting with /check's exact-origin CSRF defense.
                # Only the clean, query-free form page uses same-origin. The
                # entry/code callbacks keep no-referrer; external GitHub requests
                # still receive no Referer. Never relax /check to accept null.
                if target.query:
                    return self.respond(303, headers={"Location": "/"})
                return self.respond(200, render(setup), referrer_policy="same-origin")
            if method == "POST" and target.path == "/check":
                if self.headers.get_all("Origin") != [setup.origin] or self.headers.get("Transfer-Encoding") or \
                        self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                    return self.respond(403, "Invalid origin or encoding.")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0]):
                    return self.respond(400, "Invalid body length.")
                length = int(lengths[0])
                if length > 1024:
                    return self.respond(413, "Request too large.")
                form = parse_qs(self.rfile.read(length).decode("ascii"), max_num_fields=2)
                if form.get("csrf") != [setup.csrf]:
                    return self.respond(403, "Invalid confirmation.")
                try:
                    setup.check()
                except SetupError as exc:
                    setup.error = exc.code
                return self.respond(303, headers={"Location": "/"})
            return self.respond(404, "Not found.")
        except Exception:
            self.respond(400, "Request could not be completed. No credentials are shown.")


def make_server(setup, port=0):
    server = Server(("127.0.0.1", port), Handler)
    server.setup = setup
    setup.origin = f"http://127.0.0.1:{server.server_port}"
    server.timeout = 1
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "status"))
    parser.add_argument("--credentials-dir", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--check-installation", action="store_true", help="status: read-only GitHub verification")
    args = parser.parse_args()
    lock_fd = None
    try:
        store = CredentialStore(args.credentials_dir)
        if args.command == "status":
            record = store.read("credentials.json")
            if not record:
                print(json.dumps({"registered": False,
                                  "recovery_required": store.read("registration-attempt.json") is not None}))
                return 0
            record = credential_record(record)
            result = {"registered": True, "app_id": record["id"], "slug": record["slug"],
                      "organization": ORG, "installation": "not_checked"}
            if args.check_installation:
                result["installation"] = verify_installation(record)
            print(json.dumps(result))
            return 0
        lock_fd = store.lock()
        setup = Setup(store)
        with make_server(setup, args.port) as server:
            print(f"Setup URL: {setup.origin}/start?token={setup.entry_token}", flush=True)
            print(f"Health URL: {setup.origin}/health", flush=True)
            print(f"PID: {os.getpid()}; expires in 60 minutes; no repository writes.", flush=True)
            while time.monotonic() < setup.deadline:
                server.handle_request()
        return 0
    except SetupError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('{"error":"setup_failed_no_credentials_logged"}')
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
