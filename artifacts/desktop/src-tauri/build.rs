use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

fn collect_directory(
    root: &Path,
    label_root: &str,
    inputs: &mut Vec<(String, PathBuf)>,
) -> Result<(), String> {
    let metadata = fs::symlink_metadata(root).map_err(|error| {
        format!(
            "build input root {} is unavailable: {error}",
            root.display()
        )
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(format!(
            "build input root {} must be an ordinary directory",
            root.display()
        ));
    }
    // Watching the directory as well as its current children makes additions
    // invalidate the receipt too; watching only existing files misses that case.
    println!("cargo:rerun-if-changed={}", root.display());

    let mut entries = fs::read_dir(root)
        .map_err(|error| {
            format!(
                "build input root {} cannot be read: {error}",
                root.display()
            )
        })?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| {
            format!(
                "build input root {} cannot be read: {error}",
                root.display()
            )
        })?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        let entry_metadata = fs::symlink_metadata(&path).map_err(|error| {
            format!(
                "build input {} cannot be inspected: {error}",
                path.display()
            )
        })?;
        if entry_metadata.file_type().is_symlink() {
            return Err(format!(
                "build input {} must not be a symlink",
                path.display()
            ));
        }
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| format!("build input {} is not UTF-8", path.display()))?;
        if name.contains(['\n', '\r', '\t', '\0']) {
            return Err(format!("build input {} has an unsafe name", path.display()));
        }
        let label = format!("{label_root}/{name}");
        if entry_metadata.is_dir() {
            collect_directory(&path, &label, inputs)?;
        } else if entry_metadata.is_file() {
            inputs.push((label, path));
        } else {
            return Err(format!(
                "build input {} is not an ordinary file",
                path.display()
            ));
        }
    }
    Ok(())
}

fn add_file(path: PathBuf, label: &str, inputs: &mut Vec<(String, PathBuf)>) -> Result<(), String> {
    let metadata = fs::symlink_metadata(&path)
        .map_err(|error| format!("build input {} is unavailable: {error}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!(
            "build input {} must be an ordinary file",
            path.display()
        ));
    }
    inputs.push((label.to_string(), path));
    Ok(())
}

fn desktop_build_inputs() -> Result<Vec<(String, PathBuf)>, String> {
    let manifest_root = PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR")
            .ok_or_else(|| "CARGO_MANIFEST_DIR is unavailable".to_string())?,
    );
    let desktop_root = manifest_root
        .parent()
        .ok_or_else(|| "desktop root is unavailable".to_string())?;
    let artifacts_root = desktop_root
        .parent()
        .ok_or_else(|| "artifacts root is unavailable".to_string())?;
    let project_root = artifacts_root
        .parent()
        .ok_or_else(|| "project root is unavailable".to_string())?;

    let mut inputs = Vec::new();
    for name in ["local_appstore.py", "public_app_download.py"] {
        add_file(artifacts_root.join("registry-store").join(name), &format!("registry-store/{name}"), &mut inputs)?;
    }
    add_file(desktop_root.join("packaging/macos/Info.plist"), "desktop/packaging/macos/Info.plist", &mut inputs)?;
    add_file(
        artifacts_root.join("cloud-agent/cloud_agent.py"),
        "cloud-agent/cloud_agent.py",
        &mut inputs,
    )?;
    for name in ["opencode_instructions.txt", "codex_instructions.txt", "vibapp_support.rs", "README.md"] {
        add_file(
            artifacts_root.join("cloud-agent/starter").join(name),
            &format!("cloud-agent/starter/{name}"),
            &mut inputs,
        )?;
    }
    add_file(
        artifacts_root.join("cloud-agent/skills/vibapp-ui-ux/SKILL.md"),
        "cloud-agent/skills/vibapp-ui-ux/SKILL.md",
        &mut inputs,
    )?;
    add_file(
        artifacts_root.join("cloud-agent/fixtures/dry-run/provider-result.json"),
        "cloud-agent/fixtures/dry-run/provider-result.json",
        &mut inputs,
    )?;
    add_file(
        artifacts_root.join("cloud-agent/fixtures/dry-run/source/Cargo.toml"),
        "cloud-agent/fixtures/dry-run/source/Cargo.toml",
        &mut inputs,
    )?;
    add_file(
        artifacts_root.join("cloud-agent/fixtures/dry-run/source/src/lib.rs"),
        "cloud-agent/fixtures/dry-run/source/src/lib.rs",
        &mut inputs,
    )?;
    add_file(
        artifacts_root.join("cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json"),
        "cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json",
        &mut inputs,
    )?;
    add_file(
        artifacts_root.join("cloud-agent/schemas/cloud-codeagent-task.schema.json"),
        "cloud-agent/schemas/cloud-codeagent-task.schema.json",
        &mut inputs,
    )?;
    for name in ["provider-result.schema.json", "source-handoff.schema.json"] {
        add_file(
            artifacts_root.join("cloud-agent/schemas").join(name),
            &format!("cloud-agent/schemas/{name}"),
            &mut inputs,
        )?;
    }
    add_file(
        artifacts_root.join("codeagent-adapter/codeagent_adapter.py"),
        "codeagent-adapter/codeagent_adapter.py",
        &mut inputs,
    )?;
    for name in [
        "codeagent-adapter/docker_provider.py",
        "codeagent-launcher/codeagent_launcher.py",
        "codeagent-launcher/docker/entry.mjs",
        "codeagent-launcher/docker/opencode-provider.mjs",
        "codeagent-launcher/docker_executor.py",
        "codeagent-launcher/host_budget.py",
    ] {
        add_file(artifacts_root.join(name), name, &mut inputs)?;
    }
    for name in [
        "app_builder.py",
        "verifier.py",
        "common.py",
        "descriptor_reconciliation.py",
    ] {
        add_file(
            artifacts_root.join("app-builder").join(name),
            &format!("app-builder/{name}"),
            &mut inputs,
        )?;
    }
    for name in [
        "candidate.schema.json",
        "quarantine-receipt.schema.json",
        "verifier-decision.schema.json",
    ] {
        add_file(
            artifacts_root.join("app-builder/schemas").join(name),
            &format!("app-builder/schemas/{name}"),
            &mut inputs,
        )?;
    }
    add_file(
        desktop_root.join("fixtures/state.json"),
        "desktop/fixtures/state.json",
        &mut inputs,
    )?;
    add_file(
        desktop_root.join("runtime-apps/hello/component.wasm"),
        "desktop/runtime-apps/hello/component.wasm",
        &mut inputs,
    )?;
    for name in ["Cargo.lock", "Cargo.toml", "build.rs", "tauri.conf.json"] {
        add_file(
            manifest_root.join(name),
            &format!("desktop/src-tauri/{name}"),
            &mut inputs,
        )?;
    }
    collect_directory(
        &manifest_root.join("src"),
        "desktop/src-tauri/src",
        &mut inputs,
    )?;
    collect_directory(&desktop_root.join("ui"), "desktop/ui", &mut inputs)?;
    for name in [
        "delivery_controller.py",
        "delivery_history.py",
        "runtime_readiness.py",
        "task_archive.py",
        "dry_run_adapter.py",
        "orchestrator.py",
    ] {
        add_file(
            artifacts_root.join("orchestrator").join(name),
            &format!("orchestrator/{name}"),
            &mut inputs,
        )?;
    }
    add_file(
        project_root.join("wit/experimental-v0/contract.wit"),
        "wit/experimental-v0/contract.wit",
        &mut inputs,
    )?;
    inputs.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
    Ok(inputs)
}

fn public_registry_source_inputs() -> Result<Vec<(String, PathBuf)>, String> {
    let manifest_root = PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR")
            .ok_or_else(|| "CARGO_MANIFEST_DIR is unavailable".to_string())?,
    );
    let artifacts_root = manifest_root
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| "artifacts root is unavailable".to_string())?;
    let mut inputs = Vec::new();
    collect_directory(
        &artifacts_root.join("product-platform/registry/package-locators"),
        "product-platform/registry/package-locators",
        &mut inputs,
    )?;
    add_file(
        artifacts_root.join("product-platform/registry/snapshots/registry.snapshot.json"),
        "product-platform/registry/snapshots/registry.snapshot.json",
        &mut inputs,
    )?;
    for name in ["sync-public-package-locators.mjs", "sync-web-gui.mjs"] {
        add_file(
            artifacts_root.join("web-client-core").join(name),
            &format!("web-client-core/{name}"),
            &mut inputs,
        )?;
    }
    inputs.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
    Ok(inputs)
}

fn build_inputs_sha256(inputs: &[(String, PathBuf)]) -> Result<String, String> {
    let mut aggregate = Sha256::new();
    for (label, path) in inputs {
        let bytes = fs::read(path)
            .map_err(|error| format!("build input {} cannot be read: {error}", path.display()))?;
        let file_digest = format!("{:x}", Sha256::digest(&bytes));
        aggregate.update(label.as_bytes());
        aggregate.update([0]);
        aggregate.update(file_digest.as_bytes());
        aggregate.update([0]);
        aggregate.update(bytes.len().to_string().as_bytes());
        aggregate.update([0]);
        println!("cargo:rerun-if-changed={}", path.display());
    }
    Ok(format!("{:x}", aggregate.finalize()))
}

fn main() {
    let inputs = desktop_build_inputs().unwrap_or_else(|error| panic!("{error}"));
    let digest = build_inputs_sha256(&inputs).unwrap_or_else(|error| panic!("{error}"));
    let public_registry_inputs =
        public_registry_source_inputs().unwrap_or_else(|error| panic!("{error}"));
    let public_registry_digest =
        build_inputs_sha256(&public_registry_inputs).unwrap_or_else(|error| panic!("{error}"));
    let marker = format!(
        "\0VIBAPP_DESKTOP_BUILD_INPUTS_SHA256={digest}\0VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256={public_registry_digest}\0"
    );
    let generated = format!(
        "#[used]\nstatic DESKTOP_BUILD_INPUT_RECEIPT: [u8; {}] = *b{:?};\n",
        marker.len(),
        marker
    );
    let output_root = PathBuf::from(
        std::env::var_os("OUT_DIR").unwrap_or_else(|| panic!("OUT_DIR is unavailable")),
    );
    fs::write(
        output_root.join("desktop_build_input_receipt.rs"),
        generated,
    )
    .unwrap_or_else(|error| panic!("desktop build-input receipt could not be written: {error}"));
    println!("cargo:rustc-env=VIBAPP_DESKTOP_BUILD_INPUTS_SHA256={digest}");
    println!("cargo:rustc-env=VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256={public_registry_digest}");
    tauri_build::build()
}
