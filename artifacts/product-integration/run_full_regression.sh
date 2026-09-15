#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
stable_bin=${VIBAPP_STABLE_TOOLCHAIN_BIN:-/Users/zhuzhe/.rustup/toolchains/stable-aarch64-apple-darwin/bin}
web_bin=${VIBAPP_WEB_TOOLCHAIN_BIN:-/Users/zhuzhe/.rustup/toolchains/1.93.0-aarch64-apple-darwin/bin}
runtime_validation_target_root=${VIBAPP_RUNTIME_VALIDATION_TARGET_ROOT:-/tmp/vibapp-runtime-regression}
runtime_service_binary=${VIBAPP_TEST_SERVICE_RUNTIME:-$runtime_validation_target_root/service-runtime/release/vibapp-service-runtime}
export PYTHONDONTWRITEBYTECODE=1
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2}

run_python_suite() {
  suite=$1
  printf '[vibapp regression] python: %s\n' "$suite"
  python3 -W error::ResourceWarning -m unittest discover -s "$repo_root/$suite" -p 'test_*.py' -v
}

printf '[vibapp regression] javascript: regenerate Web from the sole Desktop GUI source\n'
node "$repo_root/artifacts/web-client-core/sync-web-gui.mjs"

run_python_suite artifacts/cloud-agent/tests
run_python_suite artifacts/codeagent-launcher/tests
run_python_suite artifacts/codeagent-adapter/tests
run_python_suite artifacts/app-builder/tests
run_python_suite artifacts/orchestrator/tests
run_python_suite artifacts/provider-runner/tests
run_python_suite artifacts/local-codeagent/tests
run_python_suite artifacts/product-integration
run_python_suite artifacts/product-integration/tests

printf '[vibapp regression] no-request CodeAgent/Builder/Verifier/shared-GUI readiness\n'
python3 -B "$repo_root/artifacts/product-integration/codeagent_vertical.py" \
  readiness --model gpt-5.6-sol

printf '[vibapp regression] python: artifacts/need-analyzer\n'
python3 -W error::ResourceWarning -m unittest discover -s "$repo_root/artifacts/need-analyzer" -p 'test_*.py' -v

run_python_suite artifacts/registry/tests
run_python_suite artifacts/registry-store/tests
printf '[vibapp regression] runtime: isolated rebuilt daemon, fixtures and CLI smoke\n'
VIBAPP_RUNTIME_VALIDATION_TARGET_ROOT="$runtime_validation_target_root" \
  "$repo_root/artifacts/runtime-daemon/tests/run-validation.sh"
run_python_suite artifacts/website/tests
run_python_suite artifacts/product-platform/client/tests
run_python_suite artifacts/product-platform/registry/tests
run_python_suite artifacts/desktop/ci

printf '[vibapp regression] javascript: shared Desktop/Web GUI oracles\n'
node --check "$repo_root/artifacts/desktop/ui/app.js"
node --test "$repo_root/artifacts/desktop/ui/tests/app-ui.test.mjs"

printf '[vibapp regression] rust: desktop\n'
PATH="$stable_bin:$PATH" \
  VIBAPP_TEST_SERVICE_RUNTIME="$runtime_service_binary" \
  CARGO_TARGET_DIR=${VIBAPP_DESKTOP_TEST_TARGET_DIR:-/tmp/vibapp-desktop-regression-target} \
  "$stable_bin/cargo" test --manifest-path "$repo_root/artifacts/desktop/src-tauri/Cargo.toml" --locked

printf '[vibapp regression] rust: web core\n'
PATH="$web_bin:$PATH" \
  CARGO_TARGET_DIR=${VIBAPP_WEB_TEST_TARGET_DIR:-/tmp/vibapp-web-regression-target} \
  "$web_bin/cargo" test --manifest-path "$repo_root/artifacts/web-client-core/Cargo.toml" --locked

printf '[vibapp regression] javascript: web contracts\n'
node --test "$repo_root"/artifacts/web-client-core/tests/*.test.mjs

printf '[vibapp regression] javascript: independent browser derivation verifier\n'
node "$repo_root/artifacts/web-client-core/verify-browser-derivation.mjs"

printf '[vibapp regression] javascript: two-origin preview host\n'
npm test --prefix "$repo_root/artifacts/web-preview-host"

printf '[vibapp regression] javascript: trusted Website product backend\n'
npm test --prefix "$repo_root/artifacts/web-product-backend"

printf '[vibapp regression] javascript: Website local/production start policy\n'
npm run test:local-start --prefix "$repo_root/artifacts/product-platform/website"

printf '[vibapp regression] synthetic complete vertical\n'
VIBAPP_SERVICE_RUNTIME_BIN="$runtime_service_binary" \
  python3 -B "$repo_root/artifacts/product-integration/synthetic_vertical_smoke.py"

if [ "${VIBAPP_INCLUDE_WEB_BUILD:-0}" = 1 ]; then
  printf '[vibapp regression] website production build\n'
  (cd "$repo_root/artifacts/product-platform/website" && \
    NODE_ENV=production \
    VIBAPP_PREVIEW_ORIGIN=https://preview.vibapp.ai \
    npm run build)
fi

printf '[vibapp regression] PASS\n'
