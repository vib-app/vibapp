#!/usr/bin/env python3
"""Bounded public Store intake. A URL identifies an app, never install authority.

Only the fixed VibApp catalog and its content-bound GitHub Release are read.
Downloaded bytes enter the existing private candidate validator, not execution.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import stat
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_appstore import LocalAppStore, StoreError, RESULT_SCHEMA, strict_json, _validate_snapshot

CATALOG_URL = "https://raw.githubusercontent.com/vib-app/packages/main/registry.json"
MAX_CATALOG = 1024 * 1024
MAX_ZIP = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
APP_ID = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")
SHA = re.compile(r"[0-9a-f]{64}")


def require(condition, message):
    if not condition:
        raise StoreError("public-store-rejected", message)


def select_app(data: bytes, app_id: str) -> dict:
    require(isinstance(app_id, str) and len(app_id) <= 128 and APP_ID.fullmatch(app_id), "Invalid app ID")
    require(len(data) <= MAX_CATALOG, "Store catalog exceeds size limit")
    catalog = strict_json(data, label="public Store catalog")
    require(isinstance(catalog, dict) and catalog.get("schema_version") == "vibapp.store-catalog.v1", "Unsupported catalog")
    apps = catalog.get("apps")
    require(isinstance(apps, list) and len(apps) <= 500, "Invalid catalog apps")
    matches = [item for item in apps if isinstance(item, dict) and item.get("app_id") == app_id]
    require(len(matches) == 1, "App is missing, withdrawn, or duplicated in the public Store")
    item = matches[0]
    require(item.get("publication_state") == "published" and item.get("verification_state") == "package-verified", "App is not a verified public package")
    for name in ("package_digest_sha256", "component_sha256"):
        require(isinstance(item.get(name), str) and SHA.fullmatch(item[name]), f"Invalid {name}")
    digest = item["package_digest_sha256"]
    download, source = item.get("download"), item.get("source")
    require(isinstance(download, dict) and isinstance(source, dict), "Missing download/source binding")
    require(source.get("organization") == "vib-app" and source.get("repository") == "sources"
            and source.get("repository_id") == 1371257005 and source.get("package_digest_sha256") == digest,
            "Source repository does not match the public Store")
    require(isinstance(source.get("commit_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", source["commit_sha"]), "Invalid source commit")
    require(isinstance(source.get("source_digest_sha256"), str) and SHA.fullmatch(source["source_digest_sha256"]), "Invalid source digest")
    require(item.get("source_url") == f"https://github.com/vib-app/sources/tree/{source['commit_sha']}", "Invalid source URL")
    require(download.get("url") == f"https://github.com/vib-app/packages/releases/download/vibapp-package-{digest}/{digest}.zip", "Unexpected package URL")
    require(isinstance(download.get("sha256"), str) and SHA.fullmatch(download["sha256"]), "Invalid archive digest")
    require(type(download.get("size_bytes")) is int and 1 <= download["size_bytes"] <= MAX_ZIP, "Invalid archive size")
    return item


class ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, enabled: bool):
        self.enabled, self.count = enabled, 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        url = urlsplit(newurl)
        require(self.enabled and self.count <= 4 and url.scheme == "https"
                and url.hostname in {"github.com", "release-assets.githubusercontent.com"}
                and not url.username and not url.password and url.port in (None, 443), "Unsafe download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, limit: int, deadline: float, *, release: bool = False) -> bytes:
    # The macOS system trust bundle also works with isolated bundled Python.
    system_ca = Path("/etc/ssl/cert.pem")
    context = ssl.create_default_context(cafile=str(system_ca) if sys.platform == "darwin" and system_ca.is_file() else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ReleaseRedirects(release), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream" if release else "application/json", "User-Agent": "VibApp-Store-Client/1", "Cache-Control": "no-cache"})
    require(time.monotonic() < deadline, "Download deadline exceeded")
    with opener.open(request, timeout=min(15, max(1, deadline - time.monotonic()))) as response:
        require(response.status == 200, "Store download failed")
        length = response.headers.get("Content-Length")
        require(length is None or (length.isdigit() and int(length) <= limit), "Download exceeds size limit")
        result = bytearray()
        while True:
            require(time.monotonic() < deadline, "Download deadline exceeded")
            chunk = response.read(min(65536, limit + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            require(len(result) <= limit, "Download exceeds size limit")


def stage_archive(data: bytes, item: dict, directory: Path) -> Path:
    download = item["download"]
    require(len(data) == download["size_bytes"] and hashlib.sha256(data).hexdigest() == download["sha256"], "Archive bytes do not match Store digest/size")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(1 <= len(entries) <= 257 and sum(e.file_size for e in entries) <= MAX_EXPANDED, "Archive exceeds expanded limits")
        names = set()
        for entry in entries:
            name = entry.filename
            mode = entry.external_attr >> 16
            require(name not in names and len(name) <= 240 and re.fullmatch(r"[A-Za-z0-9._/-]+", name)
                    and not name.startswith("/") and all(p not in ("", ".", "..") for p in name.split("/"))
                    and (name == "candidate.json" or name.startswith("package/")), "Unsafe or duplicate archive path")
            require(not entry.is_dir() and not entry.flag_bits & 1
                    and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                    and 0 < entry.file_size <= MAX_ZIP, "Unsupported archive member")
            names.add(name)
        require({"candidate.json", "package/manifest.json"}.issubset(names), "Incomplete archive")
        # Never extractall: no links, traversal, overwrite, or unbounded expansion.
        for entry in entries:
            path = directory.joinpath(*PurePosixPath(entry.filename).parts)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(entry) as source, path.open("xb") as target:
                remaining = entry.file_size
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    require(bool(chunk), "Truncated archive member")
                    target.write(chunk)
                    remaining -= len(chunk)
                require(not source.read(1), "Oversized archive member")
    return validate_staged(directory, item)


def validate_staged(directory: Path, item: dict) -> Path:
    candidate = directory / "candidate.json"
    validated = _validate_snapshot(candidate, ingested_at="2026-01-01T00:00:00Z")
    require(validated.app_id == item["app_id"] and validated.app_version == item["version"]
            and validated.package_digest == item["package_digest_sha256"]
            and validated.record["digests"]["component_sha256"] == item["component_sha256"]
            and validated.record["digests"]["source_tree_sha256"] == item["source"]["source_digest_sha256"], "Package identity differs from Store listing")
    return candidate


def stage_shared_cache(cache_root: Path, item: dict, directory: Path) -> Path:
    """Read public cache into private staging, without following links.

    No cache receipt grants trust. The existing validator and current live Store
    digest must both accept the private copy. Unsupported platforms use HTTP.
    """
    require(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "Safe shared-cache reader unavailable")
    require(cache_root.is_absolute(), "Shared cache requires an absolute path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    # Resolve every directory using handles; a swapped parent cannot redirect us.
    fd = os.open(cache_root.anchor, flags)
    try:
        for part in (*cache_root.parts[1:], item["package_digest_sha256"] + ".vibapp-candidate"):
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = next_fd
        count, total = 0, 0

        def copy_tree(source_fd: int, target: Path, depth: int = 0):
            nonlocal count, total
            require(depth <= 8, "Cache nesting limit")
            with os.scandir(source_fd) as entries:
                for entry in entries:
                    count += 1
                    require(count <= 257 and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", entry.name)
                            and entry.name not in {".", ".."}, "Unsafe cache layout")
                    info = entry.stat(follow_symlinks=False)
                    destination = target / entry.name
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, flags, dir_fd=source_fd)
                        try:
                            destination.mkdir(mode=0o700)
                            copy_tree(child, destination, depth + 1)
                        finally: os.close(child)
                    else:
                        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= MAX_ZIP, "Unsafe cache file")
                        total += info.st_size
                        require(total <= MAX_EXPANDED, "Cache size limit")
                        file_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
                        with os.fdopen(file_fd, "rb") as source, destination.open("xb") as output:
                            actual = os.fstat(source.fileno())
                            require(stat.S_ISREG(actual.st_mode) and actual.st_nlink == 1 and actual.st_size == info.st_size, "Cache file changed")
                            remaining = info.st_size
                            while remaining:
                                chunk = source.read(min(65536, remaining))
                                require(bool(chunk), "Truncated cache")
                                output.write(chunk); remaining -= len(chunk)
                            require(not source.read(1), "Cache file grew")
        copy_tree(fd, directory)
        return validate_staged(directory, item)
    finally: os.close(fd)


def download_app(root: Path, app_id: str, cache_root: Path | None = None) -> dict:
    deadline = time.monotonic() + 75
    item = select_app(fetch(CATALOG_URL, MAX_CATALOG, deadline), app_id)
    store = LocalAppStore(root)
    if cache_root:
        try:
            with tempfile.TemporaryDirectory(prefix=".public-cache-", dir=store.root) as temporary:
                result = store.ingest(stage_shared_cache(cache_root, item, Path(temporary)))
            result["public_source_url"] = item["source_url"]
            result["download_source"] = "shared-cache"
            return result
        except (OSError, ValueError, StoreError):
            pass  # Partial, tampered, incompatible, or missing cache -> verified HTTP.
    data = fetch(item["download"]["url"], item["download"]["size_bytes"], deadline, release=True)
    with tempfile.TemporaryDirectory(prefix=".public-download-", dir=store.root) as temporary:
        candidate = stage_archive(data, item, Path(temporary))
        result = store.ingest(candidate)
    result["public_source_url"] = item["source_url"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--cache-root", type=Path)
    args = parser.parse_args()
    try:
        result = download_app(args.store_root, args.app_id, args.cache_root)
    except Exception as error:
        # No signed redirect URLs or transport exception details in GUI/logs.
        message = str(error)[:512] if isinstance(error, StoreError) else "Public Store download failed; check your connection and retry."
        print(json.dumps({"schema_version": RESULT_SCHEMA, "error": {"code": "public-store-download", "message": message}}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
