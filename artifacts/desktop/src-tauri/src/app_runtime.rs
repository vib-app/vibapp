use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use crate::native_platform;

const CANDIDATE_SCHEMA: &str = "vibapp.private-candidate.experimental-v1";
const MAX_CANDIDATES: usize = 64;
const MAX_RECORD_BYTES: u64 = 64 * 1024;
const MAX_COMPONENT_BYTES: u64 = 16 * 1024 * 1024;
const MAX_RUNTIME_OUTPUT_BYTES: usize = 256 * 1024;
const MAX_SURFACE_NODES: usize = 2_048;
const MAX_SURFACE_DEPTH: usize = 32;
const RUNTIME_TIMEOUT: Duration = Duration::from_secs(5);
const UI_IMPORTS: &[&str] = &["clock", "kv", "log", "host-info", "settings"];

#[derive(Clone)]
struct LaunchableCandidate {
    record: Value,
    app_id: String,
    display_name: String,
    version: String,
    component_sha256: String,
    component_path: PathBuf,
}

fn bounded_text(value: &Value, key: &str, maximum: usize) -> Result<String, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|text| {
            !text.is_empty()
                && text.chars().count() <= maximum
                && !text.chars().any(char::is_control)
        })
        .map(str::to_owned)
        .ok_or_else(|| format!("候选记录缺少有效 {key}。"))
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

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn string_set(value: &Value, key: &str, maximum: usize) -> Result<BTreeSet<String>, String> {
    let items = value
        .get(key)
        .and_then(Value::as_array)
        .filter(|items| items.len() <= maximum)
        .ok_or_else(|| format!("候选记录 {key} 不是受限数组。"))?;
    let mut result = BTreeSet::new();
    for item in items {
        let text = item
            .as_str()
            .filter(|text| !text.is_empty() && text.len() <= 64)
            .ok_or_else(|| format!("候选记录 {key} 含无效值。"))?;
        if !result.insert(text.to_string()) {
            return Err(format!("候选记录 {key} 含重复值。"));
        }
    }
    Ok(result)
}

fn read_candidate(root: &Path, record_path: &Path) -> Result<LaunchableCandidate, String> {
    let record_metadata =
        fs::symlink_metadata(record_path).map_err(|error| format!("无法检查候选记录：{error}"))?;
    if !record_metadata.file_type().is_file()
        || record_metadata.file_type().is_symlink()
        || record_metadata.len() == 0
        || record_metadata.len() > MAX_RECORD_BYTES
    {
        return Err("候选记录必须是 1–65536 字节的普通非符号链接文件。".to_string());
    }
    let record: Value = serde_json::from_slice(
        &fs::read(record_path).map_err(|error| format!("无法读取候选记录：{error}"))?,
    )
    .map_err(|error| format!("候选记录不是有效 JSON：{error}"))?;
    if record["schema_version"] != CANDIDATE_SCHEMA
        || record["kind"] != "ui"
        || record["verification_state"] != "experimental-qa-passed"
        || record["launch_eligible"] != true
        || record["launch_mode"] != "isolated-preview"
        || record["install_eligible"] != false
        || record["formal_builder_accepted"] != false
    {
        return Err("候选记录未获私有隔离预览资格。".to_string());
    }

    let app_id = bounded_text(&record, "app_id", 128)?;
    if !valid_app_id(&app_id) {
        return Err("候选 app_id 格式无效。".to_string());
    }
    let expected_parent = record_path
        .parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str());
    if expected_parent != Some(app_id.as_str()) {
        return Err("候选目录与 app_id 不一致。".to_string());
    }
    let display_name = bounded_text(&record, "display_name", 80)?;
    let version = bounded_text(&record, "version", 64)?;
    bounded_text(&record, "summary", 1_000)?;
    bounded_text(&record, "publisher", 80)?;
    if record["publication_state"] != "private" || record["public_publication_performed"] != false {
        return Err("候选记录必须保持私有且未执行公开发布。".to_string());
    }
    let component_sha256 = bounded_text(&record, "component_sha256", 64)?;
    if !valid_digest(&component_sha256) {
        return Err("候选 Component 摘要无效。".to_string());
    }
    let component_size = record["component_size_bytes"]
        .as_u64()
        .filter(|size| (1..=MAX_COMPONENT_BYTES).contains(size))
        .ok_or_else(|| "候选 Component 大小声明无效。".to_string())?;
    let relative = bounded_text(&record, "component_relative_path", 240)?;
    let expected_relative = format!("{app_id}/{component_sha256}/component.wasm");
    if relative != expected_relative {
        return Err("候选 Component 路径没有绑定 app_id 与摘要。".to_string());
    }
    let profiles = string_set(&record, "profiles", 4)?;
    if !profiles.contains("desktop") {
        return Err("当前 Client 只能预览声明 desktop profile 的候选。".to_string());
    }
    let permissions = string_set(&record, "permissions", UI_IMPORTS.len())?;
    let expected_permissions = UI_IMPORTS
        .iter()
        .map(|item| item.to_string())
        .collect::<BTreeSet<_>>();
    if permissions != expected_permissions {
        return Err("候选权限与 ui-only-reference 的固定 imports 不一致。".to_string());
    }

    let component_path = root.join(relative);
    let component_metadata = fs::symlink_metadata(&component_path)
        .map_err(|error| format!("无法检查候选 Component：{error}"))?;
    if !component_metadata.file_type().is_file()
        || component_metadata.file_type().is_symlink()
        || component_metadata.len() != component_size
    {
        return Err("候选 Component 文件类型或大小与记录不一致。".to_string());
    }
    let canonical_root =
        fs::canonicalize(root).map_err(|error| format!("无法解析候选根目录：{error}"))?;
    let canonical_component = fs::canonicalize(&component_path)
        .map_err(|error| format!("无法解析候选 Component：{error}"))?;
    if !canonical_component.starts_with(&canonical_root) {
        return Err("候选 Component 逃逸了私有候选目录。".to_string());
    }
    let bytes = fs::read(&canonical_component)
        .map_err(|error| format!("无法读取候选 Component：{error}"))?;
    let actual_digest = format!("{:x}", Sha256::digest(&bytes));
    if actual_digest != component_sha256 {
        return Err("候选 Component 摘要与记录不一致。".to_string());
    }

    Ok(LaunchableCandidate {
        record,
        app_id,
        display_name,
        version,
        component_sha256,
        component_path: canonical_component,
    })
}

fn candidates(data_dir: &Path) -> Vec<LaunchableCandidate> {
    let root = data_dir.join("private-candidates");
    let Ok(entries) = fs::read_dir(&root) else {
        return Vec::new();
    };
    let mut records = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            (metadata.is_dir() && !entry.path().is_symlink())
                .then_some(entry.path().join("candidate.json"))
        })
        .collect::<Vec<_>>();
    records.sort();
    records.truncate(MAX_CANDIDATES);
    records
        .into_iter()
        .filter_map(|path| read_candidate(&root, &path).ok())
        .collect()
}

pub fn catalog(data_dir: &Path) -> Vec<Value> {
    candidates(data_dir)
        .into_iter()
        .map(|candidate| {
            let mut record = candidate.record;
            record["installation_state"] = json!("preview-only");
            record["runtime_state"] = json!("ready");
            record["ecosystem_target"] = json!("vibapp-client");
            record["surface_policy"] = json!({
                "renderer": "host-semantic-ui",
                "layout": "responsive",
                "size_classes": ["compact", "regular", "wide"]
            });
            record
        })
        .collect()
}

fn runtime_binary() -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 VibApp Launcher：{error}"))?;
    let runtime = executable
        .parent()
        .map(|parent| parent.join("vibapp-runtime"))
        .ok_or_else(|| "无法定位 VibApp Runtime。".to_string())?;
    let metadata = fs::symlink_metadata(&runtime)
        .map_err(|error| format!("VibApp Runtime 尚未构建或打包：{error}"))?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err("VibApp Runtime 必须是普通非符号链接文件。".to_string());
    }
    Ok(runtime)
}

fn wait_for_runtime(mut child: std::process::Child) -> Result<std::process::Output, String> {
    // Drain while the guest is alive: waiting for exit first deadlocks on a
    // valid response larger than the OS pipe buffer. Each reader is bounded.
    let stdout = child.stdout.take().ok_or("Runtime stdout is not piped")?;
    let stderr = child.stderr.take().ok_or("Runtime stderr is not piped")?;
    let (sender, receiver) = mpsc::channel();
    fn drain(
        reader: impl Read,
        index: usize,
        sender: mpsc::Sender<(usize, std::io::Result<Vec<u8>>)>,
    ) {
        let mut bytes = Vec::new();
        let result = reader
            .take((MAX_RUNTIME_OUTPUT_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = sender.send((index, result));
    }
    let stdout_sender = sender.clone();
    thread::spawn(move || drain(stdout, 0, stdout_sender));
    thread::spawn(move || drain(stderr, 1, sender));
    let deadline = Instant::now() + RUNTIME_TIMEOUT;
    let mut streams: [Option<Vec<u8>>; 2] = [None, None];
    let result = (|| {
        loop {
            while let Ok((index, bytes)) = receiver.try_recv() {
                let bytes =
                    bytes.map_err(|error| format!("无法收集 VibApp Runtime 输出：{error}"))?;
                if bytes.len() > MAX_RUNTIME_OUTPUT_BYTES {
                    return Err("VibApp Runtime 输出大小无效。".to_string());
                }
                streams[index] = Some(bytes);
            }
            if let Some(status) = child
                .try_wait()
                .map_err(|error| format!("无法读取 VibApp Runtime 状态：{error}"))?
            {
                if streams.iter().all(Option::is_some) {
                    return Ok(std::process::Output {
                        status,
                        stdout: streams[0].take().unwrap(),
                        stderr: streams[1].take().unwrap(),
                    });
                }
            }
            if Instant::now() >= deadline {
                return Err("VibApp Runtime 超过 5 秒，已终止。".to_string());
            }
            thread::sleep(Duration::from_millis(10));
        }
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}

fn validate_surface(value: &Value) -> Result<(), String> {
    let surface = value
        .get("surface")
        .and_then(Value::as_object)
        .ok_or_else(|| "VibApp Runtime 没有返回有效 surface。".to_string())?;
    let view = surface
        .get("view")
        .and_then(Value::as_object)
        .ok_or_else(|| "VibApp Runtime 没有返回有效语义视图。".to_string())?;
    let root = view
        .get("root")
        .and_then(Value::as_str)
        .filter(|root| !root.is_empty() && root.len() <= 128)
        .ok_or_else(|| "VibApp Runtime 返回的语义视图缺少有效根节点。".to_string())?;
    let nodes = view
        .get("nodes")
        .and_then(Value::as_array)
        .filter(|nodes| !nodes.is_empty() && nodes.len() <= MAX_SURFACE_NODES)
        .ok_or_else(|| "VibApp Runtime 返回的语义节点数量无效。".to_string())?;

    let allowed_kinds = [
        "text",
        "button",
        "field",
        "list-container",
        "progress",
        "confirmation",
        "Text",
        "Button",
        "Field",
        "ListContainer",
        "Progress",
        "Confirmation",
    ];
    let mut parents = BTreeMap::<String, Option<String>>::new();
    for node in nodes {
        let node = node
            .as_object()
            .ok_or_else(|| "VibApp Runtime 返回了非对象语义节点。".to_string())?;
        let id = node
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty() && id.len() <= 128 && !id.chars().any(char::is_control))
            .ok_or_else(|| "VibApp Runtime 返回了无效语义节点 ID。".to_string())?;
        let parent = match node.get("parent") {
            None | Some(Value::Null) => None,
            Some(value) => Some(
                value
                    .as_str()
                    .filter(|parent| !parent.is_empty() && parent.len() <= 128)
                    .ok_or_else(|| "VibApp Runtime 返回了无效父节点 ID。".to_string())?
                    .to_string(),
            ),
        };
        let kind = node
            .get("kind")
            .and_then(Value::as_object)
            .filter(|kind| {
                kind.len() == 1
                    && kind
                        .keys()
                        .next()
                        .is_some_and(|name| allowed_kinds.contains(&name.as_str()))
            })
            .ok_or_else(|| "VibApp Runtime 返回了未知或不唯一的语义节点类型。".to_string())?;
        if !kind.values().next().is_some_and(Value::is_object) {
            return Err("VibApp Runtime 返回了无效语义节点内容。".to_string());
        }
        if parents.insert(id.to_string(), parent).is_some() {
            return Err("VibApp Runtime 返回了重复语义节点 ID。".to_string());
        }
    }

    if !parents.contains_key(root)
        || parents.values().filter(|parent| parent.is_none()).count() != 1
        || parents.get(root) != Some(&None)
    {
        return Err("VibApp Runtime 返回的语义树根节点不唯一。".to_string());
    }
    for (id, parent) in &parents {
        if parent
            .as_ref()
            .is_some_and(|value| !parents.contains_key(value))
        {
            return Err("VibApp Runtime 返回的语义树引用了缺失父节点。".to_string());
        }
        let mut current = Some(id.as_str());
        let mut visited = BTreeSet::new();
        for _ in 0..=MAX_SURFACE_DEPTH {
            let Some(node_id) = current else {
                break;
            };
            if !visited.insert(node_id) {
                return Err("VibApp Runtime 返回的语义树存在循环。".to_string());
            }
            current = parents.get(node_id).and_then(|parent| parent.as_deref());
        }
        if current.is_some() || !visited.contains(root) {
            return Err("VibApp Runtime 返回的语义树过深或未连接到根节点。".to_string());
        }
    }
    Ok(())
}

pub fn validate_surface_update(surface: &Value) -> Result<(), String> {
    validate_surface(&json!({"surface": surface}))
}

fn validate_runtime_result(
    output: std::process::Output,
    candidate: &LaunchableCandidate,
    installed: bool,
) -> Result<Value, String> {
    if output.stdout.is_empty()
        || output.stdout.len() > MAX_RUNTIME_OUTPUT_BYTES
        || output.stderr.len() > MAX_RUNTIME_OUTPUT_BYTES
    {
        return Err("VibApp Runtime 输出大小无效。".to_string());
    }
    if !output.status.success() {
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        return Err(format!("VibApp Runtime 拒绝启动：{}", diagnostic.trim()));
    }
    let mut value: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("VibApp Runtime 返回了无效 JSON：{error}"))?;
    if value["schema_version"] != "vibapp.runtime-launch.experimental.v1"
        || value["runtime_process"] != "vibapp-runtime"
        || value["component_sha256"] != candidate.component_sha256
        || value["descriptor"]["id"] != candidate.app_id
        || value["descriptor"]["display_name"] != candidate.display_name
        || value["descriptor"]["version"] != candidate.version
        || value["isolation"]["separate_process"] != true
        || value["isolation"]["ambient_wasi_linked"] != false
    {
        return Err("VibApp Runtime 返回结果没有绑定候选身份或隔离边界。".to_string());
    }
    validate_surface(&value)?;
    value["launcher_context"] = json!({
        "ecosystem": "vibapp-client",
        "mode": if installed { "installed" } else { "isolated-preview" },
        "installed": installed,
        "host_chrome_owned": true,
        "layout_policy": "host-responsive",
        "size_classes": ["compact", "regular", "wide"]
    });
    Ok(value)
}

pub fn launch(data_dir: &Path, app_id: &str) -> Result<Value, String> {
    if !valid_app_id(app_id) {
        return Err("app_id 格式无效。".to_string());
    }
    let candidate = candidates(data_dir)
        .into_iter()
        .find(|candidate| candidate.app_id == app_id)
        .ok_or_else(|| "没有找到可由 VibApp 启动的私有候选。".to_string())?;
    let child = Command::new(runtime_binary()?)
        .arg("--component")
        .arg(&candidate.component_path)
        .arg("--expected-sha256")
        .arg(&candidate.component_sha256)
        .env_clear()
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法启动独立 VibApp Runtime：{error}"))?;
    validate_runtime_result(wait_for_runtime(child)?, &candidate, false)
}

fn installed_candidate(data_dir: &Path, app_id: &str) -> Result<LaunchableCandidate, String> {
    if !valid_app_id(app_id) {
        return Err("app_id 格式无效。".to_string());
    }
    let runtime_root = data_dir.join("runtime-daemon");
    let state_path = runtime_root.join("state.json");
    let state_metadata = fs::symlink_metadata(&state_path)
        .map_err(|error| format!("无法检查 Runtime daemon 状态：{error}"))?;
    if !state_metadata.file_type().is_file()
        || state_metadata.file_type().is_symlink()
        || state_metadata.len() > 16 * 1024 * 1024
    {
        return Err("Runtime daemon 状态文件类型或大小无效。".to_string());
    }
    let state: Value = serde_json::from_slice(
        &fs::read(&state_path).map_err(|error| format!("无法读取 Runtime daemon 状态：{error}"))?,
    )
    .map_err(|error| format!("Runtime daemon 状态不是有效 JSON：{error}"))?;
    if state["schema_version"] != "vibapp.runtime-daemon.state.experimental-v1" {
        return Err("Runtime daemon 状态版本无效。".to_string());
    }
    let installed = state["apps"]
        .get(app_id)
        .ok_or_else(|| "Runtime daemon 中没有这个已安装应用。".to_string())?;
    if installed["enabled"] != true || installed["quarantined"] == true {
        return Err("应用尚未启用或已被隔离。".to_string());
    }
    let package_relative = installed["package_path"]
        .as_str()
        .filter(|value| !value.is_empty() && value.len() <= 300)
        .ok_or_else(|| "已安装应用缺少有效 package 路径。".to_string())?;
    let canonical_root = fs::canonicalize(&runtime_root)
        .map_err(|error| format!("无法解析 Runtime daemon 根目录：{error}"))?;
    let package_dir = fs::canonicalize(runtime_root.join(package_relative))
        .map_err(|error| format!("无法解析已安装 package：{error}"))?;
    if !package_dir.starts_with(canonical_root.join("packages")) || !package_dir.is_dir() {
        return Err("已安装 package 路径逃逸了 daemon packages 目录。".to_string());
    }
    let manifest_path = package_dir.join("manifest.json");
    let manifest_metadata = fs::symlink_metadata(&manifest_path)
        .map_err(|error| format!("无法检查已安装 manifest：{error}"))?;
    if !manifest_metadata.file_type().is_file()
        || manifest_metadata.file_type().is_symlink()
        || manifest_metadata.len() > 1024 * 1024
    {
        return Err("已安装 manifest 文件类型或大小无效。".to_string());
    }
    let manifest: Value = serde_json::from_slice(
        &fs::read(&manifest_path).map_err(|error| format!("无法读取已安装 manifest：{error}"))?,
    )
    .map_err(|error| format!("已安装 manifest 不是有效 JSON：{error}"))?;
    let display_name = bounded_text(&manifest["app"], "display_name", 80)?;
    let version = bounded_text(&manifest["app"], "version", 64)?;
    if manifest["schema_version"] != "vibapp.manifest.experimental-v0.0.1"
        || manifest["app"]["id"] != app_id
        || manifest["app"]["display_name"] != installed["display_name"]
        || manifest["app"]["version"] != installed["version"]
    {
        return Err("已安装 manifest 没有绑定 daemon 应用身份。".to_string());
    }
    let component = &manifest["artifacts"]["canonical_component"];
    let component_sha256 = component["sha256"]
        .as_str()
        .filter(|value| valid_digest(value))
        .ok_or_else(|| "已安装 manifest 缺少有效 Component 摘要。".to_string())?
        .to_string();
    if installed["component_sha256"] != component_sha256 {
        return Err("已安装 manifest 与 daemon Component 摘要不一致。".to_string());
    }
    let component_relative = component["path"]
        .as_str()
        .filter(|value| !value.is_empty() && !value.contains('/') && !value.contains('\\'))
        .ok_or_else(|| "已安装 manifest Component 路径无效。".to_string())?;
    let component_path = fs::canonicalize(package_dir.join(component_relative))
        .map_err(|error| format!("无法解析已安装 Component：{error}"))?;
    if !component_path.starts_with(&package_dir) {
        return Err("已安装 Component 路径逃逸了 package。".to_string());
    }
    let component_metadata = fs::symlink_metadata(&component_path)
        .map_err(|error| format!("无法检查已安装 Component：{error}"))?;
    if !component_metadata.file_type().is_file()
        || component_metadata.file_type().is_symlink()
        || component_metadata.len() != component["size_bytes"].as_u64().unwrap_or(0)
        || component_metadata.len() > MAX_COMPONENT_BYTES
    {
        return Err("已安装 Component 文件类型或大小无效。".to_string());
    }
    let actual_digest = format!(
        "{:x}",
        Sha256::digest(
            fs::read(&component_path)
                .map_err(|error| format!("无法读取已安装 Component：{error}"))?
        )
    );
    if actual_digest != component_sha256 {
        return Err("已安装 Component 摘要校验失败。".to_string());
    }
    Ok(LaunchableCandidate {
        record: json!({}),
        app_id: app_id.to_string(),
        display_name,
        version,
        component_sha256,
        component_path,
    })
}

pub fn installed_surface(data_dir: &Path, app_id: &str, binding: &Value) -> Result<Value, String> {
    let candidate = installed_candidate(data_dir, app_id)?;
    installed_surface_result(&candidate, binding)
}

fn installed_surface_result(
    candidate: &LaunchableCandidate,
    binding: &Value,
) -> Result<Value, String> {
    // The daemon has already executed the guest with real state and the right
    // UI/hybrid world. The preview runner must never execute it a second time.
    if binding["component_sha256"] != candidate.component_sha256 {
        return Err("daemon surface 与已安装 Component 摘要不一致。".to_string());
    }
    let surface = &binding["semantic_surface"];
    for key in ["session", "surface", "route"] {
        bounded_text(binding, key, 128)?;
        if surface[key] != binding[key] {
            return Err("daemon surface 与启动绑定不一致。".to_string());
        }
    }
    validate_surface_update(surface)?;
    Ok(json!({
        "schema_version": "vibapp.runtime-launch.experimental.v1",
        "runtime_process": "vibapp-service-runtime",
        "component_sha256": candidate.component_sha256,
        "descriptor": {"id": candidate.app_id, "display_name": candidate.display_name, "version": candidate.version},
        "surface": surface,
        "isolation": {"separate_process": true, "ambient_wasi_linked": false},
        "launcher_context": {
            "ecosystem": "vibapp-client", "mode": "installed", "installed": true,
            "host_chrome_owned": true, "layout_policy": "host-responsive",
            "size_classes": ["compact", "regular", "wide"]
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    #[cfg(unix)]
    fn runtime_drains_both_pipes_before_child_exit() {
        let child = Command::new("/bin/sh")
            .args([
                "-c",
                "head -c 131072 /dev/zero; head -c 131072 /dev/zero >&2",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let output = wait_for_runtime(child).unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout.len(), 131072);
        assert_eq!(output.stderr.len(), 131072);
    }

    #[test]
    #[cfg(unix)]
    fn runtime_rejects_excess_output_without_waiting_for_exit() {
        let child = Command::new("/bin/sh")
            .args(["-c", "exec head -c 524288 /dev/zero"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        assert!(wait_for_runtime(child).unwrap_err().contains("输出大小"));
    }

    #[test]
    fn installed_surface_uses_daemon_without_a_preview_executable() {
        let candidate = LaunchableCandidate {
            record: json!({}),
            app_id: "ai.vibapp.test".into(),
            display_name: "Test".into(),
            version: "1.0.0".into(),
            component_sha256: "a".repeat(64),
            component_path: PathBuf::from("/does-not-exist/component.wasm"),
        };
        let mut binding = json!({
            "component_sha256": "a".repeat(64), "session": "s", "surface": "u", "route": "home",
            "semantic_surface": {"session": "s", "surface": "u", "route": "home",
                "view": {"root": "r", "nodes": [{"id": "r", "parent": null, "kind": {"text": {"text": "Saved"}}}]}}
        });
        let result = installed_surface_result(&candidate, &binding).unwrap();
        assert_eq!(result["runtime_process"], "vibapp-service-runtime");
        assert_eq!(result["launcher_context"]["installed"], true);
        binding["semantic_surface"]["session"] = json!("forged");
        assert!(installed_surface_result(&candidate, &binding).is_err());
        binding["component_sha256"] = json!("b".repeat(64));
        assert!(installed_surface_result(&candidate, &binding).is_err());
    }

    fn test_root(label: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target/test-artifacts")
            .join(format!("app-runtime-{label}-{suffix:x}"))
    }

    #[test]
    fn catalog_accepts_only_digest_bound_private_ui_candidate() {
        let data_dir = test_root("valid");
        let root = data_dir.join("private-candidates");
        let app_id = "ai.vibapp.private.test";
        let component = include_bytes!("../../runtime-apps/hello/component.wasm");
        let digest = format!("{:x}", Sha256::digest(component));
        let app_root = root.join(app_id);
        let component_root = app_root.join(&digest);
        fs::create_dir_all(&component_root).unwrap();
        fs::write(component_root.join("component.wasm"), component).unwrap();
        let record = json!({
            "schema_version": CANDIDATE_SCHEMA,
            "app_id": app_id,
            "display_name": "Test App",
            "version": "0.1.0",
            "kind": "ui",
            "summary": "test",
            "publisher": "test",
            "profiles": ["desktop"],
            "permissions": UI_IMPORTS,
            "verification_state": "experimental-qa-passed",
            "publication_state": "private",
            "public_publication_performed": false,
            "formal_builder_accepted": false,
            "install_eligible": false,
            "launch_eligible": true,
            "launch_mode": "isolated-preview",
            "component_relative_path": format!("{app_id}/{digest}/component.wasm"),
            "component_sha256": digest,
            "component_size_bytes": component.len()
        });
        let mut file = fs::File::create(app_root.join("candidate.json")).unwrap();
        serde_json::to_writer(&mut file, &record).unwrap();
        file.write_all(b"\n").unwrap();

        let apps = catalog(&data_dir);
        assert_eq!(apps.len(), 1);
        assert_eq!(apps[0]["ecosystem_target"], "vibapp-client");
        assert_eq!(apps[0]["surface_policy"]["layout"], "responsive");
        assert_eq!(apps[0]["installation_state"], "preview-only");
    }

    #[test]
    fn app_id_rejects_path_and_separator_confusion() {
        for value in ["../escape", "Ai.vibapp.bad", "ai..vibapp", "ai.vibapp/"] {
            assert!(!valid_app_id(value), "{value} must be rejected");
        }
        assert!(valid_app_id("ai.vibapp.good-app"));
    }

    #[test]
    fn semantic_surface_rejects_cycles_and_accepts_one_bounded_tree() {
        let valid = json!({
            "surface": {"view": {"root": "root", "nodes": [
                {"id": "root", "parent": null, "kind": {"list-container": {"label": ""}}},
                {"id": "title", "parent": "root", "kind": {"text": {"text": "Hello", "style": "title"}}}
            ]}}
        });
        validate_surface(&valid).unwrap();

        let cycle = json!({
            "surface": {"view": {"root": "root", "nodes": [
                {"id": "root", "parent": null, "kind": {"list-container": {"label": ""}}},
                {"id": "a", "parent": "b", "kind": {"text": {"text": "A", "style": "body"}}},
                {"id": "b", "parent": "a", "kind": {"text": {"text": "B", "style": "body"}}}
            ]}}
        });
        assert!(validate_surface(&cycle).is_err());
    }
}
