use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    Arc, Mutex, OnceLock,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::codeagent_settings::RuntimeCodeAgentSettings;
use crate::native_platform;

const MAX_INPUT_BYTES: usize = 512 * 1024;
const MAX_REPLY_BYTES: usize = 512 * 1024;
const MAX_WORKER_STATUS_BYTES: u64 = 64 * 1024;
const MAX_TASKS: usize = 64;
const MAX_ARCHIVED_TASKS: usize = 512;
const ARCHIVE_LIST_TIMEOUT: Duration = Duration::from_secs(2);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(45);
// Product integration budget (outside the accepted Stage 0 tranche):
// CodeAgent 1x1800s + Builder 2x300s + Verifier 3x60s + 120s orchestration.
// Preflight and the controller share this one monotonic deadline; it is not a
// fresh timeout for every nested stage.
const CODEAGENT_STAGE_BUDGET_SECONDS: u64 = 1800;
const BUILDER_STAGE_BUDGET_SECONDS: u64 = 2 * 300;
const VERIFIER_STAGE_BUDGET_SECONDS: u64 = 3 * 60;
const ORCHESTRATION_BUDGET_SECONDS: u64 = 120;
pub const PIPELINE_TIMEOUT: Duration = Duration::from_secs(
    CODEAGENT_STAGE_BUDGET_SECONDS
        + BUILDER_STAGE_BUDGET_SECONDS
        + VERIFIER_STAGE_BUDGET_SECONDS
        + ORCHESTRATION_BUDGET_SECONDS,
);
pub const PIPELINE_CLEANUP_TIMEOUT: Duration = Duration::from_secs(20);
pub const WORKER_JOINED_CLEANUP_UNCONFIRMED: &str = "worker-joined-cleanup-unconfirmed";
const CONTROLLER_CLEANUP_UNCONFIRMED: &str = "delivery-controller-cleanup-unconfirmed";
const PROCESS_POLL_INTERVAL: Duration = Duration::from_millis(25);
const ADMISSION_WAIT_TIMEOUT: Duration = Duration::from_secs(15 * 60);
const ADMISSION_RETRY_INTERVAL: Duration = Duration::from_secs(2);
const DESCENDANT_CENSUS_INTERVAL: Duration = Duration::from_secs(1);
const PROCESS_TERM_GRACE: Duration = Duration::from_secs(1);
const PROCESS_KILL_GRACE: Duration = Duration::from_secs(3);
const OUTPUT_DRAIN_GRACE: Duration = Duration::from_secs(2);
const EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED: &str =
    "必须单独确认外部 CodeAgent 可能产生费用或消耗配额；本次没有启动。";
const WORKER_STATUS_SCHEMA: &str = "vibapp.desktop-delivery-worker.experimental-v2";

const BUILDER_INPUT_ROOT_ENV: &str = "VIBAPP_BUILDER_INPUT_ROOT";
const TOOL_LAYER_ENV: &str = "VIBAPP_BUILDER_TOOL_LAYER";
const CARGO_HOME_ENV: &str = "VIBAPP_BUILDER_CARGO_HOME";
const CACHE_ACCEPTANCE_ENV: &str = "VIBAPP_BUILDER_CACHE_ACCEPTANCE";
const WASM_TOOLS_ENV: &str = "VIBAPP_VERIFIER_WASM_TOOLS";
const DEFAULT_TOOL_LAYER: &str =
    "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980";
const DEFAULT_CACHE_ROOT: &str = "generated/builder-cargo-cache-sha256-b916551cae66c03f84a512e97fe57c9567376662cca05d36443531a32fb8522d";

static ACTIVE_WORKERS: OnceLock<Mutex<BTreeSet<String>>> = OnceLock::new();
static WORKER_THREADS: OnceLock<Mutex<BTreeMap<String, WorkerThread>>> = OnceLock::new();
static WORKER_SESSION_ID: OnceLock<String> = OnceLock::new();
static PROCESS_CANCELLATION_REQUESTED: AtomicBool = AtomicBool::new(false);

struct WorkerThread {
    cancel: Arc<AtomicBool>,
    join: JoinHandle<()>,
    status_path: PathBuf,
}

/// Async-signal-safe bridge hook: callers may set this from a signal handler.
/// Every controller wait observes it in addition to its per-worker token.
pub fn request_process_cancellation() {
    PROCESS_CANCELLATION_REQUESTED.store(true, Ordering::Release);
}

struct ActiveWorkerGuard {
    key: String,
}

impl ActiveWorkerGuard {
    fn new(key: String) -> Self {
        Self { key }
    }
}

impl Drop for ActiveWorkerGuard {
    fn drop(&mut self) {
        if let Some(active) = ACTIVE_WORKERS.get()
            && let Ok(mut guard) = active.lock()
        {
            guard.remove(&self.key);
        }
    }
}

fn worker_session_id() -> &'static str {
    WORKER_SESSION_ID.get_or_init(|| {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        format!("desktop-{}-{nanos:x}", std::process::id())
    })
}

#[derive(Clone, Debug)]
struct DeliveryPaths {
    python: PathBuf,
    controller: PathBuf,
    codeagent_adapter: PathBuf,
    cloud_agent: PathBuf,
    root: PathBuf,
    appstore_root: PathBuf,
}

#[derive(Clone, Debug, Default)]
pub struct BuilderConfiguration {
    builder_input_root: Option<PathBuf>,
    tool_layer: Option<PathBuf>,
    cargo_home: Option<PathBuf>,
    cache_acceptance: Option<PathBuf>,
    wasm_tools: Option<PathBuf>,
    errors: Vec<String>,
}

impl BuilderConfiguration {
    fn from_environment() -> Self {
        let mut configuration = Self::default();
        configuration.builder_input_root = configured_or_default(
            BUILDER_INPUT_ROOT_ENV,
            true,
            "generated",
            &mut configuration.errors,
        );
        configuration.tool_layer = configured_or_default(
            TOOL_LAYER_ENV,
            true,
            DEFAULT_TOOL_LAYER,
            &mut configuration.errors,
        );
        configuration.cargo_home = configured_or_default(
            CARGO_HOME_ENV,
            true,
            &format!("{DEFAULT_CACHE_ROOT}/cargo-home"),
            &mut configuration.errors,
        );
        configuration.cache_acceptance = configured_or_default(
            CACHE_ACCEPTANCE_ENV,
            false,
            &format!("{DEFAULT_CACHE_ROOT}/acceptance.json"),
            &mut configuration.errors,
        );
        configuration.wasm_tools = configured_or_default(
            WASM_TOOLS_ENV,
            false,
            &format!("{DEFAULT_TOOL_LAYER}/bin/wasm-tools"),
            &mut configuration.errors,
        );
        validate_builder_input_containment(&mut configuration);
        configuration
    }

    fn ready(&self) -> bool {
        self.errors.is_empty()
            && self.builder_input_root.is_some()
            && self.tool_layer.is_some()
            && self.cargo_home.is_some()
            && self.cache_acceptance.is_some()
            && self.wasm_tools.is_some()
    }

    pub fn public_view(&self) -> Value {
        let missing = [
            (BUILDER_INPUT_ROOT_ENV, self.builder_input_root.is_none()),
            (TOOL_LAYER_ENV, self.tool_layer.is_none()),
            (CARGO_HOME_ENV, self.cargo_home.is_none()),
            (CACHE_ACCEPTANCE_ENV, self.cache_acceptance.is_none()),
            (WASM_TOOLS_ENV, self.wasm_tools.is_none()),
        ]
        .into_iter()
        .filter_map(|(name, missing)| missing.then_some(name))
        .collect::<Vec<_>>();
        json!({
            "ready": self.ready(),
            "status": if self.ready() { "ready" } else { "builder-configuration-required" },
            "missing": missing,
            "errors": self.errors,
            "authority": {
                "compile": "isolated-builder",
                "verify": "independent-verifier",
                "install": "runtime-daemon",
                "publish": "none"
            }
        })
    }
}

fn validate_builder_input_containment(configuration: &mut BuilderConfiguration) {
    let Some(root) = configuration.builder_input_root.as_deref() else {
        return;
    };
    for (name, path) in [
        (TOOL_LAYER_ENV, configuration.tool_layer.as_deref()),
        (CARGO_HOME_ENV, configuration.cargo_home.as_deref()),
        (
            CACHE_ACCEPTANCE_ENV,
            configuration.cache_acceptance.as_deref(),
        ),
        (WASM_TOOLS_ENV, configuration.wasm_tools.as_deref()),
    ] {
        let Some(path) = path else {
            continue;
        };
        if path != root && !path.starts_with(root) {
            configuration
                .errors
                .push(format!("{name} 必须位于 {BUILDER_INPUT_ROOT_ENV} 目录内。"));
        }
    }
}

fn configured_path(name: &str, directory: bool, errors: &mut Vec<String>) -> Option<PathBuf> {
    let Some(raw) = std::env::var_os(name) else {
        return None;
    };
    validate_path(name, PathBuf::from(raw), directory, errors)
}

fn configured_or_default(
    name: &str,
    directory: bool,
    default_relative: &str,
    errors: &mut Vec<String>,
) -> Option<PathBuf> {
    if std::env::var_os(name).is_some() {
        return configured_path(name, directory, errors);
    }
    let repository_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    validate_path(
        &format!("内置 {name}"),
        repository_root.join(default_relative),
        directory,
        errors,
    )
}

fn validate_path(
    name: &str,
    path: PathBuf,
    directory: bool,
    errors: &mut Vec<String>,
) -> Option<PathBuf> {
    if !path.is_absolute() {
        errors.push(format!("{name} 必须是绝对路径。"));
        return None;
    }
    let Ok(metadata) = path.symlink_metadata() else {
        errors.push(format!("{name} 指向的路径不存在。"));
        return None;
    };
    let valid_kind = if directory {
        metadata.file_type().is_dir()
    } else {
        metadata.file_type().is_file()
    };
    if metadata.file_type().is_symlink() || !valid_kind {
        errors.push(format!(
            "{name} 必须是非符号链接的{}。",
            if directory { "目录" } else { "普通文件" }
        ));
        return None;
    }
    match path.canonicalize() {
        Ok(resolved) => Some(resolved),
        Err(error) => {
            errors.push(format!("{name} 无法解析：{error}"));
            None
        }
    }
}

fn development_path(packaged: &str, development: &str) -> Result<PathBuf, String> {
    native_platform::resource_file(
        packaged,
        &Path::new(env!("CARGO_MANIFEST_DIR")).join(development),
    )
}

fn paths(data_dir: &Path) -> Result<DeliveryPaths, String> {
    Ok(DeliveryPaths {
        python: native_platform::python_executable(true)?,
        controller: development_path(
            "orchestrator/delivery_controller.py",
            "../../orchestrator/delivery_controller.py",
        )?,
        codeagent_adapter: development_path(
            "codeagent-adapter/codeagent_adapter.py",
            "../../codeagent-adapter/codeagent_adapter.py",
        )?,
        cloud_agent: development_path(
            "cloud-agent/cloud_agent.py",
            "../../cloud-agent/cloud_agent.py",
        )?,
        root: data_dir.join("delivery-controller"),
        appstore_root: data_dir.join("local-appstore"),
    })
}

fn base_command(
    paths: &DeliveryPaths,
    codeagent: &RuntimeCodeAgentSettings,
    builder: Option<&BuilderConfiguration>,
) -> Result<Command, String> {
    let mut command = Command::new(&paths.python);
    command
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(&paths.controller)
        .arg("--root")
        .arg(&paths.root)
        .arg("--codeagent-adapter")
        .arg(&paths.codeagent_adapter)
        .arg("--cloud-agent")
        .arg(&paths.cloud_agent)
        .arg("--provider")
        .arg(&codeagent.provider_id)
        .arg("--appstore-root")
        .arg(&paths.appstore_root)
        .env_clear()
        .envs(native_platform::trusted_system_environment()?)
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command.arg("--model").arg(&codeagent.model);
    if let Some(builder) = builder.filter(|configuration| configuration.ready()) {
        command
            .arg("--builder-input-root")
            .arg(
                builder
                    .builder_input_root
                    .as_ref()
                    .expect("ready Builder input root"),
            )
            .arg("--tool-layer")
            .arg(builder.tool_layer.as_ref().expect("ready tool layer"))
            .arg("--cargo-home")
            .arg(builder.cargo_home.as_ref().expect("ready Cargo home"))
            .arg("--cache-acceptance")
            .arg(
                builder
                    .cache_acceptance
                    .as_ref()
                    .expect("ready cache acceptance"),
            )
            .arg("--wasm-tools")
            .arg(builder.wasm_tools.as_ref().expect("ready wasm-tools"));
    }
    inherit_provider_environment(&mut command, &codeagent.provider_id);
    Ok(command)
}

fn inherit_provider_environment(command: &mut Command, provider_id: &str) {
    let names: &[&str] = match provider_id {
        "codex" => &["HOME", "CODEX_HOME"],
        "claude-code" => &["ANTHROPIC_API_KEY"],
        "opencode" => &["HOME"],
        "gemini-cli" => &["HOME", "GEMINI_API_KEY"],
        _ => &[],
    };
    for name in names {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
}

struct DrainHandle {
    join: JoinHandle<Vec<u8>>,
    bytes_seen: Arc<AtomicUsize>,
    exceeded: Arc<AtomicBool>,
}

fn drain_bounded<R: Read + Send + 'static>(mut reader: R) -> DrainHandle {
    let bytes_seen = Arc::new(AtomicUsize::new(0));
    let exceeded = Arc::new(AtomicBool::new(false));
    let reader_bytes = Arc::clone(&bytes_seen);
    let reader_exceeded = Arc::clone(&exceeded);
    let join = thread::spawn(move || {
        let mut kept = Vec::new();
        let mut buffer = [0_u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => {
                    reader_bytes.fetch_add(read, Ordering::Relaxed);
                    let available = MAX_REPLY_BYTES.saturating_sub(kept.len());
                    let copy = available.min(read);
                    kept.extend_from_slice(&buffer[..copy]);
                    if copy < read {
                        reader_exceeded.store(true, Ordering::Release);
                    }
                }
                Err(_) => {
                    reader_exceeded.store(true, Ordering::Release);
                    break;
                }
            }
        }
        kept
    });
    DrainHandle {
        join,
        bytes_seen,
        exceeded,
    }
}

fn finish_drain(handle: DrainHandle, label: &str) -> Result<Vec<u8>, String> {
    let deadline = Instant::now() + OUTPUT_DRAIN_GRACE;
    while !handle.join.is_finished() && Instant::now() < deadline {
        thread::sleep(PROCESS_POLL_INTERVAL);
    }
    if !handle.join.is_finished() {
        return Err(cleanup_unconfirmed(format!(
            "自动交付{label}在进程树清理后仍未关闭；拒绝把清理视为完成。"
        )));
    }
    handle
        .join
        .join()
        .map_err(|_| format!("读取自动交付{label}的线程异常退出。"))
}

fn cleanup_unconfirmed(message: impl AsRef<str>) -> String {
    let message = message.as_ref();
    if message.contains(CONTROLLER_CLEANUP_UNCONFIRMED) {
        message.to_string()
    } else {
        format!("{CONTROLLER_CLEANUP_UNCONFIRMED}: {message}")
    }
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
struct ProcessRecord {
    pid: i32,
    ppid: i32,
    pgid: i32,
}

#[cfg(unix)]
fn process_table() -> Result<Vec<ProcessRecord>, String> {
    let output = Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,pgid="])
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|error| {
            format!("descendant-census-best-effort unavailable: /bin/ps failed: {error}")
        })?;
    if !output.status.success() || output.stdout.len() > 8 * 1024 * 1024 {
        return Err(
            "descendant-census-best-effort unavailable: /bin/ps returned an invalid result"
                .to_string(),
        );
    }
    let text = std::str::from_utf8(&output.stdout).map_err(|_| {
        "descendant-census-best-effort unavailable: /bin/ps output is not UTF-8".to_string()
    })?;
    let mut records = Vec::new();
    for line in text.lines() {
        let fields = line.split_ascii_whitespace().collect::<Vec<_>>();
        if fields.len() != 3 {
            return Err(
                "descendant-census-best-effort unavailable: /bin/ps row is malformed".to_string(),
            );
        }
        let parse = |value: &str| {
            value.parse::<i32>().map_err(|_| {
                "descendant-census-best-effort unavailable: /bin/ps PID is malformed".to_string()
            })
        };
        records.push(ProcessRecord {
            pid: parse(fields[0])?,
            ppid: parse(fields[1])?,
            pgid: parse(fields[2])?,
        });
    }
    Ok(records)
}

#[cfg(unix)]
struct ProcessTracker {
    root_pid: i32,
    known_pids: BTreeSet<i32>,
    known_pgids: BTreeSet<i32>,
}

#[cfg(unix)]
impl ProcessTracker {
    fn new(root_pid: u32) -> Result<Self, String> {
        let root_pid =
            i32::try_from(root_pid).map_err(|_| "自动交付控制器 PID 超出平台范围。".to_string())?;
        let mut known_pids = BTreeSet::new();
        known_pids.insert(root_pid);
        let mut known_pgids = BTreeSet::new();
        // execute_until starts the controller in a fresh process group whose
        // PGID is its PID. Descendant census is defense in depth for observed
        // setsid/setpgid children; it cannot prove absence of an instantaneous
        // double-fork on macOS.
        known_pgids.insert(root_pid);
        Ok(Self {
            root_pid,
            known_pids,
            known_pgids,
        })
    }

    fn observe(&mut self) -> Result<Vec<ProcessRecord>, String> {
        let records = process_table()?;
        loop {
            let mut changed = false;
            for record in &records {
                if record.pid == self.root_pid
                    || self.known_pids.contains(&record.ppid)
                    || self.known_pgids.contains(&record.pgid)
                {
                    changed |= self.known_pids.insert(record.pid);
                    if record.pgid > 1 {
                        changed |= self.known_pgids.insert(record.pgid);
                    }
                }
            }
            if !changed {
                break;
            }
        }
        Ok(records
            .into_iter()
            .filter(|record| {
                self.known_pids.contains(&record.pid) || self.known_pgids.contains(&record.pgid)
            })
            .collect())
    }

    fn signal_known(&mut self, signal: i32) -> Result<(), String> {
        let own_pid = unsafe { libc::getpid() };
        let own_pgid = unsafe { libc::getpgrp() };
        for pgid in self.known_pgids.iter().copied().rev() {
            if pgid <= 1 || pgid == own_pgid {
                continue;
            }
            let result = unsafe { libc::kill(-pgid, signal) };
            if result != 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(format!("无法向自动交付进程组 {pgid} 发送信号：{error}"));
                }
            }
        }
        for pid in self.known_pids.iter().copied().rev() {
            if pid <= 1 || pid == own_pid {
                continue;
            }
            let result = unsafe { libc::kill(pid, signal) };
            if result != 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(format!("无法向自动交付进程 {pid} 发送信号：{error}"));
                }
            }
        }
        Ok(())
    }

    fn wait_quiescent(&mut self, child: &mut Child, timeout: Duration) -> Result<bool, String> {
        let deadline = Instant::now() + timeout;
        loop {
            let _ = child.try_wait();
            if self.observe()?.is_empty() {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            thread::sleep(PROCESS_POLL_INTERVAL);
        }
    }

    fn terminate_and_confirm(&mut self, child: &mut Child) -> Result<(), String> {
        let census_error = self.observe().err();
        self.signal_known(libc::SIGTERM)?;
        if let Some(error) = census_error {
            thread::sleep(PROCESS_TERM_GRACE);
            self.signal_known(libc::SIGKILL)?;
            let _ = child.wait();
            return Err(format!(
                "{error}; root process group was killed, but descendant-census-best-effort could not confirm the full tree"
            ));
        }
        if !self.wait_quiescent(child, PROCESS_TERM_GRACE)? {
            self.signal_known(libc::SIGKILL)?;
            if !self.wait_quiescent(child, PROCESS_KILL_GRACE)? {
                return Err(
                    "自动交付进程树在 SIGKILL 后仍未清空；descendant-census-best-effort 未能确认收尾。"
                        .to_string(),
                );
            }
        }
        let _ = child.wait();
        Ok(())
    }
}

#[cfg(not(unix))]
struct ProcessTracker;

#[cfg(not(unix))]
impl ProcessTracker {
    fn new(_root_pid: u32) -> Result<Self, String> {
        Err(
            "process-tree-containment-unavailable: 当前平台不能确认自动交付进程树收尾。"
                .to_string(),
        )
    }

    fn terminate_and_confirm(&mut self, child: &mut Child) -> Result<(), String> {
        let _ = child.kill();
        let _ = child.wait();
        Err(
            "process-tree-containment-unavailable: 当前平台只能终止 leader，不能确认整个进程树。"
                .to_string(),
        )
    }
}

fn wait_bounded(
    child: Child,
    deadline: Instant,
    cancel: Option<&AtomicBool>,
) -> Result<(bool, Vec<u8>, Vec<u8>), String> {
    wait_bounded_with_census_interval(child, deadline, cancel, DESCENDANT_CENSUS_INTERVAL)
}

fn wait_bounded_with_census_interval(
    mut child: Child,
    deadline: Instant,
    cancel: Option<&AtomicBool>,
    descendant_census_interval: Duration,
) -> Result<(bool, Vec<u8>, Vec<u8>), String> {
    let mut tracker = match ProcessTracker::new(child.id()) {
        Ok(tracker) => tracker,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(cleanup_unconfirmed(error));
        }
    };
    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            return Err(match tracker.terminate_and_confirm(&mut child) {
                Ok(()) => "无法读取交付控制器输出；受控进程树已清空。".to_string(),
                Err(cleanup_error) => cleanup_unconfirmed(format!(
                    "无法读取交付控制器输出；cleanup failed: {cleanup_error}"
                )),
            });
        }
    };
    let stderr = match child.stderr.take() {
        Some(stderr) => stderr,
        None => {
            return Err(match tracker.terminate_and_confirm(&mut child) {
                Ok(()) => "无法读取交付控制器错误输出；受控进程树已清空。".to_string(),
                Err(cleanup_error) => cleanup_unconfirmed(format!(
                    "无法读取交付控制器错误输出；cleanup failed: {cleanup_error}"
                )),
            });
        }
    };
    let stdout_reader = drain_bounded(stdout);
    let stderr_reader = drain_bounded(stderr);
    let mut next_descendant_census = Instant::now();
    let status = loop {
        #[cfg(unix)]
        if Instant::now() >= next_descendant_census {
            if let Err(error) = tracker.observe() {
                let cleanup = tracker.terminate_and_confirm(&mut child);
                return Err(cleanup_unconfirmed(match cleanup {
                    Ok(()) => error,
                    Err(cleanup_error) => format!("{error}; cleanup failed: {cleanup_error}"),
                }));
            }
            next_descendant_census = Instant::now() + descendant_census_interval;
        }
        if PROCESS_CANCELLATION_REQUESTED.load(Ordering::Acquire)
            || cancel.is_some_and(|token| token.load(Ordering::Acquire))
        {
            #[cfg(unix)]
            tracker
                .terminate_and_confirm(&mut child)
                .map_err(cleanup_unconfirmed)?;
            return Err(
                "delivery-controller-cancelled: 自动交付已取消，受控进程树已清空。".to_string(),
            );
        }
        if stdout_reader.exceeded.load(Ordering::Acquire)
            || stderr_reader.exceeded.load(Ordering::Acquire)
            || stdout_reader.bytes_seen.load(Ordering::Relaxed) > MAX_REPLY_BYTES
            || stderr_reader.bytes_seen.load(Ordering::Relaxed) > MAX_REPLY_BYTES
        {
            #[cfg(unix)]
            tracker
                .terminate_and_confirm(&mut child)
                .map_err(cleanup_unconfirmed)?;
            return Err("delivery-controller-output-limit: 自动交付控制器输出超过 512 KiB 上限，受控进程树已清空。".to_string());
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(PROCESS_POLL_INTERVAL),
            Ok(None) => {
                #[cfg(unix)]
                tracker
                    .terminate_and_confirm(&mut child)
                    .map_err(cleanup_unconfirmed)?;
                return Err("delivery-controller-timeout: 自动交付超过统一 pipeline deadline，受控进程树已清空。".to_string());
            }
            Err(error) => {
                #[cfg(unix)]
                tracker
                    .terminate_and_confirm(&mut child)
                    .map_err(cleanup_unconfirmed)?;
                return Err(format!("无法读取自动交付进程状态：{error}"));
            }
        }
    };
    #[cfg(unix)]
    {
        let survivors = match tracker.observe() {
            Ok(survivors) => survivors,
            Err(error) => {
                let cleanup = tracker.terminate_and_confirm(&mut child);
                return Err(cleanup_unconfirmed(match cleanup {
                    Ok(()) => error,
                    Err(cleanup_error) => format!("{error}; cleanup failed: {cleanup_error}"),
                }));
            }
        };
        if !survivors.is_empty() {
            tracker
                .terminate_and_confirm(&mut child)
                .map_err(cleanup_unconfirmed)?;
            return Err(format!(
                "delivery-controller-descendants-survived: controller leader 已退出，但 {} 个已观察后代仍存活；已清空并拒绝本次结果（descendant-census-best-effort）。",
                survivors.len()
            ));
        }
    }
    let stdout = finish_drain(stdout_reader, "输出")?;
    let stderr = finish_drain(stderr_reader, "错误输出")?;
    Ok((status.success(), stdout, stderr))
}

fn parse_reply(success: bool, stdout: &[u8], stderr: &[u8]) -> Result<Value, String> {
    let bytes = if success { stdout } else { stderr };
    if bytes.is_empty() || bytes.len() > MAX_REPLY_BYTES {
        return Err("自动交付控制器返回大小无效。".to_string());
    }
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|error| format!("自动交付控制器返回了无效 JSON：{error}"))?;
    if !success || value["ok"] != true {
        let code = value["code"]
            .as_str()
            .unwrap_or("delivery-controller-failed");
        let message = value["message"].as_str().unwrap_or("自动交付失败");
        return Err(format!("{code}: {message}"));
    }
    value
        .get("result")
        .cloned()
        .ok_or_else(|| "自动交付控制器缺少 result。".to_string())
}

fn execute_until(
    mut command: Command,
    deadline: Instant,
    cancel: Option<&AtomicBool>,
) -> Result<Value, String> {
    if Instant::now() >= deadline {
        return Err("delivery-controller-timeout: pipeline deadline 在启动前已耗尽。".to_string());
    }
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(not(unix))]
    return Err(
        "process-tree-containment-unavailable: 当前平台不能安全启动自动交付控制器。".to_string(),
    );
    let child = command
        .spawn()
        .map_err(|error| format!("无法启动自动交付控制器：{error}"))?;
    let (success, stdout, stderr) = wait_bounded(child, deadline, cancel)?;
    parse_reply(success, &stdout, &stderr)
}

fn execute(command: Command, timeout: Duration) -> Result<Value, String> {
    execute_until(command, Instant::now() + timeout, None)
}

fn preflight_command(
    paths: &DeliveryPaths,
    codeagent: &RuntimeCodeAgentSettings,
    builder: &BuilderConfiguration,
) -> Result<Command, String> {
    let mut command = base_command(paths, codeagent, Some(builder))?;
    for name in ["HOME", "CODEX_HOME", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"] {
        command.env_remove(name);
    }
    command.arg("preflight");
    Ok(command)
}

fn builder_verifier_preflight_until(
    paths: &DeliveryPaths,
    codeagent: &RuntimeCodeAgentSettings,
    builder: &BuilderConfiguration,
    deadline: Instant,
) -> Result<Value, String> {
    let readiness = execute_until(
        preflight_command(paths, codeagent, builder)?,
        deadline,
        None,
    )
    .map_err(|error| format!("Builder/Verifier 配置 preflight 失败：{error}"))?;
    if readiness["status"] != "ready"
        || readiness["provider_process_started"] != false
        || readiness["external_request_attempted"] != false
        || readiness["external_request_observed"] != false
        || readiness["source_executed"] != false
        || readiness["builder"]["status"] != "ready"
        || readiness["verifier"]["status"] != "ready"
    {
        return Err(
            "Builder/Verifier 配置 preflight 返回了不可信的就绪状态；CodeAgent 未启动。"
                .to_string(),
        );
    }
    Ok(readiness)
}

fn canonical_bytes(value: &Value) -> Result<Vec<u8>, String> {
    let bytes =
        serde_json::to_vec(value).map_err(|error| format!("无法序列化交付输入：{error}"))?;
    if bytes.is_empty() || bytes.len() > MAX_INPUT_BYTES {
        return Err("交付输入超过 512 KiB 上限。".to_string());
    }
    Ok(bytes)
}

fn ensure_real_directory(path: &Path, label: &str) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|error| format!("无法创建{label}：{error}"))?;
    let metadata = path
        .symlink_metadata()
        .map_err(|error| format!("无法检查{label}：{error}"))?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Err(format!("{label}必须是非符号链接目录。"));
    }
    Ok(())
}

fn write_private_input(root: &Path, label: &str, value: &Value) -> Result<PathBuf, String> {
    let bytes = canonical_bytes(value)?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    let directory = root.join("desktop-inputs");
    ensure_real_directory(root, "交付控制器目录")?;
    ensure_real_directory(&directory, "交付输入目录")?;
    let path = directory.join(format!("{label}-{digest}.json"));
    if path.exists() {
        let metadata = path
            .symlink_metadata()
            .map_err(|error| format!("无法检查交付输入：{error}"))?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() as usize != bytes.len()
            || fs::read(&path).map_err(|error| format!("无法读取交付输入：{error}"))? != bytes
        {
            return Err("已存在的摘要绑定交付输入不一致。".to_string());
        }
        return Ok(path);
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&path)
        .map_err(|error| format!("无法创建交付输入：{error}"))?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法持久化交付输入：{error}"))?;
    Ok(path)
}

fn valid_task_id(value: &str) -> bool {
    value.strip_prefix("development-").is_some_and(|suffix| {
        suffix.len() == 32
            && suffix
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn valid_attempt_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 29
        && bytes.starts_with(b"attempt-")
        && bytes[8..12].iter().all(u8::is_ascii_digit)
        && bytes[12] == b'-'
        && bytes[13..]
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
}

fn computed_delivery_task_id(task: &Value) -> Result<String, String> {
    let need_id = task["need_spec"]["need_id"]
        .as_str()
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 128
                && value.trim() == *value
                && !value.chars().any(char::is_control)
        })
        .ok_or_else(|| "任务缺少有效 NeedSpec need_id。".to_string())?;
    let immutable_digest = task["immutable_task_digest_sha256"]
        .as_str()
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or_else(|| "任务缺少有效 immutable task digest。".to_string())?;
    let digest = format!(
        "{:x}",
        Sha256::digest(format!("{need_id}:{immutable_digest}").as_bytes())
    );
    Ok(format!("development-{}", &digest[..32]))
}

fn classify_previous_task<'a>(
    task: &Value,
    previous_task_id: Option<&'a str>,
) -> Result<(Option<&'a str>, Option<&'a str>), String> {
    let Some(previous_task_id) = previous_task_id else {
        return Ok((None, None));
    };
    if !valid_task_id(previous_task_id) {
        return Err("previousTaskId 格式无效。".to_string());
    }
    if previous_task_id == computed_delivery_task_id(task)? {
        Ok((Some(previous_task_id), None))
    } else {
        // A changed immutable task is a replacement, never another attempt on
        // the prior task. The controller will independently verify newer
        // NeedSpec/job/digest/consent and failed-task lineage.
        Ok((None, Some(previous_task_id)))
    }
}

fn worker_status_path(root: &Path, task_id: &str, attempt_id: &str) -> PathBuf {
    debug_assert!(valid_task_id(task_id));
    debug_assert!(valid_attempt_id(attempt_id));
    root.join("desktop-workers")
        .join(format!("{task_id}-{attempt_id}.json"))
}

fn provider_task_binding(provider_id: &str) -> Option<&'static str> {
    match provider_id {
        "codex" => Some("openai-codex"),
        "claude-code" => Some("anthropic-claude-code"),
        "opencode" => Some("opencode"),
        "gemini-cli" => Some("google-gemini-cli"),
        _ => None,
    }
}

fn persisted_codeagent(
    value: &Value,
    expected_immutable_task_digest: &str,
) -> Option<RuntimeCodeAgentSettings> {
    if value["schema_version"] != WORKER_STATUS_SCHEMA
        || value["external_cost_acknowledged"] != true
        || value["immutable_task_digest_sha256"] != expected_immutable_task_digest
    {
        return None;
    }
    let provider_id = value["provider_id"].as_str()?;
    let task_provider = value["task_provider"].as_str()?;
    if provider_task_binding(provider_id) != Some(task_provider) {
        return None;
    }
    let model = match &value["model"] {
        Value::String(model)
            if !model.is_empty()
                && model.trim() == model
                && model.chars().count() <= 256
                && !model.chars().any(char::is_control)
                && !model.starts_with('-') =>
        {
            model.clone()
        }
        _ => return None,
    };
    Some(RuntimeCodeAgentSettings {
        provider_id: provider_id.to_string(),
        task_provider: task_provider.to_string(),
        model,
    })
}

fn worker_status(
    status: &str,
    task_id: &str,
    attempt_id: &str,
    immutable_task_digest_sha256: &str,
    codeagent: &RuntimeCodeAgentSettings,
) -> Value {
    json!({
        "schema_version": WORKER_STATUS_SCHEMA,
        "status": status,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "immutable_task_digest_sha256": immutable_task_digest_sha256,
        "worker_session_id": worker_session_id(),
        "external_cost_acknowledged": true,
        "provider_id": codeagent.provider_id,
        "task_provider": codeagent.task_provider,
        "model": codeagent.model,
        "external_request_attempted": false,
        "external_request_observed": false,
        "error": null
    })
}

fn write_worker_status(path: &Path, value: &Value) -> Result<(), String> {
    let bytes = canonical_bytes(value)?;
    if bytes.len() as u64 > MAX_WORKER_STATUS_BYTES {
        return Err("交付 worker 状态超过 64 KiB。".to_string());
    }
    let directory = path
        .parent()
        .ok_or_else(|| "无法确定 worker 状态目录。".to_string())?;
    fs::create_dir_all(directory).map_err(|error| format!("无法创建 worker 状态目录：{error}"))?;
    let metadata = directory
        .symlink_metadata()
        .map_err(|error| format!("无法检查 worker 状态目录：{error}"))?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Err("worker 状态目录必须是非符号链接目录。".to_string());
    }
    let temporary = directory.join(format!(
        ".worker-{}-{:x}.tmp",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|error| format!("无法创建 worker 临时状态：{error}"))?;
    let result = file
        .write_all(&bytes)
        .and_then(|_| file.sync_all())
        .and_then(|_| fs::rename(&temporary, path));
    if let Err(error) = result {
        let _ = fs::remove_file(&temporary);
        return Err(format!("无法写入 worker 状态：{error}"));
    }
    Ok(())
}

fn worker_key(task_id: &str, attempt_id: &str) -> Result<String, String> {
    if !valid_task_id(task_id) || !valid_attempt_id(attempt_id) {
        return Err("task_id 或 attempt_id 格式无效。".to_string());
    }
    Ok(format!("{task_id}:{attempt_id}"))
}

fn remove_finished_worker(key: &str) -> Result<Option<WorkerThread>, String> {
    let workers = WORKER_THREADS.get_or_init(|| Mutex::new(BTreeMap::new()));
    let mut guard = workers
        .lock()
        .map_err(|_| "交付 worker 线程锁已损坏。".to_string())?;
    if guard
        .get(key)
        .is_some_and(|worker| worker.join.is_finished())
    {
        Ok(guard.remove(key))
    } else {
        Ok(None)
    }
}

fn join_finished_worker(key: &str) -> Result<Option<PathBuf>, String> {
    let Some(worker) = remove_finished_worker(key)? else {
        return Ok(None);
    };
    let status_path = worker.status_path;
    if worker.join.join().is_err() {
        let _ = overwrite_worker_terminal(
            &status_path,
            "worker-cancellation-unconfirmed",
            WORKER_JOINED_CLEANUP_UNCONFIRMED,
            "worker 线程异常退出，无法确认受控进程树清理。",
            false,
        );
        return Err(format!(
            "{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker 线程异常退出。"
        ));
    }
    Ok(Some(status_path))
}

fn overwrite_worker_terminal(
    status_path: &Path,
    status: &str,
    code: &str,
    message: &str,
    cleanup_confirmed: bool,
) -> Result<(), String> {
    let metadata = status_path
        .symlink_metadata()
        .map_err(|error| format!("无法读取 worker 状态以记录取消结果：{error}"))?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_WORKER_STATUS_BYTES
    {
        return Err("worker 状态文件不能安全更新。".to_string());
    }
    let mut value: Value = serde_json::from_slice(
        &fs::read(status_path).map_err(|error| format!("无法读取 worker 状态：{error}"))?,
    )
    .map_err(|error| format!("worker 状态不是有效 JSON：{error}"))?;
    value["status"] = json!(status);
    value["terminal"] = json!(true);
    value["cleanup_confirmed"] = json!(cleanup_confirmed);
    value["error"] = json!(format!("{code}: {message}"));
    write_worker_status(status_path, &value)
}

fn controller_cleanup_confirmed(error: &str) -> bool {
    // Unconfirmed cleanup is tagged at the point where containment evidence is
    // lost. Controller/provider failures after an observed-empty tree remain
    // confirmed without guessing from human-readable error text.
    !error.contains(CONTROLLER_CLEANUP_UNCONFIRMED)
}

fn confirm_joined_worker_status(
    status_path: &Path,
    task_id: &str,
    attempt_id: &str,
    cancellation_reason: Option<&str>,
) -> Result<(), String> {
    let metadata = status_path.symlink_metadata().map_err(|error| {
        format!("{WORKER_JOINED_CLEANUP_UNCONFIRMED}: 无法读取 worker 状态：{error}")
    })?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_WORKER_STATUS_BYTES
    {
        return Err(format!(
            "{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker terminal 状态文件不安全或缺失。"
        ));
    }
    let mut value: Value = serde_json::from_slice(&fs::read(status_path).map_err(|error| {
        format!("{WORKER_JOINED_CLEANUP_UNCONFIRMED}: 无法读取 worker 状态：{error}")
    })?)
    .map_err(|error| {
        format!("{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker 状态不是有效 JSON：{error}")
    })?;
    if value["schema_version"] != WORKER_STATUS_SCHEMA
        || value["task_id"] != task_id
        || value["attempt_id"] != attempt_id
    {
        value["schema_version"] = json!(WORKER_STATUS_SCHEMA);
        value["task_id"] = json!(task_id);
        value["attempt_id"] = json!(attempt_id);
        value["status"] = json!("worker-cancellation-unconfirmed");
        value["terminal"] = json!(true);
        value["cleanup_confirmed"] = json!(false);
        value["error"] = json!(format!(
            "{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker terminal 状态与精确 task/attempt 绑定不一致。"
        ));
        write_worker_status(status_path, &value)?;
        return Err(format!(
            "{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker terminal 状态与精确 task/attempt 绑定不一致。"
        ));
    }
    if value["terminal"] != true || value["cleanup_confirmed"] != true {
        let previous_error = value["error"].clone();
        value["status"] = json!("worker-cancellation-unconfirmed");
        value["terminal"] = json!(true);
        value["cleanup_confirmed"] = json!(false);
        value["controller_cleanup_error"] = previous_error;
        value["error"] = json!(format!(
            "{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker 已 join，但受控进程树清理没有可验证的确认。"
        ));
        write_worker_status(status_path, &value)?;
        return Err(format!(
            "{WORKER_JOINED_CLEANUP_UNCONFIRMED}: worker 已 join，但受控进程树清理没有可验证的确认。"
        ));
    }
    if let Some(reason) = cancellation_reason.filter(|_| value["status"] == "worker-cancelled") {
        value["owner_cancellation_reason"] = json!(reason);
        write_worker_status(status_path, &value)?;
    }
    Ok(())
}

/// Wait for one exact task/attempt worker. The bridge uses this instead of a
/// broad jobs() poll so another task can never extend this worker's lifetime.
pub fn wait_for_worker(task_id: &str, attempt_id: &str, timeout: Duration) -> Result<(), String> {
    let key = worker_key(task_id, attempt_id)?;
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status_path) = join_finished_worker(&key)? {
            return confirm_joined_worker_status(&status_path, task_id, attempt_id, None);
        }
        let exists = WORKER_THREADS
            .get_or_init(|| Mutex::new(BTreeMap::new()))
            .lock()
            .map_err(|_| "交付 worker 线程锁已损坏。".to_string())?
            .contains_key(&key);
        if !exists {
            return Err("精确绑定的交付 worker 不存在。".to_string());
        }
        if Instant::now() >= deadline {
            return Err("bridge-worker-lifetime-exceeded".to_string());
        }
        thread::sleep(PROCESS_POLL_INTERVAL);
    }
}

/// Request cancellation, wait for controller cleanup, and join the exact worker.
/// A timeout is durably recorded and returned as a failure; it is never treated
/// as a successful best-effort cancellation.
pub fn cancel_and_join_worker(
    task_id: &str,
    attempt_id: &str,
    reason: &str,
    timeout: Duration,
) -> Result<(), String> {
    let key = worker_key(task_id, attempt_id)?;
    let (cancel, status_path) = {
        let workers = WORKER_THREADS.get_or_init(|| Mutex::new(BTreeMap::new()));
        let guard = workers
            .lock()
            .map_err(|_| "交付 worker 线程锁已损坏。".to_string())?;
        let worker = guard
            .get(&key)
            .ok_or_else(|| "精确绑定的交付 worker 不存在，无法确认取消。".to_string())?;
        (Arc::clone(&worker.cancel), worker.status_path.clone())
    };
    cancel.store(true, Ordering::Release);
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status_path) = join_finished_worker(&key)? {
            return confirm_joined_worker_status(&status_path, task_id, attempt_id, Some(reason));
        }
        if Instant::now() >= deadline {
            overwrite_worker_terminal(
                &status_path,
                "worker-cancellation-unconfirmed",
                "worker-cancellation-unconfirmed",
                reason,
                false,
            )?;
            return Err(
                "worker-cancellation-unconfirmed: worker 未在取消期限内完成进程树清理和 join。"
                    .to_string(),
            );
        }
        thread::sleep(PROCESS_POLL_INTERVAL);
    }
}

fn wait_for_admission<T>(
    pipeline_deadline: Instant,
    queue_budget: Duration,
    cancel: &AtomicBool,
    mut run: impl FnMut(Instant) -> Result<T, String>,
    mut confirm_unspent: impl FnMut(Instant) -> Result<(), String>,
) -> Result<T, String> {
    let queue_deadline = pipeline_deadline.min(Instant::now() + queue_budget);
    let interrupted = |deadline| {
        if PROCESS_CANCELLATION_REQUESTED.load(Ordering::Acquire)
            || cancel.load(Ordering::Acquire)
        {
            Err("delivery-controller-cancelled: admission wait cancelled; no controller remains active.".to_string())
        } else if Instant::now() >= deadline {
            Err("delivery-controller-timeout: admission wait exhausted its original bounded deadline.".to_string())
        } else {
            Ok(())
        }
    };
    let mut waiting = false;
    loop {
        interrupted(if waiting { queue_deadline } else { pipeline_deadline })?;
        if waiting {
            confirm_unspent(queue_deadline)?;
            interrupted(queue_deadline)?;
        }
        match run(pipeline_deadline) {
            Err(error) if error.starts_with("local-capacity-busy:")
                && controller_cleanup_confirmed(&error) => {
                interrupted(queue_deadline)?;
                waiting = true;
                let next_poll = queue_deadline.min(Instant::now() + ADMISSION_RETRY_INTERVAL);
                while Instant::now() < next_poll {
                    interrupted(queue_deadline)?;
                    thread::sleep(PROCESS_POLL_INTERVAL.min(next_poll.saturating_duration_since(Instant::now())));
                }
            }
            result => return result,
        }
    }
}

fn confirm_queued_unspent(
    root: &Path,
    status: &Value,
    task_id: &str,
    attempt_id: &str,
    immutable_digest: &str,
) -> Result<(), String> {
    let refused = || "admission-retry-refused: exact current queued and unspent attempt is required.".to_string();
    let attempt = &status["attempt"];
    if !valid_task_id(task_id) || !valid_attempt_id(attempt_id)
        || status["task"]["task_id"] != task_id
        || status["task"]["current_attempt_id"] != attempt_id
        || attempt["task_id"] != task_id || attempt["attempt_id"] != attempt_id
        || attempt["immutable_task_digest_sha256"] != immutable_digest
        || attempt["status"] != "queued"
        || !attempt["outputs"].as_object().is_some_and(|outputs| outputs.is_empty())
    {
        return Err(refused());
    }
    // Status is controller-validated. Independently reject any adapter status
    // or consent claim, including dangling links and unsafe parent directories.
    let mut directory = root.to_path_buf();
    for part in ["tasks", task_id, "attempts", attempt_id, "codeagent", "output", "consents"] {
        directory.push(part);
        match fs::symlink_metadata(&directory) {
            Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && matches!(part, "codeagent" | "output" | "consents") => return Ok(()),
            _ => return Err(refused()),
        }
        if part == "codeagent" {
            match fs::symlink_metadata(directory.join("status.json")) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                _ => return Err(refused()),
            }
        }
    }
    if fs::read_dir(directory).map_err(|_| refused())?.next().is_some() {
        return Err(refused());
    }
    Ok(())
}

fn validate_waiting_consent(
    paths: &DeliveryPaths, task_id: &str, attempt_id: &str,
    job_id: &Value, deadline: Instant, cancel: &AtomicBool,
) -> Result<(), String> {
    // Reuse the authority's read-only validation, including expiry, without
    // importing credentials, renewing consent, or adding a second date parser.
    let mut command = Command::new(&paths.python);
    command.args(["-X", "utf8"]).arg("-I").arg("-B").arg(&paths.cloud_agent).arg("validate")
        .arg(paths.root.join("tasks").join(task_id).join("attempts").join(attempt_id).join("input/task.json"))
        .env_clear().envs(native_platform::trusted_system_environment()?).env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8").env("LC_ALL", "C.UTF-8").env("TZ", "UTC")
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    if Instant::now() >= deadline {
        return Err("delivery-controller-timeout: admission validation deadline exhausted.".to_string());
    }
    #[cfg(unix)]
    command.process_group(0);
    let child = command.spawn().map_err(|error| format!("admission-retry-refused: {error}"))?;
    let (success, stdout, _) = wait_bounded(child, deadline, Some(cancel))?;
    let value: Value = serde_json::from_slice(&stdout)
        .map_err(|_| "admission-retry-refused: invalid consent validation result.".to_string())?;
    if success && value["status"] == "valid-consented-task" && &value["job_id"] == job_id
        && value["external_request_attempted"] == false && value["external_request_observed"] == false
    {
        Ok(())
    } else if !success && value["status"] == "rejected" {
        Err(format!("{}: {}", value["code"].as_str().unwrap_or("admission-retry-refused"),
                    value["message"].as_str().unwrap_or("consent validation failed")))
    } else {
        Err("admission-retry-refused: consent validation did not confirm the queued task.".to_string())
    }
}

fn spawn_worker(
    paths: DeliveryPaths,
    codeagent: RuntimeCodeAgentSettings,
    builder: BuilderConfiguration,
    task_id: String,
    attempt_id: String,
    immutable_task_digest_sha256: String,
    acknowledge_external_cost: bool,
    pipeline_deadline: Instant,
) -> Result<bool, String> {
    if !acknowledge_external_cost {
        return Err(EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED.to_string());
    }
    let key = worker_key(&task_id, &attempt_id)?;
    if let Some(worker) = remove_finished_worker(&key)? {
        let status_path = worker.status_path;
        worker
            .join
            .join()
            .map_err(|_| "上一次自动交付 worker 线程异常退出。".to_string())?;
        confirm_joined_worker_status(&status_path, &task_id, &attempt_id, None)?;
    }
    let active = ACTIVE_WORKERS.get_or_init(|| Mutex::new(BTreeSet::new()));
    let mut guard = active
        .lock()
        .map_err(|_| "交付 worker 状态锁已损坏。".to_string())?;
    if !guard.insert(key.clone()) {
        return Ok(false);
    }
    let active_guard = ActiveWorkerGuard::new(key.clone());
    drop(guard);
    let status_path = worker_status_path(&paths.root, &task_id, &attempt_id);
    let readiness =
        match builder_verifier_preflight_until(&paths, &codeagent, &builder, pipeline_deadline) {
            Ok(readiness) => readiness,
            Err(error) => {
                let cleanup_confirmed = controller_cleanup_confirmed(&error);
                let mut failed = worker_status(
                    "preflight-failed",
                    &task_id,
                    &attempt_id,
                    &immutable_task_digest_sha256,
                    &codeagent,
                );
                failed["error"] = json!(&error);
                failed["terminal"] = json!(true);
                failed["cleanup_confirmed"] = json!(cleanup_confirmed);
                failed["pipeline_budget_seconds"] = json!(PIPELINE_TIMEOUT.as_secs());
                failed["containment"] = json!({
                    "process_group": "hard-kill-and-confirm",
                    "descendant_census": "descendant-census-best-effort",
                    "adapter_live_gate_required": true
                });
                failed["execution_preflight"] = json!({
                    "status": "failed",
                    "provider_process_started": false,
                    "external_request_attempted": false,
                    "external_request_observed": false,
                    "source_executed": false
                });
                write_worker_status(&status_path, &failed)?;
                drop(active_guard);
                return Err(error);
            }
        };
    let mut starting = worker_status(
        "starting",
        &task_id,
        &attempt_id,
        &immutable_task_digest_sha256,
        &codeagent,
    );
    starting["execution_preflight"] = readiness;
    starting["terminal"] = json!(false);
    starting["cleanup_confirmed"] = json!(false);
    starting["pipeline_budget_seconds"] = json!(PIPELINE_TIMEOUT.as_secs());
    starting["containment"] = json!({
        "process_group": "hard-kill-and-confirm",
        "descendant_census": "descendant-census-best-effort",
        "adapter_live_gate_required": true
    });
    write_worker_status(&status_path, &starting)?;
    let cancel = Arc::new(AtomicBool::new(false));
    let worker_cancel = Arc::clone(&cancel);
    let worker_status_path = status_path.clone();
    // Reserve the registry entry before spawning. A poisoned registry lock must
    // never make a newly spawned worker unreachable and therefore unjoinable.
    let workers = WORKER_THREADS.get_or_init(|| Mutex::new(BTreeMap::new()));
    let mut worker_guard = match workers.lock() {
        Ok(guard) => guard,
        Err(_) => {
            overwrite_worker_terminal(
                &status_path,
                "worker-failed",
                "worker-registry-unavailable",
                "交付 worker 线程锁已损坏；worker 未启动。",
                true,
            )?;
            return Err("交付 worker 线程锁已损坏；worker 未启动。".to_string());
        }
    };
    if worker_guard.contains_key(&key) {
        return Err("精确绑定的交付 worker 已在进程内注册。".to_string());
    }
    let join = thread::spawn(move || {
        let result = wait_for_admission(pipeline_deadline, ADMISSION_WAIT_TIMEOUT, &worker_cancel, |deadline| {
            let mut command = base_command(&paths, &codeagent, Some(&builder))?;
            command
                .arg("--acknowledge-external-cost")
                .arg("run")
                .arg(&task_id)
                .arg("--attempt-id")
                .arg(&attempt_id);
            execute_until(command, deadline, Some(&worker_cancel))
        }, |deadline| {
            let mut command = base_command(&paths, &codeagent, None)?;
            command.arg("status").arg(&task_id);
            let status = execute_until(command, deadline, Some(&worker_cancel))?;
            confirm_queued_unspent(&paths.root, &status, &task_id, &attempt_id, &immutable_task_digest_sha256)?;
            validate_waiting_consent(&paths, &task_id, &attempt_id, &status["attempt"]["job_id"], deadline, &worker_cancel)
        });
        let value = match result {
            Ok(attempt) => {
                let mut value = worker_status(
                    "finished",
                    &task_id,
                    &attempt_id,
                    &immutable_task_digest_sha256,
                    &codeagent,
                );
                value["attempt"] = attempt;
                value["terminal"] = json!(true);
                value["cleanup_confirmed"] = json!(true);
                value
            }
            Err(error) => {
                let cancelled = error.starts_with("delivery-controller-cancelled:");
                let timed_out = error.starts_with("delivery-controller-timeout:");
                let cleanup_confirmed = controller_cleanup_confirmed(&error);
                let mut value = worker_status(
                    if cancelled {
                        "worker-cancelled"
                    } else if timed_out {
                        "worker-timeout"
                    } else {
                        "worker-failed"
                    },
                    &task_id,
                    &attempt_id,
                    &immutable_task_digest_sha256,
                    &codeagent,
                );
                value["attempt"] = Value::Null;
                value["error"] = json!(error);
                value["terminal"] = json!(true);
                value["cleanup_confirmed"] = json!(cleanup_confirmed);
                value
            }
        };
        let _ = write_worker_status(&worker_status_path, &value);
        drop(active_guard);
    });
    worker_guard.insert(
        key,
        WorkerThread {
            cancel,
            join,
            status_path,
        },
    );
    Ok(true)
}

fn read_worker_status(root: &Path, task_id: &str, attempt_id: &str) -> Option<Value> {
    if !valid_task_id(task_id) || !valid_attempt_id(attempt_id) {
        return None;
    }
    let path = worker_status_path(root, task_id, attempt_id);
    let metadata = path.symlink_metadata().ok()?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_WORKER_STATUS_BYTES
    {
        return None;
    }
    let value: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if value["schema_version"] != WORKER_STATUS_SCHEMA
        || value["task_id"] != task_id
        || value["attempt_id"] != attempt_id
    {
        return None;
    }
    Some(value)
}

fn task_bound_codeagent(
    paths: &DeliveryPaths,
    task_id: &str,
    attempt_id: &str,
    expected_immutable_task_digest: &str,
) -> Option<RuntimeCodeAgentSettings> {
    if !valid_task_id(task_id) || !valid_attempt_id(attempt_id) {
        return None;
    }
    let path = paths
        .root
        .join("tasks")
        .join(task_id)
        .join("attempts")
        .join(attempt_id)
        .join("input/task.json");
    let metadata = path.symlink_metadata().ok()?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() as usize > MAX_INPUT_BYTES
    {
        return None;
    }
    let task: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if task["immutable_task_digest_sha256"] != expected_immutable_task_digest {
        return None;
    }
    crate::codeagent_settings::runtime_from_task(&task).ok()
}

pub fn builder_configuration() -> Value {
    BuilderConfiguration::from_environment().public_view()
}

pub fn submit_and_start(
    data_dir: &Path,
    task: &Value,
    registry: &Value,
    explicit_user_submit: bool,
    acknowledge_external_cost: bool,
    previous_task_id: Option<&str>,
    codeagent: &RuntimeCodeAgentSettings,
) -> Result<Value, String> {
    if !explicit_user_submit {
        return Err("必须由用户点击明确提交按钮；本次没有启动。".to_string());
    }
    if !acknowledge_external_cost {
        return Err(EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED.to_string());
    }
    let task_bound_codeagent = crate::codeagent_settings::runtime_from_task(task)?;
    if &task_bound_codeagent != codeagent {
        return Err(
            "提交执行配置必须来自任务内已授权的 immutable provider/model 绑定。".to_string(),
        );
    }
    let immutable_task_digest = task["immutable_task_digest_sha256"]
        .as_str()
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or_else(|| "任务缺少有效 immutable task digest。".to_string())?
        .to_string();
    let (retry_task_id, edited_from_task_id) = classify_previous_task(task, previous_task_id)?;
    let paths = paths(data_dir)?;
    let pipeline_deadline = Instant::now() + PIPELINE_TIMEOUT;
    let builder = BuilderConfiguration::from_environment();
    let task_path = write_private_input(&paths.root, "task", task)?;
    let registry_path = write_private_input(&paths.root, "registry", registry)?;
    let mut command = base_command(&paths, codeagent, None)?;
    command
        .arg("submit")
        .arg("--task")
        .arg(task_path)
        .arg("--registry-no-match")
        .arg(registry_path);
    if explicit_user_submit {
        command.arg("--explicit-user-submit");
    }
    if let Some(task_id) = retry_task_id {
        command.arg("--retry-task-id").arg(task_id);
    }
    if let Some(task_id) = edited_from_task_id {
        command.arg("--edited-from-task-id").arg(task_id);
    }
    let mut attempt = execute_until(command, pipeline_deadline, None)?;
    let task_id = attempt["task_id"]
        .as_str()
        .filter(|value| valid_task_id(value))
        .ok_or_else(|| "交付控制器回执缺少有效 task_id。".to_string())?
        .to_string();
    let attempt_id = attempt["attempt_id"]
        .as_str()
        .filter(|value| valid_attempt_id(value))
        .ok_or_else(|| "交付控制器回执缺少有效 attempt_id。".to_string())?
        .to_string();
    let status_path = worker_status_path(&paths.root, &task_id, &attempt_id);
    write_worker_status(
        &status_path,
        &worker_status(
            "authorized-awaiting-builder",
            &task_id,
            &attempt_id,
            &immutable_task_digest,
            codeagent,
        ),
    )?;
    attempt["external_cost_acknowledged"] = json!(true);
    attempt["provider_id"] = json!(codeagent.provider_id);
    attempt["task_provider"] = json!(codeagent.task_provider);
    attempt["model"] = json!(codeagent.model);
    let configuration = builder.public_view();
    if !builder.ready() {
        return Ok(json!({
            "receipt": attempt,
            "codeagent_adapter": {
                "provider_id": codeagent.provider_id,
                "adapter": "automatic-delivery-controller",
                "started": false,
                "status": "builder-configuration-required",
                "error": "隔离 Builder 与独立 Verifier 尚未完整配置；为避免生成后无法验收，本次没有启动 CodeAgent。",
                "external_request_attempted": false,
                "external_request_observed": false,
                "external_cost_acknowledged": true
            },
            "builder_configuration": configuration
        }));
    }

    spawn_worker(
        paths,
        codeagent.clone(),
        builder,
        task_id.clone(),
        attempt_id.clone(),
        immutable_task_digest,
        acknowledge_external_cost,
        pipeline_deadline,
    )?;

    Ok(json!({
        "receipt": attempt,
        "codeagent_adapter": {
            "provider_id": codeagent.provider_id,
            "adapter": "automatic-delivery-controller",
            "started": true,
            "status": "codeagent-starting",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "external_request_attempted": false,
            "external_request_observed": false,
            "external_cost_acknowledged": true
        },
        "builder_configuration": configuration
    }))
}

/// Product DTO adapter for the controller's split lineage contract. The
/// historical `retry_task_id` field is treated as a previous-task hint and is
/// classified from the immutable task identity, so an edited task can never be
/// forwarded as `--retry-task-id`. New callers should send
/// `edited_from_task_id` explicitly for edited NeedSpec replacements.
pub fn submit_and_start_with_lineage(
    data_dir: &Path,
    task: &Value,
    registry: &Value,
    explicit_user_submit: bool,
    acknowledge_external_cost: bool,
    retry_task_id: Option<&str>,
    edited_from_task_id: Option<&str>,
    codeagent: &RuntimeCodeAgentSettings,
) -> Result<Value, String> {
    if retry_task_id.is_some() && edited_from_task_id.is_some() {
        return Err(
            "retryTaskId 与 editedFromTaskId 不能同时提供；同任务重试和编辑后替换是不同操作。"
                .to_string(),
        );
    }
    if let Some(edited_from_task_id) = edited_from_task_id {
        let (retry, edited) = classify_previous_task(task, Some(edited_from_task_id))?;
        if retry.is_some() || edited != Some(edited_from_task_id) {
            return Err(
                "editedFromTaskId 只能用于 immutable task 已变化的新任务；同任务请使用 retryTaskId。"
                    .to_string(),
            );
        }
    }
    submit_and_start(
        data_dir,
        task,
        registry,
        explicit_user_submit,
        acknowledge_external_cost,
        edited_from_task_id.or(retry_task_id),
        codeagent,
    )
}

fn status_with_paths(
    paths: &DeliveryPaths,
    task_id: &str,
    codeagent: &RuntimeCodeAgentSettings,
) -> Result<Value, String> {
    if !valid_task_id(task_id) {
        return Err("task_id 格式无效。".to_string());
    }
    let mut command = base_command(paths, codeagent, None)?;
    command.arg("status").arg(task_id);
    execute(command, COMMAND_TIMEOUT)
}

pub fn status(data_dir: &Path, task_id: &str) -> Result<Value, String> {
    status_with_paths(&paths(data_dir)?, task_id, &history_codeagent())
}

fn history_with_paths(
    paths: &DeliveryPaths,
    task_id: &str,
    codeagent: &RuntimeCodeAgentSettings,
) -> Result<Value, String> {
    if !valid_task_id(task_id) {
        return Err("task_id 格式无效。".to_string());
    }
    let mut command = base_command(paths, codeagent, None)?;
    command.arg("history").arg(task_id);
    execute(command, COMMAND_TIMEOUT)
}

pub fn history(data_dir: &Path, task_id: &str) -> Result<Value, String> {
    history_with_paths(&paths(data_dir)?, task_id, &history_codeagent())
}

pub fn run(
    data_dir: &Path,
    task_id: &str,
    attempt_id: Option<&str>,
    acknowledge_external_cost: bool,
    codeagent: &RuntimeCodeAgentSettings,
) -> Result<Value, String> {
    if !acknowledge_external_cost {
        return Err(EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED.to_string());
    }
    let paths = paths(data_dir)?;
    if !valid_task_id(task_id) {
        return Err("task_id 格式无效。".to_string());
    }
    if attempt_id.is_some_and(|value| !valid_attempt_id(value)) {
        return Err("attempt_id 格式无效。".to_string());
    }
    let builder = BuilderConfiguration::from_environment();
    if !builder.ready() {
        return Err(
            "builder-configuration-required: 隔离 Builder 与独立 Verifier 未完整配置。".to_string(),
        );
    }
    let pipeline_deadline = Instant::now() + PIPELINE_TIMEOUT;
    builder_verifier_preflight_until(&paths, codeagent, &builder, pipeline_deadline)?;
    let mut command = base_command(&paths, codeagent, Some(&builder))?;
    command
        .arg("--acknowledge-external-cost")
        .arg("run")
        .arg(task_id);
    if let Some(attempt_id) = attempt_id {
        command.arg("--attempt-id").arg(attempt_id);
    }
    execute_until(command, pipeline_deadline, None)
}

fn compact_history(attempts: &[Value]) -> Vec<Value> {
    attempts
        .iter()
        .map(|attempt| {
            json!({
                "task_id": attempt["task_id"],
                "attempt_id": attempt["attempt_id"],
                "attempt_number": attempt["attempt_number"],
                "retry_of_attempt_id": attempt["retry_of_attempt_id"],
                "need_id": attempt["need_id"],
                "need_spec_revision": attempt["need_spec_revision"],
                "job_id": attempt["job_id"],
                "status": attempt["status"],
                "stage": attempt["stage"],
                "error": attempt["error"],
                "codeagent_diagnostics": safe_codeagent_diagnostics(attempt),
                "outputs": attempt["outputs"],
                "created_at_utc": attempt["created_at_utc"],
                "updated_at_utc": attempt["updated_at_utc"]
            })
        })
        .collect()
}

fn diagnostic_integer(value: &Value, maximum: u64) -> Option<u64> {
    value.as_u64().filter(|number| *number <= maximum)
}

fn safe_model_request_budget(value: &Value, requests: u64) -> Option<Value> {
    let object = value.as_object()?;
    let phase = value["phase"].as_str()?;
    if object.len() != 11 || value["schema_version"] != "vibapp.model-request-budget-v1"
        || !matches!(phase, "authoring" | "repair") { return None; }
    for prefix in ["total", "authoring", "repair"] {
        let limit = diagnostic_integer(object.get(&format!("{prefix}_limit"))?, 64)?;
        let used = diagnostic_integer(object.get(&format!("{prefix}_used"))?, 64)?;
        let remaining = diagnostic_integer(object.get(&format!("{prefix}_remaining"))?, 64)?;
        if limit == 0 || used + remaining != limit { return None; }
    }
    let number = |key: &str| value[key].as_u64().unwrap();
    if number("total_limit") != number("authoring_limit") + number("repair_limit")
        || number("total_used") != number("authoring_used") + number("repair_used")
        || number("total_used") != requests || (phase == "authoring" && number("repair_used") != 0) { return None; }
    Some(value.clone())
}

fn safe_failure_diagnostic(value: &Value) -> Option<Value> {
    let object = value.as_object()?;
    if object.len() != 14 || value["schema_version"] != "vibapp.docker-failure-diagnostic-v1"
        || !matches!(value["failure_origin"].as_str()?, "provider-process" | "provider-spawn"
            | "provider-output-limit" | "bridge-process" | "host-relay" | "host-control"
            | "host-deadline" | "host-cancellation" | "host-protocol" | "unknown")
        || !matches!(value["provider_error_category"].as_str()?, "stream-decode" | "stream-disconnected"
            | "authentication" | "rate-limit" | "thread-resource" | "process-failed" | "unknown" | "none") { return None; }
    let signal = object.get("child_signal")?;
    if !signal.is_null() && !matches!(signal.as_str()?, "SIGABRT" | "SIGBUS" | "SIGFPE" | "SIGHUP"
        | "SIGILL" | "SIGINT" | "SIGKILL" | "SIGPIPE" | "SIGQUIT" | "SIGSEGV" | "SIGTERM"
        | "SIGTRAP" | "SIGXCPU" | "SIGXFSZ" | "other") { return None; }
    for (key, maximum) in [("child_exit_code", 255), ("container_exit_code", 255),
        ("stdout_bytes", 8_388_608), ("stderr_bytes", 8_388_608),
        ("bridge_stdout_bytes", 8_388_608), ("bridge_stderr_bytes", 8_388_608)] {
        let item = object.get(key)?;
        if !item.is_null() { diagnostic_integer(item, maximum)?; }
    }
    for key in ["output_limit_exceeded", "frame_limit_exceeded", "container_oom_killed", "container_running"] {
        let item = object.get(key)?;
        if !item.is_null() { item.as_bool()?; }
    }
    Some(value.clone())
}

fn safe_codeagent_diagnostics(attempt: &Value) -> Option<Value> {
    let input = attempt.get("codeagent_diagnostics")?.as_object()?;
    let state = input.get("state")?.as_str()?;
    let terminal = matches!(state, "succeeded" | "failed" | "cancelled");
    if !terminal && !matches!(state, "authoring" | "compiler-feedback" | "model-retry") {
        return None;
    }
    if (!terminal && (attempt["status"] != "running" || attempt["stage"] != "codeagent-running"))
        || (attempt["status"] == "private-appstore-ready" && state != "succeeded")
    {
        return None;
    }
    let requests = diagnostic_integer(input.get("model_requests")?, 64)?;
    let checks = diagnostic_integer(input.get("compiler_checks")?, 3)?;
    let mut result = json!({"state": state, "model_requests": requests, "compiler_checks": checks});
    if let Some(budget) = input.get("model_request_budget") {
        result["model_request_budget"] = safe_model_request_budget(budget, requests)?;
    }
    for (key, maximum) in [("logical_model_requests", 64), ("model_retries", 8)] {
        if let Some(value) = input.get(key) {
            let count = diagnostic_integer(value, maximum)?;
            if count > requests + u64::from(terminal && key == "logical_model_requests") {
                return None;
            }
            result[key] = json!(count);
        }
    }
    if let (Some(logical), Some(retries)) = (result["logical_model_requests"].as_u64(), result["model_retries"].as_u64()) {
        if logical + retries < requests || logical + retries > requests + u64::from(terminal) {
            return None;
        }
    }
    if state == "model-retry" {
        let retry = input.get("retry")?.as_object()?;
        let attempt_number = diagnostic_integer(retry.get("attempt")?, 3).filter(|number| *number >= 2)?;
        let maximum = retry.get("max_attempts")?.as_u64().filter(|number| *number == 3)?;
        let http_status = retry.get("http_status")?.as_u64().filter(|number| matches!(number, 429 | 502 | 503 | 504))?;
        let delay = retry.get("delay_seconds")?.as_f64().filter(|number| number.is_finite() && (0.0..=30.0).contains(number))?;
        let reason = if http_status == 429 { "provider-rate-limited" } else { "provider-upstream-unavailable" };
        let request_limit = result["model_request_budget"]["total_limit"].as_u64().unwrap_or(24);
        if retry.len() != 5 || retry.get("reason")?.as_str()? != reason || requests > request_limit
            || result["logical_model_requests"].is_null() || result["model_retries"].is_null()
        {
            return None;
        }
        result["retry"] = json!({"attempt": attempt_number, "max_attempts": maximum, "http_status": http_status, "delay_seconds": delay, "reason": reason});
    }
    if terminal {
        if matches!(state, "failed" | "cancelled") {
            if let Some(observation) = input.get("failure_diagnostic").and_then(safe_failure_diagnostic) {
                result["failure_diagnostic"] = observation;
            }
            if let Some(code) = input.get("failure_code").and_then(Value::as_str).filter(|code| matches!(*code,
                "provider-upstream-unavailable" | "provider-rate-limited" | "provider-authentication-failed"
                | "provider-upstream-rejected" | "provider-timeout" | "provider-network-error"
                | "provider-cancelled" | "provider-request-budget-exhausted" | "provider-request-limit"
                | "provider-response-limit" | "provider-output-invalid" | "provider-output-limit"
                | "provider-protocol-invalid" | "provider-upstream-protocol-invalid" | "provider-failed"
                | "provider-container-failed" | "docker-cleanup-unconfirmed" | "docker-execution-failed")) {
                result["failure_code"] = json!(code);
            }
        }
        if let Some(status) = input.get("upstream_status").and_then(|value| diagnostic_integer(value, 599)).filter(|number| *number >= 100) {
            result["upstream_status"] = json!(status);
        }
        if let Some(request) = input.get("upstream_request").and_then(Value::as_object) {
            let mut safe_request = json!({});
            for (key, maximum) in [("ordinal", 64), ("bytes", 8 * 1024 * 1024), ("connect_attempts", 2)] {
                if let Some(number) = request.get(key).and_then(|value| diagnostic_integer(value, maximum)) {
                    safe_request[key] = json!(number);
                }
            }
            if let Some(phase) = request.get("phase").and_then(Value::as_str).filter(|phase| matches!(*phase, "validate" | "connect" | "send-request" | "response-headers" | "http-rejected" | "response-body" | "complete")) {
                safe_request["phase"] = json!(phase);
            }
            if let Some(error) = request.get("transport_error").and_then(Value::as_str).filter(|error| matches!(*error, "timeout" | "http-framing" | "connection")) {
                safe_request["transport_error"] = json!(error);
            }
            result["upstream_request"] = safe_request;
        }
    }
    Some(result)
}

fn has_durable_stage_output(outputs: &Value, path_key: &str, digest_key: &str) -> bool {
    let valid_path = outputs[path_key].as_str().is_some_and(|value| {
        !value.is_empty()
            && value.len() <= 1024
            && !value.starts_with('/')
            && !value.contains('\\')
            && value
                .split('/')
                .all(|part| !part.is_empty() && part != "." && part != "..")
    });
    let valid_digest = outputs[digest_key].as_str().is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    });
    valid_path && valid_digest
}

fn attempt_to_job(
    attempt: &Value,
    attempts: &[Value],
    builder: &BuilderConfiguration,
    worker: Option<&Value>,
) -> Option<Value> {
    attempt["task_id"]
        .as_str()
        .filter(|value| valid_task_id(value))?;
    attempt["attempt_id"]
        .as_str()
        .filter(|value| valid_attempt_id(value))?;
    let status = attempt["status"].as_str()?;
    let stage = attempt["stage"].as_str()?;
    let progress = attempt["progress_percent"].as_u64()?;
    let outputs = &attempt["outputs"];
    let manifest_digest_bound = outputs["manifest_sha256"].as_str().is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    });
    let terminal_ready = status == "private-appstore-ready";
    let binding_proven = outputs["task_handoff_manifest_binding_proven"] == true
        && outputs["digest_equality_proven"] == true
        && manifest_digest_bound;
    // Pre-binding terminal records are historical evidence, not an exact
    // task/handoff/manifest acceptance.  Keep them visible but fail closed.
    let unbound_terminal = terminal_ready && !binding_proven;
    let ready = terminal_ready && binding_proven;
    let terminal_attempt_failed = status == "failed" || unbound_terminal;
    let queued_without_builder = status == "queued" && !builder.ready();
    let worker_error = worker
        .filter(|value| {
            matches!(
                value["status"].as_str(),
                Some(
                    "worker-failed"
                        | "preflight-failed"
                        | "worker-cancelled"
                        | "worker-timeout"
                        | "worker-cancellation-unconfirmed"
                )
            )
        })
        .and_then(|value| value["error"].as_str());
    let error = if unbound_terminal {
        json!({
            "stage": "appstore",
            "code": "delivery-binding-unproven",
            "message": "旧交付记录缺少 task / handoff / manifest 精确绑定证明，不能作为已验收应用。",
            "retryable_with_new_attempt": true
        })
    } else if queued_without_builder {
        json!({
            "stage": "builder-configuration",
            "code": "builder-configuration-required",
            "message": "隔离 Builder 与独立 Verifier尚未完整配置；CodeAgent 未启动。",
            "retryable_with_new_attempt": false,
            "configuration": builder.public_view()
        })
    } else if let Some(message) = worker_error {
        let worker_status = worker.and_then(|value| value["status"].as_str());
        let (stage, code) = if worker_status == Some("worker-cancellation-unconfirmed") {
            ("delivery-worker", "worker-cancellation-unconfirmed")
        } else if worker_status == Some("worker-cancelled") {
            ("delivery-worker", "worker-cancelled")
        } else if worker_status == Some("worker-timeout") {
            ("delivery-worker", "worker-timeout")
        } else if message.contains("builder-preflight-failed") {
            ("builder", "builder-preflight-failed")
        } else if message.contains("verifier-preflight-failed") {
            ("verifier", "verifier-preflight-failed")
        } else {
            ("delivery-worker", "delivery-worker-failed")
        };
        json!({
            "stage": stage,
            "code": code,
            "message": message,
            "retryable_with_new_attempt": true
        })
    } else {
        attempt["error"].clone()
    };
    let pipeline_failed = terminal_attempt_failed || worker_error.is_some();
    let diagnostics = safe_codeagent_diagnostics(attempt).filter(|value| {
        !pipeline_failed || value["retry"].is_null()
    });
    let retrying = !queued_without_builder && diagnostics.as_ref().is_some_and(|value| value["state"] == "model-retry");
    let public_status = if ready {
        "succeeded"
    } else if terminal_attempt_failed {
        "failed"
    } else if worker_error.is_some() {
        "delivery-worker-failed"
    } else if queued_without_builder {
        "builder-not-configured"
    } else {
        // The current UI uses this legacy value as its polling sentinel. Exact pipeline
        // truth remains in current_stage, stages, error, outputs and history.
        "codeagent-running"
    };
    let failure_stage = error["stage"].as_str().unwrap_or("");
    let codeagent_output =
        has_durable_stage_output(outputs, "source_handoff_path", "source_handoff_sha256");
    let builder_output =
        has_durable_stage_output(outputs, "builder_receipt_path", "builder_receipt_sha256");
    let verifier_output =
        has_durable_stage_output(outputs, "candidate_path", "candidate_record_sha256");
    let stage_state = |name: &str, durable_output: bool| -> &'static str {
        if pipeline_failed && failure_stage == name {
            "failed"
        } else if ready || durable_output {
            "succeeded"
        } else if queued_without_builder && name == "builder" {
            "blocked"
        } else {
            "pending"
        }
    };
    let summary = if ready {
        "CodeAgent 源码已由隔离 Builder 编译，并经独立 Verifier 验收，已进入私有 AppStore。"
            .to_string()
    } else if !error.is_null() {
        error["message"]
            .as_str()
            .unwrap_or("自动交付失败，未产生可运行应用。")
            .to_string()
    } else {
        format!("自动交付正在执行：{stage}。未完成前不视为可运行应用。")
    };
    Some(json!({
        "job_id": attempt["job_id"],
        "task_id": attempt["task_id"],
        "attempt_id": attempt["attempt_id"],
        "immutable_task_digest_sha256": attempt["immutable_task_digest_sha256"],
        "need_id": attempt["need_id"],
        "provider_id": worker.map(|value| &value["provider_id"]),
        "title": attempt["title"],
        "route": "automatic-delivery-controller",
        "status": public_status,
        "progress_percent": if queued_without_builder { 5 } else { progress },
        "current_stage": if queued_without_builder { "builder-configuration-required" } else if retrying { "model-retry" } else { stage },
        "updated_at_utc": attempt["updated_at_utc"],
        "history": compact_history(attempts),
        "error": error,
        "codeagent_diagnostics": diagnostics,
        "outputs": attempt["outputs"],
        "stages": [
            {"kind": "queue", "status": "succeeded", "label": "确认 Registry 无匹配并进入交付队列"},
            {"kind": "codeagent", "status": stage_state("codeagent", codeagent_output), "label": "配置的 CodeAgent 编写源码"},
            {"kind": "builder", "status": stage_state("builder", builder_output), "label": "隔离 Builder 编译"},
            {"kind": "verifier", "status": stage_state("verifier", verifier_output), "label": "独立 Verifier 验收"},
            {"kind": "appstore", "status": if ready { "succeeded" } else if pipeline_failed && failure_stage == "appstore" { "failed" } else { "pending" }, "label": "进入私有 AppStore"}
        ],
        "verification": {
            "status": if ready { "verified" } else if pipeline_failed { "failed" } else if queued_without_builder { "builder-not-configured" } else { "running" },
            "independent": ready || verifier_output || matches!(failure_stage, "verifier" | "appstore"),
            "summary": summary
        },
        "delivery_worker": worker,
        "builder_configuration": builder.public_view()
    }))
}

#[derive(Default)]
struct ArchivedTaskIds {
    tasks: BTreeSet<String>,
    jobs: BTreeSet<(String, String, String, String)>,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ArchivedListReply {
    schema_version: String,
    archived: Vec<ArchivedListRow>,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ArchivedListRow {
    task_id: String,
    job_id: String,
    immutable_task_digest_sha256: String,
    provider_execution_identity_sha256: Option<String>,
    consent_id: Option<String>,
}

fn archive_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn parse_archived_task_ids(bytes: &[u8]) -> Option<ArchivedTaskIds> {
    if bytes.is_empty() || bytes.len() > MAX_REPLY_BYTES {
        return None;
    }
    // Typed deserialization also rejects duplicate object fields. The trusted
    // helper owns record/snapshot/terminal-state checks; Rust only accepts its
    // small, closed projection and never interprets arbitrary marker files.
    let reply: ArchivedListReply = serde_json::from_slice(bytes).ok()?;
    if reply.schema_version != "vibapp.delivery-task-archive-list-v1"
        || reply.archived.len() > MAX_ARCHIVED_TASKS
    {
        return None;
    }
    let mut result = ArchivedTaskIds::default();
    for row in reply.archived {
        if !valid_task_id(&row.task_id)
            || row.job_id.is_empty()
            || row.job_id.len() > 128
            || !row.job_id.as_bytes()[0].is_ascii_alphanumeric()
            || !row.job_id.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
            || !archive_sha256(&row.immutable_task_digest_sha256)
            || row.provider_execution_identity_sha256.as_deref().is_some_and(|value| !archive_sha256(value))
            || row.consent_id.as_deref().is_some_and(|value| value.is_empty() || value.len() > 128 || value.chars().any(char::is_control))
            || !result.tasks.insert(row.task_id)
        {
            return None;
        }
        if let (Some(provider), Some(consent)) = (row.provider_execution_identity_sha256, row.consent_id) {
            result.jobs.insert((row.job_id, row.immutable_task_digest_sha256, provider, consent));
        }
    }
    Some(result)
}

fn archive_list_command(paths: &DeliveryPaths, data_dir: &Path) -> Result<Command, String> {
    let mut command = Command::new(&paths.python);
    command
        .args(["-X", "utf8"]).arg("-I")
        .arg("-B")
        .arg(paths.controller.with_file_name("task_archive.py"))
        .arg("--data-root")
        .arg(data_dir)
        .arg("--archived-only")
        .env_clear()
        .envs(native_platform::trusted_system_environment()?)
        .env("PATH", native_platform::safe_path()?)
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("TZ", "UTC")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // This helper only reads archive evidence. Do not inherit provider identity,
    // credentials, HOME or a write/unknown-root override from a history request.
    Ok(command)
}

fn read_archive_list(mut command: Command, timeout: Duration) -> Option<ArchivedTaskIds> {
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(not(unix))]
    return None;
    let deadline = Instant::now() + timeout;
    let child = command.spawn().ok()?;
    let (success, stdout, _) = wait_bounded(child, deadline, None).ok()?;
    success.then(|| parse_archived_task_ids(&stdout)).flatten()
}

fn archived_task_ids(paths: &DeliveryPaths, data_dir: &Path) -> ArchivedTaskIds {
    archive_list_command(paths, data_dir)
        .ok()
        .and_then(|command| read_archive_list(command, ARCHIVE_LIST_TIMEOUT))
        .unwrap_or_default() // Missing/stale/invalid helper evidence never hides a task.
}

fn task_ids(root: &Path, archived: &ArchivedTaskIds) -> Vec<String> {
    let Ok(entries) = fs::read_dir(root.join("tasks")) else {
        return Vec::new();
    };
    let mut values = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.file_type().ok()?;
            let value = entry.file_name().into_string().ok()?;
            (metadata.is_dir() && valid_task_id(&value) && !archived.tasks.contains(&value)).then_some(value)
        })
        .collect::<Vec<_>>();
    values.sort();
    values.reverse();
    values.truncate(MAX_TASKS);
    values
}

pub fn jobs(data_dir: &Path) -> Result<Vec<Value>, String> {
    let paths = paths(data_dir)?;
    let builder = BuilderConfiguration::from_environment();
    let codeagent = history_codeagent();
    let archived = archived_task_ids(&paths, data_dir);
    let mut jobs = Vec::new();
    for task_id in task_ids(&paths.root, &archived) {
        let history = match history_with_paths(&paths, &task_id, &codeagent) {
            Ok(history) => history,
            Err(_) => { jobs.push(unreadable_history_job(&task_id)); continue; }
        };
        let Some(attempts) = history["attempts"].as_array() else {
            jobs.push(unreadable_history_job(&task_id));
            continue;
        };
        let Some(attempt) = attempts.last() else {
            jobs.push(unreadable_history_job(&task_id));
            continue;
        };
        let attempt_id = attempt["attempt_id"].as_str().unwrap_or_default();
        let worker = read_worker_status(&paths.root, &task_id, attempt_id);
        if let Some(mut job) = attempt_to_job(attempt, attempts, &builder, worker.as_ref()) {
            if history["legacy_read_only"] == true {
                mark_legacy_history(&mut job, attempt);
            }
            jobs.push(job);
        } else {
            jobs.push(unreadable_history_job(&task_id));
        }
    }
    Ok(jobs)
}

fn unreadable_history_job(task_id: &str) -> Value {
    json!({"job_id": task_id, "task_id": task_id, "title": "无法读取的历史任务",
        "status": "history-unavailable", "legacy_read_only": true, "current_stage": "history",
        "history": [], "outputs": {}, "progress_percent": 0,
        "error": {"code": "history-record-unreadable", "message": "这条历史记录损坏或版本暂不支持；其他任务仍可正常查看。原记录已保留，不会自动重跑。"},
        "verification": {"status": "historical", "independent": false,
            "summary": "仅此记录暂时不可读取，不影响其他任务。"}})
}

fn mark_legacy_history(job: &mut Value, attempt: &Value) {
    let status = attempt["status"].as_str().unwrap_or("");
    let summary = match status {
        "private-appstore-ready" => "历史记录：当时已完成交付。这里只读展示原结果，不代表新版流程重新验收。",
        "failed" => "历史记录：此前开发失败，原始原因保留在详情中；不会自动重新提交。",
        _ => "历史记录的运行状态未收尾，不能据此认定仍有任务运行；不会自动重新提交。",
    };
    job["legacy_read_only"] = json!(true);
    job["historical_status"] = attempt["status"].clone();
    job["status"] = json!(if matches!(status, "private-appstore-ready" | "failed") { "legacy-history" } else { "legacy-state-unproven" });
    job["current_stage"] = json!("historical-record");
    job["error"] = attempt["error"].clone();
    job["codeagent_diagnostics"] = Value::Null;
    job["stages"] = json!([]);
    job["verification"] = json!({"status": "historical", "independent": false, "summary": summary});
}

fn history_codeagent() -> RuntimeCodeAgentSettings {
    // status/history never execute a provider.  A stable provider keeps old history
    // readable even while the user has selected a recognized-but-pending adapter.
    RuntimeCodeAgentSettings {
        provider_id: "codex".to_string(),
        task_provider: "openai-codex".to_string(),
        model: "history-read-only-no-provider-invocation".to_string(),
    }
}

pub fn merge_with_legacy(delivery: Vec<Value>, legacy: Vec<Value>) -> Vec<Value> {
    let delivery_ids = delivery
        .iter()
        .filter_map(|item| item["job_id"].as_str())
        .map(str::to_owned)
        .collect::<BTreeSet<_>>();
    delivery
        .into_iter()
        .chain(legacy.into_iter().filter(|item| {
            item["job_id"]
                .as_str()
                .is_none_or(|job_id| !delivery_ids.contains(job_id))
        }))
        .collect()
}

fn without_archived_jobs(jobs: Vec<Value>, archived: &ArchivedTaskIds) -> Vec<Value> {
    jobs.into_iter().filter(|item| {
        // A matching historic ID is not authority to hide a newer successful,
        // active or ambiguous feed row from a different source.
        if !matches!(item["status"].as_str(), Some("failed" | "codeagent-failed"))
            || item["verification"]["status"] == "verified"
            || !item["outputs"]["app_id"].is_null()
            || !item["outputs"]["appstore_candidate_path"].is_null()
            || item["outputs"]["appstore_created"] == true
        {
            return true;
        }
        if let Some(task_id) = item["task_id"].as_str() {
            // A new exact task is not hidden merely because it shares an old job ID.
            !archived.tasks.contains(task_id)
        } else if item["task_id"].is_null() {
            let binding = (item["job_id"].as_str(),
                item["queue_receipt"]["immutable_task_digest_sha256"].as_str(),
                item["queue_receipt"]["provider_execution_identity_sha256"].as_str(),
                item["queue_receipt"]["consent_id"].as_str());
            match binding {
                (Some(job), Some(digest), Some(provider), Some(consent)) =>
                    !archived.jobs.contains(&(job.to_owned(), digest.to_owned(), provider.to_owned(), consent.to_owned())),
                _ => true,
            }
        } else {
            true
        }
    }).collect()
}


pub fn merge_with_legacy_for_data_dir(data_dir: &Path, delivery: Vec<Value>, legacy: Vec<Value>) -> Vec<Value> {
    let merged = merge_with_legacy(delivery, legacy);
    let Ok(paths) = paths(data_dir) else {
        return merged;
    };
    // Recheck after all sources merge so an archived controller task cannot
    // reappear through its older orchestrator row. Failure means show all rows.
    without_archived_jobs(merged, &archived_task_ids(&paths, data_dir))
}

pub fn resume_pending(data_dir: &Path, jobs: &[Value]) -> Result<usize, String> {
    let builder = BuilderConfiguration::from_environment();
    if !builder.ready() {
        return Ok(0);
    }
    let paths = paths(data_dir)?;
    let mut started = 0usize;
    for job in jobs.iter().take(MAX_TASKS) {
        if job["legacy_read_only"] == true {
            continue;
        }
        if job["status"] != "codeagent-running" && job["status"] != "delivery-worker-failed" {
            continue;
        }
        if job["status"] == "delivery-worker-failed"
            && job["delivery_worker"]["worker_session_id"] == worker_session_id()
        {
            continue;
        }
        if job["delivery_worker"]["external_cost_acknowledged"] != true {
            continue;
        }
        let Some(immutable_task_digest) =
            job["immutable_task_digest_sha256"]
                .as_str()
                .filter(|value| {
                    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
                })
        else {
            continue;
        };
        let Some(task_id) = job["task_id"].as_str().filter(|value| valid_task_id(value)) else {
            continue;
        };
        let Some(attempt_id) = job["attempt_id"]
            .as_str()
            .filter(|value| valid_attempt_id(value))
        else {
            continue;
        };
        let Some(persisted) = persisted_codeagent(&job["delivery_worker"], immutable_task_digest)
        else {
            continue;
        };
        let Some(codeagent) =
            task_bound_codeagent(&paths, task_id, attempt_id, immutable_task_digest)
        else {
            continue;
        };
        if persisted != codeagent {
            continue;
        }
        if spawn_worker(
            paths.clone(),
            codeagent,
            builder.clone(),
            task_id.to_string(),
            attempt_id.to_string(),
            immutable_task_digest.to_string(),
            true,
            Instant::now() + PIPELINE_TIMEOUT,
        )? {
            started += 1;
        }
    }
    Ok(started)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn attempt(status: &str, stage: &str, progress: u64) -> Value {
        json!({
            "task_id": "development-0123456789abcdef0123456789abcdef",
            "attempt_id": "attempt-0002-0123456789abcdef",
            "attempt_number": 2,
            "retry_of_attempt_id": "attempt-0001-fedcba9876543210",
            "need_id": "need-1234",
            "title": "测试应用",
            "need_spec_revision": 3,
            "job_id": "job-new",
            "immutable_task_digest_sha256": "a".repeat(64),
            "status": status,
            "stage": stage,
            "progress_percent": progress,
            "error": null,
            "outputs": {},
            "created_at_utc": "2026-08-27T01:00:00Z",
            "updated_at_utc": "2026-08-27T01:01:00Z"
        })
    }

    fn ready_builder() -> BuilderConfiguration {
        BuilderConfiguration {
            builder_input_root: Some(PathBuf::from("/accepted")),
            tool_layer: Some(PathBuf::from("/accepted/tool-layer")),
            cargo_home: Some(PathBuf::from("/accepted/cargo-home")),
            cache_acceptance: Some(PathBuf::from("/accepted/cache.json")),
            wasm_tools: Some(PathBuf::from("/accepted/wasm-tools")),
            errors: Vec::new(),
        }
    }

    #[test]
    fn legacy_history_is_visible_but_never_claims_a_current_worker_or_new_acceptance() {
        for status in ["running", "queued", "failed", "private-appstore-ready"] {
            let original = attempt(status, "codeagent-running", 20);
            let mut job = attempt_to_job(&original, &[original.clone()], &ready_builder(), None).unwrap();
            mark_legacy_history(&mut job, &original);
            assert_eq!(job["legacy_read_only"], true);
            assert_eq!(job["historical_status"], status);
            assert_eq!(job["verification"]["status"], "historical");
            assert!(!["codeagent-running", "delivery-worker-failed", "succeeded"].contains(&job["status"].as_str().unwrap()));
            assert!(job["codeagent_diagnostics"].is_null());
            assert_eq!(job["history"].as_array().unwrap().len(), 1);
            assert_eq!(job["outputs"], original["outputs"]);
        }
    }

    #[test]
    fn unreadable_record_has_its_own_inert_task_row_instead_of_losing_the_feed() {
        let id = "development-0123456789abcdef0123456789abcdef";
        let row = unreadable_history_job(id);
        assert_eq!(row["task_id"], id);
        assert_eq!(row["status"], "history-unavailable");
        assert_eq!(row["legacy_read_only"], true);
        assert_eq!(row["error"]["code"], "history-record-unreadable");
        assert!(row["outputs"].as_object().unwrap().is_empty());
    }

    #[test]
    fn live_retry_is_visible_without_changing_pipeline_authority() {
        let mut current = attempt("running", "codeagent-running", 20);
        current["codeagent_diagnostics"] = json!({
            "state": "model-retry", "model_requests": 1, "logical_model_requests": 1,
            "model_retries": 0, "compiler_checks": 0,
            "retry": {"attempt": 2, "max_attempts": 3, "http_status": 502, "delay_seconds": 1.25, "reason": "provider-upstream-unavailable"}
        });
        let job = attempt_to_job(&current, &[current.clone()], &ready_builder(), None).unwrap();
        assert_eq!(job["status"], "codeagent-running");
        assert_eq!(job["current_stage"], "model-retry");
        assert_eq!(job["codeagent_diagnostics"]["retry"]["attempt"], 2);
        assert_eq!(job["codeagent_diagnostics"]["model_retries"], 0);
        assert_eq!(stage_status(&job, "codeagent"), "pending");
        current["status"] = json!("failed");
        let terminal = attempt_to_job(&current, &[current.clone()], &ready_builder(), None).unwrap();
        assert_eq!(terminal["status"], "failed");
        assert!(terminal["codeagent_diagnostics"].is_null());
    }

    #[test]
    fn later_transport_retries_remain_visible_without_becoming_compiler_repairs() {
        let mut current = attempt("running", "codeagent-running", 20);
        for retries in [5, 8] {
            current["codeagent_diagnostics"] = json!({"state": "authoring", "model_requests": 14 + retries,
                "logical_model_requests": 14, "model_retries": retries, "compiler_checks": 0});
            let job = attempt_to_job(&current, &[current.clone()], &ready_builder(), None).unwrap();
            assert_eq!(job["status"], "codeagent-running");
            assert_eq!(job["codeagent_diagnostics"]["model_retries"], retries);
            assert_eq!(job["codeagent_diagnostics"]["compiler_checks"], 0);
            assert_eq!(stage_status(&job, "builder"), "pending");
        }
        current["codeagent_diagnostics"]["model_requests"] = json!(23);
        current["codeagent_diagnostics"]["model_retries"] = json!(9);
        assert!(safe_codeagent_diagnostics(&current).is_none());
    }

    fn diagnostic_budget() -> Value {
        json!({"schema_version": "vibapp.model-request-budget-v1", "phase": "authoring",
            "total_limit": 48, "authoring_limit": 40, "repair_limit": 8,
            "total_used": 30, "authoring_used": 30, "repair_used": 0,
            "total_remaining": 18, "authoring_remaining": 10, "repair_remaining": 8})
    }

    #[test]
    fn phase_budget_is_closed_and_retry_accepts_current_bound() {
        let mut current = attempt("running", "codeagent-running", 20);
        current["codeagent_diagnostics"] = json!({"state": "model-retry", "model_requests": 30,
            "logical_model_requests": 29, "model_retries": 1, "compiler_checks": 0,
            "model_request_budget": diagnostic_budget(),
            "retry": {"attempt": 2, "max_attempts": 3, "http_status": 502, "delay_seconds": 1, "reason": "provider-upstream-unavailable"}});
        assert_eq!(safe_codeagent_diagnostics(&current).unwrap()["model_request_budget"]["repair_remaining"], 8);
        for (key, value) in [("total_used", json!(true)), ("authoring_remaining", json!(9)),
            ("phase", json!("SECRET")), ("total_used", json!(31)), ("extra", json!("SECRET"))] {
            let mut invalid = diagnostic_budget();
            invalid[key] = value;
            current["codeagent_diagnostics"]["model_request_budget"] = invalid;
            assert!(safe_codeagent_diagnostics(&current).is_none());
        }
        let mut repair = diagnostic_budget();
        repair["phase"] = json!("repair");
        repair["authoring_used"] = json!(28);
        repair["authoring_remaining"] = json!(12);
        repair["repair_used"] = json!(2);
        repair["repair_remaining"] = json!(6);
        assert!(safe_model_request_budget(&repair, 30).is_some());
        repair["phase"] = json!("authoring");
        assert!(safe_model_request_budget(&repair, 30).is_none());
    }

    #[test]
    fn closed_exit_observations_preserve_unknown_without_becoming_authority() {
        let observation = json!({"schema_version": "vibapp.docker-failure-diagnostic-v1",
            "failure_origin": "provider-process", "provider_error_category": "stream-decode",
            "child_exit_code": 0, "child_signal": null, "stdout_bytes": 0, "stderr_bytes": 100,
            "bridge_stdout_bytes": null, "bridge_stderr_bytes": null, "output_limit_exceeded": false,
            "frame_limit_exceeded": false, "container_exit_code": null,
            "container_oom_killed": null, "container_running": null});
        let mut current = attempt("failed", "codeagent-failed", 20);
        current["codeagent_diagnostics"] = json!({"state": "failed", "model_requests": 2, "compiler_checks": 0,
            "failure_code": "provider-failed", "failure_diagnostic": observation});
        let safe = safe_codeagent_diagnostics(&current).unwrap();
        assert_eq!(safe["failure_code"], "provider-failed");
        assert_eq!(safe["failure_diagnostic"]["child_exit_code"], 0);
        assert!(safe["failure_diagnostic"]["container_oom_killed"].is_null());
        for (key, value) in [("raw_stderr", json!("SECRET")), ("failure_origin", json!("SECRET")),
            ("child_exit_code", json!(true)), ("container_oom_killed", json!(1)),
            ("stdout_bytes", json!(8_388_609)), ("child_signal", json!("SECRET"))] {
            let mut invalid = observation.clone();
            invalid[key] = value;
            current["codeagent_diagnostics"]["failure_diagnostic"] = invalid;
            let safe = safe_codeagent_diagnostics(&current).unwrap();
            assert!(safe["failure_diagnostic"].is_null());
            assert!(!safe.to_string().contains("SECRET"));
        }
        current["status"] = json!("private-appstore-ready");
        current["codeagent_diagnostics"]["state"] = json!("succeeded");
        current["codeagent_diagnostics"]["failure_diagnostic"] = observation;
        let safe = safe_codeagent_diagnostics(&current).unwrap();
        assert!(safe["failure_diagnostic"].is_null());
        assert!(safe["failure_code"].is_null());
    }

    #[test]
    fn exhausted_upstream_diagnostics_are_closed_and_never_override_success() {
        let mut current = attempt("failed", "codeagent-failed", 20);
        current["codeagent_diagnostics"] = json!({
            "state": "failed", "model_requests": 3, "logical_model_requests": 1,
            "model_retries": 2, "compiler_checks": 0, "failure_code": "provider-upstream-unavailable",
            "upstream_status": 502, "upstream_error": "SECRET /private/path",
            "upstream_request": {"ordinal": 3, "phase": "http-rejected", "bytes": 0, "path": "/private/path", "headers": "SECRET"},
            "retry": {"attempt": 3}
        });
        let job = attempt_to_job(&current, &[current.clone()], &ready_builder(), None).unwrap();
        assert_eq!(job["codeagent_diagnostics"]["failure_code"], "provider-upstream-unavailable");
        assert_eq!(job["codeagent_diagnostics"]["upstream_status"], 502);
        assert!(job["codeagent_diagnostics"]["retry"].is_null());
        assert!(!job.to_string().contains("SECRET"));
        assert!(!job.to_string().contains("/private/path"));
        current["status"] = json!("private-appstore-ready");
        assert!(safe_codeagent_diagnostics(&current).is_none());
    }

    #[test]
    fn legacy_and_forged_retry_diagnostics_fail_closed_without_failing_jobs() {
        let mut current = attempt("running", "codeagent-running", 20);
        assert!(safe_codeagent_diagnostics(&current).is_none());
        current["codeagent_diagnostics"] = json!({"state": "authoring", "model_requests": 64, "compiler_checks": 1});
        let legacy = safe_codeagent_diagnostics(&current).unwrap();
        assert!(legacy["model_retries"].is_null());
        assert!(legacy["logical_model_requests"].is_null());
        for value in [
            json!({"state": [], "model_requests": 1, "compiler_checks": 0}),
            json!({"state": "authoring", "model_requests": true, "compiler_checks": 0}),
            json!({"state": "model-retry", "model_requests": 1, "logical_model_requests": 1, "model_retries": 0, "compiler_checks": 0, "retry": {"attempt": 2, "max_attempts": 3, "http_status": 401, "delay_seconds": 1, "reason": "SECRET"}}),
            json!({"state": "model-retry", "model_requests": 1, "logical_model_requests": 1, "model_retries": 0, "compiler_checks": 0, "retry": {"attempt": 2, "max_attempts": 3, "http_status": 502, "delay_seconds": 31, "reason": "provider-upstream-unavailable"}}),
        ] {
            current["codeagent_diagnostics"] = value;
            let job = attempt_to_job(&current, &[current.clone()], &ready_builder(), None).unwrap();
            assert!(job["codeagent_diagnostics"].is_null());
            assert_eq!(job["status"], "codeagent-running");
            assert!(!job.to_string().contains("SECRET"));
        }
    }

    fn stage_status<'a>(job: &'a Value, kind: &str) -> &'a str {
        job["stages"]
            .as_array()
            .unwrap()
            .iter()
            .find(|stage| stage["kind"] == kind)
            .and_then(|stage| stage["status"].as_str())
            .unwrap()
    }

    #[cfg(not(target_os = "windows"))]
    #[test]
    fn builder_command_carries_the_explicit_input_root() {
        let paths = DeliveryPaths {
            python: PathBuf::from("/not-used/python"),
            controller: PathBuf::from("/not-used/controller"),
            codeagent_adapter: PathBuf::from("/not-used/adapter"),
            cloud_agent: PathBuf::from("/not-used/cloud-agent"),
            root: PathBuf::from("/not-used/root"),
            appstore_root: PathBuf::from("/not-used/appstore"),
        };
        let command = base_command(&paths, &history_codeagent(), Some(&ready_builder())).unwrap();
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        let input_root = arguments
            .iter()
            .position(|value| value == "--builder-input-root")
            .expect("Builder command must carry an explicit trust root");
        assert_eq!(
            arguments.get(input_root + 1).map(String::as_str),
            Some("/accepted")
        );
    }

    #[cfg(not(target_os = "windows"))]
    #[test]
    fn desktop_preflight_command_is_bound_before_worker_execution() {
        let paths = DeliveryPaths {
            python: PathBuf::from("/not-used/python"),
            controller: PathBuf::from("/not-used/controller"),
            codeagent_adapter: PathBuf::from("/not-used/adapter"),
            cloud_agent: PathBuf::from("/not-used/cloud-agent"),
            root: PathBuf::from("/not-used/root"),
            appstore_root: PathBuf::from("/not-used/appstore"),
        };
        let command = preflight_command(&paths, &history_codeagent(), &ready_builder()).unwrap();
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert_eq!(arguments.last().map(String::as_str), Some("preflight"));
        assert!(arguments.iter().any(|value| value == "--cache-acceptance"));
        assert!(arguments.iter().any(|value| value == "--wasm-tools"));
        assert!(
            !arguments
                .iter()
                .any(|value| value == "--acknowledge-external-cost")
        );
        let configured_environment = command
            .get_envs()
            .map(|(name, _)| name.to_string_lossy().into_owned())
            .collect::<BTreeSet<_>>();
        for sensitive in ["HOME", "CODEX_HOME", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"] {
            assert!(!configured_environment.contains(sensitive));
        }
    }

    #[test]
    fn verified_attempt_exposes_app_and_exact_retry_history() {
        let mut first = attempt("failed", "builder-failed", 0);
        first["attempt_id"] = json!("attempt-0001-fedcba9876543210");
        first["attempt_number"] = json!(1);
        first["retry_of_attempt_id"] = Value::Null;
        first["need_spec_revision"] = json!(2);
        first["error"] =
            json!({"stage": "builder", "code": "compile-failed", "message": "bad source"});
        let mut second = attempt("private-appstore-ready", "private-appstore-ready", 100);
        second["outputs"] = json!({
            "app_id": "example.todo",
            "package_digest_sha256": "a".repeat(64),
            "manifest_sha256": "b".repeat(64),
            "digest_equality_proven": true,
            "task_handoff_manifest_binding_proven": true
        });
        let attempts = vec![first, second.clone()];
        let job = attempt_to_job(&second, &attempts, &ready_builder(), None).unwrap();
        assert_eq!(job["status"], "succeeded");
        assert_eq!(job["title"], "测试应用");
        assert_eq!(job["verification"]["status"], "verified");
        assert_eq!(job["outputs"]["app_id"], "example.todo");
        assert_eq!(job["history"].as_array().unwrap().len(), 2);
        assert_eq!(
            job["history"][1]["retry_of_attempt_id"],
            attempts[1]["retry_of_attempt_id"]
        );
    }

    #[test]
    fn legacy_terminal_record_without_exact_binding_fails_closed() {
        let mut legacy = attempt("private-appstore-ready", "private-appstore-ready", 100);
        legacy["outputs"] = json!({
            "app_id": "example.todo",
            "package_digest_sha256": "a".repeat(64)
        });

        let job = attempt_to_job(
            &legacy,
            std::slice::from_ref(&legacy),
            &ready_builder(),
            None,
        )
        .unwrap();

        assert_eq!(job["status"], "failed");
        assert_eq!(job["error"]["code"], "delivery-binding-unproven");
        assert_eq!(job["verification"]["status"], "failed");
        assert_eq!(stage_status(&job, "appstore"), "failed");
    }

    #[test]
    fn missing_builder_is_visible_and_never_claims_success() {
        let queued = attempt("queued", "attempt-admitted", 5);
        let job = attempt_to_job(
            &queued,
            std::slice::from_ref(&queued),
            &BuilderConfiguration::default(),
            None,
        )
        .unwrap();
        assert_eq!(job["status"], "builder-not-configured");
        assert_eq!(job["error"]["code"], "builder-configuration-required");
        assert_eq!(job["verification"]["status"], "builder-not-configured");
        assert_eq!(job["verification"]["independent"], false);
    }

    #[test]
    fn controller_failure_reason_is_preserved() {
        let mut failed = attempt("failed", "codeagent-failed", 0);
        failed["error"] = json!({
            "stage": "codeagent",
            "code": "provider-exit-nonzero",
            "message": "Codex exited with status 1"
        });
        let job = attempt_to_job(
            &failed,
            std::slice::from_ref(&failed),
            &ready_builder(),
            None,
        )
        .unwrap();
        assert_eq!(job["status"], "failed");
        assert_eq!(job["error"]["code"], "provider-exit-nonzero");
        assert_eq!(job["verification"]["summary"], "Codex exited with status 1");
    }

    #[test]
    fn worker_timeout_is_a_retryable_failed_projection_not_running() {
        let running = attempt("codeagent-running", "codeagent", 30);
        let worker = json!({
            "schema_version": WORKER_STATUS_SCHEMA,
            "status": "worker-timeout",
            "error": "delivery-controller-timeout: process tree was cleaned up",
            "external_cost_acknowledged": true,
            "immutable_task_digest_sha256": "a".repeat(64),
            "provider_id": "codex",
            "task_provider": "openai-codex",
            "model": "gpt-5.6-sol"
        });
        let job = attempt_to_job(
            &running,
            std::slice::from_ref(&running),
            &ready_builder(),
            Some(&worker),
        )
        .unwrap();
        assert_eq!(job["status"], "delivery-worker-failed");
        assert_eq!(job["verification"]["status"], "failed");
        assert_eq!(job["error"]["code"], "worker-timeout");
        assert_eq!(job["error"]["retryable_with_new_attempt"], true);
    }

    #[test]
    fn builder_failure_keeps_durable_codeagent_stage_succeeded() {
        let mut failed = attempt("failed", "builder-failed", 0);
        failed["error"] = json!({
            "stage": "builder",
            "code": "process-failed",
            "message": "generated source did not compile"
        });
        failed["outputs"] = json!({
            "source_handoff_path": "tasks/development-1/attempts/attempt-1/codeagent/output/handoff.json",
            "source_handoff_sha256": "a".repeat(64),
            "codeagent_provider_id": "codex",
            "codeagent_model": "gpt-5.6-sol"
        });

        let job = attempt_to_job(
            &failed,
            std::slice::from_ref(&failed),
            &ready_builder(),
            None,
        )
        .unwrap();

        assert_eq!(stage_status(&job, "codeagent"), "succeeded");
        assert_eq!(stage_status(&job, "builder"), "failed");
        assert_eq!(stage_status(&job, "verifier"), "pending");
    }

    #[test]
    fn verifier_failure_keeps_durable_codeagent_and_builder_stages_succeeded() {
        let mut failed = attempt("failed", "verifier-failed", 0);
        failed["error"] = json!({
            "stage": "verifier",
            "code": "verification-failed",
            "message": "candidate did not pass independent verification"
        });
        failed["outputs"] = json!({
            "source_handoff_path": "tasks/development-1/attempts/attempt-1/codeagent/output/handoff.json",
            "source_handoff_sha256": "a".repeat(64),
            "builder_receipt_path": "tasks/development-1/attempts/attempt-1/builder/quarantine-receipt.json",
            "builder_receipt_sha256": "b".repeat(64)
        });

        let job = attempt_to_job(
            &failed,
            std::slice::from_ref(&failed),
            &ready_builder(),
            None,
        )
        .unwrap();

        assert_eq!(stage_status(&job, "codeagent"), "succeeded");
        assert_eq!(stage_status(&job, "builder"), "succeeded");
        assert_eq!(stage_status(&job, "verifier"), "failed");
    }

    #[test]
    fn builder_preflight_failure_without_handoff_does_not_claim_codeagent_success() {
        let mut failed = attempt("failed", "builder-failed", 0);
        failed["error"] = json!({
            "stage": "builder",
            "code": "builder-preflight-failed",
            "message": "Builder preflight failed before CodeAgent"
        });
        failed["outputs"] = json!({
            "external_request_attempted": false,
            "external_request_observed": false,
            "source_executed": false
        });

        let job = attempt_to_job(
            &failed,
            std::slice::from_ref(&failed),
            &ready_builder(),
            None,
        )
        .unwrap();

        assert_eq!(stage_status(&job, "codeagent"), "pending");
        assert_eq!(stage_status(&job, "builder"), "failed");
    }

    #[test]
    fn progress_without_durable_outputs_never_marks_stages_succeeded() {
        let running = attempt("codeagent-running", "verifier", 99);

        let job = attempt_to_job(
            &running,
            std::slice::from_ref(&running),
            &ready_builder(),
            None,
        )
        .unwrap();

        assert_eq!(job["progress_percent"], 99);
        assert_eq!(stage_status(&job, "codeagent"), "pending");
        assert_eq!(stage_status(&job, "builder"), "pending");
        assert_eq!(stage_status(&job, "verifier"), "pending");
        assert_eq!(stage_status(&job, "appstore"), "pending");
        assert_eq!(job["verification"]["independent"], false);
    }

    #[test]
    fn delivery_history_wins_over_duplicate_legacy_job() {
        let delivery = vec![json!({"job_id": "same", "route": "delivery"})];
        let legacy = vec![
            json!({"job_id": "same", "route": "legacy"}),
            json!({"job_id": "legacy-only", "route": "legacy"}),
        ];
        let merged = merge_with_legacy(delivery, legacy);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0]["route"], "delivery");
        assert_eq!(merged[1]["job_id"], "legacy-only");
    }

    fn archive_reply() -> Value {
        json!({"schema_version": "vibapp.delivery-task-archive-list-v1", "archived": [{
            "task_id": "development-0123456789abcdef0123456789abcdef", "job_id": "job-archived",
            "immutable_task_digest_sha256": "a".repeat(64),
            "provider_execution_identity_sha256": "b".repeat(64), "consent_id": "consent-old"
        }]})
    }

    fn archived_legacy_failure() -> Value {
        json!({"job_id": "job-archived", "status": "codeagent-failed", "queue_receipt": {
            "immutable_task_digest_sha256": "a".repeat(64),
            "provider_execution_identity_sha256": "b".repeat(64), "consent_id": "consent-old"
        }})
    }

    #[test]
    fn archive_projection_is_closed_and_invalid_evidence_never_hides_tasks() {
        let valid = archive_reply();
        assert_eq!(parse_archived_task_ids(&serde_json::to_vec(&valid).unwrap()).unwrap().tasks.len(), 1);
        let mut unknown = valid.clone(); unknown["override"] = json!(true);
        let mut row_unknown = valid.clone(); row_unknown["archived"][0]["raw_path"] = json!("/private");
        let mut duplicate = valid.clone(); duplicate["archived"].as_array_mut().unwrap().push(valid["archived"][0].clone());
        let mut invalid_id = valid.clone(); invalid_id["archived"][0]["task_id"] = json!("../task");
        let mut invalid_job = valid.clone(); invalid_job["archived"][0]["job_id"] = json!("job/private");
        let mut oversized = valid.clone(); oversized["archived"] = json!(vec![valid["archived"][0].clone(); MAX_ARCHIVED_TASKS + 1]);
        for value in [unknown, row_unknown, duplicate, invalid_id, invalid_job, oversized,
            json!({"schema_version": "other", "archived": []}),
            json!({"schema_version": "vibapp.delivery-task-archive-list-v1", "archived": null})] {
            let archived = parse_archived_task_ids(&serde_json::to_vec(&value).unwrap());
            assert!(archived.is_none());
            assert_eq!(without_archived_jobs(vec![json!({"job_id": "job-archived"})], &archived.unwrap_or_default()).len(), 1);
        }
        assert!(parse_archived_task_ids(br#"{"schema_version":"wrong","schema_version":"vibapp.delivery-task-archive-list-v1","archived":[]}"#).is_none());
        assert!(parse_archived_task_ids(&vec![b' '; MAX_REPLY_BYTES + 1]).is_none());
    }

    #[test]
    fn archived_tasks_do_not_reappear_through_legacy_merge_or_hide_new_exact_tasks() {
        let archived = parse_archived_task_ids(&serde_json::to_vec(&archive_reply()).unwrap()).unwrap();
        let delivery = vec![
            json!({"job_id": "job-archived", "task_id": "development-0123456789abcdef0123456789abcdef", "status": "failed"}),
            json!({"job_id": "job-ready", "task_id": "development-11111111111111111111111111111111", "status": "succeeded"}),
        ];
        let legacy = vec![archived_legacy_failure(), json!({"job_id": "legacy-kept"})];
        let merged = without_archived_jobs(merge_with_legacy(delivery, legacy.clone()), &archived);
        assert_eq!(merged.iter().map(|job| job["job_id"].as_str().unwrap()).collect::<Vec<_>>(), ["job-ready", "legacy-kept"]);
        // Early task filtering removes the delivery row; its legacy twin must
        // still be removed by the second, post-merge check.
        assert_eq!(without_archived_jobs(merge_with_legacy(Vec::new(), legacy), &archived), vec![json!({"job_id": "legacy-kept"})]);
        let current = json!({"job_id": "job-archived", "task_id": "development-22222222222222222222222222222222", "status": "running"});
        assert_eq!(without_archived_jobs(vec![current.clone()], &archived), vec![current]);
    }

    #[test]
    fn archive_ids_never_hide_successful_active_or_ambiguous_feed_rows() {
        let archived = parse_archived_task_ids(&serde_json::to_vec(&archive_reply()).unwrap()).unwrap();
        for key in ["immutable_task_digest_sha256", "provider_execution_identity_sha256", "consent_id"] {
            let mut changed = archived_legacy_failure();
            changed["queue_receipt"][key] = json!("changed");
            assert_eq!(without_archived_jobs(vec![changed.clone()], &archived), vec![changed]);
        }
        let unbound = json!({"job_id": "job-archived", "status": "codeagent-failed"});
        assert_eq!(without_archived_jobs(vec![unbound.clone()], &archived), vec![unbound]);
        for task_id in [Value::Null, json!("development-0123456789abcdef0123456789abcdef")] {
            for status in [Value::Null, json!("succeeded"), json!("source-ready"), json!("legacy-state-unproven"), json!("private-appstore-ready"),
                json!("running"), json!("queued"), json!("codeagent-running"), json!("unknown")] {
                let row = json!({"job_id": "job-archived", "task_id": task_id, "status": status});
                assert_eq!(without_archived_jobs(vec![row.clone()], &archived), vec![row]);
            }
            for evidence in [json!({"verification": {"status": "verified"}}),
                json!({"outputs": {"app_id": "ai.test"}}),
                json!({"outputs": {"appstore_candidate_path": "/candidate"}}),
                json!({"outputs": {"appstore_created": true}})] {
                let mut row = json!({"job_id": "job-archived", "task_id": task_id, "status": "failed"});
                row.as_object_mut().unwrap().extend(evidence.as_object().unwrap().clone());
                assert_eq!(without_archived_jobs(vec![row.clone()], &archived), vec![row]);
            }
        }
    }

    #[test]
    fn archive_filter_applies_before_the_visible_task_limit() {
        let suffix = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("vibapp-archive-limit-{}-{suffix:x}", std::process::id()));
        for ordinal in 0..=MAX_TASKS {
            fs::create_dir_all(root.join("tasks").join(format!("development-{ordinal:032x}"))).unwrap();
        }
        let mut archived = ArchivedTaskIds::default();
        archived.tasks.insert(format!("development-{:032x}", MAX_TASKS));
        let visible = task_ids(&root, &archived);
        assert_eq!(visible.len(), MAX_TASKS);
        assert!(visible.contains(&format!("development-{:032x}", 0)));
        assert!(!visible.contains(&format!("development-{:032x}", MAX_TASKS)));
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn archive_reader_uses_only_the_bounded_read_only_helper_interface() {
        let suffix = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("vibapp-archive-reader-{}-{suffix:x}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let paths = DeliveryPaths { python: PathBuf::from("/usr/bin/python3"), controller: root.join("delivery_controller.py"),
            codeagent_adapter: root.join("unused-adapter"), cloud_agent: root.join("unused-cloud-agent"),
            root: root.join("delivery-controller"), appstore_root: root.join("local-appstore") };
        let command = archive_list_command(&paths, &root).unwrap();
        let args = command.get_args().map(|arg| arg.to_string_lossy().into_owned()).collect::<Vec<_>>();
        assert_eq!(args, vec!["-I".to_string(), "-B".to_string(), root.join("task_archive.py").to_string_lossy().into_owned(),
            "--data-root".to_string(), root.to_string_lossy().into_owned(), "--archived-only".to_string()]);
        assert!(!command.get_envs().any(|(key, _)| ["HOME", "CODEX_HOME", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"].iter().any(|name| key == *name)));
        let program = format!("import json, os, sys\nassert sys.argv[1:] == ['--data-root', {}, '--archived-only']\nassert 'CODEX_HOME' not in os.environ\nprint({})\n",
            serde_json::to_string(&root.to_string_lossy()).unwrap(), serde_json::to_string(&archive_reply().to_string()).unwrap());
        fs::write(root.join("task_archive.py"), program).unwrap();
        let archived = read_archive_list(command, Duration::from_secs(2)).unwrap();
        assert!(archived.jobs.contains(&("job-archived".into(), "a".repeat(64), "b".repeat(64), "consent-old".into())));
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn failed_or_slow_archive_helpers_leave_rows_visible() {
        for (program, timeout) in [
            ("import sys; sys.exit(1)", Duration::from_secs(2)),
            ("print('not JSON')", Duration::from_secs(2)),
            ("print('x' * 524289)", Duration::from_secs(2)),
            ("import time; time.sleep(10)", Duration::from_millis(30)),
        ] {
            let archived = read_archive_list(synthetic_python(program), timeout);
            assert!(archived.is_none());
            let jobs = vec![json!({"job_id": "job-archived", "status": "failed"})];
            assert_eq!(without_archived_jobs(jobs.clone(), &archived.unwrap_or_default()), jobs);
        }
    }

    #[test]
    fn active_worker_guard_cleans_the_process_local_dedup_key() {
        let key = format!("guard-test-{}", worker_session_id());
        let active = ACTIVE_WORKERS.get_or_init(|| Mutex::new(BTreeSet::new()));
        active.lock().unwrap().insert(key.clone());
        {
            let _guard = ActiveWorkerGuard::new(key.clone());
            assert!(active.lock().unwrap().contains(&key));
        }
        assert!(!active.lock().unwrap().contains(&key));
    }

    #[test]
    fn every_delivery_execution_entrypoint_rejects_missing_cost_authority() {
        let paths = DeliveryPaths {
            python: PathBuf::from("/not-used/python"),
            controller: PathBuf::from("/not-used/controller"),
            codeagent_adapter: PathBuf::from("/not-used/adapter"),
            cloud_agent: PathBuf::from("/not-used/cloud-agent"),
            root: PathBuf::from("/not-used/root"),
            appstore_root: PathBuf::from("/not-used/appstore"),
        };
        let codeagent = history_codeagent();
        let task_id = "development-0123456789abcdef0123456789abcdef";
        let attempt_id = "attempt-0001-0123456789abcdef";

        assert_eq!(
            spawn_worker(
                paths,
                codeagent.clone(),
                ready_builder(),
                task_id.to_string(),
                attempt_id.to_string(),
                "a".repeat(64),
                false,
                Instant::now() + PIPELINE_TIMEOUT,
            )
            .unwrap_err(),
            EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED
        );
        assert_eq!(
            submit_and_start(
                Path::new("/not-used/data"),
                &json!({}),
                &json!({}),
                true,
                false,
                None,
                &codeagent,
            )
            .unwrap_err(),
            EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED
        );
        assert_eq!(
            run(
                Path::new("/not-used/data"),
                task_id,
                Some(attempt_id),
                false,
                &codeagent,
            )
            .unwrap_err(),
            EXTERNAL_COST_ACKNOWLEDGEMENT_REQUIRED
        );
    }

    #[test]
    fn resume_requires_persisted_cost_authority() {
        let mut authorized = attempt("queued", "attempt-admitted", 5);
        authorized["status"] = json!("codeagent-running");
        let job = attempt_to_job(
            &authorized,
            std::slice::from_ref(&authorized),
            &ready_builder(),
            Some(&json!({
                "schema_version": WORKER_STATUS_SCHEMA,
                "status": "starting",
                "external_cost_acknowledged": true,
                "immutable_task_digest_sha256": "a".repeat(64),
                "provider_id": "claude-code",
                "task_provider": "anthropic-claude-code",
                "model": "claude-synthetic"
            })),
        )
        .unwrap();
        assert_eq!(job["delivery_worker"]["external_cost_acknowledged"], true);
        assert_eq!(job["provider_id"], "claude-code");
        let recovered = persisted_codeagent(&job["delivery_worker"], &"a".repeat(64)).unwrap();
        assert_eq!(recovered.provider_id, "claude-code");
        assert_eq!(recovered.task_provider, "anthropic-claude-code");
        assert_eq!(recovered.model, "claude-synthetic");

        let mut legacy = job.clone();
        legacy["delivery_worker"] = json!({"status": "starting"});
        assert_ne!(
            legacy["delivery_worker"]["external_cost_acknowledged"],
            true
        );
        assert!(persisted_codeagent(&legacy["delivery_worker"], &"a".repeat(64)).is_none());

        let mut mismatched = job["delivery_worker"].clone();
        mismatched["task_provider"] = json!("openai-codex");
        assert!(persisted_codeagent(&mismatched, &"a".repeat(64)).is_none());
    }

    #[test]
    fn resume_binding_stays_fail_closed_when_local_execution_is_paused() {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("delivery-task-binding-{suffix:x}"));
        let task_id = "development-0123456789abcdef0123456789abcdef";
        let attempt_id = "attempt-0001-0123456789abcdef";
        let digest = "b".repeat(64);
        let input = root
            .join("tasks")
            .join(task_id)
            .join("attempts")
            .join(attempt_id)
            .join("input");
        fs::create_dir_all(&input).unwrap();
        fs::write(
            input.join("task.json"),
            serde_json::to_vec(&json!({
                "provider": "anthropic-claude-code",
                "model": "claude-authorized",
                "consent": {
                    "provider": "anthropic-claude-code",
                    "model": "claude-authorized"
                },
                "immutable_task_digest_sha256": digest
            }))
            .unwrap(),
        )
        .unwrap();
        let paths = DeliveryPaths {
            python: PathBuf::from("/not-used/python"),
            controller: PathBuf::from("/not-used/controller"),
            codeagent_adapter: PathBuf::from("/not-used/adapter"),
            cloud_agent: PathBuf::from("/not-used/cloud-agent"),
            root,
            appstore_root: PathBuf::from("/not-used/appstore"),
        };
        assert!(task_bound_codeagent(&paths, task_id, attempt_id, &digest).is_none());
        assert!(task_bound_codeagent(&paths, task_id, attempt_id, &"c".repeat(64)).is_none());
        fs::remove_dir_all(&paths.root).unwrap();
    }

    #[test]
    fn attempt_id_validation_is_exact_and_lowercase() {
        assert!(valid_attempt_id("attempt-0001-0123456789abcdef"));
        for invalid in [
            "attempt-1-0123456789abcdef",
            "attempt-0001-0123456789ABCDEf",
            "attempt-0001-0123456789abcdef/child",
            "attempt-0001-0123456789abcdef-extra",
            "assessment-attempt-0001-0123456789abcdef",
        ] {
            assert!(!valid_attempt_id(invalid), "accepted {invalid}");
        }
        let mut malformed = attempt("queued", "attempt-admitted", 5);
        malformed["attempt_id"] = json!("attempt-1-0123456789abcdef");
        assert!(
            attempt_to_job(
                &malformed,
                std::slice::from_ref(&malformed),
                &ready_builder(),
                None,
            )
            .is_none()
        );
    }

    #[test]
    fn edited_task_is_never_classified_as_same_task_retry() {
        let task = json!({
            "need_spec": {"need_id": "need-01234567"},
            "immutable_task_digest_sha256": "a".repeat(64)
        });
        let computed = computed_delivery_task_id(&task).unwrap();
        assert_eq!(computed, "development-2fcc4ca6e1d932d9d099ef9bad8a3e8f");
        let (retry, edited) = classify_previous_task(&task, Some(&computed)).unwrap();
        assert_eq!(retry, Some(computed.as_str()));
        assert_eq!(edited, None);

        let prior = "development-fedcba9876543210fedcba9876543210";
        assert_ne!(prior, computed);
        let (retry, edited) = classify_previous_task(&task, Some(prior)).unwrap();
        assert_eq!(retry, None);
        assert_eq!(edited, Some(prior));
        assert!(
            classify_previous_task(&task, Some("development-ABCDEF0123456789abcdef0123456789"))
                .is_err()
        );
    }

    #[test]
    fn outer_budget_covers_the_additive_stage_maximum() {
        assert_eq!(
            PIPELINE_TIMEOUT.as_secs(),
            CODEAGENT_STAGE_BUDGET_SECONDS
                + BUILDER_STAGE_BUDGET_SECONDS
                + VERIFIER_STAGE_BUDGET_SECONDS
                + ORCHESTRATION_BUDGET_SECONDS
        );
        assert_eq!(PIPELINE_TIMEOUT.as_secs(), 2700);
    }

    #[test]
    fn cleanup_confirmation_is_fail_closed() {
        assert!(controller_cleanup_confirmed(
            "delivery-controller-timeout: 受控进程树已清空。"
        ));
        assert!(controller_cleanup_confirmed(
            "provider-failed: controller returned after its tree was observed empty"
        ));
        for error in [
            "descendant-census-best-effort unavailable: ps failed",
            "cleanup failed: SIGKILL 后仍未清空",
            "process-tree-containment-unavailable",
            "自动交付输出在进程树清理后仍未关闭",
        ] {
            let tagged = cleanup_unconfirmed(error);
            assert!(!controller_cleanup_confirmed(&tagged), "accepted {tagged}");
        }
    }

    #[test]
    fn joined_worker_requires_durable_cleanup_confirmation() {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "vibapp-worker-confirmation-{}-{suffix:x}",
            std::process::id()
        ));
        let status_path = root.join("worker.json");
        let task_id = "development-0123456789abcdef0123456789abcdef";
        let attempt_id = "attempt-0001-0123456789abcdef";
        write_worker_status(
            &status_path,
            &json!({
                "schema_version": WORKER_STATUS_SCHEMA,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": "worker-failed",
                "terminal": true,
                "cleanup_confirmed": false,
                "error": "controller cleanup failed"
            }),
        )
        .unwrap();
        let error =
            confirm_joined_worker_status(&status_path, task_id, attempt_id, None).unwrap_err();
        assert!(error.starts_with(WORKER_JOINED_CLEANUP_UNCONFIRMED));
        let failed: Value = serde_json::from_slice(&fs::read(&status_path).unwrap()).unwrap();
        assert_eq!(failed["status"], "worker-cancellation-unconfirmed");
        assert_eq!(failed["cleanup_confirmed"], false);
        assert_eq!(
            failed["controller_cleanup_error"],
            "controller cleanup failed"
        );

        let mut confirmed = failed;
        confirmed["status"] = json!("worker-cancelled");
        confirmed["cleanup_confirmed"] = json!(true);
        write_worker_status(&status_path, &confirmed).unwrap();
        confirm_joined_worker_status(
            &status_path,
            task_id,
            attempt_id,
            Some("bridge lifetime expired"),
        )
        .unwrap();
        let annotated: Value = serde_json::from_slice(&fs::read(&status_path).unwrap()).unwrap();
        assert_eq!(
            annotated["owner_cancellation_reason"],
            "bridge lifetime expired"
        );
        let mut mismatched = annotated;
        mismatched["attempt_id"] = json!("attempt-0002-fedcba9876543210");
        write_worker_status(&status_path, &mismatched).unwrap();
        let error =
            confirm_joined_worker_status(&status_path, task_id, attempt_id, None).unwrap_err();
        assert!(error.contains("精确 task/attempt 绑定不一致"));
        let rebound: Value = serde_json::from_slice(&fs::read(&status_path).unwrap()).unwrap();
        assert_eq!(rebound["task_id"], task_id);
        assert_eq!(rebound["attempt_id"], attempt_id);
        assert_eq!(rebound["cleanup_confirmed"], false);
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    fn synthetic_python(program: &str) -> Command {
        let mut command = Command::new("/usr/bin/python3");
        command
            .args(["-X", "utf8"]).arg("-I")
            .arg("-B")
            .arg("-c")
            .arg(program)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("LANG", "C")
            .env("LC_ALL", "C")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        command
    }

    #[cfg(unix)]
    #[test]
    fn admission_wait_retries_same_attempt_with_original_deadline() {
        let deadline = Instant::now() + Duration::from_secs(10);
        let cancel = AtomicBool::new(false);
        let mut calls = 0;
        let mut confirmations = 0;
        let result = wait_for_admission(deadline, ADMISSION_WAIT_TIMEOUT, &cancel, |observed| {
            assert_eq!(observed, deadline);
            calls += 1;
            let program = if calls == 1 {
                "import sys; print('{\"ok\":false,\"stage\":\"admission\",\"code\":\"local-capacity-busy\",\"message\":\"occupied\"}', file=sys.stderr); sys.exit(1)"
            } else {
                "print('{\"ok\":true,\"result\":{\"task_id\":\"same-task\",\"attempt_id\":\"same-attempt\"}}')"
            };
            execute_until(synthetic_python(program), observed, Some(&cancel))
        }, |observed| {
            assert!(observed <= deadline);
            confirmations += 1;
            Ok(())
        }).unwrap();
        assert_eq!(calls, 2);
        assert_eq!(confirmations, 1);
        assert_eq!(result["attempt_id"], "same-attempt");
    }

    #[test]
    fn admission_wait_cancellation_stops_before_second_controller() {
        let cancel = Arc::new(AtomicBool::new(false));
        let request = Arc::clone(&cancel);
        let trigger = thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            request.store(true, Ordering::Release);
        });
        let started = Instant::now();
        let mut calls = 0;
        let error = wait_for_admission(Instant::now() + Duration::from_secs(5), ADMISSION_WAIT_TIMEOUT,
            &cancel, |_| { calls += 1; Err::<(), _>("local-capacity-busy: occupied".to_string()) },
            |_| Ok(())).unwrap_err();
        trigger.join().unwrap();
        assert!(error.starts_with("delivery-controller-cancelled:"));
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(calls, 1);
    }

    #[test]
    fn admission_wait_caps_queue_and_never_renews_pipeline_deadline() {
        for (pipeline, queue) in [(40, 5000), (5000, 40)] {
            let mut calls = 0;
            let started = Instant::now();
            let deadline = started + Duration::from_millis(pipeline);
            let error = wait_for_admission(deadline, Duration::from_millis(queue), &AtomicBool::new(false),
                |observed| { assert_eq!(observed, deadline); calls += 1;
                    Err::<(), _>("local-capacity-busy: occupied".to_string()) }, |_| Ok(())).unwrap_err();
            assert!(error.starts_with("delivery-controller-timeout:"));
            assert!(started.elapsed() < Duration::from_secs(1));
            assert_eq!(calls, 1);
        }
    }

    #[test]
    fn admission_wait_requires_exact_queued_unspent_evidence() {
        let current = attempt("queued", "attempt-admitted", 5);
        let task_id = current["task_id"].as_str().unwrap();
        let attempt_id = current["attempt_id"].as_str().unwrap();
        let digest = "a".repeat(64);
        let status = json!({"task": {"task_id": task_id, "current_attempt_id": attempt_id}, "attempt": current});
        let suffix = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("vibapp-admission-{}-{suffix:x}", std::process::id()));
        let directory = root.join("tasks").join(task_id).join("attempts").join(attempt_id);
        fs::create_dir_all(&directory).unwrap();
        let verify = |status: &Value| confirm_queued_unspent(&root, status, task_id, attempt_id, &digest);
        assert!(verify(&status).is_ok());
        for (field, value) in [("status", json!("running")), ("outputs", json!({"source_handoff_path": "source"})),
                               ("immutable_task_digest_sha256", json!("b".repeat(64))), ("attempt_id", json!("another"))] {
            let mut changed = status.clone(); changed["attempt"][field] = value;
            assert!(verify(&changed).is_err());
        }
        let mut changed = status.clone(); changed["task"]["current_attempt_id"] = json!("another");
        assert!(verify(&changed).is_err());
        let agent = directory.join("codeagent");
        fs::create_dir(&agent).unwrap();
        fs::write(agent.join("status.json"), b"{}").unwrap();
        assert!(verify(&status).is_err());
        fs::remove_file(agent.join("status.json")).unwrap();
        let claims = agent.join("output/consents");
        fs::create_dir_all(&claims).unwrap();
        assert!(verify(&status).is_ok());
        fs::write(claims.join("consumed.json"), b"{}").unwrap();
        assert!(verify(&status).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn admission_wait_never_retries_other_failures_or_expired_consent() {
        for error in ["provider-timeout: failed", "consent-expired: expired", "admission-retry-refused: spent",
                      "local-capacity-busy: delivery-controller-cleanup-unconfirmed"] {
            let mut calls = 0;
            let observed = wait_for_admission(Instant::now() + Duration::from_secs(5), ADMISSION_WAIT_TIMEOUT,
                &AtomicBool::new(false), |_| { calls += 1; Err::<(), _>(error.to_string()) },
                |_| panic!("non-admission failure must not be retried")).unwrap_err();
            assert_eq!(observed, error); assert_eq!(calls, 1);
        }
        let mut calls = 0;
        let error = wait_for_admission(Instant::now() + Duration::from_secs(5), ADMISSION_WAIT_TIMEOUT,
            &AtomicBool::new(false), |_| { calls += 1; Err::<(), _>("local-capacity-busy: occupied".to_string()) },
            |_| Err("consent-expired: explicit new consent required".to_string())).unwrap_err();
        assert!(error.starts_with("consent-expired:")); assert_eq!(calls, 1);
    }

    #[cfg(unix)]
    #[test]
    fn timeout_kills_stubborn_child_and_grandchild_group() {
        let program = r#"
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    grandchild = os.fork()
    if grandchild == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True: time.sleep(1)
    while True: time.sleep(1)
while True: time.sleep(1)
"#;
        let child = synthetic_python(program)
            .spawn()
            .expect("spawn synthetic tree");
        let process_group = i32::try_from(child.id()).unwrap();
        let error =
            wait_bounded(child, Instant::now() + Duration::from_millis(250), None).unwrap_err();
        assert!(error.starts_with("delivery-controller-timeout:"), "{error}");
        assert!(
            process_table()
                .unwrap()
                .iter()
                .all(|record| record.pgid != process_group),
            "synthetic process group survived timeout cleanup"
        );
    }

    #[cfg(unix)]
    #[test]
    fn observed_setsid_descendant_is_killed_after_leader_exit() {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let pid_path = std::env::temp_dir().join(format!(
            "vibapp-delivery-setsid-child-{}-{suffix:x}.pid",
            std::process::id()
        ));
        let program = r#"
import os, signal, sys, time
pid_path = sys.argv[1]
child = os.fork()
if child == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(pid_path, "w", encoding="ascii") as handle:
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
    while True: time.sleep(1)
while not os.path.exists(pid_path): time.sleep(0.01)
time.sleep(0.25)
os._exit(0)
"#;
        let mut command = synthetic_python(program);
        command.arg(&pid_path);
        let child = command.spawn().expect("spawn leader-exit tree");
        let error = wait_bounded_with_census_interval(
            child,
            Instant::now() + Duration::from_secs(5),
            None,
            Duration::from_millis(50),
        )
        .unwrap_err();
        assert!(
            error.starts_with("delivery-controller-descendants-survived:"),
            "{error}"
        );
        let escaped_pid = fs::read_to_string(&pid_path)
            .unwrap()
            .parse::<i32>()
            .unwrap();
        assert!(
            process_table()
                .unwrap()
                .iter()
                .all(|record| record.pid != escaped_pid),
            "observed setsid descendant survived leader cleanup"
        );
        fs::remove_file(pid_path).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn cancellation_kills_and_confirms_the_controller_group() {
        let program =
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)";
        let child = synthetic_python(program)
            .spawn()
            .expect("spawn cancellable child");
        let cancel = Arc::new(AtomicBool::new(false));
        let request = Arc::clone(&cancel);
        let trigger = thread::spawn(move || {
            thread::sleep(Duration::from_millis(100));
            request.store(true, Ordering::Release);
        });
        let error = wait_bounded(
            child,
            Instant::now() + Duration::from_secs(5),
            Some(cancel.as_ref()),
        )
        .unwrap_err();
        trigger.join().unwrap();
        assert!(
            error.starts_with("delivery-controller-cancelled:"),
            "{error}"
        );
    }
}
