#!/usr/bin/env python3
"""Exercise packaged owner transport and a real, digest-verified Store app.

No candidate or runtime is mocked. The catalog is an explicit pinned fixture;
an optional pre-downloaded archive must match its exact SHA-256 and size. The
By default this fails for an undeclared desktop platform. Explicit compatibility
diagnostic mode tests the current host path without claiming Store eligibility.
This verifies daemon execution, not native GUI painting or installer behavior.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def helper_environment() -> dict[str, str]:
    """Match local_product.rs env_clear, including no inherited auth or HOME."""
    if os.name == "nt":
        directory = ctypes.create_unicode_buffer(32768)
        get_directory = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
        get_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        get_directory.restype = ctypes.c_uint
        length = get_directory(directory, len(directory))
        if not 0 < length < len(directory):
            raise OSError("Cannot resolve trusted Windows system PATH")
        path = directory.value
    else:
        path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" if sys.platform == "darwin" else "/usr/local/bin:/usr/bin:/bin"
    return {"PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}


def layout(bundle: Path) -> tuple[Path, Path, Path, Path]:
    if sys.platform == "darwin":
        resources = bundle / "Contents/Resources"
        return (resources, resources / "python/bin/python3.13", bundle / "Contents/MacOS/vibapp-launcher",
                resources / "runtime-daemon/service-runtime/vibapp-service-runtime")
    resources = bundle / "resources"
    if os.name == "nt":
        return resources, resources / "python/python.exe", bundle / "vibapp-launcher.exe", bundle / "libexec/vibapp-service-runtime.exe"
    return resources, resources / "python/bin/python3.13", bundle / "bin/vibapp-launcher", bundle / "libexec/vibapp-service-runtime"


def host_architecture() -> str:
    if os.name == "nt":
        # platform.machine() can fall back to environment variables stripped by
        # the production helper boundary. Resolve the actual native OS instead.
        information = ctypes.create_string_buffer(64)
        get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetNativeSystemInfo
        get_info.argtypes = [ctypes.c_void_p]
        get_info.restype = None
        get_info(information)
        return {9: "x86_64", 12: "aarch64"}[ctypes.c_uint16.from_buffer(information).value]
    return {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[platform.machine().lower()]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def startup_workspace_checks(launcher: Path, workspace: Path, env: dict[str, str]) -> list[str]:
    if os.name != "nt":
        return []
    from vibapp_daemon import windows_security as security
    private = workspace / "private-workspace"
    private.mkdir()
    security.protect(private, directory=True)
    # --help exits before WebView creation but after the real Rust parser and
    # packaged Python ACL verifier. Cargo tests cannot resolve bundled Python.
    for name in (str(private), "\\\\?\\" + str(private)):
        result = subprocess.run([str(launcher), "--help", "--data-dir", name], env=env,
                                capture_output=True, timeout=15)
        require(result.returncode == 0, "Packaged launcher rejected an owner-private Windows workspace")
    require(not list(private.iterdir()), "Workspace selection unexpectedly wrote files")
    security.verify(private, directory=True)
    # CI must permit native symbolic-link creation (the Windows release runner
    # does). A nested leaf tests ancestor checks, not only the final path.
    nested = private / "nested"
    nested.mkdir()
    linked = workspace / "linked-workspace"
    os.symlink(private, linked, target_is_directory=True)
    try:
        for selected in (linked, linked / "nested"):
            result = subprocess.run([str(launcher), "--help", "--data-dir", str(selected)],
                                    env=env, capture_output=True, timeout=15)
            require(result.returncode != 0, "Packaged launcher accepted a reparse-point workspace or ancestor")
    finally:
        linked.unlink()
        nested.rmdir()
    public = workspace / "public-workspace"
    public.mkdir()
    security.protect(public, directory=True)
    descriptor = security.P()
    sid = security.process_sid()
    security.checked(security._from_sddl(f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FR;;;WD)",
                                        1, security.ctypes.byref(descriptor), None))
    try:
        security.checked(security._set_file(str(public), 4 | 0x80000000, descriptor))
        result = subprocess.run([str(launcher), "--help", "--data-dir", str(public)], env=env,
                                capture_output=True, timeout=15)
        require(result.returncode != 0, "Packaged launcher accepted a publicly readable workspace")
        try:
            security.verify(public, directory=True)
        except PermissionError:
            pass  # rejection did not silently repair the user's selected ACL
        else:
            raise RuntimeError("Workspace selection changed the caller's public ACL")
    finally:
        security._free(descriptor)
        security.protect(public, directory=True)
    return ["windows-normal-and-extended-private-workspace", "windows-reparse-leaf-and-ancestor-rejected",
            "windows-public-workspace-rejected-without-acl-mutation"]


class OwnerDaemon:
    def __init__(self, python: Path, resources: Path, runtime: Path, workspace: Path):
        self.workspace = workspace
        self.endpoint = workspace / "runtime/run/vibappd.sock"
        self.prefix = [str(python), "-I", "-B", "-X", "utf8", "-c",
                       "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')",
                       str(resources / "runtime-daemon")]
        self.env = {**helper_environment(), "VIBAPP_SERVICE_RUNTIME_BIN": str(runtime)}
        self.process = None
        self.diagnostic = bytearray()
        self.reader = None

    def start(self) -> None:
        self.diagnostic.clear()
        self.process = subprocess.Popen(self.prefix + ["serve", "--root", str(self.workspace / "runtime"),
                                        "--promotion-root", str(self.workspace / "store/candidates"),
                                        "--socket", str(self.endpoint)], env=self.env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def drain():
            while chunk := self.process.stderr.read(4096):
                self.diagnostic.extend(chunk)
                del self.diagnostic[:-65536]
        self.reader = threading.Thread(target=drain, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.process.poll() is None:
            probe = subprocess.run(self.prefix + ["probe", "--socket", str(self.endpoint)],
                                   env=helper_environment(), capture_output=True, timeout=3)
            if probe.returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError("Packaged owner daemon failed readiness: " + self.diagnostic.decode(errors="replace")[-4096:])

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=3)
        require(not self.reader.is_alive(), "Daemon diagnostic reader did not terminate")
        self.process.stderr.close()
        self.process = None

    def control(self, tag: str, app_id: str | None, value=None, candidate: Path | None = None) -> dict:
        identity = "packaged-smoke-" + uuid.uuid4().hex
        envelope = {"request_id": identity, "idempotency_key": identity, "client": "launcher",
                    "subject": app_id, "command": {"tag": tag, "value": value}}
        command = self.prefix + ["ctl", "--socket", str(self.endpoint), "--envelope", "-", "--timeout", "15"]
        if candidate is not None:
            command += ["--promotion-record", str(candidate)]
        result = subprocess.run(command, input=json.dumps(envelope).encode(), env=helper_environment(),
                                capture_output=True, timeout=20)
        require(len(result.stdout) <= 65536 and len(result.stderr) <= 65536, "Daemon response exceeded its transport bound")
        if result.returncode != 0:
            raise RuntimeError(f"Packaged daemon {tag} failed: " + (result.stdout + result.stderr).decode(errors="replace")[-4096:])
        response = json.loads(result.stdout)
        require(response.get("request_id") == identity and "outcome" in response, "Unbound daemon response")
        return response["outcome"]


def exercise(args, workspace: Path) -> dict:
    resources, python, launcher, runtime = layout(args.bundle)
    for file in (python, launcher, runtime):
        require(file.is_file(), f"Missing packaged executable: {file}")
    sys.path.insert(0, str(resources / "runtime-daemon"))
    sys.path.insert(0, str(resources / "registry-store"))
    from vibapp_daemon.host_storage import protect
    from public_app_download import LocalAppStore, select_app, fetch, stage_archive
    protect(workspace, directory=True)
    checks = startup_workspace_checks(launcher, workspace, helper_environment())
    catalog = args.catalog_file.read_bytes()
    item = select_app(catalog, args.app_id)
    archive = args.archive_file.read_bytes() if args.archive_file else fetch(
        item["download"]["url"], item["download"]["size_bytes"], time.monotonic() + 75, release=True)
    store = LocalAppStore(workspace / "store")
    with tempfile.TemporaryDirectory(prefix=".smoke-download-", dir=store.root) as temporary:
        candidate = stage_archive(archive, item, Path(temporary))
        manifest = json.loads((candidate.parent / "package/manifest.json").read_bytes())
        operating_system = {"darwin": "macos", "win32": "windows", "linux": "linux"}[sys.platform]
        architecture = host_architecture()
        declared_for_host = any(row.get("os") == operating_system and row.get("arch") == architecture
                                and "desktop" in row.get("profiles", []) for row in manifest["runtime"]["platforms"])
        require(declared_for_host or args.compatibility_diagnostic,
                f"Published package does not declare desktop {operating_system}/{architecture}; use explicit compatibility diagnostic, not eligibility acceptance")
        result = store.ingest(candidate)
    record = result["record"]
    digest = record["digests"]["package_sha256"]
    candidate = store.root / record["paths"]["candidate"]
    entrypoint = next(row["id"] for row in manifest["entrypoints"] if row["kind"] == "launcher-ui" and "desktop" in row["profiles"])
    checks += ["pinned-store-archive-and-source-validation", "host-platform-declaration-inspected", "private-candidate-ingest"]
    daemon = OwnerDaemon(python, resources, runtime, workspace)
    views = []
    try:
        daemon.start()
        daemon.control("install", None, {"package_digest_sha256": digest, "enable_after_install": False}, candidate)
        require(daemon.control("status", args.app_id)["value"]["enabled"] is False, "Installation unexpectedly enabled execution")
        daemon.control("enable", args.app_id)
        checks += ["packaged-owner-transport", "install-disabled", "explicit-enable"]
        generations = []
        for iteration in range(2):
            status = daemon.control("status", args.app_id)["value"]
            require(status["enabled"] is True and status["package_digest_sha256"] == digest, "Installed state was not preserved")
            launch = daemon.control("launch", args.app_id, {"entrypoint": entrypoint, "route": None})
            require(launch["tag"] == "launched", "Real component did not launch")
            binding = launch["value"]
            require(binding["component_sha256"] == item["component_sha256"], "Launched component digest differs from the Store")
            generations.append(binding["generation"])
            before = binding["semantic_surface"]["view"]
            time.sleep(1.1)
            refresh = {key: binding[key] for key in ("entrypoint", "package_digest_sha256", "component_sha256", "generation", "session", "surface", "route")}
            refresh["event_id"] = "refresh-" + uuid.uuid4().hex
            updated = daemon.control("ui-refresh", args.app_id, refresh)
            require(updated["tag"] == "ui-updated", "Real component did not refresh")
            after = updated["value"]["semantic_surface"]["view"]
            require(before != after, "LED clock surface did not advance after a real second")
            views.append({"before_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                          "after_sha256": hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()})
            daemon.control("surface-close", args.app_id, {key: binding[key] for key in ("session", "surface")})
            require(not any(row["state"] == "open" for row in daemon.control("status", args.app_id)["value"]["surfaces"]), "Close left an open UI surface")
            if iteration == 0:
                daemon.stop()
                daemon.start()
        require(generations[0] != generations[1], "Reopen reused a dead generation")
        checks += ["real-component-launch", "clock-refresh-advances", "surface-close", "daemon-restart-preserves-install", "real-component-reopen-and-refresh"]
    finally:
        daemon.stop()
    return {"schema_version": "vibapp.packaged-daemon-smoke.v1", "platform": sys.platform,
            "app_id": args.app_id, "package_digest_sha256": digest, "component_sha256": item["component_sha256"],
            "catalog_sha256": hashlib.sha256(catalog).hexdigest(), "checks": checks, "clock_views": views,
            "helper_environment_keys": sorted(helper_environment()), "native_gui_verified": False,
            "manifest_declared_for_host": declared_for_host, "host_runtime_checked": True,
            "store_native_eligibility_verified": False, "compatibility_diagnostic": args.compatibility_diagnostic}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--catalog-file", required=True, type=Path)
    parser.add_argument("--archive-file", type=Path)
    parser.add_argument("--app-id", default="ai.vibapp.custom.1a04442610c")
    parser.add_argument("--compatibility-diagnostic", action="store_true")
    parser.add_argument("--isolated-workspace", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.bundle = args.bundle.resolve(strict=True)
    args.catalog_file = args.catalog_file.resolve(strict=True)
    if args.archive_file:
        args.archive_file = args.archive_file.resolve(strict=True)
    if args.isolated_workspace:
        print(json.dumps(exercise(args, args.isolated_workspace.resolve(strict=True)), ensure_ascii=False))
        return 0
    # Bootstrap only: all actual work uses bundled isolated Python with the same
    # cleared environment as the Rust launcher, not the CI runner's environment.
    _, python, _, _ = layout(args.bundle)
    # Darwin's per-user temporary root can exceed the Unix socket path ceiling.
    with tempfile.TemporaryDirectory(prefix="va-smoke-", dir=None if os.name == "nt" else "/tmp") as temporary:
        command = [str(python), "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()),
                   "--bundle", str(args.bundle), "--catalog-file", str(args.catalog_file),
                   "--app-id", args.app_id, "--isolated-workspace", str(Path(temporary).resolve())]
        if args.archive_file:
            command += ["--archive-file", str(args.archive_file)]
        if args.compatibility_diagnostic:
            command += ["--compatibility-diagnostic"]
        return subprocess.run(command, env=helper_environment(), timeout=150).returncode


if __name__ == "__main__":
    raise SystemExit(main())
