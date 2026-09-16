from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Any

from .core import MAX_ENVELOPE_BYTES, TRANSPORT_SCHEMA, DaemonError, RuntimeDaemon, canonical_json, loads_strict_json


def _peer_uid(connection: socket.socket) -> int:
    if hasattr(connection, "getpeereid"):
        uid, _gid = connection.getpeereid()  # type: ignore[attr-defined]
        return int(uid)
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return int(uid)
    if hasattr(socket, "LOCAL_PEERCRED"):
        # Darwin exposes struct xucred through SOL_LOCAL(0)/LOCAL_PEERCRED.
        # Only the fixed cr_version and cr_uid prefix is needed here.
        credentials = connection.getsockopt(0, socket.LOCAL_PEERCRED, 128)
        version, uid = struct.unpack_from("=II", credentials)
        if version != 0:
            raise DaemonError("permission-denied", "local peer credential version is unsupported")
        return int(uid)
    raise DaemonError("permission-denied", "local peer credentials are unavailable")


def _read_one_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(8192, MAX_ENVELOPE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_ENVELOPE_BYTES:
            raise DaemonError("resource-limit", "transport request exceeds the byte ceiling")
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    if data.count(b"\n") != 1 or not data.endswith(b"\n"):
        raise DaemonError("malformed-output", "transport requires exactly one newline-delimited JSON object")
    return data[:-1]


def serve(args: argparse.Namespace) -> int:
    runtime_root = Path(args.root).expanduser().resolve()
    promotion_root = Path(args.promotion_root).expanduser().resolve()
    daemon = RuntimeDaemon(runtime_root, promotion_root)
    socket_path = Path(args.socket).expanduser().resolve()
    if not socket_path.is_relative_to(runtime_root) or socket_path == runtime_root:
        raise SystemExit("socket path must stay below the explicit runtime root")
    if os.name == "nt":
        from .windows_transport import Listener
        return _serve_windows(daemon, Listener(str(socket_path)))
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(socket_path.parent, 0o700)
    if socket_path.exists():
        existing = socket_path.lstat()
        if not stat.S_ISSOCK(existing.st_mode):
            raise SystemExit("refusing to replace a non-socket runtime path")
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            probe.connect(str(socket_path))
        except OSError:
            socket_path.unlink()
        else:
            probe.close()
            raise SystemExit("socket is already active")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(16)
    server.settimeout(0.5)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    uid = _peer_uid(connection)
                    if uid != os.getuid():
                        raise DaemonError("permission-denied", "peer UID is not the daemon owner")
                    transport = loads_strict_json(_read_one_line(connection), maximum=MAX_ENVELOPE_BYTES)
                    if not isinstance(transport, dict) or set(transport) != {"schema_version", "envelope", "promotion_record"} or transport.get("schema_version") != TRANSPORT_SCHEMA:
                        raise DaemonError("invalid-argument", "transport wrapper fields are invalid")
                    response = daemon.execute(transport["envelope"], principal=f"uid:{uid}", allowed_apps={"*"}, promotion_record=transport["promotion_record"])
                except DaemonError as error:
                    response = {"request_id": "unknown", "error": error.as_dict()}
                try:
                    connection.sendall(canonical_json(response) + b"\n")
                except (BrokenPipeError, ConnectionResetError):
                    # Readiness probes may connect and close without completing a
                    # transport frame.  A peer-local disconnect must not terminate
                    # the long-lived lifecycle owner.
                    pass
    finally:
        daemon.shutdown()
        server.close()
        if socket_path.exists():
            socket_path.unlink()
    return 0


def control(args: argparse.Namespace) -> int:
    if args.envelope == "-":
        raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
    else:
        raw = Path(args.envelope).read_bytes()
    envelope = loads_strict_json(raw, maximum=MAX_ENVELOPE_BYTES)
    transport = {"schema_version": TRANSPORT_SCHEMA, "envelope": envelope, "promotion_record": args.promotion_record}
    if os.name == "nt":
        from .windows_transport import connect
        client = connect(str(Path(args.socket).expanduser().resolve()), args.timeout)
    else:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(args.timeout)
        client.connect(str(Path(args.socket).expanduser().resolve()))
    try:
        client.sendall(canonical_json(transport) + b"\n")
        response = _read_one_line(client)
        if os.name == "nt":
            client.sendall(b"\0")
    finally:
        client.close()
    sys.stdout.buffer.write(response + b"\n")
    parsed = loads_strict_json(response, maximum=MAX_ENVELOPE_BYTES)
    return 1 if isinstance(parsed, dict) and "error" in parsed else 0


def _serve_windows(daemon, server) -> int:
    from .windows_security import process_sid
    import time
    try:
        while True:
            try:
                connection, _ = server.accept()
            except TimeoutError:
                time.sleep(0.02)
                continue
            except OSError:
                server.close()
                continue
            with connection:
                try:
                    transport = loads_strict_json(_read_one_line(connection), maximum=MAX_ENVELOPE_BYTES)
                    if not isinstance(transport, dict) or set(transport) != {"schema_version", "envelope", "promotion_record"} or transport.get("schema_version") != TRANSPORT_SCHEMA:
                        raise DaemonError("invalid-argument", "transport wrapper fields are invalid")
                    response = daemon.execute(transport["envelope"], principal=f"sid:{process_sid()}", allowed_apps={"*"}, promotion_record=transport["promotion_record"])
                except DaemonError as error:
                    response = {"request_id": "unknown", "error": error.as_dict()}
                except (TimeoutError, OSError):
                    continue
                try:
                    connection.sendall(canonical_json(response) + b"\n")
                    connection.recv(1)  # bounded delivery acknowledgement before pipe close
                except (OSError, TimeoutError):
                    pass
    finally:
        daemon.shutdown()
        server.close()


def probe(args):
    if os.name == "nt":
        from .windows_transport import connect
        with connect(str(Path(args.socket).expanduser().resolve()), 0.1):
            return 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.1)
        connection.connect(str(Path(args.socket).expanduser().resolve()))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="vibapp-runtime-daemon")
    subcommands = result.add_subparsers(dest="subcommand", required=True)
    server = subcommands.add_parser("serve", help="run the owner-authenticated local daemon")
    server.add_argument("--root", required=True)
    server.add_argument("--promotion-root", required=True)
    server.add_argument("--socket", required=True)
    server.set_defaults(handler=serve)
    ctl = subcommands.add_parser("ctl", help="send one canonical daemon envelope")
    ctl.add_argument("--socket", required=True)
    ctl.add_argument("--envelope", default="-", help="JSON file or - for stdin")
    ctl.add_argument(
        "--promotion-record",
        help="required for install/update; must be under daemon promotion root",
    )
    ctl.add_argument("--timeout", type=float, default=5.0)
    ctl.set_defaults(handler=control)
    readiness = subcommands.add_parser("probe", help="verify the owner-authenticated endpoint")
    readiness.add_argument("--socket", required=True)
    readiness.set_defaults(handler=probe)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    return int(arguments.handler(arguments))
