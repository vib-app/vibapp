#!/usr/bin/env python3
"""Assemble and independently verify the minimal offline Builder Cargo cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
import tomllib
from typing import Any, Iterable


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
DEFAULT_LOCK = REPO / "artifacts/runtime-daemon/tests/service-fixture/Cargo.lock"
DEFAULT_GENERATED = REPO / "generated"
DEFAULT_TOOL_LAYER = (
    REPO
    / "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980"
)
EXPECTED_TOOL_LAYER_SHA256 = "89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980"
SCHEMA = "vibapp.builder-offline-cache-evidence.experimental-v1"
ACCEPTANCE_SCHEMA = "vibapp.offline-cache-acceptance.experimental-v1"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_FILE_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_TOTAL_BYTES = 256 * 1024 * 1024
NATIVE_SUFFIXES = {".a", ".asm", ".c", ".cc", ".cpp", ".cxx", ".o", ".s"}
APPROVED_WIT_BINDGEN_WASM_SUPPORT = [
    "src/rt/libwit_bindgen_cabi.a",
    "src/rt/wit_bindgen_cabi_realloc.c",
    "src/rt/wit_bindgen_cabi_realloc.o",
    "src/rt/wit_bindgen_cabi_wasip3.c",
    "src/rt/wit_bindgen_cabi_wasip3.o",
]
APPROVED_NON_NATIVE_LINKS = {
    (
        "prettyplease",
        "0.2.37",
        "479ca8adacdd7ce8f1fb39ce9ecccbfe93a3f1344b3d0d97f20bc0196208f62b",
        "prettyplease02",
    )
}


class CacheError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_regular(path: Path, maximum: int, context: str, *, allow_empty: bool = False) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise CacheError(f"{context} must be one regular non-linked file")
    minimum = 0 if allow_empty else 1
    if metadata.st_size < minimum or metadata.st_size > maximum:
        raise CacheError(f"{context} size is outside {minimum}..{maximum}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise CacheError(f"{context} changed while opening")
        payload = b""
        while len(payload) <= maximum:
            block = os.read(descriptor, min(65_536, maximum + 1 - len(payload)))
            if not block:
                break
            payload += block
        if len(payload) != metadata.st_size:
            raise CacheError(f"{context} changed while reading")
        return payload
    finally:
        os.close(descriptor)


def write_exclusive(path: Path, payload: bytes, mode: int = 0o444) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def normalized_member(name: str, archive_root: str) -> str:
    if not name or "\\" in name or name.startswith("/") or "\0" in name:
        raise CacheError(f"unsafe archive member: {name!r}")
    pure = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise CacheError(f"unsafe archive member: {name!r}")
    if not pure.parts or pure.parts[0] != archive_root:
        raise CacheError(f"archive member is outside {archive_root}: {name!r}")
    relative = PurePosixPath(*pure.parts[1:]).as_posix()
    if relative in {"", "."}:
        return ""
    return relative


def load_lock(path: Path) -> tuple[bytes, list[dict[str, str]], dict[str, str]]:
    payload = read_regular(path, 4 * 1024 * 1024, "reference Cargo.lock")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CacheError(f"reference Cargo.lock is invalid: {error}") from error
    if document.get("version") != 4 or not isinstance(document.get("package"), list):
        raise CacheError("reference Cargo.lock must be a v4 lock")
    packages: list[dict[str, str]] = []
    roots: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for row in document["package"]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("version"), str):
            raise CacheError("reference Cargo.lock has a malformed package")
        key = (row["name"], row["version"])
        if key in seen:
            raise CacheError(f"duplicate locked package: {key[0]} {key[1]}")
        seen.add(key)
        source = row.get("source")
        checksum = row.get("checksum")
        if source is None:
            roots[row["name"]] = row["version"]
            continue
        if source != "registry+https://github.com/rust-lang/crates.io-index":
            raise CacheError(f"non-crates.io source is forbidden: {row['name']} {source}")
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise CacheError(f"missing checksum: {row['name']} {row['version']}")
        packages.append({"name": row["name"], "version": row["version"], "checksum": checksum})
    packages.sort(key=lambda item: (item["name"].encode(), item["version"].encode()))
    if roots != {"vibapp-runtime-service-fixture": "0.1.0"}:
        raise CacheError(f"unexpected reference workspace roots: {roots}")
    direct = next((item for item in packages if item["name"] == "wit-bindgen"), None)
    if direct != {
        "name": "wit-bindgen",
        "version": "0.60.0",
        "checksum": "a301904d6657d6364c758d869e5389d05d393b16d5b65db60b4f03cbe71bb80d",
    }:
        raise CacheError("reference lock does not bind exact wit-bindgen 0.60.0")
    return payload, packages, roots


def locate_archive(package: dict[str, str], archive_roots: Iterable[Path]) -> tuple[Path, str]:
    filename = f"{package['name']}-{package['version']}.crate"
    selected: Path | None = None
    selected_class = ""
    for index, root in enumerate(archive_roots):
        root = root.resolve(strict=True)
        candidate = root / filename
        if not candidate.exists():
            continue
        payload = read_regular(candidate, MAX_ARCHIVE_BYTES, filename)
        observed = sha256_bytes(payload)
        if observed != package["checksum"]:
            raise CacheError(f"local archive checksum mismatch: {filename}")
        if selected is None:
            selected = candidate
            selected_class = f"explicit-local-archive-cache-{index + 1}"
    if selected is None:
        raise CacheError(f"locked archive is absent from explicit local caches: {filename}")
    return selected, selected_class


def extract_archive(archive: Path, package: dict[str, str], vendor: Path) -> dict[str, Any]:
    package_id = f"{package['name']}-{package['version']}"
    destination = vendor / package_id
    destination.mkdir(mode=0o700)
    file_hashes: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        members.sort(key=lambda item: item.name.encode("utf-8"))
        for member in members:
            relative = normalized_member(member.name, package_id)
            if not relative:
                if not member.isdir():
                    raise CacheError(f"archive root is not a directory: {package_id}")
                continue
            if relative in seen:
                raise CacheError(f"duplicate archive member: {package_id}/{relative}")
            seen.add(relative)
            output = destination.joinpath(*PurePosixPath(relative).parts)
            if member.isdir():
                output.mkdir(parents=True, mode=0o700, exist_ok=True)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise CacheError(f"archive has a link or special member: {package_id}/{relative}")
            if member.size < 0 or member.size > MAX_EXTRACTED_FILE_BYTES:
                raise CacheError(f"archive member is too large: {package_id}/{relative}")
            total += member.size
            if total > MAX_EXTRACTED_TOTAL_BYTES:
                raise CacheError(f"archive expands beyond limit: {package_id}")
            source = bundle.extractfile(member)
            if source is None:
                raise CacheError(f"archive member cannot be read: {package_id}/{relative}")
            payload = source.read(MAX_EXTRACTED_FILE_BYTES + 1)
            if len(payload) != member.size or len(payload) > MAX_EXTRACTED_FILE_BYTES:
                raise CacheError(f"archive member changed size: {package_id}/{relative}")
            write_exclusive(output, payload, mode=0o400)
            file_hashes[relative] = sha256_bytes(payload)
    if "Cargo.toml" not in file_hashes:
        raise CacheError(f"archive lacks Cargo.toml: {package_id}")
    checksum = {"files": dict(sorted(file_hashes.items())), "package": package["checksum"]}
    write_exclusive(destination / ".cargo-checksum.json", canonical_json(checksum) + b"\n", mode=0o400)
    return {"package_id": package_id, "file_count": len(file_hashes), "expanded_bytes": total}


def inspect_package(vendor_child: Path, locked: dict[str, str]) -> dict[str, Any]:
    cargo_path = vendor_child / "Cargo.toml"
    payload = read_regular(cargo_path, 2 * 1024 * 1024, f"{vendor_child.name}/Cargo.toml")
    try:
        cargo = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CacheError(f"invalid vendored Cargo.toml: {vendor_child.name}: {error}") from error
    package = cargo.get("package")
    if not isinstance(package, dict) or package.get("name") != locked["name"] or str(package.get("version")) != locked["version"]:
        raise CacheError(f"vendored identity mismatch: {vendor_child.name}")
    library = cargo.get("lib", {})
    proc_macro = isinstance(library, dict) and library.get("proc-macro") is True
    build_value = package.get("build")
    build_script = (vendor_child / "build.rs").is_file() or (
        isinstance(build_value, str) and build_value not in {"", "false"}
    )
    build_script_sha256 = None
    if build_script:
        build_path = vendor_child / (build_value if isinstance(build_value, str) else "build.rs")
        build_script_sha256 = sha256_bytes(
            read_regular(build_path, MAX_EXTRACTED_FILE_BYTES, f"{vendor_child.name}/build script")
        )
    links = package.get("links")
    native_sources: list[str] = []
    for path in sorted(vendor_child.rglob("*")):
        if path.is_file() and path.suffix.lower() in NATIVE_SUFFIXES:
            native_sources.append(path.relative_to(vendor_child).as_posix())
    license_value = package.get("license")
    return {
        **locked,
        "license": license_value if isinstance(license_value, str) else None,
        "proc_macro": proc_macro,
        "build_script": build_script,
        "build_script_sha256": build_script_sha256,
        "links": links if isinstance(links, str) else None,
        "native_sources": native_sources,
    }


def inventory_tree(root: Path) -> tuple[list[dict[str, Any]], str]:
    root = root.resolve(strict=True)
    entries: list[dict[str, Any]] = []
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode())]:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise CacheError(f"cache contains a link or special path: {relative}")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise CacheError(f"cache contains a hard-linked file: {relative}")
        row: dict[str, Any] = {
            "path": relative,
            "type": "directory" if stat.S_ISDIR(metadata.st_mode) else "regular",
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "nlink": metadata.st_nlink,
            "size_bytes": metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
            "sha256": None,
        }
        if stat.S_ISREG(metadata.st_mode):
            row["sha256"] = sha256_bytes(
                read_regular(path, MAX_EXTRACTED_FILE_BYTES, relative, allow_empty=True)
            )
        entries.append(row)
    identity_rows = [
        {key: row[key] for key in ("path", "type", "mode", "size_bytes", "sha256")}
        for row in entries
    ]
    return entries, sha256_bytes(canonical_json(identity_rows))


def seal_tree(root: Path) -> None:
    paths = [root, *root.rglob("*")]
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o555)
        elif stat.S_ISREG(metadata.st_mode):
            path.chmod(0o444)
        else:
            raise CacheError(f"cannot seal link or special path: {path}")


def atomic_root_json(path: Path, value: Any) -> None:
    payload = canonical_json(value) + b"\n"
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    write_exclusive(temporary, payload, mode=0o600)
    os.replace(temporary, path)
    path.chmod(0o444)


def assemble(lock_path: Path, archive_roots: list[Path], generated: Path) -> Path:
    if not archive_roots:
        raise CacheError("at least one explicit local archive cache is required")
    lock_bytes, packages, _ = load_lock(lock_path.resolve(strict=True))
    generated = generated.resolve(strict=True)
    if generated != DEFAULT_GENERATED.resolve(strict=True):
        raise CacheError("cache output must be the repository generated/ directory")
    staging = Path(tempfile.mkdtemp(prefix=".builder-cargo-cache-", dir=generated))
    staging.chmod(0o700)
    try:
        cargo_home = staging / "cargo-home"
        vendor = cargo_home / "vendor"
        vendor.mkdir(parents=True, mode=0o700)
        # Cargo resolves a directory source in CARGO_HOME/config.toml relative to
        # the parent of CARGO_HOME. Keep the path relocatable while still binding
        # it to the sealed cache tree.
        config = b'''[net]\noffline = true\nretry = 0\n\n[source.crates-io]\nreplace-with = "vibapp-vendor"\n\n[source.vibapp-vendor]\ndirectory = "cargo-home/vendor"\n'''
        write_exclusive(cargo_home / "config.toml", config, mode=0o400)
        archive_evidence = []
        expansion = []
        for package in packages:
            archive, source_class = locate_archive(package, archive_roots)
            archive_evidence.append(
                {
                    **package,
                    "archive_source": source_class,
                    "archive_sha256": package["checksum"],
                }
            )
            expansion.append(extract_archive(archive, package, vendor))
        package_inventory = [
            inspect_package(vendor / f"{item['name']}-{item['version']}", item)
            for item in packages
        ]
        links_rows = {
            (item["name"], item["version"], item["checksum"], item["links"])
            for item in package_inventory
            if item["links"] is not None
        }
        if links_rows != APPROVED_NON_NATIVE_LINKS:
            raise CacheError("guest cache links inventory differs from the exact non-native allowlist")
        native_rows = [item for item in package_inventory if item["native_sources"]]
        if len(native_rows) != 1 or native_rows[0]["name"] != "wit-bindgen" or native_rows[0]["version"] != "0.60.0" or native_rows[0]["native_sources"] != APPROVED_WIT_BINDGEN_WASM_SUPPORT:
            raise CacheError("guest cache native/binary inventory differs from exact wit-bindgen Wasm support files")
        proc_macros = [
            {"name": item["name"], "version": item["version"], "checksum": item["checksum"]}
            for item in package_inventory
            if item["proc_macro"]
        ]
        if not proc_macros:
            raise CacheError("proc-macro inventory is unexpectedly empty")
        write_exclusive(staging / "reference-Cargo.lock", lock_bytes, mode=0o400)
        write_exclusive(
            staging / "package-inventory.json",
            canonical_json(
                {
                    "schema_version": SCHEMA,
                    "registry_package_count": len(package_inventory),
                    "packages": package_inventory,
                    "proc_macros": proc_macros,
                    "build_scripts": [
                        {"name": item["name"], "version": item["version"], "checksum": item["checksum"]}
                        for item in package_inventory
                        if item["build_script"]
                    ],
                    "non_native_links": [
                        {"name": name, "version": version, "checksum": checksum, "links": links}
                        for name, version, checksum, links in sorted(APPROVED_NON_NATIVE_LINKS)
                    ],
                    "host_native_source_count": 0,
                    "approved_wasm_target_support_artifacts": APPROVED_WIT_BINDGEN_WASM_SUPPORT,
                }
            )
            + b"\n",
            mode=0o400,
        )
        seal_tree(cargo_home)
        entries, cache_digest = inventory_tree(cargo_home)
        destination = generated / f"builder-cargo-cache-sha256-{cache_digest}"
        if destination.exists() or destination.is_symlink():
            raise CacheError(f"content-addressed cache already exists: {destination}")
        inventory = {
            "schema_version": SCHEMA,
            "cache_root_sha256": cache_digest,
            "cargo_home_relative": "cargo-home",
            "owner_uid": os.getuid(),
            "owner_gid": os.getgid(),
            "entry_count": len(entries),
            "entries": entries,
        }
        write_exclusive(staging / "cache-inventory.json", canonical_json(inventory) + b"\n", mode=0o400)
        assembly = {
            "schema_version": SCHEMA,
            "state": "assembled-awaiting-independent-cache-verifier",
            "network": "none",
            "source_execution": False,
            "reference_lock_sha256": sha256_bytes(lock_bytes),
            "cache_root_sha256": cache_digest,
            "registry_package_count": len(packages),
            "archive_evidence": archive_evidence,
            "expansion": expansion,
        }
        write_exclusive(staging / "assembly-evidence.json", canonical_json(assembly) + b"\n", mode=0o400)
        staging.rename(destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify(cache_root: Path, tool_layer: Path) -> tuple[Path, Path]:
    cache_root = cache_root.resolve(strict=True)
    generated = DEFAULT_GENERATED.resolve(strict=True)
    if generated not in cache_root.parents or not cache_root.name.startswith("builder-cargo-cache-sha256-"):
        raise CacheError("cache root must be a content-addressed child of generated/")
    cargo_home = cache_root / "cargo-home"
    inventory_path = cache_root / "cache-inventory.json"
    package_path = cache_root / "package-inventory.json"
    lock_path = cache_root / "reference-Cargo.lock"
    inventory = json.loads(read_regular(inventory_path, 32 * 1024 * 1024, "cache inventory"))
    package_inventory = json.loads(read_regular(package_path, 4 * 1024 * 1024, "package inventory"))
    lock_bytes, packages, _ = load_lock(lock_path)
    observed, digest = inventory_tree(cargo_home)
    expected_digest = cache_root.name.removeprefix("builder-cargo-cache-sha256-")
    if digest != expected_digest or inventory.get("cache_root_sha256") != digest:
        raise CacheError("cache content address does not match bytes and modes")
    if inventory.get("entries") != observed or inventory.get("entry_count") != len(observed):
        raise CacheError("cache inventory differs from filesystem")
    for row in observed:
        expected_mode = "0555" if row["type"] == "directory" else "0444"
        if row["mode"] != expected_mode or row["uid"] != os.getuid() or row["gid"] != os.getgid():
            raise CacheError(f"cache owner/mode mismatch: {row['path']}")
        if row["type"] == "regular" and row["nlink"] != 1:
            raise CacheError(f"cache hardlink detected: {row['path']}")
    package_rows = package_inventory.get("packages")
    if not isinstance(package_rows, list) or len(package_rows) != len(packages):
        raise CacheError("package inventory count differs from lock")
    locked_keys = {(item["name"], item["version"], item["checksum"]) for item in packages}
    inventory_keys = {(item.get("name"), item.get("version"), item.get("checksum")) for item in package_rows}
    if locked_keys != inventory_keys:
        raise CacheError("package inventory differs from reference lock")
    proc_macros = package_inventory.get("proc_macros")
    expected_proc_macros = [
        {"name": item["name"], "version": item["version"], "checksum": item["checksum"]}
        for item in package_rows
        if item.get("proc_macro") is True
    ]
    if proc_macros != expected_proc_macros or not proc_macros:
        raise CacheError("proc-macro inventory is incomplete")
    native_rows = [item for item in package_rows if item.get("native_sources")]
    if (
        package_inventory.get("host_native_source_count") != 0
        or package_inventory.get("approved_wasm_target_support_artifacts") != APPROVED_WIT_BINDGEN_WASM_SUPPORT
        or len(native_rows) != 1
        or native_rows[0].get("name") != "wit-bindgen"
        or native_rows[0].get("version") != "0.60.0"
        or native_rows[0].get("native_sources") != APPROVED_WIT_BINDGEN_WASM_SUPPORT
        or {
            (item.get("name"), item.get("version"), item.get("checksum"), item.get("links"))
            for item in package_rows
            if item.get("links") is not None
        }
        != APPROVED_NON_NATIVE_LINKS
    ):
        raise CacheError("native/binary inventory differs from approved target-Wasm support files")
    tool_layer = tool_layer.resolve(strict=True)
    if tool_layer.name != f"sha256-{EXPECTED_TOOL_LAYER_SHA256}":
        raise CacheError("tool layer is not the accepted content-addressed layer")
    tools = {}
    for name, relative in (
        ("cargo", "toolchain/bin/cargo"),
        ("rustc", "toolchain/bin/rustc"),
        ("wasm-tools", "bin/wasm-tools"),
    ):
        path = tool_layer / relative
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1 or not metadata.st_mode & 0o111:
            raise CacheError(f"accepted tool is unsafe: {name}")
        tools[name] = {"relative_path": relative, "sha256": sha256_bytes(path.read_bytes())}
    acceptance = {
        "schema_version": ACCEPTANCE_SCHEMA,
        "state": "independently-accepted",
        "cargo_home": str(cargo_home),
        "network": "none",
        "read_only": True,
        "allowed_proc_macros": proc_macros,
        "accepted_by": "offline-cache-verifier",
    }
    evidence = {
        "schema_version": SCHEMA,
        "state": "independently-accepted-for-local-macos-sandbox-runner",
        "accepted_by": "offline-cache-verifier",
        "authority": {"build": "none", "verify_cache": "offline-cache-verifier", "install": "none", "publish": "none"},
        "scope": "local-product-prototype-not-stage0-production-isolation",
        "network": "none",
        "source_execution": False,
        "cache_root": str(cache_root),
        "cargo_home": str(cargo_home),
        "cache_root_sha256": digest,
        "cache_inventory_sha256": sha256_bytes(read_regular(inventory_path, 32 * 1024 * 1024, "cache inventory")),
        "package_inventory_sha256": sha256_bytes(read_regular(package_path, 4 * 1024 * 1024, "package inventory")),
        "reference_lock_sha256": sha256_bytes(lock_bytes),
        "registry_package_count": len(packages),
        "proc_macro_count": len(proc_macros),
        "proc_macros": proc_macros,
        "build_scripts": package_inventory.get("build_scripts"),
        "non_native_links": package_inventory.get("non_native_links"),
        "host_native_source_count": 0,
        "approved_wasm_target_support_artifacts": APPROVED_WIT_BINDGEN_WASM_SUPPORT,
        "tool_layer": {"path": str(tool_layer), "identity_sha256": EXPECTED_TOOL_LAYER_SHA256, "tools": tools},
        "checks": [
            {"id": "locked-crates-io-only", "outcome": "pass"},
            {"id": "archive-checksums-bound", "outcome": "pass"},
            {"id": "safe-regular-file-extraction", "outcome": "pass"},
            {"id": "no-symlink-hardlink-special", "outcome": "pass"},
            {"id": "owner-and-read-only-modes", "outcome": "pass"},
            {"id": "proc-macro-inventory-exact", "outcome": "pass"},
            {"id": "exact-non-native-links-and-no-host-native-build", "outcome": "pass"},
            {"id": "exact-wit-bindgen-wasm-target-support-files", "outcome": "pass"},
            {"id": "accepted-tool-layer-bound", "outcome": "pass"},
        ],
    }
    acceptance_path = cache_root / "acceptance.json"
    evidence_path = cache_root / "independent-evidence.json"
    for path, value in ((acceptance_path, acceptance), (evidence_path, evidence)):
        expected = canonical_json(value) + b"\n"
        if path.exists():
            if read_regular(path, 32 * 1024 * 1024, path.name) != expected:
                raise CacheError(f"existing {path.name} differs from verifier result")
        else:
            atomic_root_json(path, value)
    return acceptance_path, evidence_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assemble_parser = commands.add_parser("assemble")
    assemble_parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    assemble_parser.add_argument("--archive-cache", action="append", required=True, type=Path)
    assemble_parser.add_argument("--generated-root", type=Path, default=DEFAULT_GENERATED)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("cache_root", type=Path)
    verify_parser.add_argument("--tool-layer", type=Path, default=DEFAULT_TOOL_LAYER)
    arguments = parser.parse_args()
    try:
        if arguments.command == "assemble":
            root = assemble(arguments.lock, arguments.archive_cache, arguments.generated_root)
            print(json.dumps({"state": "assembled-awaiting-independent-cache-verifier", "cache_root": str(root)}, sort_keys=True))
        else:
            acceptance, evidence = verify(arguments.cache_root, arguments.tool_layer)
            print(json.dumps({"state": "independently-accepted", "acceptance": str(acceptance), "evidence": str(evidence)}, sort_keys=True))
    except (CacheError, OSError, ValueError, tarfile.TarError) as error:
        print(f"CACHE_FAILED {str(error)[:4096]}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
