#!/bin/sh
set -eu

umask 077
test "$(id -u)" = "65532"
test "$(id -g)" = "65532"
test -r /contract/contract.wit
test -r /input/Cargo.toml
test -r /input/Cargo.lock
test -r /input/lib.rs
test -d /output

mkdir -p /tmp/home /tmp/cargo-home /tmp/project/src
cp /input/Cargo.toml /tmp/project/Cargo.toml
cp /input/Cargo.lock /tmp/project/Cargo.lock
cp /input/lib.rs /tmp/project/src/lib.rs

wit-bindgen rust \
  --world ui-only-reference \
  --out-dir /tmp/project/src \
  /contract/contract.wit

cd /tmp/project
cargo build --release --target wasm32-wasip2 --locked --offline

artifact=/tmp/project/target/wasm32-wasip2/release/vibapp_generated_ui.wasm
test -f "$artifact"
size="$(wc -c < "$artifact")"
test "$size" -gt 0
test "$size" -le 16777216
cp "$artifact" /output/component.wasm
chmod 0600 /output/component.wasm

echo "COMPILE_PASS component_bytes=$size"
