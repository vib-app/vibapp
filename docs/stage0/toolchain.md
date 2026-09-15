# Stage 0 Reproducible Toolchain and Dependency Policy

Status: **Gate 5 contract candidate — normative for `experimental-v0`**

Machine-readable pins: [`../../config/contract-toolchain.toml`](../../config/contract-toolchain.toml)

Rustup selection: [`../../rust-toolchain.toml`](../../rust-toolchain.toml)
Observed: 2026-08-11 (Asia/Shanghai)

This document prevents ambient developer state from defining the experimental ABI. It authorizes no source, generated-code execution, cloud builder, live model or credential use. The first Cargo manifest/lock graph remains prohibited until all pre-code gates pass together.

## 1. Frozen Stage 0 choices

| Layer | Exact choice | Provenance / reason |
| --- | --- | --- |
| Rust | `1.93.0`, minimal profile, `clippy`, `rustfmt` | Dated official Rust dist manifest `2026-01-22`, SHA-256 `beb6ba4e…b1f8`; local rustc commit `254b596…`; Cargo commit `083ac513…`. |
| Guest target | `wasm32-wasip2` | Rust 1.93.0 dist manifest marks it available; rust-std xz SHA-256 `0ef01bb5…f8c`. WASI P3 is excluded. |
| Host runtime | `wasmtime = =36.0.13` | Latest non-yanked patch observed on the active multiple-of-12 LTS line; exact crates.io checksum in machine config. |
| WASI host crate | `wasmtime-wasi = =36.0.13`, default features off | Same LTS line; `preview1` and `p3` explicitly excluded. Linking any WASI interface still requires Gate 2 import policy and Gate 3 grants. |
| Guest bindings | `wit-bindgen = =0.60.0` | Exact checksum; minimal synchronous Stage 0 features. Async/P3-related features excluded. |
| Inspect/canonical tooling | `wasm-tools 1.256.0` | Exact checksum; used to validate, print WIT and show metadata. It never defines the artifact digest by rewriting signed bytes. |
| Binding audit CLI | `wit-bindgen-cli 0.60.0` | Same project/version as guest bindings; exact checksum. Macro-generated bindings remain the normative guest path. |
| Dependency policy | `cargo-deny 0.20.2`, `cargo-vet 0.10.2` | Exact checksums; required when a Cargo graph is first authorized. |
| Component driver | `cargo-component` excluded | Rust's `wasm32-wasip2` target + `wit-bindgen` + `wasm-tools` is the sole Stage 0 path. A second driver would add unneeded ambient behavior. |
| Bootstrap image | `docker.io/library/rust@sha256:776861…0d57` | Official `rust:1.93.0-slim-bookworm` OCI index; source revision `37d9b1d…`; platform manifests pinned separately. Tags are documentation only. |

Wasmtime's current release policy makes multiples of 12 LTS releases with a longer support window than normal monthly releases. The crates.io index observed `36.0.13` as the newest non-yanked 36.x patch on 2026-08-11. Normal lines 46/47 and older LTS 24 are not selected. A later patch or LTS requires an explicit config change, changelog/security review and full conformance rerun; SemVer ranges such as `36`, `^36` or `>=36` are forbidden.

## 2. Exact future Cargo feature contract

When source is authorized, the first host manifest MUST express the equivalent of:

```toml
wasmtime = { version = "=36.0.13", default-features = false, features = ["async", "component-model", "cranelift", "runtime", "std"] }
wasmtime-wasi = { version = "=36.0.13", default-features = false }
```

`all-arch`, automatic `cache`, `coredump`, debug builtins, Wasm GC, pooling allocator, profiling, threads, WAT and Winch are excluded from Stage 0. Adding a feature changes the engine policy and requires Gate 3/5 review. Local private AOT caches are also excluded from the first tranche; Registry AOT/native input is always forbidden.

The first guest manifest MUST express the equivalent of:

```toml
wit-bindgen = { version = "=0.60.0", default-features = false, features = ["bitflags", "macro-string", "macros", "realloc", "std"] }
```

Guest async, inter-task wakeup, futures streams and WASI P3 are excluded. Host bindings may be async so outer cancellation can interrupt blocked host operations; that does not authorize guest threads/background loops.

These snippets are contract examples, not source authorization and not an existing dependency graph.

## 3. Builder image identity and minimum invocation profile

The OCI index and allowed platform manifests are immutable:

| Platform | Manifest digest | Base digest |
| --- | --- | --- |
| `linux/amd64` | `sha256:5c5066e3f3bdd22a5cec7ba22ef0ee6e0bf6eaf63b65b63c9bf25f6f69a5e26a` | `debian:bookworm-slim@sha256:6458e6ce…47f4` |
| `linux/arm64` | `sha256:9a94ba32757132618f161731e0b93b8bbd5a34c10f256a1d883ca50ff4f422f2` | `debian:bookworm-slim@sha256:1c5d4fd0…9f96` |

The multi-platform index digest is not a substitute for recording the selected platform manifest in provenance. Other image architectures are not Stage 0 build platforms.

The official image defaults are not the security profile. Any later deterministic builder fixture MUST override them with the `ISO-BUILDER-01` requirements in [threat-model.md](threat-model.md): UID/GID `65532`, read-only root, all Linux capabilities dropped, no-new-privileges, no host namespaces/devices/sockets/home/metadata/credentials, bounded tmpfs/workspace/output, and exact CPU/RAM/PID/disk/time limits.

The immutable build identity is the tuple:

```text
platform image manifest digest
+ Rust dated dist manifest digest and wasm32-wasip2 rust-std digest
+ tool crate name/version/checksum set
+ Cargo.lock digest
+ config/contract-toolchain.toml digest
+ build command/environment digest
```

No final derived builder image exists in this pre-code tranche. Before any build executes, a future authorized builder definition must either produce a derived image digest containing the verified tools/target or mount a content-addressed, read-only tool layer with the same tuple. Runtime `rustup`/`cargo install` network fetch during an untrusted build is forbidden.

### 3.1 Setup-only bootstrap image retrieval

Bootstrap-image retrieval is a distinct, credential-free setup phase. It is not agent, build or test egress and grants no source-execution authority. For the accepted public `library/rust` image, only HTTPS `GET`/`HEAD` to `registry-1.docker.io`, anonymous token acquisition from `auth.docker.io`, and blob redirects to `production.cloudfront.docker.com` are allowed. Docker's current official firewall allowlist names those pull/authentication endpoints, its registry reference locates the v2 API at `registry-1.docker.io`, and its pull-limit documentation shows the anonymous token flow. Provenance was re-observed from those primary sources on 2026-08-11.

The setup client MUST use no login, credential helper, user Docker configuration or persisted token. The bearer token may exist only ephemerally in memory with pull-only scope for `library/rust`. Every redirect host is rechecked; any other host, method, repository, authentication request or redirect fails closed and requires a new Gate 5 change. The tag is non-authoritative. Retrieval selects the exact `linux/arm64` platform reference `docker.io/library/rust@sha256:9a94ba32757132618f161731e0b93b8bbd5a34c10f256a1d883ca50ff4f422f2`; the accepted index digest `sha256:776861…0d57` and its index-to-platform linkage are verified and recorded separately.

This policy change has two independent gates to avoid circular authorization. First, an independent Gate 5 policy review may release only the exact setup retrieval above. After retrieval, a second independent review must verify the selected manifest/config/layers, derived content-addressed tool image, SBOM/provenance and `ISO-BUILDER-01` isolation canaries. Repository `cargo metadata`, policy, check, test or any source execution stays prohibited until the second review passes. The builder, test and agent phases always use `network=none`.

### 3.2 Setup-only RustSec advisory snapshot retrieval

Full `cargo deny --frozen check` includes the advisories check and, in offline/frozen mode, requires a previously populated RustSec advisory database. That database is a separate credential-free setup input; it is not a Cargo dependency, repository source, build/test egress or permission to omit advisories. The accepted upstream is only the official public `RustSec/advisory-db` repository.

Retrieval was a two-request, fail-closed setup flow and is now complete. No further RustSec network request is authorized. The exact anonymous `GET` to `https://github.com/RustSec/advisory-db/commits/main.atom` returned a complete 200 Atom body and exchange privately retained under `tmp/rustsec-fetch-mbevh64_`; it was not retried. The machine contract binds its failure, exchange and body SHA-256 values, body size, status, server Date, local receipt time and private-root mode. GitHub's response canonically identifies the feed and commit links with lowercase `rustsec`; the executed request/final URL retains its exact uppercase path while feed ID, self URL, entry links and the completed codeload path use the exact lowercase identities retained in evidence.

This policy deliberately does **not** claim that Atom document order proves the branch HEAD. It validates every retained entry, requires each lowercase 40-hex commit to be unique and bound by exactly one GitHub commit tag ID and exactly one exact lowercase `https://github.com/rustsec/advisory-db/commit/<commit>` alternate link, selects the greatest `updated` timestamp (lowercase SHA ascending as a deterministic tie break), and requires `feed.updated` to equal that timestamp. The feed must have exactly one exact lowercase self relation. The selected entry must remain strictly inside cargo-deny's `P90D` window relative to the retained single HTTPS `Date` header, and the bound local receipt time must agree with that Date within ten minutes. This validates the retained selection `d0861df1eab469d3c58d6b836ce48b5766e5f217` at `2026-08-11T11:04:49Z` only as a recent official `main`-feed commit, not as the current branch tip.

GitHub's retained response contains three `Set-Cookie` header occurrences. Request cookies, a cookie jar, cookie application, persistence into request state and replay are forbidden. Received Set-Cookie values are retained only inside the private response evidence because the all-header rule precedes validation; they are untrusted metadata and cannot influence the URL, selection, archive request headers or provenance identity. The client constructs every request header set explicitly and never emits `Cookie`.

After fresh independent Gate 5 review validated the retained hashes, canonical identities, unconsumed-cookie semantics, every entry, selection, clocks and freshness, the sole remaining anonymous `GET` retrieved exactly `https://codeload.github.com/rustsec/advisory-db/tar.gz/d0861df1eab469d3c58d6b836ce48b5766e5f217`. It returned complete `200` evidence, `456286` bytes and archive SHA-256 `2d9c8ef529a3d0942730fcf3c8549634b937bb2e0b8cebf3aa3ceedb1c8a9315`, with no redirect, response cookie or retry. The archive exchange, safe tree, manifest and provenance hashes are now fixed in the machine contract. Resolver and archive request counts are both exhausted; `GET`, `HEAD`, probes, retries, Git clone/fetch/smart protocol, credentials, user Git configuration and every other RustSec network action are now forbidden. The consumed completion program reads the current machine policy before creating a temporary packet or calling the network and fails closed unless the exact one-request authority is present; with the completed policy it cannot repeat the request.

The first REST resolver attempt on 2026-08-11 remains an explicitly non-authoritative historical failure: only status `403` was recorded, its headers/body are irrecoverably absent, it selected no commit, issued no archive request, contributes to no snapshot/provenance/freshness fact, and may not be retried or used to justify this replacement flow. The later Atom attempt is distinct: its exact response was retained and independently reviewed before the completed archive GET. Neither request may be retried or reused to justify another network action.

The content-addressed offline snapshot candidate is retained under `generated/rustsec-advisory-db/d0861df1eab469d3c58d6b836ce48b5766e5f217/packet` and may be installed only into the isolated verifier's synthetic `CARGO_HOME/advisory-dbs/advisory-db-3157b0e258782691`. Its `2139` manifest records describe `1231` regular files and `908` child directories; with the root, the tree has `2140` paths, zero links/special/writable paths and manifest SHA-256 `7de6f5314132f8bf168df84cdbfd7d9f130f9033a784d2893f7dcec254e8ef63`. Because the archive intentionally contains no Git metadata while cargo-deny 0.20.2 reads `.git/FETCH_HEAD` metadata to enforce freshness, the snapshot adds exactly one compatibility marker at `.git/FETCH_HEAD`: its content is `<selected-commit>\n` and its modification time is the selected entry's retained `updated` timestamp. This does not assert a Git clone or branch HEAD. The timestamp remains subject to cargo-deny's default `P90D` maximum staleness and must never be refreshed without a newly authorized retrieval. The snapshot subtree is mounted read-only; only its parent advisory-db directory may be an ephemeral writable location for cargo-deny's `db.lock`.

As with bootstrap-image retrieval, this change has two independent gates. The first Gate 5 review and the single released archive request are complete; they authorize no further network. The snapshot remains a candidate until a current-config/snapshot-bound builder provenance set is produced and a second fresh independent review validates the retained response, commit/timestamp, archive hash, safe tree, licenses, marker, non-writable snapshot and a full network-none `cargo deny --frozen check`. Until that second verdict, repository `cargo metadata`, `cargo deny`, `cargo vet`, `cargo check`, `cargo test`, source implementation and artifact generation all remain held.

### 3.3 Setup-only Cargo dependency retrieval

Tranche 2 introduces the first application graph that is not already present in the accepted offline tool layer. Dependency retrieval is a credential-free setup phase, not build/test egress. It has three fail-closed releases:

1. A fresh independent policy review establishes the resolver boundary but releases no request until an exact wrapper/input binding also passes. Exact Cargo 1.93.0 receives manifest files but no repository Rust source and may run only `cargo generate-lockfile`. Cargo is placed alone on a Docker `--internal` network with no public route. The sparse mirror is a separate non-root container attached both to that internal network and to a dedicated egress network; it is the only process that can reach `index.crates.io:443`. Its current local runtime candidate is the content-addressed Linux ARM64/v8 `python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3` image with Python 3.11.15; that runtime is not accepted merely by naming it and must be independently closed with the exact wrapper/image/config/layer/runtime/SBOM/license evidence before the first request. Both Cargo and mirror containers must be unprivileged, drop all capabilities and use no-new-privileges; forwarding, proxy variables, credentials and a route from Cargo through the mirror are forbidden. Docker network/container inspections plus denial canaries must prove Cargo cannot reach the index host, `static.crates.io`, an arbitrary public IP or a credential provider. The mirror permits one complete anonymous `GET` for each unique canonical sparse path, with hard attempt-wide ceilings of 1,024 unique paths and 536,870,912 retained/received body bytes; reaching either ceiling fails before another request or after retaining the bounded prefix. It rejects redirects, credentials, cookies, every non-GET method, non-canonical paths and ambiguous framing, and retains status, all response headers, final URL, body size and SHA-256. It strictly accepts only the current public `config.json` `api`/`dl` pair, then gives Cargo a derived config with no API and an internal deliberately forbidden archive path. The Cargo home is empty and isolated; user Cargo/Rustup state is not mounted.
2. The candidate `Cargo.lock`, every selected index record and the complete exact archive-request plan receive a second fresh independent review. Only that review may release one anonymous, non-retried `GET` per selected non-workspace registry package from `https://static.crates.io`; no archive request is authorized by the first review. Every body must match both the selected sparse-index checksum and `Cargo.lock` before entering quarantine.
3. A third fresh independent review reconstructs the lock graph, crate archives, safe extracted trees, SBOM, licenses, build-script/proc-macro/native-code inventory and provenance. Only accepted content-addressed read-only cache bytes may then be bound into a new builder identity. Repository metadata/policy/check/test/build and guest generation remain network-none and held until that acceptance.

The mirror starts only inside a supervisor-precreated canonical absolute empty private evidence root. It opens every path component with directory/no-follow primitives and retains directory descriptors so later exclusive/atomic writes cannot be redirected through ancestor replacement. Failed responses retain their bounded body prefix and error metadata. SIGINT, SIGTERM, cancellation and ordinary server exit are failures unless SIGTERM nonblockingly opens a no-follow/CLOEXEC regular root-owned mode-0444 completion handshake that the separate supervisor creates only after exact Cargo exit zero and quarantined-lock capture. FIFO, socket, device, directory, symlink, short or extra-content completion nodes fail without blocking. The mirror derives Cargo's no-API/internal-forbidden-archive projection; the later supervisor/verifier must validate it independently. The mirror then seals files/directories read-only; the supervisor must still reclose them as root-owned, produce a canonical inventory, bind wrapper/input/Cargo/lock/request evidence, and consume the one-shot authority before any result can be accepted.

The machine-readable child policy is `[network_policy.dependency_fetch]` in `config/contract-toolchain.toml`; its narrow mirror implementation is `infra/stage0-builder/dependency_sparse_mirror.py`. No resolver wrapper is accepted yet, so even a PASS on the mirror repair releases zero requests. Standard Cargo registry access, ambient user caches, retries, redirects, alternate registries, Git dependencies, credentials and using a successful resolution as source-execution evidence are forbidden.

### Linux ARM64 / future headless CLI applicability

The pinned OCI index includes an explicit `linux/arm64` manifest so the future deterministic tool layer can be verified natively on 64-bit ARM Linux as well as amd64. This supports a later Raspberry Pi 4/5 64-bit and Ubuntu/headless-Linux spike; it is not evidence that the CLI/daemon/runtime already builds or passes on those systems.

The current `rust-toolchain.toml` intentionally adds only the guest `wasm32-wasip2` target. Native CLI/daemon targets, libc baseline, packaging and UDS behavior are outside Stage 0. Before a CLI tranche, its own gate change must pin and verify `aarch64-unknown-linux-gnu` or a deliberately selected musl target, state the minimum kernel/libc and Pi OS architecture, add amd64/arm64 CI, and run the `ISO-CLI-01` fixtures. Thirty-two-bit Raspberry Pi/ARM is not covered by the present toolchain contract.

## 4. Canonical build and inspection commands

Once source and the isolated tool layer are authorized, the only guest build path is:

```text
cargo +1.93.0 build --release --target wasm32-wasip2 --locked --offline
wasm-tools validate <candidate-component.wasm>
wasm-tools component wit <candidate-component.wasm>
wasm-tools metadata show <candidate-component.wasm>
sha256(<exact candidate bytes>)
```

The verifier, not the repository, chooses the artifact path and runs inspection. `wasm-tools strip`, `metadata add`, `component new`, parse/print round trips and any other rewriting command are forbidden after the canonical bytes are selected; any transformation creates a new artifact that must be hashed and revalidated.

The future build environment is cleared and allowlisted. Required reproducibility values are:

- `CARGO_INCREMENTAL=0`;
- `SOURCE_DATE_EPOCH` derived from the recorded source revision, not current wall time;
- `TZ=UTC`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`;
- remap `/workspace` out of debug/diagnostic paths; release artifacts contain no developer absolute path;
- no `RUSTC_WRAPPER`, `RUSTFLAGS`, target linker, runner or Cargo config from user/home state unless its exact value is committed to the toolchain contract;
- `CARGO_HOME`/`RUSTUP_HOME` point to the verified read-only tool layer; `HOME` is an empty synthetic directory;
- build and verification occur twice in fresh workspaces before a reproducibility claim; the two component SHA-256 values must match.

## 5. Cargo.lock and source policy

The first authorized Cargo graph MUST satisfy all of the following:

1. `Cargo.lock` is generated by Rust/Cargo 1.93.0, committed, reviewed and passed to every fetch/build/test/policy command with `--locked` where supported. A lock change is a source review event.
2. Direct dependency requirements are exact `=x.y.z`; default features are disabled or explicitly justified. Transitive selection is frozen by `Cargo.lock`.
3. Only crates.io is allowed. Alternate registries, git dependencies, path dependencies outside the workspace, `[replace]`, and unreviewed `[patch]` are denied.
4. Dependency fetch is a separate credential-free phase restricted to `index.crates.io`, `static.crates.io` and `static.rust-lang.org`, methods GET/HEAD, with redirects rechecked. The separately scoped bootstrap-image retrieval in section 3.1 does not expand Cargo/dependency hosts. Agent/build/test phases are offline and network-denied.
5. After fetch, `cargo vendor --locked --versioned-dirs generated/vendor` (or an equivalent content-addressed cache) is created as generated output and mounted read-only. Checksums remain tied to `Cargo.lock`/`.cargo-checksum.json`.
6. Every target of kind `custom-build` or `proc-macro`, every package with `links`, and every C/C++/assembly source in the resolved vendor tree is inventoried by exact package/version/checksum and reviewed. Default is deny.
7. Guest packages may not depend on native C/C++/assembly or system libraries in Stage 0. Host exceptions must document why pure Rust is insufficient and remain inside the isolated builder.
8. Repository code, build scripts, proc macros and tests are all untrusted at build time. Passing policy does not relax `ISO-BUILDER-01`.
9. Agent/build-produced test reports and provenance are ignored. The verifier derives dependency, license, feature and output evidence from the clean lock/vendor/artifact itself.

Minimum future policy commands, all from the exact tool layer, are:

`cargo-vet 0.10.2` opens `config.toml` read/write for its exclusive store lock and rewrites all three store files after a successful locked check. The repository source tree MUST remain root-owned and non-writable. The isolated verifier therefore copies the three canonical committed `supply-chain` files into a dedicated ephemeral store outside the source tree, keeps that directory root-owned and non-writable, and makes only those three pre-existing regular files temporarily writable by UID 65532. It records a root-owned byte baseline, passes the explicit `--store-path`, then immediately requires the same three-file set and byte-for-byte equality before changing the ephemeral files back to mode `0444`. A final post-test reclosure is also mandatory. Any extra path, replacement, type/mode/owner drift or content change fails the gate.

```text
cargo metadata --locked --offline --format-version 1
cargo tree --locked --offline --edges normal,build,dev,features
cargo deny --frozen check
cargo vet --store-path /workspace/vet-store --locked --frozen
```

The policy must also compare `cargo tree -e features` with the exact Wasmtime/WIT allowlist. Any forbidden feature in `config/contract-toolchain.toml` fails the candidate.

## 6. License inventory

There is no authorized Cargo application graph yet, so there is no honest transitive application license inventory or `Cargo.lock` to claim. The complete **pre-authorized direct/tool set** is:

| Package/tool | Version | Declared license | Status |
| --- | --- | --- | --- |
| Rust toolchain | 1.93.0 | MIT OR Apache-2.0 with project exceptions | approved toolchain pin |
| `wasmtime` | 36.0.13 | Apache-2.0 WITH LLVM-exception | proposed exact host direct dependency |
| `wasmtime-wasi` | 36.0.13 | Apache-2.0 WITH LLVM-exception | proposed exact host direct dependency |
| `wit-bindgen` | 0.60.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | proposed exact guest direct dependency |
| `wasm-tools` | 1.256.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | verification tool |
| `wit-bindgen-cli` | 0.60.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | binding audit tool |
| `cargo-deny` | 0.20.2 | MIT OR Apache-2.0 | policy tool |
| `cargo-vet` | 0.10.2 | Apache-2.0/MIT | policy tool |

Before the first source commit is accepted, the resulting lock graph and builder OS/tool layer need a generated SBOM/license inventory. Unknown licenses fail. Allowed, review and denied SPDX expressions are machine-readable in `config/contract-toolchain.toml`; a transitive package does not become acceptable merely because its direct parent is approved.

The official Rust slim image is a development carrier, not a redistributable VibApp artifact. Its source revision/base digests are recorded, but its OS package SBOM/license closure was not generated in this non-executing tranche. The first authorized image/tool-layer creation MUST record that SBOM before the image is used to produce a candidate.

## 7. Non-source bootstrap verification

The verification policy is read-only with respect to both the repository and the user's Rustup state. It does not install or run Component tools, compile/execute generated code, use a credential or contact the network. Repository-read-only alone is insufficient: resolving a Rustup proxy under this repository can reconcile the targets in `rust-toolchain.toml` and write the user's tool cache.

### 7.1 Recorded local timeline

| Time (Asia/Shanghai) | Observation | Disposition |
| --- | --- | --- |
| 2026-08-11 12:30 | Initial Gate 3/5 check found `wasm32-wasip2` absent. | True at observation time; now historical. |
| 2026-08-11 13:21:12 | A later repository Rustup-proxy resolution installed the exact pinned `wasm32-wasip2` standard library. | Recorded side effect; no source or package build ran. |
| 2026-08-11 13:46:33 | A diagnostic auto-self-update setting command rewrote Rustup `settings.toml` and left `auto_self_update=enable`; the prior value is unknown. | Recorded side effect; do not guess-restore user state. |
| 2026-08-11 13:52:55 | A guarded local observation used no-auto-install, a fully qualified toolchain and resolved binaries. | Entire Rustup metadata, target metadata and settings metadata were identical before/after. |
| 2026-08-11 14:01:18 | Shell interpolation while creating the repair workstream invoked the Rust and Cargo proxies with empty toolchain selectors. Both returned `invalid toolchain name ''`; neither resolved a toolchain. | Pure filesystem metadata/hash still matched the 13:52 snapshot exactly; no observable Rustup write. |

The current local state remains a documentary observation, not build evidence:

```text
host toolchain: 1.93.0-aarch64-apple-darwin installed
installed targets: aarch64-apple-darwin, wasm32-unknown-unknown, wasm32-wasip2
wasm-tools: absent
wit-bindgen: absent
cargo-deny: absent
cargo-vet: absent
cargo-component: absent
```

The 14:01 empty-selector calls were checked without invoking Rustup, Rustc, Cargo or any network tool. The canonical path/size/mtime metadata hash for every regular file below the Rustup directory stayed `59e12a2c…a907`; the target subtree stayed `59752934…95a9`; `settings.toml` stayed `126|1786427193`. These values equal the snapshots taken before those calls. This proves no observable filesystem write, not that an invalid invocation is acceptable.

### 7.2 Human/machine policy parity

The following fields exactly mirror `[verification]` in `config/contract-toolchain.toml`:

| Field | Normative value |
| --- | --- |
| policy version | `2` |
| mode | `read_only=true`, `offline=true`, no credentials/model/source build |
| process setting | `RUSTUP_AUTO_INSTALL=0` |
| toolchain | `1.93.0-aarch64-apple-darwin` |
| required caller inputs | absolute `VIBAPP_RUSTUP_BIN`, absolute `VIBAPP_RUSTUP_DIR` |
| execution | one ordered fail-fast shell; commands may not be executed independently |
| state guard | capture before and after; equality required |
| snapshot fields | canonical path, type, mode, size, nanosecond mtime and file-content SHA-256 |
| snapshot walk/encoding | recursive `lstat` without following symlinks; UTF-8 JSONL sorted by normalized absolute path; any error fails closed |
| snapshot roots | Rustup settings file, exact 1.93.0 toolchain tree and exact `wasm32-wasip2` target subtree |
| state-change action | fail the read-only claim and record a side effect |
| forbidden classes | plus-toolchain Rust/Cargo proxies, bare repository Rust/Cargo proxies, unqualified toolchains and auto-install-enabled observation |
| upstream recheck | separate explicitly authorized network phase |

The independent verifier supplies the two absolute `VIBAPP_*` inputs; a repository script may not infer them from `PATH`, a home directory or repository configuration. Before the first command, the verifier captures the declared roots using the declared snapshot fields. After the last command, it repeats the snapshot. Any difference fails verification even when every command returned success.

### 7.3 Exact guarded local command sequence

The following lines are identical, in order, to the machine-readable `verification.commands` array. They are a single shell program between the two verifier-owned snapshots:

```sh
python3 -c 'import tomllib; tomllib.load(open("rust-toolchain.toml", "rb")); tomllib.load(open("config/contract-toolchain.toml", "rb"))'
test -n "$VIBAPP_RUSTUP_BIN" && test -x "$VIBAPP_RUSTUP_BIN"
test -n "$VIBAPP_RUSTUP_DIR" && test -d "$VIBAPP_RUSTUP_DIR"
case "$VIBAPP_RUSTUP_BIN" in /*) ;; *) exit 64 ;; esac
case "$VIBAPP_RUSTUP_DIR" in /*) ;; *) exit 64 ;; esac
RUSTUP_AUTO_INSTALL=0 "$VIBAPP_RUSTUP_BIN" toolchain list
RUSTUP_AUTO_INSTALL=0 "$VIBAPP_RUSTUP_BIN" target list --installed --toolchain 1.93.0-aarch64-apple-darwin
VIBAPP_RUSTC_PATH="$(RUSTUP_AUTO_INSTALL=0 "$VIBAPP_RUSTUP_BIN" which --toolchain 1.93.0-aarch64-apple-darwin rustc)"
VIBAPP_CARGO_PATH="$(RUSTUP_AUTO_INSTALL=0 "$VIBAPP_RUSTUP_BIN" which --toolchain 1.93.0-aarch64-apple-darwin cargo)"
test "$VIBAPP_RUSTC_PATH" = "$VIBAPP_RUSTUP_DIR/toolchains/1.93.0-aarch64-apple-darwin/bin/rustc"
test "$VIBAPP_CARGO_PATH" = "$VIBAPP_RUSTUP_DIR/toolchains/1.93.0-aarch64-apple-darwin/bin/cargo"
RUSTUP_AUTO_INSTALL=0 "$VIBAPP_RUSTC_PATH" --version --verbose
RUSTUP_AUTO_INSTALL=0 "$VIBAPP_CARGO_PATH" --version --verbose
```

Every Rustup command has process-scoped auto-install disabled; every toolchain-specific query names the exact host toolchain. The path equality checks prove the final version commands target installed toolchain binaries rather than Rustup proxies. Plus-selector proxy forms and bare Rust/Cargo names from the repository are prohibited as read-only verification.

The guarded local sequence checks that both TOML files parse, Rust/Cargo report the pinned version and commits, and installed targets are reported rather than reconciled. It does not prove the unavailable Component/policy tools, target execution, a Cargo graph or a builder.

### 7.4 Separate upstream provenance

The dated Rust distribution URLs/checksums, crates.io checksums/licenses/repositories and OCI digests above were verified in the original Gate 5 workstream. Re-fetching them requires a separate explicitly authorized network phase. It is not part of the guarded local command array and may not be smuggled into a local/offline check. The user authorized a bounded credential-free setup/fetch phase on 2026-08-11; section 3.1 narrows its OCI portion to exact anonymous, digest-pinned retrieval, and section 3.2 narrows the RustSec portion to one anonymous branch-resolution response followed by one commit-addressed archive. Each still requires independent policy acceptance before its first local fetch.

The exact repair evidence and hashes are recorded in the assigned Chief-of-Staff inbox report. Until the first independent policy verdict passes, pulling the image remains held. Running it against repository source, installing tools during an untrusted build and executing any repository Cargo command remain a separate **fail-closed execution hold** until the derived image and isolation fixture receive their own independent acceptance; ambient versions are never a fallback.

## 8. Change control and future skill routing

Any change to Rust, target, runtime/tool version, Wasmtime feature, builder digest, dependency source class, license rule or fetch host requires:

1. update this document and the machine config together;
2. record current primary-source provenance/checksum and reason;
3. rebuild the tool layer from an empty cache;
4. regenerate Cargo.lock/SBOM/license/build-script/proc-macro/native inventory;
5. rerun all ABI, adversarial, launcher/service/preview and alarm fixtures;
6. compare two clean artifact digests and record exceptions;
7. obtain independent Gate 5 review before merging.

For a fetch-host change needed to retrieve an already accepted immutable bootstrap image, step 7 first reviews the synchronized retrieval policy and may release retrieval only. Steps 3–6 are then satisfied by rebuilding and reviewing the derived Linux tool image from an empty setup context. The RustSec snapshot flow uses the analogous two-gate process in section 3.2: policy review releases only resolution/retrieval, then snapshot review validates the content-addressed offline input and full advisories check. No repository execution is released between either pair of gates.

The future `.agents/skills/vibapp` MUST read `rust-toolchain.toml`, `config/contract-toolchain.toml`, this policy, the threat-control IDs and the package compatibility RFC. It MUST reject prompts that request ambient/latest tool versions, git dependencies, networked build/test, unpinned images, forbidden Wasmtime features or bypassing quarantine/verifier steps.

## 9. Gate 5 acceptance statement

This tranche provides exact reversible pins, immutable provenance, direct license inventory, dependency/lock policy and a reproducible read-only verification procedure without introducing source. It does **not** claim the future Cargo graph or derived tool image already exists. No code execution is allowed until an independent gate review confirms this contract and the first authorized tool layer/lock inventory fulfills the explicit pre-execution holds above.
