# Client release and website download handoff — 2026-09-15

## Scope

- Product source repository: https://github.com/vib-app/vibapp
- Shared GUI: web-only Download link and bilingual install/update fallback, both
  pointing to the product repository's Releases page, not the app package ZIPs.
- Current release target: Apple Silicon macOS preview. No Windows/Intel/Linux
  support claim, no Apple Developer ID/notarization claim.
- Product CI uses pinned Rust 1.98.0, Node 22.23.2, Python standalone 3.13.15 and
  RoomHash headless revision `03345ed558c1f3e38a5cdf0ab95a20467aa1d333`.
  Historical Stage 0 contracts and toolchain pins are unchanged.

## Source publication boundary

The initial public commit is `93c70e8e3b627599c7535a0b45619159205e6ede`.
It is a source-only snapshot, not a publication of the local repository history.
`scripts/export-public-source.mjs` includes required ignored build inputs, rejects
links/oversized files and credential-like values, and excludes private task data,
provider settings, caches, bytecode and operational memory. The import contained
533 files, approximately 10.9 MB.

The existing workspace had substantial staged and unstaged changes. Its HEAD,
index and history were preserved. The clean public Git checkout used for pushes
is `generated/public-source-release-20260915-b`; it has the requested SSH origin.
Do not force-push the original workspace's unrelated historical branch over it.
Subsequent product edits in this task were applied to both source locations.

## Checks completed locally

- 61 shared UI/build-input/release-policy Node tests.
- 16 native resource-path/platform tests with the installed Rust 1.98.0 toolchain.
- 4 public downloader and 12 local app-store tests using the bundled Python 3.13.
- Python SSL, SQLite, TOML, ctypes and multiprocessing imports after relocation
  with a clean environment; packaged daemon import also passed.
- Full independently staged `.app` packaging, deep/strict signature validation,
  pinned descriptor-inspector preflight, launcher startup/help, and real bundled
  RoomHash host start/status/shutdown.
- A local DMG was built, verified, mounted read-only, and its client signature,
  Python and launcher entrypoint checked again; the test mount was detached.
- Website production build and three hosted-shell/read-only backend tests passed.

These are client packaging/readiness checks, not acceptance of every Store app,
not full new-app generation testing, and not a clean bill of health for all older
project tests. The code-agent/build services still need separate configuration.

## Failure lessons fixed in product code

1. Fresh CI inspector bytes differ from a developer's signed binary. Stamp the
   exact newly built and signed trusted inspector hash before desktop compilation;
   retain runtime hash enforcement. Test the actual multiline source format.
2. Packaged clients need their own Python, not a Homebrew dependency. Prefer the
   contained interpreter and fail if the bundled interpreter is invalid.
3. Resolve macOS temporary-directory aliases before checking dependency symlinks.
4. A dylib's first `otool -L` entry may be its own install ID, not an external
   dependency. Still reject real non-relocatable external dependencies.
5. Transport smoke tests must create their own allowed seed root, never depend on
   local Builder demo output. Child failure now surfaces immediately.
6. Publishing is a separate write-permission job. Validate transferred checksums,
   source commit/platform and preview tag; fail on existing tags or lookup errors.

## Publication status

- Release workflow **completed successfully**: https://github.com/vib-app/vibapp/actions/runs/34988955070
- Build source: `fe15cce42f32b075949ded3d572ee812bc720a9a`.
- Published tag: [client-v0.1.0-preview.1](https://github.com/vib-app/vibapp/releases/tag/client-v0.1.0-preview.1), public pre-release, 2026-09-15 15:45:52 UTC.
- Uploaded DMG: `VibApp-macOS-Apple-Silicon.dmg`, 169,937,991 bytes,
  SHA-256 `0afb857b0b953cebbd1c3ca4cacdf048d635ea466e62bfcba311d46035892fd6`.
  `SHA256SUMS` and `release-manifest.json` are also uploaded. Anonymous public
  download HEAD followed redirects to HTTP 200 with the exact expected length.
- The downloaded CI artifact passed SHA-256 checks, DMG verification, read-only
  mount, deep/strict client signature verification, launcher entrypoint, bundled
  Python HTTPS to GitHub (HTTP 200), real RoomHash startup/shutdown and the bundled
  inspector's pinned-byte preflight. The test mount was detached.
- CI inspector SHA-256: `3b291c35b91a6232467427b2f944e0bbc50dd62d6dbc72ce306d0a85d9f3dac3`.
- Website source: `5e05aecb6338eda89f9f5a19b02640d7ecfce897`.
- Sites version 13 saved: `appgprj_6aa8ed4613e48191b21de525dc2795b2~appgver_ec6487fd073c8191bd5930b8ca86c7dd`.
- Sites deployment `appgdep_6aa96863c2208191a231949a0539fcb2` **succeeded** at
  2026-09-15 15:47:16 UTC, after the public release assets existed.
- https://www.vibapp.ai and the Sites alias both serve the new Download anchor.
  The English/Chinese client download and install/update fallback are in the
  deployed shared GUI. Existing public audience was preserved.
- No automated website browser/click/screenshot test was run in this turn;
  verification used builds, contract tests and HTTP responses, per Sites policy.
