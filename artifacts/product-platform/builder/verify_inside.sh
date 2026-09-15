#!/bin/sh
set -eu

umask 077
test "$(id -u)" = "65532"
test "$(id -g)" = "65532"
test -r /candidate/manifest.json
test -r /candidate/component.wasm

mkdir -p /tmp/home /tmp/cargo-home /exec/verify-target

wasm-tools validate /candidate/component.wasm
echo "WASM_VALIDATE_PASS"

cargo run \
  --quiet \
  --manifest-path /workspace/builder/verifier-helper/Cargo.toml \
  --target-dir /exec/verify-target \
  --locked \
  --offline \
  -- /candidate/manifest.json

echo "WIT_BEGIN"
wasm-tools component wit /candidate/component.wasm
echo "WIT_END"

echo "METADATA_BEGIN"
wasm-tools metadata show /candidate/component.wasm
echo "METADATA_END"
