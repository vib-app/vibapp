# Diagnostic compiler-path fixture

This directory is not a VibApp product CodeAgent and is not used by Desktop. It is
retained only to diagnose the offline Rust/WASI compiler and runtime path. The prior
`papercolors` evidence is historical diagnostic evidence, not a generated app or
proof of the product development flow.

Qwen may propose three bounded presentation-copy fields for this fixture, but it
never authors Rust and its output is explicitly marked `codeagent_output=false`.
The trusted diagnostic scaffold is compiled in the existing pinned, no-network
tool image and checked with `wasm-tools`. The resulting record is
`publication_state=diagnostic-only`, `launch_eligible=false`,
`install_eligible=false`, and is neither packaged nor surfaced by Desktop.

The actual product path is:

```text
Qwen NeedSpec preprocessing -> Registry -> explicit no-match submission
-> durable CloudAgent task -> real CodeAgent ProviderRunner -> returned Rust source
-> Builder/verifier -> private Registry -> Desktop launch
```

This repository currently stops at the durable CloudAgent queue because the real
ProviderRunner/backend/trust material is not provisioned. No Qwen fallback is
allowed for that missing CodeAgent stage.

The accepted r62 subject currently in this repository is a frozen Stage 0
dependency resolver. It has no app-source request surface, and its exact FD65
formal-assignment body was not available to this integration. Its one-shot was
therefore not consumed. This adapter does not claim to replace or satisfy r62.

Run the corrected synthetic queue handoff evidence flow (Qwen preprocesses NeedSpec;
no CodeAgent/provider request is made):

```sh
python3 artifacts/local-codeagent/e2e_synthetic.py
```

Run bounded offline unit tests:

```sh
python3 -m unittest discover -s artifacts/local-codeagent/tests -v
```
