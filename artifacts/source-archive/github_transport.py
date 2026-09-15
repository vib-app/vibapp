"""Repository-scoped GitHub App transport shared by trusted build/publish roles.

No owner CLI fallback. Tokens remain in memory; authenticated redirects are never
followed. An artifact redirect is followed with a new credential-free request.
"""
from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from github_app_setup import NoRedirect, unique_object
from github_archive import (Archiver, CredentialStore, DEFAULT_STORE, SetupError, canonical,
                            credential_record, verify_installation)

MAX_JSON = 32 * 1048576
MAX_BINARY = 64 * 1048576
PACKAGES_REPOSITORY_ID = 1359065065


def public_download(url, limit=MAX_BINARY, headers=None):
    """Only public GitHub/Actions distribution hosts, never forward credentials."""
    if type(limit) is not int or not 0 < limit <= MAX_BINARY:
        raise SetupError("download_limit")
    for _ in range(4):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password or
                parsed.fragment or not (host in ("github.com", "raw.githubusercontent.com", "release-assets.githubusercontent.com",
                                                "results-receiver.actions.githubusercontent.com") or
                                       re.fullmatch(r"[a-z0-9]+\.blob\.core\.windows\.net", host))):
            raise SetupError("download_host_policy")
        safe_headers = {"User-Agent": "VibApp-Package-Download", "Accept": "application/octet-stream"}
        if headers:
            if set(headers) - {"Range", "Origin"}:
                raise SetupError("download_header_policy")
            safe_headers.update(headers)
        request = Request(url, headers=safe_headers)
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
                if response.status not in (200, 206):
                    raise SetupError("download_status")
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise SetupError("download_limit")
                return data
        except HTTPError as exc:
            status, location = exc.code, exc.headers.get("Location")
            exc.close()
            if status in (301, 302, 303, 307, 308) and location:
                url = location
                continue
            raise SetupError(f"download_http_{status}") from None
        except (URLError, OSError, TimeoutError):
            raise SetupError("download_network") from None
    raise SetupError("download_redirect_limit")


def endpoint_allowed(method, suffix, packages=False):
    if method == "GET" and suffix == "":
        return True
    if method == "GET" and re.fullmatch(r"/git/(?:ref/(?:heads/main|tags/[A-Za-z0-9._-]{1,180})|(?:blobs|trees|commits)/[0-9a-f]{40})(?:\?recursive=1)?", suffix):
        return True
    if method == "POST" and suffix in ("/git/blobs", "/git/trees", "/git/commits", "/git/refs"):
        return True
    if method == "PATCH" and suffix == "/git/refs/heads/main":
        return True
    if packages:
        return bool(
            method == "PUT" and suffix in ("/contents/README.md", "/contents/.vibapp-packages-bootstrap") or
            method == "POST" and suffix == "/releases" or
            method == "PATCH" and re.fullmatch(r"/releases/[0-9]+", suffix) or
            method == "GET" and re.fullmatch(r"/releases/(?:tags/[A-Za-z0-9._-]{1,180}|[0-9]+(?:/assets\?per_page=100)?|assets/[0-9]+)", suffix))
    return bool(
        method == "POST" and suffix == "/actions/workflows/vibapp-build.yml/dispatches" or
        method == "GET" and (suffix == "/actions/workflows/vibapp-build.yml/runs?event=workflow_dispatch&per_page=100" or
            re.fullmatch(r"/actions/runs/[0-9]+(?:/(?:artifacts|jobs)\?per_page=100|/logs)?", suffix) or
            re.fullmatch(r"/actions/artifacts/[0-9]+/zip", suffix)))


class ScopedAppApi:
    def __init__(self, repository, repository_id, permissions):
        if not re.fullmatch(r"app-[0-9a-f]{64}|sources|packages", repository) or type(repository_id) is not int or repository_id <= 0:
            raise SetupError("repository_scope_invalid")
        packages = repository == "packages"
        if packages and repository_id != PACKAGES_REPOSITORY_ID:
            raise SetupError("packages_repository_mismatch")
        if permissions not in ({"contents": "write"}, {"contents": "read", "actions": "read"},
                               {"contents": "write", "actions": "write", "workflows": "write"}):
            raise SetupError("role_permissions_invalid")
        if packages and permissions != {"contents": "write"}:
            raise SetupError("packages_role_invalid")
        record = credential_record(CredentialStore(DEFAULT_STORE).read("credentials.json"))
        installation = verify_installation(record)
        self.repository, self.repository_id, self.packages = repository, repository_id, packages
        self.prefix = f"/repos/vib-app/{repository}"
        self.token = Archiver(record, installation).token(permissions, repository_id)
        self.calls = 0
        self.deadline = time.monotonic() + 300
        remote = self.request("GET", "")
        if (remote.get("id") != repository_id or remote.get("full_name") != "vib-app/" + repository or
                remote.get("owner", {}).get("id") != record["owner"]["id"] or
                remote.get("private") is not (repository not in ("sources", "packages")) or
                remote.get("owner", {}).get("type") != "Organization" or remote.get("archived") or remote.get("disabled")):
            raise SetupError("repository_scope_mismatch")

    def _send(self, request, binary=False, missing_ok=False):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > 500 or remaining <= 0:
            raise SetupError("github_role_budget")
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=min(30, remaining)) as response:
                if response.status not in (200, 201, 204):
                    raise SetupError("github_role_response")
                limit = MAX_BINARY if binary else MAX_JSON
                data = response.read(limit + 1)
            if len(data) > limit:
                raise SetupError("github_role_response_limit")
            return data if binary else json.loads(data, object_pairs_hook=unique_object) if data else None
        except HTTPError as exc:
            status, location = exc.code, exc.headers.get("Location")
            exc.close()
            if missing_ok and status == 404 and request.get_method() == "GET":
                return None
            if binary and request.get_method() == "GET" and status == 302 and location:
                return public_download(location)
            raise SetupError(f"github_role_http_{status}") from None
        except (URLError, OSError, TimeoutError, ValueError):
            raise SetupError("github_role_network_or_response") from None

    def request(self, method, suffix, body=None, missing_ok=False, binary=False):
        if not endpoint_allowed(method, suffix, self.packages):
            raise SetupError("github_role_endpoint")
        if method == "PATCH" and suffix.startswith("/git/refs/") and (not isinstance(body, dict) or body.get("force") is not False):
            raise SetupError("github_force_push_denied")
        payload = canonical(body) if body is not None else None
        if payload is not None and len(payload) > MAX_JSON:
            raise SetupError("github_role_request_limit")
        request = Request("https://api.github.com" + self.prefix + suffix, method=method, data=payload,
            headers={"Authorization": "Bearer " + self.token, "User-Agent": "VibApp-Scoped-App",
                     "X-GitHub-Api-Version": "2026-03-10", "Content-Type": "application/json",
                     "Accept": "application/octet-stream" if binary and self.packages else "application/vnd.github+json"})
        for attempt in range(2):
            try:
                return self._send(request, binary, missing_ok)
            except SetupError as error:
                if method != "GET" or attempt or error.code not in (
                    "github_role_network_or_response", "github_role_http_502", "github_role_http_503", "github_role_http_504"):
                    raise
                time.sleep(0.25)

    def upload_asset(self, release_id, name, payload, content_type):
        if not self.packages or type(release_id) is not int or release_id <= 0 or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", name):
            raise SetupError("release_upload_target")
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BINARY or content_type not in (
                "application/zip", "application/octet-stream", "application/x-bittorrent", "application/json"):
            raise SetupError("release_upload_payload")
        url = "https://uploads.github.com" + self.prefix + f"/releases/{release_id}/assets?name=" + quote(name)
        return self._send(Request(url, method="POST", data=payload,
            headers={"Authorization": "Bearer " + self.token, "User-Agent": "VibApp-Scoped-App",
                     "X-GitHub-Api-Version": "2026-03-10", "Content-Type": content_type,
                     "Accept": "application/vnd.github+json"}))
