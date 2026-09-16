#!/bin/sh
set -eu

desktop_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
artifacts_root=$(CDPATH= cd -- "$desktop_root/.." && pwd)
build_profile=${1:-debug}
codesign_bin=/usr/bin/codesign
otool_bin=/usr/bin/otool
shasum_bin=/usr/bin/shasum
strings_bin=/usr/bin/strings
host_arch=$(/usr/bin/uname -m)
case "$host_arch" in arm64|x86_64) ;; *) echo "unsupported macOS architecture" >&2; exit 64 ;; esac

case "$build_profile" in
  debug|release) ;;
  *) echo "usage: $0 [debug|release]" >&2; exit 64 ;;
esac

target_root=${CARGO_TARGET_DIR:-"$desktop_root/target"}
launcher="$target_root/$build_profile/vibapp-launcher"
runtime="$target_root/$build_profile/vibapp-runtime"
registry_root="$desktop_root/../registry"
registry_cli="$registry_root/registry_service.py"
registry_catalog="$registry_root/fixtures/catalog.json"
orchestrator_root="$desktop_root/../orchestrator"
orchestrator_cli="$orchestrator_root/orchestrator.py"
orchestrator_dry_run_adapter="$orchestrator_root/dry_run_adapter.py"
delivery_controller="$orchestrator_root/delivery_controller.py"
delivery_history="$orchestrator_root/delivery_history.py"
runtime_readiness="$orchestrator_root/runtime_readiness.py"
task_archive="$orchestrator_root/task_archive.py"
cloud_agent_root="$desktop_root/../cloud-agent"
cloud_agent_cli="$cloud_agent_root/cloud_agent.py"
codeagent_adapter_root="$desktop_root/../codeagent-adapter"
codeagent_adapter_cli="$codeagent_adapter_root/codeagent_adapter.py"
codeagent_launcher_root="$desktop_root/../codeagent-launcher"
app_builder_root="$desktop_root/../app-builder"
app_builder_cli="$app_builder_root/app_builder.py"
app_verifier_cli="$app_builder_root/verifier.py"
app_builder_common="$app_builder_root/common.py"
app_descriptor_reconciliation="$app_builder_root/descriptor_reconciliation.py"
runtime_daemon_root="$desktop_root/../runtime-daemon"
runtime_daemon_core="$runtime_daemon_root/vibapp_daemon/core.py"
runtime_daemon_service_executor="$runtime_daemon_root/vibapp_daemon/service_executor.py"
service_runtime_source_root="$runtime_daemon_root/service-runtime"
service_runtime=${VIBAPP_SERVICE_RUNTIME_BIN:-"$runtime_daemon_root/target-service-1_98/release/vibapp-service-runtime"}
local_appstore_root="$desktop_root/../registry-store"
local_appstore_cli="$local_appstore_root/local_appstore.py"
need_analyzer_root="$desktop_root/../need-analyzer"
need_analyzer_cli="$need_analyzer_root/need_analyzer.py"
product_config_root="$desktop_root/../product-config"
llm_env_example="$product_config_root/llm.env.example"
roomhash_adapter_root="$desktop_root/../roomhash-transport"
roomhash_host="$roomhash_adapter_root/roomhash-host.mjs"
roomhash_collaboration_root="$desktop_root/../roomhash-collaboration/src"
roomhash_collaboration_adapter="$roomhash_collaboration_root/index.mjs"
roomhash_collaboration_factory="$roomhash_collaboration_root/roomhash-factory.mjs"
roomhash_source_root=${VIBAPP_ROOMHASH_ROOT:-"$desktop_root/../../../RoomHash"}
roomhash_headless="$roomhash_source_root/headless"
roomhash_node=${VIBAPP_ROOMHASH_NODE_BIN:-}
if [ -z "$roomhash_node" ]; then
  roomhash_node=$(command -v node || true)
fi
public_registry_data="$artifacts_root/product-platform/registry/generated/production-public-data"
public_registry_snapshot="$public_registry_data/registry.snapshot.json"
public_registry_locators="$public_registry_data/package-locators"
public_registry_index="$public_registry_locators/index.json"
public_registry_source_snapshot="$desktop_root/../product-platform/registry/snapshots/registry.snapshot.json"
public_registry_source_locators="$desktop_root/../product-platform/registry/package-locators"
public_registry_projection_sync="$desktop_root/../web-client-core/sync-web-gui.mjs"
public_registry_data_verifier="$desktop_root/../web-client-core/sync-public-package-locators.mjs"
contract_wit="$desktop_root/../../wit/experimental-v0/contract.wit"
runtime_entitlements="$desktop_root/packaging/macos/Runtime.entitlements.plist"
macos_icon="$desktop_root/src-tauri/icons/VibApp.icns"
bundle="$desktop_root/dist/VibApp.app"

emit_desktop_build_input_entries() {
  for input_file in \
    "$app_builder_root/app_builder.py" \
    "$app_builder_root/common.py" \
    "$app_builder_root/descriptor_reconciliation.py" \
    "$app_builder_root/schemas/candidate.schema.json" \
    "$app_builder_root/schemas/quarantine-receipt.schema.json" \
    "$app_builder_root/schemas/verifier-decision.schema.json" \
    "$app_builder_root/verifier.py" \
    "$cloud_agent_root/cloud_agent.py" \
    "$cloud_agent_root/fixtures/dry-run/provider-result.json" \
    "$cloud_agent_root/fixtures/dry-run/source/Cargo.toml" \
    "$cloud_agent_root/fixtures/dry-run/source/src/lib.rs" \
    "$cloud_agent_root/schemas/cloud-codeagent-task.experimental-v2.schema.json" \
    "$cloud_agent_root/schemas/cloud-codeagent-task.schema.json" \
    "$cloud_agent_root/schemas/provider-result.schema.json" \
    "$cloud_agent_root/schemas/source-handoff.schema.json" \
    "$cloud_agent_root/skills/vibapp-ui-ux/SKILL.md" \
    "$cloud_agent_root/starter/README.md" \
    "$cloud_agent_root/starter/codex_instructions.txt" \
    "$cloud_agent_root/starter/opencode_instructions.txt" \
    "$cloud_agent_root/starter/vibapp_support.rs" \
    "$codeagent_adapter_root/codeagent_adapter.py" \
    "$codeagent_adapter_root/docker_provider.py" \
    "$codeagent_launcher_root/codeagent_launcher.py" \
    "$codeagent_launcher_root/docker/entry.mjs" \
    "$codeagent_launcher_root/docker/opencode-provider.mjs" \
    "$codeagent_launcher_root/docker_executor.py" \
    "$codeagent_launcher_root/host_budget.py" \
    "$desktop_root/fixtures/state.json" \
    "$desktop_root/packaging/macos/Info.plist" \
    "$desktop_root/runtime-apps/hello/component.wasm" \
    "$desktop_root/src-tauri/Cargo.lock" \
    "$desktop_root/src-tauri/Cargo.toml" \
    "$desktop_root/src-tauri/build.rs"
  do
    case "$input_file" in
      "$cloud_agent_root"/*) input_label="cloud-agent/${input_file#"$cloud_agent_root"/}" ;;
      "$codeagent_adapter_root"/*) input_label="codeagent-adapter/${input_file#"$codeagent_adapter_root"/}" ;;
      "$codeagent_launcher_root"/*) input_label="codeagent-launcher/${input_file#"$codeagent_launcher_root"/}" ;;
      "$app_builder_root"/*) input_label="app-builder/${input_file#"$app_builder_root"/}" ;;
      "$desktop_root"/*) input_label="desktop/${input_file#"$desktop_root"/}" ;;
      *) echo "desktop build input is outside an approved root: $input_file" >&2; return 66 ;;
    esac
    emit_desktop_build_input_entry "$input_label" "$input_file" || return $?
  done

  emit_desktop_build_input_tree "$desktop_root/src-tauri/src" "desktop/src-tauri/src" || return $?
  emit_desktop_build_input_entry "desktop/src-tauri/tauri.conf.json" "$desktop_root/src-tauri/tauri.conf.json" || return $?
  emit_desktop_build_input_tree "$desktop_root/ui" "desktop/ui" || return $?
  emit_desktop_build_input_entry "orchestrator/delivery_controller.py" "$delivery_controller" || return $?
  emit_desktop_build_input_entry "orchestrator/delivery_history.py" "$delivery_history" || return $?
  emit_desktop_build_input_entry "orchestrator/dry_run_adapter.py" "$orchestrator_dry_run_adapter" || return $?
  emit_desktop_build_input_entry "orchestrator/orchestrator.py" "$orchestrator_cli" || return $?
  emit_desktop_build_input_entry "orchestrator/runtime_readiness.py" "$runtime_readiness" || return $?
  emit_desktop_build_input_entry "orchestrator/task_archive.py" "$task_archive" || return $?
  emit_desktop_build_input_entry "registry-store/local_appstore.py" "$local_appstore_cli" || return $?
  emit_desktop_build_input_entry "registry-store/public_app_download.py" "$local_appstore_root/public_app_download.py" || return $?
  emit_desktop_build_input_entry "wit/experimental-v0/contract.wit" "$contract_wit"
}

emit_public_registry_source_entries() {
  emit_desktop_build_input_tree \
    "$public_registry_source_locators" \
    "product-platform/registry/package-locators" || return $?
  emit_desktop_build_input_entry \
    "product-platform/registry/snapshots/registry.snapshot.json" \
    "$public_registry_source_snapshot" || return $?
  emit_desktop_build_input_entry \
    "web-client-core/sync-public-package-locators.mjs" \
    "$public_registry_data_verifier" || return $?
  emit_desktop_build_input_entry \
    "web-client-core/sync-web-gui.mjs" \
    "$public_registry_projection_sync"
}

emit_desktop_build_input_tree() {
  input_root=$1
  input_label_root=$2
  if [ ! -d "$input_root" ] || [ -L "$input_root" ]; then
    echo "desktop build input root is unavailable or unsafe: $input_root" >&2
    return 66
  fi
  if unsafe_inputs=$(find "$input_root" -type l -print); then
    :
  else
    echo "desktop build input tree could not be inspected: $input_root" >&2
    return 65
  fi
  unsafe_input=$(/usr/bin/printf '%s\n' "$unsafe_inputs" | /usr/bin/sed -n '1p')
  if [ -n "$unsafe_input" ]; then
    echo "desktop build input must not be a symlink: $unsafe_input" >&2
    return 66
  fi

  if unsafe_inputs=$(find "$input_root" ! -type d ! -type f ! -type l -print); then
    :
  else
    echo "desktop build input tree could not be inspected: $input_root" >&2
    return 65
  fi
  unsafe_input=$(/usr/bin/printf '%s\n' "$unsafe_inputs" | /usr/bin/sed -n '1p')
  if [ -n "$unsafe_input" ]; then
    echo "desktop build input must be an ordinary file or directory: $unsafe_input" >&2
    return 66
  fi

  if tree_inputs=$(find "$input_root" -type f -print); then
    :
  else
    echo "desktop build input tree could not be inspected: $input_root" >&2
    return 65
  fi
  if [ -z "$tree_inputs" ]; then
    return 0
  fi
  /usr/bin/printf '%s\n' "$tree_inputs" | LC_ALL=C /usr/bin/sort | while IFS= read -r input_file; do
    relative_input=${input_file#"$input_root"/}
    emit_desktop_build_input_entry "$input_label_root/$relative_input" "$input_file" || exit $?
  done
}

emit_desktop_build_input_entry() {
  input_label=$1
  input_file=$2
  if [ ! -f "$input_file" ] || [ -L "$input_file" ]; then
    echo "desktop build input is unavailable or unsafe: $input_file" >&2
    return 66
  fi
  input_carriage_return=$(/usr/bin/printf '\r')
  input_newline='
'
  case "$input_label" in
    *"$(/usr/bin/printf '\t')"*|*"$input_carriage_return"*|*"$input_newline"*)
      echo "desktop build input label is unsafe: $input_label" >&2
      return 66
      ;;
  esac
  if input_digest_line=$("$shasum_bin" -a 256 "$input_file"); then
    :
  else
    echo "desktop build input could not be hashed: $input_file" >&2
    return 65
  fi
  input_sha256=${input_digest_line%% *}
  if ! /usr/bin/printf '%s\n' "$input_sha256" | /usr/bin/grep -Eq '^[0-9a-f]{64}$'; then
    echo "desktop build input hash is malformed: $input_file" >&2
    return 65
  fi
  if input_size=$(/usr/bin/stat -f '%z' "$input_file"); then
    :
  else
    echo "desktop build input size could not be read: $input_file" >&2
    return 65
  fi
  case "$input_size" in
    ''|*[!0-9]*)
      echo "desktop build input size is malformed: $input_file" >&2
      return 65
      ;;
  esac
  /usr/bin/printf '%s\0%s\0%s\0' "$input_label" "$input_sha256" "$input_size"
}

compute_desktop_build_inputs_sha256() {
  desktop_input_stream=$(mktemp "${TMPDIR:-/tmp}/vibapp-desktop-inputs.XXXXXX") || {
    echo "desktop build-input stream could not be created" >&2
    return 65
  }
  if emit_desktop_build_input_entries > "$desktop_input_stream"; then
    :
  else
    desktop_input_status=$?
    rm -f -- "$desktop_input_stream"
    return "$desktop_input_status"
  fi
  if desktop_input_digest_line=$("$shasum_bin" -a 256 "$desktop_input_stream"); then
    :
  else
    desktop_input_status=$?
    rm -f -- "$desktop_input_stream"
    echo "desktop build-input stream could not be hashed" >&2
    return "$desktop_input_status"
  fi
  rm -f -- "$desktop_input_stream"
  desktop_input_digest=${desktop_input_digest_line%% *}
  /usr/bin/printf '%s\n' "$desktop_input_digest"
}

compute_public_registry_source_sha256() {
  public_registry_input_stream=$(mktemp "${TMPDIR:-/tmp}/vibapp-public-registry-inputs.XXXXXX") || {
    echo "public Registry source stream could not be created" >&2
    return 65
  }
  if emit_public_registry_source_entries > "$public_registry_input_stream"; then
    :
  else
    public_registry_input_status=$?
    rm -f -- "$public_registry_input_stream"
    return "$public_registry_input_status"
  fi
  if public_registry_input_digest_line=$("$shasum_bin" -a 256 "$public_registry_input_stream"); then
    :
  else
    public_registry_input_status=$?
    rm -f -- "$public_registry_input_stream"
    echo "public Registry source stream could not be hashed" >&2
    return "$public_registry_input_status"
  fi
  rm -f -- "$public_registry_input_stream"
  public_registry_input_digest=${public_registry_input_digest_line%% *}
  /usr/bin/printf '%s\n' "$public_registry_input_digest"
}

require_desktop_build_input_receipt() {
  binary_path=$1
  binary_label=$2
  expected_marker="VIBAPP_DESKTOP_BUILD_INPUTS_SHA256=$desktop_build_inputs_sha256"
  if ! "$strings_bin" "$binary_path" | /usr/bin/grep -Fqx "$expected_marker"; then
    echo "desktop build-input receipt mismatch: $binary_label was not built from the current frontend and embedded inputs" >&2
    print_desktop_build_hint
    exit 65
  fi
}

require_public_registry_source_receipt() {
  binary_path=$1
  binary_label=$2
  expected_marker="VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256=$public_registry_source_sha256"
  if ! "$strings_bin" "$binary_path" | /usr/bin/grep -Fqx "$expected_marker"; then
    echo "public Registry source receipt mismatch: $binary_label was not built against the current public projection source" >&2
    print_desktop_build_hint
    exit 65
  fi
}

require_unchanged_build_sources_before_sign() {
  current_desktop_build_inputs_sha256=$(compute_desktop_build_inputs_sha256)
  if [ "$current_desktop_build_inputs_sha256" != "$desktop_build_inputs_sha256" ]; then
    echo "desktop build inputs changed after binary receipt verification and resource staging" >&2
    exit 65
  fi
  current_public_registry_source_sha256=$(compute_public_registry_source_sha256)
  if [ "$current_public_registry_source_sha256" != "$public_registry_source_sha256" ]; then
    echo "public Registry source changed after binary receipt verification and resource staging" >&2
    exit 65
  fi
}

print_desktop_build_hint() {
  echo "rebuild the exact binaries consumed by this package command:" >&2
  if [ "$build_profile" = "release" ]; then
    echo "  CARGO_TARGET_DIR=\"$target_root\" cargo build --manifest-path \"$desktop_root/src-tauri/Cargo.toml\" --locked --release --bin vibapp-launcher --bin vibapp-runtime" >&2
  else
    echo "  CARGO_TARGET_DIR=\"$target_root\" cargo build --manifest-path \"$desktop_root/src-tauri/Cargo.toml\" --locked --bin vibapp-launcher --bin vibapp-runtime" >&2
  fi
}

print_service_runtime_build_hint() {
  echo "rebuild the service runtime consumed by this package command:" >&2
  echo "  CARGO_TARGET_DIR=\"$runtime_daemon_root/target-service-1_98\" cargo build --manifest-path \"$service_runtime_source_root/Cargo.toml\" --offline --locked --release" >&2
  if [ "$service_runtime" != "$runtime_daemon_root/target-service-1_98/release/vibapp-service-runtime" ]; then
    echo "  then replace the selected VIBAPP_SERVICE_RUNTIME_BIN with that fresh build output: $service_runtime" >&2
  fi
}

require_fresh_desktop_binary() {
  binary_path=$1
  binary_label=$2
  rust_source_root="$desktop_root/src-tauri/src"

  if [ ! -f "$binary_path" ] || [ -L "$binary_path" ]; then
    echo "missing or unsafe desktop binary: $binary_path" >&2
    print_desktop_build_hint
    exit 66
  fi
  if [ ! -d "$rust_source_root" ] || [ -L "$rust_source_root" ]; then
    echo "desktop Rust source root is unavailable or unsafe: $rust_source_root" >&2
    exit 66
  fi

  for cargo_input in \
    "$desktop_root/src-tauri/Cargo.toml" \
    "$desktop_root/src-tauri/Cargo.lock" \
    "$desktop_root/src-tauri/build.rs"
  do
    if [ ! -f "$cargo_input" ] || [ -L "$cargo_input" ]; then
      echo "desktop Cargo input is unavailable or unsafe: $cargo_input" >&2
      exit 66
    fi
    if [ "$cargo_input" -nt "$binary_path" ]; then
      echo "stale desktop binary: $binary_label is older than $cargo_input" >&2
      print_desktop_build_hint
      exit 65
    fi
  done

  if ! rust_inputs=$(find "$rust_source_root" -type f -name '*.rs' -print); then
    echo "desktop Rust inputs could not be inspected: $rust_source_root" >&2
    exit 65
  fi
  if [ -z "$rust_inputs" ]; then
    echo "desktop Rust source root contains no Rust inputs: $rust_source_root" >&2
    exit 66
  fi
  if ! stale_rust_inputs=$(find "$rust_source_root" -type f -name '*.rs' -newer "$binary_path" -print); then
    echo "desktop Rust input freshness could not be inspected: $rust_source_root" >&2
    exit 65
  fi
  if [ -n "$stale_rust_inputs" ]; then
    stale_rust_input=$(/usr/bin/printf '%s\n' "$stale_rust_inputs" | /usr/bin/sed -n '1p')
    echo "stale desktop binary: $binary_label is older than $stale_rust_input" >&2
    print_desktop_build_hint
    exit 65
  fi
}

require_fresh_service_runtime() {
  service_rust_source_root="$service_runtime_source_root/src"

  if [ ! -f "$service_runtime" ] || [ -L "$service_runtime" ]; then
    echo "missing or unsafe service runtime binary: $service_runtime" >&2
    print_service_runtime_build_hint
    exit 66
  fi
  if [ ! -d "$service_rust_source_root" ] || [ -L "$service_rust_source_root" ]; then
    echo "service runtime Rust source root is unavailable or unsafe: $service_rust_source_root" >&2
    print_service_runtime_build_hint
    exit 66
  fi

  for cargo_input in \
    "$service_runtime_source_root/Cargo.toml" \
    "$service_runtime_source_root/Cargo.lock"
  do
    if [ ! -f "$cargo_input" ] || [ -L "$cargo_input" ]; then
      echo "service runtime Cargo input is unavailable or unsafe: $cargo_input" >&2
      print_service_runtime_build_hint
      exit 66
    fi
    if [ "$cargo_input" -nt "$service_runtime" ]; then
      echo "stale service runtime binary: vibapp-service-runtime is older than $cargo_input" >&2
      print_service_runtime_build_hint
      exit 65
    fi
  done

  if ! service_rust_inputs=$(find "$service_rust_source_root" -type f -name '*.rs' -print); then
    echo "service runtime Rust inputs could not be inspected: $service_rust_source_root" >&2
    print_service_runtime_build_hint
    exit 65
  fi
  if [ -z "$service_rust_inputs" ]; then
    echo "service runtime Rust source root contains no Rust inputs: $service_rust_source_root" >&2
    print_service_runtime_build_hint
    exit 66
  fi
  if ! stale_service_rust_inputs=$(find "$service_rust_source_root" -type f -name '*.rs' -newer "$service_runtime" -print); then
    echo "service runtime Rust input freshness could not be inspected: $service_rust_source_root" >&2
    print_service_runtime_build_hint
    exit 65
  fi
  if [ -n "$stale_service_rust_inputs" ]; then
    stale_service_rust_input=$(/usr/bin/printf '%s\n' "$stale_service_rust_inputs" | /usr/bin/sed -n '1p')
    echo "stale service runtime binary: vibapp-service-runtime is older than $stale_service_rust_input" >&2
    print_service_runtime_build_hint
    exit 65
  fi
}

require_fresh_desktop_binary "$launcher" "vibapp-launcher"
require_fresh_desktop_binary "$runtime" "vibapp-runtime"
require_fresh_service_runtime
desktop_build_inputs_sha256=$(compute_desktop_build_inputs_sha256)
if ! /usr/bin/printf '%s\n' "$desktop_build_inputs_sha256" | /usr/bin/grep -Eq '^[0-9a-f]{64}$'; then
  echo "desktop build-input digest could not be computed" >&2
  exit 65
fi
require_desktop_build_input_receipt "$launcher" "vibapp-launcher"
require_desktop_build_input_receipt "$runtime" "vibapp-runtime"
public_registry_source_sha256=$(compute_public_registry_source_sha256)
if ! /usr/bin/printf '%s\n' "$public_registry_source_sha256" | /usr/bin/grep -Eq '^[0-9a-f]{64}$'; then
  echo "public Registry source digest could not be computed" >&2
  exit 65
fi
require_public_registry_source_receipt "$launcher" "vibapp-launcher"
require_public_registry_source_receipt "$runtime" "vibapp-runtime"

mkdir -p "$desktop_root/dist"
stage_root=$(mktemp -d "$desktop_root/dist/.vibapp-package.XXXXXX")
staged_bundle="$stage_root/VibApp.app"
contents="$staged_bundle/Contents"
previous_bundle="$stage_root/previous.app"
package_committed=0
bundle_promoted=0

cleanup() {
  cleanup_ok=1
  if [ "$package_committed" -ne 1 ]; then
    if [ "$bundle_promoted" -eq 1 ] && { [ -e "$bundle" ] || [ -L "$bundle" ]; }; then
      if ! mv "$bundle" "$stage_root/aborted.app"; then
        cleanup_ok=0
      fi
    fi
    if [ -e "$previous_bundle" ] || [ -L "$previous_bundle" ]; then
      if [ "$cleanup_ok" -eq 1 ] && ! mv "$previous_bundle" "$bundle"; then
        cleanup_ok=0
      fi
    fi
  fi
  if [ "$cleanup_ok" -eq 1 ]; then
    rm -rf -- "$stage_root"
  else
    echo "package recovery failed; preserved recoverable files at: $stage_root" >&2
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

validate_relocatable_node() {
  node_path=$1
  node_label=$2
  node_failure_status=$3
  if ! /usr/bin/file -L "$node_path" | /usr/bin/grep -q "Mach-O.*$host_arch"; then
    echo "$node_label must be a $host_arch macOS executable: $node_path" >&2
    echo "set VIBAPP_ROOMHASH_NODE_BIN to a standalone $host_arch Node executable" >&2
    exit "$node_failure_status"
  fi
  if ! node_dependency_report=$("$otool_bin" -L "$node_path" 2>&1); then
    echo "$node_label could not be inspected with otool: $node_path" >&2
    /usr/bin/printf '%s\n' "$node_dependency_report" >&2
    exit "$node_failure_status"
  fi
  non_system_node_dependencies=$(
    /usr/bin/printf '%s\n' "$node_dependency_report" \
      | /usr/bin/awk 'NR > 1 && $1 !~ /^\/System\/Library\// && $1 !~ /^\/usr\/lib\// { print $1 }'
  )
  if [ -n "$non_system_node_dependencies" ]; then
    echo "$node_label has non-relocatable dynamic dependencies:" >&2
    /usr/bin/printf '%s\n' "$non_system_node_dependencies" >&2
    echo "set VIBAPP_ROOMHASH_NODE_BIN to a standalone arm64 Node executable" >&2
    exit "$node_failure_status"
  fi
  if ! "$node_path" --version >/dev/null 2>&1; then
    echo "$node_label cannot execute: $node_path" >&2
    echo "set VIBAPP_ROOMHASH_NODE_BIN to a working standalone arm64 Node executable" >&2
    exit "$node_failure_status"
  fi
}

reject_python_bytecode() {
  python_bytecode=$(
    find "$contents/Resources" \( -type d -name '__pycache__' -o -type f \( -name '*.pyc' -o -name '*.pyo' \) \) -print \
      | /usr/bin/sed -n '1p'
  )
  if [ -n "$python_bytecode" ]; then
    echo "packaged Resources contain mutable Python bytecode: $python_bytecode" >&2
    exit 65
  fi
}

for required_file in "$launcher" "$runtime" "$registry_cli" "$registry_catalog" \
  "$orchestrator_cli" "$orchestrator_dry_run_adapter" "$delivery_controller" "$delivery_history" "$runtime_readiness" "$task_archive" "$cloud_agent_cli" \
  "$cloud_agent_root/starter/README.md" "$cloud_agent_root/starter/opencode_instructions.txt" \
  "$cloud_agent_root/starter/codex_instructions.txt" "$cloud_agent_root/starter/vibapp_support.rs" \
  "$cloud_agent_root/skills/vibapp-ui-ux/SKILL.md" \
  "$codeagent_adapter_cli" "$codeagent_launcher_root/docker/entry.mjs" "$codeagent_launcher_root/docker/opencode-provider.mjs" "$codeagent_launcher_root/host_budget.py" \
  "$app_builder_cli" "$app_verifier_cli" "$app_builder_common" "$app_descriptor_reconciliation" \
  "$runtime_daemon_core" "$runtime_daemon_service_executor" "$service_runtime" \
  "$local_appstore_cli" "$need_analyzer_cli" "$llm_env_example" \
  "$roomhash_host" "$roomhash_collaboration_adapter" "$roomhash_collaboration_factory" "$roomhash_node" \
  "$public_registry_source_snapshot" "$public_registry_projection_sync" "$public_registry_data_verifier" \
  "$contract_wit" "$runtime_entitlements" "$macos_icon"; do
  if [ ! -f "$required_file" ]; then
    echo "missing required file: $required_file" >&2
    exit 66
  fi
done
if [ ! -d "$roomhash_headless" ] || [ -L "$roomhash_headless" ] || [ ! -f "$roomhash_headless/package.json" ] || [ ! -f "$roomhash_headless/src/torrent-service.js" ] || [ ! -d "$roomhash_headless/node_modules" ]; then
  echo "RoomHash current headless source is unavailable or unsafe: $roomhash_headless" >&2
  exit 66
fi
if [ ! -x "$codesign_bin" ]; then
  echo "codesign is unavailable: $codesign_bin" >&2
  exit 69
fi
if [ ! -x "$otool_bin" ]; then
  echo "otool is unavailable: $otool_bin" >&2
  exit 69
fi
validate_relocatable_node "$roomhash_node" "RoomHash Node" 65

if [ ! -d "$public_registry_source_locators" ] || [ -L "$public_registry_source_locators" ]; then
  echo "public Registry locator source is unavailable or unsafe: $public_registry_source_locators" >&2
  exit 66
fi
if ! "$roomhash_node" "$public_registry_projection_sync" --production-public-registry-only >/dev/null; then
  echo "production public Registry projection sync failed" >&2
  exit 65
fi
public_registry_source_sha256_after_sync=$(compute_public_registry_source_sha256)
if [ "$public_registry_source_sha256_after_sync" != "$public_registry_source_sha256" ]; then
  echo "public Registry source changed during deterministic projection sync" >&2
  exit 65
fi
if [ ! -d "$public_registry_data" ] || [ -L "$public_registry_data" ] || [ -L "$public_registry_snapshot" ] || [ ! -d "$public_registry_locators" ] || [ -L "$public_registry_locators" ] || [ -L "$public_registry_index" ]; then
  echo "production public Registry data is unavailable or unsafe: $public_registry_data" >&2
  exit 66
fi
if ! "$roomhash_node" "$public_registry_data_verifier" --verify-production-data-root "$public_registry_data" >/dev/null; then
  echo "production public Registry snapshot/index binding is invalid" >&2
  exit 65
fi
public_registry_snapshot_digest_line=$("$shasum_bin" -a 256 "$public_registry_snapshot")
public_registry_snapshot_sha256=${public_registry_snapshot_digest_line%% *}
public_registry_index_digest_line=$("$shasum_bin" -a 256 "$public_registry_index")
public_registry_index_sha256=${public_registry_index_digest_line%% *}
if ! /usr/bin/printf '%s\n' "$public_registry_snapshot_sha256" | /usr/bin/grep -Eq '^[0-9a-f]{64}$' \
  || ! /usr/bin/printf '%s\n' "$public_registry_index_sha256" | /usr/bin/grep -Eq '^[0-9a-f]{64}$'; then
  echo "production public Registry projection digest is malformed" >&2
  exit 65
fi

mkdir -p \
  "$contents/MacOS" \
  "$contents/Resources/registry/fixtures" \
  "$contents/Resources/orchestrator" \
  "$contents/Resources/need-analyzer" \
  "$contents/Resources/product-config" \
  "$contents/Resources/public-registry/data/package-locators" \
  "$contents/Resources/roomhash/collaboration" \
  "$contents/Resources/roomhash/current/headless" \
  "$contents/Resources/codeagent-adapter" \
  "$contents/Resources/codeagent-launcher/docker" \
  "$contents/Resources/app-builder/schemas" \
  "$contents/Resources/runtime-daemon/service-runtime" \
  "$contents/Resources/runtime-daemon/vibapp_daemon" \
  "$contents/Resources/registry-store" \
  "$contents/Resources/cloud-agent/schemas" \
  "$contents/Resources/cloud-agent/starter" \
  "$contents/Resources/cloud-agent/skills/vibapp-ui-ux" \
  "$contents/Resources/cloud-agent/fixtures/dry-run/source/src" \
  "$contents/Resources/build-provenance" \
  "$contents/wit/experimental-v0"
cp "$desktop_root/packaging/macos/Info.plist" "$contents/Info.plist"
cp "$macos_icon" "$contents/Resources/VibApp.icns"
cp "$launcher" "$contents/MacOS/vibapp-launcher"
cp "$runtime" "$contents/MacOS/vibapp-runtime"
cp "$registry_cli" "$contents/Resources/registry/registry_service.py"
cp "$registry_catalog" "$contents/Resources/registry/fixtures/catalog.json"
cp "$orchestrator_cli" "$contents/Resources/orchestrator/orchestrator.py"
cp "$orchestrator_dry_run_adapter" "$contents/Resources/orchestrator/dry_run_adapter.py"
cp "$delivery_controller" "$contents/Resources/orchestrator/delivery_controller.py"
cp "$delivery_history" "$contents/Resources/orchestrator/delivery_history.py"
cp "$runtime_readiness" "$contents/Resources/orchestrator/runtime_readiness.py"
cp "$task_archive" "$contents/Resources/orchestrator/task_archive.py"
cp "$cloud_agent_cli" "$contents/Resources/cloud-agent/cloud_agent.py"
cp "$cloud_agent_root/skills/vibapp-ui-ux/SKILL.md" "$contents/Resources/cloud-agent/skills/vibapp-ui-ux/SKILL.md"
cp "$cloud_agent_root/starter/README.md" "$contents/Resources/cloud-agent/starter/README.md"
cp "$cloud_agent_root/starter/opencode_instructions.txt" "$contents/Resources/cloud-agent/starter/opencode_instructions.txt"
cp "$cloud_agent_root/starter/codex_instructions.txt" "$contents/Resources/cloud-agent/starter/codex_instructions.txt"
cp "$cloud_agent_root/starter/vibapp_support.rs" "$contents/Resources/cloud-agent/starter/vibapp_support.rs"
cp "$codeagent_adapter_cli" "$contents/Resources/codeagent-adapter/codeagent_adapter.py"
cp "$codeagent_adapter_root/docker_provider.py" "$contents/Resources/codeagent-adapter/docker_provider.py"
cp "$codeagent_launcher_root/codeagent_launcher.py" "$codeagent_launcher_root/docker_executor.py" "$codeagent_launcher_root/host_budget.py" "$contents/Resources/codeagent-launcher/"
cp "$codeagent_launcher_root/docker/entry.mjs" "$contents/Resources/codeagent-launcher/docker/entry.mjs"
cp "$codeagent_launcher_root/docker/opencode-provider.mjs" "$contents/Resources/codeagent-launcher/docker/opencode-provider.mjs"
cp "$app_builder_cli" "$contents/Resources/app-builder/app_builder.py"
cp "$app_verifier_cli" "$contents/Resources/app-builder/verifier.py"
cp "$app_builder_common" "$contents/Resources/app-builder/common.py"
cp "$app_descriptor_reconciliation" "$contents/Resources/app-builder/descriptor_reconciliation.py"
cp "$app_builder_root/schemas/candidate.schema.json" "$contents/Resources/app-builder/schemas/candidate.schema.json"
cp "$app_builder_root/schemas/quarantine-receipt.schema.json" "$contents/Resources/app-builder/schemas/quarantine-receipt.schema.json"
cp "$app_builder_root/schemas/verifier-decision.schema.json" "$contents/Resources/app-builder/schemas/verifier-decision.schema.json"
cp "$runtime_daemon_root/vibapp_daemon/__init__.py" "$contents/Resources/runtime-daemon/vibapp_daemon/__init__.py"
cp "$runtime_daemon_root/vibapp_daemon/__main__.py" "$contents/Resources/runtime-daemon/vibapp_daemon/__main__.py"
cp "$runtime_daemon_root/vibapp_daemon/cli.py" "$contents/Resources/runtime-daemon/vibapp_daemon/cli.py"
cp "$runtime_daemon_core" "$contents/Resources/runtime-daemon/vibapp_daemon/core.py"
cp "$runtime_daemon_service_executor" "$contents/Resources/runtime-daemon/vibapp_daemon/service_executor.py"
cp "$service_runtime" "$contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime"
cp "$local_appstore_cli" "$contents/Resources/registry-store/local_appstore.py"
cp "$local_appstore_root/public_app_download.py" "$contents/Resources/registry-store/public_app_download.py"
cp "$need_analyzer_cli" "$contents/Resources/need-analyzer/need_analyzer.py"
cp "$llm_env_example" "$contents/Resources/product-config/llm.env.example"
cp "$public_registry_snapshot" "$contents/Resources/public-registry/data/registry.snapshot.json"
for public_registry_entry in "$public_registry_locators"/* "$public_registry_locators"/.[!.]* "$public_registry_locators"/..?*; do
  if [ -f "$public_registry_entry" ] && [ ! -L "$public_registry_entry" ]; then
    cp "$public_registry_entry" "$contents/Resources/public-registry/data/package-locators/"
  fi
done
packaged_public_registry_data="$contents/Resources/public-registry/data"
if ! "$roomhash_node" "$public_registry_data_verifier" --verify-production-data-root "$packaged_public_registry_data" >/dev/null; then
  echo "packaged public Registry snapshot/index binding is invalid" >&2
  exit 65
fi
public_registry_source_sha256_after_copy=$(compute_public_registry_source_sha256)
current_public_registry_snapshot_digest_line=$("$shasum_bin" -a 256 "$public_registry_snapshot")
current_public_registry_snapshot_sha256=${current_public_registry_snapshot_digest_line%% *}
current_public_registry_index_digest_line=$("$shasum_bin" -a 256 "$public_registry_index")
current_public_registry_index_sha256=${current_public_registry_index_digest_line%% *}
packaged_public_registry_snapshot_digest_line=$("$shasum_bin" -a 256 "$packaged_public_registry_data/registry.snapshot.json")
packaged_public_registry_snapshot_sha256=${packaged_public_registry_snapshot_digest_line%% *}
packaged_public_registry_index_digest_line=$("$shasum_bin" -a 256 "$packaged_public_registry_data/package-locators/index.json")
packaged_public_registry_index_sha256=${packaged_public_registry_index_digest_line%% *}
if [ "$public_registry_source_sha256_after_copy" != "$public_registry_source_sha256" ] \
  || [ "$current_public_registry_snapshot_sha256" != "$public_registry_snapshot_sha256" ] \
  || [ "$current_public_registry_index_sha256" != "$public_registry_index_sha256" ] \
  || [ "$packaged_public_registry_snapshot_sha256" != "$public_registry_snapshot_sha256" ] \
  || [ "$packaged_public_registry_index_sha256" != "$public_registry_index_sha256" ]; then
  echo "public Registry source or generated projection changed while the package was staged" >&2
  exit 65
fi
cp "$roomhash_host" "$contents/Resources/roomhash/roomhash-host.mjs"
cp "$roomhash_collaboration_adapter" "$contents/Resources/roomhash/collaboration/index.mjs"
cp "$roomhash_collaboration_factory" "$contents/Resources/roomhash/collaboration/roomhash-factory.mjs"
cp "$roomhash_node" "$contents/Resources/roomhash/node"
cp "$roomhash_headless/package.json" "$contents/Resources/roomhash/current/headless/package.json"
cp "$roomhash_headless/package-lock.json" "$contents/Resources/roomhash/current/headless/package-lock.json"
cp -R "$roomhash_headless/src" "$contents/Resources/roomhash/current/headless/src"
cp -R "$roomhash_headless/node_modules" "$contents/Resources/roomhash/current/headless/node_modules"
cp "$cloud_agent_root/schemas/cloud-codeagent-task.schema.json" "$contents/Resources/cloud-agent/schemas/cloud-codeagent-task.schema.json"
cp "$cloud_agent_root/schemas/cloud-codeagent-task.experimental-v2.schema.json" "$contents/Resources/cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json"
cp "$cloud_agent_root/schemas/provider-result.schema.json" "$contents/Resources/cloud-agent/schemas/provider-result.schema.json"
cp "$cloud_agent_root/schemas/source-handoff.schema.json" "$contents/Resources/cloud-agent/schemas/source-handoff.schema.json"
cp "$cloud_agent_root/fixtures/dry-run/provider-result.json" "$contents/Resources/cloud-agent/fixtures/dry-run/provider-result.json"
cp "$cloud_agent_root/fixtures/dry-run/source/Cargo.toml" "$contents/Resources/cloud-agent/fixtures/dry-run/source/Cargo.toml"
cp "$cloud_agent_root/fixtures/dry-run/source/src/lib.rs" "$contents/Resources/cloud-agent/fixtures/dry-run/source/src/lib.rs"
cp "$contract_wit" "$contents/wit/experimental-v0/contract.wit"
require_unchanged_build_sources_before_sign
if ! "$roomhash_node" "$public_registry_data_verifier" --verify-production-data-root "$packaged_public_registry_data" >/dev/null; then
  echo "packaged public Registry snapshot/index binding changed before signing" >&2
  exit 65
fi
final_packaged_public_registry_snapshot_digest_line=$("$shasum_bin" -a 256 "$packaged_public_registry_data/registry.snapshot.json")
final_packaged_public_registry_snapshot_sha256=${final_packaged_public_registry_snapshot_digest_line%% *}
final_packaged_public_registry_index_digest_line=$("$shasum_bin" -a 256 "$packaged_public_registry_data/package-locators/index.json")
final_packaged_public_registry_index_sha256=${final_packaged_public_registry_index_digest_line%% *}
if [ "$final_packaged_public_registry_snapshot_sha256" != "$public_registry_snapshot_sha256" ] \
  || [ "$final_packaged_public_registry_index_sha256" != "$public_registry_index_sha256" ]; then
  echo "packaged public Registry projection changed before signing" >&2
  exit 65
fi
/usr/bin/printf '{"schema_version":"vibapp.desktop-build-inputs.v1","sha256":"%s","public_registry_source_sha256":"%s","public_registry_snapshot_sha256":"%s","public_registry_locator_index_sha256":"%s"}\n' \
  "$desktop_build_inputs_sha256" \
  "$public_registry_source_sha256" \
  "$public_registry_snapshot_sha256" \
  "$public_registry_index_sha256" > "$contents/Resources/build-provenance/desktop-inputs.json"
chmod 755 "$contents/MacOS/vibapp-launcher" "$contents/MacOS/vibapp-runtime" "$contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime" "$contents/Resources/roomhash/node"
chmod 644 "$contents/Resources/roomhash/roomhash-host.mjs" "$contents/Resources/roomhash/collaboration/index.mjs" "$contents/Resources/roomhash/collaboration/roomhash-factory.mjs"
find "$contents/Resources/public-registry/data" -type f -exec chmod 644 {} +
if [ -n "${VIBAPP_BUNDLED_PYTHON_ROOT:-}" ]; then
  "$roomhash_node" "$desktop_root/scripts/bundle-python.mjs" "$VIBAPP_BUNDLED_PYTHON_ROOT" "$contents/Resources/python"
fi
reject_python_bytecode
"$codesign_bin" --force --sign - --timestamp=none --options runtime --entitlements "$runtime_entitlements" "$contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime"
# The Verifier pins executable bytes, including their Mach-O signature. Signing
# must not silently turn an accepted inspector into an unrecognized executable.
source_runtime_digest_line=$("$shasum_bin" -a 256 "$service_runtime")
packaged_runtime_digest_line=$("$shasum_bin" -a 256 "$contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime")
if [ "${source_runtime_digest_line%% *}" != "${packaged_runtime_digest_line%% *}" ]; then
  echo "runtime signing changed the accepted inspector bytes; sign the release input with Runtime.entitlements.plist, revalidate its digest pin, and rebuild desktop binaries before packaging" >&2
  exit 65
fi
"${VIBAPP_PYTHON_BIN:-python3}" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from descriptor_reconciliation import descriptor_inspector_preflight; descriptor_inspector_preflight()' "$contents/Resources/app-builder"
"$codesign_bin" --force --sign - --timestamp=none --options runtime --entitlements "$runtime_entitlements" "$contents/Resources/roomhash/node"
validate_relocatable_node "$contents/Resources/roomhash/node" "packaged RoomHash Node" 70
find "$contents/Resources/roomhash/current/headless/node_modules" -type f -name '*.node' -exec sh -c '
  for binary do
    if /usr/bin/file "$binary" | /usr/bin/grep -q "Mach-O"; then
      /usr/bin/codesign --force --sign - --timestamp=none "$binary" || exit 1
    fi
  done
' sh {} +
"$codesign_bin" --force --sign - --timestamp=none --options runtime --entitlements "$runtime_entitlements" "$contents/MacOS/vibapp-runtime"
"$codesign_bin" --force --sign - --timestamp=none --options runtime "$contents/MacOS/vibapp-launcher"
"$codesign_bin" --force --sign - --timestamp=none --options runtime "$staged_bundle"
"$codesign_bin" --verify --deep --strict --verbose=2 "$staged_bundle"

if [ -e "$bundle" ] || [ -L "$bundle" ]; then
  mv "$bundle" "$previous_bundle"
fi
bundle_promoted=1
if ! mv "$staged_bundle" "$bundle"; then
  exit 1
fi
"$codesign_bin" --verify --deep --strict --verbose=2 "$bundle"
package_committed=1

echo "$bundle"
