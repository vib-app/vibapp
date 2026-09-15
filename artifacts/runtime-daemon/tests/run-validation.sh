#!/bin/sh
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
RUNTIME_ROOT="$REPO_ROOT/artifacts/runtime-daemon"
HOST_TOOLCHAIN="/Users/zhuzhe/.rustup/toolchains/stable-aarch64-apple-darwin/bin"
GUEST_TOOLCHAIN="/Users/zhuzhe/.rustup/toolchains/1.93.0-aarch64-apple-darwin/bin"
VALIDATION_TARGET_ROOT=${VIBAPP_RUNTIME_VALIDATION_TARGET_ROOT:-/tmp/vibapp-runtime-validation}
SERVICE_RUNTIME_TARGET="$VALIDATION_TARGET_ROOT/service-runtime"
SERVICE_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-service"
HYBRID_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-hybrid"
HYBRID_V2_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-hybrid-v2"
HYBRID_V2_UNHEALTHY_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-hybrid-v2-unhealthy"
HYBRID_V2_MIGRATION_FAIL_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-hybrid-v2-migration-fail"
UI_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-ui"
UI_V2_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-ui-v2"
HYBRID_APP_B_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-hybrid-app-b"
HYBRID_V2_APP_B_FIXTURE_TARGET="$VALIDATION_TARGET_ROOT/fixture-hybrid-v2-app-b"

export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2}

RUSTC="$HOST_TOOLCHAIN/rustc" RUSTDOC="$HOST_TOOLCHAIN/rustdoc" \
  "$HOST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/service-runtime/Cargo.toml" \
  --target-dir "$SERVICE_RUNTIME_TARGET" \
  --offline --locked --release

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$SERVICE_FIXTURE_TARGET" \
  --offline --locked --release

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$HYBRID_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features hybrid

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$HYBRID_V2_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features hybrid,v2

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$HYBRID_V2_UNHEALTHY_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features hybrid,v2,unhealthy

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$HYBRID_V2_MIGRATION_FAIL_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features hybrid,v2,migration-fail

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$UI_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features ui

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$UI_V2_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features ui,v2

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$HYBRID_APP_B_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features hybrid,app-b

RUSTC="$GUEST_TOOLCHAIN/rustc" RUSTDOC="$GUEST_TOOLCHAIN/rustdoc" \
  "$GUEST_TOOLCHAIN/cargo" build \
  --manifest-path "$RUNTIME_ROOT/tests/service-fixture/Cargo.toml" \
  --target wasm32-wasip2 \
  --target-dir "$HYBRID_V2_APP_B_FIXTURE_TARGET" \
  --offline --locked --release --no-default-features --features hybrid,v2,app-b

export VIBAPP_TEST_SERVICE_RUNTIME="$SERVICE_RUNTIME_TARGET/release/vibapp-service-runtime"
export VIBAPP_SERVICE_RUNTIME_BIN="$VIBAPP_TEST_SERVICE_RUNTIME"
export VIBAPP_TEST_SERVICE_COMPONENT="$SERVICE_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_HYBRID_COMPONENT="$HYBRID_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_HYBRID_V2_COMPONENT="$HYBRID_V2_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_HYBRID_V2_UNHEALTHY_COMPONENT="$HYBRID_V2_UNHEALTHY_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_HYBRID_V2_MIGRATION_FAIL_COMPONENT="$HYBRID_V2_MIGRATION_FAIL_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_UI_COMPONENT="$UI_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_UI_V2_COMPONENT="$UI_V2_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_HYBRID_APP_B_COMPONENT="$HYBRID_APP_B_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"
export VIBAPP_TEST_HYBRID_V2_APP_B_COMPONENT="$HYBRID_V2_APP_B_FIXTURE_TARGET/wasm32-wasip2/release/vibapp_runtime_service_fixture.wasm"

cd "$REPO_ROOT"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s artifacts/runtime-daemon/tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 artifacts/runtime-daemon/tests/cli_smoke.py
