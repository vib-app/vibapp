use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::Read;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use crate::{native_platform, roomhash_host};

const SNAPSHOT_MAX_BYTES: usize = 4 * 1024 * 1024;
const INDEX_MAX_BYTES: usize = 2 * 1024 * 1024;
const LOCATOR_MAX_BYTES: usize = 256 * 1024;
const MAX_RECORDS: usize = 1_024;
const MAX_INDEX_ENTRIES: usize = 1_024;
const MAX_SAFE_JSON_INTEGER: u64 = 9_007_199_254_740_991;
const REGISTRY_RECORD_SCHEMA: &str = "vibapp.registry-record.product-v0.0.1";
const LOCATOR_INDEX_SCHEMA: &str = "vibapp.public-package-locator-index.experimental-v1";
const LOCATOR_TRUST_NOTE: &str = "locator-only-package-bytes-require-vibapp-verification";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RegistrySnapshot {
    browser_artifact_bindings: Vec<Value>,
    browser_preview_records: Vec<Value>,
    consumer_notes: Value,
    corpus_version: String,
    generated_at_utc: String,
    platform_status: String,
    records: Vec<RegistryRecord>,
    sample_search: Value,
    snapshot_version: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryRecord {
    app: RegistryApp,
    compatibility: RegistryCompatibility,
    contract: RegistryContract,
    created_at_utc: String,
    document_type: String,
    package: RegistryPackage,
    permissions: Vec<RegistryPermission>,
    publication: RegistryPublication,
    record_id: String,
    record_revision: u64,
    schema_version: String,
    search_metadata: RegistrySearchMetadata,
    source: RegistrySource,
    verification: RegistryVerification,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryApp {
    display_name: String,
    id: String,
    kind: String,
    publisher: RegistryPublisher,
    summary: String,
    version: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryPublisher {
    display_name: String,
    publisher_id: String,
    verification_state: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryCompatibility {
    platforms: Vec<RegistryPlatform>,
    profiles: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryPlatform {
    arch: String,
    os: String,
    profile: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryContract {
    component_contract: String,
    package_format: String,
    wasi: String,
    wit_world: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryPackage {
    app_id: String,
    manifest_digest_sha256: String,
    package_digest_sha256: String,
    version: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryPermission {
    interface: String,
    necessity: String,
    scope_digest_sha256: String,
    summary: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryPublication {
    public_metadata_digest_sha256: String,
    published_at_utc: String,
    revoked_at_utc: Option<String>,
    state: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistrySearchMetadata {
    capability_labels: Vec<String>,
    search_text_digest_sha256: String,
    tags: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistrySource {
    github_archive: GithubSourceArchive,
    license_spdx: String,
    source_digest_sha256: String,
    visibility: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct GithubSourceArchive {
    organization: String,
    repository: String,
    repository_id: u64,
    commit_sha: String,
    source_digest_sha256: String,
    package_digest_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryVerification {
    evidence: Vec<RegistryEvidence>,
    provenance: RegistryArtifactEvidence,
    revocation: String,
    sbom: RegistryArtifactEvidence,
    scan: RegistryArtifactEvidence,
    status: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryEvidence {
    evidence_id: String,
    kind: String,
    sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryArtifactEvidence {
    media_type: String,
    sha256: String,
    size_bytes: u64,
    visibility: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LocatorIndex {
    schema_version: String,
    trust_note: String,
    registry_snapshot_sha256: String,
    entries: Vec<LocatorIndexEntry>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LocatorIndexEntry {
    app_id: String,
    record_id: String,
    record_revision: u64,
    registry_record_sha256: String,
    package_digest_sha256: String,
    locator_path: String,
    locator_sha256: String,
    size_bytes: u64,
    web_runtime_available: bool,
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn bounded_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.chars().count() <= maximum && !value.chars().any(char::is_control)
}

fn valid_app_id(value: &str) -> bool {
    if value.is_empty() || value.len() > 128 || !value.as_bytes()[0].is_ascii_lowercase() {
        return false;
    }
    let mut previous_separator = false;
    for byte in value.bytes() {
        let separator = matches!(byte, b'.' | b'-');
        if !(byte.is_ascii_lowercase() || byte.is_ascii_digit() || separator)
            || (separator && previous_separator)
        {
            return false;
        }
        previous_separator = separator;
    }
    !previous_separator
}

fn valid_record_id(value: &str) -> bool {
    bounded_text(value, 256)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn unique_bounded_strings(values: &[String], maximum_items: usize, maximum_chars: usize) -> bool {
    values.len() <= maximum_items
        && values
            .iter()
            .all(|value| bounded_text(value, maximum_chars))
        && values.iter().collect::<BTreeSet<_>>().len() == values.len()
}

fn validate_artifact_evidence(value: &RegistryArtifactEvidence) -> bool {
    value.visibility == "public"
        && bounded_text(&value.media_type, 128)
        && is_lower_hex(&value.sha256, 64)
        && (1..=64 * 1024 * 1024).contains(&value.size_bytes)
}

fn validate_public_record(record: &RegistryRecord) -> Result<(), String> {
    if record.app.publisher.publisher_id == "fixture.vibapp"
        || record.app.id.starts_with("ai.vibapp.fixture.")
        || record.record_id.starts_with("registry.fixture.")
        || record.verification.evidence.iter().any(|item| item.evidence_id.starts_with("fixture.verify."))
    {
        return Err("合成测试样例不能进入公开应用商店。".to_string());
    }
    if record.schema_version != REGISTRY_RECORD_SCHEMA
        || record.document_type != "registry-record"
        || !valid_record_id(&record.record_id)
        || record.record_revision == 0
        || record.record_revision > MAX_SAFE_JSON_INTEGER
    {
        return Err("公开 Registry record 身份无效。".to_string());
    }
    if !valid_app_id(&record.app.id)
        || !bounded_text(&record.app.display_name, 160)
        || !bounded_text(&record.app.summary, 2_000)
        || !bounded_text(&record.app.version, 64)
        || !matches!(record.app.kind.as_str(), "ui" | "service" | "hybrid")
        || !valid_app_id(&record.app.publisher.publisher_id)
        || !bounded_text(&record.app.publisher.display_name, 160)
        || record.app.publisher.verification_state != "verified"
    {
        return Err("公开 Registry record 应用或发布者身份无效。".to_string());
    }
    if record.package.app_id != record.app.id
        || record.package.version != record.app.version
        || !is_lower_hex(&record.package.manifest_digest_sha256, 64)
        || !is_lower_hex(&record.package.package_digest_sha256, 64)
    {
        return Err("公开 Registry record 包绑定无效。".to_string());
    }
    if record.publication.state != "published"
        || record.publication.revoked_at_utc.is_some()
        || !is_lower_hex(&record.publication.public_metadata_digest_sha256, 64)
        || !bounded_text(&record.publication.published_at_utc, 64)
        || record.verification.status != "verified"
        || record.verification.revocation != "not-revoked"
        || !matches!(record.source.visibility.as_str(), "private" | "public")
        || !is_lower_hex(&record.source.source_digest_sha256, 64)
    {
        return Err("Registry record 不是公开、已发布、已验证且未撤销的记录。".to_string());
    }
    let archive = &record.source.github_archive;
    let repository = format!("app-{:x}", Sha256::digest(format!("{}\n{}",
        record.app.publisher.publisher_id, record.app.id).as_bytes()));
    if archive.organization != "vib-app" || archive.repository != repository
        || archive.repository_id == 0 || archive.repository_id > MAX_SAFE_JSON_INTEGER
        || !is_lower_hex(&archive.commit_sha, 40)
        || archive.source_digest_sha256 != record.source.source_digest_sha256
        || archive.package_digest_sha256 != record.package.package_digest_sha256
    {
        return Err("公开上架前必须将此版本源码归档到 vib-app GitHub 组织。".to_string());
    }
    if record.contract.package_format != "vibapp.package.experimental-v0"
        || record.contract.component_contract != "vibapp:experimental-v0@0.0.1"
        || record.contract.wasi != "0.2"
        || !matches!(
            record.contract.wit_world.as_str(),
            "ui-only-reference"
                | "service-only-reference"
                | "hybrid-reference"
                | "web-preview-reference"
        )
    {
        return Err("公开 Registry record 使用了不受支持的 VibApp contract。".to_string());
    }
    if record.compatibility.platforms.is_empty()
        || record.compatibility.platforms.len() > 16
        || !unique_bounded_strings(&record.compatibility.profiles, 4, 32)
        || record.compatibility.platforms.iter().any(|platform| {
            !matches!(
                platform.os.as_str(),
                "macos" | "windows" | "linux" | "browser"
            ) || !matches!(platform.arch.as_str(), "aarch64" | "x86-64" | "wasm32")
                || !matches!(
                    platform.profile.as_str(),
                    "desktop" | "headless" | "web-preview" | "web-runtime"
                )
                || !record.compatibility.profiles.contains(&platform.profile)
        })
    {
        return Err("公开 Registry record 兼容性声明无效。".to_string());
    }
    let mut permission_interfaces = HashSet::new();
    if record.permissions.len() > 64
        || record.permissions.iter().any(|permission| {
            !bounded_text(&permission.interface, 256)
                || !permission.interface.starts_with("vibapp:experimental-v0/")
                || !permission.interface.ends_with("@0.0.1")
                || !matches!(permission.necessity.as_str(), "required" | "degradable")
                || !is_lower_hex(&permission.scope_digest_sha256, 64)
                || !bounded_text(&permission.summary, 1_000)
                || !permission_interfaces.insert(permission.interface.as_str())
        })
    {
        return Err("公开 Registry record 权限声明无效。".to_string());
    }
    if record.verification.evidence.is_empty()
        || record.verification.evidence.len() > 64
        || record.verification.evidence.iter().any(|evidence| {
            !bounded_text(&evidence.evidence_id, 256)
                || !bounded_text(&evidence.kind, 64)
                || !is_lower_hex(&evidence.sha256, 64)
        })
        || !validate_artifact_evidence(&record.verification.provenance)
        || !validate_artifact_evidence(&record.verification.sbom)
        || !validate_artifact_evidence(&record.verification.scan)
    {
        return Err("公开 Registry record verifier 证据无效。".to_string());
    }
    if !bounded_text(&record.created_at_utc, 64)
        || !bounded_text(&record.source.license_spdx, 128)
        || !unique_bounded_strings(&record.search_metadata.capability_labels, 64, 128)
        || !unique_bounded_strings(&record.search_metadata.tags, 128, 128)
        || !is_lower_hex(&record.search_metadata.search_text_digest_sha256, 64)
    {
        return Err("公开 Registry record 元数据无效。".to_string());
    }
    Ok(())
}

fn value_u64(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64)
}

fn valid_browser_descriptor(value: &Value, prefix: &str, format_required: bool) -> bool {
    let Some(item) = value.as_object() else {
        return false;
    };
    let Some(path) = item.get("path").and_then(Value::as_str) else {
        return false;
    };
    path.starts_with(prefix)
        && !path
            .split('/')
            .enumerate()
            .any(|(index, part)| index > 0 && (part.is_empty() || part == "." || part == ".."))
        && item.get("media_type").and_then(Value::as_str).is_some()
        && (!format_required || item.get("format").and_then(Value::as_str).is_some())
        && item
            .get("sha256")
            .and_then(Value::as_str)
            .is_some_and(|value| is_lower_hex(value, 64))
        && value_u64(item.get("size_bytes"))
            .is_some_and(|size| (1..=4 * 1024 * 1024).contains(&size))
}

fn is_real_public_browser_binding(record: &RegistryRecord, binding: &Value) -> bool {
    let Some(binding) = binding.as_object() else {
        return false;
    };
    let Some(component) = binding
        .get("canonical_component")
        .and_then(Value::as_object)
    else {
        return false;
    };
    let Some(component_digest) = component.get("sha256").and_then(Value::as_str) else {
        return false;
    };
    let component_prefix = format!("/launcher/components/{component_digest}/");
    let Some(entry) = binding.get("entry") else {
        return false;
    };
    let Some(files) = binding.get("files").and_then(Value::as_array) else {
        return false;
    };
    let Some(attestation) = binding.get("attestation").and_then(Value::as_object) else {
        return false;
    };
    let profile = binding.get("profile").and_then(Value::as_str);
    let mut file_paths = HashSet::new();
    binding.get("schema_version").and_then(Value::as_str)
        == Some("vibapp.browser-derivation-binding.experimental-v1")
        && binding.get("source_kind").and_then(Value::as_str) == Some("verifier-promoted-candidate")
        && binding.get("app_id").and_then(Value::as_str) == Some(record.app.id.as_str())
        && binding
            .get("canonical_package_digest_sha256")
            .and_then(Value::as_str)
            == Some(record.package.package_digest_sha256.as_str())
        && profile.is_some_and(|profile| {
            matches!(profile, "web-preview" | "web-runtime")
                && record
                    .compatibility
                    .profiles
                    .iter()
                    .any(|value| value == profile)
        })
        && component.get("media_type").and_then(Value::as_str) == Some("application/wasm")
        && is_lower_hex(component_digest, 64)
        && value_u64(component.get("size_bytes"))
            .is_some_and(|size| (1..=16 * 1024 * 1024).contains(&size))
        && binding.get("derived_from_sha256").and_then(Value::as_str) == Some(component_digest)
        && entry.get("format").and_then(Value::as_str) == Some("jco-esm")
        && valid_browser_descriptor(entry, &component_prefix, true)
        && (1..=32).contains(&files.len())
        && files.iter().all(|file| {
            valid_browser_descriptor(file, &component_prefix, true)
                && file
                    .get("path")
                    .and_then(Value::as_str)
                    .is_some_and(|path| file_paths.insert(path))
        })
        && files.iter().any(|file| {
            file.get("path") == entry.get("path")
                && file.get("sha256") == entry.get("sha256")
                && file.get("size_bytes") == entry.get("size_bytes")
        })
        && attestation
            .get("verification_state")
            .and_then(Value::as_str)
            == Some("verified")
        && attestation
            .get("canonical_component_transformation_proven")
            .and_then(Value::as_bool)
            == Some(true)
        && attestation
            .get("stage0_activation_eligible")
            .and_then(Value::as_bool)
            == Some(true)
        && attestation
            .get("binding_payload_sha256")
            .and_then(Value::as_str)
            .is_some_and(|value| is_lower_hex(value, 64))
        && attestation
            .get("artifact")
            .is_some_and(|artifact| valid_browser_descriptor(artifact, &component_prefix, false))
}

fn has_public_browser_runtime(snapshot: &RegistrySnapshot, record: &RegistryRecord) -> bool {
    snapshot
        .browser_artifact_bindings
        .iter()
        .any(|binding| is_real_public_browser_binding(record, binding))
}

fn current_platform() -> (&'static str, &'static str) {
    let os = if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else {
        "linux"
    };
    let arch = if cfg!(target_arch = "aarch64") {
        "aarch64"
    } else {
        "x86-64"
    };
    (os, arch)
}

fn supports_current_desktop(record: &RegistryRecord) -> bool {
    let (os, arch) = current_platform();
    record
        .compatibility
        .platforms
        .iter()
        .any(|platform| platform.os == os && platform.arch == arch && platform.profile == "desktop")
}

fn canonical_json(value: &Value, output: &mut String) -> Result<(), String> {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => output.push_str(
            &serde_json::to_string(value)
                .map_err(|_| "Registry canonical JSON 字符串无法编码。".to_string())?,
        ),
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                canonical_json(value, output)?;
            }
            output.push(']');
        }
        Value::Object(values) => {
            output.push('{');
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(
                    &serde_json::to_string(key)
                        .map_err(|_| "Registry canonical JSON key 无法编码。".to_string())?,
                );
                output.push(':');
                canonical_json(&values[key], output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

fn canonical_record_sha256(record: &RegistryRecord) -> Result<String, String> {
    let value = serde_json::to_value(record)
        .map_err(|_| "公开 Registry record 无法规范化。".to_string())?;
    let mut encoded = String::new();
    canonical_json(&value, &mut encoded)?;
    Ok(format!("{:x}", Sha256::digest(encoded.as_bytes())))
}

#[cfg(unix)]
fn same_file_identity(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(not(unix))]
fn same_file_identity(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.is_file() && right.is_file() && left.len() == right.len()
}

#[cfg(unix)]
fn same_directory_identity(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(not(unix))]
fn same_directory_identity(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.is_dir() && right.is_dir()
}

fn checked_directory(path: &Path, label: &str) -> Result<(PathBuf, fs::Metadata), String> {
    if !path.is_absolute() {
        return Err(format!("{label} 必须是绝对路径。"));
    }
    let initial = fs::symlink_metadata(path).map_err(|_| format!("{label} 不可用。"))?;
    if initial.file_type().is_symlink() || !initial.is_dir() {
        return Err(format!("{label} 必须是普通目录，不能是符号链接。"));
    }
    let canonical = fs::canonicalize(path).map_err(|_| format!("{label} 无法解析。"))?;
    let final_metadata = fs::symlink_metadata(path).map_err(|_| format!("{label} 已变化。"))?;
    if final_metadata.file_type().is_symlink()
        || !final_metadata.is_dir()
        || !same_directory_identity(&initial, &final_metadata)
    {
        return Err(format!("{label} 在解析时发生变化。"));
    }
    Ok((canonical, final_metadata))
}

fn confirm_directory(path: &Path, expected: &fs::Metadata, label: &str) -> Result<(), String> {
    let current = fs::symlink_metadata(path).map_err(|_| format!("{label} 已消失。"))?;
    if current.file_type().is_symlink()
        || !current.is_dir()
        || !same_directory_identity(expected, &current)
    {
        return Err(format!("{label} 在读取过程中发生变化。"));
    }
    Ok(())
}

fn read_bounded_regular(path: &Path, maximum: usize, label: &str) -> Result<Vec<u8>, String> {
    let initial = fs::symlink_metadata(path).map_err(|_| format!("{label} 不可用。"))?;
    if initial.file_type().is_symlink()
        || !initial.is_file()
        || initial.len() < 2
        || initial.len() > maximum as u64
    {
        return Err(format!("{label} 不是受限普通文件。"));
    }
    #[cfg(unix)]
    if initial.nlink() != 1 {
        return Err(format!("{label} 不能是硬链接。"));
    }
    let mut file = OpenOptions::new()
        .read(true)
        .open(path)
        .map_err(|_| format!("{label} 无法打开。"))?;
    let opened = file
        .metadata()
        .map_err(|_| format!("{label} 元数据不可用。"))?;
    let opened_path = fs::symlink_metadata(path).map_err(|_| format!("{label} 已变化。"))?;
    if !opened.is_file()
        || opened_path.file_type().is_symlink()
        || !opened_path.is_file()
        || opened.len() != initial.len()
        || !same_file_identity(&initial, &opened)
        || !same_file_identity(&opened, &opened_path)
    {
        return Err(format!("{label} 在打开时发生变化。"));
    }
    #[cfg(unix)]
    if opened.nlink() != 1 || opened_path.nlink() != 1 {
        return Err(format!("{label} 不能是硬链接。"));
    }
    let mut bytes = Vec::with_capacity(initial.len() as usize);
    Read::by_ref(&mut file)
        .take((maximum + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| format!("{label} 无法读取。"))?;
    if bytes.len() != initial.len() as usize || bytes.len() > maximum {
        return Err(format!("{label} 在读取时大小发生变化。"));
    }
    let final_metadata = fs::symlink_metadata(path).map_err(|_| format!("{label} 已变化。"))?;
    if final_metadata.file_type().is_symlink()
        || !final_metadata.is_file()
        || final_metadata.len() != opened.len()
        || !same_file_identity(&opened, &final_metadata)
    {
        return Err(format!("{label} 在读取过程中发生变化。"));
    }
    #[cfg(unix)]
    if final_metadata.nlink() != 1 {
        return Err(format!("{label} 不能是硬链接。"));
    }
    Ok(bytes)
}

fn development_source_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../product-platform/registry/generated/production-public-data")
}

fn source_root() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os("VIBAPP_PUBLIC_REGISTRY_ROOT") {
        let configured = PathBuf::from(configured);
        return checked_directory(&configured, "VIBAPP_PUBLIC_REGISTRY_ROOT").map(|(path, _)| path);
    }
    native_platform::resource_directory("public-registry/data", &development_source_root())
}

fn validate_snapshot(snapshot: &RegistrySnapshot) -> Result<(), String> {
    if snapshot.records.len() > MAX_RECORDS
        || snapshot.browser_artifact_bindings.len() > MAX_RECORDS
        || !snapshot.browser_preview_records.is_empty()
        || !bounded_text(&snapshot.corpus_version, 256)
        || !bounded_text(&snapshot.generated_at_utc, 64)
        || !bounded_text(&snapshot.platform_status, 128)
        || !bounded_text(&snapshot.snapshot_version, 256)
        || !snapshot.sample_search.is_object()
        || snapshot.consumer_notes["website_projection_mode"] != "production-public-only"
        || snapshot.consumer_notes["local_private_preview_enabled"] != false
    {
        return Err("公开 Registry snapshot 不是受限的 production-public-only 投影。".to_string());
    }
    let mut app_ids = HashSet::new();
    let mut record_ids = HashSet::new();
    let mut digests = HashSet::new();
    for record in &snapshot.records {
        validate_public_record(record)?;
        if !app_ids.insert(record.app.id.as_str())
            || !record_ids.insert(record.record_id.as_str())
            || !digests.insert(record.package.package_digest_sha256.as_str())
        {
            return Err("公开 Registry snapshot 含重复应用、record 或包摘要。".to_string());
        }
    }
    let records_by_app = snapshot
        .records
        .iter()
        .map(|record| (record.app.id.as_str(), record))
        .collect::<HashMap<_, _>>();
    let mut browser_profiles = HashSet::new();
    for binding in &snapshot.browser_artifact_bindings {
        let app_id = binding
            .get("app_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let profile = binding
            .get("profile")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let Some(record) = records_by_app.get(app_id) else {
            return Err("公开 Registry browser binding 没有对应的 record。".to_string());
        };
        if !browser_profiles.insert((app_id, profile))
            || !is_real_public_browser_binding(record, binding)
        {
            return Err(
                "公开 Registry browser binding 未通过 exact public trust policy。".to_string(),
            );
        }
    }
    Ok(())
}

fn public_projection(record: &RegistryRecord, entry: &LocatorIndexEntry) -> Value {
    json!({
        "app_id": record.app.id,
        "display_name": record.app.display_name,
        "version": record.app.version,
        "kind": record.app.kind,
        "summary": record.app.summary,
        "publisher": record.app.publisher.display_name,
        "publisher_id": record.app.publisher.publisher_id,
        "permissions": record.permissions.iter().map(|permission| permission.interface.as_str()).collect::<Vec<_>>(),
        "profiles": record.compatibility.profiles,
        "verification_state": "verified",
        "verification_summary": "公开 Registry、独立 verifier 与 P2P locator 已完成摘要绑定；安装时仍由本机 AppStore 复验包内容。",
        "publication_state": "published",
        "publication_badge": "public-appstore",
        "package_digest_sha256": record.package.package_digest_sha256,
        "manifest_digest_sha256": record.package.manifest_digest_sha256,
        "install_eligible": true,
        "launch_eligible": false,
        "installation_state": "candidate",
        "runtime_state": "not-installed",
        "web_cache_eligible": true,
        "web_runtime_available": entry.web_runtime_available,
        "service_entrypoints": [],
        "active_service_entrypoints": [],
        "ecosystem_target": "vibapp-client",
        "surface_policy": {"renderer": "host-semantic-ui", "layout": "responsive"},
        "registry_record": {
            "record_id": record.record_id,
            "record_revision": record.record_revision,
            "registry_record_sha256": entry.registry_record_sha256,
            "locator_sha256": entry.locator_sha256,
            "source_visibility": record.source.visibility,
            "github_archive": record.source.github_archive
        }
    })
}

pub fn sync(data_dir: &Path) -> Result<Vec<Value>, String> {
    sync_from_root(data_dir, &source_root()?)
}

pub(crate) fn sync_from_root(data_dir: &Path, source: &Path) -> Result<Vec<Value>, String> {
    let (source, source_metadata) = checked_directory(source, "公开 Registry 数据根")?;
    let snapshot_path = source.join("registry.snapshot.json");
    let locator_directory_path = source.join("package-locators");
    let (locator_directory, locator_directory_metadata) =
        checked_directory(&locator_directory_path, "公开 Registry locator 目录")?;
    if locator_directory.parent() != Some(source.as_path()) {
        return Err("公开 Registry locator 目录逃逸了数据根。".to_string());
    }
    let root_entries = fs::read_dir(&source)
        .map_err(|_| "公开 Registry 数据根无法枚举。".to_string())?
        .map(|entry| {
            entry
                .map_err(|_| "公开 Registry 数据根无法枚举。".to_string())?
                .file_name()
                .into_string()
                .map_err(|_| "公开 Registry 数据根含非 UTF-8 项。".to_string())
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    if root_entries
        != BTreeSet::from([
            "package-locators".to_string(),
            "registry.snapshot.json".to_string(),
        ])
    {
        return Err("公开 Registry 数据根含意外或缺失项。".to_string());
    }
    let snapshot_bytes =
        read_bounded_regular(&snapshot_path, SNAPSHOT_MAX_BYTES, "公开 Registry snapshot")?;
    let snapshot: RegistrySnapshot = serde_json::from_slice(&snapshot_bytes).map_err(|_| {
        "公开 Registry snapshot JSON malformed, duplicate, or not exact。".to_string()
    })?;
    validate_snapshot(&snapshot)?;
    let snapshot_sha256 = format!("{:x}", Sha256::digest(&snapshot_bytes));

    let index_path = locator_directory.join("index.json");
    let index_bytes = read_bounded_regular(&index_path, INDEX_MAX_BYTES, "公开 locator index")?;
    let index: LocatorIndex = serde_json::from_slice(&index_bytes)
        .map_err(|_| "公开 locator index JSON malformed, duplicate, or not exact。".to_string())?;
    if index.schema_version != LOCATOR_INDEX_SCHEMA
        || index.trust_note != LOCATOR_TRUST_NOTE
        || !is_lower_hex(&index.registry_snapshot_sha256, 64)
        || index.registry_snapshot_sha256 != snapshot_sha256
        || index.entries.len() > MAX_INDEX_ENTRIES
    {
        return Err("公开 locator index 没有绑定 exact Registry snapshot。".to_string());
    }

    let records_by_digest = snapshot
        .records
        .iter()
        .map(|record| (record.package.package_digest_sha256.as_str(), record))
        .collect::<HashMap<_, _>>();
    let mut app_ids = HashSet::new();
    let mut record_ids = HashSet::new();
    let mut package_digests = HashSet::new();
    let mut locator_paths = HashSet::new();
    let mut accepted = Vec::with_capacity(index.entries.len());
    let mut previous_package_digest: Option<&str> = None;
    for entry in &index.entries {
        if !valid_app_id(&entry.app_id)
            || !valid_record_id(&entry.record_id)
            || entry.record_revision == 0
            || entry.record_revision > MAX_SAFE_JSON_INTEGER
            || !is_lower_hex(&entry.registry_record_sha256, 64)
            || !is_lower_hex(&entry.package_digest_sha256, 64)
            || !is_lower_hex(&entry.locator_sha256, 64)
            || !(2..=64 * 1024 * 1024).contains(&entry.size_bytes)
            || entry.locator_path
                != format!(
                    "/data/package-locators/{}.json",
                    entry.package_digest_sha256
                )
            || !app_ids.insert(entry.app_id.as_str())
            || !record_ids.insert(entry.record_id.as_str())
            || !package_digests.insert(entry.package_digest_sha256.as_str())
            || !locator_paths.insert(entry.locator_path.as_str())
            || previous_package_digest
                .is_some_and(|previous| entry.package_digest_sha256.as_str() <= previous)
        {
            return Err("公开 locator index entry 身份、边界或唯一性无效。".to_string());
        }
        previous_package_digest = Some(entry.package_digest_sha256.as_str());
        let record = records_by_digest
            .get(entry.package_digest_sha256.as_str())
            .ok_or_else(|| "公开 locator 没有对应的 Registry record。".to_string())?;
        if entry.app_id != record.app.id
            || entry.record_id != record.record_id
            || entry.record_revision != record.record_revision
            || entry.registry_record_sha256 != canonical_record_sha256(record)?
            || entry.web_runtime_available != has_public_browser_runtime(&snapshot, record)
        {
            return Err("公开 locator index 没有绑定 canonical Registry record。".to_string());
        }
        let locator_path = locator_directory.join(format!("{}.json", entry.package_digest_sha256));
        let locator_bytes =
            read_bounded_regular(&locator_path, LOCATOR_MAX_BYTES, "公开 RoomHash locator")?;
        if format!("{:x}", Sha256::digest(&locator_bytes)) != entry.locator_sha256 {
            return Err("公开 RoomHash locator 文件摘要不匹配。".to_string());
        }
        let locator = roomhash_host::validate_package_locator_bytes(
            &locator_bytes,
            &entry.package_digest_sha256,
        )?;
        if locator["size_bytes"].as_u64() != Some(entry.size_bytes) {
            return Err("公开 RoomHash locator 大小没有绑定 index。".to_string());
        }
        accepted.push((record, entry, locator));
    }
    let expected_locator_entries = std::iter::once("index.json".to_string())
        .chain(
            index
                .entries
                .iter()
                .map(|entry| format!("{}.json", entry.package_digest_sha256)),
        )
        .collect::<BTreeSet<_>>();
    let actual_locator_entries = fs::read_dir(&locator_directory)
        .map_err(|_| "公开 Registry locator 目录无法枚举。".to_string())?
        .map(|entry| {
            let entry = entry.map_err(|_| "公开 Registry locator 目录无法枚举。".to_string())?;
            let metadata = fs::symlink_metadata(entry.path())
                .map_err(|_| "公开 Registry locator 项无法检查。".to_string())?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err("公开 Registry locator 目录含非普通文件。".to_string());
            }
            entry
                .file_name()
                .into_string()
                .map_err(|_| "公开 Registry locator 目录含非 UTF-8 项。".to_string())
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    if actual_locator_entries != expected_locator_entries {
        return Err("公开 Registry locator 目录含意外或缺失项。".to_string());
    }
    confirm_directory(&source, &source_metadata, "公开 Registry 数据根")?;
    confirm_directory(
        &locator_directory,
        &locator_directory_metadata,
        "公开 Registry locator 目录",
    )?;

    let mut projection = Vec::new();
    for (record, entry, locator) in accepted {
        roomhash_host::persist_package_locator(data_dir, &locator)?;
        if supports_current_desktop(record) {
            projection.push(public_projection(record, entry));
        }
    }
    projection.sort_by(|left, right| left["app_id"].as_str().cmp(&right["app_id"].as_str()));
    Ok(projection)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const INFO_HASH: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    #[test]
    fn development_source_is_the_stable_production_projection() {
        let source = development_source_root();
        assert!(source.ends_with("product-platform/registry/generated/production-public-data"));
        assert!(!source.to_string_lossy().contains("website/public/data"));
    }

    fn test_root(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target-roomhash-1_93/test-artifacts")
            .join(format!("public-registry-{label}-{nonce:x}"));
        fs::create_dir_all(&root).unwrap();
        fs::canonicalize(root).unwrap()
    }

    fn valid_record() -> Value {
        json!({
            "app": {
                "display_name": "Public Todo",
                "id": "ai.vibapp.public.todo",
                "kind": "ui",
                "publisher": {
                    "display_name": "Verified Publisher",
                    "publisher_id": "publisher.vibapp",
                    "verification_state": "verified"
                },
                "summary": "A public test application.",
                "version": "0.1.0"
            },
            "compatibility": {
                "platforms": [{
                    "arch": if cfg!(target_arch = "aarch64") { "aarch64" } else { "x86-64" },
                    "os": if cfg!(target_os = "macos") { "macos" } else if cfg!(target_os = "windows") { "windows" } else { "linux" },
                    "profile": "desktop"
                }],
                "profiles": ["desktop"]
            },
            "contract": {
                "component_contract": "vibapp:experimental-v0@0.0.1",
                "package_format": "vibapp.package.experimental-v0",
                "wasi": "0.2",
                "wit_world": "ui-only-reference"
            },
            "created_at_utc": "2026-08-27T00:00:00Z",
            "document_type": "registry-record",
            "package": {
                "app_id": "ai.vibapp.public.todo",
                "manifest_digest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                "package_digest_sha256": DIGEST,
                "version": "0.1.0"
            },
            "permissions": [{
                "interface": "vibapp:experimental-v0/kv@0.0.1",
                "necessity": "required",
                "scope_digest_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                "summary": "App-scoped storage."
            }],
            "publication": {
                "public_metadata_digest_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                "published_at_utc": "2026-08-27T00:00:00Z",
                "revoked_at_utc": null,
                "state": "published"
            },
            "record_id": "registry.public.todo.1",
            "record_revision": 1,
            "schema_version": REGISTRY_RECORD_SCHEMA,
            "search_metadata": {
                "capability_labels": ["local-state"],
                "search_text_digest_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                "tags": ["todo"]
            },
            "source": {
                "github_archive": {
                    "organization": "vib-app",
                    "repository": format!("app-{:x}", Sha256::digest(b"publisher.vibapp\nai.vibapp.public.todo")),
                    "repository_id": 12345,
                    "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "source_digest_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
                    "package_digest_sha256": DIGEST
                },
                "license_spdx": "Apache-2.0",
                "source_digest_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
                "visibility": "public"
            },
            "verification": {
                "evidence": [{
                    "evidence_id": "verify.public.todo.1",
                    "kind": "verifier-report",
                    "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
                }],
                "provenance": {"media_type": "application/json", "sha256": "3333333333333333333333333333333333333333333333333333333333333333", "size_bytes": 1, "visibility": "public"},
                "revocation": "not-revoked",
                "sbom": {"media_type": "application/json", "sha256": "4444444444444444444444444444444444444444444444444444444444444444", "size_bytes": 1, "visibility": "public"},
                "scan": {"media_type": "application/json", "sha256": "5555555555555555555555555555555555555555555555555555555555555555", "size_bytes": 1, "visibility": "public"},
                "status": "verified"
            }
        })
    }

    fn valid_locator() -> Value {
        json!({
            "schema_version": "vibapp.roomhash-package-locator.experimental-v1",
            "package_digest_sha256": DIGEST,
            "transport": "bittorrent-v1",
            "info_hash": INFO_HASH,
            "magnet_uri": format!("magnet:?xt=urn:btih:{INFO_HASH}&dn={DIGEST}.vibapp-candidate&tr=wss%3A%2F%2Ftracker.webtorrent.dev"),
            "size_bytes": 2,
            "files": [
                {"path": "candidate.json", "sha256": "6666666666666666666666666666666666666666666666666666666666666666", "size_bytes": 1},
                {"path": "package/manifest.json", "sha256": "7777777777777777777777777777777777777777777777777777777777777777", "size_bytes": 1}
            ],
            "trust_note": LOCATOR_TRUST_NOTE
        })
    }

    fn write_fixture(root: &Path, record: Value, locator: Value) -> (PathBuf, PathBuf) {
        let source = root.join("source");
        let data = root.join("data");
        let locators = source.join("package-locators");
        fs::create_dir_all(&locators).unwrap();
        fs::create_dir_all(&data).unwrap();
        let snapshot = json!({
            "browser_artifact_bindings": [],
            "browser_preview_records": [],
            "consumer_notes": {"website_projection_mode": "production-public-only", "local_private_preview_enabled": false},
            "corpus_version": "test-corpus.1",
            "generated_at_utc": "2026-08-27T00:00:00Z",
            "platform_status": "local-test",
            "records": [record.clone()],
            "sample_search": {},
            "snapshot_version": "test-snapshot.1"
        });
        let snapshot_bytes = serde_json::to_vec_pretty(&snapshot).unwrap();
        fs::write(source.join("registry.snapshot.json"), &snapshot_bytes).unwrap();
        let locator_bytes = [serde_json::to_vec_pretty(&locator).unwrap(), b"\n".to_vec()].concat();
        fs::write(locators.join(format!("{DIGEST}.json")), &locator_bytes).unwrap();
        let typed: RegistryRecord = serde_json::from_value(record).unwrap();
        let index = json!({
            "schema_version": LOCATOR_INDEX_SCHEMA,
            "trust_note": LOCATOR_TRUST_NOTE,
            "registry_snapshot_sha256": format!("{:x}", Sha256::digest(&snapshot_bytes)),
            "entries": [{
                "app_id": typed.app.id,
                "record_id": typed.record_id,
                "record_revision": typed.record_revision,
                "registry_record_sha256": canonical_record_sha256(&typed).unwrap(),
                "package_digest_sha256": DIGEST,
                "locator_path": format!("/data/package-locators/{DIGEST}.json"),
                "locator_sha256": format!("{:x}", Sha256::digest(&locator_bytes)),
                "size_bytes": 2,
                "web_runtime_available": false
            }]
        });
        fs::write(
            locators.join("index.json"),
            serde_json::to_vec_pretty(&index).unwrap(),
        )
        .unwrap();
        (source, data)
    }

    #[test]
    fn exact_public_registry_locator_sync_projects_and_persists() {
        let root = test_root("positive");
        let (source, data) = write_fixture(&root, valid_record(), valid_locator());
        let apps = sync_from_root(&data, &source).unwrap();
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0]["app_id"], "ai.vibapp.public.todo");
        assert_eq!(apps[0]["publication_badge"], "public-appstore");
        assert_eq!(apps[0]["install_eligible"], true);
        assert!(
            roomhash_host::read_package_locator(&data, DIGEST)
                .unwrap()
                .is_some()
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn canonical_json_matches_the_website_record_hash_algorithm() {
        let value = json!({"z": "中文", "a": {"n": 1, "b": true}, "q": [null, "x"]});
        let mut canonical = String::new();
        canonical_json(&value, &mut canonical).unwrap();
        assert_eq!(
            canonical,
            r#"{"a":{"b":true,"n":1},"q":[null,"x"],"z":"中文"}"#
        );
        assert_eq!(
            format!("{:x}", Sha256::digest(canonical.as_bytes())),
            "578e9e7856accdcd4b5408dddae2b2cb4f2667d09934c7d53c78c62ca8d654cb"
        );
    }

    #[test]
    fn synthetic_namespaces_cannot_claim_public_authority() {
        for pointer in ["/app/publisher/publisher_id", "/app/id", "/record_id", "/verification/evidence/0/evidence_id"] {
            let mut value = valid_record();
            *value.pointer_mut(pointer).unwrap() = json!(match pointer {
                "/app/publisher/publisher_id" => "fixture.vibapp",
                "/app/id" => "ai.vibapp.fixture.todo",
                "/record_id" => "registry.fixture.todo",
                _ => "fixture.verify.1",
            });
            let record: RegistryRecord = serde_json::from_value(value).unwrap();
            assert!(validate_public_record(&record).unwrap_err().contains("合成测试样例"));
        }
    }

    #[test]
    fn registry_and_index_bindings_fail_closed_before_persist() {
        let mut legacy = valid_record();
        legacy["source"].as_object_mut().unwrap().remove("github_archive");
        assert!(serde_json::from_value::<RegistryRecord>(legacy).is_err());
        let mut private_source = valid_record();
        private_source["source"]["visibility"] = json!("private");
        validate_public_record(&serde_json::from_value(private_source).unwrap()).unwrap();
        for label in [
            "schema",
            "record-id",
            "record-revision",
            "private",
            "unverified",
            "wrong-archive-source",
            "wrong-archive-org",
            "publisher",
            "public-metadata-digest",
            "revoked-at",
            "manifest-digest",
            "source-digest",
            "version",
        ] {
            let root = test_root(label);
            let mut record = valid_record();
            match label {
                "schema" => {
                    record["schema_version"] = json!("vibapp.registry-record.product-v0.0.0")
                }
                "record-id" => record["record_id"] = json!("registry/public/todo"),
                "record-revision" => record["record_revision"] = json!(0),
                "private" => record["publication"]["state"] = json!("private"),
                "unverified" => record["verification"]["status"] = json!("failed"),
                "wrong-archive-source" => record["source"]["github_archive"]["source_digest_sha256"] = json!("0".repeat(64)),
                "wrong-archive-org" => record["source"]["github_archive"]["organization"] = json!("other-org"),
                "publisher" => {
                    record["app"]["publisher"]["verification_state"] = json!("unverified")
                }
                "public-metadata-digest" => {
                    record["publication"]["public_metadata_digest_sha256"] = json!("invalid")
                }
                "revoked-at" => {
                    record["publication"]["revoked_at_utc"] = json!("2026-08-30T00:00:00Z")
                }
                "manifest-digest" => record["package"]["manifest_digest_sha256"] = json!("invalid"),
                "source-digest" => record["source"]["source_digest_sha256"] = json!("invalid"),
                "version" => record["package"]["version"] = json!("0.2.0"),
                _ => unreachable!(),
            }
            let (source, data) = write_fixture(&root, record, valid_locator());
            assert!(sync_from_root(&data, &source).is_err());
            assert!(!data.join("roomhash/package-locators").exists());
            fs::remove_dir_all(root).unwrap();
        }

        let root = test_root("snapshot-binding");
        let (source, data) = write_fixture(&root, valid_record(), valid_locator());
        let index_path = source.join("package-locators/index.json");
        let mut index: Value = serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
        index["registry_snapshot_sha256"] =
            json!("9999999999999999999999999999999999999999999999999999999999999999");
        fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
        assert!(sync_from_root(&data, &source).is_err());
        assert!(!data.join("roomhash/package-locators").exists());
        fs::remove_dir_all(root).unwrap();

        let root = test_root("forged-web-runtime-availability");
        let (source, data) = write_fixture(&root, valid_record(), valid_locator());
        let index_path = source.join("package-locators/index.json");
        let mut index: Value = serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
        index["entries"][0]["web_runtime_available"] = json!(true);
        fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
        assert!(sync_from_root(&data, &source).is_err());
        assert!(!data.join("roomhash/package-locators").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn locator_hash_size_tracker_and_unknown_fields_fail_closed() {
        let cases = [
            "locator-hash",
            "locator-size",
            "tracker",
            "index-extra",
            "record-extra",
            "extra-root",
            "extra-locator",
        ];
        for label in cases {
            let root = test_root(label);
            let (source, data) = write_fixture(&root, valid_record(), valid_locator());
            let index_path = source.join("package-locators/index.json");
            let locator_path = source
                .join("package-locators")
                .join(format!("{DIGEST}.json"));
            match label {
                "locator-hash" => {
                    let mut index: Value =
                        serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
                    index["entries"][0]["locator_sha256"] =
                        json!("8888888888888888888888888888888888888888888888888888888888888888");
                    fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
                }
                "locator-size" => {
                    let mut index: Value =
                        serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
                    index["entries"][0]["size_bytes"] = json!(3);
                    fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
                }
                "tracker" => {
                    let mut locator = valid_locator();
                    locator["magnet_uri"] = json!(format!(
                        "magnet:?xt=urn:btih:{INFO_HASH}&dn={DIGEST}.vibapp-candidate&tr=wss%3A%2F%2Fevil.invalid"
                    ));
                    let bytes = serde_json::to_vec(&locator).unwrap();
                    fs::write(&locator_path, &bytes).unwrap();
                    let mut index: Value =
                        serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
                    index["entries"][0]["locator_sha256"] =
                        json!(format!("{:x}", Sha256::digest(&bytes)));
                    fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
                }
                "index-extra" => {
                    let mut index: Value =
                        serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
                    index["entries"][0]["install_authority"] = json!(true);
                    fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
                }
                "record-extra" => {
                    let snapshot_path = source.join("registry.snapshot.json");
                    let mut snapshot: Value =
                        serde_json::from_slice(&fs::read(&snapshot_path).unwrap()).unwrap();
                    snapshot["records"][0]["install_authority"] = json!(true);
                    let bytes = serde_json::to_vec(&snapshot).unwrap();
                    fs::write(&snapshot_path, &bytes).unwrap();
                    let mut index: Value =
                        serde_json::from_slice(&fs::read(&index_path).unwrap()).unwrap();
                    index["registry_snapshot_sha256"] =
                        json!(format!("{:x}", Sha256::digest(&bytes)));
                    fs::write(&index_path, serde_json::to_vec(&index).unwrap()).unwrap();
                }
                "extra-root" => {
                    fs::write(source.join("unexpected.json"), b"{}\n").unwrap();
                }
                "extra-locator" => {
                    fs::write(source.join("package-locators/unlisted.json"), b"{}\n").unwrap();
                }
                _ => unreachable!(),
            }
            assert!(sync_from_root(&data, &source).is_err(), "{label}");
            assert!(!data.join("roomhash/package-locators").exists(), "{label}");
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn source_and_sidecar_symlinks_are_rejected_without_escape() {
        use std::os::unix::fs::symlink;

        let root = test_root("symlinks");
        let (source, data) = write_fixture(&root, valid_record(), valid_locator());
        let alias = root.join("source-alias");
        symlink(&source, &alias).unwrap();
        assert!(sync_from_root(&data, &alias).is_err());

        let locator_path = source
            .join("package-locators")
            .join(format!("{DIGEST}.json"));
        let outside = root.join("outside.json");
        fs::rename(&locator_path, &outside).unwrap();
        symlink(&outside, &locator_path).unwrap();
        assert!(sync_from_root(&data, &source).is_err());
        assert!(!data.join("roomhash/package-locators").exists());
        fs::remove_dir_all(root).unwrap();
    }
}
