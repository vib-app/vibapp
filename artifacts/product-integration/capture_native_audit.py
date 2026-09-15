#!/usr/bin/env python3
"""Capture real macOS app windows from a previously prepared private audit root.

Uses the normal signed Launcher and daemon control interface. It never renders
screenshots itself and never labels captured pixels as visually accepted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid
import zlib

from audit_existing_apps import bounded_bytes, strict_json, write_evidence

PYTHON = "/opt/homebrew/bin/python3.11"
SCREENSHOT = "/Users/zhuzhe/.codex/skills/screenshot/scripts/take_screenshot.py"
WINDOW_HELPER = Path(__file__).with_name("native_audit_windows.swift")
MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_PNG_RAW_BYTES = 64 * 1024 * 1024


def run(arguments, **kwargs):
    # Do not inherit screenshot test-mode switches, provider credentials or
    # Python/Swift overrides into the OS evidence helpers.
    kwargs.setdefault("env", {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"})
    return subprocess.run(arguments, capture_output=True, timeout=15, check=True, **kwargs)


def process_rows():
    rows = run(["/bin/ps", "-axo", "pid=,lstart=,command="]).stdout.decode().splitlines()
    if len(rows) > 10000:
        raise ValueError("Process inventory exceeds audit bound")
    return {int(parts[0]): line.strip() for line in rows if (parts := line.split())}


def windows(module_cache):
    output = run(["/usr/bin/swift", "-module-cache-path", str(module_cache), str(WINDOW_HELPER)]).stdout
    if len(output) > 1024 * 1024:
        raise ValueError("Window metadata exceeds audit bound")
    payload = strict_json(output)
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "windows"}
            or payload["schema_version"] != "vibapp.native-window-metadata-v1"
            or not isinstance(payload["windows"], list) or len(payload["windows"]) > 512):
        raise ValueError("Invalid bounded OS window metadata")
    result = {}
    for item in payload["windows"]:
        if (not isinstance(item, dict) or set(item) != {"id", "owner_pid", "owner", "title", "width", "height"}
                or any(type(item[key]) is not int or item[key] <= 0 for key in ("id", "owner_pid", "width", "height"))
                or any(not isinstance(item[key], str) or len(item[key]) > 1024 for key in ("owner", "title"))
                or item["id"] in result):
            raise ValueError("Invalid or duplicate OS window identity")
        result[item["id"]] = item
    return result


def select_window(before, observed, title, pid):
    matches = [item for wid, item in observed.items()
               if wid not in before and item["title"] == title and item["owner_pid"] == pid]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one new app window owned by the matched Launcher PID")
    return matches[0]


def current_window(item, module_cache):
    if process_rows().get(item["pid"]) != item["process_identity"]:
        raise RuntimeError("Launcher PID/start-time changed before screenshot evidence completed")
    observed = windows(module_cache).get(item["window_id"])
    if (observed is None or observed["owner_pid"] != item["pid"]
            or observed["title"] != item["window_metadata"]["title"]):
        raise RuntimeError("Screenshot window ownership changed")
    return observed


def png_evidence(path):
    """Validate bounded PNG bytes and pin the exact captured file, not stdout."""
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Screenshot must be an existing canonical non-linked PNG")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or not 57 <= before.st_size <= MAX_PNG_BYTES):
            raise ValueError("Screenshot must be a bounded owned regular PNG")
        data = stream.read(MAX_PNG_BYTES + 1)
        after = os.fstat(stream.fileno())
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if len(data) != before.st_size or identity(before) != identity(after) or identity(after) != identity(path.lstat()):
        raise ValueError("Screenshot changed while it was being recorded")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Screenshot is not PNG")
    offset, header, compressed, ended, saw_idat, left_idat, palette = 8, None, bytearray(), False, False, False, False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data) or not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind):
            raise ValueError("Invalid PNG chunk")
        payload = data[offset + 8:end - 4]
        if zlib.crc32(kind + payload) & 0xffffffff != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("PNG checksum mismatch")
        if header is None and kind != b"IHDR":
            raise ValueError("PNG must start with IHDR")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise ValueError("Invalid PNG header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            if palette or saw_idat or length == 0 or length > 768 or length % 3:
                raise ValueError("Invalid PNG palette")
            palette = True
        elif kind == b"IDAT":
            if left_idat:
                raise ValueError("Noncontiguous PNG image data")
            compressed.extend(payload)
            saw_idat = True
        elif kind == b"IEND":
            if length or end != len(data) or not saw_idat:
                raise ValueError("Invalid PNG end")
            ended = True
        elif not kind[0] & 32:
            raise ValueError("Unknown critical PNG chunk")
        if saw_idat and kind != b"IDAT":
            left_idat = True
        offset = end
    if not ended or header is None:
        raise ValueError("Incomplete PNG")
    width, height, depth, color, compression, filtering, interlace = header
    depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
    if (not 16 <= width <= 16384 or not 16 <= height <= 16384 or depth not in depths.get(color, set())
            or compression != 0 or filtering != 0 or interlace not in {0, 1} or (color == 3 and not palette)):
        raise ValueError("Unsupported or implausible screenshot PNG dimensions/header")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = [(0, 0, 1, 1)] if interlace == 0 else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
    ]
    scanlines = []
    for x, y, dx, dy in passes:
        columns, lines = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if columns and lines:
            scanlines.append((lines, (columns * channels * depth + 7) // 8 + 1))
    expected = sum(lines * size for lines, size in scanlines)
    if expected > MAX_PNG_RAW_BYTES:
        raise ValueError("PNG decoded image exceeds audit bound")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected + 1)
    except zlib.error as error:
        raise ValueError("Invalid PNG compressed image") from error
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid or oversized PNG decoded image")
    offset = 0
    for lines, size in scanlines:
        for _ in range(lines):
            if raw[offset] > 4:
                raise ValueError("Invalid PNG scanline filter")
            offset += size
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "width": width, "height": height}


def capture_png(window_id, destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refusing to overwrite earlier screenshot evidence")
    reply = run([PYTHON, "-I", "-B", SCREENSHOT, "--window-id", str(window_id), "--path", str(destination)])
    paths = reply.stdout.decode("utf-8", errors="strict").splitlines()
    if paths != [str(destination)]:
        raise ValueError("Screenshot helper did not return the one expected output path")
    return png_evidence(destination)


def control(bundle, root, app_id, tag, value=None):
    envelope = {"request_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4()),
                "client": "cli", "subject": app_id, "command": {"tag": tag, "value": value}}
    module = bundle / "Contents/Resources/runtime-daemon"
    response = run([PYTHON, "-I", "-B", "-c",
                    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')",
                    str(module), "ctl", "--socket", str(root / "runtime-daemon/run/vibappd.sock")],
                   input=json.dumps(envelope).encode())
    return strict_json(response.stdout)


def capture(preparation, bundle, evidence, selected):
    prepared = strict_json(bounded_bytes(preparation, 8 * 1024 * 1024))
    root = Path(prepared["product_root"])
    info = root.lstat()
    if (root != root.resolve(strict=True) or root.parent != Path("/private/tmp")
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Only the prepared short owner-private audit root is allowed")
    if prepared["status"] != "PASS" or prepared["user_data_copied"] or prepared["model_or_auth_configured"]:
        raise ValueError("Screenshot preparation did not pass")
    launcher = bundle / "Contents/MacOS/vibapp-launcher"
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)])
    network = strict_json(bounded_bytes(root / "settings/p2p-network.json"))
    if any(network[key] for key in ["p2p_enabled", "rtc_enabled", "turn_enabled"]):
        raise ValueError("Audit networking must remain disabled")
    rows = [row for row in prepared["apps"] if not selected or row["app_id"] in selected]
    if not rows or (selected and set(selected) != {r["app_id"] for r in rows}):
        raise ValueError("Unknown screenshot app selection")
    evidence = evidence.resolve(strict=False)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    module_cache = evidence / "swift-module-cache"
    module_cache.mkdir(mode=0o700)
    report = {"status": "IN_PROGRESS", "root": str(root), "bundle": str(bundle),
              "launcher_sha256": hashlib.sha256(bounded_bytes(launcher, 32 * 1024 * 1024)).hexdigest(),
              "preparation": str(preparation), "accepted_screenshots": 0, "apps": [],
              "capture_evidence_version": "owner-pid-and-png-v1",
              "window_helper_sha256": hashlib.sha256(bounded_bytes(WINDOW_HELPER)).hexdigest()}
    try:
        for index, row in enumerate(rows):
            expected = f"{launcher} --data-dir {root} --open-app {row['app_id']}"
            before_processes, before_windows = process_rows(), windows(module_cache)
            if any(line.endswith(expected) for line in before_processes.values()):
                raise ValueError("This audit app already has a Launcher process; close it first")
            item = {**row, "status": "STARTING", "pid": None, "process_identity": None,
                    "window_id": None, "screenshot": None, "visually_accepted": False, "cleanup": False}
            report["apps"].append(item)
            write_evidence(evidence / "report.json", report)
            try:
                run(["/usr/bin/open", "-n", "-a", str(bundle), "--args", "--data-dir", str(root), "--open-app", row["app_id"]])
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    matches = [(pid, identity) for pid, identity in process_rows().items()
                               if pid not in before_processes and identity.endswith(expected)]
                    if len(matches) > 1:
                        raise ValueError("Ambiguous native process identity")
                    if matches:
                        item["pid"], item["process_identity"] = matches[0]
                        write_evidence(evidence / "report.json", report)
                        break
                    time.sleep(.2)
                if item["pid"] is None:
                    raise RuntimeError("macOS did not start the requested native app")
                # Only a bounded presentation settling interval; real status is checked below.
                time.sleep(4)
                title = row["display_name"] + " — VibApp"
                item["window_metadata"] = select_window(before_windows, windows(module_cache), title, item["pid"])
                item["window_id"] = item["window_metadata"]["id"]
                status_response = control(bundle, root, row["app_id"], "status")
                item["daemon_status"] = status_response
                status_value = status_response["outcome"]["value"]
                if (status_value["app"] != row["app_id"] or status_value["enabled"] is not True
                        or status_value["package_digest_sha256"] != row["package_digest_sha256"]):
                    raise ValueError("Native app active package does not match frozen audit")
                active = [s for s in status_value["surfaces"] if s["state"] == "open"]
                if len(active) != 1:
                    raise RuntimeError("Native window has no single active runtime surface")
                item["runtime_surface"] = active[0]
                item["window_before_capture"] = current_window(item, module_cache)
                destination = evidence / f"{index:02d}-{hashlib.sha256(row['app_id'].encode()).hexdigest()[:16]}.png"
                item["screenshot_evidence"] = capture_png(item["window_id"], destination)
                item["window_after_capture"] = current_window(item, module_cache)
                item["screenshot_evidence"]["captured_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                item["screenshot"] = item["screenshot_evidence"]["path"]
                item["status"] = "CAPTURED_AWAITING_VISUAL_REVIEW"
                print(json.dumps({"app": row["app_id"], "screenshot": item["screenshot"], "pid": item["pid"]}), flush=True)
            except Exception as error:
                item["status"] = "FAIL"
                item["error"] = str(error)[:1200]
                raise
            finally:
                pid = item["pid"]
                if pid is not None and process_rows().get(pid) == item["process_identity"]:
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while pid in process_rows() and time.monotonic() < deadline:
                        time.sleep(.2)
                if pid is not None and pid in process_rows():
                    raise RuntimeError("Native audit process cleanup unproven")
                status_response = control(bundle, root, row["app_id"], "status")
                for surface in status_response["outcome"]["value"]["surfaces"]:
                    if surface["state"] == "open":
                        control(bundle, root, row["app_id"], "surface-close",
                                {"session": surface["session"], "surface": surface["surface"]})
                item["cleanup_status"] = control(bundle, root, row["app_id"], "status")
                item["cleanup"] = not any(s["state"] == "open" for s in item["cleanup_status"]["outcome"]["value"]["surfaces"])
                write_evidence(evidence / "report.json", report)
                if not item["cleanup"]:
                    raise RuntimeError("Audit runtime surface cleanup unproven")
        report["status"] = "CAPTURED_AWAITING_VISUAL_REVIEW"
    except BaseException:
        report["status"] = "FAIL"
        raise
    finally:
        write_evidence(evidence / "report.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ["preparation", "bundle", "evidence"]:
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--app-id", action="append", default=[])
    args = parser.parse_args()
    capture(args.preparation.resolve(strict=True), args.bundle.resolve(strict=True), args.evidence, args.app_id)
