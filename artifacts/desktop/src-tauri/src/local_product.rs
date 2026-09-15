use crate::native_platform;
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const MAX_REPLY_BYTES: usize = 512 * 1024;
const MAX_STATE_BYTES: u64 = 16 * 1024 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const DAEMON_START_TIMEOUT: Duration = Duration::from_secs(5);
const HOST_PRESENTATION_SCHEMA: &str = "vibapp.host-presentation.experimental-v1";

#[derive(Deserialize)]
pub struct LifecycleInput {
    pub app_id: String,
    pub action: String,
    #[serde(default)]
    pub entrypoint: Option<String>,
    #[serde(default)]
    pub disposition: Option<String>,
    #[serde(default)]
    pub session: Option<String>,
    #[serde(default)]
    pub surface: Option<String>,
    #[serde(default)]
    pub trigger_id: Option<String>,
    #[serde(default)]
    pub payload: Option<Vec<u8>>,
    #[serde(default, alias = "packageDigestSha256")]
    pub package_digest_sha256: Option<String>,
}

#[derive(Deserialize)]
pub struct UiActionInput {
    #[serde(alias = "appId")]
    pub app_id: String,
    pub entrypoint: String,
    #[serde(alias = "packageDigestSha256")]
    pub package_digest_sha256: String,
    #[serde(alias = "componentSha256")]
    pub component_sha256: String,
    pub generation: String,
    pub session: String,
    pub surface: String,
    pub route: String,
    pub action: String,
    #[serde(alias = "eventId")]
    pub event_id: String,
    pub fields: Vec<Value>,
}

#[derive(Deserialize)]
pub struct UiRefreshInput {
    #[serde(alias = "appId")]
    pub app_id: String,
    pub entrypoint: String,
    #[serde(alias = "packageDigestSha256")]
    pub package_digest_sha256: String,
    #[serde(alias = "componentSha256")]
    pub component_sha256: String,
    pub generation: String,
    pub session: String,
    pub surface: String,
    pub route: String,
    #[serde(alias = "eventId")]
    pub event_id: String,
}

#[derive(Deserialize)]
pub struct UiRenderFailureInput {
    #[serde(alias = "appId")]
    pub app_id: String,
    pub entrypoint: String,
    #[serde(alias = "packageDigestSha256")]
    pub package_digest_sha256: String,
    #[serde(alias = "componentSha256")]
    pub component_sha256: String,
    pub generation: String,
    pub session: String,
    pub surface: String,
    pub route: String,
    #[serde(alias = "eventId")]
    pub event_id: String,
    #[serde(alias = "renderFailureToken")]
    pub render_failure_token: String,
    pub reason: String,
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

fn appstore_script() -> Result<PathBuf, String> {
    native_platform::resource_file(
        "registry-store/local_appstore.py",
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../registry-store/local_appstore.py"),
    )
}

fn daemon_module_root() -> Result<PathBuf, String> {
    native_platform::resource_directory(
        "runtime-daemon",
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../runtime-daemon"),
    )
}

fn service_runtime_binary() -> Result<PathBuf, String> {
    #[cfg(test)]
    if let Some(configured) = std::env::var_os("VIBAPP_TEST_SERVICE_RUNTIME") {
        return Ok(PathBuf::from(configured));
    }
    native_platform::service_runtime_binary(
        &Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../runtime-daemon/target-service-1_98/release/vibapp-service-runtime"),
    )
}

pub fn store_root(data_dir: &Path) -> PathBuf {
    data_dir.join("local-appstore")
}

pub fn runtime_root(data_dir: &Path) -> PathBuf {
    data_dir.join("runtime-daemon")
}

fn wait_for_output(
    mut child: std::process::Child,
    timeout: Duration,
) -> Result<std::process::Output, String> {
    let stdout = child.stdout.take().map(|pipe| {
        thread::spawn(move || {
            let mut bytes = Vec::new();
            pipe.take((MAX_REPLY_BYTES + 1) as u64)
                .read_to_end(&mut bytes)
                .map(|_| bytes)
        })
    });
    let stderr = child.stderr.take().map(|pipe| {
        thread::spawn(move || {
            let mut bytes = Vec::new();
            pipe.take((MAX_REPLY_BYTES + 1) as u64)
                .read_to_end(&mut bytes)
                .map(|_| bytes)
        })
    });
    let deadline = SystemTime::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if SystemTime::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("本地产品命令超时，已终止。".to_string());
            }
            Err(error) => return Err(format!("无法读取本地产品命令状态：{error}")),
        }
    };
    let collect = |reader: Option<thread::JoinHandle<std::io::Result<Vec<u8>>>>| {
        reader
            .map(|handle| {
                handle
                    .join()
                    .map_err(|_| "本地产品输出读取线程异常退出。".to_string())?
                    .map_err(|error| format!("无法收集本地产品命令输出：{error}"))
            })
            .unwrap_or_else(|| Ok(Vec::new()))
    };
    Ok(std::process::Output {
        status,
        stdout: collect(stdout)?,
        stderr: collect(stderr)?,
    })
}

fn parse_output(output: std::process::Output, label: &str) -> Result<Value, String> {
    if output.stdout.is_empty()
        || output.stdout.len() > MAX_REPLY_BYTES
        || output.stderr.len() > MAX_REPLY_BYTES
    {
        return Err(format!("{label} 返回大小无效。"));
    }
    let parsed: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("{label} 返回了无效 JSON：{error}"))?;
    if !output.status.success() {
        if let (Some(code), Some(message)) = (
            parsed["error"]["code"].as_str(),
            parsed["error"]["message"].as_str(),
        ) {
            return Err(format!("{label} 拒绝操作 [{code}]：{message}"));
        }
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        return Err(format!("{label} 拒绝操作：{}", diagnostic.trim()));
    }
    Ok(parsed)
}

fn appstore_command(data_dir: &Path, arguments: &[&str]) -> Result<Value, String> {
    let mut command = Command::new(native_platform::python_executable(false)?);
    command
        .arg("-I")
        .arg("-B")
        .arg(appstore_script()?)
        .arg("--store-root")
        .arg(store_root(data_dir));
    command.args(arguments);
    let child = command
        .env_clear()
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法启动本地 AppStore：{error}"))?;
    let value = parse_output(wait_for_output(child, COMMAND_TIMEOUT)?, "本地 AppStore")?;
    if value["schema_version"] != "vibapp.local-appstore-result.experimental-v1" {
        return Err("本地 AppStore 返回了不支持的结果版本。".to_string());
    }
    Ok(value)
}

pub fn ingest(data_dir: &Path, candidate_path: &str) -> Result<Value, String> {
    let candidate = Path::new(candidate_path);
    if !candidate.is_absolute() || !candidate.is_file() || candidate.is_symlink() {
        return Err("只接受明确的绝对 verifier candidate.json 普通文件路径。".to_string());
    }
    appstore_command(data_dir, &["ingest", candidate_path])
}

pub fn fetch_public_store_app(data_dir: &Path, app_id: &str) -> Result<Value, String> {
    if !valid_app_id(app_id) { return Err("Invalid Store app ID".into()); }
    let script = native_platform::resource_file("registry-store/public_app_download.py",
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../registry-store/public_app_download.py"))?;
    let child = Command::new(native_platform::python_executable(false)?)
        .args(["-I", "-B"]).arg(script).arg("--store-root").arg(store_root(data_dir))
        .arg("--app-id").arg(app_id)
        .env_clear().env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8").env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("Cannot start Store download: {e}"))?;
    let result = parse_output(wait_for_output(child, Duration::from_secs(90))?, "Store download")?;
    if result["schema_version"] != "vibapp.local-appstore-result.experimental-v1"
        || result["record"]["app"]["id"] != app_id
    { return Err("Store download returned an unexpected application".into()); }
    Ok(result)
}

fn read_daemon_state(data_dir: &Path) -> Result<Value, String> {
    let path = runtime_root(data_dir).join("state.json");
    let Ok(metadata) = fs::symlink_metadata(&path) else {
        return Ok(json!({"apps": {}}));
    };
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > MAX_STATE_BYTES
    {
        return Err("Runtime daemon 状态文件类型或大小无效。".to_string());
    }
    let value: Value = serde_json::from_slice(
        &fs::read(&path).map_err(|error| format!("无法读取 Runtime daemon 状态：{error}"))?,
    )
    .map_err(|error| format!("Runtime daemon 状态不是有效 JSON：{error}"))?;
    if value["schema_version"] != "vibapp.runtime-daemon.state.experimental-v1"
        || !value["apps"].is_object()
    {
        return Err("Runtime daemon 状态版本无效。".to_string());
    }
    Ok(value)
}

fn capability_labels(record: &Value) -> Vec<Value> {
    record["runtime"]["capabilities"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|item| {
            item.as_str()
                .map(str::to_owned)
                .or_else(|| item["interface"].as_str().map(str::to_owned))
        })
        .map(Value::String)
        .collect()
}

fn derive_host_presentation(
    kind: &str,
    display_name: &str,
    summary: &str,
) -> Result<Value, String> {
    if !matches!(kind, "ui" | "service" | "hybrid") {
        return Err("应用窗口提示含不支持的应用类型。".to_string());
    }
    let normalized = format!("{display_name}\n{summary}").to_lowercase();
    let compact = [
        "clock",
        "timer",
        "stopwatch",
        "counter",
        "calculator",
        "reminder",
        "时钟",
        "计时",
        "秒表",
        "倒计时",
        "计数器",
        "计算器",
        "提醒",
    ]
    .iter()
    .any(|marker| normalized.contains(marker));
    let wide = [
        "dashboard",
        "workspace",
        "editor",
        "studio",
        "inventory",
        "kanban",
        "看板",
        "工作台",
        "编辑器",
        "库存",
        "管理台",
    ]
    .iter()
    .any(|marker| normalized.contains(marker));
    let (preferred_width, preferred_height, minimum_width, minimum_height, resizable) =
        if kind == "service" {
            (720, 480, 480, 320, false)
        } else if compact {
            (520, 300, 420, 240, true)
        } else if wide {
            (1120, 760, 720, 480, true)
        } else if kind == "hybrid" {
            (1040, 720, 640, 420, true)
        } else {
            (900, 640, 480, 360, true)
        };
    Ok(json!({
        "schema_version": HOST_PRESENTATION_SCHEMA,
        "preferred_width": preferred_width,
        "preferred_height": preferred_height,
        "minimum_width": minimum_width,
        "minimum_height": minimum_height,
        "resizable": resizable,
    }))
}

fn host_presentation(
    value: Option<&Value>,
    kind: &str,
    display_name: &str,
    summary: &str,
) -> Result<Value, String> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return derive_host_presentation(kind, display_name, summary);
    };
    let object = value
        .as_object()
        .filter(|object| {
            object.len() == 6
                && [
                    "schema_version",
                    "preferred_width",
                    "preferred_height",
                    "minimum_width",
                    "minimum_height",
                    "resizable",
                ]
                .iter()
                .all(|field| object.contains_key(*field))
        })
        .ok_or_else(|| "应用窗口提示字段不完整。".to_string())?;
    if object["schema_version"] != HOST_PRESENTATION_SCHEMA {
        return Err("应用窗口提示版本不受支持。".to_string());
    }
    let bounded = |field: &str, minimum: u64, maximum: u64| {
        object[field]
            .as_u64()
            .filter(|value| (minimum..=maximum).contains(value))
            .ok_or_else(|| format!("应用窗口提示 {field} 超出宿主限制。"))
    };
    let preferred_width = bounded("preferred_width", 320, 1920)?;
    let preferred_height = bounded("preferred_height", 240, 1200)?;
    let minimum_width = bounded("minimum_width", 320, 1280)?;
    let minimum_height = bounded("minimum_height", 240, 960)?;
    if preferred_width < minimum_width || preferred_height < minimum_height {
        return Err("应用窗口首选尺寸小于最小尺寸。".to_string());
    }
    let resizable = object["resizable"]
        .as_bool()
        .ok_or_else(|| "应用窗口 resizable 提示不是布尔值。".to_string())?;
    Ok(json!({
        "schema_version": HOST_PRESENTATION_SCHEMA,
        "preferred_width": preferred_width,
        "preferred_height": preferred_height,
        "minimum_width": minimum_width,
        "minimum_height": minimum_height,
        "resizable": resizable,
    }))
}

fn store_item(record: &Value) -> Result<Value, String> {
    let app = record["app"]
        .as_object()
        .ok_or_else(|| "本地 AppStore 记录缺少应用身份。".to_string())?;
    let app_id = app
        .get("id")
        .and_then(Value::as_str)
        .filter(|value| valid_app_id(value))
        .ok_or_else(|| "本地 AppStore 记录含无效 app_id。".to_string())?;
    let package_digest = record["digests"]["package_sha256"]
        .as_str()
        .filter(|value| valid_digest(value))
        .ok_or_else(|| "本地 AppStore 记录含无效 package digest。".to_string())?;
    let component_digest = record["digests"]["component_sha256"]
        .as_str()
        .filter(|value| valid_digest(value))
        .ok_or_else(|| "本地 AppStore 记录含无效 Component digest。".to_string())?;
    if record["state"] != "private"
        || record["authority"] != json!({"install": "daemon", "publish": "none"})
    {
        return Err("本地 AppStore 记录没有私有 daemon 安装授权。".to_string());
    }
    let display_name = app
        .get("display_name")
        .and_then(Value::as_str)
        .unwrap_or(app_id);
    let kind = app.get("kind").and_then(Value::as_str).unwrap_or("ui");
    let summary = app
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("已由独立 verifier 验收的本地私有应用。");
    let presentation = host_presentation(record.get("presentation"), kind, display_name, summary)?;
    Ok(json!({
        "app_id": app_id,
        "display_name": display_name,
        "version": app.get("version").and_then(Value::as_str).unwrap_or("unknown"),
        "kind": kind,
        "summary": summary,
        "publisher": app.get("publisher").and_then(|value| value["display_name"].as_str()).unwrap_or("private"),
        "permissions": capability_labels(record),
        "presentation": presentation,
        "profiles": record["runtime"]["profiles"],
        "verification_state": "verified",
        "verification_summary": "独立 verifier 已通过；可交给本机 daemon 安装。",
        "publication_state": "private",
        "publication_badge": "private",
        "package_digest_sha256": package_digest,
        "component_sha256": component_digest,
        "candidate_state_contract": record["app_state"],
        "install_eligible": true,
        "launch_eligible": false,
        "installation_state": "candidate",
        "runtime_state": "not-installed",
        "service_entrypoints": record["runtime"]["entrypoints"].as_array().into_iter().flatten().filter(|item| item["kind"] == "service").cloned().collect::<Vec<_>>(),
        "active_service_entrypoints": [],
        "ecosystem_target": "vibapp-client",
        "surface_policy": {"renderer": "host-semantic-ui", "layout": "responsive"}
    }))
}

fn merge_installed(item: &mut Value, installed: &Value) -> Result<(), String> {
    let enabled = installed["enabled"] == true;
    let candidate_digest = item["package_digest_sha256"].clone();
    let candidate_version = item["version"].clone();
    let active_digest = installed["package_digest_sha256"].clone();
    let active_version = installed["version"].clone();
    let active_digest_text = active_digest
        .as_str()
        .filter(|value| valid_digest(value))
        .ok_or_else(|| "Runtime daemon 状态含无效 active package digest。".to_string())?;
    let active_component_digest = installed["component_sha256"]
        .as_str()
        .filter(|value| valid_digest(value))
        .ok_or_else(|| "Runtime daemon 状态含无效 active Component digest。".to_string())?;
    let display_name = installed["display_name"]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "Runtime daemon 状态缺少 active display name。".to_string())?;
    let kind = installed["kind"]
        .as_str()
        .filter(|value| matches!(*value, "ui" | "service" | "hybrid"))
        .ok_or_else(|| "Runtime daemon 状态含无效 active app kind。".to_string())?;
    let publisher = installed["publisher_display_name"]
        .as_str()
        .or_else(|| installed["publisher_id"].as_str())
        .unwrap_or("private");
    let permissions = match installed.get("permissions") {
        None | Some(Value::Null) => json!([]),
        Some(Value::Array(items)) if items.iter().all(Value::is_string) => {
            Value::Array(items.clone())
        }
        _ => return Err("Runtime daemon 状态含无效 active permissions。".to_string()),
    };
    let presentation = host_presentation(installed.get("presentation"), kind, display_name, "")?;
    let update_eligible = candidate_digest
        .as_str()
        .zip(active_digest.as_str())
        .is_some_and(|(candidate, active)| valid_digest(candidate) && candidate != active);
    item["install_eligible"] = json!(false);
    item["launch_eligible"] = json!(
        enabled
            && installed["ui_entrypoints"]
                .as_array()
                .is_some_and(|entries| !entries.is_empty())
    );
    item["installation_state"] = json!(if enabled {
        "installed"
    } else {
        "installed-disabled"
    });
    item["runtime_state"] = json!(installed["lifecycle_state"].as_str().unwrap_or("installed"));
    // Candidate records may describe a pending update. Host-owned identity and
    // presentation must instead stay bound to the daemon's active package.
    item["display_name"] = json!(display_name);
    item["kind"] = json!(kind);
    item["publisher"] = json!(publisher);
    item["permissions"] = permissions;
    item["presentation"] = presentation;
    item["active_package_digest_sha256"] = json!(active_digest_text);
    item["package_digest_sha256"] = active_digest;
    item["component_sha256"] = json!(active_component_digest);
    item["version"] = active_version;
    item["update_eligible"] = json!(update_eligible);
    item["available_update"] = if update_eligible {
        json!({
            "version": candidate_version,
            "package_digest_sha256": candidate_digest,
        })
    } else {
        Value::Null
    };
    item["state"] = installed["state"].clone();
    item["update"] = installed["update"].clone();
    item["enabled"] = json!(enabled);
    item["service_entrypoints"] = installed["service_entrypoints"].clone();
    item["ui_entrypoints"] = installed["ui_entrypoints"].clone();
    item["active_service_entrypoints"] = json!(
        installed["services"]
            .as_object()
            .into_iter()
            .flat_map(|services| services.iter())
            .filter(|(_, service)| service["state"] == "running")
            .map(|(entrypoint, _)| Value::String(entrypoint.clone()))
            .collect::<Vec<_>>()
    );
    item["service_statuses"] = installed["services"].clone();
    item["guest_execution_performed"] = installed["guest_execution_performed"].clone();
    Ok(())
}

pub fn catalog(data_dir: &Path) -> Result<Vec<Value>, String> {
    let listed = appstore_command(data_dir, &["list", "--limit", "100"])?;
    let records = listed["items"]
        .as_array()
        .ok_or_else(|| "本地 AppStore list 结果缺少 items。".to_string())?;
    let mut items = BTreeMap::<String, Value>::new();
    for record in records {
        let item = store_item(record)?;
        let app_id = item["app_id"].as_str().unwrap_or_default().to_string();
        items.insert(app_id, item);
    }
    let daemon = read_daemon_state(data_dir)?;
    for (app_id, installed) in daemon["apps"].as_object().into_iter().flatten() {
        if !valid_app_id(app_id) {
            return Err("Runtime daemon 状态含无效 app_id。".to_string());
        }
        let item = items.entry(app_id.clone()).or_insert_with(|| {
            json!({
                "app_id": app_id,
                "display_name": installed["display_name"].as_str().unwrap_or(app_id),
                "version": installed["version"].as_str().unwrap_or("unknown"),
                "kind": installed["kind"].as_str().unwrap_or("ui"),
                "summary": "已安装到本机 daemon 的私有 VibApp 应用。",
                "publisher": installed["publisher_id"].as_str().unwrap_or("private"),
                "permissions": [],
                "verification_state": "verified",
                "verification_summary": "安装包由 daemon 重新校验并以私有模式保存。",
                "publication_state": "private",
                "publication_badge": "private",
                "package_digest_sha256": installed["package_digest_sha256"],
                "component_sha256": installed["component_sha256"],
                "ecosystem_target": "vibapp-client"
            })
        });
        merge_installed(item, installed)?;
    }
    Ok(items.into_values().collect())
}

#[cfg(unix)]
fn daemon_is_active(path: &Path) -> bool {
    std::os::unix::net::UnixStream::connect(path).is_ok()
}

#[cfg(not(unix))]
fn daemon_is_active(_path: &Path) -> bool {
    false
}

#[cfg(unix)]
fn ensure_daemon(data_dir: &Path) -> Result<(), String> {
    let socket = native_platform::unix_daemon_socket(data_dir)?;
    if daemon_is_active(&socket) {
        return Ok(());
    }
    let module_root = daemon_module_root()?;
    fs::create_dir_all(store_root(data_dir).join("candidates"))
        .map_err(|error| format!("无法创建本地 AppStore candidate 目录：{error}"))?;
    let runtime = runtime_root(data_dir);
    let mut command = Command::new(native_platform::python_executable(false)?);
    command
        .arg("-I")
        .arg("-B")
        .arg("-c")
        .arg("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')")
        .arg(&module_root)
        .arg("serve")
        .arg("--root")
        .arg(&runtime)
        .arg("--promotion-root")
        .arg(store_root(data_dir).join("candidates"))
        .arg("--socket")
        .arg(&socket)
        .env_clear()
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command.env("VIBAPP_SERVICE_RUNTIME_BIN", service_runtime_binary()?);
    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动 Runtime daemon：{error}"))?;
    let deadline = SystemTime::now() + DAEMON_START_TIMEOUT;
    while SystemTime::now() < deadline {
        if daemon_is_active(&socket) {
            thread::spawn(move || {
                let _ = child.wait();
            });
            return Ok(());
        }
        if child.try_wait().is_ok_and(|status| status.is_some()) {
            return Err("Runtime daemon 启动后立即退出。".to_string());
        }
        thread::sleep(Duration::from_millis(20));
    }
    let _ = child.kill();
    let _ = child.wait();
    Err("Runtime daemon 未在 5 秒内就绪。".to_string())
}

#[cfg(not(unix))]
fn ensure_daemon(_data_dir: &Path) -> Result<(), String> {
    native_platform::unix_daemon_socket(_data_dir).map(|_| ())
}

fn daemon_command(
    data_dir: &Path,
    tag: &str,
    subject: Option<&str>,
    value: Value,
    promotion_record: Option<&Path>,
) -> Result<Value, String> {
    ensure_daemon(data_dir)?;
    let request_suffix = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let envelope = json!({
        "request_id": format!("desktop-{tag}-{request_suffix:x}"),
        "idempotency_key": format!("desktop-{tag}-{request_suffix:x}"),
        "client": "launcher",
        "subject": subject,
        "command": {"tag": tag, "value": value}
    });
    let bytes =
        serde_json::to_vec(&envelope).map_err(|error| format!("无法编码 daemon 请求：{error}"))?;
    let module_root = daemon_module_root()?;
    let mut command = Command::new(native_platform::python_executable(false)?);
    command
        .arg("-I")
        .arg("-B")
        .arg("-c")
        .arg("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')")
        .arg(&module_root)
        .arg("ctl")
        .arg("--socket")
        .arg(native_platform::unix_daemon_socket(data_dir)?)
        .arg("--envelope")
        .arg("-");
    if let Some(record) = promotion_record {
        command.arg("--promotion-record").arg(record);
    }
    let mut child = command
        .env_clear()
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法连接 Runtime daemon：{error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "无法打开 Runtime daemon 输入。".to_string())?
        .write_all(&bytes)
        .map_err(|error| format!("无法发送 Runtime daemon 请求：{error}"))?;
    let output = wait_for_output(child, COMMAND_TIMEOUT)?;
    if output.stdout.is_empty()
        || output.stdout.len() > MAX_REPLY_BYTES
        || output.stderr.len() > MAX_REPLY_BYTES
    {
        return Err("Runtime daemon 返回大小无效。".to_string());
    }
    let response: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("Runtime daemon 返回了无效 JSON：{error}"))?;
    if let Some(error) = response.get("error") {
        return Err(format!(
            "{}: {}",
            error["code"].as_str().unwrap_or("daemon-failed"),
            error["message"]
                .as_str()
                .unwrap_or("Runtime daemon 拒绝操作")
        ));
    }
    if !output.status.success() {
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        return Err(format!("Runtime daemon 命令失败：{}", diagnostic.trim()));
    }
    let expected_outcome = match tag {
        "launch" => "launched",
        "ui-action" | "ui-refresh" => "ui-updated",
        "status" => "status",
        _ => "accepted",
    };
    if response["outcome"]["tag"] != expected_outcome {
        return Err(format!(
            "Runtime daemon 没有返回预期结果 {expected_outcome}。"
        ));
    }
    Ok(response)
}

fn candidate_record(
    data_dir: &Path,
    app_id: &str,
    package_digest: &str,
) -> Result<PathBuf, String> {
    if !valid_app_id(app_id) || !valid_digest(package_digest) {
        return Err("应用目标身份或摘要无效。".to_string());
    }
    let detail = appstore_command(data_dir, &["detail", package_digest])?;
    let record = &detail["record"];
    if record["app"]["id"] != app_id
        || record["digests"]["package_sha256"] != package_digest
        || record["authority"] != json!({"install": "daemon", "publish": "none"})
        || record["state"] != "private"
    {
        return Err("本地 AppStore detail 没有绑定所选私有应用。".to_string());
    }
    let relative = record["paths"]["candidate"]
        .as_str()
        .ok_or_else(|| "本地 AppStore detail 缺少 candidate 路径。".to_string())?;
    let root = fs::canonicalize(store_root(data_dir))
        .map_err(|error| format!("无法解析本地 AppStore 根目录：{error}"))?;
    let candidate = fs::canonicalize(root.join(relative))
        .map_err(|error| format!("无法解析 verifier candidate：{error}"))?;
    if !candidate.starts_with(&root) || !candidate.is_file() || candidate.is_symlink() {
        return Err("verifier candidate 路径逃逸或文件类型无效。".to_string());
    }
    Ok(candidate)
}

pub fn install(data_dir: &Path, app_id: &str, package_digest: &str) -> Result<Value, String> {
    let candidate = candidate_record(data_dir, app_id, package_digest)?;
    let response = daemon_command(
        data_dir,
        "install",
        None,
        json!({"package_digest_sha256": package_digest, "enable_after_install": false}),
        Some(&candidate),
    )?;
    Ok(json!({
        "status": "installed-disabled",
        "installation_performed": true,
        "next_authority": "runtime-daemon",
        "app_id": app_id,
        "package_digest_sha256": package_digest,
        "daemon": response
    }))
}

pub fn update(data_dir: &Path, app_id: &str, package_digest: &str) -> Result<Value, String> {
    let candidate = candidate_record(data_dir, app_id, package_digest)?;
    let response = daemon_command(
        data_dir,
        "update",
        Some(app_id),
        json!({"package_digest_sha256": package_digest}),
        Some(&candidate),
    )?;
    match daemon_command(data_dir, "status", Some(app_id), Value::Null, None) {
        Ok(status_response) => {
            let status = &status_response["outcome"]["value"];
            Ok(json!({
                "status": status["update"]["status"],
                "update_performed": true,
                "next_authority": "runtime-daemon",
                "app_id": app_id,
                "package_digest_sha256": package_digest,
                "state": status["state"],
                "update": status["update"],
                "daemon": response,
                "daemon_status": status_response
            }))
        }
        Err(error) => Ok(json!({
            "status": "status-unavailable",
            "update_performed": true,
            "next_authority": "runtime-daemon",
            "app_id": app_id,
            "package_digest_sha256": package_digest,
            "state": null,
            "update": null,
            "status_error": error,
            "daemon": response
        })),
    }
}

pub fn lifecycle(data_dir: &Path, input: LifecycleInput) -> Result<Value, String> {
    if !valid_app_id(&input.app_id) {
        return Err("app_id 格式无效。".to_string());
    }
    if input.action == "update" {
        if input.entrypoint.is_some()
            || input.disposition.is_some()
            || input.session.is_some()
            || input.surface.is_some()
            || input.trigger_id.is_some()
            || input.payload.is_some()
        {
            return Err("更新操作包含了不适用的字段。".to_string());
        }
        let package_digest = input
            .package_digest_sha256
            .as_deref()
            .filter(|value| valid_digest(value))
            .ok_or_else(|| "更新操作缺少精确 package digest。".to_string())?;
        return update(data_dir, &input.app_id, package_digest);
    }
    let (value, allowed_fields) = match input.action.as_str() {
        "enable" | "disable" | "status" => (Value::Null, "none"),
        "service-start" | "service-stop" | "service-health" => {
            let entrypoint = input
                .entrypoint
                .as_deref()
                .filter(|value| {
                    !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)
                })
                .ok_or_else(|| "服务操作缺少有效 entrypoint。".to_string())?;
            (json!({"entrypoint": entrypoint}), "entrypoint")
        }
        "service-trigger" => {
            let entrypoint = input
                .entrypoint
                .as_deref()
                .filter(|value| {
                    !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)
                })
                .ok_or_else(|| "服务触发缺少有效 entrypoint。".to_string())?;
            let trigger_id = input
                .trigger_id
                .as_deref()
                .filter(|value| {
                    !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)
                })
                .ok_or_else(|| "服务触发缺少有效 trigger_id。".to_string())?;
            let payload = input.payload.as_deref().unwrap_or_default();
            if payload.len() > 64 * 1024 {
                return Err("服务触发 payload 超过 64 KiB 上限。".to_string());
            }
            (
                json!({"entrypoint": entrypoint, "trigger_id": trigger_id, "payload": payload}),
                "trigger",
            )
        }
        "surface-close" => {
            let bounded = |value: Option<&str>, label: &str| {
                value
                    .filter(|value| {
                        !value.is_empty()
                            && value.len() <= 256
                            && !value.chars().any(char::is_control)
                    })
                    .map(str::to_owned)
                    .ok_or_else(|| format!("关闭应用表面缺少有效 {label}。"))
            };
            let session = bounded(input.session.as_deref(), "session")?;
            let surface = bounded(input.surface.as_deref(), "surface")?;
            (json!({"session": session, "surface": surface}), "surface")
        }
        "uninstall" => {
            let disposition = input.disposition.as_deref().unwrap_or("retain");
            if !matches!(disposition, "delete" | "retain" | "export-then-delete") {
                return Err("卸载数据处置选项无效。".to_string());
            }
            (json!({"disposition": disposition}), "disposition")
        }
        _ => return Err("不支持的应用生命周期操作。".to_string()),
    };
    if (!matches!(allowed_fields, "entrypoint" | "trigger") && input.entrypoint.is_some())
        || (allowed_fields != "disposition" && input.disposition.is_some())
        || (allowed_fields != "surface" && (input.session.is_some() || input.surface.is_some()))
        || (allowed_fields != "trigger" && (input.trigger_id.is_some() || input.payload.is_some()))
        || input.package_digest_sha256.is_some()
    {
        return Err("生命周期操作包含了不适用的字段。".to_string());
    }
    daemon_command(data_dir, &input.action, Some(&input.app_id), value, None)
}

pub fn is_installed(data_dir: &Path, app_id: &str) -> Result<bool, String> {
    if !valid_app_id(app_id) {
        return Err("app_id 格式无效。".to_string());
    }
    Ok(read_daemon_state(data_dir)?["apps"].get(app_id).is_some())
}

pub fn authorize_launch(data_dir: &Path, app_id: &str) -> Result<Value, String> {
    let state = read_daemon_state(data_dir)?;
    let app = state["apps"]
        .get(app_id)
        .ok_or_else(|| "Runtime daemon 中没有这个已安装应用。".to_string())?;
    if app["enabled"] != true {
        return Err("应用已安装但尚未启用。".to_string());
    }
    let entrypoint = app["ui_entrypoints"]
        .as_array()
        .and_then(|entries| entries.first())
        .and_then(|entry| entry["id"].as_str())
        .ok_or_else(|| "这个应用没有可启动的 UI entrypoint。".to_string())?;
    daemon_command(
        data_dir,
        "launch",
        Some(app_id),
        json!({"entrypoint": entrypoint, "route": null}),
        None,
    )
}

pub fn dispatch_ui_action(data_dir: &Path, input: UiActionInput) -> Result<Value, String> {
    if !valid_app_id(&input.app_id)
        || !valid_digest(&input.package_digest_sha256)
        || !valid_digest(&input.component_sha256)
        || input.fields.len() > 256
    {
        return Err("UI action 的应用身份、摘要或字段数量无效。".to_string());
    }
    daemon_command(
        data_dir,
        "ui-action",
        Some(&input.app_id),
        json!({
            "entrypoint": input.entrypoint,
            "package_digest_sha256": input.package_digest_sha256,
            "component_sha256": input.component_sha256,
            "generation": input.generation,
            "session": input.session,
            "surface": input.surface,
            "route": input.route,
            "action": input.action,
            "event_id": input.event_id,
            "fields": input.fields,
        }),
        None,
    )
}

pub fn refresh_ui_surface(data_dir: &Path, input: UiRefreshInput) -> Result<Value, String> {
    if !valid_app_id(&input.app_id)
        || !valid_digest(&input.package_digest_sha256)
        || !valid_digest(&input.component_sha256)
    {
        return Err("UI refresh 的应用身份或摘要无效。".to_string());
    }
    daemon_command(
        data_dir,
        "ui-refresh",
        Some(&input.app_id),
        json!({
            "entrypoint": input.entrypoint,
            "package_digest_sha256": input.package_digest_sha256,
            "component_sha256": input.component_sha256,
            "generation": input.generation,
            "session": input.session,
            "surface": input.surface,
            "route": input.route,
            "event_id": input.event_id,
        }),
        None,
    )
}

pub fn report_ui_render_failure(
    data_dir: &Path,
    input: UiRenderFailureInput,
) -> Result<Value, String> {
    if !valid_app_id(&input.app_id)
        || !valid_digest(&input.package_digest_sha256)
        || !valid_digest(&input.component_sha256)
    {
        return Err("UI failure report 的应用身份或摘要无效。".to_string());
    }
    daemon_command(
        data_dir,
        "ui-render-failure",
        Some(&input.app_id),
        json!({
            "entrypoint": input.entrypoint,
            "package_digest_sha256": input.package_digest_sha256,
            "component_sha256": input.component_sha256,
            "generation": input.generation,
            "session": input.session,
            "surface": input.surface,
            "route": input.route,
            "event_id": input.event_id,
            "render_failure_token": input.render_failure_token,
            "reason": input.reason,
        }),
        None,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    struct TestDaemon(std::process::Child);

    #[cfg(unix)]
    impl Drop for TestDaemon {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    fn test_root(label: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        Path::new("/tmp").join(format!("vibapp-{label}-{suffix:x}"))
    }

    #[test]
    fn identity_validation_rejects_path_confusion() {
        for value in ["../bad", "Ai.vibapp.bad", "ai..bad", "ai/bad"] {
            assert!(!valid_app_id(value));
        }
        assert!(valid_app_id("ai.vibapp.good-app"));
        assert!(valid_digest(&"a".repeat(64)));
        assert!(!valid_digest(&"A".repeat(64)));
    }

    #[test]
    fn store_item_is_private_verified_and_installable() {
        let record = json!({
            "state": "private",
            "authority": {"install": "daemon", "publish": "none"},
            "app": {"id": "ai.vibapp.test", "version": "0.1.0", "kind": "hybrid", "display_name": "Test", "description": "Synthetic", "publisher": {"display_name": "Tests"}},
            "runtime": {"profiles": [], "entrypoints": [{"id": "main-ui", "kind": "launcher-ui"}, {"id": "main-service", "kind": "service"}], "capabilities": [{"interface": "clock"}]},
            "digests": {"package_sha256": "a".repeat(64), "component_sha256": "b".repeat(64)}
        });
        let item = store_item(&record).unwrap();
        assert_eq!(item["verification_state"], "verified");
        assert_eq!(item["install_eligible"], true);
        assert_eq!(item["launch_eligible"], false);
        assert_eq!(item["permissions"], json!(["clock"]));
        assert_eq!(item["presentation"]["preferred_width"], 1040);
        assert_eq!(item["presentation"]["preferred_height"], 720);
        assert_eq!(item["service_entrypoints"][0]["id"], "main-service");
    }

    #[test]
    fn legacy_clock_gets_a_compact_host_owned_window_hint() {
        let presentation = host_presentation(None, "ui", "LED 数字时钟", "").unwrap();
        assert_eq!(presentation["schema_version"], HOST_PRESENTATION_SCHEMA);
        assert_eq!(presentation["preferred_width"], 520);
        assert_eq!(presentation["preferred_height"], 300);
        assert_eq!(presentation["minimum_width"], 420);
        assert_eq!(presentation["minimum_height"], 240);
        assert_eq!(presentation["resizable"], true);
    }

    #[test]
    fn installed_state_changes_real_lifecycle_actions() {
        let mut item = json!({
            "install_eligible": true,
            "launch_eligible": false,
            "display_name": "Pending update name",
            "kind": "hybrid",
            "publisher": "Pending publisher",
            "permissions": ["pending:authority"],
            "presentation": {
                "schema_version": HOST_PRESENTATION_SCHEMA,
                "preferred_width": 1120,
                "preferred_height": 760,
                "minimum_width": 720,
                "minimum_height": 480,
                "resizable": true
            },
            "version": "0.2.0",
            "package_digest_sha256": "c".repeat(64),
            "component_sha256": "d".repeat(64)
        });
        let installed = json!({
            "enabled": true,
            "display_name": "Active LED 数字时钟",
            "kind": "ui",
            "publisher_id": "ai.vibapp.active",
            "publisher_display_name": "Active Publisher",
            "permissions": ["vibapp:experimental/clock"],
            "presentation": {
                "schema_version": HOST_PRESENTATION_SCHEMA,
                "preferred_width": 520,
                "preferred_height": 300,
                "minimum_width": 420,
                "minimum_height": 240,
                "resizable": true
            },
            "version": "0.1.0",
            "package_digest_sha256": "a".repeat(64),
            "component_sha256": "b".repeat(64),
            "lifecycle_state": "enabled",
            "ui_entrypoints": [{"id": "main-ui"}],
            "service_entrypoints": [{"id": "main-service"}],
            "services": {"main-service": {"state": "running"}},
            "guest_execution_performed": false,
            "state": {"schema_version": "vibapp.app-state-routing.experimental-v1", "schema": 1, "revision": 1},
            "update": {"schema_version": "vibapp.update-transaction.experimental-v1", "status": "none"}
        });
        merge_installed(&mut item, &installed).unwrap();
        assert_eq!(item["installation_state"], "installed");
        assert_eq!(item["launch_eligible"], true);
        assert_eq!(item["active_service_entrypoints"], json!(["main-service"]));
        assert_eq!(item["guest_execution_performed"], false);
        assert_eq!(item["update_eligible"], true);
        assert_eq!(item["package_digest_sha256"], "a".repeat(64));
        assert_eq!(item["active_package_digest_sha256"], "a".repeat(64));
        assert_eq!(item["component_sha256"], "b".repeat(64));
        assert_eq!(item["display_name"], "Active LED 数字时钟");
        assert_eq!(item["kind"], "ui");
        assert_eq!(item["publisher"], "Active Publisher");
        assert_eq!(item["permissions"], json!(["vibapp:experimental/clock"]));
        assert_eq!(item["presentation"]["preferred_width"], 520);
        assert_eq!(
            item["available_update"]["package_digest_sha256"],
            "c".repeat(64)
        );
        assert_eq!(item["state"]["revision"], 1);
        assert_eq!(item["update"]["status"], "none");
    }

    #[test]
    fn update_lifecycle_input_requires_an_exact_digest_and_accepts_camel_alias() {
        let expected_digest = "d".repeat(64);
        let input: LifecycleInput = serde_json::from_value(json!({
            "app_id": "ai.vibapp.test",
            "action": "update",
            "packageDigestSha256": expected_digest.clone()
        }))
        .unwrap();
        assert_eq!(
            input.package_digest_sha256.as_deref(),
            Some(expected_digest.as_str())
        );

        let rejected = lifecycle(
            Path::new("/definitely-not-used"),
            LifecycleInput {
                app_id: "ai.vibapp.test".to_string(),
                action: "update".to_string(),
                entrypoint: None,
                disposition: None,
                session: None,
                surface: None,
                trigger_id: None,
                payload: None,
                package_digest_sha256: None,
            },
        )
        .unwrap_err();
        assert!(rejected.contains("精确 package digest"));
    }

    #[test]
    fn ui_action_accepts_direct_camel_case_binding() {
        let input: UiActionInput = serde_json::from_value(json!({
            "appId": "ai.vibapp.hello",
            "entrypoint": "main-ui",
            "packageDigestSha256": "a".repeat(64),
            "componentSha256": "b".repeat(64),
            "generation": "gen:1",
            "session": "session:1",
            "surface": "surface:1",
            "route": "home",
            "action": "save",
            "eventId": "event:1",
            "fields": [{"field": "name", "value": {"tag": "text", "value": "Ada"}}],
        }))
        .unwrap();
        assert_eq!(input.app_id, "ai.vibapp.hello");
        assert_eq!(input.event_id, "event:1");
        assert_eq!(input.fields.len(), 1);
    }

    #[test]
    fn ui_refresh_accepts_direct_camel_case_binding_without_render_token() {
        let input: UiRefreshInput = serde_json::from_value(json!({
            "appId": "ai.vibapp.hello",
            "entrypoint": "main-ui",
            "packageDigestSha256": "a".repeat(64),
            "componentSha256": "b".repeat(64),
            "generation": "gen:1",
            "session": "session:1",
            "surface": "surface:1",
            "route": "home",
            "eventId": "refresh:1",
        }))
        .unwrap();
        assert_eq!(input.app_id, "ai.vibapp.hello");
        assert_eq!(input.event_id, "refresh:1");
        assert_eq!(input.generation, "gen:1");
    }

    #[cfg(unix)]
    #[test]
    fn daemon_rejection_preserves_the_structured_failure_reason() {
        use std::os::unix::process::ExitStatusExt;

        let rejected = parse_output(
            std::process::Output {
                status: std::process::ExitStatus::from_raw(1 << 8),
                stdout: serde_json::to_vec(&json!({
                    "error": {
                        "code": "incompatible-contract",
                        "message": "candidate cannot migrate the installed state schema",
                        "retryable": false
                    }
                }))
                .unwrap(),
                stderr: Vec::new(),
            },
            "Runtime daemon",
        )
        .unwrap_err();
        assert!(rejected.contains("incompatible-contract"));
        assert!(rejected.contains("cannot migrate"));
    }

    #[cfg(unix)]
    #[test]
    fn real_appstore_candidate_reaches_daemon_lifecycle() {
        let data_dir = test_root("vertical");
        fs::create_dir_all(&data_dir).unwrap();
        let candidate = Path::new(env!("CARGO_MANIFEST_DIR")).join(
            "../../app-builder/demo-output/pipeline/candidates/dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674/candidate.json",
        );
        assert!(candidate.is_file(), "accepted Builder fixture must exist");
        let ingested = ingest(&data_dir, candidate.to_str().unwrap()).unwrap();
        let package_digest = ingested["record"]["digests"]["package_sha256"]
            .as_str()
            .unwrap()
            .to_string();

        let module_root = daemon_module_root().unwrap();
        let socket = native_platform::unix_daemon_socket(&data_dir).unwrap();
        let child = Command::new(native_platform::python_executable(false).unwrap())
            .arg("-I")
            .arg("-B")
            .arg("-c")
            .arg("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')")
            .arg(&module_root)
            .arg("serve")
            .arg("--root")
            .arg(runtime_root(&data_dir))
            .arg("--promotion-root")
            .arg(store_root(&data_dir).join("candidates"))
            .arg("--socket")
            .arg(&socket)
            .env_clear()
            .env("PATH", native_platform::safe_path().unwrap())
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("TZ", "UTC")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env(
                "VIBAPP_SERVICE_RUNTIME_BIN",
                service_runtime_binary().unwrap(),
            )
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let daemon = TestDaemon(child);
        let deadline = SystemTime::now() + DAEMON_START_TIMEOUT;
        while SystemTime::now() < deadline && !daemon_is_active(&socket) {
            thread::sleep(Duration::from_millis(20));
        }
        assert!(daemon_is_active(&socket), "test daemon must become ready");

        let installed = install(&data_dir, "ai.vibapp.hello", &package_digest).unwrap();
        assert_eq!(installed["installation_performed"], true);
        assert_eq!(installed["status"], "installed-disabled");
        lifecycle(
            &data_dir,
            LifecycleInput {
                app_id: "ai.vibapp.hello".to_string(),
                action: "enable".to_string(),
                entrypoint: None,
                disposition: None,
                session: None,
                surface: None,
                trigger_id: None,
                payload: None,
                package_digest_sha256: None,
            },
        )
        .unwrap();
        let surface = authorize_launch(&data_dir, "ai.vibapp.hello").unwrap();
        assert_eq!(surface["outcome"]["tag"], "launched");
        assert_eq!(surface["outcome"]["value"]["route"], "home");
        let session = surface["outcome"]["value"]["session"]
            .as_str()
            .unwrap()
            .to_string();
        let surface_id = surface["outcome"]["value"]["surface"]
            .as_str()
            .unwrap()
            .to_string();
        lifecycle(
            &data_dir,
            LifecycleInput {
                app_id: "ai.vibapp.hello".to_string(),
                action: "surface-close".to_string(),
                entrypoint: None,
                disposition: None,
                session: Some(session),
                surface: Some(surface_id),
                trigger_id: None,
                payload: None,
                package_digest_sha256: None,
            },
        )
        .unwrap();
        let apps = catalog(&data_dir).unwrap();
        let app = apps
            .iter()
            .find(|item| item["app_id"] == "ai.vibapp.hello")
            .unwrap();
        assert_eq!(app["installation_state"], "installed");
        assert_eq!(app["launch_eligible"], true);
        assert_eq!(app["guest_execution_performed"], true);
        lifecycle(
            &data_dir,
            LifecycleInput {
                app_id: "ai.vibapp.hello".to_string(),
                action: "disable".to_string(),
                entrypoint: None,
                disposition: None,
                session: None,
                surface: None,
                trigger_id: None,
                payload: None,
                package_digest_sha256: None,
            },
        )
        .unwrap();
        lifecycle(
            &data_dir,
            LifecycleInput {
                app_id: "ai.vibapp.hello".to_string(),
                action: "uninstall".to_string(),
                entrypoint: None,
                disposition: Some("retain".to_string()),
                session: None,
                surface: None,
                trigger_id: None,
                payload: None,
                package_digest_sha256: None,
            },
        )
        .unwrap();
        drop(daemon);
        fs::remove_dir_all(&data_dir).unwrap();
    }
}
