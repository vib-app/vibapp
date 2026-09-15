"""Platform-owned Linux Actions compiler. Never run scripts from the source tree.

Only this module and a reviewed dependency lock are installed on the default
branch. Generated sources are checked out separately at an immutable commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
import tomllib
import uuid

IMAGE = "docker.io/library/rust@sha256:5c5066e3f3bdd22a5cec7ba22ef0ee6e0bf6eaf63b65b63c9bf25f6f69a5e26a"
MAX_COMPONENT = 16 * 1024 * 1024
MARKER = ".vibapp-source-archive.json"
FEATURES = ["bitflags", "macro-string", "macros", "realloc"]


class CommandFailure(ValueError):
    def __init__(self, stderr):
        super().__init__("compiler-command-failed")
        self.diagnostics = stderr[-8192:]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_files(files):
    result = hashlib.sha256(b"VIBAPP-CODEAGENT-SOURCE\0")
    for path, data in sorted(files.items(), key=lambda item: item[0].encode()):
        result.update(path.encode() + b"\0" + hashlib.sha256(data).digest() + len(data).to_bytes(8, "big"))
    return result.hexdigest()


def validated_source(root, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("invalid-source-digest")
    files = {}
    total = 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        if Path(parent) == root:
            dirs[:] = [d for d in dirs if d != ".git"]
        for name in dirs + names:
            p = Path(parent) / name
            if p.is_symlink():
                raise ValueError("source-link")
        for name in names:
            p = Path(parent) / name
            rel = p.relative_to(root).as_posix()
            if rel == MARKER:
                continue
            if not (rel in ("Cargo.toml", "README.md", "manifest.intent.json", "wit/contract.wit") or
                    re.fullmatch(r"src/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.rs", rel)):
                raise ValueError("source-path-policy")
            info = p.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1048576:
                raise ValueError("source-file-policy")
            data = p.read_bytes()
            total += len(data)
            if total > 16 * 1048576 or len(files) >= 512:
                raise ValueError("source-size-policy")
            files[rel] = data
    if digest_files(files) != expected:
        raise ValueError("source-digest-mismatch")
    cargo = tomllib.loads(files["Cargo.toml"].decode())
    if set(cargo) != {"package", "lib", "dependencies"}:
        raise ValueError("cargo-sections")
    package = cargo["package"]
    if set(package) != {"name", "version", "edition", "publish", "autobins", "autoexamples", "autotests", "autobenches"}:
        raise ValueError("cargo-package")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", package["name"]) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", package["version"]):
        raise ValueError("cargo-identity")
    if package["edition"] != "2024" or any(package[k] is not False for k in ("publish", "autobins", "autoexamples", "autotests", "autobenches")):
        raise ValueError("cargo-automatic-target")
    if cargo["lib"] != {"crate-type": ["cdylib"]} or cargo["dependencies"] != {
        "wit-bindgen": {"version": "=0.60.0", "default-features": False, "features": FEATURES}
    }:
        raise ValueError("cargo-dependencies")
    return files, package


def bounded(command, limit, timeout=600):
    """Never allow compiler output or a hung Docker client to exhaust the host."""
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    streams = selectors.DefaultSelector()
    streams.register(proc.stdout, selectors.EVENT_READ, "out")
    streams.register(proc.stderr, selectors.EVENT_READ, "err")
    chunks = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while streams.get_map():
            if time.monotonic() > deadline:
                raise ValueError("compiler-timeout")
            for key, _ in streams.select(0.2):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    streams.unregister(key.fileobj)
                    continue
                buf = chunks[key.data]
                buf.extend(block)
                if len(buf) > (limit if key.data == "out" else 1048576):
                    raise ValueError("compiler-output-limit")
        if proc.wait(timeout=max(1, deadline-time.monotonic())):
            # Compiler diagnostics are private artifacts, never echoed to public logs.
            raise CommandFailure(bytes(chunks["err"]))
        return bytes(chunks["out"])
    finally:
        streams.close()
        if proc.poll() is None:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


def compile_source(root, policy, out, expected, request_id):
    files, package = validated_source(root, expected)
    out.mkdir(mode=0o700)
    work = out.parent / ("compile-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    source = work / "source"
    source.mkdir()
    for rel, data in files.items():
        p = source / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    lock = (policy / "Cargo.lock").read_text()
    lock = lock.replace('name = "vibapp-runtime-service-fixture"\nversion = "0.1.0"',
                        f'name = "{package["name"]}"\nversion = "{package["version"]}"')
    if sum("source" not in p for p in tomllib.loads(lock)["package"]) != 1:
        raise ValueError("invalid-policy-lock")
    fetch = work / "fetch"
    (fetch / "src").mkdir(parents=True)
    (fetch / "src/lib.rs").write_bytes(b"// Fetch only: no source execution.\n")
    (fetch / "Cargo.toml").write_bytes(files["Cargo.toml"])
    (fetch / "Cargo.lock").write_text(lock)
    # Only allowlisted manifest data, fixed lock and a dummy lib enter setup.
    dockerfile = f'''FROM {IMAGE}
RUN rustup target add --toolchain 1.93.0 wasm32-wasip2
WORKDIR /fetch
COPY Cargo.toml Cargo.lock ./
COPY src/lib.rs src/lib.rs
RUN cargo fetch --locked --target wasm32-wasip2 && cargo vendor --locked --offline /vendor > /fetch/config.toml && chmod -R a+rX /vendor /fetch
'''
    (fetch / "Dockerfile").write_text(dockerfile)
    image = "vibapp-build-" + uuid.uuid4().hex
    bounded(["docker", "build", "--quiet", "--tag", image, str(fetch)], 1024*1024)
    image_id = bounded(["docker", "image", "inspect", "--format", "{{.Id}}", image], 1024, 20).decode().strip()
    container = "vibapp-compile-" + uuid.uuid4().hex
    crate = package["name"].replace("-", "_")
    script = ('cp -R /input/. /work/; cp /fetch/Cargo.lock /work/Cargo.lock; '
              'mkdir -p /work/.cargo /tmp/cargo-home; cp /fetch/config.toml /work/.cargo/config.toml; cd /work; '
              'cargo build --release --target wasm32-wasip2 --locked --offline --jobs 1 >/work/build.log 2>&1 || '
              '{ tail -c 8192 /work/build.log >&2; exit 1; }; '
              f'test -s target/wasm32-wasip2/release/{crate}.wasm; '
              f'cat target/wasm32-wasip2/release/{crate}.wasm')
    command = ["docker", "run", "--rm", "--name", container, "--user", "65532:65532",
               "--network", "none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--cpus=2", "--memory=4g", "--memory-swap=4g", "--pids-limit=128",
               # Reviewed dependency build scripts/proc macros must execute in
               # this disposable container-only workspace. Docker tmpfs defaults
               # to noexec; other mounts remain read-only/non-executable.
               "--tmpfs", "/work:rw,exec,nosuid,nodev,size=3g,uid=65532,gid=65532,mode=0700",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,uid=65532,gid=65532",
               "--mount", f"type=bind,source={source.resolve()},target=/input,readonly",
               "--env", "CARGO_INCREMENTAL=0", "--env", "CARGO_NET_OFFLINE=true", "--env", "CARGO_HOME=/tmp/cargo-home",
               "--env", "SOURCE_DATE_EPOCH=1787796000", "--env", "RUSTFLAGS=--remap-path-prefix=/work=/workspace",
               "--env", "TZ=UTC", image_id, "sh", "-eu", "-c", script]
    try:
        component = bounded(command, MAX_COMPONENT)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=20)
    if component[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("not-a-component")
    (out / "component.wasm").write_bytes(component)
    (out / "Cargo.lock").write_text(lock)
    (out / "build.json").write_bytes(canonical({
        "schema_version": "vibapp.github-build.v1", "request_id": request_id,
        "source_digest_sha256": expected, "source_commit": os.environ["VIBAPP_SOURCE_SHA"],
        "workflow_commit": os.environ["GITHUB_SHA"], "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "component_sha256": hashlib.sha256(component).hexdigest(),
        "cargo_lock_sha256": hashlib.sha256(lock.encode()).hexdigest(),
        "builder_image": image_id, "bootstrap_image": IMAGE, "network": "none",
        "source_execution_observed": True, "production_isolation": False,
    }))
    print("WASI Component compiled; output remains untrusted until separate verification.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        compile_source(args.source, args.policy, args.output,
                       os.environ["VIBAPP_SOURCE_DIGEST"], os.environ["VIBAPP_REQUEST_ID"])
    except CommandFailure as error:
        # This log stays in the private source repository; never an artifact to
        # be copied into the public package. Compiler environment has no tokens.
        import sys
        sys.stderr.write(error.diagnostics.decode("utf-8", errors="replace"))
        raise
