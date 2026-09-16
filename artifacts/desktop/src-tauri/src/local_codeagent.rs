use serde_json::{json, Value};
use crate::native_platform;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, SystemTime};

const MAX_INPUT_BYTES: usize = 256 * 1024;
const MAX_REPLY_BYTES: usize = 256 * 1024;
const RUN_TIMEOUT: Duration = Duration::from_secs(180);

fn script_path() -> Result<PathBuf, String> {
    native_platform::resource_file(
        "local-codeagent/local_codeagent.py",
        &Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../local-codeagent/local_codeagent.py"),
    )
}

fn wait(
    mut child: std::process::Child,
    timeout: Duration,
    label: &str,
) -> Result<std::process::Output, String> {
    let deadline = SystemTime::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if SystemTime::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("{label} 超时并已终止。"));
            }
            Err(error) => return Err(format!("无法读取 {label} 状态：{error}")),
        }
    }
    child
        .wait_with_output()
        .map_err(|error| format!("无法收集 {label} 输出：{error}"))
}

fn parse_reply(output: std::process::Output) -> Result<Value, String> {
    if output.stdout.is_empty()
        || output.stdout.len() > MAX_REPLY_BYTES
        || output.stderr.len() > MAX_REPLY_BYTES
    {
        return Err("本地 CodeAgent 返回大小无效。".to_string());
    }
    let value: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("本地 CodeAgent 返回无效 JSON：{error}"))?;
    if !output.status.success() || value["ok"] != true {
        return Err(format!(
            "{}: {}",
            value["code"].as_str().unwrap_or("local-chain-failed"),
            value["message"].as_str().unwrap_or("本地 CodeAgent 失败")
        ));
    }
    let receipt = &value["receipt"];
    if receipt["status"] != "private-candidate-ready"
        || receipt["authorities"]["public_registry_publish"] != false
        || receipt["authorities"]["installation"] != false
        || receipt["candidate"]["publication_state"] != "private"
        || receipt["candidate"]["install_eligible"] != false
    {
        return Err("本地 CodeAgent 返回了越权或不可信状态。".to_string());
    }
    Ok(value)
}

pub fn submit(
    data_dir: &Path,
    task: &Value,
    registry: &Value,
    explicit_user_submit: bool,
    local_processing_consent: bool,
) -> Result<Value, String> {
    if !explicit_user_submit || !local_processing_consent {
        return Err("必须明确提交并单独授权本次 170 局域网 CodeAgent 处理。".to_string());
    }
    let envelope = json!({
        "task": task,
        "registry": registry,
        "explicit_user_submit": true,
        "local_processing_consent": true,
        "allow_deterministic_fallback": false
    });
    let input = serde_json::to_vec(&envelope)
        .map_err(|error| format!("无法序列化本地 CodeAgent 请求：{error}"))?;
    if input.is_empty() || input.len() > MAX_INPUT_BYTES {
        return Err("本地 CodeAgent 请求超过 256 KiB。".to_string());
    }
    let root = data_dir.join("local-codeagent");
    let candidates = data_dir.join("private-candidates");
    fs::create_dir_all(&root).map_err(|error| format!("无法创建本地 CodeAgent 队列：{error}"))?;
    fs::create_dir_all(&candidates).map_err(|error| format!("无法创建私有候选目录：{error}"))?;
    let mut child = Command::new(native_platform::python_executable(false)?)
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(script_path()?)
        .arg("run")
        .arg("--root")
        .arg(root)
        .arg("--candidate-root")
        .arg(candidates)
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
        .map_err(|error| format!("无法启动本地 CodeAgent：{error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "无法打开本地 CodeAgent 输入。".to_string())?
        .write_all(&input)
        .map_err(|error| format!("无法发送本地 CodeAgent 请求：{error}"))?;
    parse_reply(wait(child, RUN_TIMEOUT, "本地 CodeAgent")?)
}

pub fn state(data_dir: &Path) -> Vec<Value> {
    let output = Command::new(match native_platform::python_executable(false) {
        Ok(path) => path,
        Err(_) => return Vec::new(),
    })
    .args(["-X", "utf8"]).arg("-I")
    .arg("-B")
    .arg(match script_path() {
        Ok(path) => path,
        Err(_) => return Vec::new(),
    })
    .arg("state")
    .arg("--candidate-root")
    .arg(data_dir.join("private-candidates"))
    .env_clear()
    .env(
        "PATH",
        match native_platform::safe_path() {
            Ok(path) => path,
            Err(_) => return Vec::new(),
        },
    )
    .env("PYTHONDONTWRITEBYTECODE", "1")
    .stdin(Stdio::null())
    .stdout(Stdio::piped())
    .stderr(Stdio::piped())
    .output();
    let Ok(output) = output else {
        return Vec::new();
    };
    if !output.status.success() || output.stdout.len() > MAX_REPLY_BYTES {
        return Vec::new();
    }
    serde_json::from_slice::<Value>(&output.stdout)
        .ok()
        .and_then(|value| value["apps"].as_array().cloned())
        .unwrap_or_default()
}
