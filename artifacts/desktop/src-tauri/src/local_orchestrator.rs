use crate::native_platform;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAX_TASK_BYTES: usize = 128 * 1024;
const MAX_REPLY_BYTES: usize = 64 * 1024;
const MAX_RECEIPT_BYTES: u64 = 128 * 1024;
const MAX_RECEIPTS: usize = 64;
const SUBMIT_TIMEOUT: Duration = Duration::from_secs(45);
const PREFLIGHT_TIMEOUT: Duration = Duration::from_secs(10);
const EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED: &str =
    "必须单独确认外部 CodeAgent 可能产生费用或消耗配额；本次没有启动。";

fn terminate_bounded_child(child: &mut Child) {
    #[cfg(unix)]
    {
        if let Ok(process_group) = i32::try_from(child.id()) {
            // The child is created as its own process-group leader.  Negative
            // PID signalling is intentionally limited to that exact group.
            unsafe {
                libc::kill(-process_group, libc::SIGTERM);
            }
            // Keep the group leader unreaped during the grace period so its PID
            // cannot be reused before the final group signal.
            thread::sleep(Duration::from_millis(500));
            unsafe {
                libc::kill(-process_group, libc::SIGKILL);
            }
            let _ = child.wait();
            return;
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn development_path(packaged: &str, development: &str) -> Result<PathBuf, String> {
    native_platform::resource_file(
        packaged,
        &Path::new(env!("CARGO_MANIFEST_DIR")).join(development),
    )
}

fn orchestrator_path() -> Result<PathBuf, String> {
    development_path(
        "orchestrator/orchestrator.py",
        "../../orchestrator/orchestrator.py",
    )
}

fn cloud_agent_path() -> Result<PathBuf, String> {
    development_path(
        "cloud-agent/cloud_agent.py",
        "../../cloud-agent/cloud_agent.py",
    )
}

fn codeagent_adapter_path() -> Result<PathBuf, String> {
    development_path(
        "codeagent-adapter/codeagent_adapter.py",
        "../../codeagent-adapter/codeagent_adapter.py",
    )
}

pub fn codeagent_adapter_available() -> bool {
    codeagent_preflight("codex", "gpt-5.6-sol").is_ok()
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn canonicalize(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys = map.keys().collect::<Vec<_>>();
            keys.sort_unstable();
            let mut canonical = Map::new();
            for key in keys {
                canonical.insert(key.clone(), canonicalize(&map[key]));
            }
            Value::Object(canonical)
        }
        Value::Array(items) => Value::Array(items.iter().map(canonicalize).collect()),
        _ => value.clone(),
    }
}

fn canonical_sha256(value: &Value) -> Result<String, String> {
    let bytes = serde_json::to_vec(&canonicalize(value))
        .map_err(|error| format!("无法生成 CodeAgent identity canonical JSON：{error}"))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn exact_object_keys(value: &Value, keys: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
    })
}

fn validate_provider_execution_identity(provider: &Value) -> Result<(), String> {
    let identity = &provider["provider_execution_identity"];
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
        return Err("CodeAgent preflight 缺少严格的 provider execution identity。".to_string());
    }
    for value in [
        identity["endpoint"]["endpoint_sha256"].as_str(),
        identity["runtime"]["adapter_sha256"].as_str(),
        identity["runtime"]["executable_sha256"].as_str(),
        identity["non_secret_config_sha256"].as_str(),
        identity["identity_sha256"].as_str(),
    ] {
        if !value.is_some_and(valid_sha256) {
            return Err("CodeAgent provider execution identity 含有无效摘要。".to_string());
        }
    }
    for value in [
        identity["endpoint"]["kind"].as_str(),
        identity["endpoint"]["canonical_endpoint"].as_str(),
        identity["runtime"]["adapter_id"].as_str(),
        identity["runtime"]["adapter_version"].as_str(),
        identity["runtime"]["package_id"].as_str(),
        identity["runtime"]["package_version"].as_str(),
    ] {
        if value.is_none_or(|value| {
            value.is_empty()
                || value.len() > 512
                || value.trim() != value
                || value.chars().any(char::is_control)
        }) {
            return Err("CodeAgent provider execution identity 含有无效文本。".to_string());
        }
    }
    let endpoint_body = json!({
        "kind": identity["endpoint"]["kind"],
        "canonical_endpoint": identity["endpoint"]["canonical_endpoint"],
    });
    if canonical_sha256(&endpoint_body)? != identity["endpoint"]["endpoint_sha256"] {
        return Err("CodeAgent endpoint identity 摘要不匹配。".to_string());
    }
    let mut identity_body = identity.clone();
    identity_body
        .as_object_mut()
        .ok_or_else(|| "CodeAgent provider execution identity 不是对象。".to_string())?
        .remove("identity_sha256");
    if canonical_sha256(&identity_body)? != identity["identity_sha256"] {
        return Err("CodeAgent provider execution identity 摘要不匹配。".to_string());
    }
    if provider["adapter_id"] != identity["runtime"]["adapter_id"]
        || provider["adapter_version"] != identity["runtime"]["adapter_version"]
        || provider["adapter_sha256"] != identity["runtime"]["adapter_sha256"]
        || provider["executable_version"] != identity["runtime"]["package_version"]
        || provider["executable_sha256"] != identity["runtime"]["executable_sha256"]
    {
        return Err("CodeAgent preflight 顶层身份与任务身份不一致。".to_string());
    }
    Ok(())
}

fn provider_environment_names(provider_id: &str) -> Result<&'static [&'static str], String> {
    match provider_id {
        "codex" => Ok(&["HOME", "CODEX_HOME"]),
        "claude-code" => Ok(&["ANTHROPIC_API_KEY"]),
        "opencode" => Ok(&["HOME"]),
        "gemini-cli" => Ok(&["HOME", "GEMINI_API_KEY"]),
        _ => Err(format!("不支持的 CodeAgent provider：{provider_id}")),
    }
}

fn inherit_provider_environment(command: &mut Command, provider_id: &str) -> Result<(), String> {
    for name in provider_environment_names(provider_id)? {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    Ok(())
}

fn provider_id_to_task_provider(provider_id: &str) -> &'static str {
    match provider_id {
        "codex" => "openai-codex",
        "claude-code" => "anthropic-claude-code",
        "opencode" => "opencode",
        "gemini-cli" => "google-gemini-cli",
        _ => "openai-codex",
    }
}

pub fn fallback_paused_provider_observation(
    provider_id: &str,
    model: &str,
    blocker_code: &str,
    blocker_message: &str,
) -> Result<Value, String> {
    let task_provider = provider_id_to_task_provider(provider_id);
    let endpoint_body = json!({
        "kind": "provider-managed",
        "canonical_endpoint": "provider-managed",
    });
    let endpoint = json!({
        "kind": endpoint_body["kind"],
        "canonical_endpoint": endpoint_body["canonical_endpoint"],
        "endpoint_sha256": canonical_sha256(&endpoint_body)?,
    });
    let runtime = json!({
        "adapter_id": "local-codeagent-adapter",
        "adapter_version": "vibapp.codeagent-adapter.experimental-v1",
        "adapter_sha256": "0".repeat(64),
        "package_id": format!("{provider_id}-cli"),
        "package_version": format!("{provider_id} unconfigured"),
        "executable_sha256": "0".repeat(64),
    });
    let identity_body = json!({
        "schema_version": "vibapp.provider-execution-identity.experimental-v1",
        "endpoint": endpoint,
        "runtime": runtime,
        "non_secret_config_sha256": "0".repeat(64),
    });
    let identity = json!({
        "schema_version": identity_body["schema_version"],
        "endpoint": identity_body["endpoint"],
        "runtime": identity_body["runtime"],
        "non_secret_config_sha256": identity_body["non_secret_config_sha256"],
        "identity_sha256": canonical_sha256(&identity_body)?,
    });
    let mut provider = json!({
        "provider_id": provider_id,
        "task_provider": task_provider,
        "model": model,
        "identity_observed": true,
        "available": false,
        "execution_available": false,
        "execution_blocker": {
            "code": blocker_code,
            "message": blocker_message,
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
        "executable_path": format!("/unavailable/{provider_id}"),
        "executable_version": identity["runtime"]["package_version"],
        "executable_sha256": identity["runtime"]["executable_sha256"],
        "adapter_id": identity["runtime"]["adapter_id"],
        "adapter_version": identity["runtime"]["adapter_version"],
        "adapter_sha256": identity["runtime"]["adapter_sha256"],
        "provider_execution_identity": identity,
    });
    if provider_id == "codex" {
        provider["identity_policy_version"] = json!("vibapp.codex-cli-compatibility.experimental-v1");
        provider["identity_basis"] = json!("apple-developer-id-and-static-cask-identity");
        provider["signing_team_identifier"] = json!("2DC432GLL2");
        provider["signature_requirement_satisfied"] = json!(true);
        provider["protocol_version"] = json!("vibapp.codex-exec-protocol.experimental-v1");
        provider["protocol_preflight_passed"] = json!(false);
        provider["version_within_exercised_range"] = json!(false);
        provider["digest_reviewed"] = json!(false);
    }
    validate_provider_execution_identity(&provider)?;
    Ok(provider)
}

fn parse_preflight_reply(
    bytes: &[u8],
    success: bool,
    provider_id: &str,
    model: &str,
) -> Result<Value, String> {
    if bytes.is_empty() || bytes.len() > MAX_REPLY_BYTES {
        return fallback_paused_provider_observation(
            provider_id,
            model,
            "provider-preflight-empty",
            "CodeAgent preflight 未返回有效输出。",
        );
    }
    let value: Value = match serde_json::from_slice(bytes) {
        Ok(v) => v,
        Err(error) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "provider-preflight-malformed-json",
                &format!("CodeAgent preflight 返回了无效 JSON：{error}"),
            );
        }
    };
    if !success || value["ok"] != true {
        let code = value["code"]
            .as_str()
            .unwrap_or("provider-preflight-failed");
        let message = value["message"]
            .as_str()
            .unwrap_or("CodeAgent preflight 未通过");
        return fallback_paused_provider_observation(provider_id, model, code, message);
    }
    let provider = &value["provider"];
    let digest = provider["executable_sha256"].as_str().unwrap_or("");
    if provider["provider_id"] != provider_id
        || provider["model"] != model
        || provider["task_provider"].as_str().is_none()
        || provider["available"].as_bool().is_none()
        || provider["execution_available"].as_bool().is_none()
        || provider["identity_observed"] != true
        || provider["source_authoring_only"] != true
        || provider["provider_process_started"] != false
        || provider["external_request_attempted"] != false
        || provider["external_request_observed"] != false
        || provider["consent_consumed"] != false
        || provider["compile_authority"] != false
        || provider["verify_authority"] != false
        || provider["install_authority"] != false
        || provider["publish_authority"] != false
        || provider["executable_path"]
            .as_str()
            .is_none_or(|path| path.is_empty() || path.len() > 4096)
        || provider["executable_version"]
            .as_str()
            .is_none_or(|version| version.is_empty() || version.len() > 256)
        || digest.len() != 64
        || !digest.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err("CodeAgent preflight 返回了不可信的身份状态。".to_string());
    }
    validate_provider_execution_identity(provider)?;
    if provider["execution_available"] != true {
        let blocker = &provider["execution_blocker"];
        if blocker["code"]
            .as_str()
            .is_none_or(|value| value.is_empty() || value.len() > 128)
            || blocker["message"]
                .as_str()
                .is_none_or(|value| value.is_empty() || value.len() > 1000)
        {
            return Err("CodeAgent preflight 缺少安全执行阻止原因。".to_string());
        }
    }
    let docker_provider = matches!(provider_id, "codex" | "opencode") && provider["containment_backend"] == "docker-whole-container";
    if docker_provider {
        let image = provider["docker_image_id"].as_str().unwrap_or("");
        let delivery = if provider_id == "codex" { "host-only-stdio-responses-relay" } else { "host-only-stdio-chat-relay" };
        if image.len() != 71 || !image.starts_with("sha256:")
            || !image[7..].bytes().all(|byte| byte.is_ascii_hexdigit())
            || provider["docker_policy"]["image_id"] != image
            || provider["docker_policy"]["model"] != model
            || provider["provider_tool_network"] != "none"
            || provider["credential_delivery"] != delivery
            || provider["executable_path"] != format!("docker://{image}/usr/local/bin/{provider_id}")
            || provider["provider_execution_identity"]["non_secret_config_sha256"] != canonical_sha256(&provider["docker_policy"])?
        {
            return Err("Docker CodeAgent 的镜像、连接或隔离策略绑定无效。".to_string());
        }
    }
    if provider_id == "codex" && !docker_provider
        && (provider["identity_policy_version"] != "vibapp.codex-cli-compatibility.experimental-v1"
            || !matches!(
                provider["identity_basis"].as_str(),
                Some(
                    "apple-developer-id-and-static-cask-identity"
                        | "apple-developer-id-and-live-protocol"
                )
            )
            || provider["signing_team_identifier"] != "2DC432GLL2"
            || provider["signature_requirement_satisfied"] != true
            || provider["protocol_version"] != "vibapp.codex-exec-protocol.experimental-v1"
            || provider["version_within_exercised_range"]
                .as_bool()
                .is_none()
            || provider["digest_reviewed"].as_bool().is_none()
            || (provider["execution_available"] == true
                && (provider["identity_basis"] != "apple-developer-id-and-live-protocol"
                    || provider["protocol_preflight_passed"] != true)))
    {
        return Err("Codex 的发布者身份或命令协议 preflight 未通过。".to_string());
    }
    Ok(provider.clone())
}

pub fn codeagent_identity_observation(provider_id: &str, model: &str) -> Result<Value, String> {
    provider_environment_names(provider_id)?;
    if model.is_empty()
        || model.trim() != model
        || model.len() > 256
        || model.chars().any(char::is_control)
        || model.starts_with('-')
    {
        return Err("CodeAgent preflight 需要明确且有效的 model。".to_string());
    }
    let python = match native_platform::python_executable(true) {
        Ok(p) => p,
        Err(err) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "python-unavailable",
                &format!("无法定位 Python 环境：{err}"),
            );
        }
    };
    let adapter = match codeagent_adapter_path() {
        Ok(a) => a,
        Err(err) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "codeagent-adapter-unavailable",
                &format!("无法定位 CodeAgent adapter：{err}"),
            );
        }
    };
    let cloud_agent = match cloud_agent_path() {
        Ok(c) => c,
        Err(err) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "cloud-agent-unavailable",
                &format!("无法定位 cloud agent：{err}"),
            );
        }
    };
    let safe_path = match native_platform::safe_path() {
        Ok(p) => p,
        Err(err) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "safe-path-unavailable",
                &format!("无法配置安全 PATH：{err}"),
            );
        }
    };
    let mut command = Command::new(python);
    command
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(adapter)
        .arg("--cloud-agent")
        .arg(cloud_agent)
        .arg("preflight")
        .arg("--provider")
        .arg(provider_id)
        .arg("--model")
        .arg(model)
        .env_clear()
        .env("PATH", safe_path)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Err(err) = inherit_provider_environment(&mut command, provider_id) {
        return fallback_paused_provider_observation(
            provider_id,
            model,
            "provider-env-failed",
            &format!("无法配置 provider 环境变量：{err}"),
        );
    }
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(not(unix))]
    return fallback_paused_provider_observation(
        provider_id,
        model,
        "process-tree-containment-unavailable",
        "当前平台不能安全启动 CodeAgent preflight。",
    );
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "provider-preflight-spawn-failed",
                &format!("无法启动 CodeAgent preflight：{error}"),
            );
        }
    };
    let deadline = Instant::now() + PREFLIGHT_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                terminate_bounded_child(&mut child);
                return fallback_paused_provider_observation(
                    provider_id,
                    model,
                    "provider-preflight-timeout",
                    "CodeAgent preflight 超过 10 秒，已安全暂停执行。",
                );
            }
            Err(error) => {
                terminate_bounded_child(&mut child);
                return fallback_paused_provider_observation(
                    provider_id,
                    model,
                    "provider-preflight-error",
                    &format!("无法读取 CodeAgent preflight 状态：{error}"),
                );
            }
        }
    }
    let output = match child.wait_with_output() {
        Ok(out) => out,
        Err(error) => {
            return fallback_paused_provider_observation(
                provider_id,
                model,
                "provider-preflight-output-error",
                &format!("无法收集 CodeAgent preflight 输出：{error}"),
            );
        }
    };
    if output.stdout.len() > MAX_REPLY_BYTES || output.stderr.len() > MAX_REPLY_BYTES {
        return fallback_paused_provider_observation(
            provider_id,
            model,
            "provider-preflight-overflow",
            "CodeAgent preflight 日志超过 64 KiB 上限。",
        );
    }
    parse_preflight_reply(&output.stdout, output.status.success(), provider_id, model)
}

pub fn codeagent_preflight(provider_id: &str, model: &str) -> Result<Value, String> {
    let provider = codeagent_identity_observation(provider_id, model)?;
    if provider["available"] != true || provider["execution_available"] != true {
        let blocker = &provider["execution_blocker"];
        return Err(format!(
            "{}: {}",
            blocker["code"]
                .as_str()
                .unwrap_or("provider-execution-unavailable"),
            blocker["message"]
                .as_str()
                .unwrap_or("CodeAgent 当前不能安全执行")
        ));
    }
    Ok(provider)
}

fn parse_reply(bytes: &[u8], success: bool) -> Result<Value, String> {
    if bytes.is_empty() || bytes.len() > MAX_REPLY_BYTES {
        return Err("本地 Orchestrator 返回大小无效。".to_string());
    }
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|error| format!("本地 Orchestrator 返回了无效 JSON：{error}"))?;
    if !success || value["ok"] != true {
        let code = value["code"].as_str().unwrap_or("orchestrator-failed");
        let message = value["message"].as_str().unwrap_or("本地任务提交失败");
        return Err(format!("{code}: {message}"));
    }
    let receipt = &value["receipt"];
    let status = receipt["status"].as_str().unwrap_or("");
    if !matches!(
        status,
        "queued-for-codeagent" | "waiting-for-external-runner"
    ) {
        return Err("本地 Orchestrator 返回了不允许的任务状态。".to_string());
    }
    if receipt["external_request_attempted"] != false
        || receipt["external_request_observed"] != false
        || !receipt["gateway_request_id"].is_null()
    {
        return Err("本地 Orchestrator 返回了不可信的外部执行标记。".to_string());
    }
    Ok(value)
}

pub fn submit(
    queue_root: &Path,
    task: &Value,
    registry: &Value,
    explicit_user_submit: bool,
) -> Result<Value, String> {
    if !explicit_user_submit {
        return Err("必须由用户点击明确提交按钮。".to_string());
    }
    if task["need_spec_complete"] != true
        || task["remote_processing_consent"] != true
        || task["consent"]["decision"] != "granted"
        || task["consent"]["single_use"] != true
    {
        return Err("NeedSpec、schema preview 或一次性远程授权尚未满足。".to_string());
    }
    validate_registry_no_match(registry)?;
    let bytes = serde_json::to_vec(task).map_err(|error| format!("无法序列化任务：{error}"))?;
    if bytes.is_empty() || bytes.len() > MAX_TASK_BYTES {
        return Err("任务预览超过 128 KiB 上限。".to_string());
    }
    fs::create_dir_all(queue_root).map_err(|error| format!("无法创建本地开发队列：{error}"))?;
    let mut command = Command::new(native_platform::python_executable(true)?);
    command
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(orchestrator_path()?)
        .arg("submit")
        .arg("--root")
        .arg(queue_root)
        .arg("--cloud-agent")
        .arg(cloud_agent_path()?)
        .arg("--task")
        .arg("-")
        .arg("--explicit-submit")
        .env_clear()
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(not(unix))]
    return Err(
        "process-tree-containment-unavailable: 当前平台不能安全启动本地 Orchestrator。".to_string(),
    );
    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动本地 Orchestrator：{error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "无法打开 Orchestrator 输入。".to_string())?
        .write_all(&bytes)
        .map_err(|error| format!("无法发送任务预览：{error}"))?;
    let deadline = Instant::now() + SUBMIT_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                terminate_bounded_child(&mut child);
                return Err("本地 Orchestrator 超过 45 秒，已终止。".to_string());
            }
            Err(error) => {
                terminate_bounded_child(&mut child);
                return Err(format!("无法读取 Orchestrator 状态：{error}"));
            }
        }
    }
    let output = child
        .wait_with_output()
        .map_err(|error| format!("无法收集 Orchestrator 输出：{error}"))?;
    if output.stdout.len() > MAX_REPLY_BYTES || output.stderr.len() > MAX_REPLY_BYTES {
        return Err("本地 Orchestrator 日志超过 64 KiB 上限。".to_string());
    }
    parse_reply(&output.stdout, output.status.success())
}

pub fn start_codeagent(
    queue_root: &Path,
    task: &Value,
    receipt: &Value,
    provider_id: &str,
    model: &str,
    acknowledge_external_cost: bool,
) -> Result<Value, String> {
    if !acknowledge_external_cost {
        return Err(EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED.to_string());
    }
    // This legacy launcher must share the same hard gate as the delivery
    // controller.  Identity observation alone is insufficient: no adapter
    // process may start until a whole-descendant containment backend is ready.
    codeagent_preflight(provider_id, model)?;
    let digest = receipt["immutable_task_digest_sha256"]
        .as_str()
        .filter(|value| {
            value.len() == 64 && value.chars().all(|character| character.is_ascii_hexdigit())
        })
        .ok_or_else(|| "队列回执缺少有效任务摘要。".to_string())?;
    let job_id = task["job_id"]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "任务缺少 job_id。".to_string())?;
    let consent_id = task["consent"]["consent_id"]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "任务缺少一次性 consent_id。".to_string())?;
    let task_path = queue_root.join("ready").join(format!("task-{digest}.json"));
    if !task_path.is_file() || task_path.is_symlink() {
        return Err("本地队列中找不到可交给 CodeAgent 的任务文件。".to_string());
    }
    let output_root = queue_root.join("codeagent");
    let status_root = queue_root.join("codeagent-status");
    fs::create_dir_all(&output_root)
        .map_err(|error| format!("无法创建 CodeAgent 输出目录：{error}"))?;
    fs::create_dir_all(&status_root)
        .map_err(|error| format!("无法创建 CodeAgent 状态目录：{error}"))?;
    let status_path = status_root.join(format!("{digest}.json"));
    let mut command = Command::new(native_platform::python_executable(true)?);
    command
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(codeagent_adapter_path()?)
        .arg("--cloud-agent")
        .arg(cloud_agent_path()?)
        .arg("run")
        .arg("--provider")
        .arg(provider_id)
        .arg("--task")
        .arg(&task_path)
        .arg("--output-root")
        .arg(&output_root)
        .arg("--status-file")
        .arg(&status_path)
        .arg("--confirm-job")
        .arg(job_id)
        .arg("--confirm-consent")
        .arg(consent_id)
        .arg("--acknowledge-external-cost")
        .env_clear()
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if model.is_empty() || model.trim() != model || model.len() > 256 {
        return Err("任务缺少明确的 CodeAgent model 绑定。".to_string());
    }
    command.arg("--model").arg(model);
    inherit_provider_environment(&mut command, provider_id)?;
    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动本地 CodeAgent Adapter：{error}"))?;
    let process_id = child.id();
    thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(json!({
        "provider_id": provider_id,
        "adapter": "local-codeagent-adapter",
        "started": true,
        "process_id": process_id,
        "status": "codeagent-starting",
        "external_request_attempted": false,
        "external_request_observed": false,
        "external_cost_acknowledged": true
    }))
}

fn validate_registry_no_match(registry: &Value) -> Result<(), String> {
    if registry["route"] != "refinement"
        || registry["recommendations"] != json!([])
        || registry["codeagent_handoff"]["created"] != false
        || registry["codeagent_handoff"]["permitted"] != false
    {
        return Err(
            "只有 Registry 明确无匹配、且尚未越权创建 handoff 时，才能提交 CodeAgent。".to_string(),
        );
    }
    let request_id = registry["request_id"]
        .as_str()
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .ok_or_else(|| "Registry no-match 证据缺少有效 request_id。".to_string())?;
    if request_id.chars().any(char::is_control) {
        return Err("Registry request_id 含有控制字符。".to_string());
    }
    Ok(())
}

fn receipt_to_job(receipt: Value, adapter_status: Option<&Value>) -> Option<Value> {
    let status = receipt["status"].as_str()?;
    if !matches!(
        status,
        "queued-for-codeagent" | "waiting-for-external-runner"
    ) || receipt["external_request_attempted"] != false
        || receipt["external_request_observed"] != false
    {
        return None;
    }
    let adapter_state = adapter_status
        .filter(|value| {
            value["schema_version"] == "vibapp.codeagent-adapter-status.experimental-v2"
        })
        .filter(|value| {
            receipt["immutable_task_digest_sha256"]
                .as_str()
                .is_some_and(valid_sha256)
                && receipt["provider_execution_identity_sha256"]
                    .as_str()
                    .is_some_and(valid_sha256)
                && receipt["consent_id"]
                    .as_str()
                    .is_some_and(|consent| !consent.is_empty() && consent.len() <= 128)
                && value["job_id"] == receipt["job_id"]
                && value["immutable_task_digest_sha256"] == receipt["immutable_task_digest_sha256"]
                && value["consent_id"] == receipt["consent_id"]
                && value["provider_execution_identity_sha256"]
                    == receipt["provider_execution_identity_sha256"]
                && matches!(
                    value["provider_id"].as_str(),
                    Some("codex" | "claude-code" | "opencode" | "gemini-cli")
                )
        });
    let observed_adapter_status = adapter_state
        .and_then(|value| value["status"].as_str())
        .unwrap_or("legacy-state-unproven");
    // Legacy status paths are digest-only and carry no v3 attempt identity.
    // Preserve terminal history, but never project a stale/mismatched legacy
    // process observation as a currently running development attempt.
    let adapter_job_status = match observed_adapter_status {
        "source-ready" | "codeagent-failed" => observed_adapter_status,
        _ => "legacy-state-unproven",
    };
    let (progress, current_stage, codeagent_status, builder_status, verification_status, summary) =
        match adapter_job_status {
            "codeagent-running" => (
                adapter_state
                    .and_then(|value| value["progress_percent"].as_u64())
                    .unwrap_or(30),
                "codeagent-authoring-source",
                "running",
                "pending",
                "running",
                "选定的 CodeAgent 已接单，正在有界工作区内编写 Rust 源码。Builder、验收、安装和发布尚未启动。",
            ),
            "source-ready" => (
                60,
                "awaiting-separate-builder",
                "succeeded",
                "pending",
                "ready-for-builder",
                "CodeAgent 已返回并通过源码交接审计；当前是未信任源码，等待独立 Builder 编译。",
            ),
            "codeagent-failed" => (
                0,
                "codeagent-failed-closed",
                "failed",
                "pending",
                "failed",
                "CodeAgent 或源码审计失败，任务已关闭；未调用 Builder、安装或发布。",
            ),
            _ => (
                0,
                "legacy-attempt-binding-unproven",
                "failed",
                "pending",
                "failed",
                "旧队列记录缺少 v3 task/attempt 绑定，保留历史但不会显示为运行中；请从原需求创建新 attempt。",
            ),
        };
    Some(json!({
        "job_id": receipt["job_id"],
        "need_id": receipt["need_id"],
        "title": receipt["title"],
        "route": "local-development-queue",
        "status": adapter_job_status,
        "progress_percent": progress,
        "current_stage": current_stage,
        "updated_at_utc": adapter_state.map(|value| &value["updated_at_utc"]).unwrap_or(&receipt["updated_at_utc"]),
        "stages": [
            {"kind": "queue", "status": "succeeded", "label": "进入本地开发队列"},
            {"kind": "validate", "status": "succeeded", "label": "schema 本地校验"},
            {"kind": "codeagent", "status": codeagent_status, "label": "本地 Codex 编写 Rust 源码"},
            {"kind": "builder", "status": builder_status, "label": "独立 Builder 编译"}
        ],
        "verification": {
            "status": verification_status,
            "independent": false,
            "summary": summary
        },
        "error": if adapter_job_status == "legacy-state-unproven" {
            json!({
                "stage": "legacy-migration",
                "code": "legacy-attempt-binding-unproven",
                "message": "旧队列记录无法证明精确 task/attempt 绑定，已安全停止运行态投影。"
            })
        } else {
            Value::Null
        },
        "codeagent_adapter_status": adapter_state,
        "queue_receipt": receipt
    }))
}

pub fn queued_jobs(queue_root: &Path) -> Vec<Value> {
    let receipts = queue_root.join("receipts");
    let Ok(entries) = fs::read_dir(receipts) else {
        return Vec::new();
    };
    let mut paths = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    paths.sort();
    paths.truncate(MAX_RECEIPTS);
    paths
        .into_iter()
        .filter_map(|path| {
            let metadata = path.symlink_metadata().ok()?;
            if !metadata.file_type().is_file()
                || metadata.file_type().is_symlink()
                || metadata.len() > MAX_RECEIPT_BYTES
            {
                return None;
            }
            let receipt = serde_json::from_slice::<Value>(&fs::read(&path).ok()?).ok()?;
            let digest = receipt["immutable_task_digest_sha256"].as_str()?;
            let adapter_path = queue_root
                .join("codeagent-status")
                .join(format!("{digest}.json"));
            let adapter_status = adapter_path
                .symlink_metadata()
                .ok()
                .filter(|metadata| {
                    metadata.file_type().is_file()
                        && !metadata.file_type().is_symlink()
                        && metadata.len() <= MAX_RECEIPT_BYTES
                })
                .and_then(|_| fs::read(adapter_path).ok())
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
            receipt_to_job(receipt, adapter_status.as_ref())
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn receipt(status: &str) -> Value {
        json!({
            "job_id": "job-test",
            "need_id": "need-test",
            "title": "测试应用",
            "status": status,
            "updated_at_utc": "2026-08-24T05:00:00Z",
            "external_request_attempted": false,
            "external_request_observed": false,
            "immutable_task_digest_sha256": "a".repeat(64),
            "provider_execution_identity_sha256": "b".repeat(64),
            "consent_id": "consent-test",
            "gateway_request_id": null
        })
    }

    fn contained_preflight_reply() -> Value {
        let endpoint_body = json!({
            "kind": "provider-managed",
            "canonical_endpoint": "provider-managed",
        });
        let endpoint = json!({
            "kind": endpoint_body["kind"],
            "canonical_endpoint": endpoint_body["canonical_endpoint"],
            "endpoint_sha256": canonical_sha256(&endpoint_body).unwrap(),
        });
        let runtime = json!({
            "adapter_id": "local-codeagent-adapter",
            "adapter_version": "vibapp.codeagent-adapter.experimental-v1",
            "adapter_sha256": "a".repeat(64),
            "package_id": "openai-codex-cli",
            "package_version": "codex-cli 0.151.0",
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
            "identity_sha256": canonical_sha256(&identity_body).unwrap(),
        });
        json!({
            "ok": true,
            "provider": {
                "provider_id": "codex",
                "task_provider": "openai-codex",
                "model": "gpt-test",
                "identity_observed": true,
                "available": false,
                "execution_available": false,
                "execution_blocker": {
                    "code": "local-live-containment-unavailable",
                    "message": "whole-descendant containment is unavailable"
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
                "executable_path": "/signed/codex",
                "executable_version": identity["runtime"]["package_version"],
                "executable_sha256": identity["runtime"]["executable_sha256"],
                "adapter_id": identity["runtime"]["adapter_id"],
                "adapter_version": identity["runtime"]["adapter_version"],
                "adapter_sha256": identity["runtime"]["adapter_sha256"],
                "provider_execution_identity": identity,
                "identity_policy_version": "vibapp.codex-cli-compatibility.experimental-v1",
                "identity_basis": "apple-developer-id-and-static-cask-identity",
                "signing_team_identifier": "2DC432GLL2",
                "signature_requirement_satisfied": true,
                "protocol_version": "vibapp.codex-exec-protocol.experimental-v1",
                "protocol_preflight_passed": false,
                "version_within_exercised_range": false,
                "digest_reviewed": false,
            }
        })
    }

    #[test]
    fn identity_observation_accepts_signed_newer_codex_but_not_execution() {
        let reply = contained_preflight_reply();
        let observed = parse_preflight_reply(
            &serde_json::to_vec(&reply).unwrap(),
            true,
            "codex",
            "gpt-test",
        )
        .unwrap();
        assert_eq!(observed["identity_observed"], true);
        assert_eq!(observed["version_within_exercised_range"], false);
        assert_eq!(observed["execution_available"], false);
    }

    #[test]
    fn docker_preflight_binds_image_model_policy_and_credential_boundary() {
        let mut reply = contained_preflight_reply();
        let provider = &mut reply["provider"];
        let image = format!("sha256:{}", "a".repeat(64));
        provider["available"] = json!(true);
        provider["execution_available"] = json!(true);
        provider["execution_blocker"] = Value::Null;
        provider["containment_backend"] = json!("docker-whole-container");
        provider["docker_image_id"] = json!(image);
        provider["docker_policy"] = json!({"image_id": image, "model": "gpt-test"});
        provider["provider_tool_network"] = json!("none");
        provider["credential_delivery"] = json!("host-only-stdio-responses-relay");
        provider["executable_path"] = json!(format!("docker://{image}/usr/local/bin/codex"));
        provider["provider_execution_identity"]["non_secret_config_sha256"] = json!(canonical_sha256(&provider["docker_policy"]).unwrap());
        let mut identity_body = provider["provider_execution_identity"].clone();
        identity_body.as_object_mut().unwrap().remove("identity_sha256");
        provider["provider_execution_identity"]["identity_sha256"] = json!(canonical_sha256(&identity_body).unwrap());
        assert!(parse_preflight_reply(&serde_json::to_vec(&reply).unwrap(), true, "codex", "gpt-test").is_ok());
        for (key, invalid) in [("provider_tool_network", "host"), ("credential_delivery", "host-directory-mount"), ("docker_image_id", "codex:latest")] {
            let mut forged = reply.clone();
            forged["provider"][key] = json!(invalid);
            assert!(parse_preflight_reply(&serde_json::to_vec(&forged).unwrap(), true, "codex", "gpt-test").is_err());
        }
    }

    #[test]
    fn preflight_failure_recovers_to_fallback_paused_observation() {
        let failed_reply = json!({
            "ok": false,
            "code": "cloud-agent-unavailable",
            "message": "reviewed cloud-agent module is unavailable"
        });
        let observed = parse_preflight_reply(
            &serde_json::to_vec(&failed_reply).unwrap(),
            false,
            "opencode",
            "gpt-test",
        )
        .unwrap();
        assert_eq!(observed["identity_observed"], true);
        assert_eq!(observed["available"], false);
        assert_eq!(observed["execution_available"], false);
        assert_eq!(observed["execution_blocker"]["code"], "cloud-agent-unavailable");
        assert_eq!(observed["provider_id"], "opencode");
        assert_eq!(observed["task_provider"], "opencode");
    }

    #[test]
    fn identity_observation_rejects_a_tampered_aggregate_digest() {
        let mut reply = contained_preflight_reply();
        reply["provider"]["provider_execution_identity"]["identity_sha256"] = json!("d".repeat(64));
        assert!(
            parse_preflight_reply(
                &serde_json::to_vec(&reply).unwrap(),
                true,
                "codex",
                "gpt-test",
            )
            .is_err()
        );
    }

    #[test]
    fn only_durable_waiting_codeagent_status_is_rendered() {
        let legacy = receipt_to_job(receipt("queued-for-codeagent"), None).unwrap();
        assert_eq!(legacy["status"], "legacy-state-unproven");
        assert_eq!(legacy["verification"]["status"], "failed");
        assert_eq!(legacy["error"]["code"], "legacy-attempt-binding-unproven");
        assert!(receipt_to_job(receipt("waiting-for-external-runner"), None).is_some());
        assert!(receipt_to_job(receipt("dry-run-complete"), None).is_none());
        assert!(receipt_to_job(receipt("running-cloud"), None).is_none());
        let mut untruthful = receipt("dry-run-complete");
        untruthful["external_request_attempted"] = json!(true);
        assert!(receipt_to_job(untruthful, None).is_none());
    }

    #[test]
    fn legacy_running_status_never_masquerades_as_a_v3_attempt() {
        let adapter = json!({
            "schema_version": "vibapp.codeagent-adapter-status.experimental-v2",
            "job_id": "job-test",
            "provider_id": "codex",
            "immutable_task_digest_sha256": "a".repeat(64),
            "consent_id": "consent-test",
            "provider_execution_identity_sha256": "b".repeat(64),
            "status": "codeagent-running",
            "updated_at_utc": "2026-08-24T05:01:00Z"
        });
        let job = receipt_to_job(receipt("queued-for-codeagent"), Some(&adapter)).unwrap();
        assert_eq!(job["status"], "legacy-state-unproven");
        assert_eq!(job["progress_percent"], 0);
    }

    #[test]
    fn registry_no_match_is_required_before_codeagent_queueing() {
        let valid = json!({
            "request_id": "registry-test-1",
            "route": "refinement",
            "recommendations": [],
            "codeagent_handoff": {"created": false, "permitted": false}
        });
        assert!(validate_registry_no_match(&valid).is_ok());

        let mut recommended = valid.clone();
        recommended["route"] = json!("recommendation");
        recommended["recommendations"] = json!([{"app": {"id": "existing"}}]);
        assert!(validate_registry_no_match(&recommended).is_err());

        let mut forged_handoff = valid;
        forged_handoff["codeagent_handoff"]["created"] = json!(true);
        assert!(validate_registry_no_match(&forged_handoff).is_err());
    }

    #[test]
    fn local_codex_source_ready_is_not_reported_as_built_or_verified() {
        let adapter = json!({
            "schema_version": "vibapp.codeagent-adapter-status.experimental-v2",
            "job_id": "job-test",
            "provider_id": "codex",
            "immutable_task_digest_sha256": "a".repeat(64),
            "consent_id": "consent-test",
            "provider_execution_identity_sha256": "b".repeat(64),
            "status": "source-ready",
            "updated_at_utc": "2026-08-24T05:01:00Z"
        });
        let job = receipt_to_job(receipt("queued-for-codeagent"), Some(&adapter)).unwrap();
        assert_eq!(job["status"], "source-ready");
        assert_eq!(job["verification"]["status"], "ready-for-builder");
        assert_eq!(job["stages"][2]["status"], "succeeded");
        assert_eq!(job["stages"][3]["status"], "pending");
    }

    #[test]
    fn reply_rejects_external_truth_or_unknown_status() {
        let valid = json!({"ok": true, "receipt": receipt("queued-for-codeagent")});
        assert!(parse_reply(&serde_json::to_vec(&valid).unwrap(), true).is_ok());
        let legacy = json!({"ok": true, "receipt": receipt("waiting-for-external-runner")});
        assert!(parse_reply(&serde_json::to_vec(&legacy).unwrap(), true).is_ok());
        let mut bad = valid.clone();
        bad["receipt"]["external_request_observed"] = json!(true);
        assert!(parse_reply(&serde_json::to_vec(&bad).unwrap(), true).is_err());
        bad = valid;
        bad["receipt"]["status"] = json!("cloud-running");
        assert!(parse_reply(&serde_json::to_vec(&bad).unwrap(), true).is_err());
    }

    #[test]
    fn start_codeagent_rejects_missing_cost_authority_before_io() {
        let error = start_codeagent(
            Path::new("/not-used/queue"),
            &json!({}),
            &json!({}),
            "codex",
            "gpt-explicit-not-started",
            false,
        )
        .unwrap_err();
        assert_eq!(error, EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED);
    }
}
#[test]
fn provider_environment_is_an_exact_allowlist() {
    assert_eq!(
        provider_environment_names("codex").unwrap(),
        ["HOME", "CODEX_HOME"]
    );
    assert_eq!(
        provider_environment_names("claude-code").unwrap(),
        ["ANTHROPIC_API_KEY"]
    );
    assert_eq!(provider_environment_names("opencode").unwrap(), ["HOME"]);
    assert_eq!(
        provider_environment_names("gemini-cli").unwrap(),
        ["HOME", "GEMINI_API_KEY"]
    );
    assert!(provider_environment_names("unknown").is_err());
}
