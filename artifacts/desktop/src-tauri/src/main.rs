#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64_STANDARD};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State};
use uuid::{Uuid, Variant};

mod app_runtime;
mod app_window;
mod codeagent_settings;
mod delivery_integration;
mod deep_link;
mod local_orchestrator;
mod local_product;
mod model_settings;
mod native_platform;
mod network_settings;
mod public_registry_sync;
mod registry_integration;
mod roomhash_host;
mod startup;

#[derive(Default)]
struct NetworkNodeState {
    inner: Mutex<NetworkNodeInner>,
}

#[derive(Default)]
struct NetworkNodeInner {
    host: Option<roomhash_host::RoomHashHost>,
    last_error: Option<String>,
    collaboration_sessions: HashMap<String, CollaborationSessionBinding>,
    package_fetch_in_progress: bool,
}

#[derive(Clone)]
struct CollaborationSessionBinding {
    app_id: String,
    channel_id: String,
    expires_at: u64,
    received_event_ids: VecDeque<String>,
}

#[cfg(test)]
const DEMO_FIXTURE: &str = include_str!("../../fixtures/state.json");
const MAX_OUTBOX_BYTES: u64 = 2 * 1024 * 1024;
include!(concat!(env!("OUT_DIR"), "/desktop_build_input_receipt.rs"));

#[derive(Deserialize)]
struct NeedInput {
    title: String,
    description: String,
    #[serde(default)]
    #[serde(alias = "embeddingConsent")]
    embedding_consent: bool,
    #[serde(default)]
    #[serde(alias = "retryTaskId")]
    retry_task_id: Option<String>,
    #[serde(default)]
    #[serde(alias = "needId")]
    need_id: Option<String>,
}

#[derive(Deserialize)]
struct InstallInput {
    app_id: String,
    package_digest_sha256: String,
}

#[derive(Deserialize)]
struct ImportCandidateInput {
    candidate_path: String,
}

#[derive(Deserialize)]
struct DevelopmentSubmitInput {
    task: Value,
    registry: Value,
    #[serde(alias = "explicitUserSubmit")]
    explicit_user_submit: bool,
    #[serde(default)]
    #[serde(alias = "acknowledgeExternalCost")]
    acknowledge_external_cost: bool,
    #[serde(default)]
    #[serde(alias = "retryTaskId")]
    retry_task_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct CreateCollaborationInput {
    app_id: String,
    confirmed: bool,
    expires_in_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct JoinCollaborationInput {
    app_id: String,
    channel_id: String,
    confirmed: bool,
    expires_in_ms: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct LeaveCollaborationInput {
    session_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct SendCollaborationInput {
    session_id: String,
    payload: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct ReceiveCollaborationInput {
    session_id: String,
    limit: u64,
}

fn unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn unix_ms_u64() -> u64 {
    unix_ms().try_into().unwrap_or(u64::MAX)
}

fn collaboration_uuid(value: &str, label: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value).map_err(|_| format!("{label} 不是有效 UUID。"))?;
    if parsed.is_nil()
        || parsed.get_variant() != Variant::RFC4122
        || !matches!(parsed.get_version_num(), 1..=5)
    {
        return Err(format!("{label} 必须是非零 RFC 4122 UUID。"));
    }
    Ok(parsed.to_string())
}

fn collaboration_app_id(value: &str) -> Result<String, String> {
    let valid = !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        });
    if !valid {
        return Err("协作应用 ID 格式无效。".to_string());
    }
    Ok(value.to_string())
}

fn collaboration_expiry(expires_in_ms: u64) -> Result<u64, String> {
    const MIN_TTL_MS: u64 = 60 * 1_000;
    const MAX_TTL_MS: u64 = 24 * 60 * 60 * 1_000;
    if !(MIN_TTL_MS..=MAX_TTL_MS).contains(&expires_in_ms) {
        return Err("协作频道有效期必须在 1 分钟到 24 小时之间。".to_string());
    }
    unix_ms_u64()
        .checked_add(expires_in_ms)
        .ok_or_else(|| "协作频道有效期溢出。".to_string())
}

fn collaboration_grant(operation: &str, channel_id: &str, expires_at: u64) -> Value {
    json!({
        "schema": "vibapp.collaboration-grant.experimental-v1",
        "grant_id": Uuid::new_v4().to_string(),
        "approved": true,
        "operation": operation,
        "channel_id": channel_id,
        "expires_at": expires_at,
    })
}

fn app_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let path = match app.try_state::<startup::StartupOptions>().and_then(|options| options.data_dir.clone()) {
        Some(path) => path,
        None => app.path().app_data_dir().map_err(|error| format!("无法确定客户端数据目录：{error}"))?,
    };
    fs::create_dir_all(path.join("outbox"))
        .map_err(|error| format!("无法创建客户端数据目录：{error}"))?;
    fs::create_dir_all(path.join("history"))
        .map_err(|error| format!("无法创建需求历史目录：{error}"))?;
    fs::create_dir_all(path.join("registry-requests"))
        .map_err(|error| format!("无法创建 Registry 请求目录：{error}"))?;
    Ok(path)
}

fn read_jsonl(path: &Path) -> Vec<Value> {
    let Ok(metadata) = path.metadata() else {
        return Vec::new();
    };
    if metadata.len() > MAX_OUTBOX_BYTES {
        return Vec::new();
    }
    let Ok(content) = fs::read_to_string(path) else {
        return Vec::new();
    };
    content
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .take(200)
        .collect()
}

fn append_jsonl(path: &Path, value: &Value) -> Result<(), String> {
    if path.metadata().map(|item| item.len()).unwrap_or(0) > MAX_OUTBOX_BYTES {
        return Err("本地任务箱已达到 2 MiB 上限，请先归档。".to_string());
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("无法打开本地任务箱：{error}"))?;
    serde_json::to_writer(&mut file, value).map_err(|error| format!("无法写入任务：{error}"))?;
    file.write_all(b"\n")
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法持久化任务：{error}"))
}

fn default_state() -> Value {
    json!({
        "meta": {
            "schema_version": "vibapp.desktop-state.experimental.v1",
            "label": "VibApp Desktop product state",
            "source": "product-default",
            "demo_mode": false,
            "generated_source_present": false,
            "build_history_present": false,
            "candidate_present": false,
            "acceptance_history_present": false,
            "launch_available": false
        },
        "needs": [],
        "jobs": [],
        "apps": [],
        "install_intents": [],
        "feed_errors": []
    })
}

#[tauri::command]
fn get_state(app: AppHandle) -> Result<Value, String> {
    let mut state = default_state();
    let data_dir = app_data_dir(&app)?;
    let codeagent = codeagent_settings::get(&data_dir)?;

    let mut feed_errors = Vec::new();
    let mut discovered_apps = match public_registry_sync::sync(&data_dir) {
        Ok(public_apps) => public_apps,
        Err(error) => {
            feed_errors.push(json!({
                "source": "public-registry-locator-sync",
                "message": error
            }));
            Vec::new()
        }
    };
    for preview in app_runtime::catalog(&data_dir) {
        let app_id = preview["app_id"].as_str();
        discovered_apps.retain(|existing| existing["app_id"].as_str() != app_id);
        discovered_apps.push(preview);
    }
    match local_product::catalog(&data_dir) {
        Ok(local_apps) => {
            for local_app in local_apps {
                let app_id = local_app["app_id"].as_str();
                discovered_apps.retain(|existing| existing["app_id"].as_str() != app_id);
                discovered_apps.push(local_app);
            }
        }
        Err(error) => feed_errors.push(json!({
            "source": "local-appstore-runtime",
            "message": error
        })),
    }
    let launch_available = discovered_apps
        .iter()
        .any(|app| app["launch_eligible"] == true);
    let installation_performed = discovered_apps.iter().any(|app| {
        matches!(
            app["installation_state"].as_str(),
            Some("installed") | Some("installed-disabled")
        )
    });
    let apps = state["apps"]
        .as_array_mut()
        .ok_or_else(|| "默认 apps 不是数组。".to_string())?;
    apps.extend(discovered_apps);

    let needs = state["needs"]
        .as_array_mut()
        .ok_or_else(|| "默认 needs 不是数组。".to_string())?;
    for item in read_jsonl(&data_dir.join("history/need-requests.jsonl")) {
        needs.insert(0, item);
    }
    for item in read_jsonl(&data_dir.join("outbox/need-requests.jsonl"))
        .into_iter()
        .rev()
    {
        needs.push(item);
    }

    let intents = state["install_intents"]
        .as_array_mut()
        .ok_or_else(|| "默认 install_intents 不是数组。".to_string())?;
    for item in read_jsonl(&data_dir.join("outbox/install-intents.jsonl"))
        .into_iter()
        .rev()
    {
        intents.insert(0, item);
    }

    let legacy_jobs = local_orchestrator::queued_jobs(&data_dir.join("orchestrator"));
    let mut delivery_jobs = match delivery_integration::jobs(&data_dir) {
        Ok(values) => values,
        Err(error) => {
            feed_errors.push(json!({
                "source": "automatic-delivery-controller",
                "message": error
            }));
            Vec::new()
        }
    };
    if delivery_jobs.iter().any(|job| {
        matches!(
            job["status"].as_str(),
            Some("codeagent-running" | "delivery-worker-failed")
        )
    }) {
        if let Err(error) = delivery_integration::resume_pending(&data_dir, &delivery_jobs) {
            feed_errors.push(json!({
                "source": "automatic-delivery-resume",
                "message": error
            }));
        }
    }
    match registry_integration::product_assessment_jobs(&data_dir) {
        Ok(mut assessments) => delivery_jobs.append(&mut assessments),
        Err(error) => feed_errors.push(json!({
            "source": "product-capability-assessment-history",
            "message": error
        })),
    }
    let mut merged_jobs = delivery_integration::merge_with_legacy_for_data_dir(&data_dir, delivery_jobs, legacy_jobs);
    merged_jobs.sort_by(|left, right| {
        right["updated_at_utc"]
            .as_str()
            .cmp(&left["updated_at_utc"].as_str())
    });
    state["jobs"] = json!(merged_jobs);

    state["meta"]["runtime_mode"] = json!("native-tauri-experimental");
    state["meta"]["ecosystem_role"] = json!("launcher-appstore-runtime");
    state["meta"]["generated_app_target"] = json!("vibapp-client");
    state["meta"]["surface_layout_policy"] = json!("host-responsive");
    state["meta"]["candidate_present"] = json!(launch_available);
    state["meta"]["launch_available"] = json!(launch_available);
    state["meta"]["agent_mode"] = json!("registry-first-needspec-consent-phase-2");
    state["meta"]["cloud_agent_connected"] = json!(false);
    let local_codeagent_connected = local_orchestrator::codeagent_adapter_available();
    state["meta"]["local_codeagent_connected"] = json!(local_codeagent_connected);
    state["meta"]["codeagent_queue_enabled"] = json!(local_codeagent_connected);
    state["meta"]["codeagent_external_runner_configured"] = json!(false);
    state["meta"]["codeagent_adapter"] = codeagent["selectedProvider"].clone();
    state["meta"]["delivery_controller"] = json!("durable-automatic-v1");
    state["meta"]["builder_configuration"] = delivery_integration::builder_configuration();
    state["codeagent_settings"] = codeagent;
    state["meta"]["installation_performed"] = json!(installation_performed);
    state["feed_errors"] = json!(feed_errors);
    Ok(state)
}

#[tauri::command]
fn submit_need(app: AppHandle, payload: NeedInput) -> Result<Value, String> {
    let title = payload.title.trim();
    let description = payload.description.trim();
    if title.is_empty() || title.chars().count() > 80 {
        return Err("需求名称需为 1–80 个字符。".to_string());
    }
    if description.chars().count() < 10 || description.chars().count() > 2000 {
        return Err("需求描述需为 10–2000 个字符。".to_string());
    }

    let now = unix_ms();
    let data_dir = app_data_dir(&app)?;
    let model_settings = model_settings::runtime(&data_dir)?;
    match (payload.retry_task_id.as_deref(), payload.need_id.as_deref()) {
        (None, None) => registry_integration::submit_need(
            &data_dir,
            title,
            description,
            payload.embedding_consent,
            now,
            &model_settings,
        ),
        (Some(task_id), Some(need_id)) => {
            let status = delivery_integration::status(&data_dir, task_id)?;
            registry_integration::submit_need_retry(
                &data_dir,
                title,
                description,
                payload.embedding_consent,
                now,
                &model_settings,
                task_id,
                need_id,
                &status,
            )
        }
        _ => Err("修改失败任务时必须同时提供 retryTaskId 与 needId。".to_string()),
    }
}

#[tauri::command]
fn get_model_settings(app: AppHandle) -> Result<Value, String> {
    model_settings::get(&app_data_dir(&app)?)
}

#[tauri::command]
fn save_model_settings(
    app: AppHandle,
    payload: model_settings::SaveModelSettingsInput,
) -> Result<Value, String> {
    model_settings::save(&app_data_dir(&app)?, payload)
}

#[tauri::command]
fn get_codeagent_settings(app: AppHandle) -> Result<Value, String> {
    codeagent_settings::get(&app_data_dir(&app)?)
}

#[tauri::command]
fn save_codeagent_settings(
    app: AppHandle,
    payload: codeagent_settings::SaveCodeAgentSettingsInput,
) -> Result<Value, String> {
    codeagent_settings::save(&app_data_dir(&app)?, payload)
}

#[tauri::command]
fn get_network_settings(app: AppHandle) -> Result<Value, String> {
    network_settings::get(&app_data_dir(&app)?)
}

#[tauri::command]
fn save_network_settings(
    app: AppHandle,
    payload: network_settings::SaveNetworkSettingsInput,
) -> Result<Value, String> {
    network_settings::save(&app_data_dir(&app)?, payload)
}

fn public_network_node_status(transport: Option<Value>, last_error: Option<&str>) -> Value {
    let transport = transport.unwrap_or_else(|| {
        json!({
            "status": "stopped",
            "started": false,
            "torrent_count": 0,
            "active_transfers": 0,
            "error_count": 0
        })
    });
    let running = transport["started"] == true || transport["status"] == "running";
    let state = if running {
        "running"
    } else if last_error.is_some() {
        "error"
    } else {
        "stopped"
    };
    json!({
        "schemaVersion": "vibapp.network-node-status.experimental-v1",
        "state": state,
        "implementation": "roomhash-current",
        "surface": "desktop-trusted-host",
        "foregroundOnly": false,
        "activeTransfers": transport["active_transfers"].as_u64().unwrap_or(0),
        "torrents": transport["torrent_count"].as_u64().unwrap_or(0),
        "torrentPort": transport["torrent_port"],
        "errorCount": transport["error_count"].as_u64().unwrap_or(0),
        "lastError": last_error,
        "transport": transport
    })
}

fn public_network_node_fetch_status(last_error: Option<&str>) -> Value {
    json!({
        "schemaVersion": "vibapp.network-node-status.experimental-v1",
        "state": "running",
        "implementation": "roomhash-current",
        "surface": "desktop-trusted-host",
        "foregroundOnly": false,
        "activeTransfers": 1,
        "torrents": 0,
        "torrentPort": null,
        "errorCount": 0,
        "lastError": last_error,
        "transport": {
            "status": "running",
            "operation": "fetch-public-package",
            "active_transfers": 1
        }
    })
}

fn public_package_is_seedable(item: &Value) -> bool {
    item["verification_state"] == "verified"
        && item["publication_state"] == "published"
        && item["publication_badge"] == "public-appstore"
}

fn public_package_is_downloadable(item: &Value, app_id: &str, package_digest: &str) -> bool {
    item["app_id"] == app_id
        && item["package_digest_sha256"] == package_digest
        && item["verification_state"] == "verified"
        && item["publication_state"] == "published"
        && item["publication_badge"] == "public-appstore"
        && item["install_eligible"] == true
}

fn local_candidate_is_ready(
    data_dir: &Path,
    app_id: &str,
    package_digest: &str,
) -> Result<bool, String> {
    Ok(local_product::catalog(data_dir)?.iter().any(|item| {
        item["app_id"] == app_id
            && item["package_digest_sha256"] == package_digest
            && item["verification_state"] == "verified"
            && item["install_eligible"] == true
    }))
}

fn seed_verified_app_packages(
    data_dir: &Path,
    host: &mut roomhash_host::RoomHashHost,
) -> Vec<String> {
    const MAX_AUTOMATIC_SEEDS: usize = 16;
    let catalog = match local_product::catalog(data_dir) {
        Ok(value) => value,
        Err(error) => return vec![format!("无法读取待上传的公开已验证应用：{error}")],
    };
    let mut digests = catalog
        .iter()
        .filter(|item| public_package_is_seedable(item))
        .filter_map(|item| item["package_digest_sha256"].as_str())
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .map(str::to_owned)
        .collect::<Vec<_>>();
    digests.sort();
    digests.dedup();
    let mut warnings = Vec::new();
    if digests.len() > MAX_AUTOMATIC_SEEDS {
        warnings.push(format!(
            "公开已验证应用超过自动上传上限；本次仅加载前 {MAX_AUTOMATIC_SEEDS} 个。"
        ));
        digests.truncate(MAX_AUTOMATIC_SEEDS);
    }
    for digest in digests {
        let candidate = local_product::store_root(data_dir)
            .join("candidates")
            .join(&digest)
            .join("candidate.json");
        let safe_file = candidate.symlink_metadata().is_ok_and(|metadata| {
            metadata.file_type().is_file() && !metadata.file_type().is_symlink()
        });
        if !safe_file {
            warnings.push(format!(
                "应用包 {} 缺少安全的 candidate.json。",
                &digest[..12]
            ));
            continue;
        }
        match host.seed_package(&candidate, &digest) {
            Ok(receipt) => {
                if let Err(error) = roomhash_host::persist_package_locator(data_dir, &receipt) {
                    warnings.push(format!(
                        "应用包 {} 定位记录保存失败：{error}",
                        &digest[..12]
                    ));
                }
            }
            Err(error) => warnings.push(format!("应用包 {} 上传校验失败：{error}", &digest[..12])),
        }
        if warnings.len() >= 4 {
            break;
        }
    }
    warnings
}

fn start_network_node_inner(
    data_dir: &Path,
    state: &mut NetworkNodeInner,
) -> Result<Value, String> {
    state.collaboration_sessions.clear();
    let settings = network_settings::get(data_dir)?;
    if settings["p2pEnabled"] != true {
        return Err("P2P 网络已在设置中关闭。".to_string());
    }
    let private_config = network_settings::roomhash_host_config(data_dir)?;
    let config = roomhash_host::RoomHashConfig::try_from_value(&private_config)
        .map_err(|error| error.to_string())?;
    if let Some(mut previous) = state.host.take() {
        let _ = previous.shutdown();
    }
    let result: Result<(Value, Vec<String>), String> = (|| {
        let mut host =
            roomhash_host::RoomHashHost::spawn(data_dir).map_err(|error| error.to_string())?;
        host.start(&config).map_err(|error| error.to_string())?;
        let warnings = if settings["seedVerifiedApps"] == true {
            seed_verified_app_packages(data_dir, &mut host)
        } else {
            Vec::new()
        };
        let status = host.status().map_err(|error| error.to_string())?;
        state.host = Some(host);
        Ok((status, warnings))
    })();
    match result {
        Ok((status, warnings)) => {
            state.last_error = (!warnings.is_empty())
                .then(|| warnings.join(" ").chars().take(512).collect::<String>());
            Ok(public_network_node_status(
                Some(status),
                state.last_error.as_deref(),
            ))
        }
        Err(error) => {
            state.last_error = Some(error.chars().take(512).collect());
            Err(error)
        }
    }
}

#[tauri::command]
fn get_network_status(state: State<'_, NetworkNodeState>) -> Result<Value, String> {
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "P2P 网络宿主状态锁不可用。".to_string())?;
    let last_error = state.last_error.clone();
    if state.package_fetch_in_progress {
        return Ok(public_network_node_fetch_status(last_error.as_deref()));
    }
    let Some(host) = state.host.as_mut() else {
        return Ok(public_network_node_status(None, last_error.as_deref()));
    };
    if !host.is_running() {
        state.host = None;
        state.collaboration_sessions.clear();
        state.last_error = Some("RoomHash 宿主进程已退出。".to_string());
        return Ok(public_network_node_status(
            None,
            state.last_error.as_deref(),
        ));
    }
    match host.status() {
        Ok(status) => Ok(public_network_node_status(
            Some(status),
            last_error.as_deref(),
        )),
        Err(error) => {
            let error = error.to_string();
            state.host = None;
            state.collaboration_sessions.clear();
            state.last_error = Some(error.chars().take(512).collect());
            Ok(public_network_node_status(
                None,
                state.last_error.as_deref(),
            ))
        }
    }
}

#[tauri::command]
fn start_network_node(app: AppHandle, state: State<'_, NetworkNodeState>) -> Result<Value, String> {
    let data_dir = app_data_dir(&app)?;
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "P2P 网络宿主状态锁不可用。".to_string())?;
    if state.package_fetch_in_progress {
        return Err("公开应用包正在下载，请完成后再重启网络节点。".to_string());
    }
    start_network_node_inner(&data_dir, &mut state)
}

#[tauri::command]
fn stop_network_node(state: State<'_, NetworkNodeState>) -> Result<Value, String> {
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "P2P 网络宿主状态锁不可用。".to_string())?;
    if state.package_fetch_in_progress {
        return Err("公开应用包正在下载，请完成后再停止网络节点。".to_string());
    }
    let result = state
        .host
        .take()
        .map(|mut host| host.shutdown().map_err(|error| error.to_string()))
        .transpose();
    match result {
        Ok(_) => {
            state.last_error = None;
            state.collaboration_sessions.clear();
            Ok(public_network_node_status(None, None))
        }
        Err(error) => {
            state.last_error = Some(error.chars().take(512).collect());
            Err(error)
        }
    }
}

fn collaboration_limit(data_dir: &Path) -> Result<usize, String> {
    let value = network_settings::get(data_dir)?["maxActiveChannels"]
        .as_u64()
        .ok_or_else(|| "协作频道上限设置无效。".to_string())?;
    usize::try_from(value.min(16)).map_err(|_| "协作频道上限无法转换。".to_string())
}

fn prune_collaboration_sessions(state: &mut NetworkNodeInner) {
    let now = unix_ms_u64();
    state
        .collaboration_sessions
        .retain(|_, binding| binding.expires_at > now);
}

fn open_collaboration_session(
    data_dir: &Path,
    state: &mut NetworkNodeInner,
    operation: &str,
    app_id: &str,
    channel_id: &str,
    confirmed: bool,
    expires_in_ms: u64,
) -> Result<Value, String> {
    if !confirmed {
        return Err("创建或加入共享频道前需要用户明确确认。".to_string());
    }
    let settings = network_settings::get(data_dir)?;
    if settings["p2pEnabled"] != true || settings["rtcEnabled"] != true {
        return Err("P2P 或实时协作已在设置中关闭。".to_string());
    }
    let app_id = collaboration_app_id(app_id)?;
    let channel_id = collaboration_uuid(channel_id, "协作频道 ID")?;
    let expires_at = collaboration_expiry(expires_in_ms)?;
    prune_collaboration_sessions(state);
    if state.collaboration_sessions.len() >= collaboration_limit(data_dir)? {
        return Err("已达到设置中的协作频道上限。".to_string());
    }
    let grant = collaboration_grant(operation, &channel_id, expires_at);
    let host = state
        .host
        .as_mut()
        .ok_or_else(|| "P2P 网络节点尚未启动。".to_string())?;
    let result = match operation {
        "create" => host.collaboration_create(&channel_id, expires_at, &grant),
        "join" => host.collaboration_join(&channel_id, expires_at, &grant),
        _ => return Err("协作频道操作无效。".to_string()),
    }?;
    let session_id = collaboration_uuid(
        result["session_id"]
            .as_str()
            .ok_or_else(|| "RoomHash 未返回有效协作会话。".to_string())?,
        "协作会话 ID",
    )?;
    let returned_channel = collaboration_uuid(
        result["channel_id"]
            .as_str()
            .ok_or_else(|| "RoomHash 未返回有效协作频道。".to_string())?,
        "协作频道 ID",
    )?;
    if returned_channel != channel_id || result["expires_at"].as_u64() != Some(expires_at) {
        return Err("RoomHash 返回的协作会话与授权不匹配。".to_string());
    }
    state.collaboration_sessions.insert(
        session_id.clone(),
        CollaborationSessionBinding {
            app_id: app_id.clone(),
            channel_id: channel_id.clone(),
            expires_at,
            received_event_ids: VecDeque::new(),
        },
    );
    Ok(json!({
        "sessionId": session_id,
        "channelId": channel_id,
        "appId": app_id,
        "expiresAt": expires_at,
    }))
}

#[tauri::command]
fn create_collaboration_session(
    app: AppHandle,
    state: State<'_, NetworkNodeState>,
    payload: CreateCollaborationInput,
) -> Result<Value, String> {
    let data_dir = app_data_dir(&app)?;
    let channel_id = Uuid::new_v4().to_string();
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "协作网络宿主状态锁不可用。".to_string())?;
    open_collaboration_session(
        &data_dir,
        &mut state,
        "create",
        &payload.app_id,
        &channel_id,
        payload.confirmed,
        payload.expires_in_ms,
    )
}

#[tauri::command]
fn join_collaboration_session(
    app: AppHandle,
    state: State<'_, NetworkNodeState>,
    payload: JoinCollaborationInput,
) -> Result<Value, String> {
    let data_dir = app_data_dir(&app)?;
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "协作网络宿主状态锁不可用。".to_string())?;
    open_collaboration_session(
        &data_dir,
        &mut state,
        "join",
        &payload.app_id,
        &payload.channel_id,
        payload.confirmed,
        payload.expires_in_ms,
    )
}

#[tauri::command]
fn leave_collaboration_session(
    state: State<'_, NetworkNodeState>,
    payload: LeaveCollaborationInput,
) -> Result<Value, String> {
    let session_id = collaboration_uuid(&payload.session_id, "协作会话 ID")?;
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "协作网络宿主状态锁不可用。".to_string())?;
    if !state.collaboration_sessions.contains_key(&session_id) {
        return Err("协作会话不存在或已过期。".to_string());
    }
    let result = state
        .host
        .as_mut()
        .ok_or_else(|| "P2P 网络节点尚未启动。".to_string())?
        .collaboration_leave(&session_id)?;
    state.collaboration_sessions.remove(&session_id);
    Ok(json!({ "sessionId": session_id, "status": result["status"] }))
}

#[tauri::command]
fn send_collaboration_event(
    state: State<'_, NetworkNodeState>,
    payload: SendCollaborationInput,
) -> Result<Value, String> {
    let session_id = collaboration_uuid(&payload.session_id, "协作会话 ID")?;
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "协作网络宿主状态锁不可用。".to_string())?;
    prune_collaboration_sessions(&mut state);
    let binding = state
        .collaboration_sessions
        .get(&session_id)
        .cloned()
        .ok_or_else(|| "协作会话不存在或已过期。".to_string())?;
    let event_id = Uuid::new_v4().to_string();
    let envelope = json!({
        "schema_version": "vibapp.collaboration-event.experimental-v1",
        "app_id": binding.app_id,
        "channel_id": binding.channel_id,
        "event_id": event_id,
        "sent_at": unix_ms_u64(),
        "payload": payload.payload,
    });
    let message =
        serde_json::to_vec(&envelope).map_err(|_| "协作事件无法编码为 JSON。".to_string())?;
    if message.is_empty() || message.len() > 64 * 1024 {
        return Err("协作事件编码后必须在 1 到 64 KiB 之间。".to_string());
    }
    let result = state
        .host
        .as_mut()
        .ok_or_else(|| "P2P 网络节点尚未启动。".to_string())?
        .collaboration_send(&session_id, &message)?;
    Ok(json!({
        "sessionId": session_id,
        "eventId": event_id,
        "acceptedBytes": result["accepted_bytes"],
    }))
}

fn validate_received_collaboration_event(
    message_base64: &str,
    binding: &CollaborationSessionBinding,
) -> Option<Value> {
    let bytes = BASE64_STANDARD.decode(message_base64).ok()?;
    if bytes.is_empty()
        || bytes.len() > 64 * 1024
        || BASE64_STANDARD.encode(&bytes) != message_base64
    {
        return None;
    }
    let value = serde_json::from_slice::<Value>(&bytes).ok()?;
    let object = value.as_object()?;
    let expected = [
        "schema_version",
        "app_id",
        "channel_id",
        "event_id",
        "sent_at",
        "payload",
    ];
    if object.len() != expected.len() || expected.iter().any(|key| !object.contains_key(*key)) {
        return None;
    }
    if value["schema_version"] != "vibapp.collaboration-event.experimental-v1"
        || value["app_id"] != binding.app_id
        || value["channel_id"] != binding.channel_id
        || value["sent_at"].as_u64().is_none()
        || collaboration_uuid(value["event_id"].as_str()?, "协作事件 ID").is_err()
    {
        return None;
    }
    Some(value)
}

fn remember_received_event_id(ids: &mut VecDeque<String>, event_id: &str) -> bool {
    const MAX_REMEMBERED_EVENT_IDS: usize = 256;
    if ids.iter().any(|known| known == event_id) {
        return false;
    }
    ids.push_back(event_id.to_string());
    while ids.len() > MAX_REMEMBERED_EVENT_IDS {
        ids.pop_front();
    }
    true
}

#[tauri::command]
fn receive_collaboration_events(
    state: State<'_, NetworkNodeState>,
    payload: ReceiveCollaborationInput,
) -> Result<Value, String> {
    let session_id = collaboration_uuid(&payload.session_id, "协作会话 ID")?;
    if !(1..=4).contains(&payload.limit) {
        return Err("单次最多接收 4 条协作事件。".to_string());
    }
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "协作网络宿主状态锁不可用。".to_string())?;
    prune_collaboration_sessions(&mut state);
    let binding = state
        .collaboration_sessions
        .get(&session_id)
        .cloned()
        .ok_or_else(|| "协作会话不存在或已过期。".to_string())?;
    let received = state
        .host
        .as_mut()
        .ok_or_else(|| "P2P 网络节点尚未启动。".to_string())?
        .collaboration_receive(&session_id, payload.limit)?;
    let mut received_event_ids = binding.received_event_ids.clone();
    let events = received["messages"]
        .as_array()
        .ok_or_else(|| "RoomHash 返回的协作收件箱无效。".to_string())?
        .iter()
        .filter_map(|message| {
            let event = validate_received_collaboration_event(
                message["message_base64"].as_str()?,
                &binding,
            )?;
            let event_id = event["event_id"].as_str()?;
            if !remember_received_event_id(&mut received_event_ids, event_id) {
                return None;
            }
            Some(json!({
                "channelId": binding.channel_id,
                "receivedAt": message["received_at"],
                "event": event,
            }))
        })
        .collect::<Vec<_>>();
    if let Some(current) = state.collaboration_sessions.get_mut(&session_id) {
        current.received_event_ids = received_event_ids;
    }
    Ok(json!({ "sessionId": session_id, "events": events }))
}

#[tauri::command]
fn get_collaboration_status(state: State<'_, NetworkNodeState>) -> Result<Value, String> {
    let mut state = state
        .inner
        .lock()
        .map_err(|_| "协作网络宿主状态锁不可用。".to_string())?;
    prune_collaboration_sessions(&mut state);
    let Some(host) = state.host.as_mut() else {
        return Ok(json!({
            "state": "stopped",
            "sessionCount": 0,
            "queuedEvents": 0,
            "sessions": [],
        }));
    };
    let raw = host.collaboration_status()?;
    let sessions = state
        .collaboration_sessions
        .iter()
        .map(|(session_id, binding)| {
            let transport = raw["sessions"]
                .as_array()
                .and_then(|sessions| sessions.iter().find(|value| value["session_id"] == *session_id));
            json!({
                "sessionId": session_id,
                "appId": binding.app_id,
                "channelId": binding.channel_id,
                "expiresAt": binding.expires_at,
                "peerCount": transport.and_then(|value| value["transport"]["peer_count"].as_u64()).unwrap_or(0),
                "sentMessages": transport.and_then(|value| value["sent_messages"].as_u64()).unwrap_or(0),
                "receivedMessages": transport.and_then(|value| value["received_messages"].as_u64()).unwrap_or(0),
            })
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "state": raw["state"],
        "available": raw["available"],
        "reasonCode": raw["reason_code"],
        "turnConfigured": raw["turn_configured"],
        "turnApplied": raw["turn_applied"],
        "sessionCount": sessions.len(),
        "queuedEvents": raw["queued_messages"],
        "queuedBytes": raw["queued_bytes"],
        "limits": raw["limits"],
        "sessions": sessions,
    }))
}

#[tauri::command]
fn complete_need(
    app: AppHandle,
    payload: registry_integration::CompleteNeedInput,
) -> Result<Value, String> {
    let data_dir = app_data_dir(&app)?;
    let model_settings = model_settings::runtime(&data_dir)?;
    let codeagent = codeagent_settings::runtime(&data_dir)?;
    let provider_observation = local_orchestrator::codeagent_identity_observation(
        &codeagent.provider_id,
        &codeagent.model,
    )?;
    registry_integration::complete_need_with_provider(
        &data_dir,
        payload,
        unix_ms(),
        &model_settings.embedding,
        &codeagent.task_provider,
        &codeagent.model,
        &provider_observation,
    )
}

#[tauri::command]
fn submit_development_task(
    app: AppHandle,
    payload: DevelopmentSubmitInput,
) -> Result<Value, String> {
    let data_dir = app_data_dir(&app)?;
    // Submission always uses the provider/model authorized inside the immutable
    // task. Later settings changes only affect newly prepared tasks.
    let codeagent = codeagent_settings::runtime_from_task(&payload.task)?;
    let trusted_registry = registry_integration::load_trusted_registry_development_evidence(
        &data_dir,
        &payload.task,
        &payload.registry,
    )?;
    delivery_integration::submit_and_start(
        &data_dir,
        &payload.task,
        &trusted_registry,
        payload.explicit_user_submit,
        payload.acknowledge_external_cost,
        payload.retry_task_id.as_deref(),
        &codeagent,
    )
}

fn fetch_public_candidate(
    data_dir: &Path,
    network: &NetworkNodeState,
    selected: &Value,
    app_id: &str,
    package_digest: &str,
) -> Result<Value, String> {
    if !public_package_is_downloadable(selected, app_id, package_digest) {
        return Err("只有 AppStore 中公开且独立验证的应用才能通过 P2P 获取。".to_string());
    }
    if local_candidate_is_ready(data_dir, app_id, package_digest)? {
        return Ok(json!({
            "status": "already-local",
            "packageDigestSha256": package_digest,
        }));
    }
    let locator = roomhash_host::read_package_locator(data_dir, package_digest)?
        .ok_or_else(|| "Registry 尚未提供这个公开应用的 P2P 定位信息。".to_string())?;
    let settings = network_settings::get(data_dir)?;
    if settings["p2pEnabled"] != true {
        return Err("请先在设置中启用 P2P 网络。".to_string());
    }
    let magnet_uri = locator["magnet_uri"]
        .as_str()
        .ok_or_else(|| "公开应用定位信息缺少 magnet。".to_string())?
        .to_string();
    let files = locator["files"].clone();

    let mut host = {
        let mut state = network
            .inner
            .lock()
            .map_err(|_| "P2P 网络宿主状态锁不可用。".to_string())?;
        if state.package_fetch_in_progress {
            return Err("已有一个公开应用包正在下载。".to_string());
        }
        let needs_start = state.host.as_mut().is_none_or(|host| !host.is_running());
        if needs_start {
            state.host = None;
            state.collaboration_sessions.clear();
            start_network_node_inner(data_dir, &mut state)?;
        }
        let host = state
            .host
            .take()
            .ok_or_else(|| "P2P 网络节点尚未启动。".to_string())?;
        state.package_fetch_in_progress = true;
        host
    };
    let fetched = host.fetch_package(&magnet_uri, package_digest, files, 120_000);
    let host_running = host.is_running();
    {
        let mut state = network
            .inner
            .lock()
            .map_err(|_| "P2P 网络宿主状态锁不可用。".to_string())?;
        state.package_fetch_in_progress = false;
        if host_running {
            state.host = Some(host);
        } else {
            state.collaboration_sessions.clear();
            state.last_error = Some("RoomHash 宿主在公开应用下载后已退出。".to_string());
        }
        if let Err(error) = &fetched {
            state.last_error = Some(error.chars().take(512).collect());
        }
    }
    let fetched = fetched?;
    if fetched["operation"] != "fetch"
        || fetched["package_digest_sha256"] != package_digest
        || fetched["info_hash"] != locator["info_hash"]
        || fetched["size_bytes"] != locator["size_bytes"]
    {
        return Err("P2P 下载回执与 Registry 定位信息不一致。".to_string());
    }
    let candidate_path = fetched["candidate_path"]
        .as_str()
        .ok_or_else(|| "P2P 下载没有返回候选包入口。".to_string())?;
    let candidate = fs::canonicalize(candidate_path)
        .map_err(|error| format!("无法检查 P2P 下载的候选包：{error}"))?;
    let managed_root = fs::canonicalize(data_dir.join("roomhash/downloads").join(package_digest))
        .map_err(|error| format!("无法检查 P2P 下载目录：{error}"))?;
    let metadata = fs::symlink_metadata(candidate_path)
        .map_err(|error| format!("无法检查 P2P 下载的候选记录：{error}"))?;
    if !candidate.starts_with(&managed_root)
        || candidate.file_name().and_then(|name| name.to_str()) != Some("candidate.json")
        || !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
    {
        return Err("P2P 下载的候选包入口逃逸了受管目录。".to_string());
    }
    local_product::ingest(
        data_dir,
        candidate
            .to_str()
            .ok_or_else(|| "P2P 下载的候选路径无法安全编码。".to_string())?,
    )?;
    if !local_candidate_is_ready(data_dir, app_id, package_digest)? {
        return Err("下载的候选包没有通过本地 AppStore 独立复验。".to_string());
    }
    Ok(json!({
        "status": "downloaded-and-verified",
        "packageDigestSha256": package_digest,
        "infoHash": locator["info_hash"],
        "sizeBytes": locator["size_bytes"],
    }))
}

#[tauri::command]
fn submit_install_intent(
    app: AppHandle,
    network: State<'_, NetworkNodeState>,
    payload: InstallInput,
) -> Result<Value, String> {
    let product_state = get_state(app.clone())?;
    let selected = product_state["apps"]
        .as_array()
        .and_then(|apps| apps.iter().find(|item| item["app_id"] == payload.app_id))
        .ok_or_else(|| "没有找到这个应用。".to_string())?;
    if selected["verification_state"] != "verified" || selected["install_eligible"] != true {
        return Err("只有独立验收为 verified 的候选包才能安装。".to_string());
    }
    if selected["package_digest_sha256"] != payload.package_digest_sha256 {
        return Err("应用摘要已变化，请刷新后重试。".to_string());
    }
    let data_dir = app_data_dir(&app)?;
    let transport =
        if local_candidate_is_ready(&data_dir, &payload.app_id, &payload.package_digest_sha256)? {
            None
        } else {
            Some(fetch_public_candidate(
                &data_dir,
                &network,
                selected,
                &payload.app_id,
                &payload.package_digest_sha256,
            )?)
        };
    let installation =
        local_product::install(&data_dir, &payload.app_id, &payload.package_digest_sha256)?;
    let intent = json!({
        "intent_id": format!("intent-{:x}", unix_ms()),
        "app_id": payload.app_id,
        "package_digest_sha256": payload.package_digest_sha256,
        "status": "installed-disabled",
        "installation_performed": true,
        "next_authority": "runtime-daemon",
        "created_at_utc": unix_ms(),
        "transport": transport,
        "installation": installation
    });
    append_jsonl(&data_dir.join("outbox/install-intents.jsonl"), &intent)?;
    Ok(json!({ "intent": intent }))
}

#[tauri::command]
fn import_verified_candidate(
    app: AppHandle,
    payload: ImportCandidateInput,
) -> Result<Value, String> {
    local_product::ingest(&app_data_dir(&app)?, &payload.candidate_path)
}

#[tauri::command]
fn control_app_lifecycle(
    app: AppHandle,
    payload: local_product::LifecycleInput,
) -> Result<Value, String> {
    local_product::lifecycle(&app_data_dir(&app)?, payload)
}

#[tauri::command]
async fn open_app_window(app: AppHandle, app_id: String) -> Result<Value, String> {
    open_known_app_window(&app, &app_id, false)
}

fn open_known_app_window(app: &AppHandle, app_id: &str, installed_only: bool) -> Result<Value, String> {
    let product_state = get_state(app.clone())?;
    let selected = product_state["apps"]
        .as_array()
        .and_then(|apps| apps.iter().find(|item| item["app_id"] == app_id))
        .ok_or_else(|| "没有找到这个应用。".to_string())?;
    if installed_only {
        startup::require_installed_application(selected)?;
    }
    if selected["launch_eligible"] != true {
        return Err("这个应用当前不可启动。".to_string());
    }
    let display_name = selected["display_name"]
        .as_str()
        .unwrap_or("VibApp 应用")
        .to_string();
    let presentation = app_window::ValidatedWindowPresentation::from_catalog_item(selected)?;
    app_window::open(app, app_id, &display_name, presentation)
}

#[tauri::command]
fn register_app_window_surface(
    window: tauri::WebviewWindow,
    state: State<'_, app_window::AppWindowState>,
    payload: app_window::RegisterSurfaceInput,
) -> Result<(), String> {
    let content_height = payload.content_height;
    app_window::register_surface(&state, window.label(), payload)?;
    // Window fitting is cosmetic; a platform resize failure must not turn a
    // successfully launched component into a fatal application boot screen.
    if let Err(error) = app_window::fit_initial_content(&window, &state, content_height) {
        eprintln!("VibApp initial window fit: {error}");
    }
    Ok(())
}

#[tauri::command]
fn close_app_window(
    window: tauri::WebviewWindow,
    state: State<'_, app_window::AppWindowState>,
) -> Result<(), String> {
    app_window::close(&window, &state)
}

fn trusted_installed_identity(
    item: &Value,
    descriptor: &Value,
    binding: &Value,
) -> Result<Value, String> {
    let package_digest = binding["package_digest_sha256"]
        .as_str()
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or_else(|| "运行时缺少有效 active package 摘要。".to_string())?;
    let generation = binding["generation"]
        .as_str()
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .ok_or_else(|| "运行时缺少有效 generation。".to_string())?;
    if item["installation_state"] != "installed"
        || item["enabled"] != true
        || item["active_package_digest_sha256"] != package_digest
        || item["package_digest_sha256"] != package_digest
        || item["app_id"] != descriptor["id"]
        || item["display_name"] != descriptor["display_name"]
        || item["version"] != descriptor["version"]
    {
        return Err("应用窗口身份没有绑定 daemon active generation。".to_string());
    }
    let publisher = item["publisher"]
        .as_str()
        .filter(|value| !value.is_empty() && value.chars().count() <= 80)
        .filter(|value| !value.chars().any(char::is_control))
        .ok_or_else(|| "daemon active 应用缺少有效发布者。".to_string())?;
    let permissions = item["permissions"]
        .as_array()
        .filter(|items| items.len() <= 64)
        .ok_or_else(|| "daemon active 应用权限投影无效。".to_string())?;
    if !permissions.iter().all(|permission| {
        permission
            .as_str()
            .is_some_and(|value| !value.is_empty() && value.len() <= 160)
    }) {
        return Err("daemon active 应用权限投影含无效值。".to_string());
    }
    let publication_badge = item["publication_badge"]
        .as_str()
        .filter(|value| matches!(*value, "private" | "public-appstore"))
        .unwrap_or("private");
    Ok(json!({
        "app_id": descriptor["id"],
        "display_name": descriptor["display_name"],
        "version": descriptor["version"],
        "publisher": publisher,
        "publication_badge": publication_badge,
        "permission_count": permissions.len(),
        "permission_status": "host-managed",
        "active_package_digest_sha256": package_digest,
        "generation": generation,
    }))
}

#[tauri::command]
fn launch_app(app: AppHandle, app_id: String) -> Result<Value, String> {
    let data_dir = app_data_dir(&app)?;
    if local_product::is_installed(&data_dir, &app_id)? {
        let daemon_surface = local_product::authorize_launch(&data_dir, &app_id)?;
        let binding = daemon_surface["outcome"]["value"].clone();
        let mut result = match app_runtime::installed_surface(&data_dir, &app_id, &binding) {
            Ok(result) => result,
            Err(error) => {
                let report = local_product::report_ui_render_failure(
                    &data_dir,
                    local_product::UiRenderFailureInput {
                        app_id: app_id.clone(),
                        entrypoint: binding["entrypoint"]
                            .as_str()
                            .unwrap_or_default()
                            .to_string(),
                        package_digest_sha256: binding["package_digest_sha256"]
                            .as_str()
                            .unwrap_or_default()
                            .to_string(),
                        component_sha256: binding["component_sha256"]
                            .as_str()
                            .unwrap_or_default()
                            .to_string(),
                        generation: binding["generation"]
                            .as_str()
                            .unwrap_or_default()
                            .to_string(),
                        session: binding["session"].as_str().unwrap_or_default().to_string(),
                        surface: binding["surface"].as_str().unwrap_or_default().to_string(),
                        route: binding["route"].as_str().unwrap_or_default().to_string(),
                        event_id: format!("desktop-render-failure-{:x}", unix_ms()),
                        render_failure_token: binding["render_failure_token"]
                            .as_str()
                            .unwrap_or_default()
                            .to_string(),
                        reason: error.chars().take(400).collect(),
                    },
                );
                return Err(match report {
                    Ok(_) => format!("{error}；候选 UI 已安全回滚。"),
                    Err(report_error) => format!("{error}；失败上报结果：{report_error}"),
                });
            }
        };
        let surface = binding["semantic_surface"].clone();
        app_runtime::validate_surface_update(&surface)?;
        result["surface"] = surface;
        result["runtime_binding"] = json!({
            "entrypoint": binding["entrypoint"],
            "package_digest_sha256": binding["package_digest_sha256"],
            "component_sha256": binding["component_sha256"],
            "generation": binding["generation"],
            "session": binding["session"],
            "surface": binding["surface"],
            "route": binding["route"],
            "render_failure_token": binding["render_failure_token"],
        });
        result["daemon_surface"] = daemon_surface;
        let active_item = local_product::catalog(&data_dir)?
            .into_iter()
            .find(|item| item["app_id"] == app_id)
            .ok_or_else(|| "daemon active 应用没有可信 catalog 投影。".to_string())?;
        result["trusted_identity"] =
            trusted_installed_identity(&active_item, &result["descriptor"], &binding)?;
        Ok(result)
    } else {
        app_runtime::launch(&data_dir, &app_id)
    }
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn dispatch_app_action(
    app: AppHandle,
    app_id: String,
    entrypoint: String,
    package_digest_sha256: String,
    component_sha256: String,
    generation: String,
    session: String,
    surface: String,
    route: String,
    action: String,
    event_id: String,
    fields: Vec<Value>,
) -> Result<Value, String> {
    let daemon_surface = local_product::dispatch_ui_action(
        &app_data_dir(&app)?,
        local_product::UiActionInput {
            app_id,
            entrypoint,
            package_digest_sha256,
            component_sha256,
            generation,
            session,
            surface,
            route,
            action,
            event_id,
            fields,
        },
    )?;
    let binding = &daemon_surface["outcome"]["value"];
    let semantic_surface = binding["semantic_surface"].clone();
    app_runtime::validate_surface_update(&semantic_surface)?;
    Ok(json!({
        "runtime_binding": {
            "entrypoint": binding["entrypoint"],
            "package_digest_sha256": binding["package_digest_sha256"],
            "component_sha256": binding["component_sha256"],
            "generation": binding["generation"],
            "session": binding["session"],
            "surface": binding["surface"],
            "route": binding["route"],
        },
        "surface": semantic_surface,
        "daemon_surface": daemon_surface,
    }))
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn refresh_app_surface(
    app: AppHandle,
    app_id: String,
    entrypoint: String,
    package_digest_sha256: String,
    component_sha256: String,
    generation: String,
    session: String,
    surface: String,
    route: String,
    event_id: String,
) -> Result<Value, String> {
    let daemon_surface = local_product::refresh_ui_surface(
        &app_data_dir(&app)?,
        local_product::UiRefreshInput {
            app_id,
            entrypoint,
            package_digest_sha256,
            component_sha256,
            generation,
            session,
            surface,
            route,
            event_id,
        },
    )?;
    let binding = &daemon_surface["outcome"]["value"];
    let semantic_surface = binding["semantic_surface"].clone();
    app_runtime::validate_surface_update(&semantic_surface)?;
    Ok(json!({
        "runtime_binding": {
            "entrypoint": binding["entrypoint"],
            "package_digest_sha256": binding["package_digest_sha256"],
            "component_sha256": binding["component_sha256"],
            "generation": binding["generation"],
            "session": binding["session"],
            "surface": binding["surface"],
            "route": binding["route"],
        },
        "surface": semantic_surface,
        "daemon_surface": daemon_surface,
    }))
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
fn report_app_render_failure(
    app: AppHandle,
    app_id: String,
    entrypoint: String,
    package_digest_sha256: String,
    component_sha256: String,
    generation: String,
    session: String,
    surface: String,
    route: String,
    event_id: String,
    render_failure_token: String,
    reason: String,
) -> Result<Value, String> {
    local_product::report_ui_render_failure(
        &app_data_dir(&app)?,
        local_product::UiRenderFailureInput {
            app_id,
            entrypoint,
            package_digest_sha256,
            component_sha256,
            generation,
            session,
            surface,
            route,
            event_id,
            render_failure_token,
            reason,
        },
    )
}

fn main() {
    std::hint::black_box(&DESKTOP_BUILD_INPUT_RECEIPT);
    let startup = match startup::StartupOptions::parse(std::env::args_os().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("{error}\n{}", startup::USAGE);
            std::process::exit(2);
        }
    };
    if startup.help {
        println!("{}", startup::USAGE);
        return;
    }
    tauri::Builder::default()
        .manage(startup)
        .manage(deep_link::StoreOpenState::default())
        .manage(NetworkNodeState::default())
        .manage(app_window::AppWindowState::default())
        .setup(|app| {
            let handle = app.handle().clone();
            if let Ok(data_dir) = app_data_dir(&handle)
                && network_settings::get(&data_dir)
                    .is_ok_and(|settings| settings["p2pEnabled"] == true)
            {
                let state = app.state::<NetworkNodeState>();
                if let Ok(mut state) = state.inner.lock()
                    && let Err(error) = start_network_node_inner(&data_dir, &mut state)
                {
                    state.last_error = Some(error.chars().take(512).collect());
                }
            }
            if let Some(app_id) = app.state::<startup::StartupOptions>().app_id.clone() {
                open_known_app_window(&handle, &app_id, true).map_err(std::io::Error::other)?;
                if let Some(main) = handle.get_webview_window("main") {
                    main.hide()?;
                }
            }
            if let Some(app_id) = app.state::<startup::StartupOptions>().store_app_id.clone() {
                deep_link::receive(&handle, &format!("vibapp://{app_id}")).map_err(std::io::Error::other)?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_state,
            deep_link::get_store_open_request,
            deep_link::confirm_store_open_request,
            deep_link::dismiss_store_open_request,
            get_model_settings,
            save_model_settings,
            get_codeagent_settings,
            save_codeagent_settings,
            get_network_settings,
            save_network_settings,
            get_network_status,
            start_network_node,
            stop_network_node,
            create_collaboration_session,
            join_collaboration_session,
            leave_collaboration_session,
            send_collaboration_event,
            receive_collaboration_events,
            get_collaboration_status,
            submit_need,
            complete_need,
            submit_development_task,
            submit_install_intent,
            import_verified_candidate,
            control_app_lifecycle,
            open_app_window,
            register_app_window_surface,
            close_app_window,
            launch_app,
            dispatch_app_action,
            refresh_app_surface,
            report_app_render_failure
        ])
        .build(tauri::generate_context!())
        .expect("VibApp native launcher failed")
        .run(|app, event| {
            #[cfg(any(target_os = "macos", target_os = "ios", target_os = "android"))]
            if let tauri::RunEvent::Opened { urls } = event {
                for url in urls.into_iter().take(1) {
                    let _ = deep_link::receive(app, url.as_str());
                }
            }
            #[cfg(not(any(target_os = "macos", target_os = "ios", target_os = "android")))]
            let _ = (app, event);
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    struct AcceptanceDaemon(std::process::Child);

    #[cfg(unix)]
    impl Drop for AcceptanceDaemon {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[cfg(unix)]
    fn acceptance_root(label: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64;
        let path = Path::new("/tmp").join(format!("vibapp-rh-{label}-{suffix:x}"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[cfg(unix)]
    fn start_acceptance_daemon(data_dir: &Path) -> AcceptanceDaemon {
        use std::os::unix::net::UnixStream;
        use std::process::{Command, Stdio};
        use std::thread;
        use std::time::Duration;

        let module_root =
            fs::canonicalize(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../runtime-daemon"))
                .unwrap();
        let service_runtime = fs::canonicalize(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../runtime-daemon/target-service-1_98/release/vibapp-service-runtime"),
        )
        .unwrap();
        let promotion_root = local_product::store_root(data_dir).join("candidates");
        fs::create_dir_all(&promotion_root).unwrap();
        let socket = native_platform::unix_daemon_socket(data_dir).unwrap();
        let mut child = Command::new(native_platform::python_executable(false).unwrap())
            .args(["-X", "utf8"]).arg("-I")
            .arg("-B")
            .arg("-c")
            .arg("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('vibapp_daemon',run_name='__main__')")
            .arg(&module_root)
            .arg("serve")
            .arg("--root")
            .arg(local_product::runtime_root(data_dir))
            .arg("--promotion-root")
            .arg(&promotion_root)
            .arg("--socket")
            .arg(&socket)
            .env_clear()
            .env("PATH", native_platform::safe_path().unwrap())
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("TZ", "UTC")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("VIBAPP_SERVICE_RUNTIME_BIN", service_runtime)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let deadline = SystemTime::now() + Duration::from_secs(5);
        while SystemTime::now() < deadline {
            if UnixStream::connect(&socket).is_ok() {
                return AcceptanceDaemon(child);
            }
            if child.try_wait().unwrap().is_some() {
                panic!("acceptance Runtime daemon exited before becoming ready");
            }
            thread::sleep(Duration::from_millis(20));
        }
        let _ = child.kill();
        let _ = child.wait();
        panic!("acceptance Runtime daemon did not become ready");
    }

    #[test]
    fn product_default_state_has_no_generated_or_acceptance_claims() {
        let state = default_state();
        for collection in ["needs", "jobs", "apps", "install_intents"] {
            assert_eq!(
                state[collection],
                json!([]),
                "{collection} must start empty"
            );
        }
        assert_eq!(state["meta"]["source"], "product-default");
        assert_eq!(state["meta"]["demo_mode"], false);
        for claim in [
            "generated_source_present",
            "build_history_present",
            "candidate_present",
            "acceptance_history_present",
            "launch_available",
        ] {
            assert_eq!(state["meta"][claim], false, "{claim} must be false");
        }
    }

    #[test]
    fn trusted_window_identity_requires_active_package_and_generation_binding() {
        let digest = "a".repeat(64);
        let item = json!({
            "app_id": "ai.vibapp.clock",
            "display_name": "Clock",
            "version": "1.0.0",
            "installation_state": "installed",
            "enabled": true,
            "package_digest_sha256": digest,
            "active_package_digest_sha256": "a".repeat(64),
            "publisher": "VibApp Local Builder",
            "permissions": ["clock", "kv"],
            "publication_badge": "private",
        });
        let descriptor = json!({
            "id": "ai.vibapp.clock",
            "display_name": "Clock",
            "version": "1.0.0",
        });
        let binding = json!({
            "package_digest_sha256": "a".repeat(64),
            "generation": "gen:7",
        });
        let identity = trusted_installed_identity(&item, &descriptor, &binding).unwrap();
        assert_eq!(identity["publisher"], "VibApp Local Builder");
        assert_eq!(identity["permission_count"], 2);
        assert_eq!(identity["generation"], "gen:7");

        let mut stale = item;
        stale["active_package_digest_sha256"] = json!("b".repeat(64));
        assert!(trusted_installed_identity(&stale, &descriptor, &binding).is_err());
    }

    #[test]
    fn demo_fixture_is_test_only_and_not_a_product_default() {
        let state: Value = serde_json::from_str(DEMO_FIXTURE).expect("fixture must be JSON");
        let apps = state["apps"].as_array().expect("apps must be an array");
        assert!(
            !apps.is_empty(),
            "test demo must remain distinguishable from product default"
        );
        assert_eq!(state["meta"]["source"], "local-fixture");
        assert_ne!(state, default_state());
    }

    #[test]
    fn development_submit_accepts_snake_and_camel_case_authority_fields() {
        for value in [
            json!({
                "task": {}, "registry": {}, "explicit_user_submit": true,
                "acknowledge_external_cost": true,
                "retry_task_id": "development-0123456789abcdef0123456789abcdef"
            }),
            json!({
                "task": {}, "registry": {}, "explicitUserSubmit": true,
                "acknowledgeExternalCost": true,
                "retryTaskId": "development-0123456789abcdef0123456789abcdef"
            }),
        ] {
            let input: DevelopmentSubmitInput = serde_json::from_value(value).unwrap();
            assert!(input.explicit_user_submit);
            assert!(input.acknowledge_external_cost);
            assert_eq!(
                input.retry_task_id.as_deref(),
                Some("development-0123456789abcdef0123456789abcdef")
            );
        }
    }

    #[test]
    fn development_submit_defaults_external_cost_authority_to_denied() {
        let input: DevelopmentSubmitInput = serde_json::from_value(json!({
            "task": {}, "registry": {}, "explicit_user_submit": true
        }))
        .unwrap();
        assert!(!input.acknowledge_external_cost);
    }

    #[test]
    fn edited_need_accepts_snake_and_camel_case_binding_fields() {
        for value in [
            json!({
                "title": "Todo", "description": "做一个可以离线保存事项的应用。",
                "embedding_consent": false, "retry_task_id": "development-0123456789abcdef0123456789abcdef",
                "need_id": "need-1234"
            }),
            json!({
                "title": "Todo", "description": "做一个可以离线保存事项的应用。",
                "embeddingConsent": false, "retryTaskId": "development-0123456789abcdef0123456789abcdef",
                "needId": "need-1234"
            }),
        ] {
            let input: NeedInput = serde_json::from_value(value).unwrap();
            assert_eq!(
                input.retry_task_id.as_deref(),
                Some("development-0123456789abcdef0123456789abcdef")
            );
            assert_eq!(input.need_id.as_deref(), Some("need-1234"));
        }
    }

    #[test]
    fn collaboration_policy_rejects_local_uuid_and_unbound_messages() {
        assert!(collaboration_uuid("00000000-0000-0000-0000-000000000000", "channel").is_err());
        assert!(collaboration_app_id("../escape").is_err());
        assert!(collaboration_expiry(59_999).is_err());
        assert!(collaboration_expiry(60_000).is_ok());
        let binding = CollaborationSessionBinding {
            app_id: "ai.vibapp.fixture".into(),
            channel_id: "20000000-0000-4000-8000-000000000001".into(),
            expires_at: unix_ms_u64() + 60_000,
            received_event_ids: VecDeque::new(),
        };
        let message = json!({
            "schema_version": "vibapp.collaboration-event.experimental-v1",
            "app_id": binding.app_id,
            "channel_id": binding.channel_id,
            "event_id": "10000000-0000-4000-8000-000000000001",
            "sent_at": unix_ms_u64(),
            "payload": {"move": 3},
        });
        let encoded = BASE64_STANDARD.encode(serde_json::to_vec(&message).unwrap());
        assert_eq!(
            validate_received_collaboration_event(&encoded, &binding),
            Some(message.clone())
        );
        let mut wrong_app = message;
        wrong_app["app_id"] = json!("other.app");
        assert!(
            validate_received_collaboration_event(
                &BASE64_STANDARD.encode(serde_json::to_vec(&wrong_app).unwrap()),
                &binding,
            )
            .is_none()
        );

        let mut received = VecDeque::new();
        assert!(remember_received_event_id(
            &mut received,
            "10000000-0000-4000-8000-000000000001"
        ));
        assert!(!remember_received_event_id(
            &mut received,
            "10000000-0000-4000-8000-000000000001"
        ));
        for index in 0..300 {
            assert!(remember_received_event_id(
                &mut received,
                &format!("event-{index}")
            ));
        }
        assert_eq!(received.len(), 256);
    }

    #[test]
    fn automatic_p2p_seeding_requires_public_appstore_publication() {
        let public = json!({
            "app_id": "ai.vibapp.public.todo",
            "package_digest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "verification_state": "verified",
            "publication_state": "published",
            "publication_badge": "public-appstore",
            "install_eligible": true,
        });
        assert!(public_package_is_seedable(&public));
        assert!(public_package_is_downloadable(
            &public,
            "ai.vibapp.public.todo",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ));
        for private_variant in [
            json!({
                "app_id": "ai.vibapp.public.todo",
                "package_digest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "verification_state": "verified",
                "publication_state": "private",
                "publication_badge": "private",
                "install_eligible": true,
            }),
            json!({
                "app_id": "ai.vibapp.public.todo",
                "package_digest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "verification_state": "verified",
                "publication_state": "published",
                "publication_badge": "private",
                "install_eligible": true,
            }),
            json!({
                "app_id": "ai.vibapp.public.todo",
                "package_digest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "verification_state": "experimental-qa-passed",
                "publication_state": "published",
                "publication_badge": "public-appstore",
                "install_eligible": true,
            }),
        ] {
            assert!(!public_package_is_seedable(&private_variant));
            assert!(!public_package_is_downloadable(
                &private_variant,
                "ai.vibapp.public.todo",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ));
        }
        let mut non_installable = public;
        non_installable["install_eligible"] = json!(false);
        assert!(!public_package_is_downloadable(
            &non_installable,
            "ai.vibapp.public.todo",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ));
        let transfer = public_network_node_fetch_status(None);
        assert_eq!(transfer["state"], "running");
        assert_eq!(transfer["activeTransfers"], 1);
        assert_eq!(transfer["transport"]["operation"], "fetch-public-package");
    }

    #[cfg(unix)]
    #[test]
    #[ignore = "real two-process RoomHash transfer plus verifier and Runtime daemon acceptance"]
    fn real_public_p2p_candidate_reaches_disabled_runtime_install() {
        const APP_ID: &str = "ai.vibapp.hello";
        const PACKAGE_DIGEST: &str =
            "dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674";
        let seeder_data = acceptance_root("s");
        let receiver_data = acceptance_root("r");
        let public_registry_root = std::env::var_os("VIBAPP_ACCEPTANCE_PUBLIC_REGISTRY_ROOT")
            .map(PathBuf::from)
            .expect(
                "VIBAPP_ACCEPTANCE_PUBLIC_REGISTRY_ROOT must point to an isolated exact public-data fixture",
            );
        assert!(public_registry_root.is_absolute());
        let source_candidate = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!(
            "../../app-builder/demo-output/pipeline/candidates/{PACKAGE_DIGEST}/candidate.json"
        ));
        assert!(source_candidate.is_file());

        let seeded_ingest = local_product::ingest(
            &seeder_data,
            source_candidate
                .to_str()
                .expect("candidate path must be UTF-8"),
        )
        .unwrap();
        assert_eq!(
            seeded_ingest["record"]["digests"]["package_sha256"],
            PACKAGE_DIGEST
        );
        let seed_candidate = local_product::store_root(&seeder_data)
            .join("candidates")
            .join(PACKAGE_DIGEST)
            .join("candidate.json");
        assert!(seed_candidate.is_file());

        let config = roomhash_host::RoomHashConfig::try_from_value(
            &network_settings::roomhash_host_config(&seeder_data).unwrap(),
        )
        .unwrap();
        let mut seeder = roomhash_host::RoomHashHost::spawn(&seeder_data).unwrap();
        seeder.start(&config).unwrap();
        let seeded = seeder
            .seed_package(&seed_candidate, PACKAGE_DIGEST)
            .unwrap();
        assert_eq!(seeded["operation"], "seed-package");
        assert_eq!(seeded["package_digest_sha256"], PACKAGE_DIGEST);
        let public_apps =
            public_registry_sync::sync_from_root(&receiver_data, &public_registry_root).unwrap();
        let selected = public_apps
            .into_iter()
            .find(|item| item["app_id"] == APP_ID)
            .expect("strict Registry projection must expose the acceptance application");
        assert_eq!(selected["package_digest_sha256"], PACKAGE_DIGEST);
        assert_eq!(selected["verification_state"], "verified");
        assert_eq!(selected["publication_badge"], "public-appstore");
        assert_eq!(selected["install_eligible"], true);
        let accepted_locator = roomhash_host::read_package_locator(&receiver_data, PACKAGE_DIGEST)
            .unwrap()
            .expect("strict Registry sync must persist the accepted locator");
        assert_eq!(accepted_locator["info_hash"], seeded["info_hash"]);
        let receiver_network = NetworkNodeState::default();
        let transfer = fetch_public_candidate(
            &receiver_data,
            &receiver_network,
            &selected,
            APP_ID,
            PACKAGE_DIGEST,
        )
        .unwrap();
        assert_eq!(transfer["status"], "downloaded-and-verified");
        assert_eq!(transfer["packageDigestSha256"], PACKAGE_DIGEST);
        assert_eq!(transfer["infoHash"], seeded["info_hash"]);
        assert_eq!(transfer["sizeBytes"], seeded["size_bytes"]);
        assert!(local_candidate_is_ready(&receiver_data, APP_ID, PACKAGE_DIGEST).unwrap());

        let daemon = start_acceptance_daemon(&receiver_data);
        let installation = local_product::install(&receiver_data, APP_ID, PACKAGE_DIGEST).unwrap();
        assert_eq!(installation["installation_performed"], true);
        assert_eq!(installation["status"], "installed-disabled");
        let installed = local_product::catalog(&receiver_data)
            .unwrap()
            .into_iter()
            .find(|item| item["app_id"] == APP_ID)
            .expect("installed app must appear in the local catalog");
        assert_eq!(installed["installation_state"], "installed-disabled");
        assert_eq!(installed["enabled"], false);
        assert_eq!(installed["launch_eligible"], false);

        println!(
            "{}",
            serde_json::to_string(&json!({
                "schema_version": "vibapp.roomhash-public-flow-acceptance.experimental-v1",
                "result": "pass",
                "fixture_scope": "isolated-synthetic-public-acceptance-real-transport",
                "genuine_registry_publication_tested": false,
                "strict_registry_locator_sync_tested": true,
                "app_id": APP_ID,
                "package_digest_sha256": PACKAGE_DIGEST,
                "info_hash": seeded["info_hash"],
                "file_count": seeded["files"].as_array().map(Vec::len).unwrap_or(0),
                "size_bytes": seeded["size_bytes"],
                "transport": transfer,
                "installation_state": installed["installation_state"],
                "launch_eligible": installed["launch_eligible"],
                "seeder_data": seeder_data,
                "receiver_data": receiver_data,
            }))
            .unwrap()
        );

        drop(daemon);
        drop(receiver_network);
        seeder.shutdown().unwrap();
    }

    #[test]
    fn production_context_embeds_the_shared_gui_assets() {
        let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        assert!(
            !tauri::is_dev(),
            "direct Cargo builds must use Tauri's embedded custom protocol"
        );
        let index = context
            .assets()
            .get(&tauri::utils::assets::AssetKey::from("index.html"))
            .expect("index.html must be embedded");
        let script = context
            .assets()
            .get(&tauri::utils::assets::AssetKey::from("app.js"))
            .expect("app.js must be embedded");
        let stylesheet = context
            .assets()
            .get(&tauri::utils::assets::AssetKey::from("styles.css"))
            .expect("styles.css must be embedded");
        assert!(
            index
                .windows(b"native-shell".len())
                .any(|item| item == b"native-shell")
        );
        assert!(
            script
                .windows(b"LAUNCHER + APPSTORE".len())
                .any(|item| item == b"LAUNCHER + APPSTORE")
        );
        assert!(
            stylesheet
                .windows(b".native-shell".len())
                .any(|item| item == b".native-shell")
        );
    }
}
