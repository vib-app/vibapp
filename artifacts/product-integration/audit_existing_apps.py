#!/usr/bin/env python3
"""Freeze existing private catalog bytes; audit real packages in new private roots.

No models, builds, publication, user-state copies, or business-code rewriting.
Each package runs in a separately supervised process; retained product roots use
the ordinary local-appstore/runtime-daemon layout for actual GUI screenshots.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time

ARTIFACTS = Path(__file__).resolve().parents[1]
MAX_APPS = 64
MAX_JSON = 2 * 1024 * 1024
MAX_DB = 32 * 1024 * 1024
CHILD_DEADLINE = 35
SHA = re.compile(r"[0-9a-f]{64}\Z")
APP = re.compile(r"[a-z0-9][a-z0-9.-]{0,199}\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"


def bounded_bytes(path, maximum=MAX_JSON):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum:
        raise ValueError("Unsafe or oversized evidence file: " + str(path))
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("Evidence grew beyond bound")
    return value


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate evidence field")
            result[key] = value
        return result
    def reject_constant(_value):
        raise ValueError("Nonfinite evidence number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


def write_evidence(path, value):
    data = canonical(value)
    if len(data) > 8 * MAX_JSON:
        raise ValueError("Audit evidence exceeds bound")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
    os.replace(temporary, path)


def owned_directory(path):
    if path != path.resolve(strict=True):
        raise ValueError("Directory must be canonical and not linked")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Directory must be owned")


def freeze_catalog(label, root):
    """No LocalAppStore constructor here: it performs writes on user roots."""
    owned_directory(root)
    store = root / "local-appstore"
    owned_directory(store)
    database = store / "registry.sqlite3"
    # The store serializes writers under this existing lock. Do not create a
    # read lock or SQLite WAL/SHM files in the user's tree.
    lock_fd = os.open(store / ".local-appstore.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if any(path.exists() for path in (Path(str(database) + "-wal"), Path(str(database) + "-journal"))):
            raise ValueError("Catalog has an unproven journal; no immutable read")
        before = bounded_bytes(database, MAX_DB)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            releases = connection.execute(
                "SELECT app_id,app_version,package_digest,state,record_json FROM releases ORDER BY app_id,package_digest LIMIT ?",
                (MAX_APPS + 1,),
            ).fetchall()
        finally:
            connection.close()
        if len(releases) > MAX_APPS:
            raise ValueError("Catalog exceeds bounded application count")
        rows = []
        for app_id, version, digest, state, raw_record in releases:
            if not APP.fullmatch(app_id) or not SHA.fullmatch(digest) or state not in {"private", "withdrawn"}:
                raise ValueError("Catalog has an invalid application binding")
            record = strict_json(raw_record)
            candidate = store / "candidates" / digest / "candidate.json"
            row = {"origin": label, "source_product_root": str(root), "app_id": app_id,
                   "version": version, "package_digest_sha256": digest, "catalog_state": state,
                   "source_candidate": str(candidate), "inventory_error": None}
            try:
                owned_directory(candidate.parent)
                owned_directory(candidate.parent / "package")
                source_bytes = bounded_bytes(candidate)
                source = strict_json(source_bytes)
                manifest_bytes = bounded_bytes(candidate.parent / "package/manifest.json")
                manifest = strict_json(manifest_bytes)
                if source["package_digest_sha256"] != digest or manifest["app"]["id"] != app_id or record["app"]["id"] != app_id:
                    raise ValueError("Candidate identity differs from catalog")
                row.update(display_name=manifest["app"]["display_name"], app_kind=manifest["app"]["kind"],
                           entrypoints=manifest["entrypoints"], capabilities=manifest["capabilities"],
                           candidate_sha256=hashlib.sha256(source_bytes).hexdigest(),
                           manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest())
            except (OSError, ValueError, KeyError, TypeError) as error:
                row["inventory_error"] = {"type": type(error).__name__, "message": str(error)[:512]}
            rows.append(row)
        if bounded_bytes(database, MAX_DB) != before:
            raise ValueError("Catalog changed during inventory")
        return {"origin": label, "source_product_root": str(root),
                "catalog_sha256": hashlib.sha256(before).hexdigest(), "apps": rows}
    finally:
        os.close(lock_fd)


def process_identity(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
    if len(result.stdout) > 16 * 1024:
        raise ValueError("Process identity exceeds bound")
    return result.stdout.decode().strip() if result.returncode == 0 else None


def audit_child(row, directory, runtime_binary):
    sys.path.insert(0, str(ARTIFACTS / "registry-store"))
    sys.path.insert(0, str(ARTIFACTS / "runtime-daemon"))
    from local_appstore import LocalAppStore
    from vibapp_daemon import core, service_executor

    product = directory / "product"
    product.mkdir(mode=0o700)
    result = {**row, "status": "FAIL", "product_root": str(product), "operations": [],
              "ui": [], "services": [], "cleanup": {}, "scope": "real isolated launch/service health; not full business acceptance"}
    daemon = None
    spawned = []
    popen = subprocess.Popen
    sequence = 0

    def tracking_popen(arguments, *args, **kwargs):
        process = popen(arguments, *args, **kwargs)
        if isinstance(arguments, list) and arguments and arguments[0] == str(runtime_binary):
            # Publish handles immediately after spawn, including constructors
            # that later fail. Guest code cannot access this evidence directory.
            spawned.append(process)
            try:
                identity = process_identity(process.pid)
                handles.append({"pid": process.pid, "identity": identity})
                write_evidence(directory / "worker-handles.json", handles)
            except BaseException:
                process.kill()
                process.wait(timeout=2)
                raise
        return process

    def call(tag, value=None, *, subject=True, promotion=None):
        nonlocal sequence
        sequence += 1
        response = daemon.execute(
            {"request_id": f"existing-audit-{sequence}", "idempotency_key": f"existing-audit-{sequence}",
             "client": "cli", "subject": row["app_id"] if subject else None,
             "command": {"tag": tag, "value": value}},
            principal="uid:existing-app-audit", allowed_apps={row["app_id"]}, promotion_record=promotion,
        )
        result["operations"].append({"command": tag, "response": response})
        write_evidence(directory / "result.json", result)
        return response

    handles = []
    service_executor.subprocess.Popen = tracking_popen
    try:
        if row["inventory_error"]:
            raise ValueError("Frozen inventory contains an error")
        source = Path(row["source_candidate"])
        if hashlib.sha256(bounded_bytes(source)).hexdigest() != row["candidate_sha256"]:
            raise ValueError("Frozen candidate record changed")
        if hashlib.sha256(bounded_bytes(source.parent / "package/manifest.json")).hexdigest() != row["manifest_sha256"]:
            raise ValueError("Frozen manifest changed")
        store = LocalAppStore(product / "local-appstore")
        store.ingest(source)
        candidate = store.candidates / row["package_digest_sha256"] / "candidate.json"
        result["isolated_candidate"] = str(candidate)
        daemon = core.RuntimeDaemon(product / "runtime-daemon", store.candidates, service_runtime_binary=runtime_binary)
        installed = call("install", {"package_digest_sha256": row["package_digest_sha256"], "enable_after_install": False}, subject=False, promotion=candidate)
        if "error" in installed:
            return result
        enabled = call("enable")
        if "error" in enabled:
            return result
        for entry in row["entrypoints"]:
            if "desktop" not in entry.get("profiles", []):
                result["ui" if entry["kind"] == "launcher-ui" else "services"].append(
                    {"entrypoint": entry["id"], "status": "unsupported-profile"})
                continue
            if entry["kind"] == "launcher-ui":
                response = call("launch", {"entrypoint": entry["id"], "route": entry["routes"]["initial"]})
                ui = {"entrypoint": entry["id"], "status": "FAIL" if "error" in response else "PASS"}
                if "error" in response:
                    ui["error"] = response["error"]
                else:
                    binding = response["outcome"]["value"]
                    surface = binding["semantic_surface"]
                    ui.update(title=surface["view"]["title"], nodes=len(surface["view"]["nodes"]), generation=binding["generation"])
                    call("surface-close", {"session": binding["session"], "surface": binding["surface"]})
                result["ui"].append(ui)
            elif entry["kind"] == "service":
                response = call("service-start", {"entrypoint": entry["id"]})
                service = {"entrypoint": entry["id"], "status": "FAIL" if "error" in response else "PASS"}
                if "error" not in response:
                    response = call("service-health", {"entrypoint": entry["id"]})
                    if "error" in response:
                        service["status"] = "FAIL"
                if "error" in response:
                    service["error"] = response["error"]
                result["services"].append(service)
        final = call("status")
        result["enabled_for_screenshot"] = final.get("outcome", {}).get("value", {}).get("enabled") is True
        operations_clean = all("error" not in item["response"] for item in result["operations"])
        entrypoints_clean = all(item["status"] == "PASS" for item in result["ui"] + result["services"])
        if operations_clean and entrypoints_clean and (result["ui"] or result["services"]):
            result["status"] = "PASS" if result["ui"] else "PASS-service-no-window"
        return result
    except BaseException as error:
        result["exception"] = {"type": type(error).__name__, "message": str(error)[:512]}
        return result
    finally:
        cleanup_errors = []
        if daemon is not None:
            try:
                daemon.shutdown()
            except BaseException as error:
                cleanup_errors.append(str(error)[:512])
        for process in spawned:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        service_executor.subprocess.Popen = popen
        result["cleanup"] = {"worker_count": len(spawned), "all_reaped": all(p.poll() is not None for p in spawned), "errors": cleanup_errors}
        if cleanup_errors or not result["cleanup"]["all_reaped"]:
            result["status"] = "FAIL-cleanup"
        write_evidence(directory / "result.json", result)


def cleanup_orphans(directory):
    """Kill only exact recorded worker identities after an outer deadline."""
    path = directory / "worker-handles.json"
    outcomes = []
    if not path.exists():
        return outcomes
    handles = strict_json(bounded_bytes(path))
    if not isinstance(handles, list) or len(handles) > 64:
        raise ValueError("Invalid bounded worker handles")
    for record in handles:
        pid, identity = record["pid"], record["identity"]
        current = process_identity(pid)
        if current is None:
            outcomes.append({"pid": pid, "status": "already-exited"})
        elif identity is None or current != identity or str(directory / "product/runtime-daemon") not in current:
            outcomes.append({"pid": pid, "status": "unproven-not-killed"})
        else:
            os.kill(pid, signal.SIGKILL)
            outcomes.append({"pid": pid, "status": "killed-exact-owned-worker"})
    return outcomes


def run_audit(roots, output, runtime_binary):
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    catalogs = [freeze_catalog(label, root) for label, root in roots]
    inventory = {"schema_version": "vibapp.existing-app-audit-inventory-v1", "catalogs": catalogs,
                 "runtime_binary": str(runtime_binary),
                 "runtime_sha256": hashlib.sha256(bounded_bytes(runtime_binary, MAX_DB)).hexdigest()}
    rows = [row for catalog in catalogs for row in catalog["apps"]]
    if len(rows) > MAX_APPS:
        raise ValueError("Combined inventory exceeds bound")
    for index, row in enumerate(rows):
        row["audit_directory"] = str(output / f"{index:02d}-{row['origin']}-{row['app_id']}")
    write_evidence(output / "inventory.json", inventory)
    print(json.dumps({"inventory": str(output / "inventory.json"), "apps": len(rows)}), flush=True)
    results = []
    for row in rows:
        directory = Path(row["audit_directory"])
        directory.mkdir(mode=0o700)
        write_evidence(directory / "input.json", row)
        log = directory / "child.log"
        with log.open("xb") as stream:
            os.chmod(log, 0o600)
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(directory),
                                        "--runtime-binary", str(runtime_binary)], stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                process.wait(timeout=CHILD_DEADLINE)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                write_evidence(directory / "deadline.json", {"status": "FAIL-outer-deadline", "seconds": CHILD_DEADLINE})
        cleanup = cleanup_orphans(directory)
        result_path = directory / "result.json"
        result = strict_json(bounded_bytes(result_path, 8 * MAX_JSON)) if result_path.exists() else {**row, "status": "FAIL-no-result"}
        if process.returncode != 0 or (directory / "deadline.json").exists():
            result["status"] = "FAIL-child"
        result["outer_cleanup"] = cleanup
        if any(item["status"] == "unproven-not-killed" for item in cleanup):
            result["status"] = "FAIL-cleanup-unproven"
        write_evidence(directory / "result.json", result)
        summary = {key: result.get(key) for key in ("origin", "app_id", "app_kind", "display_name", "package_digest_sha256", "catalog_state", "status", "product_root", "isolated_candidate", "enabled_for_screenshot", "ui", "services", "cleanup", "exception")}
        summary["evidence"] = str(result_path)
        summary["errors"] = [item for item in result.get("operations", []) if "error" in item["response"]]
        results.append(summary)
        write_evidence(output / "summary.json", {"scope": "isolated startup only; screenshots and business acceptance separate", "apps": results})
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-root", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--runtime-binary", type=Path, required=True)
    arguments = parser.parse_args()
    runtime_binary = arguments.runtime_binary.resolve(strict=True)
    if arguments.child:
        directory = arguments.child.resolve(strict=True)
        if not directory.is_relative_to(ARTIFACTS / "product-integration/output"):
            parser.error("Child output must be under product-integration/output")
        owned_directory(directory)
        result = audit_child(strict_json(bounded_bytes(directory / "input.json")), directory, runtime_binary)
        print(json.dumps({"status": result["status"]}), flush=True)
        return 0
    if not arguments.output or not 1 <= len(arguments.product_root) <= 2:
        parser.error("Supply one or two explicit product roots and a new output directory")
    output = arguments.output.absolute()
    if output.parent.resolve(strict=True) != ARTIFACTS / "product-integration/output":
        parser.error("Output must be one new directory under product-integration/output")
    roots = []
    for item in arguments.product_root:
        label, separator, root = item.partition("=")
        if not separator or label not in {"native", "web"} or label in {pair[0] for pair in roots}:
            parser.error("Product root syntax: native=/absolute/path or web=/absolute/path")
        roots.append((label, Path(root).resolve(strict=True)))
    run_audit(roots, output, runtime_binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
