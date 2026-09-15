"""Bounded cold first-surface check before automatic private AppStore delivery.

This is not business-function acceptance. No Registry ingestion, model, compiler,
user runtime, app action, refresh, or service entrypoint is used here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ARTIFACTS = Path(__file__).resolve().parents[1]
for dependency in (ARTIFACTS / "app-builder", ARTIFACTS / "runtime-daemon"):
    if str(dependency) not in sys.path:
        sys.path.insert(0, str(dependency))
from common import ProcessLimits, _limit_child, _process_group_usage, bounded_tree_size

POLICY = {
    "version": "ui-cold-first-surface-v1",
    "scope": "ui-only-reference",
    "state": "fresh-private-root-per-entrypoint",
    "settings": "manifest-defaults-only",
    "operations": ["install-disabled", "enable", "launch-initial-once", "disable", "shutdown"],
    "business_function_acceptance": False,
    "maximum_entrypoints": 8,
    "wall_seconds": 45,
    "memory_bytes": 768 * 1024 * 1024,
    "disk_bytes": 128 * 1024 * 1024,
}
MAX_JSON = 512 * 1024
MAX_BINARY = 256 * 1024 * 1024
FORBIDDEN_EFFECTS = tuple(f"vibapp:experimental-v0/{name}@0.0.1" for name in ("http", "scheduler", "notification"))


class RuntimeReadinessError(Exception):
    code = "runtime-readiness-failed"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeReadinessError("duplicate readiness JSON field")
        result[key] = value
    return result


def _read(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise RuntimeReadinessError("readiness input is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise RuntimeReadinessError("readiness input grew beyond its bound")
        return value
    finally:
        os.close(descriptor)


def _json(path, maximum=MAX_JSON):
    value = json.loads(_read(path, maximum), object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise RuntimeReadinessError("readiness document must be an object")
    return value


def _hash(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise RuntimeReadinessError("readiness hash input is outside its bound")
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(descriptor, 64 * 1024):
            size += len(chunk)
            if size > maximum:
                raise RuntimeReadinessError("readiness hash input grew beyond its bound")
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def candidate_binding(candidate):
    raw = _read(candidate, MAX_JSON)
    record = json.loads(raw, object_pairs_hook=_object)
    if (not isinstance(record, dict) or record.get("schema_version") != "vibapp.builder-candidate.experimental-v1"
            or record.get("document_type") != "verifier-promoted-candidate" or record.get("state") != "candidate-ready"):
        raise RuntimeReadinessError("readiness requires an independently promoted candidate")
    manifest = _json(candidate.parent / "package/manifest.json")
    manifest_sha = _hash(candidate.parent / "package/manifest.json", MAX_JSON)
    component_sha = _hash(candidate.parent / "package/component.wasm", 32 * 1024 * 1024)
    if (record.get("manifest", {}).get("sha256") != manifest_sha
            or record.get("component", {}).get("sha256") != component_sha
            or record.get("manifest", {}).get("path") != "manifest.json"
            or record.get("component", {}).get("path") != "component.wasm"):
        raise RuntimeReadinessError("readiness candidate artifact binding changed")
    return manifest, {
        "candidate_record_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_sha256": manifest_sha,
        "component_sha256": component_sha,
        "package_digest_sha256": record.get("package_digest_sha256"),
        "policy_sha256": hashlib.sha256(_canonical(POLICY)).hexdigest(),
    }


def _entries(manifest):
    if manifest.get("app", {}).get("kind") != "ui":
        return None
    rows = manifest.get("entrypoints")
    if (manifest.get("runtime", {}).get("world") != "ui-only-reference"
            or not isinstance(rows, list) or not 1 <= len(rows) <= POLICY["maximum_entrypoints"]
            or any(not isinstance(row, dict) or row.get("kind") != "launcher-ui" for row in rows)):
        raise RuntimeReadinessError("UI readiness requires bounded UI-only launcher entrypoints")
    result = [{"entrypoint": row["id"], "route": row["routes"]["initial"]} for row in rows]
    if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for row in result for value in row.values()):
        raise RuntimeReadinessError("UI readiness initial route binding is invalid")
    return result


def _group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        raise RuntimeReadinessError("readiness process-group cleanup could not be confirmed") from error


def _run_child(command, scratch, cancellation):
    """Own one process group including every Wasmtime child, with no pipes to fill."""
    limits = ProcessLimits(wall_seconds=POLICY["wall_seconds"], cpu_seconds=40,
                           memory_bytes=POLICY["memory_bytes"], pids=10,
                           disk_bytes=POLICY["disk_bytes"], open_files=128)
    process = subprocess.Popen(command, cwd=scratch, env={"LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True,
                               preexec_fn=lambda: _limit_child(limits))
    deadline, failure = time.monotonic() + limits.wall_seconds, None
    try:
        while process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                raise RuntimeReadinessError("readiness was cancelled")
            if time.monotonic() >= deadline:
                raise RuntimeReadinessError("readiness exceeded its total wall timeout")
            rss, pids = _process_group_usage(process.pid)
            if pids == 0 and process.poll() is None:
                raise RuntimeReadinessError("readiness process-group monitoring is unavailable")
            if rss > limits.memory_bytes or pids > limits.pids:
                raise RuntimeReadinessError("readiness exceeded aggregate process limits")
            bounded_tree_size(scratch, limits.disk_bytes)
            time.sleep(0.05)
        if process.returncode != 0:
            raise RuntimeReadinessError("readiness worker exited without a successful protocol result")
    except Exception as error:
        failure = error
    finally:
        # Even a successful wrapper may not leave an unacknowledged descendant.
        leftover = _group_exists(process.pid)
        if leftover:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise RuntimeReadinessError("readiness process-group cleanup could not be confirmed") from error
        cleanup_deadline = time.monotonic() + 2
        while _group_exists(process.pid) and time.monotonic() < cleanup_deadline:
            time.sleep(0.05)
        if _group_exists(process.pid):
            raise RuntimeReadinessError("readiness process-group cleanup could not be confirmed")
        if leftover and failure is None:
            failure = RuntimeReadinessError("readiness left a live child after reporting completion")
    if failure is not None:
        raise RuntimeReadinessError(str(failure)[:512]) from failure


def check_runtime_readiness(candidate, *, cancellation=None):
    """Always execute a fresh check; an old success record is never a cache."""
    candidate = Path(candidate).absolute()
    scratch = None
    try:
        manifest = _json(candidate.parent / "package/manifest.json")
        entries = _entries(manifest)
        if entries is None:
            return {"status": "not-applicable", "scope": "ui-only", "business_function_acceptance": False}
        manifest, binding = candidate_binding(candidate)
        from vibapp_daemon.service_executor import resolve_runtime_binary
        binary = resolve_runtime_binary()
        binding["runtime_binary_sha256"] = _hash(binary, MAX_BINARY)
        binding["entrypoints"] = entries
        scratch = Path(tempfile.mkdtemp(prefix="vibapp-runtime-readiness-"))
        result_path = scratch / "result.json"
        _run_child([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", "--candidate", str(candidate),
                    "--binary", str(binary), "--scratch", str(scratch)], scratch, cancellation)
        result = _json(result_path, 64 * 1024)
        if result.get("status") != "PASS":
            raise RuntimeReadinessError(str(result.get("error", "readiness worker did not pass"))[:512])
        current_manifest, current_binding = candidate_binding(candidate)
        current_binding.update(runtime_binary_sha256=_hash(binary, MAX_BINARY), entrypoints=_entries(current_manifest))
        if current_binding != binding or result.get("binding") != binding:
            raise RuntimeReadinessError("readiness exact candidate/runtime binding changed during execution")
        if (set(result) != {"status", "binding", "checked_initial_surfaces", "cleanup_confirmed", "business_function_acceptance"}
                or result.get("checked_initial_surfaces") != entries
                or result.get("cleanup_confirmed") is not True or result.get("business_function_acceptance") is not False):
            raise RuntimeReadinessError("readiness worker did not confirm cleanup and limited acceptance scope")
        return result
    except RuntimeReadinessError:
        raise
    except Exception as error:
        raise RuntimeReadinessError(f"readiness unavailable: {str(error)[:448]}") from error
    finally:
        # Guest state is fresh and disposable, never user data. A failed group
        # cleanup leaves its scratch for diagnosis instead of deleting live data.
        if scratch is not None and not _group_cleanup_unknown():
            shutil.rmtree(scratch)


def _group_cleanup_unknown():
    error = sys.exc_info()[1]
    return error is not None and "cleanup could not be confirmed" in str(error)


def _exercise(candidate, binary, scratch, daemon_type, workers):
    manifest, binding = candidate_binding(candidate)
    entries = _entries(manifest)
    if entries is None:
        raise RuntimeReadinessError("worker must only execute a UI candidate")
    binding.update(runtime_binary_sha256=_hash(binary, MAX_BINARY), entrypoints=entries)
    app_id = manifest["app"]["id"]
    checked = []
    for index, entry in enumerate(entries):
        daemon, enabled, sequence = None, False, 0
        def execute(tag, value, subject=app_id):
            nonlocal sequence
            sequence += 1
            response = daemon.execute({"request_id": f"readiness-{sequence}", "idempotency_key": f"readiness-{sequence}",
                                       "client": "cli", "subject": subject, "command": {"tag": tag, "value": value}},
                                      principal="uid:runtime-readiness", allowed_apps={app_id},
                                      promotion_record=candidate if tag == "install" else None)
            if "error" in response:
                error = response["error"]
                raise RuntimeReadinessError(f"{tag}: {error.get('code')}: {str(error.get('message'))[:384]}")
            return response["outcome"]["value"]
        try:
            daemon = daemon_type(scratch / f"entry-{index}", candidate.parent.parent, service_runtime_binary=binary)
            execute("install", {"package_digest_sha256": binding["package_digest_sha256"], "enable_after_install": False}, None)
            execute("enable", None)
            enabled = True
            launched = execute("launch", entry)
            if (launched.get("package_digest_sha256") != binding["package_digest_sha256"]
                    or launched.get("component_sha256") != binding["component_sha256"]
                    or launched.get("entrypoint") != entry["entrypoint"] or launched.get("route") != entry["route"]
                    or not isinstance(launched.get("semantic_surface"), dict)):
                raise RuntimeReadinessError("initial surface is not bound to the exact candidate and route")
            checked.append(entry)
        finally:
            try:
                if daemon is not None:
                    try:
                        if enabled:
                            execute("disable", None)
                    finally:
                        daemon.shutdown()
            finally:
                cleanup_errors = []
                for worker in workers:
                    try:
                        worker.terminate()
                    except Exception as error:
                        cleanup_errors.append(error)
                if cleanup_errors or any(getattr(worker, "process", None) is not None and worker.process.poll() is None for worker in workers):
                    raise RuntimeReadinessError("readiness worker cleanup could not be confirmed")
    return {"status": "PASS", "binding": binding, "checked_initial_surfaces": checked,
            "cleanup_confirmed": True, "business_function_acceptance": False}


def _worker(candidate, binary, scratch):
    from vibapp_daemon import core, service_executor
    if any(core.LOCAL_HOST_CAPABILITY_AVAILABILITY.get(interface) != "unavailable" for interface in FORBIDDEN_EFFECTS):
        raise RuntimeReadinessError("readiness requires unavailable HTTP, notification and scheduler hosts")
    workers = []
    class GroupSubprocess:
        def __getattr__(self, name):
            return getattr(subprocess, name)
        def Popen(self, *args, **kwargs):
            kwargs["start_new_session"] = False
            return subprocess.Popen(*args, **kwargs)
    class GateWorker(service_executor.ServiceWorker):
        def __init__(self, *args, **kwargs):
            workers.append(self)
            super().__init__(*args, **kwargs)
        def terminate(self):
            process = getattr(self, "process", None)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                for channel in (process.stdin, process.stdout):
                    if channel is not None:
                        channel.close()
            directory = getattr(self, "_context_directory", None)
            if directory is not None:
                shutil.rmtree(directory)
                self._context_directory = None
            self._closed = True
    # These references exist only in this fresh child, never in a controller or
    # user daemon. Guest process imports are absent; the outer group is the kill boundary.
    service_executor.subprocess = GroupSubprocess()
    core.ServiceWorker = GateWorker
    return _exercise(candidate, binary, scratch, core.RuntimeDaemon, workers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    # Wrapper owns the process group. It is never the user's process group.
    if os.getpgrp() != os.getpid():
        return 2
    try:
        result = _worker(args.candidate, args.binary, args.scratch)
    except Exception as error:
        result = {"status": "FAIL", "error": str(error)[:512], "business_function_acceptance": False}
    descriptor = os.open(args.scratch / "result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
