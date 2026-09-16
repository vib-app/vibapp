use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, SystemTime};

use crate::model_settings::{
    EmbeddingRuntimeSettings, GenerationRuntimeSettings, RuntimeModelSettings,
};
use crate::native_platform;

#[path = "cloud_task_preview.rs"]
mod cloud_task_preview;

const MAX_REGISTRY_OUTPUT_BYTES: usize = 512 * 1024;
const MAX_ANALYZER_OUTPUT_BYTES: usize = 64 * 1024;
const MAX_REGISTRY_REQUEST_BYTES: u64 = 2 * 1024 * 1024;
const MAX_REGISTRY_REQUEST_FILES: usize = 200;
const MAX_HISTORY_BYTES: u64 = 2 * 1024 * 1024;
const MAX_HISTORY_RECORD_BYTES: usize = 256 * 1024;
const MAX_PRODUCT_ASSESSMENT_RECORD_BYTES: u64 = 64 * 1024;
const MAX_PRODUCT_ASSESSMENT_TASKS: usize = 64;
const MAX_TRUSTED_REGISTRY_EVIDENCE_BYTES: u64 = 640 * 1024;
const MAX_TRUSTED_REGISTRY_EVIDENCE_DIRECTORY_BYTES: u64 = 16 * 1024 * 1024;
const MAX_TRUSTED_REGISTRY_EVIDENCE_FILES: usize = 64;
const MAX_TRANSCRIPT_CONTENT_CHARS: usize = 4_000;
const TRANSCRIPT_SCHEMA: &str = "vibapp.conversation-entry.experimental-v1";
const PRODUCT_ASSESSMENT_TASK_SCHEMA: &str = "vibapp.product-assessment-task.experimental-v1";
const PRODUCT_ASSESSMENT_ATTEMPT_SCHEMA: &str = "vibapp.product-assessment-attempt.experimental-v1";
const AUTHORITATIVE_REGISTRY_ROUTE_SCHEMA: &str = "vibapp.registry-route.experimental.2026-08-24.1";
const AUTHORITATIVE_REGISTRY_STATUS: &str = "experimental-product-hold";
const TRUSTED_REGISTRY_EVIDENCE_SCHEMA: &str =
    "vibapp.registry-development-evidence.experimental-v1";
const CLOUD_CODEAGENT_TASK_SCHEMA: &str = "vibapp.cloud-codeagent-task.experimental-v3";
const UNSUPPORTED_CAPABILITY_REASON: &str = "unsupported-capability-combination";
const REGISTRY_TIMEOUT: Duration = Duration::from_secs(8);
const ANALYZER_TIMEOUT: Duration = Duration::from_secs(62);
const INTERFACE_PREFIX: &str = "vibapp:experimental-v0/";
const INTERFACE_SUFFIX: &str = "@0.0.1";

const SUPPORTED_CAPABILITIES: &[&str] = &[
    "clock",
    "scheduler",
    "notification",
    "kv",
    "log",
    "host-info",
    "settings",
    "system-metrics",
    "http",
];

#[derive(Clone, Deserialize)]
pub struct CompleteNeedInput {
    pub need_id: String,
    pub app_kind: String,
    pub capabilities: Vec<String>,
    pub allowed_permissions: Vec<String>,
    pub forbidden_permissions: Vec<String>,
    pub permission_ceiling_confirmed: bool,
    pub negative_constraints: String,
    pub negative_constraints_confirmed: bool,
    pub network_mode: String,
    pub package_id: String,
    pub package_name: String,
    pub package_version: String,
    pub acceptance_example: String,
    pub proceed_after_recommendation: bool,
    pub registry_embedding_consent: bool,
    pub remote_processing_consent: bool,
    pub public_publication_consent: bool,
}

fn interface(short: &str) -> String {
    format!("{INTERFACE_PREFIX}{short}{INTERFACE_SUFFIX}")
}

#[derive(Clone, Copy)]
struct NeedShape {
    app_kind: &'static str,
    world: &'static str,
    allowed_interfaces: &'static [&'static str],
    reason: &'static str,
}

fn shape_for_kind(kind: &str) -> Option<NeedShape> {
    match kind {
        "hybrid" => Some(NeedShape {
            app_kind: "hybrid",
            world: "hybrid-reference",
            allowed_interfaces: &[
                "clock",
                "scheduler",
                "notification",
                "kv",
                "log",
                "host-info",
                "settings",
            ],
            reason: "需求同时包含可见交互和持续后台行为；Registry 将 hybrid 作为硬约束。",
        }),
        "service" => Some(NeedShape {
            app_kind: "service",
            world: "service-only-reference",
            allowed_interfaces: &[
                "clock",
                "scheduler",
                "kv",
                "log",
                "host-info",
                "settings",
                "system-metrics",
                "http",
            ],
            reason: "需求以持续或定时行为为主；Registry 将 service 作为硬约束。",
        }),
        "ui" => Some(NeedShape {
            app_kind: "ui",
            world: "ui-only-reference",
            allowed_interfaces: &["clock", "kv", "log", "host-info", "settings"],
            reason: "需求以可见交互为主；Registry 将 ui 作为硬约束。",
        }),
        _ => None,
    }
}

fn infer_shape(description: &str) -> NeedShape {
    let normalized = description.to_lowercase();
    let ui = [
        "界面", "页面", "窗口", "表单", "看板", "前端", "desktop", " ui",
    ]
    .iter()
    .any(|keyword| normalized.contains(keyword));
    let service = [
        "后台", "定时", "服务", "接口", "api", "监听", "提醒", "同步", "预约", "日程", "calendar",
    ]
    .iter()
    .any(|keyword| normalized.contains(keyword));

    shape_for_kind(match (ui, service) {
        (true, true) => "hybrid",
        (false, true) => "service",
        _ => "ui",
    })
    .expect("closed app kind")
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

fn civil_from_days(days_since_epoch: i64) -> (i64, u32, u32) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month as u32, day as u32)
}

fn utc_instant_from_seconds(seconds: i64) -> String {
    let days = seconds / 86_400;
    let second_of_day = seconds % 86_400;
    let (year, month, day) = civil_from_days(days);
    let hour = second_of_day / 3_600;
    let minute = (second_of_day % 3_600) / 60;
    let second = second_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

fn utc_instant_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    utc_instant_from_seconds(seconds)
}

#[cfg(test)]
fn build_need_spec(need_id: &str, description: &str, shape: NeedShape) -> Value {
    build_need_spec_from_analysis(need_id, description, shape, None)
}

fn analysis_text<'a>(analysis: Option<&'a Value>, key: &str) -> Option<&'a str> {
    analysis?
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
}

fn build_need_spec_from_analysis(
    need_id: &str,
    description: &str,
    shape: NeedShape,
    analysis: Option<&Value>,
) -> Value {
    let (os, arch) = current_platform();
    let analyzed_offline = analysis_text(analysis, "network_mode") == Some("offline");
    let forbidden_interfaces = if analyzed_offline
        || description.contains("不联网")
        || description.contains("无网络")
        || description.to_lowercase().contains("no network")
    {
        vec![interface("http")]
    } else {
        Vec::new()
    };
    let negative_constraints = forbidden_interfaces
        .iter()
        .map(|value| {
            json!({
                "constraint_id": "constraint.no-http",
                "kind": "forbidden-capability",
                "value": value,
                "source": "user"
            })
        })
        .collect::<Vec<_>>();
    let analyzed_negative_constraints = analysis
        .and_then(|value| value.get("negative_constraints"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .enumerate()
        .map(|(index, value)| {
            json!({
                "constraint_id": format!("constraint.ai-proposal-{index}"),
                "kind": "user-review-required",
                "value": value,
                "source": "llm-proposal"
            })
        })
        .collect::<Vec<_>>();
    let mut all_negative_constraints = negative_constraints;
    all_negative_constraints.extend(analyzed_negative_constraints);
    let goal = analysis_text(analysis, "goal_summary").unwrap_or(description);
    let acceptance_examples = analysis_text(analysis, "acceptance_example")
        .map(|value| vec![value])
        .unwrap_or_default();
    // OS/architecture are an internal Registry admission context for the current
    // Client. The generated product target is VibApp itself, not a user-selected OS.
    let platform_os = os;
    let platform_arch = arch;
    let profile = "desktop";
    let proposed_capabilities = analysis
        .map(|value| analysis_strings(value, "capabilities"))
        .unwrap_or_default()
        .into_iter()
        .collect::<BTreeSet<_>>();
    let allowed_interfaces = shape
        .allowed_interfaces
        .iter()
        .filter(|name| {
            proposed_capabilities.contains::<str>(**name)
                && !(**name == "http" && !forbidden_interfaces.is_empty())
        })
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    json!({
        "schema_version": "vibapp.need-spec.product-v0.0.1",
        "document_type": "need-spec",
        "need_id": need_id,
        "owner": {"principal_id": "desktop.local.user", "principal_kind": "user"},
        "goal": goal,
        "requirements": [{
            "requirement_id": "requirement.primary",
            // Summaries aid discovery; the authoring contract retains every
            // original requirement, including behavior and boundary conditions.
            "text": description,
            "priority": "must-have",
            "acceptance_examples": acceptance_examples
        }],
        "negative_constraints": all_negative_constraints,
        "platforms": [{"os": platform_os, "arch": platform_arch, "profile": profile}],
        "profiles": [profile],
        "permission_ceiling": {
            "allowed_interfaces": allowed_interfaces,
            "forbidden_interfaces": forbidden_interfaces,
            "maximum_scope_digests": []
        },
        "privacy_requirement": "remote-private",
        "created_at_utc": utc_instant_now(),
        "revision": 1
    })
}

fn registry_script() -> Result<PathBuf, String> {
    native_platform::resource_file(
        "registry/registry_service.py",
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../registry/registry_service.py"),
    )
}

fn analyzer_script() -> Result<PathBuf, String> {
    native_platform::resource_file(
        "need-analyzer/need_analyzer.py",
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../need-analyzer/need_analyzer.py"),
    )
}

fn write_private_json(path: &Path, value: &Value) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "Registry 请求没有父目录。".to_string())?;
    let mut file_count = 0usize;
    let mut total_bytes = 0u64;
    for entry in
        fs::read_dir(parent).map_err(|error| format!("无法检查 Registry 请求目录：{error}"))?
    {
        let entry = entry.map_err(|error| format!("无法检查 Registry 请求项：{error}"))?;
        let metadata = entry
            .metadata()
            .map_err(|error| format!("无法检查 Registry 请求大小：{error}"))?;
        if metadata.is_file() {
            file_count += 1;
            total_bytes = total_bytes.saturating_add(metadata.len());
        }
    }
    if file_count >= MAX_REGISTRY_REQUEST_FILES || total_bytes >= MAX_REGISTRY_REQUEST_BYTES {
        return Err("Registry 请求历史已达到 200 个文件或 2 MiB 上限，请先归档。".to_string());
    }
    let encoded =
        serde_json::to_vec(value).map_err(|error| format!("无法序列化 NeedSpec：{error}"))?;
    if encoded.len() > 64 * 1024
        || total_bytes.saturating_add(encoded.len() as u64 + 1) > MAX_REGISTRY_REQUEST_BYTES
    {
        return Err("NeedSpec 或 Registry 请求目录超过资源上限。".to_string());
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(path)
        .map_err(|error| format!("无法写入 Registry 请求：{error}"))?;
    file.write_all(&encoded)
        .map_err(|error| format!("无法写入 NeedSpec：{error}"))?;
    file.write_all(b"\n")
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法持久化 NeedSpec：{error}"))
}

fn read_child_output(
    child: std::process::Child,
) -> Result<(std::process::ExitStatus, Vec<u8>, Vec<u8>), String> {
    read_child_output_with_timeout(child, REGISTRY_TIMEOUT, "Registry")
}

fn read_child_output_with_timeout(
    mut child: std::process::Child,
    timeout: Duration,
    label: &str,
) -> Result<(std::process::ExitStatus, Vec<u8>, Vec<u8>), String> {
    let deadline = SystemTime::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut stdout = Vec::new();
                let mut stderr = Vec::new();
                if let Some(pipe) = child.stdout.take() {
                    pipe.take((MAX_REGISTRY_OUTPUT_BYTES + 1) as u64)
                        .read_to_end(&mut stdout)
                        .map_err(|error| format!("无法读取 {label} 输出：{error}"))?;
                }
                if let Some(pipe) = child.stderr.take() {
                    pipe.take((MAX_REGISTRY_OUTPUT_BYTES + 1) as u64)
                        .read_to_end(&mut stderr)
                        .map_err(|error| format!("无法读取 {label} 诊断：{error}"))?;
                }
                return Ok((status, stdout, stderr));
            }
            Ok(None) if SystemTime::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("{label} 超时，已终止。"));
            }
            Err(error) => return Err(format!("无法读取 {label} 状态：{error}")),
        }
    }
}

fn validate_need_analysis(value: &Value) -> Result<(), String> {
    if value.get("status").and_then(Value::as_str) != Some("analyzed") {
        return Err("需求预处理器没有返回 analyzed 状态。".to_string());
    }
    let bounded = |key: &str, maximum: usize| {
        value
            .get(key)
            .and_then(Value::as_str)
            .is_some_and(|text| is_bounded_text(text, 1, maximum))
    };
    if !bounded("model", 128) || !bounded("goal_summary", 2000) {
        return Err("需求预处理器返回了无效模型或目标摘要。".to_string());
    }
    if !value.get("app_kind").is_some_and(|item| {
        item.is_null()
            || item
                .as_str()
                .is_some_and(|kind| shape_for_kind(kind).is_some())
    }) {
        return Err("需求预处理器返回了未知应用形态。".to_string());
    }
    if value
        .get("target_runtime")
        .and_then(Value::as_str)
        .is_some_and(|target| target != "vibapp-client")
    {
        return Err("需求预处理器返回了未知应用运行目标。".to_string());
    }
    let capabilities = value
        .get("capabilities")
        .and_then(Value::as_array)
        .ok_or_else(|| "需求预处理器 capabilities 不是数组。".to_string())?;
    if capabilities.len() > SUPPORTED_CAPABILITIES.len()
        || capabilities.iter().any(|item| {
            item.as_str()
                .is_none_or(|name| !SUPPORTED_CAPABILITIES.contains(&name))
        })
    {
        return Err("需求预处理器包含未知 capability。".to_string());
    }
    const ALLOWED_MISSING: &[&str] = &[
        "app-kind",
        "capabilities",
        "acceptance-criteria",
        "negative-constraints",
        "network-mode",
        "package-intent",
    ];
    let missing = value
        .get("missing_fields")
        .and_then(Value::as_array)
        .ok_or_else(|| "需求预处理器 missing_fields 不是数组。".to_string())?;
    if missing.len() > ALLOWED_MISSING.len()
        || missing.iter().any(|item| {
            item.as_str()
                .is_none_or(|name| !ALLOWED_MISSING.contains(&name))
        })
    {
        return Err("需求预处理器包含未知缺失字段。".to_string());
    }
    let questions = value
        .get("questions")
        .and_then(Value::as_array)
        .ok_or_else(|| "需求预处理器 questions 不是数组。".to_string())?;
    if questions.len() > 7
        || questions.iter().any(|item| {
            item.as_str()
                .is_none_or(|text| !is_bounded_text(text, 1, 240))
        })
    {
        return Err("需求预处理器问题列表无效。".to_string());
    }
    Ok(())
}

fn call_need_analyzer(
    title: &str,
    description: &str,
    settings: &GenerationRuntimeSettings,
) -> Result<Value, String> {
    let input = serde_json::to_vec(&json!({
        "title": title,
        "description": description,
        "model_config": settings
    }))
    .map_err(|error| format!("无法序列化需求预处理请求：{error}"))?;
    let mut child = Command::new(native_platform::python_executable(false)?)
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(analyzer_script()?)
        .arg("analyze")
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
        .map_err(|error| format!("无法启动需求预处理器：{error}"))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "无法打开需求预处理器输入。".to_string())?;
    stdin
        .write_all(&input)
        .map_err(|error| format!("无法写入需求预处理器：{error}"))?;
    drop(stdin);
    let (status, stdout, stderr) =
        read_child_output_with_timeout(child, ANALYZER_TIMEOUT, "需求预处理")?;
    if stdout.len() > MAX_ANALYZER_OUTPUT_BYTES || stderr.len() > MAX_ANALYZER_OUTPUT_BYTES {
        return Err("需求预处理器输出超过 64 KiB 上限。".to_string());
    }
    if !status.success() {
        let diagnostic = String::from_utf8_lossy(&stderr)
            .trim()
            .chars()
            .take(240)
            .collect::<String>();
        return Err(if diagnostic.is_empty() {
            "局域网 LLM 暂时不可用或返回无效草稿。".to_string()
        } else {
            format!("局域网 LLM 草稿未通过预处理：{diagnostic}")
        });
    }
    let value: Value = serde_json::from_slice(&stdout)
        .map_err(|error| format!("需求预处理器返回了无效 JSON：{error}"))?;
    validate_need_analysis(&value)?;
    Ok(value)
}

fn call_registry(
    need_path: &Path,
    shape: NeedShape,
    required_capabilities: &[String],
    embedding_consent: bool,
    settings: &EmbeddingRuntimeSettings,
) -> Result<Value, String> {
    let config =
        serde_json::to_vec(settings).map_err(|error| format!("无法序列化向量模型设置：{error}"))?;
    if config.is_empty() || config.len() > 32 * 1024 {
        return Err("向量模型设置超过 32 KiB 上限。".to_string());
    }
    let mut command = Command::new(native_platform::python_executable(false)?);
    command
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(registry_script()?)
        .arg("search")
        .arg("--need")
        .arg(need_path)
        .arg("--limit")
        .arg("5")
        .arg("--kind")
        .arg(shape.app_kind)
        .arg("--embedding-config-stdin");
    for required in required_capabilities {
        command.arg("--required-interface").arg(required);
    }
    if embedding_consent {
        command.arg("--embedding-consent");
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
        .map_err(|error| format!("无法启动 Registry CLI：{error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "无法打开 Registry 模型设置输入。".to_string())?
        .write_all(&config)
        .map_err(|error| format!("无法发送 Registry 模型设置：{error}"))?;
    let (status, stdout, stderr) = read_child_output(child)?;
    if stdout.len() > MAX_REGISTRY_OUTPUT_BYTES || stderr.len() > MAX_REGISTRY_OUTPUT_BYTES {
        return Err("Registry 输出超过 512 KiB 上限。".to_string());
    }
    if !status.success() {
        return Err("Registry 拒绝了这份 NeedSpec；请补充更明确的需求。".to_string());
    }
    let value: Value = serde_json::from_slice(&stdout)
        .map_err(|error| format!("Registry 返回了无效 JSON：{error}"))?;
    validate_registry_route(&value)?;
    Ok(value)
}

fn validate_registry_route(value: &Value) -> Result<(), String> {
    let route = value
        .get("route")
        .and_then(Value::as_str)
        .ok_or_else(|| "Registry 缺少 route。".to_string())?;
    if !matches!(route, "recommendation" | "refinement") {
        return Err("Registry 返回了未知 route。".to_string());
    }
    let recommendations = value
        .get("recommendations")
        .and_then(Value::as_array)
        .ok_or_else(|| "Registry recommendations 不是数组。".to_string())?;
    if recommendations.len() > 20 || (route == "recommendation" && recommendations.is_empty()) {
        return Err("Registry 推荐数量无效。".to_string());
    }
    if route == "refinement" && !recommendations.is_empty() {
        return Err("refinement 不得携带可安装推荐。".to_string());
    }
    let handoff = value
        .get("codeagent_handoff")
        .and_then(Value::as_object)
        .ok_or_else(|| "Registry 缺少 CodeAgent 边界声明。".to_string())?;
    if handoff.get("created").and_then(Value::as_bool) != Some(false)
        || handoff.get("permitted").and_then(Value::as_bool) != Some(false)
    {
        return Err("Registry 试图越权创建开发任务，已拒绝。".to_string());
    }
    Ok(())
}

fn safe_refinement(need_id: &str, message: String) -> Value {
    json!({
        "schema_version": "vibapp.registry-route.desktop-fallback.v1",
        "status": "experimental-product-hold",
        "route": "refinement",
        "request_id": format!("desktop-registry-{need_id}"),
        "need_id": need_id,
        "recommendations": [],
        "refinement": {
            "reason_code": "registry-unavailable",
            "message": message
        },
        "retrieval": {
            "mode": "registry-cli-failed-closed",
            "embedding": {"status": "not-called"}
        },
        "rejected": [],
        "codeagent_handoff": {
            "created": false,
            "permitted": false,
            "reason": "Desktop failed closed; no development task was created."
        }
    })
}

fn refinement_questions(reason_code: &str) -> Vec<&'static str> {
    match reason_code {
        "embedding-consent-required" => vec![
            "是否允许把这条需求发送到局域网语义检索服务？这只用于匹配现有应用。",
            "如果不允许，请补充最重要的功能关键词和不能使用的权限。",
        ],
        "no-hard-filter-match" => vec![
            "这个 VibApp 需要可见界面、后台服务，还是两者都要？",
            "哪些能力是必须的，哪些权限明确不能授予？宿主兼容由 VibApp Client 自动处理。",
        ],
        "no-acceptable-semantic-match" => vec![
            "请把必须完成的输入、输出和成功标准各写一条。",
            "现有应用差在哪里：缺功能、权限过多，还是平台不支持？",
        ],
        _ => vec![
            "请确认应用形态、必须功能和禁止权限。",
            "Registry 暂不可用时不会自动转入云开发，请稍后重试。",
        ],
    }
}

fn append_history(path: &Path, value: &Value) -> Result<(), String> {
    let encoded =
        serde_json::to_vec(value).map_err(|error| format!("无法序列化需求历史：{error}"))?;
    if encoded.len() > MAX_HISTORY_RECORD_BYTES {
        return Err("单条本地需求历史超过 256 KiB 上限。".to_string());
    }
    let current_bytes = match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
                return Err("本地需求历史必须是普通非符号链接文件。".to_string());
            }
            metadata.len()
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(format!("无法检查需求历史：{error}")),
    };
    if current_bytes.saturating_add(encoded.len() as u64 + 1) > MAX_HISTORY_BYTES {
        return Err("本地需求历史已达到 2 MiB 上限，请先归档。".to_string());
    }
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(path)
        .map_err(|error| format!("无法打开需求历史：{error}"))?;
    file.write_all(&encoded)
        .map_err(|error| format!("无法写入需求历史：{error}"))?;
    file.write_all(b"\n")
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法持久化需求历史：{error}"))
}

fn transcript_entry(
    need_id: &str,
    task_id: Option<&str>,
    attempt_id: Option<&str>,
    role: &str,
    kind: &str,
    content: &str,
    now: u128,
    ordinal: u8,
    snapshot: Option<Value>,
) -> Result<Value, String> {
    if !valid_need_id(need_id)
        || !matches!(role, "user" | "assistant" | "system")
        || !is_bounded_text(kind, 1, 64)
        || !is_bounded_text(content, 1, MAX_TRANSCRIPT_CONTENT_CHARS)
        || task_id.is_some_and(|value| value.len() > 96 || value.chars().any(char::is_control))
        || attempt_id.is_some_and(|value| value.len() > 96 || value.chars().any(char::is_control))
    {
        return Err("对话记录身份、类型或内容超出边界。".to_string());
    }
    if snapshot.as_ref().is_some_and(|value| {
        !value.is_object()
            || serde_json::to_vec(value)
                .map(|bytes| bytes.len() > MAX_HISTORY_RECORD_BYTES / 2)
                .unwrap_or(true)
    }) {
        return Err("对话快照超过 128 KiB 上限或不是对象。".to_string());
    }
    let sequence = now
        .checked_mul(10)
        .and_then(|value| value.checked_add(u128::from(ordinal)))
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| "对话序号超过 u64 上限。".to_string())?;
    Ok(json!({
        "schema_version": TRANSCRIPT_SCHEMA,
        "entry_id": format!("conversation-{need_id}-{now:x}-{ordinal:02x}"),
        "sequence": sequence,
        "need_id": need_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "role": role,
        "kind": kind,
        "content": content,
        "created_at_unix_ms": now,
        "snapshot": snapshot
    }))
}

fn bounded_assistant_content(value: &str, fallback: &str) -> String {
    let selected = if value.trim().is_empty() {
        fallback
    } else {
        value
    };
    selected
        .chars()
        .filter(|character| *character >= ' ' || matches!(character, '\t' | '\n'))
        .take(MAX_TRANSCRIPT_CONTENT_CHARS)
        .collect()
}

fn bounded_response_snapshot(response: &Value) -> Value {
    if serde_json::to_vec(response).is_ok_and(|bytes| bytes.len() <= MAX_HISTORY_RECORD_BYTES / 2) {
        return response.clone();
    }
    json!({
        "snapshot_truncated": true,
        "need": response["need"],
        "need_spec": {
            "state": response["need_spec"]["state"],
            "inferred_app_kind": response["need_spec"]["inferred_app_kind"],
            "app_kind": response["need_spec"]["app_kind"]
        },
        "consent": response["consent"],
        "registry": {
            "route": response["registry"]["route"],
            "refinement": response["registry"]["refinement"]
        },
        "cloud_development": {
            "enabled": response["cloud_development"]["enabled"],
            "blockers": response["cloud_development"]["blockers"]
        }
    })
}

#[allow(clippy::too_many_arguments)]
fn persist_conversation_turn(
    data_dir: &Path,
    mut history: Value,
    mut response: Value,
    now: u128,
    task_id: Option<&str>,
    attempt_id: Option<&str>,
    user_kind: &str,
    user_content: &str,
    assistant_kind: &str,
    assistant_content: &str,
) -> Result<Value, String> {
    let need_id = history["need_id"]
        .as_str()
        .ok_or_else(|| "需求历史缺少 need_id。".to_string())?
        .to_string();
    // The assistant snapshot deliberately contains the typed turn response before
    // transcript attachment, preventing recursive history while retaining the exact
    // NeedSpec/Registry/consent presentation that the user actually saw.
    response["need"] = history.clone();
    let snapshot = bounded_response_snapshot(&response);
    let entries = vec![
        transcript_entry(
            &need_id,
            task_id,
            attempt_id,
            "user",
            user_kind,
            user_content,
            now,
            0,
            None,
        )?,
        transcript_entry(
            &need_id,
            task_id,
            attempt_id,
            "assistant",
            assistant_kind,
            assistant_content,
            now,
            1,
            Some(snapshot),
        )?,
    ];
    history["conversation_transcript"] = json!(entries);
    response["need"] = history.clone();
    append_history(&data_dir.join("history/need-requests.jsonl"), &history)?;
    Ok(response)
}

fn latest_retry_binding(data_dir: &Path, need_id: &str) -> (Option<String>, Option<String>) {
    let path = data_dir.join("history/need-requests.jsonl");
    let Ok(metadata) = fs::symlink_metadata(&path) else {
        return (None, None);
    };
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > MAX_HISTORY_BYTES
    {
        return (None, None);
    }
    let Ok(content) = fs::read_to_string(path) else {
        return (None, None);
    };
    content
        .lines()
        .rev()
        .take(200)
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|item| item["need_id"] == need_id)
        .filter_map(|item| {
            let task = item["retry_task_id"].as_str()?.to_string();
            let attempt = item["retry_attempt_id"].as_str().map(str::to_owned);
            Some((Some(task), attempt))
        })
        .next()
        .unwrap_or((None, None))
}

fn cloud_development_state(route: &str) -> Value {
    cloud_task_preview::initial_state(route)
}

fn local_fallback_analysis(
    title: &str,
    description: &str,
    status: &str,
    reason: &str,
    settings: &GenerationRuntimeSettings,
) -> Value {
    let shape = infer_shape(description);
    let offline = description.contains("不联网")
        || description.contains("无网络")
        || description.to_lowercase().contains("no network");
    json!({
        "schema_version": "vibapp.need-analysis.experimental-v1",
        "status": status,
        "reason": reason,
        "provider": if settings.base_url == "http://192.168.199.170:8081" { "localai-lan" } else { "openai-compatible-byom" },
        "model": settings.model,
        "endpoint": settings.base_url,
        "goal_summary": description,
        "app_kind": shape.app_kind,
        "target_runtime": "vibapp-client",
        "profile": null,
        "platform_os": null,
        "platform_arch": null,
        "capabilities": Vec::<&str>::new(),
        "acceptance_example": "",
        "negative_constraints": if offline { vec!["应用不得访问网络。"] } else { Vec::<&str>::new() },
        "network_mode": if offline { "offline" } else if shape.app_kind == "service" { "scoped-network" } else { "offline" },
        "package_name": title,
        "assumptions": [],
        "missing_fields": [
            "app-kind",
            "capabilities",
            "acceptance-criteria",
            "negative-constraints",
            "network-mode",
            "package-intent"
        ],
        "questions": if status == "not-called" {
            vec!["未授权局域网 AI 预处理，请手动补全草稿；也可以返回勾选授权后重新发送需求。"]
        } else {
            vec!["局域网 AI 本次未能返回合格草稿，请检查或补全下面的本地规则草稿。"]
        }
    })
}

fn analysis_strings(value: &Value, key: &str) -> Vec<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

fn constrain_analysis_to_shape(analysis: &mut Value, shape: NeedShape) {
    let proposed = analysis_strings(analysis, "capabilities")
        .into_iter()
        .collect::<BTreeSet<_>>();
    let capabilities = SUPPORTED_CAPABILITIES
        .iter()
        .filter(|candidate| {
            shape.allowed_interfaces.contains(candidate) && proposed.contains::<str>(**candidate)
        })
        .copied()
        .collect::<Vec<_>>();
    analysis["capabilities"] = json!(capabilities);
}

pub fn submit_need(
    data_dir: &Path,
    title: &str,
    description: &str,
    embedding_consent: bool,
    now: u128,
    model_settings: &RuntimeModelSettings,
) -> Result<Value, String> {
    let need_id = format!("need-{now:x}");
    let mut analysis = if embedding_consent {
        call_need_analyzer(title, description, &model_settings.generation).unwrap_or_else(
            |message| {
                local_fallback_analysis(
                    title,
                    description,
                    "degraded",
                    &message,
                    &model_settings.generation,
                )
            },
        )
    } else {
        local_fallback_analysis(
            title,
            description,
            "not-called",
            "lan-ai-consent-not-granted",
            &model_settings.generation,
        )
    };
    let shape = analysis
        .get("app_kind")
        .and_then(Value::as_str)
        .and_then(shape_for_kind)
        .unwrap_or_else(|| infer_shape(description));
    constrain_analysis_to_shape(&mut analysis, shape);
    let need_spec = build_need_spec_from_analysis(&need_id, description, shape, Some(&analysis));
    let requests = data_dir.join("registry-requests");
    fs::create_dir_all(&requests)
        .map_err(|error| format!("无法创建 Registry 请求目录：{error}"))?;
    let need_path = requests.join(format!("{need_id}.json"));
    write_private_json(&need_path, &need_spec)?;
    let required_capabilities = analysis_strings(&analysis, "capabilities")
        .iter()
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    let registry = call_registry(
        &need_path,
        shape,
        &required_capabilities,
        embedding_consent,
        &model_settings.embedding,
    )
    .unwrap_or_else(|message| safe_refinement(&need_id, message));
    validate_registry_route(&registry)?;
    let route = registry["route"].as_str().unwrap_or("refinement");
    let reason_code = registry["refinement"]["reason_code"]
        .as_str()
        .unwrap_or("recommendation-found");
    let mut questions = analysis_strings(&analysis, "questions");
    if questions.is_empty() && route == "refinement" && analysis["status"] != "analyzed" {
        questions.extend(
            refinement_questions(reason_code)
                .into_iter()
                .map(str::to_owned),
        );
    }
    let need_spec_state = "draft";
    let missing_fields = analysis_strings(&analysis, "missing_fields");
    let cloud_development = cloud_development_state(route);
    let need_history = json!({
        "need_id": need_id,
        "title": title,
        "description": description,
        "route": route,
        "status": if route == "recommendation" { "existing-app-found" } else { "needs-refinement" },
        "handoff": "none",
        "created_at_unix_ms": now,
        "registry_request_id": registry["request_id"],
        "codeagent_task_created": false
    });
    fs::create_dir_all(data_dir.join("history"))
        .map_err(|error| format!("无法创建需求历史目录：{error}"))?;
    let response = json!({
        "need": need_history,
        "need_spec": {
            "state": need_spec_state,
            "draft": need_spec,
            "analysis": analysis,
            "missing_fields": missing_fields,
            "review_required": ["permission-ceiling", "negative-constraints", "remote-processing", "public-publication"],
            "inferred_app_kind": shape.app_kind,
            "inferred_world": shape.world,
            "inference_reason": shape.reason
        },
        "consent": {
            "lan_llm_analysis": if embedding_consent { "granted" } else { "not-granted" },
            "registry_embedding": if embedding_consent { "granted" } else { "not-granted" },
            "cloud_remote_processing": "not-requested",
            "public_sharing": "not-requested",
            "independent_decisions": true
        },
        "registry": registry,
        "refinement_questions": questions,
        "cloud_development": cloud_development
    });
    let assistant_content = if route == "recommendation" {
        "Registry found one or more compatible existing VibApps. Review the matches before requesting development."
            .to_string()
    } else {
        response["refinement_questions"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("\n")
    };
    let assistant_content = bounded_assistant_content(
        &assistant_content,
        response["registry"]["refinement"]["message"]
            .as_str()
            .unwrap_or("The request needs more detail before Registry can decide."),
    );
    persist_conversation_turn(
        data_dir,
        need_history,
        response,
        now,
        None,
        None,
        "need-submitted",
        description,
        if route == "recommendation" {
            "registry-recommendation"
        } else {
            "assistant-refinement"
        },
        &assistant_content,
    )
}

fn latest_draft(requests: &Path, need_id: &str) -> Result<(PathBuf, Value), String> {
    let mut selected: Option<(u64, PathBuf, Value)> = None;
    let base_name = format!("{need_id}.json");
    let revision_prefix = format!("{need_id}-draft-r");
    for entry in fs::read_dir(requests)
        .map_err(|error| format!("无法读取 Registry 请求目录：{error}"))?
        .take(MAX_REGISTRY_REQUEST_FILES + 1)
    {
        let entry = entry.map_err(|error| format!("无法读取 Registry 请求项：{error}"))?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if name != base_name && (!name.starts_with(&revision_prefix) || !name.ends_with(".json")) {
            continue;
        }
        let path = entry.path();
        let metadata = path
            .symlink_metadata()
            .map_err(|error| format!("无法检查 NeedSpec 草稿：{error}"))?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() == 0
            || metadata.len() > 64 * 1024
        {
            return Err("NeedSpec 草稿历史包含不安全文件。".to_string());
        }
        let value = read_private_json(&path, 64 * 1024)?;
        if value["schema_version"] != "vibapp.need-spec.product-v0.0.1"
            || value["document_type"] != "need-spec"
            || value["need_id"] != need_id
        {
            return Err("NeedSpec 草稿历史身份不匹配。".to_string());
        }
        let revision = value["revision"]
            .as_u64()
            .filter(|revision| *revision >= 1)
            .ok_or_else(|| "NeedSpec 草稿 revision 无效。".to_string())?;
        if let Some((current_revision, _, current)) = selected.as_ref() {
            if revision == *current_revision && current != &value {
                return Err("同一 revision 存在不同的 NeedSpec 草稿，已停止重试。".to_string());
            }
        }
        if selected
            .as_ref()
            .is_none_or(|(current_revision, _, _)| revision > *current_revision)
        {
            selected = Some((revision, path, value));
        }
    }
    selected
        .map(|(_, path, value)| (path, value))
        .ok_or_else(|| "找不到可编辑的 NeedSpec 草稿。".to_string())
}

fn validate_retry_binding(
    status: &Value,
    retry_task_id: &str,
    need_id: &str,
) -> Result<(), String> {
    if status["task"]["task_id"] != retry_task_id
        || status["task"]["need_id"] != need_id
        || status["attempt"]["task_id"] != retry_task_id
        || status["attempt"]["need_id"] != need_id
    {
        return Err("retryTaskId、needId 与既有交付任务不一致。".to_string());
    }
    if status["attempt"]["status"] != "failed" {
        return Err("只有失败的最新开发尝试可以修改需求并重试。".to_string());
    }
    if !status["attempt"]["attempt_id"]
        .as_str()
        .is_some_and(|value| {
            !value.is_empty() && value.len() <= 96 && !value.chars().any(char::is_control)
        })
    {
        return Err("失败交付尝试缺少有效的 attempt_id。".to_string());
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn submit_need_retry(
    data_dir: &Path,
    title: &str,
    description: &str,
    embedding_consent: bool,
    now: u128,
    model_settings: &RuntimeModelSettings,
    retry_task_id: &str,
    need_id: &str,
    delivery_status: &Value,
) -> Result<Value, String> {
    if !valid_need_id(need_id) {
        return Err("needId 格式无效。".to_string());
    }
    validate_retry_binding(delivery_status, retry_task_id, need_id)?;
    let requests = data_dir.join("registry-requests");
    fs::create_dir_all(&requests)
        .map_err(|error| format!("无法创建 Registry 请求目录：{error}"))?;
    let (_, prior) = latest_draft(&requests, need_id)?;
    let prior_revision = prior["revision"]
        .as_u64()
        .ok_or_else(|| "既有 NeedSpec 缺少 revision。".to_string())?;
    let revision = prior_revision
        .checked_add(1)
        .ok_or_else(|| "NeedSpec revision 已达到上限。".to_string())?;

    let mut analysis = if embedding_consent {
        call_need_analyzer(title, description, &model_settings.generation).unwrap_or_else(
            |message| {
                local_fallback_analysis(
                    title,
                    description,
                    "degraded",
                    &message,
                    &model_settings.generation,
                )
            },
        )
    } else {
        local_fallback_analysis(
            title,
            description,
            "not-called",
            "lan-ai-consent-not-granted",
            &model_settings.generation,
        )
    };
    let shape = analysis
        .get("app_kind")
        .and_then(Value::as_str)
        .and_then(shape_for_kind)
        .unwrap_or_else(|| infer_shape(description));
    constrain_analysis_to_shape(&mut analysis, shape);
    let mut need_spec = build_need_spec_from_analysis(need_id, description, shape, Some(&analysis));
    need_spec["revision"] = json!(revision);
    need_spec["created_at_utc"] = prior["created_at_utc"].clone();
    let digest = cloud_task_preview::canonical_sha256(&need_spec)?;
    let draft_path = requests.join(format!(
        "{need_id}-draft-r{revision}-{}.json",
        &digest[..16]
    ));
    write_private_json(&draft_path, &need_spec)?;

    let required_capabilities = analysis_strings(&analysis, "capabilities")
        .iter()
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    let registry = call_registry(
        &draft_path,
        shape,
        &required_capabilities,
        embedding_consent,
        &model_settings.embedding,
    )
    .unwrap_or_else(|message| safe_refinement(need_id, message));
    validate_registry_route(&registry)?;
    let route = registry["route"].as_str().unwrap_or("refinement");
    let reason_code = registry["refinement"]["reason_code"]
        .as_str()
        .unwrap_or("recommendation-found");
    let mut questions = analysis_strings(&analysis, "questions");
    if questions.is_empty() && route == "refinement" && analysis["status"] != "analyzed" {
        questions.extend(
            refinement_questions(reason_code)
                .into_iter()
                .map(str::to_owned),
        );
    }
    let missing_fields = analysis_strings(&analysis, "missing_fields");
    let retry_attempt_id = delivery_status["attempt"]["attempt_id"]
        .as_str()
        .ok_or_else(|| "失败交付尝试缺少 attempt_id。".to_string())?;
    let need_history = json!({
        "need_id": need_id,
        "title": title,
        "description": description,
        "route": route,
        "status": if route == "recommendation" { "existing-app-found" } else { "needs-refinement" },
        "handoff": "none",
        "event": "need-edited-for-retry",
        "retry_task_id": retry_task_id,
        "retry_attempt_id": retry_attempt_id,
        "revision": revision,
        "previous_revision": prior_revision,
        "draft_path": draft_path.file_name().and_then(|value| value.to_str()),
        "created_at_unix_ms": now,
        "registry_request_id": registry["request_id"],
        "codeagent_task_created": false
    });
    fs::create_dir_all(data_dir.join("history"))
        .map_err(|error| format!("无法创建需求历史目录：{error}"))?;
    let response = json!({
        "need": need_history,
        "retry": {
            "task_id": retry_task_id,
            "need_id": need_id,
            "previous_revision": prior_revision,
            "draft_revision": revision,
            "requires_new_completion": true,
            "requires_new_job_and_consent": true
        },
        "need_spec": {
            "state": "draft",
            "draft": need_spec,
            "analysis": analysis,
            "missing_fields": missing_fields,
            "review_required": ["permission-ceiling", "negative-constraints", "remote-processing", "public-publication"],
            "inferred_app_kind": shape.app_kind,
            "inferred_world": shape.world,
            "inference_reason": shape.reason
        },
        "consent": {
            "lan_llm_analysis": if embedding_consent { "granted" } else { "not-granted" },
            "registry_embedding": if embedding_consent { "granted" } else { "not-granted" },
            "cloud_remote_processing": "not-requested",
            "public_sharing": "not-requested",
            "independent_decisions": true
        },
        "registry": registry,
        "refinement_questions": questions,
        "cloud_development": cloud_development_state(route)
    });
    let assistant_content = if route == "recommendation" {
        "Registry found one or more compatible existing VibApps after the requirement edit."
            .to_string()
    } else {
        response["refinement_questions"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("\n")
    };
    let assistant_content = bounded_assistant_content(
        &assistant_content,
        response["registry"]["refinement"]["message"]
            .as_str()
            .unwrap_or("The edited request needs more detail before Registry can decide."),
    );
    persist_conversation_turn(
        data_dir,
        need_history,
        response,
        now,
        Some(retry_task_id),
        Some(retry_attempt_id),
        "requirement-edited",
        description,
        if route == "recommendation" {
            "registry-recommendation"
        } else {
            "assistant-refinement"
        },
        &assistant_content,
    )
}

fn is_bounded_text(value: &str, minimum: usize, maximum: usize) -> bool {
    let length = value.chars().count();
    (minimum..=maximum).contains(&length)
        && !value
            .chars()
            .any(|character| character < ' ' && !matches!(character, '\t' | '\n'))
}

fn valid_need_id(value: &str) -> bool {
    value.strip_prefix("need-").is_some_and(|suffix| {
        (1..=32).contains(&suffix.len()) && suffix.bytes().all(|byte| byte.is_ascii_hexdigit())
    })
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

fn valid_semver(value: &str) -> bool {
    let parts = value.split('.').collect::<Vec<_>>();
    parts.len() == 3
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.bytes().all(|byte| byte.is_ascii_digit())
                && (part == &"0" || !part.starts_with('0'))
        })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SupportedListError {
    Unknown,
    Duplicate,
}

fn normalized_supported(values: &[String]) -> Result<Vec<String>, SupportedListError> {
    let mut seen = BTreeSet::new();
    let mut unknown = false;
    let mut duplicate = false;
    for value in values {
        if !SUPPORTED_CAPABILITIES.contains(&value.as_str()) {
            unknown = true;
        } else if !seen.insert(value.as_str()) {
            duplicate = true;
        }
    }
    if unknown {
        return Err(SupportedListError::Unknown);
    }
    if duplicate {
        return Err(SupportedListError::Duplicate);
    }
    Ok(SUPPORTED_CAPABILITIES
        .iter()
        .filter(|candidate| seen.contains(**candidate))
        .map(|value| (*value).to_string())
        .collect())
}

fn read_private_json(path: &Path, maximum: u64) -> Result<Value, String> {
    let metadata =
        fs::symlink_metadata(path).map_err(|error| format!("找不到本机 NeedSpec 草稿：{error}"))?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err("NeedSpec 草稿必须是普通非符号链接文件。".to_string());
    }
    if metadata.len() > maximum {
        return Err("NeedSpec 草稿超过 64 KiB 上限。".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("无法读取 NeedSpec 草稿：{error}"))?;
    serde_json::from_slice(&bytes).map_err(|error| format!("NeedSpec 草稿不是有效 JSON：{error}"))
}

fn original_route(data_dir: &Path, need_id: &str) -> Result<String, String> {
    let path = data_dir.join("history/need-requests.jsonl");
    let metadata =
        fs::symlink_metadata(&path).map_err(|error| format!("找不到需求历史：{error}"))?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > MAX_HISTORY_BYTES
    {
        return Err("需求历史不是受限普通文件。".to_string());
    }
    let content = fs::read_to_string(path).map_err(|error| format!("无法读取需求历史：{error}"))?;
    content
        .lines()
        .take(200)
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|item| item["need_id"] == need_id && item["route"].is_string())
        .and_then(|item| item["route"].as_str().map(str::to_owned))
        .filter(|route| matches!(route.as_str(), "recommendation" | "refinement"))
        .ok_or_else(|| "本机历史中没有这条需求的 Registry 路由。".to_string())
}

fn valid_sha256(value: &Value) -> bool {
    value.as_str().is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn canonical_utc_shape(value: &Value) -> bool {
    value.as_str().is_some_and(|instant| {
        instant.len() == 20
            && instant.as_bytes().get(4) == Some(&b'-')
            && instant.as_bytes().get(7) == Some(&b'-')
            && instant.as_bytes().get(10) == Some(&b'T')
            && instant.as_bytes().get(13) == Some(&b':')
            && instant.as_bytes().get(16) == Some(&b':')
            && instant.as_bytes().get(19) == Some(&b'Z')
            && instant.bytes().enumerate().all(|(index, byte)| {
                matches!(index, 4 | 7 | 10 | 13 | 16 | 19) || byte.is_ascii_digit()
            })
    })
}

fn exact_object_keys(value: &Value, expected: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == expected.len() && expected.iter().all(|key| object.contains_key(*key))
    })
}

fn authoritative_registry_refinement_binding(
    registry: &Value,
    need_spec: &Value,
    need_spec_digest: &str,
) -> Result<String, String> {
    const REQUIRED_KEYS: &[&str] = &[
        "schema_version",
        "status",
        "route",
        "request_id",
        "need_id",
        "recommendations",
        "refinement",
        "retrieval",
        "rejected",
        "codeagent_handoff",
    ];
    const ALLOWED_KEYS: &[&str] = &[
        "schema_version",
        "status",
        "route",
        "request_id",
        "need_id",
        "recommendations",
        "refinement",
        "retrieval",
        "rejected",
        "considered",
        "codeagent_handoff",
    ];
    let object = registry
        .as_object()
        .ok_or_else(|| "Registry 可信证据必须是对象。".to_string())?;
    if REQUIRED_KEYS.iter().any(|key| !object.contains_key(*key))
        || object
            .keys()
            .any(|key| !ALLOWED_KEYS.contains(&key.as_str()))
    {
        return Err("Registry 可信证据字段不完整或包含未知字段。".to_string());
    }
    let computed_need_digest = cloud_task_preview::canonical_sha256(need_spec)?;
    let need_id = need_spec["need_id"]
        .as_str()
        .filter(|value| valid_need_id(value))
        .ok_or_else(|| "Registry 可信证据无法绑定有效 NeedSpec。".to_string())?;
    let expected_request_id = format!("registry.{}", &computed_need_digest[..24]);
    let refinement = registry["refinement"]
        .as_object()
        .ok_or_else(|| "Registry 可信证据缺少 refinement。".to_string())?;
    let handoff = registry["codeagent_handoff"]
        .as_object()
        .ok_or_else(|| "Registry 可信证据缺少 CodeAgent 边界。".to_string())?;
    if need_spec_digest != computed_need_digest
        || registry["schema_version"] != AUTHORITATIVE_REGISTRY_ROUTE_SCHEMA
        || registry["status"] != AUTHORITATIVE_REGISTRY_STATUS
        || registry["route"] != "refinement"
        || registry["request_id"] != expected_request_id
        || registry["need_id"] != need_id
        || registry["recommendations"] != json!([])
        || !exact_object_keys(&registry["refinement"], &["reason_code", "message"])
        || refinement
            .get("reason_code")
            .and_then(Value::as_str)
            .is_none_or(|value| !is_bounded_text(value, 1, 128) || value == "registry-unavailable")
        || refinement
            .get("message")
            .and_then(Value::as_str)
            .is_none_or(|value| !is_bounded_text(value, 1, 4_096))
        || !registry["retrieval"].is_object()
        || registry["rejected"]
            .as_array()
            .is_none_or(|items| items.len() > 50)
        || registry
            .get("considered")
            .is_some_and(|items| items.as_array().is_none_or(|items| items.len() > 20))
        || !exact_object_keys(
            &registry["codeagent_handoff"],
            &["created", "permitted", "reason"],
        )
        || handoff.get("created").and_then(Value::as_bool) != Some(false)
        || handoff.get("permitted").and_then(Value::as_bool) != Some(false)
        || handoff
            .get("reason")
            .and_then(Value::as_str)
            .is_none_or(|value| !is_bounded_text(value, 1, 1_000))
    {
        return Err(
            "只有与完整 NeedSpec 摘要精确绑定的权威 Registry no-match 才能进入开发。".to_string(),
        );
    }
    Ok(expected_request_id)
}

fn registry_grants_development_authority(
    registry: &Value,
    need_spec: &Value,
    need_spec_digest: &str,
) -> bool {
    registry["route"] == "refinement"
        && authoritative_registry_refinement_binding(registry, need_spec, need_spec_digest).is_ok()
}

fn trusted_registry_evidence_directory(data_dir: &Path) -> Result<PathBuf, String> {
    let path = data_dir.join("registry-development-evidence");
    match fs::symlink_metadata(&path) {
        Ok(metadata) => {
            if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                return Err("Registry 可信证据目录必须是真实私有目录。".to_string());
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(&path)
                .map_err(|error| format!("无法创建 Registry 可信证据目录：{error}"))?;
            let metadata = fs::symlink_metadata(&path)
                .map_err(|error| format!("无法检查 Registry 可信证据目录：{error}"))?;
            if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                return Err("Registry 可信证据目录创建后类型无效。".to_string());
            }
        }
        Err(error) => return Err(format!("无法检查 Registry 可信证据目录：{error}")),
    }
    native_platform::protect_private_directory(&path)?;
    Ok(path)
}

fn read_trusted_registry_evidence_file(path: &Path) -> Result<Value, String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("找不到与开发任务绑定的 Registry 可信证据：{error}"))?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_TRUSTED_REGISTRY_EVIDENCE_BYTES
    {
        return Err("Registry 可信证据必须是受限普通文件。".to_string());
    }
    native_platform::verify_private_file(path)?;
    let bytes = fs::read(path).map_err(|error| format!("无法读取 Registry 可信证据：{error}"))?;
    serde_json::from_slice(&bytes)
        .map_err(|error| format!("Registry 可信证据不是有效 JSON：{error}"))
}

fn trusted_registry_evidence_record(task: &Value, registry: &Value) -> Result<Value, String> {
    if task["schema_version"] != CLOUD_CODEAGENT_TASK_SCHEMA
        || task["need_spec_complete"] != true
        || !valid_sha256(&task["immutable_task_digest_sha256"])
        || !valid_sha256(&task["need_spec_digest_sha256"])
        || !task["need_spec"].is_object()
    {
        return Err("CodeAgent task 不能绑定 Registry 可信证据。".to_string());
    }
    let immutable_task_digest = task["immutable_task_digest_sha256"]
        .as_str()
        .ok_or_else(|| "CodeAgent task 缺少 immutable digest。".to_string())?;
    let need_spec_digest = task["need_spec_digest_sha256"]
        .as_str()
        .ok_or_else(|| "CodeAgent task 缺少 NeedSpec digest。".to_string())?;
    let need_id = task["need_spec"]["need_id"]
        .as_str()
        .filter(|value| valid_need_id(value))
        .ok_or_else(|| "CodeAgent task 缺少有效 need_id。".to_string())?;
    let request_id =
        authoritative_registry_refinement_binding(registry, &task["need_spec"], need_spec_digest)?;
    let registry_digest = cloud_task_preview::canonical_sha256(registry)?;
    Ok(json!({
        "schema_version": TRUSTED_REGISTRY_EVIDENCE_SCHEMA,
        "document_type": "registry-development-evidence",
        "immutable_task_digest_sha256": immutable_task_digest,
        "need_spec_digest_sha256": need_spec_digest,
        "need_id": need_id,
        "registry_request_id": request_id,
        "registry_evidence_sha256": registry_digest,
        "registry": registry
    }))
}

fn trusted_registry_evidence_path(
    data_dir: &Path,
    task: &Value,
    registry: &Value,
) -> Result<PathBuf, String> {
    let immutable_task_digest = task["immutable_task_digest_sha256"]
        .as_str()
        .filter(|_| valid_sha256(&task["immutable_task_digest_sha256"]))
        .ok_or_else(|| "CodeAgent task immutable digest 无效。".to_string())?;
    let registry_digest = cloud_task_preview::canonical_sha256(registry)?;
    Ok(trusted_registry_evidence_directory(data_dir)?
        .join(format!("{immutable_task_digest}-{registry_digest}.json")))
}

pub fn persist_trusted_registry_development_evidence(
    data_dir: &Path,
    task: &Value,
    registry: &Value,
) -> Result<Value, String> {
    let record = trusted_registry_evidence_record(task, registry)?;
    let path = trusted_registry_evidence_path(data_dir, task, registry)?;
    match fs::symlink_metadata(&path) {
        Ok(_) => {
            if read_trusted_registry_evidence_file(&path)? != record {
                return Err("同一任务和 Registry 摘要的可信证据内容发生冲突。".to_string());
            }
            return Ok(record);
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(format!("无法检查 Registry 可信证据：{error}")),
    }
    let parent = path
        .parent()
        .ok_or_else(|| "Registry 可信证据没有父目录。".to_string())?;
    let mut file_count = 0usize;
    let mut total_bytes = 0u64;
    for entry in
        fs::read_dir(parent).map_err(|error| format!("无法枚举 Registry 可信证据：{error}"))?
    {
        let entry = entry.map_err(|error| format!("无法检查 Registry 可信证据项：{error}"))?;
        let metadata = fs::symlink_metadata(entry.path())
            .map_err(|error| format!("无法检查 Registry 可信证据项类型：{error}"))?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err("Registry 可信证据目录包含非普通文件。".to_string());
        }
        file_count += 1;
        total_bytes = total_bytes.saturating_add(metadata.len());
    }
    let encoded = serde_json::to_vec(&record)
        .map_err(|error| format!("无法序列化 Registry 可信证据：{error}"))?;
    if file_count >= MAX_TRUSTED_REGISTRY_EVIDENCE_FILES
        || encoded.len() as u64 > MAX_TRUSTED_REGISTRY_EVIDENCE_BYTES
        || total_bytes.saturating_add(encoded.len() as u64 + 1)
            > MAX_TRUSTED_REGISTRY_EVIDENCE_DIRECTORY_BYTES
    {
        return Err("Registry 可信证据存储达到资源上限。".to_string());
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&path)
        .map_err(|error| format!("无法创建 Registry 可信证据：{error}"))?;
    file.write_all(&encoded)
        .and_then(|_| file.write_all(b"\n"))
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法持久化 Registry 可信证据：{error}"))?;
    drop(file);
    native_platform::protect_private_file(&path)?;
    native_platform::verify_private_file(&path)?;
    Ok(record)
}

pub fn load_trusted_registry_development_evidence(
    data_dir: &Path,
    task: &Value,
    client_registry: &Value,
) -> Result<Value, String> {
    let expected_record = trusted_registry_evidence_record(task, client_registry)?;
    let path = trusted_registry_evidence_path(data_dir, task, client_registry)?;
    let stored = read_trusted_registry_evidence_file(&path)?;
    if stored != expected_record
        || cloud_task_preview::canonical_sha256(&stored["registry"])?
            != stored["registry_evidence_sha256"]
    {
        return Err("开发提交携带的 Registry 证据与 complete_need 持久化原件不一致。".to_string());
    }
    Ok(stored["registry"].clone())
}

fn valid_product_assessment_task_id(value: &str) -> bool {
    value.strip_prefix("assessment-").is_some_and(|suffix| {
        suffix.len() == 32
            && suffix
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn valid_product_assessment_attempt_id(value: &str) -> bool {
    value
        .strip_prefix("assessment-attempt-0001-")
        .is_some_and(|suffix| {
            suffix.len() == 16
                && suffix
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
}

fn validate_need_spec_binding(value: &Value) -> bool {
    exact_object_keys(
        value,
        &[
            "schema_version",
            "need_id",
            "revision",
            "state",
            "canonical_digest_sha256",
            "source_relative_path",
        ],
    ) && value["schema_version"] == "vibapp.need-spec.product-v0.0.1"
        && value["need_id"].as_str().is_some_and(valid_need_id)
        && value["revision"]
            .as_u64()
            .is_some_and(|revision| revision >= 1)
        && value["state"] == "draft"
        && valid_sha256(&value["canonical_digest_sha256"])
        && value["source_relative_path"].as_str().is_some_and(|path| {
            path.starts_with("registry-requests/")
                && path.len() <= 256
                && !path.contains('\\')
                && path
                    .split('/')
                    .all(|part| !part.is_empty() && part != "." && part != "..")
        })
}

fn validate_requirement_summary(value: &Value) -> bool {
    exact_object_keys(
        value,
        &[
            "title",
            "goal",
            "package_id",
            "requested_app_kind",
            "requested_capabilities",
            "requested_allowed_permissions",
            "requested_forbidden_permissions",
            "request_values_truncated",
            "network_mode",
            "completion_request_digest_sha256",
        ],
    ) && value["title"]
        .as_str()
        .is_some_and(|text| is_bounded_text(text, 1, 80))
        && value["goal"]
            .as_str()
            .is_some_and(|text| is_bounded_text(text, 1, 4_000))
        && value["package_id"]
            .as_str()
            .is_some_and(|text| is_bounded_text(text, 1, 128))
        && value["requested_app_kind"]
            .as_str()
            .is_some_and(|text| is_bounded_text(text, 1, 32))
        && [
            "requested_capabilities",
            "requested_allowed_permissions",
            "requested_forbidden_permissions",
        ]
        .iter()
        .all(|key| {
            value[*key].as_array().is_some_and(|items| {
                items.len() <= 32
                    && items.iter().all(|item| {
                        item.as_str()
                            .is_some_and(|text| is_bounded_text(text, 1, 128))
                    })
            })
        })
        && value["request_values_truncated"].is_boolean()
        && value["network_mode"]
            .as_str()
            .is_some_and(|text| is_bounded_text(text, 1, 32))
        && valid_sha256(&value["completion_request_digest_sha256"])
}

fn validate_product_assessment_task(value: &Value) -> Result<(), String> {
    const KEYS: &[&str] = &[
        "schema_version",
        "document_type",
        "task_kind",
        "task_id",
        "need_id",
        "reason_code",
        "status",
        "assessment_digest_sha256",
        "need_spec_binding",
        "requirement_summary",
        "attempt_count",
        "current_attempt_id",
        "attempt_ids",
        "codeagent_task_created",
        "external_request_made",
        "provider_process_started",
        "created_at_utc",
        "updated_at_utc",
    ];
    let attempt_id = value["current_attempt_id"].as_str().unwrap_or_default();
    if !exact_object_keys(value, KEYS)
        || value["schema_version"] != PRODUCT_ASSESSMENT_TASK_SCHEMA
        || value["document_type"] != "product-assessment-task"
        || value["task_kind"] != "product-assessment"
        || !value["task_id"]
            .as_str()
            .is_some_and(valid_product_assessment_task_id)
        || !value["need_id"].as_str().is_some_and(valid_need_id)
        || value["reason_code"] != UNSUPPORTED_CAPABILITY_REASON
        || value["status"] != "unsupported"
        || !valid_sha256(&value["assessment_digest_sha256"])
        || !validate_need_spec_binding(&value["need_spec_binding"])
        || !validate_requirement_summary(&value["requirement_summary"])
        || value["attempt_count"] != 1
        || !valid_product_assessment_attempt_id(attempt_id)
        || value["attempt_ids"] != json!([attempt_id])
        || value["codeagent_task_created"] != false
        || value["external_request_made"] != false
        || value["provider_process_started"] != false
        || !canonical_utc_shape(&value["created_at_utc"])
        || !canonical_utc_shape(&value["updated_at_utc"])
        || value["need_id"] != value["need_spec_binding"]["need_id"]
    {
        return Err("产品能力评估 task 记录损坏或不兼容。".to_string());
    }
    Ok(())
}

fn validate_product_assessment_attempt(value: &Value) -> Result<(), String> {
    const KEYS: &[&str] = &[
        "schema_version",
        "document_type",
        "task_kind",
        "task_id",
        "attempt_id",
        "attempt_number",
        "need_id",
        "reason_code",
        "status",
        "stage",
        "progress_percent",
        "terminal",
        "assessment_digest_sha256",
        "need_spec_binding",
        "requirement_summary",
        "reasons",
        "codeagent_task_created",
        "external_request_made",
        "provider_process_started",
        "authorities",
        "created_at_utc",
        "updated_at_utc",
    ];
    let authorities = &value["authorities"];
    if !exact_object_keys(value, KEYS)
        || value["schema_version"] != PRODUCT_ASSESSMENT_ATTEMPT_SCHEMA
        || value["document_type"] != "product-assessment-attempt"
        || value["task_kind"] != "product-assessment"
        || !value["task_id"]
            .as_str()
            .is_some_and(valid_product_assessment_task_id)
        || !value["attempt_id"]
            .as_str()
            .is_some_and(valid_product_assessment_attempt_id)
        || value["attempt_number"] != 1
        || !value["need_id"].as_str().is_some_and(valid_need_id)
        || value["reason_code"] != UNSUPPORTED_CAPABILITY_REASON
        || value["status"] != "unsupported"
        || value["stage"] != "product-capability-assessment"
        || value["progress_percent"] != 100
        || value["terminal"] != true
        || !valid_sha256(&value["assessment_digest_sha256"])
        || !validate_need_spec_binding(&value["need_spec_binding"])
        || !validate_requirement_summary(&value["requirement_summary"])
        || value["reasons"].as_array().is_none_or(|reasons| {
            reasons.is_empty()
                || reasons.len() > 32
                || reasons.iter().any(|reason| {
                    reason
                        .as_str()
                        .is_none_or(|text| !is_bounded_text(text, 1, 1_000))
                })
        })
        || value["codeagent_task_created"] != false
        || value["external_request_made"] != false
        || value["provider_process_started"] != false
        || !exact_object_keys(
            authorities,
            &[
                "codeagent",
                "provider",
                "builder",
                "verifier",
                "publication",
            ],
        )
        || authorities["codeagent"] != "not-invoked"
        || authorities["provider"] != "not-invoked"
        || authorities["builder"] != "not-invoked"
        || authorities["verifier"] != "not-invoked"
        || authorities["publication"] != "not-performed"
        || !canonical_utc_shape(&value["created_at_utc"])
        || !canonical_utc_shape(&value["updated_at_utc"])
        || value["need_id"] != value["need_spec_binding"]["need_id"]
    {
        return Err("产品能力评估 attempt 记录损坏或不兼容。".to_string());
    }
    Ok(())
}

fn bounded_assessment_text(value: &str, maximum: usize, fallback: &str) -> String {
    let cleaned = value
        .trim()
        .chars()
        .filter(|character| *character >= ' ' || matches!(character, '\t' | '\n'))
        .take(maximum)
        .collect::<String>();
    if cleaned.is_empty() {
        fallback.to_string()
    } else {
        cleaned
    }
}

fn bounded_assessment_values(values: &[String]) -> Vec<String> {
    values
        .iter()
        .take(32)
        .map(|value| bounded_assessment_text(value, 128, "invalid-empty-value"))
        .collect()
}

fn write_private_json_idempotent(path: &Path, value: &Value) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(_) => {
            if read_private_json(path, MAX_PRODUCT_ASSESSMENT_RECORD_BYTES)? != *value {
                return Err("已存在同 ID 但内容不同的产品能力评估记录。".to_string());
            }
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            write_private_json(path, value)
        }
        Err(error) => Err(format!("无法检查产品能力评估记录：{error}")),
    }
}

fn persist_unsupported_product_assessment(
    data_dir: &Path,
    need_id: &str,
    draft_path: &Path,
    draft: &Value,
    errors: &[String],
    input: &CompleteNeedInput,
    now: u128,
) -> Result<Value, String> {
    let draft_digest = cloud_task_preview::canonical_sha256(draft)?;
    let draft_name = draft_path
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty() && !value.contains('/') && !value.contains('\\'))
        .ok_or_else(|| "NeedSpec 草稿路径不能绑定到产品能力评估。".to_string())?;
    let request_value = json!({
        "need_id": input.need_id,
        "app_kind": input.app_kind,
        "capabilities": input.capabilities,
        "allowed_permissions": input.allowed_permissions,
        "forbidden_permissions": input.forbidden_permissions,
        "permission_ceiling_confirmed": input.permission_ceiling_confirmed,
        "negative_constraints": input.negative_constraints,
        "negative_constraints_confirmed": input.negative_constraints_confirmed,
        "network_mode": input.network_mode,
        "package_id": input.package_id,
        "package_name": input.package_name,
        "package_version": input.package_version,
        "acceptance_example": input.acceptance_example,
        "proceed_after_recommendation": input.proceed_after_recommendation,
        "registry_embedding_consent": input.registry_embedding_consent,
        "remote_processing_consent": input.remote_processing_consent,
        "public_publication_consent": input.public_publication_consent
    });
    let completion_request_digest = cloud_task_preview::canonical_sha256(&request_value)?;
    let need_spec_binding = json!({
        "schema_version": draft["schema_version"],
        "need_id": need_id,
        "revision": draft["revision"],
        "state": "draft",
        "canonical_digest_sha256": draft_digest,
        "source_relative_path": format!("registry-requests/{draft_name}")
    });
    let request_values_truncated = [
        &input.capabilities,
        &input.allowed_permissions,
        &input.forbidden_permissions,
    ]
    .iter()
    .any(|values| values.len() > 32 || values.iter().any(|value| value.chars().count() > 128));
    let requirement_summary = json!({
        "title": bounded_assessment_text(input.package_name.trim(), 80, "VibApp 需求"),
        "goal": bounded_assessment_text(draft["goal"].as_str().unwrap_or_default(), 4_000, "VibApp 需求"),
        "package_id": bounded_assessment_text(input.package_id.trim(), 128, "invalid-package-id"),
        "requested_app_kind": bounded_assessment_text(input.app_kind.trim(), 32, "invalid-app-kind"),
        "requested_capabilities": bounded_assessment_values(&input.capabilities),
        "requested_allowed_permissions": bounded_assessment_values(&input.allowed_permissions),
        "requested_forbidden_permissions": bounded_assessment_values(&input.forbidden_permissions),
        "request_values_truncated": request_values_truncated,
        "network_mode": bounded_assessment_text(input.network_mode.trim(), 32, "invalid-network-mode"),
        "completion_request_digest_sha256": completion_request_digest
    });
    let assessment_basis = json!({
        "reason_code": UNSUPPORTED_CAPABILITY_REASON,
        "need_spec_binding": need_spec_binding,
        "requirement_summary": requirement_summary,
        "reasons": errors
    });
    let assessment_digest = cloud_task_preview::canonical_sha256(&assessment_basis)?;
    let task_id = format!("assessment-{}", &assessment_digest[..32]);
    let attempt_id = format!("assessment-attempt-0001-{}", &assessment_digest[..16]);
    let task_dir = data_dir.join("product-assessments/tasks").join(&task_id);
    let attempt_dir = task_dir.join("attempts").join(&attempt_id);
    let task_path = task_dir.join("task.json");
    let attempt_path = attempt_dir.join("attempt.json");

    if task_path.exists() {
        let task = read_private_json(&task_path, MAX_PRODUCT_ASSESSMENT_RECORD_BYTES)?;
        validate_product_assessment_task(&task)?;
        let attempt = read_private_json(&attempt_path, MAX_PRODUCT_ASSESSMENT_RECORD_BYTES)?;
        validate_product_assessment_attempt(&attempt)?;
        if task["assessment_digest_sha256"] != assessment_digest
            || task["current_attempt_id"] != attempt_id
            || attempt["assessment_digest_sha256"] != assessment_digest
            || attempt["need_spec_binding"] != need_spec_binding
            || attempt["requirement_summary"] != requirement_summary
            || attempt["reasons"] != json!(errors)
        {
            return Err("既有产品能力评估与本次需求绑定不一致。".to_string());
        }
        return Ok(attempt);
    }

    fs::create_dir_all(&attempt_dir)
        .map_err(|error| format!("无法创建产品能力评估目录：{error}"))?;
    let attempt = match fs::symlink_metadata(&attempt_path) {
        Ok(_) => {
            let existing = read_private_json(&attempt_path, MAX_PRODUCT_ASSESSMENT_RECORD_BYTES)?;
            validate_product_assessment_attempt(&existing)?;
            if existing["task_id"] != task_id
                || existing["attempt_id"] != attempt_id
                || existing["need_id"] != need_id
                || existing["assessment_digest_sha256"] != assessment_digest
                || existing["need_spec_binding"] != need_spec_binding
                || existing["requirement_summary"] != requirement_summary
                || existing["reasons"] != json!(errors)
            {
                return Err("孤立的产品能力评估 attempt 与本次需求绑定不一致。".to_string());
            }
            existing
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let created_at = utc_instant_from_seconds((now / 1_000) as i64);
            json!({
                "schema_version": PRODUCT_ASSESSMENT_ATTEMPT_SCHEMA,
                "document_type": "product-assessment-attempt",
                "task_kind": "product-assessment",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "attempt_number": 1,
                "need_id": need_id,
                "reason_code": UNSUPPORTED_CAPABILITY_REASON,
                "status": "unsupported",
                "stage": "product-capability-assessment",
                "progress_percent": 100,
                "terminal": true,
                "assessment_digest_sha256": assessment_digest,
                "need_spec_binding": need_spec_binding,
                "requirement_summary": requirement_summary,
                "reasons": errors,
                "codeagent_task_created": false,
                "external_request_made": false,
                "provider_process_started": false,
                "authorities": {
                    "codeagent": "not-invoked",
                    "provider": "not-invoked",
                    "builder": "not-invoked",
                    "verifier": "not-invoked",
                    "publication": "not-performed"
                },
                "created_at_utc": created_at,
                "updated_at_utc": created_at
            })
        }
        Err(error) => return Err(format!("无法检查产品能力评估 attempt：{error}")),
    };
    let created_at = attempt["created_at_utc"]
        .as_str()
        .ok_or_else(|| "产品能力评估 attempt 缺少创建时间。".to_string())?
        .to_string();
    let task = json!({
        "schema_version": PRODUCT_ASSESSMENT_TASK_SCHEMA,
        "document_type": "product-assessment-task",
        "task_kind": "product-assessment",
        "task_id": task_id,
        "need_id": need_id,
        "reason_code": UNSUPPORTED_CAPABILITY_REASON,
        "status": "unsupported",
        "assessment_digest_sha256": assessment_digest,
        "need_spec_binding": need_spec_binding,
        "requirement_summary": requirement_summary,
        "attempt_count": 1,
        "current_attempt_id": attempt_id,
        "attempt_ids": [attempt_id],
        "codeagent_task_created": false,
        "external_request_made": false,
        "provider_process_started": false,
        "created_at_utc": created_at,
        "updated_at_utc": created_at
    });
    validate_product_assessment_attempt(&attempt)?;
    validate_product_assessment_task(&task)?;
    // The attempt becomes durable before the task index. A crash can therefore
    // leave only an unindexed record, never a visible task without its terminal fact.
    write_private_json_idempotent(&attempt_path, &attempt)?;
    write_private_json_idempotent(&task_path, &task)?;
    Ok(attempt)
}

fn product_assessment_to_job(task: &Value, attempt: &Value) -> Result<Value, String> {
    validate_product_assessment_task(task)?;
    validate_product_assessment_attempt(attempt)?;
    if task["task_id"] != attempt["task_id"]
        || task["need_id"] != attempt["need_id"]
        || task["current_attempt_id"] != attempt["attempt_id"]
        || task["assessment_digest_sha256"] != attempt["assessment_digest_sha256"]
        || task["need_spec_binding"] != attempt["need_spec_binding"]
        || task["requirement_summary"] != attempt["requirement_summary"]
    {
        return Err("产品能力评估 task/attempt 绑定不一致。".to_string());
    }
    Ok(json!({
        "job_id": task["task_id"],
        "task_id": task["task_id"],
        "attempt_id": attempt["attempt_id"],
        "task_kind": "product-assessment",
        "need_id": task["need_id"],
        "title": task["requirement_summary"]["title"],
        "route": "product-assessment",
        "status": "unsupported",
        "progress_percent": 100,
        "current_stage": "product-capability-assessment",
        "updated_at_utc": attempt["updated_at_utc"],
        "history": [attempt],
        "assessment": attempt,
        "error": null,
        "outputs": {},
        "codeagent_task_created": false,
        "external_request_made": false,
        "stages": [{
            "kind": "product-assessment",
            "status": "succeeded",
            "label": "产品能力兼容评估已完成"
        }],
        "verification": {
            "status": "rejected",
            "independent": false,
            "summary": "产品能力评估已完成：当前 VibApp 平台不支持这组应用形态与能力；未创建或调用 CodeAgent，未启动 Builder。"
        }
    }))
}

pub fn product_assessment_jobs(data_dir: &Path) -> Result<Vec<Value>, String> {
    let root = data_dir.join("product-assessments/tasks");
    let Ok(entries) = fs::read_dir(&root) else {
        return Ok(Vec::new());
    };
    let mut tasks = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = fs::symlink_metadata(entry.path()).ok()?;
            let task_id = entry.file_name().into_string().ok()?;
            (metadata.file_type().is_dir()
                && !metadata.file_type().is_symlink()
                && valid_product_assessment_task_id(&task_id))
            .then_some((task_id, entry.path()))
        })
        .collect::<Vec<_>>();
    tasks.sort_by(|left, right| right.0.cmp(&left.0));
    tasks.truncate(MAX_PRODUCT_ASSESSMENT_TASKS);
    let mut jobs = Vec::new();
    for (task_id, task_dir) in tasks {
        let task = read_private_json(
            &task_dir.join("task.json"),
            MAX_PRODUCT_ASSESSMENT_RECORD_BYTES,
        )?;
        validate_product_assessment_task(&task)?;
        if task["task_id"] != task_id {
            return Err("产品能力评估目录与 task_id 不一致。".to_string());
        }
        let attempt_id = task["current_attempt_id"]
            .as_str()
            .ok_or_else(|| "产品能力评估缺少 current_attempt_id。".to_string())?;
        let attempt = read_private_json(
            &task_dir
                .join("attempts")
                .join(attempt_id)
                .join("attempt.json"),
            MAX_PRODUCT_ASSESSMENT_RECORD_BYTES,
        )?;
        jobs.push(product_assessment_to_job(&task, &attempt)?);
    }
    jobs.sort_by(|left, right| {
        right["updated_at_utc"]
            .as_str()
            .cmp(&left["updated_at_utc"].as_str())
            .then_with(|| right["task_id"].as_str().cmp(&left["task_id"].as_str()))
    });
    Ok(jobs)
}

fn needspec_refinement(
    data_dir: &Path,
    need_id: &str,
    draft_path: &Path,
    draft: Value,
    route: &str,
    errors: Vec<String>,
    unsupported_capability_combination: bool,
    input: &CompleteNeedInput,
    now: u128,
) -> Result<Value, String> {
    let mut cloud = cloud_development_state(route);
    if unsupported_capability_combination {
        cloud["blockers"] = json!([UNSUPPORTED_CAPABILITY_REASON]);
    }
    cloud["codeagent_task_created"] = json!(false);
    cloud["external_request_made"] = json!(false);
    cloud["publication"] = json!({
        "consent": input.public_publication_consent,
        "performed": false,
        "independent_from_remote_processing": true
    });
    let reason_code = if unsupported_capability_combination {
        UNSUPPORTED_CAPABILITY_REASON
    } else {
        "needspec-incomplete"
    };
    let refinement_message = if unsupported_capability_combination {
        "当前平台不支持所选应用形态与能力组合；不会创建开发任务。"
    } else {
        "NeedSpec 仍有缺失或矛盾字段；不会创建云任务。"
    };
    let missing_fields = if unsupported_capability_combination {
        Vec::new()
    } else {
        errors.clone()
    };
    let product_assessment = if unsupported_capability_combination {
        Some(persist_unsupported_product_assessment(
            data_dir, need_id, draft_path, &draft, &errors, input, now,
        )?)
    } else {
        None
    };
    let assessment_task_id = product_assessment
        .as_ref()
        .and_then(|assessment| assessment["task_id"].as_str())
        .map(str::to_owned);
    let assessment_attempt_id = product_assessment
        .as_ref()
        .and_then(|assessment| assessment["attempt_id"].as_str())
        .map(str::to_owned);
    let history = json!({
        "need_id": need_id,
        "title": input.package_name.trim(),
        "description": draft["goal"].as_str().unwrap_or_default(),
        "route": "refinement",
        "status": if unsupported_capability_combination { "product-assessment-unsupported" } else { "needs-refinement" },
        "event": if unsupported_capability_combination { "product-assessment-completed" } else { "needspec-confirmation-rejected" },
        "handoff": if unsupported_capability_combination { "product-assessment" } else { "none" },
        "task_id": assessment_task_id,
        "attempt_id": assessment_attempt_id,
        "reason_code": reason_code,
        "need_spec_digest_sha256": product_assessment.as_ref().map(|assessment| &assessment["need_spec_binding"]["canonical_digest_sha256"]),
        "created_at_unix_ms": now,
        "codeagent_task_created": false,
        "external_request_made": false
    });
    let response = json!({
        "need": {
            "need_id": need_id,
            "status": if unsupported_capability_combination { "product-assessment-unsupported" } else { "needs-refinement" },
            "handoff": if unsupported_capability_combination { "product-assessment" } else { "none" },
            "task_id": assessment_task_id,
            "attempt_id": assessment_attempt_id,
            "reason_code": reason_code,
            "codeagent_task_created": false,
            "external_request_made": false
        },
        "need_spec": {
            "state": "draft",
            "draft": draft,
            "canonical_digest_sha256": null,
            "validation_errors": errors,
            "missing_fields": missing_fields,
            "inferred_app_kind": shape_for_kind(&input.app_kind).map(|shape| shape.app_kind),
            "inferred_world": shape_for_kind(&input.app_kind).map(|shape| shape.world)
        },
        "consent": {
            "lan_llm_analysis": if input.registry_embedding_consent { "granted" } else { "not-granted" },
            "registry_embedding": if input.registry_embedding_consent { "granted" } else { "not-granted" },
            "cloud_remote_processing": if unsupported_capability_combination && input.remote_processing_consent {
                "not-consumed-platform-unsupported"
            } else if input.remote_processing_consent {
                "requested-pending-complete-needspec"
            } else {
                "not-requested"
            },
            "public_sharing": if input.public_publication_consent { "granted-not-published" } else { "not-granted" },
            "independent_decisions": true
        },
        "registry": {
            "route": "refinement",
            "recommendations": [],
            "refinement": {
                "reason_code": reason_code,
                "message": refinement_message
            },
            "codeagent_handoff": {
                "created": false,
                "permitted": false,
                "reason": if unsupported_capability_combination {
                    "A terminal product capability assessment was recorded; this is not a CodeAgent task."
                } else {
                    "NeedSpec validation is incomplete."
                }
            }
        },
        "refinement_questions": errors,
        "cloud_development": cloud,
        "product_assessment": product_assessment
    });
    let assistant_content = response["refinement_questions"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>()
        .join("\n");
    let assistant_content = bounded_assistant_content(
        &assistant_content,
        if unsupported_capability_combination {
            "The product capability assessment is complete and unsupported; no CodeAgent or provider was invoked."
        } else {
            "NeedSpec validation requires additional user input."
        },
    );
    let confirmation = format!(
        "Confirmed NeedSpec: package={}; kind={}; acceptance={}; negative-constraints={}; remote-processing={}; public-publication={}",
        input.package_id.trim(),
        input.app_kind,
        input
            .acceptance_example
            .chars()
            .take(1_000)
            .collect::<String>(),
        input
            .negative_constraints
            .chars()
            .take(1_500)
            .collect::<String>(),
        input.remote_processing_consent,
        input.public_publication_consent
    );
    let (task_id, attempt_id) = if unsupported_capability_combination {
        (assessment_task_id, assessment_attempt_id)
    } else {
        latest_retry_binding(data_dir, need_id)
    };
    persist_conversation_turn(
        data_dir,
        history,
        response,
        now,
        task_id.as_deref(),
        attempt_id.as_deref(),
        "needspec-confirmation",
        &confirmation,
        "needspec-validation",
        &assistant_content,
    )
}

fn package_entrypoints(app_kind: &str, label: &str) -> Value {
    match app_kind {
        "service" => json!([{
            "id": "service.main",
            "kind": "service",
            "label": label,
            "initial_route": null,
            "triggers": ["on-enable", "manual"]
        }]),
        "hybrid" => json!([
            {"id": "launcher.main", "kind": "launcher-ui", "label": label, "initial_route": "home"},
            {
                "id": "service.background",
                "kind": "service",
                "label": format!("{label} 后台服务"),
                "initial_route": null,
                "triggers": ["on-enable", "manual"]
            }
        ]),
        _ => json!([{
            "id": "launcher.main", "kind": "launcher-ui", "label": label, "initial_route": "home"
        }]),
    }
}

struct ObservedProviderExecutionIdentity {
    endpoint_kind: String,
    canonical_endpoint: String,
    adapter_id: String,
    adapter_version: String,
    adapter_sha256: String,
    runtime_package: String,
    runtime_version: String,
    runtime_executable_sha256: String,
    non_secret_config_sha256: String,
    identity_sha256: String,
}

fn observed_provider_execution_identity(
    observation: &Value,
    task_provider: &str,
    model: &str,
) -> Result<(ObservedProviderExecutionIdentity, Value), String> {
    let expected_provider_id = match task_provider {
        "openai-codex" => "codex",
        "anthropic-claude-code" => "claude-code",
        "opencode" => "opencode",
        "google-gemini-cli" => "gemini-cli",
        _ => return Err("CodeAgent task provider 不受支持。".to_string()),
    };
    if observation["provider_id"] != expected_provider_id
        || observation["task_provider"] != task_provider
        || observation["model"] != model
        || observation["identity_observed"] != true
        || observation["source_authoring_only"] != true
        || observation["provider_process_started"] != false
        || observation["external_request_attempted"] != false
        || observation["external_request_observed"] != false
        || observation["consent_consumed"] != false
        || observation["compile_authority"] != false
        || observation["verify_authority"] != false
        || observation["install_authority"] != false
        || observation["publish_authority"] != false
        || observation["execution_available"].as_bool().is_none()
    {
        return Err("CodeAgent provider 身份观察与待确认任务不一致。".to_string());
    }
    let identity = &observation["provider_execution_identity"];
    if !exact_object_keys(
        identity,
        &[
            "schema_version",
            "endpoint",
            "runtime",
            "non_secret_config_sha256",
            "identity_sha256",
        ],
    ) || identity["schema_version"] != "vibapp.provider-execution-identity.experimental-v1"
        || !exact_object_keys(
            &identity["endpoint"],
            &["kind", "canonical_endpoint", "endpoint_sha256"],
        )
        || !exact_object_keys(
            &identity["runtime"],
            &[
                "adapter_id",
                "adapter_version",
                "adapter_sha256",
                "package_id",
                "package_version",
                "executable_sha256",
            ],
        )
    {
        return Err("CodeAgent provider execution identity 结构无效。".to_string());
    }
    for value in [
        &identity["endpoint"]["endpoint_sha256"],
        &identity["runtime"]["adapter_sha256"],
        &identity["runtime"]["executable_sha256"],
        &identity["non_secret_config_sha256"],
        &identity["identity_sha256"],
    ] {
        if !valid_sha256(value) {
            return Err("CodeAgent provider execution identity 摘要无效。".to_string());
        }
    }
    let endpoint_kind = identity["endpoint"]["kind"]
        .as_str()
        .ok_or_else(|| "CodeAgent endpoint kind 缺失。".to_string())?;
    let canonical_endpoint = identity["endpoint"]["canonical_endpoint"]
        .as_str()
        .ok_or_else(|| "CodeAgent canonical endpoint 缺失。".to_string())?;
    let adapter_id = identity["runtime"]["adapter_id"]
        .as_str()
        .ok_or_else(|| "CodeAgent adapter id 缺失。".to_string())?;
    let adapter_version = identity["runtime"]["adapter_version"]
        .as_str()
        .ok_or_else(|| "CodeAgent adapter version 缺失。".to_string())?;
    let adapter_sha256 = identity["runtime"]["adapter_sha256"]
        .as_str()
        .ok_or_else(|| "CodeAgent adapter digest 缺失。".to_string())?;
    let runtime_package = identity["runtime"]["package_id"]
        .as_str()
        .ok_or_else(|| "CodeAgent runtime package 缺失。".to_string())?;
    let runtime_version = identity["runtime"]["package_version"]
        .as_str()
        .ok_or_else(|| "CodeAgent runtime version 缺失。".to_string())?;
    let runtime_executable_sha256 = identity["runtime"]["executable_sha256"]
        .as_str()
        .ok_or_else(|| "CodeAgent runtime executable digest 缺失。".to_string())?;
    let non_secret_config_sha256 = identity["non_secret_config_sha256"]
        .as_str()
        .ok_or_else(|| "CodeAgent non-secret config digest 缺失。".to_string())?;
    let identity_sha256 = identity["identity_sha256"]
        .as_str()
        .ok_or_else(|| "CodeAgent identity digest 缺失。".to_string())?;
    let endpoint_body = json!({
        "kind": endpoint_kind,
        "canonical_endpoint": canonical_endpoint,
    });
    let expected_endpoint = json!({
        "kind": endpoint_kind,
        "canonical_endpoint": canonical_endpoint,
        "endpoint_sha256": cloud_task_preview::canonical_sha256(&endpoint_body)?,
    });
    let runtime = json!({
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "adapter_sha256": adapter_sha256,
        "package_id": runtime_package,
        "package_version": runtime_version,
        "executable_sha256": runtime_executable_sha256,
    });
    let identity_body = json!({
        "schema_version": "vibapp.provider-execution-identity.experimental-v1",
        "endpoint": expected_endpoint,
        "runtime": runtime,
        "non_secret_config_sha256": non_secret_config_sha256,
    });
    let expected_identity = json!({
        "schema_version": identity_body["schema_version"],
        "endpoint": identity_body["endpoint"],
        "runtime": identity_body["runtime"],
        "non_secret_config_sha256": identity_body["non_secret_config_sha256"],
        "identity_sha256": cloud_task_preview::canonical_sha256(&identity_body)?,
    });
    if identity != &expected_identity
        || observation["adapter_id"] != adapter_id
        || observation["adapter_version"] != adapter_version
        || observation["adapter_sha256"] != adapter_sha256
        || observation["executable_version"] != runtime_version
        || observation["executable_sha256"] != runtime_executable_sha256
    {
        return Err("CodeAgent provider execution identity 摘要或顶层绑定不匹配。".to_string());
    }
    let execution_available =
        observation["execution_available"] == true && observation["available"] == true;
    let blocker = if execution_available {
        Value::Null
    } else {
        let code = observation["execution_blocker"]["code"]
            .as_str()
            .filter(|value| !value.is_empty() && value.len() <= 128)
            .ok_or_else(|| "CodeAgent 执行暂停但缺少安全阻止代码。".to_string())?;
        let message = observation["execution_blocker"]["message"]
            .as_str()
            .filter(|value| !value.is_empty() && value.len() <= 1000)
            .ok_or_else(|| "CodeAgent 执行暂停但缺少安全阻止说明。".to_string())?;
        json!({"code": code, "message": message})
    };
    let metadata = json!({
        "identity_observed": true,
        "execution_available": execution_available,
        "provider_id": expected_provider_id,
        "task_provider": task_provider,
        "model": model,
        "provider_execution_identity_sha256": identity_sha256,
        "runtime_version": runtime_version,
        "runtime_executable_sha256": runtime_executable_sha256,
        "version_within_exercised_range": observation.get("version_within_exercised_range").cloned().unwrap_or(Value::Null),
        "digest_reviewed": observation.get("digest_reviewed").cloned().unwrap_or(Value::Null),
        "blocker": blocker,
    });
    Ok((
        ObservedProviderExecutionIdentity {
            endpoint_kind: endpoint_kind.to_string(),
            canonical_endpoint: canonical_endpoint.to_string(),
            adapter_id: adapter_id.to_string(),
            adapter_version: adapter_version.to_string(),
            adapter_sha256: adapter_sha256.to_string(),
            runtime_package: runtime_package.to_string(),
            runtime_version: runtime_version.to_string(),
            runtime_executable_sha256: runtime_executable_sha256.to_string(),
            non_secret_config_sha256: non_secret_config_sha256.to_string(),
            identity_sha256: identity_sha256.to_string(),
        },
        metadata,
    ))
}

pub fn complete_need_with_provider(
    data_dir: &Path,
    input: CompleteNeedInput,
    now: u128,
    embedding_settings: &EmbeddingRuntimeSettings,
    codeagent_provider: &str,
    codeagent_model: &str,
    provider_observation: &Value,
) -> Result<Value, String> {
    if !valid_need_id(&input.need_id) {
        return Err("need_id 格式无效。".to_string());
    }
    let (provider_identity, provider_execution) = observed_provider_execution_identity(
        provider_observation,
        codeagent_provider,
        codeagent_model,
    )?;
    let requests = data_dir.join("registry-requests");
    let (draft_path, draft) = latest_draft(&requests, &input.need_id)?;
    if draft["need_id"] != input.need_id
        || draft["schema_version"] != "vibapp.need-spec.product-v0.0.1"
        || draft["document_type"] != "need-spec"
    {
        return Err("本机 NeedSpec 草稿身份不匹配。".to_string());
    }
    let original_route = original_route(data_dir, &input.need_id)?;
    let mut errors = Vec::new();
    let Some(shape) = shape_for_kind(&input.app_kind) else {
        errors.push("请选择 ui、service 或 hybrid 应用形态。".to_string());
        return needspec_refinement(
            data_dir,
            &input.need_id,
            &draft_path,
            draft,
            &original_route,
            errors,
            false,
            &input,
            now,
        );
    };

    let (platform_os, platform_arch) = current_platform();
    let profile = "desktop";
    let mut unsupported_capability_combination = false;
    let mut capabilities_valid = true;
    let capabilities = match normalized_supported(&input.capabilities) {
        Ok(values) => values,
        Err(SupportedListError::Unknown) => {
            capabilities_valid = false;
            unsupported_capability_combination = true;
            errors.push("当前平台不支持所选的一个或多个 capability。".to_string());
            Vec::new()
        }
        Err(SupportedListError::Duplicate) => {
            capabilities_valid = false;
            errors.push("capabilities 不能包含重复项。".to_string());
            Vec::new()
        }
    };
    if capabilities_valid
        && capabilities
            .iter()
            .any(|item| !shape.allowed_interfaces.contains(&item.as_str()))
    {
        unsupported_capability_combination = true;
        errors.push(format!(
            "当前平台的 {} world 不支持所选的额外 capability。",
            shape.world
        ));
    }
    let mut allowed_valid = true;
    let allowed = match normalized_supported(&input.allowed_permissions) {
        Ok(values) => values,
        Err(SupportedListError::Unknown) => {
            allowed_valid = false;
            unsupported_capability_combination = true;
            errors.push("当前平台不支持权限上限中的一个或多个 capability。".to_string());
            Vec::new()
        }
        Err(SupportedListError::Duplicate) => {
            allowed_valid = false;
            errors.push("权限上限不能包含重复项。".to_string());
            Vec::new()
        }
    };
    if capabilities_valid
        && allowed_valid
        && capabilities
            .iter()
            .any(|required| !allowed.iter().any(|item| item == required))
    {
        unsupported_capability_combination = true;
        errors.push(format!(
            "权限上限必须覆盖应用实际必需的 capability：{}。",
            capabilities.join("、")
        ));
    }
    if allowed_valid
        && allowed
            .iter()
            .any(|item| !shape.allowed_interfaces.contains(&item.as_str()))
    {
        unsupported_capability_combination = true;
        errors.push(format!(
            "当前平台的 {} world 不支持权限上限中的额外 capability。",
            shape.world
        ));
    }
    let mut forbidden_valid = true;
    let forbidden = match normalized_supported(&input.forbidden_permissions) {
        Ok(values) => values,
        Err(SupportedListError::Unknown) => {
            forbidden_valid = false;
            unsupported_capability_combination = true;
            errors.push("当前平台不支持明确禁止列表中的一个或多个 capability。".to_string());
            Vec::new()
        }
        Err(SupportedListError::Duplicate) => {
            forbidden_valid = false;
            errors.push("明确禁止的权限不能包含重复项。".to_string());
            Vec::new()
        }
    };
    if allowed_valid && forbidden_valid && forbidden.iter().any(|item| allowed.contains(item)) {
        unsupported_capability_combination = true;
        errors.push("允许权限与禁止权限不能重叠。".to_string());
    }
    if !input.permission_ceiling_confirmed {
        errors.push("请明确确认 permission ceiling。".to_string());
    }
    if !input.negative_constraints_confirmed {
        errors.push("请确认负面约束；没有时也要明确选择“无额外约束”。".to_string());
    }
    let negative_lines = input
        .negative_constraints
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>();
    if negative_lines.len() > 16
        || negative_lines
            .iter()
            .any(|line| !is_bounded_text(line, 1, 500))
    {
        errors.push("负面约束最多 16 条，每条 1–500 个字符。".to_string());
    }
    if !matches!(input.network_mode.as_str(), "offline" | "scoped-network") {
        errors.push("请选择离线运行或受限网络。".to_string());
    }
    if input.network_mode == "offline" && allowed_valid && allowed.iter().any(|item| item == "http")
    {
        unsupported_capability_combination = true;
        errors.push("当前 WIT world 固定导入 http，不能同时声明离线运行。".to_string());
    }
    if !valid_app_id(&input.package_id) {
        errors.push("package id 必须是小写 VibApp 标识符。".to_string());
    }
    if !valid_semver(&input.package_version) {
        errors.push("package version 必须是简单语义版本（例如 0.1.0）。".to_string());
    }
    if !is_bounded_text(input.package_name.trim(), 1, 80) {
        errors.push("package name 需为 1–80 个字符。".to_string());
    }
    if !is_bounded_text(input.acceptance_example.trim(), 1, 1000) {
        errors.push("请给出 1–1000 个字符的可验收示例。".to_string());
    }
    if !errors.is_empty() {
        return needspec_refinement(
            data_dir,
            &input.need_id,
            &draft_path,
            draft,
            &original_route,
            errors,
            unsupported_capability_combination,
            &input,
            now,
        );
    }

    let mut negative_constraints = negative_lines
        .iter()
        .enumerate()
        .map(|(index, value)| {
            json!({
                "constraint_id": format!("constraint.user.{:02}", index + 1),
                "kind": "other",
                "value": value,
                "source": "user"
            })
        })
        .collect::<Vec<_>>();
    negative_constraints.push(json!({
        "constraint_id": "constraint.network-mode",
        "kind": "other",
        "value": if input.network_mode == "offline" {
            "应用运行时保持离线，不得进行网络请求。"
        } else {
            "应用只可使用 permission ceiling 内声明的受限网络能力。"
        },
        "source": "user"
    }));
    for (index, denied) in forbidden.iter().enumerate() {
        negative_constraints.push(json!({
            "constraint_id": format!("constraint.forbidden.{:02}", index + 1),
            "kind": "forbidden-capability",
            "value": interface(denied),
            "source": "user"
        }));
    }
    let goal = draft["goal"]
        .as_str()
        .ok_or_else(|| "本机 NeedSpec 草稿缺少 goal。".to_string())?;
    let capability_text = if capabilities.is_empty() {
        "无；固定 world imports 仅用于结构链接".to_string()
    } else {
        capabilities.join("、")
    };
    let required_capability_interfaces = capabilities
        .iter()
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    let structural_imports = shape
        .allowed_interfaces
        .iter()
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    let allowed_interfaces = allowed
        .iter()
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    let forbidden_interfaces = forbidden
        .iter()
        .map(|name| interface(name))
        .collect::<Vec<_>>();
    let created_at = draft["created_at_utc"]
        .as_str()
        .ok_or_else(|| "本机 NeedSpec 草稿缺少 created_at_utc。".to_string())?;
    let revision = draft["revision"].as_u64().unwrap_or(1).saturating_add(1);
    let need_spec = json!({
        "schema_version": "vibapp.need-spec.product-v0.0.1",
        "document_type": "need-spec",
        "need_id": input.need_id,
        "owner": {"principal_id": "desktop.local.user", "principal_kind": "user"},
        "goal": goal,
        "requirements": [
            {
                "requirement_id": "requirement.primary",
                "text": draft["requirements"].as_array()
                    .and_then(|items| items.iter().find(|item| item["requirement_id"] == "requirement.primary"))
                    .and_then(|item| item["text"].as_str())
                    .filter(|text| is_bounded_text(text, 1, 2000))
                    .unwrap_or(goal),
                "priority": "must-have",
                "acceptance_examples": [input.acceptance_example.trim()]
            },
            {
                "requirement_id": "requirement.capabilities",
                "text": format!("应用必须实现已确认能力：{capability_text}"),
                "priority": "must-have",
                "acceptance_examples": [input.acceptance_example.trim()]
            }
        ],
        "negative_constraints": negative_constraints,
        "platforms": [{
            "os": platform_os,
            "arch": platform_arch,
            "profile": profile
        }],
        "profiles": [profile],
        "permission_ceiling": {
            "allowed_interfaces": allowed_interfaces,
            "forbidden_interfaces": forbidden_interfaces,
            "maximum_scope_digests": []
        },
        "privacy_requirement": "remote-private",
        "created_at_utc": created_at,
        "revision": revision
    });
    let digest = cloud_task_preview::canonical_sha256(&need_spec)?;
    let complete_path = requests.join(format!("{}-complete-{}.json", input.need_id, &digest[..16]));
    if complete_path.exists() {
        if read_private_json(&complete_path, 64 * 1024)? != need_spec {
            return Err("已存在同摘要但内容不同的 NeedSpec。".to_string());
        }
    } else {
        write_private_json(&complete_path, &need_spec)?;
    }

    let registry = call_registry(
        &complete_path,
        shape,
        &required_capability_interfaces,
        input.registry_embedding_consent,
        embedding_settings,
    )
    .unwrap_or_else(|message| safe_refinement(&input.need_id, message));
    validate_registry_route(&registry)?;
    let registry_route = registry["route"].as_str().unwrap_or("refinement");
    // A transport/parser/Registry failure is represented as a local refinement so
    // the user can continue editing, but it is not an authoritative no-match.  A
    // recommendation also needs a separate durable rejection decision; the legacy
    // proceed_after_recommendation UI boolean cannot mint that authority.
    let authoritative_registry_no_match =
        registry_grants_development_authority(&registry, &need_spec, &digest);
    let existing_app_rejected = authoritative_registry_no_match;
    let package_intent = json!({
        "app_id": input.package_id,
        "version": input.package_version,
        "display_name": input.package_name.trim(),
        "description": goal.chars().take(1000).collect::<String>(),
        "entrypoints": package_entrypoints(shape.app_kind, input.package_name.trim())
    });
    let profiles = vec![profile.to_string()];
    let issued_at = utc_instant_from_seconds((now / 1000) as i64);
    let expires_at = utc_instant_from_seconds((now / 1000) as i64 + 900);
    let attempt_ordinal = 1_u32;
    let attempt_seed = json!({
        "need_spec_digest_sha256": digest,
        "provider_execution_identity_sha256": provider_identity.identity_sha256,
        "issued_at_utc": issued_at,
        "ordinal": attempt_ordinal,
    });
    let attempt_seed_digest = cloud_task_preview::canonical_sha256(&attempt_seed)?;
    let attempt_id = format!(
        "attempt-{attempt_ordinal:04}-{}",
        &attempt_seed_digest[..16]
    );
    let mut cloud_development =
        cloud_task_preview::completed_state(cloud_task_preview::CompletePreview {
            provider: codeagent_provider,
            model: codeagent_model,
            provider_execution_identity: cloud_task_preview::ProviderExecutionIdentityPreview {
                endpoint_kind: &provider_identity.endpoint_kind,
                canonical_endpoint: &provider_identity.canonical_endpoint,
                adapter_id: &provider_identity.adapter_id,
                adapter_version: &provider_identity.adapter_version,
                adapter_sha256: &provider_identity.adapter_sha256,
                runtime_package: &provider_identity.runtime_package,
                runtime_version: &provider_identity.runtime_version,
                runtime_executable_sha256: &provider_identity.runtime_executable_sha256,
                non_secret_config_sha256: &provider_identity.non_secret_config_sha256,
            },
            execution_attempt: cloud_task_preview::ExecutionAttemptPreview {
                attempt_id: &attempt_id,
                ordinal: attempt_ordinal,
            },
            need_spec: &need_spec,
            need_spec_digest: &digest,
            package_intent: &package_intent,
            app_kind: shape.app_kind,
            wit_world: shape.world,
            profiles: &profiles,
            required_imports: &structural_imports,
            required_capabilities: &required_capability_interfaces,
            issued_at_utc: &issued_at,
            expires_at_utc: &expires_at,
            remote_processing_consent: input.remote_processing_consent,
            public_publication_consent: input.public_publication_consent,
            existing_app_rejected,
        })?;
    let execution_available = provider_execution["execution_available"] == true;
    cloud_development["required_conditions"]["provider_execution_available"] =
        json!(execution_available);
    cloud_development["provider_execution"] = provider_execution.clone();
    let registry_evidence = if authoritative_registry_no_match {
        let task_preview = &cloud_development["task_preparation"]["schema_preview"];
        if task_preview.is_object() {
            Some(persist_trusted_registry_development_evidence(
                data_dir,
                task_preview,
                &registry,
            )?)
        } else {
            None
        }
    } else {
        None
    };
    let registry_evidence_available = registry_evidence.is_some();
    cloud_development["required_conditions"]["authoritative_registry_no_match"] =
        json!(authoritative_registry_no_match);
    cloud_development["required_conditions"]["registry_development_evidence_bound"] =
        json!(registry_evidence_available);
    cloud_development["task_preparation"]["registry_evidence_available"] =
        json!(registry_evidence_available);
    cloud_development["task_preparation"]["registry_evidence_binding"] = registry_evidence
        .as_ref()
        .map(|record| {
            json!({
                "schema_version": record["schema_version"],
                "document_type": record["document_type"],
                "immutable_task_digest_sha256": record["immutable_task_digest_sha256"],
                "need_spec_digest_sha256": record["need_spec_digest_sha256"],
                "need_id": record["need_id"],
                "registry_request_id": record["registry_request_id"],
                "registry_evidence_sha256": record["registry_evidence_sha256"]
            })
        })
        .unwrap_or(Value::Null);
    cloud_development["task_preparation"]["submission_available"] = json!(
        cloud_development["task_preparation"]["schema_preview_available"] == true
            && execution_available
            && registry_evidence_available
    );
    if registry_route == "recommendation" {
        let blockers = cloud_development["blockers"]
            .as_array_mut()
            .ok_or_else(|| "Cloud development blockers 结构无效。".to_string())?;
        let blocker = "registry-recommendation-rejection-record-required";
        if !blockers.iter().any(|value| value == blocker) {
            blockers.insert(0, Value::String(blocker.to_string()));
        }
    } else if !authoritative_registry_no_match {
        let blockers = cloud_development["blockers"]
            .as_array_mut()
            .ok_or_else(|| "Cloud development blockers 结构无效。".to_string())?;
        let blocker = "authoritative-registry-no-match-required";
        if !blockers.iter().any(|value| value == blocker) {
            blockers.insert(0, Value::String(blocker.to_string()));
        }
    }
    if !execution_available {
        let blocker_code = provider_execution["blocker"]["code"]
            .as_str()
            .ok_or_else(|| "CodeAgent 执行暂停但缺少阻止代码。".to_string())?;
        let blockers = cloud_development["blockers"]
            .as_array_mut()
            .ok_or_else(|| "Cloud development blockers 结构无效。".to_string())?;
        if !blockers.iter().any(|value| value == blocker_code) {
            blockers.insert(0, Value::String(blocker_code.to_string()));
        }
    }
    let history = json!({
        "need_id": input.need_id,
        "title": input.package_name.trim(),
        "description": goal,
        "route": registry_route,
        "status": "needspec-complete",
        "need_spec_digest_sha256": digest,
        "handoff": "schema-preview-only",
        "created_at_unix_ms": now,
        "codeagent_task_created": false,
        "external_request_made": false
    });
    let reason_code = registry["refinement"]["reason_code"]
        .as_str()
        .unwrap_or("recommendation-found");
    let response = json!({
        "need": history,
        "need_spec": {
            "state": "complete",
            "document": need_spec,
            "canonical_digest_sha256": digest,
            "missing_fields": [],
            "app_kind": shape.app_kind,
            "wit_world": shape.world,
            "capabilities": capabilities,
            "network_mode": input.network_mode,
            "package_intent": package_intent,
            "ecosystem_target": {
                "runtime": "vibapp-client",
                "package_model": "single-canonical-component",
                "surface_renderer": "host-semantic-ui",
                "layout_policy": "host-responsive",
                "size_classes": ["compact", "regular", "wide"],
                "host_compatibility": "client-selected",
                "registry_admission_context": {
                    "os": platform_os,
                    "arch": platform_arch,
                    "profile": profile
                }
            }
        },
        "consent": {
            "lan_llm_analysis": if input.registry_embedding_consent { "granted" } else { "not-granted" },
            "registry_embedding": if input.registry_embedding_consent { "granted" } else { "not-granted" },
            "cloud_remote_processing": if input.remote_processing_consent { "granted-single-use" } else { "not-granted" },
            "public_sharing": if input.public_publication_consent { "granted-not-published" } else { "not-granted" },
            "independent_decisions": true
        },
        "registry": registry,
        "refinement_questions": if registry_route == "refinement" { refinement_questions(reason_code) } else { Vec::new() },
        "cloud_development": cloud_development
    });
    let confirmation = format!(
        "Confirmed NeedSpec: package={}; kind={}; version={}; acceptance={}; negative-constraints={}; remote-processing={}; public-publication={}",
        input.package_id.trim(),
        input.app_kind,
        input.package_version,
        input
            .acceptance_example
            .chars()
            .take(1_000)
            .collect::<String>(),
        input
            .negative_constraints
            .chars()
            .take(1_500)
            .collect::<String>(),
        input.remote_processing_consent,
        input.public_publication_consent
    );
    let assistant_content = if registry_route == "recommendation" {
        "NeedSpec is complete. Registry found compatible existing VibApps; review them before authorizing development."
            .to_string()
    } else {
        "NeedSpec is complete and the Registry decision allows an explicitly authorized development handoff."
            .to_string()
    };
    let (task_id, attempt_id) = latest_retry_binding(data_dir, &input.need_id);
    persist_conversation_turn(
        data_dir,
        history,
        response,
        now,
        task_id.as_deref(),
        attempt_id.as_deref(),
        "needspec-confirmation",
        &confirmation,
        "needspec-completed",
        &assistant_content,
    )
}

fn offline_synthetic_provider_observation(
    task_provider: &str,
    model: &str,
) -> Result<Value, String> {
    let provider_id = match task_provider {
        "openai-codex" => "codex",
        "anthropic-claude-code" => "claude-code",
        "opencode" => "opencode",
        "google-gemini-cli" => "gemini-cli",
        _ => return Err("离线测试 provider 不受支持。".to_string()),
    };
    let endpoint_body = json!({
        "kind": "provider-managed",
        "canonical_endpoint": "provider-managed",
    });
    let endpoint = json!({
        "kind": endpoint_body["kind"],
        "canonical_endpoint": endpoint_body["canonical_endpoint"],
        "endpoint_sha256": cloud_task_preview::canonical_sha256(&endpoint_body)?,
    });
    let runtime = json!({
        "adapter_id": "offline-registry-fixture",
        "adapter_version": "vibapp.offline-registry-fixture.experimental-v1",
        "adapter_sha256": "a".repeat(64),
        "package_id": provider_id,
        "package_version": "offline-not-executed",
        "executable_sha256": "b".repeat(64),
    });
    let identity_body = json!({
        "schema_version": "vibapp.provider-execution-identity.experimental-v1",
        "endpoint": endpoint,
        "runtime": runtime,
        "non_secret_config_sha256": "c".repeat(64),
    });
    let identity = json!({
        "schema_version": identity_body["schema_version"],
        "endpoint": identity_body["endpoint"],
        "runtime": identity_body["runtime"],
        "non_secret_config_sha256": identity_body["non_secret_config_sha256"],
        "identity_sha256": cloud_task_preview::canonical_sha256(&identity_body)?,
    });
    Ok(json!({
        "provider_id": provider_id,
        "task_provider": task_provider,
        "model": model,
        "identity_observed": true,
        "available": false,
        "execution_available": false,
        "execution_blocker": {
            "code": "offline-synthetic-provider-not-executable",
            "message": "offline registry fixture observes task shape only and never starts a provider"
        },
        "source_authoring_only": true,
        "provider_process_started": false,
        "external_request_attempted": false,
        "external_request_observed": false,
        "consent_consumed": false,
        "compile_authority": false,
        "verify_authority": false,
        "install_authority": false,
        "publish_authority": false,
        "adapter_id": identity["runtime"]["adapter_id"],
        "adapter_version": identity["runtime"]["adapter_version"],
        "adapter_sha256": identity["runtime"]["adapter_sha256"],
        "executable_version": identity["runtime"]["package_version"],
        "executable_sha256": identity["runtime"]["executable_sha256"],
        "provider_execution_identity": identity,
    }))
}

pub fn complete_need(
    data_dir: &Path,
    input: CompleteNeedInput,
    now: u128,
    embedding_settings: &EmbeddingRuntimeSettings,
) -> Result<Value, String> {
    let provider = "openai-codex";
    let model = "gpt-5.6-sol";
    let observation = offline_synthetic_provider_observation(provider, model)?;
    complete_need_with_provider(
        data_dir,
        input,
        now,
        embedding_settings,
        provider,
        model,
        &observation,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn analysis_keeps_full_requirements_and_unique_constraint_ids() {
        let original = "离线换算：0摄氏是32华氏，-40摄氏是-40华氏，非法输入必须报错；重启保留输入。";
        let analysis = json!({
            "goal_summary": "离线单位换算", "network_mode": "offline",
            "negative_constraints": ["禁止联网", "禁止通知"],
            "capabilities": ["kv", "settings"]
        });
        let draft = build_need_spec_from_analysis("need-full-text", original, shape_for_kind("ui").unwrap(), Some(&analysis));
        assert_eq!(draft["goal"], "离线单位换算");
        assert_eq!(draft["requirements"][0]["text"], original);
        let constraints = draft["negative_constraints"].as_array().unwrap();
        let ids = constraints.iter().map(|item| item["constraint_id"].as_str().unwrap()).collect::<BTreeSet<_>>();
        assert_eq!(ids.len(), constraints.len());
    }

    #[test]
    fn generated_service_intent_declares_only_implemented_default_triggers() {
        let service = package_entrypoints("service", "Probe");
        assert_eq!(service[0]["triggers"], json!(["on-enable", "manual"]));

        let hybrid = package_entrypoints("hybrid", "Assistant");
        assert_eq!(hybrid[1]["triggers"], json!(["on-enable", "manual"]));
        assert!(
            package_entrypoints("ui", "Clock")[0]
                .get("triggers")
                .is_none()
        );
    }

    #[test]
    fn utc_conversion_has_canonical_epoch() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_324), (2025, 8, 24));
    }

    #[test]
    fn recommendation_requires_nonempty_candidates_and_no_handoff() {
        let response = json!({
            "route": "recommendation",
            "recommendations": [{"app": {"id": "ai.vibapp.fixture.focus-board"}}],
            "codeagent_handoff": {"created": false, "permitted": false}
        });
        assert!(validate_registry_route(&response).is_ok());
    }

    #[test]
    fn refinement_requires_empty_candidates_and_no_handoff() {
        let response = json!({
            "route": "refinement",
            "recommendations": [],
            "codeagent_handoff": {"created": false, "permitted": false}
        });
        assert!(validate_registry_route(&response).is_ok());
    }

    #[test]
    fn codeagent_handoff_is_rejected() {
        let response = json!({
            "route": "refinement",
            "recommendations": [],
            "codeagent_handoff": {"created": true, "permitted": false}
        });
        assert!(validate_registry_route(&response).is_err());
    }

    #[test]
    fn inferred_ui_need_is_bounded_to_desktop_world() {
        let shape = infer_shape("做一个专注任务看板和进度视图，本地保存状态");
        let need = build_need_spec(
            "need-test",
            "做一个专注任务看板和进度视图，本地保存状态",
            shape,
        );
        assert_eq!(shape.app_kind, "ui");
        assert_eq!(need["profiles"], json!(["desktop"]));
        assert!(
            need["permission_ceiling"]["allowed_interfaces"]
                .as_array()
                .is_some_and(|items| items.is_empty())
        );
    }

    #[test]
    fn llm_capability_proposal_is_clipped_without_fixed_world_permission_tax() {
        let mut analysis = json!({"capabilities": ["kv", "http"]});
        constrain_analysis_to_shape(&mut analysis, shape_for_kind("ui").unwrap());
        assert_eq!(analysis["capabilities"], json!(["kv"]));
    }

    #[test]
    fn history_write_never_creates_codeagent_outbox() {
        let root =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../target/test-artifacts/registry-history");
        fs::create_dir_all(root.join("history")).expect("create test history");
        let history = root.join("history/need-requests.jsonl");
        append_history(
            &history,
            &json!({"need_id": "need-test", "codeagent_task_created": false}),
        )
        .expect("append history");
        assert!(history.is_file());
        assert!(!root.join("outbox/codeagent-tasks.jsonl").exists());
    }

    #[test]
    fn typed_conversation_turn_is_durable_and_reloadable() {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let need_id = format!("need-{suffix:x}");
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("conversation-{suffix:x}"));
        fs::create_dir_all(root.join("history")).expect("create history directory");
        let history = json!({
            "need_id": need_id,
            "route": "refinement",
            "status": "needs-refinement"
        });
        let response = json!({
            "registry": {"route": "refinement"},
            "need_spec": {"state": "draft"}
        });
        let returned = persist_conversation_turn(
            &root,
            history,
            response,
            1_787_518_400_000,
            Some("task-bound"),
            Some("attempt-bound"),
            "requirement-edited",
            "Keep the previous attempt and add filtering.",
            "assistant-refinement",
            "Please confirm the updated acceptance example.",
        )
        .expect("persist typed turn");
        let entries = returned["need"]["conversation_transcript"]
            .as_array()
            .expect("response transcript");
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0]["schema_version"], TRANSCRIPT_SCHEMA);
        assert_eq!(entries[0]["need_id"], need_id);
        assert_eq!(entries[0]["task_id"], "task-bound");
        assert_eq!(entries[0]["attempt_id"], "attempt-bound");
        assert_eq!(entries[1]["snapshot"]["registry"]["route"], "refinement");

        let reloaded = fs::read_to_string(root.join("history/need-requests.jsonl"))
            .expect("reload transcript after simulated restart");
        let durable: Value = serde_json::from_str(reloaded.lines().next().unwrap()).unwrap();
        assert_eq!(durable["conversation_transcript"], json!(entries));
    }

    #[test]
    fn conversation_turn_is_bounded_before_history_write() {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let need_id = format!("need-{suffix:x}");
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("conversation-bounds-{suffix:x}"));
        fs::create_dir_all(root.join("history")).unwrap();
        let result = persist_conversation_turn(
            &root,
            json!({"need_id": need_id}),
            json!({"need_spec": {"state": "draft"}}),
            1_787_518_400_001,
            None,
            None,
            "need-submitted",
            &"x".repeat(MAX_TRANSCRIPT_CONTENT_CHARS + 1),
            "assistant-refinement",
            "bounded",
        );
        assert!(result.is_err());
        assert!(!root.join("history/need-requests.jsonl").exists());
    }

    #[test]
    fn retry_binding_reloads_latest_immutable_attempt_identity() {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let need_id = format!("need-{suffix:x}");
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("conversation-binding-{suffix:x}"));
        fs::create_dir_all(root.join("history")).unwrap();
        let path = root.join("history/need-requests.jsonl");
        append_history(&path, &json!({"need_id": need_id, "retry_task_id": "task-one", "retry_attempt_id": "attempt-one"})).unwrap();
        append_history(&path, &json!({"need_id": need_id, "retry_task_id": "task-one", "retry_attempt_id": "attempt-two"})).unwrap();
        assert_eq!(
            latest_retry_binding(&root, &need_id),
            (
                Some("task-one".to_string()),
                Some("attempt-two".to_string())
            )
        );
    }

    #[test]
    fn edited_retry_reuses_need_identity_and_preserves_prior_drafts() {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let need_id = format!("need-{suffix:x}");
        let requests = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("retry-drafts-{suffix:x}"));
        fs::create_dir_all(&requests).unwrap();
        let mut first = build_need_spec(
            &need_id,
            "做一个本地保存事项的界面应用。",
            shape_for_kind("ui").unwrap(),
        );
        first["revision"] = json!(1);
        let first_path = requests.join(format!("{need_id}.json"));
        write_private_json(&first_path, &first).unwrap();
        let mut second = first.clone();
        second["revision"] = json!(2);
        second["goal"] = json!("做一个本地保存事项并支持筛选的界面应用。");
        let second_path = requests.join(format!("{need_id}-draft-r2-test.json"));
        write_private_json(&second_path, &second).unwrap();

        let (latest_path, latest) = latest_draft(&requests, &need_id).unwrap();
        assert_eq!(latest_path, second_path);
        assert_eq!(latest["need_id"], need_id);
        assert_eq!(latest["revision"], 2);
        assert_eq!(read_private_json(&first_path, 64 * 1024).unwrap(), first);
    }

    #[test]
    fn retry_binding_requires_same_failed_task_and_need() {
        let task_id = "development-0123456789abcdef0123456789abcdef";
        let need_id = "need-1234";
        let status = json!({
            "task": {"task_id": task_id, "need_id": need_id},
            "attempt": {"attempt_id": "attempt-test", "task_id": task_id, "need_id": need_id, "status": "failed"}
        });
        assert!(validate_retry_binding(&status, task_id, need_id).is_ok());
        assert!(validate_retry_binding(&status, task_id, "need-dead").is_err());
        let mut running = status;
        running["attempt"]["status"] = json!("running");
        assert!(validate_retry_binding(&running, task_id, need_id).is_err());
    }

    #[test]
    fn registry_request_over_64_kib_is_rejected_before_write() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts/registry-request-bounds");
        fs::create_dir_all(&root).expect("create request test directory");
        let path = root.join("oversized.json");
        let value = json!({"payload": "x".repeat(65 * 1024)});
        assert!(write_private_json(&path, &value).is_err());
        assert!(!path.exists());
    }

    #[test]
    fn cloud_worker_contract_remains_uninvoked_for_both_registry_routes() {
        for route in ["recommendation", "refinement"] {
            let state = cloud_development_state(route);
            assert_eq!(state["enabled"], json!(false));
            assert_eq!(state["external_request_made"], json!(false));
            assert_eq!(state["task_preparation"]["eligible"], json!(false));
            assert_eq!(state["task_preparation"]["task_created"], json!(false));
            assert!(state["task_preparation"]["task"].is_null());
            assert_eq!(
                state["worker_contract"]["schema_version"],
                json!(cloud_task_preview::TASK_SCHEMA_VERSION)
            );
            assert_eq!(
                state["required_conditions"]["remote_processing_consent"],
                json!(false)
            );
        }
    }

    fn trusted_registry_fixture() -> (Value, Value) {
        let need_spec = json!({
            "schema_version": "vibapp.need-spec.product-v0.0.1",
            "document_type": "need-spec",
            "need_id": "need-deadbeef",
            "owner": {"principal_id": "desktop.local.user", "principal_kind": "user"},
            "goal": "Build a bounded local list.",
            "requirements": [],
            "negative_constraints": [],
            "platforms": [{"os": "macos", "arch": "aarch64", "profile": "desktop"}],
            "profiles": ["desktop"],
            "permission_ceiling": {
                "allowed_interfaces": [],
                "forbidden_interfaces": [],
                "maximum_scope_digests": []
            },
            "privacy_requirement": "remote-private",
            "created_at_utc": "2026-08-30T00:00:00Z",
            "revision": 2
        });
        let need_digest = cloud_task_preview::canonical_sha256(&need_spec).unwrap();
        let task = json!({
            "schema_version": CLOUD_CODEAGENT_TASK_SCHEMA,
            "need_spec_complete": true,
            "need_spec_digest_sha256": need_digest,
            "immutable_task_digest_sha256": "a".repeat(64),
            "need_spec": need_spec
        });
        let registry = json!({
            "schema_version": AUTHORITATIVE_REGISTRY_ROUTE_SCHEMA,
            "status": AUTHORITATIVE_REGISTRY_STATUS,
            "route": "refinement",
            "request_id": format!("registry.{}", &need_digest[..24]),
            "need_id": "need-deadbeef",
            "recommendations": [],
            "refinement": {
                "reason_code": "no-hard-filter-match",
                "message": "No candidate passed every hard filter."
            },
            "retrieval": {"mode": "hard-filter-then-keyword-plus-embedding"},
            "rejected": [],
            "codeagent_handoff": {
                "created": false,
                "permitted": false,
                "reason": "Registry only recommends or requests refinement."
            }
        });
        (task, registry)
    }

    #[test]
    fn registry_development_submission_loads_only_exact_persisted_evidence() {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("trusted-registry-{suffix:x}"));
        fs::create_dir_all(&root).unwrap();
        let (task, registry) = trusted_registry_fixture();
        assert!(registry_grants_development_authority(
            &registry,
            &task["need_spec"],
            task["need_spec_digest_sha256"].as_str().unwrap(),
        ));
        let record = persist_trusted_registry_development_evidence(&root, &task, &registry)
            .expect("host persists exact Registry result");
        assert_eq!(record["registry"], registry);
        assert_eq!(
            load_trusted_registry_development_evidence(&root, &task, &registry).unwrap(),
            registry
        );

        let mut forged = registry.clone();
        forged["refinement"]["message"] = json!("Client asserted a different no-match.");
        assert!(load_trusted_registry_development_evidence(&root, &task, &forged).is_err());

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            let path = trusted_registry_evidence_path(&root, &task, &registry).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
            assert!(
                load_trusted_registry_development_evidence(&root, &task, &registry).is_err(),
                "a broad-permission evidence file must never be accepted"
            );
            native_platform::protect_private_file(&path).unwrap();
        }
    }

    #[test]
    fn registry_crash_fallback_and_malformed_results_never_mint_development_evidence() {
        let (task, registry) = trusted_registry_fixture();
        let need_spec = &task["need_spec"];
        let need_digest = task["need_spec_digest_sha256"].as_str().unwrap();

        let fallback = safe_refinement("need-deadbeef", "Registry process crashed.".to_string());
        assert!(
            authoritative_registry_refinement_binding(&fallback, need_spec, need_digest).is_err()
        );
        assert!(!registry_grants_development_authority(
            &fallback,
            need_spec,
            need_digest
        ));

        let mut malformed = registry.clone();
        malformed["request_id"] = json!("registry.000000000000000000000000");
        assert!(
            authoritative_registry_refinement_binding(&malformed, need_spec, need_digest).is_err()
        );
        assert!(!registry_grants_development_authority(
            &malformed,
            need_spec,
            need_digest
        ));

        let mut client_extended = registry.clone();
        client_extended["client_claimed_no_match"] = json!(true);
        assert!(
            authoritative_registry_refinement_binding(&client_extended, need_spec, need_digest)
                .is_err()
        );
    }

    #[test]
    fn recommendation_cannot_become_development_authority_from_legacy_proceed_boolean() {
        let (task, mut registry) = trusted_registry_fixture();
        registry["route"] = json!("recommendation");
        registry["recommendations"] = json!([{"app": {"id": "ai.vibapp.existing"}}]);
        registry["refinement"] = Value::Null;
        assert!(
            authoritative_registry_refinement_binding(
                &registry,
                &task["need_spec"],
                task["need_spec_digest_sha256"].as_str().unwrap(),
            )
            .is_err()
        );
        assert!(!registry_grants_development_authority(
            &registry,
            &task["need_spec"],
            task["need_spec_digest_sha256"].as_str().unwrap(),
        ));
    }

    fn complete_staged_ui_need(
        case_name: &str,
        configure: impl FnOnce(&mut CompleteNeedInput),
    ) -> (PathBuf, Value, CompleteNeedInput) {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let need_id = format!("need-{suffix:x}");
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("complete-{case_name}-{suffix:x}"));
        fs::create_dir_all(root.join("registry-requests")).expect("create request directory");
        fs::create_dir_all(root.join("history")).expect("create history directory");
        let draft = build_need_spec(
            &need_id,
            "做一个本地保存事项的专注界面应用。",
            shape_for_kind("ui").unwrap(),
        );
        write_private_json(
            &root.join(format!("registry-requests/{need_id}.json")),
            &draft,
        )
        .expect("write draft");
        append_history(
            &root.join("history/need-requests.jsonl"),
            &json!({"need_id": need_id, "route": "refinement"}),
        )
        .expect("write route");
        let mut input = CompleteNeedInput {
            need_id,
            app_kind: "ui".to_string(),
            capabilities: vec!["kv".to_string(), "settings".to_string()],
            allowed_permissions: vec![
                "clock".to_string(),
                "kv".to_string(),
                "log".to_string(),
                "host-info".to_string(),
                "settings".to_string(),
            ],
            forbidden_permissions: Vec::new(),
            permission_ceiling_confirmed: true,
            negative_constraints: String::new(),
            negative_constraints_confirmed: true,
            network_mode: "scoped-network".to_string(),
            package_id: "ai.vibapp.test".to_string(),
            package_name: "Test".to_string(),
            package_version: "0.1.0".to_string(),
            acceptance_example: "事项在重启后仍然存在。".to_string(),
            proceed_after_recommendation: true,
            registry_embedding_consent: true,
            remote_processing_consent: true,
            public_publication_consent: true,
        };
        configure(&mut input);
        let model_settings = crate::model_settings::runtime(&root).expect("default model settings");
        let result = complete_need(
            &root,
            input.clone(),
            1_787_518_400_000,
            &model_settings.embedding,
        )
        .expect("CompleteNeed response");
        (root, result, input)
    }

    fn assert_platform_capability_blocker(root: &Path, result: &Value) {
        assert_eq!(result["need"]["status"], "product-assessment-unsupported");
        assert_eq!(
            result["registry"]["refinement"]["reason_code"],
            "unsupported-capability-combination"
        );
        assert_eq!(result["need_spec"]["missing_fields"], json!([]));
        assert_eq!(
            result["cloud_development"]["blockers"][0],
            "unsupported-capability-combination"
        );
        assert_eq!(result["need"]["codeagent_task_created"], false);
        assert_eq!(result["cloud_development"]["codeagent_task_created"], false);
        assert_eq!(result["cloud_development"]["external_request_made"], false);
        assert_eq!(
            result["cloud_development"]["blockers"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let assessment = &result["product_assessment"];
        assert_eq!(assessment["task_kind"], "product-assessment");
        assert_eq!(assessment["status"], "unsupported");
        assert_eq!(assessment["terminal"], true);
        assert_eq!(assessment["reason_code"], UNSUPPORTED_CAPABILITY_REASON);
        assert_eq!(assessment["codeagent_task_created"], false);
        assert_eq!(assessment["external_request_made"], false);
        assert_eq!(assessment["provider_process_started"], false);
        assert_eq!(
            assessment["need_spec_binding"]["need_id"],
            result["need"]["need_id"]
        );
        assert!(valid_sha256(
            &assessment["need_spec_binding"]["canonical_digest_sha256"]
        ));
        assert_eq!(result["need"]["task_id"], assessment["task_id"]);
        assert_eq!(result["need"]["attempt_id"], assessment["attempt_id"]);
        let task_id = assessment["task_id"].as_str().expect("assessment task id");
        let attempt_id = assessment["attempt_id"]
            .as_str()
            .expect("assessment attempt id");
        assert!(
            root.join(format!("product-assessments/tasks/{task_id}/task.json"))
                .is_file()
        );
        assert!(
            root.join(format!(
                "product-assessments/tasks/{task_id}/attempts/{attempt_id}/attempt.json"
            ))
            .is_file()
        );
        assert!(!root.join("delivery-controller/tasks").exists());
        let jobs = product_assessment_jobs(root).expect("assessment jobs");
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0]["task_kind"], "product-assessment");
        assert_eq!(jobs[0]["codeagent_task_created"], false);
        assert_eq!(jobs[0]["external_request_made"], false);
        assert!(!root.join("outbox/codeagent-tasks.jsonl").exists());
    }

    #[test]
    fn fixed_world_mismatch_is_a_platform_capability_blocker() {
        let (root, result, input) = complete_staged_ui_need("fixed-world", |input| {
            input.capabilities.push("http".to_string());
            input.allowed_permissions.push("http".to_string());
        });
        assert_platform_capability_blocker(&root, &result);
        assert!(
            result["need_spec"]["validation_errors"]
                .as_array()
                .is_some_and(|errors| errors.iter().any(|error| error
                    .as_str()
                    .is_some_and(|message| message.contains("ui-only-reference world"))))
        );
        let model_settings = crate::model_settings::runtime(&root).expect("default model settings");
        let replay = complete_need(&root, input, 1_787_518_401_000, &model_settings.embedding)
            .expect("idempotent assessment replay");
        assert_eq!(
            replay["product_assessment"]["task_id"],
            result["product_assessment"]["task_id"]
        );
        assert_eq!(
            replay["product_assessment"]["created_at_utc"],
            result["product_assessment"]["created_at_utc"]
        );
        assert_eq!(product_assessment_jobs(&root).unwrap().len(), 1);
    }

    #[test]
    fn service_world_structural_imports_do_not_expand_required_authority() {
        let (_root, result, _input) = complete_staged_ui_need("service-no-import-tax", |input| {
            input.app_kind = "service".to_string();
            input.capabilities = vec!["clock".to_string(), "kv".to_string()];
            input.allowed_permissions = vec!["clock".to_string(), "kv".to_string()];
        });
        assert_eq!(result["need"]["status"], "needspec-complete");
        let target = &result["cloud_development"]["task_preparation"]["schema_preview"]["target"];
        assert_eq!(target["wit_world"], "service-only-reference");
        assert_eq!(
            target["required_capabilities"],
            json!([
                "vibapp:experimental-v0/clock@0.0.1",
                "vibapp:experimental-v0/kv@0.0.1"
            ])
        );
        let structural = target["required_imports"]
            .as_array()
            .expect("structural imports");
        assert_eq!(structural.len(), 8);
        assert!(
            structural
                .iter()
                .any(|item| { item == "vibapp:experimental-v0/scheduler@0.0.1" })
        );
        assert!(
            structural
                .iter()
                .any(|item| { item == "vibapp:experimental-v0/system-metrics@0.0.1" })
        );
        assert_eq!(
            result["need_spec"]["document"]["permission_ceiling"]["allowed_interfaces"],
            json!([
                "vibapp:experimental-v0/clock@0.0.1",
                "vibapp:experimental-v0/kv@0.0.1"
            ])
        );
        let task = &result["cloud_development"]["task_preparation"]["schema_preview"];
        let binding = &result["cloud_development"]["task_preparation"]["registry_evidence_binding"];
        assert_eq!(binding["schema_version"], TRUSTED_REGISTRY_EVIDENCE_SCHEMA);
        assert_eq!(binding["document_type"], "registry-development-evidence");
        assert_eq!(
            binding["immutable_task_digest_sha256"],
            task["immutable_task_digest_sha256"]
        );
        assert_eq!(
            binding["need_spec_digest_sha256"],
            task["need_spec_digest_sha256"]
        );
        assert_eq!(binding["need_id"], task["need_spec"]["need_id"]);
        assert_eq!(
            binding["registry_request_id"],
            result["registry"]["request_id"]
        );
        assert_eq!(
            binding["registry_evidence_sha256"],
            cloud_task_preview::canonical_sha256(&result["registry"]).unwrap()
        );
    }

    #[test]
    fn orphaned_terminal_attempt_rebuilds_only_its_product_assessment_index() {
        let (root, result, input) = complete_staged_ui_need("orphaned-assessment", |input| {
            input.capabilities.push("http".to_string());
            input.allowed_permissions.push("http".to_string());
        });
        let task_id = result["product_assessment"]["task_id"]
            .as_str()
            .expect("assessment task id");
        let task_path = root.join(format!("product-assessments/tasks/{task_id}/task.json"));
        fs::remove_file(&task_path).expect("simulate crash before task index persist");

        let model_settings = crate::model_settings::runtime(&root).expect("default model settings");
        let recovered = complete_need(&root, input, 1_787_518_401_000, &model_settings.embedding)
            .expect("recover task index from terminal attempt");
        assert!(task_path.is_file());
        assert_eq!(
            recovered["product_assessment"]["created_at_utc"],
            result["product_assessment"]["created_at_utc"]
        );
        assert_eq!(product_assessment_jobs(&root).unwrap().len(), 1);
        assert!(!root.join("delivery-controller/tasks").exists());
        assert!(!root.join("outbox/codeagent-tasks.jsonl").exists());
    }

    #[test]
    fn unknown_capability_is_a_platform_capability_blocker_without_secondary_world_errors() {
        let (root, result, _input) = complete_staged_ui_need("unknown-capability", |input| {
            input
                .capabilities
                .push("roomhash-collaboration".to_string());
        });
        assert_platform_capability_blocker(&root, &result);
        let errors = result["need_spec"]["validation_errors"]
            .as_array()
            .expect("validation errors");
        assert_eq!(errors.len(), 1);
        assert!(
            errors[0]
                .as_str()
                .is_some_and(|message| message.contains("当前平台不支持"))
        );
    }

    #[test]
    fn incomplete_needspec_keeps_all_task_and_external_truth_false() {
        let suffix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let need_id = format!("need-{suffix:x}");
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(&need_id);
        fs::create_dir_all(root.join("registry-requests")).expect("create request directory");
        fs::create_dir_all(root.join("history")).expect("create history directory");
        let draft = build_need_spec(
            &need_id,
            "做一个本地保存事项的专注界面应用。",
            shape_for_kind("ui").unwrap(),
        );
        write_private_json(
            &root.join(format!("registry-requests/{need_id}.json")),
            &draft,
        )
        .expect("write draft");
        append_history(
            &root.join("history/need-requests.jsonl"),
            &json!({"need_id": need_id, "route": "refinement"}),
        )
        .expect("write route");
        let model_settings = crate::model_settings::runtime(&root).expect("default model settings");
        let result = complete_need(
            &root,
            CompleteNeedInput {
                need_id,
                app_kind: "ui".to_string(),
                capabilities: vec!["kv".to_string(), "settings".to_string()],
                allowed_permissions: vec![
                    "clock".to_string(),
                    "kv".to_string(),
                    "log".to_string(),
                    "host-info".to_string(),
                    "settings".to_string(),
                ],
                forbidden_permissions: vec!["http".to_string()],
                permission_ceiling_confirmed: false,
                negative_constraints: String::new(),
                negative_constraints_confirmed: true,
                network_mode: "offline".to_string(),
                package_id: "ai.vibapp.test".to_string(),
                package_name: "Test".to_string(),
                package_version: "0.1.0".to_string(),
                acceptance_example: "事项在重启后仍然存在。".to_string(),
                proceed_after_recommendation: true,
                registry_embedding_consent: true,
                remote_processing_consent: true,
                public_publication_consent: true,
            },
            1_787_518_400_000,
            &model_settings.embedding,
        )
        .expect("refinement response");
        assert_eq!(result["need_spec"]["state"], "draft");
        assert_eq!(
            result["registry"]["refinement"]["reason_code"],
            "needspec-incomplete"
        );
        assert_ne!(
            result["cloud_development"]["blockers"][0],
            "unsupported-capability-combination"
        );
        assert!(result["need_spec"]["canonical_digest_sha256"].is_null());
        assert_eq!(
            result["cloud_development"]["task_preparation"]["task_created"],
            false
        );
        assert!(result["cloud_development"]["task_preparation"]["task"].is_null());
        assert_eq!(result["cloud_development"]["external_request_made"], false);
        assert_eq!(result["cloud_development"]["publication"]["consent"], true);
        assert_eq!(
            result["cloud_development"]["publication"]["performed"],
            false
        );
        assert!(!root.join("outbox/codeagent-tasks.jsonl").exists());
    }
}
