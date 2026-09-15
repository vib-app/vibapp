# Linux and Windows native evidence definition

Status: definition only. No Linux or Windows native job was run while authoring these
files, neither platform has a package candidate, and neither platform is supported.

The deterministic assembler definition has been exercised with synthetic ordinary
files on macOS. That proves only archive construction and rejection behavior; it is
recorded as `package-definition-fixture-reproduced`, never as native compilation,
native packaging, signing, installability, runtime health, or platform support.

The Launcher also has a platform-explicit policy boundary in
`src-tauri/src/native_platform.rs`. It recognizes only the accepted macOS app,
Linux archive, and Windows archive layouts; once an executable matches a packaged
layout, a missing helper fails instead of falling back to repository source. Python
and CodeAgent helper discovery use fixed OS policy paths rather than ambient PATH.
Windows currently has no executable Python policy, names but does not implement the
owner-only named-pipe contract, and refuses BYOM secret storage until an owner-only
ACL implementation exists. The Rust tests for those branches run as policy tests on
macOS and are not native Linux/Windows evidence.

This directory separates six facts that must never be collapsed into one green
badge:

1. the matrix definition is structurally valid;
2. source compiles and tests on the named native OS;
3. two fresh native builds produce identical executable bytes;
4. an unverified platform archive is assembled twice with identical bytes;
5. that exact archive passes native signing/install/runtime smoke;
6. a separate reviewer accepts the exact evidence.

`native-platform-matrix.json` is the machine-readable claim ceiling. Its validator
rejects duplicate keys, lockfile drift, unknown labels, release self-promotion,
unreferenced blockers, and any attempt to mark package/smoke/acceptance runnable while
known blockers remain. `preflight` returns exit 78 for a blocked phase before any
package command runs.

## Deterministic package input and output

`assemble_native_package.py` accepts one strict JSON object. Unknown or duplicate keys
are rejected. `source_root` and `output_directory` must be explicit, existing,
canonical absolute directories. Every `source_path` is relative to that root and every
input row contains exactly:

```text
role, source_path, archive_path, executable, size_bytes, sha256
```

The input object additionally contains the exact fields:

```text
schema_version = vibapp.native-package-input.experimental-v1
target = linux-x86_64 | windows-x86_64
package_version = bounded SemVer-like value
source_revision = exact lowercase 40-hex Git commit
source_date_epoch = even UTC second in the ZIP/tar.gz shared range
source_root = canonical absolute directory
files = 3..512 explicit rows, <=64 MiB each and <=128 MiB total
```

There is exactly one executable `launcher`, `runtime`, and `service-runtime`. All
remaining rows are non-executable `resource` files. The exact archive layouts are:

```text
linux-x86_64 (.tar.gz, USTAR + deterministic stored-DEFLATE gzip)
  vibapp/PACKAGE-MANIFEST.json
  vibapp/bin/vibapp-launcher
  vibapp/bin/vibapp-runtime
  vibapp/libexec/vibapp-service-runtime
  vibapp/resources/**

windows-x86_64 (.zip, ZIP_STORED)
  VibApp/PACKAGE-MANIFEST.json
  VibApp/vibapp-launcher.exe
  VibApp/vibapp-runtime.exe
  VibApp/libexec/vibapp-service-runtime.exe
  VibApp/resources/**
```

Only sorted ordinary files are written. Symlinks, exposed Windows reparse points,
hard links, path escape, duplicate paths, digest/size drift, mutation while reading,
invalid roles/modes, oversized inputs, and existing output paths fail closed. Archive
UID/GID/names/modes/timestamps and gzip/ZIP headers are fixed. The assembler emits the
archive plus canonical `*.manifest.json` and `*.receipt.json` sidecars. The receipt
binds both SHA-256 digests and labels its result
`unverified-package-definition-output`, `native_execution=not-attested`,
`signing=not-applied`, `release_eligible=false`, and `support_claim=not-verified`.
One receipt is only `definition-output-only`; it explicitly states that
reproducibility is not established until a second output is byte-compared.

Exact assembly and byte comparison, after a native package gate is eventually
unblocked, is:

```sh
python3 artifacts/desktop/ci/validate_native_matrix.py preflight --target linux-x86_64 --phase package
python3 artifacts/desktop/ci/assemble_native_package.py --input /absolute/linux-input.json --output-directory /absolute/package-a
python3 artifacts/desktop/ci/assemble_native_package.py --input /absolute/linux-input.json --output-directory /absolute/package-b
(cd /absolute/package-a && shasum -a 256 *) > /absolute/a.sha256
(cd /absolute/package-b && shasum -a 256 *) > /absolute/b.sha256
diff -u /absolute/a.sha256 /absolute/b.sha256
```

The Windows workflow performs the same operation with `py -3.11` and `Get-FileHash`.
At present package preflight exits 78 on each matching native host, so the assembler
commands are deliberately unreachable in CI until the remaining platform blockers
are repaired and the matrix is reviewed.

The workflow is a template, not an active root GitHub Actions workflow. Stage 0's
accepted source boundary explicitly excludes production GUI and multi-platform
claims, and `artifacts/` is an ignored product-prototype area. Installing the workflow
at repository root requires a separately authorized product/repository decision; the
act of copying it is not evidence. It deliberately requires pre-provisioned native
self-hosted runners with explicit Rust/Cargo/Cargo-home paths. It never runs Rustup,
never fetches dependencies during build, uses `--locked --offline --jobs 1`, and does
not soften failures.

The Linux and Windows compile jobs are allowed to emit at most
`native-binaries-reproduced`. A successful compile does not unlock packaging. The two
package probe jobs are expected to fail while the matrix blockers are present; do not
mark them optional, continue-on-error, or translate exit 78 into success.

Local definition validation (safe on macOS) is:

```sh
python3 artifacts/desktop/ci/validate_native_matrix.py validate
python3 -m unittest \
  artifacts.desktop.ci.test_validate_native_matrix \
  artifacts.desktop.ci.test_assemble_native_package -v
```

Native preflight examples are:

```sh
python3 artifacts/desktop/ci/validate_native_matrix.py preflight --target linux-x86_64 --phase compile
py -3.11 artifacts/desktop/ci/validate_native_matrix.py preflight --target windows-x86_64 --phase package
```

The first command succeeds only on Linux and still makes no build claim. The second
command returns exit 78 on Windows until every Windows packaging blocker is closed.
Running either from macOS fails host matching and creates no native evidence.
