use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::HashSet;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const REQUEST_SCHEMA: &str = "vibapp.roomhash-host-request.experimental-v1";
const RESPONSE_SCHEMA: &str = "vibapp.roomhash-host-response.experimental-v1";
const MAX_LINE_BYTES: usize = 512 * 1024;
const READY_TIMEOUT: Duration = Duration::from_secs(5);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
const SEED_TIMEOUT: Duration = Duration::from_secs(60);
const EXIT_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_LOCATOR_BYTES: usize = 256 * 1024;
const PACKAGE_LOCATOR_SCHEMA: &str = "vibapp.roomhash-package-locator.experimental-v1";
const PACKAGE_LOCATOR_TRANSPORT: &str = "bittorrent-v1";
const PACKAGE_LOCATOR_TRUST_NOTE: &str = "locator-only-package-bytes-require-vibapp-verification";
const PACKAGE_LOCATOR_TRACKERS: &[&str] = &[
    "wss://tracker.webtorrent.dev",
    "wss://tracker.openwebtorrent.com",
    "wss://tracker.btorrent.xyz",
];
const MAX_PACKAGE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_PACKAGE_FILES: usize = 128;
const COLLABORATION_GRANT_SCHEMA: &str = "vibapp.collaboration-grant.experimental-v1";
const MAX_COLLABORATION_MESSAGE_BYTES: usize = 64 * 1024;
const MAX_COLLABORATION_TTL_MS: u64 = 24 * 60 * 60 * 1000;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PackageLocatorFile {
    path: String,
    sha256: String,
    size_bytes: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PackageLocatorDocument {
    schema_version: String,
    package_digest_sha256: String,
    transport: String,
    info_hash: String,
    magnet_uri: String,
    size_bytes: u64,
    files: Vec<PackageLocatorFile>,
    trust_note: String,
}

#[derive(Clone)]
pub struct RoomHashTurnConfig {
    urls: Vec<String>,
    username: String,
    credential: String,
}

#[derive(Clone)]
pub struct RoomHashConfig {
    upload_limit_bps: u64,
    download_limit_bps: u64,
    max_conns: u64,
    dht: bool,
    lsd: bool,
    pex: bool,
    tracker_urls: Vec<String>,
    turn: Option<RoomHashTurnConfig>,
}

impl RoomHashConfig {
    pub fn try_from_value(value: &Value) -> Result<Self, String> {
        let object = exact_object(
            value.clone(),
            &[
                "upload_limit_bps",
                "download_limit_bps",
                "max_conns",
                "dht",
                "lsd",
                "pex",
                "tracker_urls",
                "turn",
            ],
            "RoomHash configuration",
        )?;
        let upload_limit_bps = unsigned(&object, "upload_limit_bps")?;
        let download_limit_bps = unsigned(&object, "download_limit_bps")?;
        let max_conns = unsigned(&object, "max_conns")?;
        if upload_limit_bps > 1_073_741_824 || download_limit_bps > 1_073_741_824 {
            return Err("RoomHash bandwidth limit exceeds the supported maximum.".into());
        }
        if !(1..=128).contains(&max_conns) {
            return Err("RoomHash max_conns must be between 1 and 128.".into());
        }
        let tracker_urls = string_array(&object, "tracker_urls", 16, 2_048)?;
        for url in &tracker_urls {
            validate_url(url, &["wss://"], "tracker URL")?;
        }
        let turn = match object.get("turn") {
            Some(Value::Null) => None,
            Some(value) => {
                let turn = exact_object(
                    value.clone(),
                    &["urls", "username", "credential"],
                    "TURN configuration",
                )?;
                let urls = string_array(&turn, "urls", 8, 2_048)?;
                if urls.is_empty() {
                    return Err("TURN urls must not be empty.".into());
                }
                for url in &urls {
                    validate_url(url, &["turn:", "turns:"], "TURN URL")?;
                }
                let username = bounded_string(&turn, "username", 256, false)?;
                let credential = bounded_string(&turn, "credential", 512, false)?;
                Some(RoomHashTurnConfig {
                    urls,
                    username,
                    credential,
                })
            }
            None => return Err("RoomHash configuration is missing turn.".into()),
        };
        Ok(Self {
            upload_limit_bps,
            download_limit_bps,
            max_conns,
            dht: boolean(&object, "dht")?,
            lsd: boolean(&object, "lsd")?,
            pex: boolean(&object, "pex")?,
            tracker_urls,
            turn,
        })
    }

    fn to_value(&self) -> Value {
        let turn = self.turn.as_ref().map_or(Value::Null, |turn| {
            json!({
                "urls": turn.urls,
                "username": turn.username,
                "credential": turn.credential,
            })
        });
        json!({
            "upload_limit_bps": self.upload_limit_bps,
            "download_limit_bps": self.download_limit_bps,
            "max_conns": self.max_conns,
            "dht": self.dht,
            "lsd": self.lsd,
            "pex": self.pex,
            "tracker_urls": self.tracker_urls,
            "turn": turn,
        })
    }
}

pub struct RoomHashHost {
    child: Child,
    stdin: Option<ChildStdin>,
    responses: Receiver<Result<Value, String>>,
    next_request_id: u64,
    stopped: bool,
}

impl RoomHashHost {
    pub fn spawn(app_data_root: &Path) -> Result<Self, String> {
        let app_data_root = private_directory(app_data_root)?;
        let data_dir = private_directory(&app_data_root.join("roomhash"))?;
        let seed_root = private_directory(&data_dir.join("seedable"))?;
        let appstore_seed_root =
            private_directory(&app_data_root.join("local-appstore/candidates"))?;
        let legacy_seed_root = private_directory(&app_data_root.join("private-candidates"))?;
        let allowed_seed_roots = env::join_paths([
            seed_root.as_os_str(),
            appstore_seed_root.as_os_str(),
            legacy_seed_root.as_os_str(),
        ])
        .map_err(|_| "failed to encode RoomHash seed roots.".to_string())?;
        let node = resolve_file(
            "VIBAPP_ROOMHASH_NODE_BIN",
            packaged_candidates(&[
                "roomhash/node.exe",
                "roomhash/node",
                "roomhash-runtime/bin/node",
                "node/bin/node",
            ])
            .into_iter()
            .chain([
                PathBuf::from("/opt/homebrew/bin/node"),
                PathBuf::from("/usr/local/bin/node"),
                PathBuf::from("/usr/bin/node"),
            ]),
        )?;
        let script = resolve_file(
            "VIBAPP_ROOMHASH_HOST_SCRIPT",
            packaged_candidates(&[
                "roomhash/roomhash-host.mjs",
                "roomhash-transport/roomhash-host.mjs",
            ])
            .into_iter()
            .chain([PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../roomhash-transport/roomhash-host.mjs")]),
        )?;
        let roomhash_root = resolve_directory(
            "VIBAPP_ROOMHASH_ROOT",
            packaged_candidates(&["roomhash/current", "RoomHash"])
                .into_iter()
                .chain([PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../../RoomHash")]),
        )?;
        let collaboration_root = resolve_directory(
            "VIBAPP_ROOMHASH_COLLABORATION_ROOT",
            packaged_candidates(&["roomhash/collaboration", "roomhash/collaboration/src"])
                .into_iter()
                .chain([PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("../../roomhash-collaboration/src")]),
        )?;

        let mut command = Command::new(node);
        command
            .arg(script)
            .current_dir(&data_dir)
            .env_clear()
            .env("VIBAPP_ROOMHASH_ROOT", &roomhash_root)
            .env("VIBAPP_ROOMHASH_COLLABORATION_ROOT", &collaboration_root)
            .env("VIBAPP_ROOMHASH_DATA_DIR", &data_dir)
            .env("VIBAPP_ROOMHASH_ALLOWED_SEED_ROOTS", &allowed_seed_roots)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to spawn RoomHash host: {error}"))?;
        let stdin = child.stdin.take().ok_or_else(|| {
            let _ = child.kill();
            "RoomHash host stdin was unavailable.".to_string()
        })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            let _ = child.kill();
            "RoomHash host stdout was unavailable.".to_string()
        })?;
        if let Some(mut stderr) = child.stderr.take() {
            thread::spawn(move || {
                let mut buffer = [0_u8; 8 * 1024];
                while stderr.read(&mut buffer).ok().filter(|n| *n > 0).is_some() {}
            });
        }
        let (sender, responses) = mpsc::channel();
        thread::spawn(move || read_jsonl(stdout, sender));
        let mut host = Self {
            child,
            stdin: Some(stdin),
            responses,
            next_request_id: 1,
            stopped: false,
        };
        if let Err(error) = host.receive_ready() {
            host.terminate();
            return Err(error);
        }
        Ok(host)
    }

    pub fn start(&mut self, config: &RoomHashConfig) -> Result<Value, String> {
        self.configure(config)?;
        self.request("start", json!({}))
    }

    pub fn status(&mut self) -> Result<Value, String> {
        self.request("status", json!({}))
    }

    pub fn collaboration_status(&mut self) -> Result<Value, String> {
        self.request("collaboration-status", json!({}))
    }

    pub fn collaboration_create(
        &mut self,
        channel_id: &str,
        expires_at: u64,
        grant: &Value,
    ) -> Result<Value, String> {
        self.collaboration_open("create", channel_id, expires_at, grant)
    }

    pub fn collaboration_join(
        &mut self,
        channel_id: &str,
        expires_at: u64,
        grant: &Value,
    ) -> Result<Value, String> {
        self.collaboration_open("join", channel_id, expires_at, grant)
    }

    pub fn collaboration_leave(&mut self, session_id: &str) -> Result<Value, String> {
        let session_id = normalized_uuid(session_id, "collaboration session_id")?;
        self.request("collaboration-leave", json!({ "session_id": session_id }))
    }

    pub fn collaboration_send(
        &mut self,
        session_id: &str,
        message: &[u8],
    ) -> Result<Value, String> {
        let session_id = normalized_uuid(session_id, "collaboration session_id")?;
        if message.is_empty() || message.len() > MAX_COLLABORATION_MESSAGE_BYTES {
            return Err(format!(
                "RoomHash collaboration message must contain 1..{MAX_COLLABORATION_MESSAGE_BYTES} bytes."
            ));
        }
        self.request(
            "collaboration-send",
            json!({
                "session_id": session_id,
                "message_base64": encode_base64(message),
            }),
        )
    }

    pub fn collaboration_receive(&mut self, session_id: &str, limit: u64) -> Result<Value, String> {
        let session_id = normalized_uuid(session_id, "collaboration session_id")?;
        if !(1..=4).contains(&limit) {
            return Err("RoomHash collaboration receive limit must be between 1 and 4.".into());
        }
        self.request(
            "collaboration-receive",
            json!({ "session_id": session_id, "limit": limit }),
        )
    }

    pub fn seed_package(
        &mut self,
        candidate_path: &Path,
        package_digest_sha256: &str,
    ) -> Result<Value, String> {
        if !candidate_path.is_absolute()
            || package_digest_sha256.len() != 64
            || !package_digest_sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err("RoomHash seed-package input is invalid.".into());
        }
        self.request_with_timeout(
            "seed-package",
            json!({
                "candidate_path": candidate_path,
                "package_digest_sha256": package_digest_sha256,
            }),
            SEED_TIMEOUT,
        )
    }

    pub fn fetch_package(
        &mut self,
        magnet_uri: &str,
        package_digest_sha256: &str,
        files: Value,
        timeout_ms: u64,
    ) -> Result<Value, String> {
        if !(1_000..=120_000).contains(&timeout_ms) {
            return Err("RoomHash fetch timeout is invalid.".into());
        }
        self.request_with_timeout(
            "fetch",
            json!({
                "magnet_uri": magnet_uri,
                "package_digest_sha256": package_digest_sha256,
                "files": files,
                "timeout_ms": timeout_ms,
            }),
            Duration::from_millis(timeout_ms.saturating_add(15_000)),
        )
    }

    pub fn configure(&mut self, config: &RoomHashConfig) -> Result<Value, String> {
        self.request("configure", config.to_value())
    }

    fn collaboration_open(
        &mut self,
        operation: &str,
        channel_id: &str,
        expires_at: u64,
        grant: &Value,
    ) -> Result<Value, String> {
        let channel_id = normalized_uuid(channel_id, "collaboration channel_id")?;
        validate_collaboration_grant(grant, operation, &channel_id, expires_at)?;
        self.request(
            &format!("collaboration-{operation}"),
            json!({
                "channel_id": channel_id,
                "expires_at": expires_at,
                "grant": grant,
            }),
        )
    }

    pub fn shutdown(&mut self) -> Result<Value, String> {
        let result = self.request("shutdown", json!({}))?;
        self.stdin.take();
        let deadline = Instant::now() + EXIT_TIMEOUT;
        loop {
            match self.child.try_wait() {
                Ok(Some(status)) if status.success() => {
                    self.stopped = true;
                    return Ok(result);
                }
                Ok(Some(_)) => {
                    self.stopped = true;
                    return Err("RoomHash host exited unsuccessfully during shutdown.".into());
                }
                Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
                Ok(None) => {
                    self.terminate();
                    return Err("RoomHash host did not exit after shutdown.".into());
                }
                Err(error) => {
                    self.terminate();
                    return Err(format!("failed to inspect RoomHash host: {error}"));
                }
            }
        }
    }

    pub fn is_running(&mut self) -> bool {
        if self.stopped {
            return false;
        }
        match self.child.try_wait() {
            Ok(None) => true,
            Ok(Some(_)) | Err(_) => {
                self.stopped = true;
                self.stdin.take();
                false
            }
        }
    }

    fn receive_ready(&mut self) -> Result<(), String> {
        let response = self.receive(READY_TIMEOUT)?;
        let result = validate_response(response, &Value::String("ready".into()))?;
        if result.get("status").and_then(Value::as_str) != Some("ready") {
            return Err("RoomHash host sent an invalid ready response.".into());
        }
        Ok(())
    }

    fn request(&mut self, command: &str, args: Value) -> Result<Value, String> {
        self.request_with_timeout(command, args, COMMAND_TIMEOUT)
    }

    fn request_with_timeout(
        &mut self,
        command: &str,
        args: Value,
        timeout: Duration,
    ) -> Result<Value, String> {
        if !self.is_running() {
            return Err("RoomHash host is not running.".into());
        }
        let request_id = self.next_request_id;
        self.next_request_id = self
            .next_request_id
            .checked_add(1)
            .ok_or_else(|| "RoomHash request id space was exhausted.".to_string())?;
        let request = json!({
            "schema": REQUEST_SCHEMA,
            "request_id": request_id,
            "command": command,
            "args": args,
        });
        let mut encoded = serde_json::to_vec(&request)
            .map_err(|error| format!("failed to encode RoomHash request: {error}"))?;
        if encoded.len() + 1 > MAX_LINE_BYTES {
            return Err("RoomHash request exceeds the JSONL line limit.".into());
        }
        encoded.push(b'\n');
        let write_result = self
            .stdin
            .as_mut()
            .ok_or_else(|| "RoomHash host stdin is closed.".to_string())?
            .write_all(&encoded)
            .and_then(|_| self.stdin.as_mut().expect("checked above").flush());
        if let Err(error) = write_result {
            self.terminate();
            return Err(format!("failed to write RoomHash request: {error}"));
        }
        let response = match self.receive(timeout) {
            Ok(value) => value,
            Err(error) => {
                self.terminate();
                return Err(error);
            }
        };
        match validate_response(response, &json!(request_id)) {
            Ok(value) => Ok(value),
            Err(error) if error.starts_with("RoomHash host rejected request [") => Err(error),
            Err(error) => {
                self.terminate();
                Err(error)
            }
        }
    }

    fn receive(&mut self, timeout: Duration) -> Result<Value, String> {
        match self.responses.recv_timeout(timeout) {
            Ok(result) => result,
            Err(RecvTimeoutError::Timeout) => Err("RoomHash host response timed out.".into()),
            Err(RecvTimeoutError::Disconnected) => {
                Err("RoomHash host response stream closed.".into())
            }
        }
    }

    fn terminate(&mut self) {
        self.stdin.take();
        if !self.stopped {
            let _ = self.child.kill();
            let _ = self.child.wait();
            self.stopped = true;
        }
    }
}

impl Drop for RoomHashHost {
    fn drop(&mut self) {
        self.terminate();
    }
}

fn read_jsonl(mut stdout: impl Read, sender: mpsc::Sender<Result<Value, String>>) {
    let mut line = Vec::new();
    let mut chunk = [0_u8; 8 * 1024];
    loop {
        let count = match stdout.read(&mut chunk) {
            Ok(0) => {
                if !line.is_empty() {
                    let _ = sender.send(Err("RoomHash host closed stdout mid-line.".into()));
                }
                return;
            }
            Ok(count) => count,
            Err(error) => {
                let _ = sender.send(Err(format!("failed to read RoomHash host stdout: {error}")));
                return;
            }
        };
        for byte in &chunk[..count] {
            if *byte == b'\n' {
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                if line.is_empty() {
                    let _ = sender.send(Err("RoomHash host emitted an empty JSONL line.".into()));
                    return;
                }
                let value = serde_json::from_slice::<Value>(&line)
                    .map_err(|_| "RoomHash host emitted invalid JSON.".to_string());
                if sender.send(value).is_err() {
                    return;
                }
                line.clear();
            } else {
                line.push(*byte);
                if line.len() + 1 > MAX_LINE_BYTES {
                    let _ = sender.send(Err(
                        "RoomHash host response exceeds the JSONL line limit.".into(),
                    ));
                    return;
                }
            }
        }
    }
}

fn validate_response(value: Value, expected_id: &Value) -> Result<Value, String> {
    let object = exact_object(
        value,
        &["schema", "request_id", "ok", "result", "error"],
        "RoomHash host response",
    )?;
    if object.get("schema").and_then(Value::as_str) != Some(RESPONSE_SCHEMA)
        || object.get("request_id") != Some(expected_id)
    {
        return Err("RoomHash host response identity is invalid.".into());
    }
    match object.get("ok").and_then(Value::as_bool) {
        Some(true) if object.get("error") == Some(&Value::Null) => object
            .get("result")
            .cloned()
            .ok_or_else(|| "RoomHash host response omitted result.".into()),
        Some(false) if object.get("result") == Some(&Value::Null) => {
            let error = object
                .get("error")
                .and_then(Value::as_object)
                .ok_or_else(|| "RoomHash host rejection is malformed.".to_string())?;
            if error.len() != 2 || !error.contains_key("code") || !error.contains_key("message") {
                return Err("RoomHash host rejection is malformed.".into());
            }
            let code = error
                .get("code")
                .and_then(Value::as_str)
                .filter(|value| {
                    !value.is_empty()
                        && value.len() <= 64
                        && value.bytes().all(|byte| {
                            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'
                        })
                })
                .ok_or_else(|| "RoomHash host rejection code is invalid.".to_string())?;
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .filter(|value| {
                    !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
                })
                .ok_or_else(|| "RoomHash host rejection message is invalid.".to_string())?;
            Err(format!(
                "RoomHash host rejected request [{code}]: {message}"
            ))
        }
        _ => Err("RoomHash host response result/error state is invalid.".into()),
    }
}

fn exact_object(value: Value, keys: &[&str], label: &str) -> Result<Map<String, Value>, String> {
    let object = value
        .as_object()
        .ok_or_else(|| format!("{label} must be an object."))?;
    if object.len() != keys.len() || keys.iter().any(|key| !object.contains_key(*key)) {
        return Err(format!("{label} contains missing or unknown keys."));
    }
    Ok(object.clone())
}

fn unsigned(object: &Map<String, Value>, key: &str) -> Result<u64, String> {
    object
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("RoomHash {key} must be an unsigned integer."))
}

fn boolean(object: &Map<String, Value>, key: &str) -> Result<bool, String> {
    object
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| format!("RoomHash {key} must be a boolean."))
}

fn bounded_string(
    object: &Map<String, Value>,
    key: &str,
    maximum: usize,
    allow_empty: bool,
) -> Result<String, String> {
    let value = object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("RoomHash {key} must be a string."))?;
    if (!allow_empty && value.is_empty())
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return Err(format!("RoomHash {key} is invalid."));
    }
    Ok(value.to_string())
}

fn string_array(
    object: &Map<String, Value>,
    key: &str,
    maximum_items: usize,
    maximum_item_bytes: usize,
) -> Result<Vec<String>, String> {
    let values = object
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("RoomHash {key} must be an array."))?;
    if values.len() > maximum_items {
        return Err(format!("RoomHash {key} contains too many entries."));
    }
    let mut output = Vec::with_capacity(values.len());
    for value in values {
        let value = value
            .as_str()
            .ok_or_else(|| format!("RoomHash {key} entries must be strings."))?;
        if value.is_empty()
            || value.len() > maximum_item_bytes
            || value.chars().any(char::is_control)
            || output.iter().any(|existing| existing == value)
        {
            return Err(format!("RoomHash {key} contains an invalid entry."));
        }
        output.push(value.to_string());
    }
    Ok(output)
}

fn validate_url(value: &str, schemes: &[&str], label: &str) -> Result<(), String> {
    if !schemes.iter().any(|scheme| value.starts_with(scheme))
        || value.chars().any(char::is_whitespace)
        || value.contains('@')
        || value.contains('#')
    {
        return Err(format!("RoomHash {label} is invalid."));
    }
    Ok(())
}

fn normalized_uuid(value: &str, label: &str) -> Result<String, String> {
    let bytes = value.as_bytes();
    let hyphens = [8, 13, 18, 23];
    let valid = bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| {
            if hyphens.contains(&index) {
                *byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
        && matches!(bytes[14].to_ascii_lowercase(), b'1'..=b'5')
        && matches!(bytes[19].to_ascii_lowercase(), b'8' | b'9' | b'a' | b'b');
    if !valid || value.eq_ignore_ascii_case("00000000-0000-0000-0000-000000000000") {
        return Err(format!("RoomHash {label} must be a non-nil RFC 4122 UUID."));
    }
    Ok(value.to_ascii_lowercase())
}

fn unix_time_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn validate_collaboration_grant(
    value: &Value,
    operation: &str,
    channel_id: &str,
    expires_at: u64,
) -> Result<(), String> {
    if !matches!(operation, "create" | "join") {
        return Err("RoomHash collaboration operation is invalid.".into());
    }
    let grant = exact_object(
        value.clone(),
        &[
            "schema",
            "grant_id",
            "approved",
            "operation",
            "channel_id",
            "expires_at",
        ],
        "RoomHash collaboration grant",
    )?;
    if grant.get("schema").and_then(Value::as_str) != Some(COLLABORATION_GRANT_SCHEMA)
        || grant.get("approved").and_then(Value::as_bool) != Some(true)
        || grant.get("operation").and_then(Value::as_str) != Some(operation)
    {
        return Err("RoomHash collaboration grant is not an explicit matching approval.".into());
    }
    normalized_uuid(
        grant
            .get("grant_id")
            .and_then(Value::as_str)
            .ok_or_else(|| "RoomHash collaboration grant_id is invalid.".to_string())?,
        "collaboration grant_id",
    )?;
    let granted_channel = normalized_uuid(
        grant
            .get("channel_id")
            .and_then(Value::as_str)
            .ok_or_else(|| "RoomHash collaboration grant channel_id is invalid.".to_string())?,
        "collaboration grant channel_id",
    )?;
    if granted_channel != channel_id
        || grant.get("expires_at").and_then(Value::as_u64) != Some(expires_at)
    {
        return Err(
            "RoomHash collaboration grant does not match the requested channel or expiry.".into(),
        );
    }
    let now = unix_time_millis();
    if expires_at <= now || expires_at > now.saturating_add(MAX_COLLABORATION_TTL_MS) {
        return Err("RoomHash collaboration expiry must be within the next 24 hours.".into());
    }
    Ok(())
}

fn encode_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let first = chunk[0];
        let second = chunk.get(1).copied().unwrap_or(0);
        let third = chunk.get(2).copied().unwrap_or(0);
        output.push(TABLE[(first >> 2) as usize] as char);
        output.push(TABLE[(((first & 0x03) << 4) | (second >> 4)) as usize] as char);
        if chunk.len() > 1 {
            output.push(TABLE[(((second & 0x0f) << 2) | (third >> 6)) as usize] as char);
        } else {
            output.push('=');
        }
        if chunk.len() > 2 {
            output.push(TABLE[(third & 0x3f) as usize] as char);
        } else {
            output.push('=');
        }
    }
    output
}

fn private_directory(path: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err("RoomHash data paths must be absolute.".into());
    }
    if let Ok(metadata) = fs::symlink_metadata(path)
        && metadata.file_type().is_symlink()
    {
        return Err("RoomHash data directory must not be a symlink.".into());
    }
    fs::create_dir_all(path)
        .map_err(|error| format!("failed to create RoomHash data directory: {error}"))?;
    #[cfg(unix)]
    {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("failed to protect RoomHash data directory: {error}"))?;
    }
    #[cfg(windows)]
    crate::native_platform::protect_private_directory(path)?;
    fs::canonicalize(path)
        .map_err(|error| format!("failed to resolve RoomHash data directory: {error}"))
}

fn resolve_file(
    override_name: &str,
    candidates: impl IntoIterator<Item = PathBuf>,
) -> Result<PathBuf, String> {
    resolve_path(override_name, candidates, false)
}

fn resolve_directory(
    override_name: &str,
    candidates: impl IntoIterator<Item = PathBuf>,
) -> Result<PathBuf, String> {
    resolve_path(override_name, candidates, true)
}

fn resolve_path(
    override_name: &str,
    candidates: impl IntoIterator<Item = PathBuf>,
    directory: bool,
) -> Result<PathBuf, String> {
    let override_path = env::var_os(override_name).map(PathBuf::from);
    let paths: Vec<PathBuf> = override_path
        .clone()
        .into_iter()
        .chain(candidates)
        .collect();
    for path in paths {
        if !path.is_absolute() {
            if override_path.is_some() {
                return Err(format!("{override_name} must be an absolute path."));
            }
            continue;
        }
        let Ok(path) = fs::canonicalize(path) else {
            if override_path.is_some() {
                return Err(format!(
                    "{override_name} does not resolve to an existing path."
                ));
            }
            continue;
        };
        let valid = if directory {
            path.is_dir()
        } else {
            path.is_file()
        };
        if !valid {
            if override_path.is_some() {
                return Err(format!("{override_name} has the wrong file type."));
            }
            continue;
        }
        return Ok(path);
    }
    Err(format!(
        "unable to locate required RoomHash path ({override_name})."
    ))
}

fn packaged_candidates(relative_paths: &[&str]) -> Vec<PathBuf> {
    let Some(executable) = env::current_exe().ok() else {
        return Vec::new();
    };
    let Some(macos_dir) = executable.parent() else {
        return Vec::new();
    };
    let Some(contents_dir) = macos_dir.parent() else {
        return Vec::new();
    };
    let resources = if cfg!(target_os = "macos") {
        contents_dir.join("Resources")
    } else if cfg!(target_os = "windows") {
        macos_dir.join("resources")
    } else {
        contents_dir.join("resources")
    };
    relative_paths
        .iter()
        .map(|path| resources.join(path))
        .collect()
}

fn is_lower_hex(value: &str, expected_bytes: usize) -> bool {
    value.len() == expected_bytes
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn safe_locator_path(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && !value.starts_with('/')
        && !value.starts_with('\\')
        && !value.contains('\\')
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'/' | b'-'))
        && !value
            .split('/')
            .any(|segment| segment.is_empty() || matches!(segment, "." | ".."))
}

fn percent_decode_component(value: &str, plus_as_space: bool) -> Option<String> {
    if value.len() > 4_096 || !value.is_ascii() {
        return None;
    }
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                let high = hex_nibble(bytes[index + 1])?;
                let low = hex_nibble(bytes[index + 2])?;
                decoded.push((high << 4) | low);
                index += 3;
            }
            b'%' => return None,
            b'+' if plus_as_space => {
                decoded.push(b' ');
                index += 1;
            }
            byte if byte.is_ascii_control() || byte == b' ' => return None,
            byte => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8(decoded).ok()
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn valid_locator_magnet(value: &str, package_digest: &str, info_hash: &str) -> bool {
    if value.len() > 4_096
        || value.chars().any(char::is_control)
        || value.chars().any(char::is_whitespace)
        || value.contains('#')
    {
        return false;
    }
    let Some(query) = value.strip_prefix("magnet:?") else {
        return false;
    };
    let parameters = query.split('&').collect::<Vec<_>>();
    if !(3..=2 + PACKAGE_LOCATOR_TRACKERS.len()).contains(&parameters.len())
        || parameters[0] != format!("xt=urn:btih:{info_hash}")
        || parameters[1] != format!("dn={package_digest}.vibapp-candidate")
    {
        return false;
    }
    let mut trackers = HashSet::new();
    for parameter in &parameters[2..] {
        let Some(encoded_tracker) = parameter.strip_prefix("tr=") else {
            return false;
        };
        let Some(tracker) = percent_decode_component(encoded_tracker, false) else {
            return false;
        };
        if !PACKAGE_LOCATOR_TRACKERS.contains(&tracker.as_str())
            || !trackers.insert(tracker.clone())
            || encoded_tracker != tracker.replace(':', "%3A").replace('/', "%2F")
        {
            return false;
        }
    }
    !trackers.is_empty()
}

fn validate_package_locator(
    locator: &PackageLocatorDocument,
    expected_digest: &str,
) -> Result<(), String> {
    if locator.schema_version != PACKAGE_LOCATOR_SCHEMA
        || locator.transport != PACKAGE_LOCATOR_TRANSPORT
        || locator.trust_note != PACKAGE_LOCATOR_TRUST_NOTE
    {
        return Err("RoomHash package locator contract is invalid.".into());
    }
    if !is_lower_hex(&locator.package_digest_sha256, 64)
        || locator.package_digest_sha256 != expected_digest
    {
        return Err("RoomHash package locator digest binding is invalid.".into());
    }
    if !is_lower_hex(&locator.info_hash, 40)
        || !valid_locator_magnet(
            &locator.magnet_uri,
            &locator.package_digest_sha256,
            &locator.info_hash,
        )
    {
        return Err("RoomHash package locator transport identity is invalid.".into());
    }
    if !(2..=MAX_PACKAGE_FILES).contains(&locator.files.len())
        || !(2..=MAX_PACKAGE_BYTES).contains(&locator.size_bytes)
    {
        return Err("RoomHash package locator inventory is outside supported bounds.".into());
    }
    let mut paths = HashSet::with_capacity(locator.files.len());
    let mut total_bytes = 0_u64;
    for file in &locator.files {
        if !safe_locator_path(&file.path)
            || !is_lower_hex(&file.sha256, 64)
            || !(1..=MAX_PACKAGE_BYTES).contains(&file.size_bytes)
            || !paths.insert(file.path.as_str())
        {
            return Err("RoomHash package locator contains an invalid file descriptor.".into());
        }
        total_bytes = total_bytes
            .checked_add(file.size_bytes)
            .ok_or_else(|| "RoomHash package locator byte total overflowed.".to_string())?;
        if total_bytes > MAX_PACKAGE_BYTES {
            return Err("RoomHash package locator exceeds the package byte limit.".into());
        }
    }
    if total_bytes != locator.size_bytes
        || !paths.contains("candidate.json")
        || !paths.contains("package/manifest.json")
    {
        return Err("RoomHash package locator inventory binding is invalid.".into());
    }
    Ok(())
}

/// Parses and validates exact locator bytes using the same fixed public transport
/// contract used by the trusted desktop downloader. The returned value contains
/// locator data only and never grants install or execution authority.
pub(crate) fn validate_package_locator_bytes(
    encoded: &[u8],
    expected_digest: &str,
) -> Result<Value, String> {
    if encoded.len() < 2 || encoded.len() > MAX_LOCATOR_BYTES {
        return Err("RoomHash package locator exceeds its size limit.".into());
    }
    let locator = serde_json::from_slice::<PackageLocatorDocument>(encoded)
        .map_err(|_| "RoomHash package locator JSON is malformed or not exact.".to_string())?;
    validate_package_locator(&locator, expected_digest)?;
    serde_json::to_value(locator)
        .map_err(|_| "RoomHash package locator could not be normalized.".to_string())
}

fn checked_app_data_root(app_data_root: &Path) -> Result<PathBuf, String> {
    if !app_data_root.is_absolute() {
        return Err("RoomHash app data root must be absolute.".into());
    }
    let root_metadata = fs::symlink_metadata(app_data_root)
        .map_err(|_| "RoomHash app data root is unavailable.".to_string())?;
    if root_metadata.file_type().is_symlink() || !root_metadata.is_dir() {
        return Err("RoomHash app data root has an unsafe file type.".into());
    }
    let canonical_root = fs::canonicalize(app_data_root)
        .map_err(|_| "RoomHash app data root could not be resolved.".to_string())?;
    let final_metadata = fs::symlink_metadata(app_data_root)
        .map_err(|_| "RoomHash app data root changed while being resolved.".to_string())?;
    if final_metadata.file_type().is_symlink()
        || !final_metadata.is_dir()
        || !same_directory_identity(&root_metadata, &final_metadata)
    {
        return Err("RoomHash app data root changed while being resolved.".into());
    }
    Ok(canonical_root)
}

fn checked_child_directory(
    parent: &Path,
    name: &str,
    label: &str,
) -> Result<Option<PathBuf>, String> {
    let child_path = parent.join(name);
    let initial_metadata = match fs::symlink_metadata(&child_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => {
            return Err(format!(
                "RoomHash {label} directory could not be inspected."
            ));
        }
    };
    if initial_metadata.file_type().is_symlink() || !initial_metadata.is_dir() {
        return Err(format!(
            "RoomHash {label} directory has an unsafe file type."
        ));
    }
    let canonical_child = fs::canonicalize(&child_path)
        .map_err(|_| format!("RoomHash {label} directory could not be resolved."))?;
    if canonical_child.parent() != Some(parent) {
        return Err(format!(
            "RoomHash {label} directory escaped managed storage."
        ));
    }
    let final_metadata = fs::symlink_metadata(&child_path)
        .map_err(|_| format!("RoomHash {label} directory changed while being resolved."))?;
    if final_metadata.file_type().is_symlink()
        || !final_metadata.is_dir()
        || !same_directory_identity(&initial_metadata, &final_metadata)
    {
        return Err(format!(
            "RoomHash {label} directory changed while being resolved."
        ));
    }
    Ok(Some(canonical_child))
}

fn private_child_directory(parent: &Path, name: &str, label: &str) -> Result<PathBuf, String> {
    if let Some(existing) = checked_child_directory(parent, name, label)? {
        protect_private_child_directory(&existing, label)?;
        return Ok(existing);
    }

    let child_path = parent.join(name);
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    builder.mode(0o700);
    match builder.create(&child_path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(_) => return Err(format!("RoomHash {label} directory could not be created.")),
    }
    let created = checked_child_directory(parent, name, label)?
        .ok_or_else(|| format!("RoomHash {label} directory disappeared after creation."))?;
    protect_private_child_directory(&created, label)?;
    Ok(created)
}

#[cfg(unix)]
fn protect_private_child_directory(path: &Path, label: &str) -> Result<(), String> {
    let directory = OpenOptions::new()
        .read(true)
        .open(path)
        .map_err(|_| format!("RoomHash {label} directory could not be opened."))?;
    let opened_metadata = directory
        .metadata()
        .map_err(|_| format!("RoomHash {label} directory metadata is unavailable."))?;
    let path_metadata = fs::symlink_metadata(path)
        .map_err(|_| format!("RoomHash {label} directory changed while being opened."))?;
    if !opened_metadata.is_dir()
        || path_metadata.file_type().is_symlink()
        || !path_metadata.is_dir()
        || !same_directory_identity(&opened_metadata, &path_metadata)
    {
        return Err(format!(
            "RoomHash {label} directory changed while being opened."
        ));
    }
    directory
        .set_permissions(fs::Permissions::from_mode(0o700))
        .map_err(|_| format!("RoomHash {label} directory could not be protected."))
}

#[cfg(not(unix))]
fn protect_private_child_directory(path: &Path, _label: &str) -> Result<(), String> {
    crate::native_platform::protect_private_directory(path)
}

fn existing_locator_directory(app_data_root: &Path) -> Result<Option<PathBuf>, String> {
    let canonical_root = checked_app_data_root(app_data_root)?;
    let Some(roomhash) = checked_child_directory(&canonical_root, "roomhash", "data")? else {
        return Ok(None);
    };
    checked_child_directory(&roomhash, "package-locators", "package locator")
}

fn private_locator_directory(app_data_root: &Path) -> Result<PathBuf, String> {
    let canonical_root = checked_app_data_root(app_data_root)?;
    let roomhash = private_child_directory(&canonical_root, "roomhash", "data")?;
    private_child_directory(&roomhash, "package-locators", "package locator")
}

fn confirm_locator_directory(
    app_data_root: &Path,
    expected_path: &Path,
    expected_metadata: &fs::Metadata,
) -> Result<(), String> {
    let current = existing_locator_directory(app_data_root)?
        .ok_or_else(|| "RoomHash package locator directory disappeared.".to_string())?;
    let current_metadata = fs::symlink_metadata(&current)
        .map_err(|_| "RoomHash package locator directory could not be rechecked.".to_string())?;
    if current != expected_path
        || current_metadata.file_type().is_symlink()
        || !current_metadata.is_dir()
        || !same_directory_identity(expected_metadata, &current_metadata)
    {
        return Err("RoomHash package locator directory changed before commit.".into());
    }
    Ok(())
}

fn validate_locator_replace_target(destination: &Path) -> Result<(), String> {
    let metadata = match fs::symlink_metadata(destination) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => {
            return Err("RoomHash package locator destination could not be inspected.".into());
        }
    };
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("RoomHash package locator destination has an unsafe file type.".into());
    }
    #[cfg(unix)]
    if metadata.nlink() != 1 {
        return Err("RoomHash package locator destination must not be hard-linked.".into());
    }
    Ok(())
}

fn cleanup_owned_locator_temporary(
    app_data_root: &Path,
    directory: &Path,
    directory_metadata: &fs::Metadata,
    temporary: &Path,
    temporary_metadata: &fs::Metadata,
) {
    if confirm_locator_directory(app_data_root, directory, directory_metadata).is_err() {
        return;
    }
    let Ok(current) = fs::symlink_metadata(temporary) else {
        return;
    };
    if current.file_type().is_symlink()
        || !current.is_file()
        || !same_file_identity(temporary_metadata, &current)
    {
        return;
    }
    #[cfg(unix)]
    if current.nlink() != 1 {
        return;
    }
    let _ = fs::remove_file(temporary);
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

pub fn persist_package_locator(app_data_root: &Path, receipt: &Value) -> Result<PathBuf, String> {
    let receipt = receipt
        .as_object()
        .ok_or_else(|| "RoomHash package locator receipt must be an object.".to_string())?;
    let package_digest = receipt
        .get("package_digest_sha256")
        .and_then(Value::as_str)
        .filter(|value| is_lower_hex(value, 64))
        .ok_or_else(|| "RoomHash package locator digest is invalid.".to_string())?
        .to_string();
    let info_hash = receipt
        .get("info_hash")
        .and_then(Value::as_str)
        .filter(|value| is_lower_hex(value, 40))
        .ok_or_else(|| "RoomHash package locator info hash is invalid.".to_string())?
        .to_string();
    let magnet = receipt
        .get("magnet_uri")
        .and_then(Value::as_str)
        .filter(|value| value.len() <= 4_096)
        .ok_or_else(|| "RoomHash package locator magnet is invalid.".to_string())?
        .to_string();
    let size_bytes = receipt
        .get("size_bytes")
        .and_then(Value::as_u64)
        .ok_or_else(|| "RoomHash package locator size is invalid.".to_string())?;
    let files = receipt
        .get("files")
        .cloned()
        .ok_or_else(|| "RoomHash package locator inventory is invalid.".to_string())?;
    let files = serde_json::from_value::<Vec<PackageLocatorFile>>(files)
        .map_err(|_| "RoomHash package locator inventory is invalid.".to_string())?;
    let document = PackageLocatorDocument {
        schema_version: PACKAGE_LOCATOR_SCHEMA.to_string(),
        package_digest_sha256: package_digest.clone(),
        transport: PACKAGE_LOCATOR_TRANSPORT.to_string(),
        info_hash,
        magnet_uri: magnet,
        size_bytes,
        files,
        trust_note: PACKAGE_LOCATOR_TRUST_NOTE.to_string(),
    };
    validate_package_locator(&document, &package_digest)?;
    let encoded = serde_json::to_vec(&document)
        .map_err(|error| format!("failed to encode RoomHash package locator: {error}"))?;
    if encoded.is_empty() || encoded.len() > MAX_LOCATOR_BYTES {
        return Err("RoomHash package locator exceeds its size limit.".into());
    }
    let directory = private_locator_directory(app_data_root)?;
    let directory_metadata = fs::symlink_metadata(&directory)
        .map_err(|_| "RoomHash package locator directory metadata is unavailable.".to_string())?;
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return Err("RoomHash package locator directory has an unsafe file type.".into());
    }
    let destination = directory.join(format!("{package_digest}.json"));
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = directory.join(format!(".{package_digest}.{nonce:x}.tmp"));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|error| format!("failed to create RoomHash package locator: {error}"))?;
    let created_metadata = file
        .metadata()
        .map_err(|_| "RoomHash package locator temporary metadata is unavailable.".to_string())?;
    let created_path_metadata = fs::symlink_metadata(&temporary)
        .map_err(|_| "RoomHash package locator temporary changed after creation.".to_string())?;
    if !created_metadata.is_file()
        || created_path_metadata.file_type().is_symlink()
        || !created_path_metadata.is_file()
        || !same_file_identity(&created_metadata, &created_path_metadata)
    {
        drop(file);
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &created_metadata,
        );
        return Err("RoomHash package locator temporary changed after creation.".into());
    }
    #[cfg(unix)]
    if created_metadata.nlink() != 1 || created_path_metadata.nlink() != 1 {
        drop(file);
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &created_metadata,
        );
        return Err("RoomHash package locator temporary must not be hard-linked.".into());
    }

    let write_result = file.write_all(&encoded).and_then(|_| {
        #[cfg(unix)]
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        file.sync_all()
    });
    if let Err(error) = write_result {
        let cleanup_metadata = file.metadata().unwrap_or(created_metadata);
        drop(file);
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &cleanup_metadata,
        );
        return Err(format!(
            "failed to persist RoomHash package locator: {error}"
        ));
    }
    let temporary_metadata = file
        .metadata()
        .map_err(|_| "RoomHash package locator temporary metadata is unavailable.".to_string())?;
    let temporary_path_metadata = fs::symlink_metadata(&temporary)
        .map_err(|_| "RoomHash package locator temporary changed before commit.".to_string())?;
    if !temporary_metadata.is_file()
        || temporary_metadata.len() != encoded.len() as u64
        || temporary_path_metadata.file_type().is_symlink()
        || !temporary_path_metadata.is_file()
        || temporary_path_metadata.len() != encoded.len() as u64
        || !same_file_identity(&temporary_metadata, &temporary_path_metadata)
    {
        drop(file);
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &temporary_metadata,
        );
        return Err("RoomHash package locator temporary changed before commit.".into());
    }
    #[cfg(unix)]
    if temporary_metadata.nlink() != 1 || temporary_path_metadata.nlink() != 1 {
        drop(file);
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &temporary_metadata,
        );
        return Err("RoomHash package locator temporary must not be hard-linked.".into());
    }
    drop(file);

    if let Err(error) = confirm_locator_directory(app_data_root, &directory, &directory_metadata) {
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &temporary_metadata,
        );
        return Err(error);
    }
    if let Err(error) = validate_locator_replace_target(&destination) {
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &temporary_metadata,
        );
        return Err(error);
    }
    if let Err(error) = fs::rename(&temporary, &destination) {
        cleanup_owned_locator_temporary(
            app_data_root,
            &directory,
            &directory_metadata,
            &temporary,
            &temporary_metadata,
        );
        return Err(format!(
            "failed to commit RoomHash package locator: {error}"
        ));
    }
    let committed_metadata = fs::symlink_metadata(&destination)
        .map_err(|_| "RoomHash package locator changed immediately after commit.".to_string())?;
    if committed_metadata.file_type().is_symlink()
        || !committed_metadata.is_file()
        || committed_metadata.len() != encoded.len() as u64
        || !same_file_identity(&temporary_metadata, &committed_metadata)
    {
        return Err("RoomHash package locator changed immediately after commit.".into());
    }
    #[cfg(unix)]
    if committed_metadata.nlink() != 1 {
        return Err("RoomHash package locator became hard-linked during commit.".into());
    }
    confirm_locator_directory(app_data_root, &directory, &directory_metadata)?;
    Ok(destination)
}

/// Reads one locally persisted transport locator without exposing its filesystem path.
///
/// A locator identifies bytes only. Callers must still apply the ordinary VibApp
/// candidate verification and admission path before installing or executing them.
pub fn read_package_locator(
    app_data_root: &Path,
    package_digest_sha256: &str,
) -> Result<Option<Value>, String> {
    if !is_lower_hex(package_digest_sha256, 64) {
        return Err("RoomHash package locator digest is invalid.".into());
    }
    let Some(directory) = existing_locator_directory(app_data_root)? else {
        return Ok(None);
    };
    let locator_path = directory.join(format!("{package_digest_sha256}.json"));
    let initial_metadata = match fs::symlink_metadata(&locator_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("RoomHash package locator could not be inspected.".into()),
    };
    if initial_metadata.file_type().is_symlink()
        || !initial_metadata.is_file()
        || initial_metadata.len() < 2
        || initial_metadata.len() > MAX_LOCATOR_BYTES as u64
    {
        return Err("RoomHash package locator is not a bounded regular file.".into());
    }
    #[cfg(unix)]
    if initial_metadata.nlink() != 1 {
        return Err("RoomHash package locator must not be hard-linked.".into());
    }

    let mut file = OpenOptions::new()
        .read(true)
        .open(&locator_path)
        .map_err(|_| "RoomHash package locator could not be opened.".to_string())?;
    let opened_metadata = file
        .metadata()
        .map_err(|_| "RoomHash package locator metadata could not be read.".to_string())?;
    let opened_path_metadata = fs::symlink_metadata(&locator_path)
        .map_err(|_| "RoomHash package locator changed while being opened.".to_string())?;
    if !opened_metadata.is_file()
        || opened_metadata.len() != initial_metadata.len()
        || opened_path_metadata.file_type().is_symlink()
        || !opened_path_metadata.is_file()
        || !same_file_identity(&initial_metadata, &opened_metadata)
        || !same_file_identity(&opened_metadata, &opened_path_metadata)
    {
        return Err("RoomHash package locator changed while being opened.".into());
    }
    #[cfg(unix)]
    if opened_metadata.nlink() != 1 || opened_path_metadata.nlink() != 1 {
        return Err("RoomHash package locator must not be hard-linked.".into());
    }

    let mut encoded = Vec::with_capacity(initial_metadata.len() as usize);
    Read::by_ref(&mut file)
        .take((MAX_LOCATOR_BYTES + 1) as u64)
        .read_to_end(&mut encoded)
        .map_err(|_| "RoomHash package locator could not be read.".to_string())?;
    if encoded.len() != initial_metadata.len() as usize || encoded.len() > MAX_LOCATOR_BYTES {
        return Err("RoomHash package locator changed size while being read.".into());
    }
    let final_metadata = fs::symlink_metadata(&locator_path)
        .map_err(|_| "RoomHash package locator changed while being read.".to_string())?;
    if final_metadata.file_type().is_symlink()
        || !final_metadata.is_file()
        || final_metadata.len() != opened_metadata.len()
        || !same_file_identity(&opened_metadata, &final_metadata)
    {
        return Err("RoomHash package locator changed while being read.".into());
    }
    #[cfg(unix)]
    if final_metadata.nlink() != 1 {
        return Err("RoomHash package locator must not be hard-linked.".into());
    }

    let locator = serde_json::from_slice::<PackageLocatorDocument>(&encoded)
        .map_err(|_| "RoomHash package locator JSON is malformed or not exact.".to_string())?;
    validate_package_locator(&locator, package_digest_sha256)?;
    serde_json::to_value(locator)
        .map(Some)
        .map_err(|_| "RoomHash package locator could not be normalized.".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_PACKAGE_DIGEST: &str =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const TEST_INFO_HASH: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn test_root(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target-roomhash-1_93/test-artifacts")
            .join(format!("{label}-{nonce:x}"));
        fs::create_dir_all(&root).unwrap();
        fs::canonicalize(root).unwrap()
    }

    fn valid_locator_receipt() -> Value {
        json!({
            "operation": "seed-package",
            "package_digest_sha256": TEST_PACKAGE_DIGEST,
            "info_hash": TEST_INFO_HASH,
            "magnet_uri": format!(
                "magnet:?xt=urn:btih:{TEST_INFO_HASH}&dn={TEST_PACKAGE_DIGEST}.vibapp-candidate&tr=wss%3A%2F%2Ftracker.webtorrent.dev"
            ),
            "size_bytes": 2,
            "files": [
                {
                    "path": "candidate.json",
                    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "size_bytes": 1
                },
                {
                    "path": "package/manifest.json",
                    "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "size_bytes": 1
                }
            ]
        })
    }

    fn valid_locator_document() -> Value {
        let receipt = valid_locator_receipt();
        json!({
            "schema_version": PACKAGE_LOCATOR_SCHEMA,
            "package_digest_sha256": receipt["package_digest_sha256"],
            "transport": PACKAGE_LOCATOR_TRANSPORT,
            "info_hash": receipt["info_hash"],
            "magnet_uri": receipt["magnet_uri"],
            "size_bytes": receipt["size_bytes"],
            "files": receipt["files"],
            "trust_note": PACKAGE_LOCATOR_TRUST_NOTE,
        })
    }

    fn write_locator_value(root: &Path, value: &Value) -> PathBuf {
        let directory = root.join("roomhash/package-locators");
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join(format!("{TEST_PACKAGE_DIGEST}.json"));
        fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
        path
    }

    #[test]
    fn configuration_is_exact_and_matches_roomhash_limits() {
        let valid = json!({
            "upload_limit_bps": 1_024,
            "download_limit_bps": 2_048,
            "max_conns": 128,
            "dht": true,
            "lsd": true,
            "pex": true,
            "tracker_urls": ["wss://tracker.webtorrent.dev"],
            "turn": null
        });
        assert!(RoomHashConfig::try_from_value(&valid).is_ok());
        let mut excessive = valid.clone();
        excessive["max_conns"] = json!(129);
        assert!(RoomHashConfig::try_from_value(&excessive).is_err());
        let mut insecure = valid.clone();
        insecure["tracker_urls"] = json!(["https://tracker.invalid"]);
        assert!(RoomHashConfig::try_from_value(&insecure).is_err());
        let mut extra = valid;
        extra["secret"] = json!(true);
        assert!(RoomHashConfig::try_from_value(&extra).is_err());
    }

    #[test]
    fn rejection_preserves_only_bounded_actionable_diagnostics() {
        let error = validate_response(
            json!({
                "schema": RESPONSE_SCHEMA,
                "request_id": 7,
                "ok": false,
                "result": null,
                "error": {"code": "path-denied", "message": "candidate is outside trusted roots"}
            }),
            &json!(7),
        )
        .unwrap_err();
        assert!(error.contains("[path-denied]"));
        assert!(error.contains("outside trusted roots"));
    }

    #[test]
    fn collaboration_grants_and_base64_are_strict_and_bounded() {
        let channel_id = "20000000-0000-4000-8000-000000000001";
        let expires_at = unix_time_millis() + 60_000;
        let grant = json!({
            "schema": COLLABORATION_GRANT_SCHEMA,
            "grant_id": "10000000-0000-4000-8000-000000000001",
            "approved": true,
            "operation": "join",
            "channel_id": channel_id,
            "expires_at": expires_at,
        });
        assert!(validate_collaboration_grant(&grant, "join", channel_id, expires_at).is_ok());
        let mut not_approved = grant.clone();
        not_approved["approved"] = json!(false);
        assert!(
            validate_collaboration_grant(&not_approved, "join", channel_id, expires_at).is_err()
        );
        let mut extra = grant.clone();
        extra["guest_token"] = json!("arbitrary");
        assert!(validate_collaboration_grant(&extra, "join", channel_id, expires_at).is_err());
        assert_eq!(encode_base64(&[1, 2, 3]), "AQID");
        assert_eq!(encode_base64(&[1]), "AQ==");
        assert!(normalized_uuid("00000000-0000-0000-0000-000000000000", "channel").is_err());
    }

    #[test]
    fn package_locator_round_trips_without_exposing_local_paths() {
        let root = test_root("package-locator-round-trip");
        assert_eq!(
            read_package_locator(&root, TEST_PACKAGE_DIGEST).unwrap(),
            None
        );
        assert!(!root.join("roomhash").exists());

        let persisted = persist_package_locator(&root, &valid_locator_receipt()).unwrap();
        assert!(persisted.is_file());
        let locator = read_package_locator(&root, TEST_PACKAGE_DIGEST)
            .unwrap()
            .unwrap();
        assert_eq!(locator, valid_locator_document());
        assert_eq!(locator["size_bytes"], 2);
        assert_eq!(locator["files"].as_array().unwrap().len(), 2);
        assert!(
            !serde_json::to_string(&locator)
                .unwrap()
                .contains(root.to_str().unwrap())
        );
        assert!(read_package_locator(&root, "../candidate").is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn package_locator_rejects_non_exact_or_unbound_content() {
        let root = test_root("package-locator-invalid-content");
        let mut cases = Vec::new();

        let mut unknown = valid_locator_document();
        unknown["local_path"] = json!("/private/secret");
        cases.push(unknown);

        let mut nested_unknown = valid_locator_document();
        nested_unknown["files"][0]["source_path"] = json!("/private/secret");
        cases.push(nested_unknown);

        let mut wrong_digest = valid_locator_document();
        wrong_digest["package_digest_sha256"] =
            json!("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee");
        cases.push(wrong_digest);

        let mut wrong_info_hash = valid_locator_document();
        wrong_info_hash["info_hash"] = json!("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee");
        cases.push(wrong_info_hash);

        let mut web_seed = valid_locator_document();
        web_seed["magnet_uri"] = json!(format!(
            "{}&ws=https%3A%2F%2Funtrusted.invalid%2Fpackage",
            web_seed["magnet_uri"].as_str().unwrap()
        ));
        cases.push(web_seed);

        let mut arbitrary_tracker = valid_locator_document();
        arbitrary_tracker["magnet_uri"] = json!(format!(
            "magnet:?xt=urn:btih:{TEST_INFO_HASH}&dn={TEST_PACKAGE_DIGEST}.vibapp-candidate&tr=wss%3A%2F%2Fevil.invalid"
        ));
        cases.push(arbitrary_tracker);

        let mut missing_tracker = valid_locator_document();
        missing_tracker["magnet_uri"] = json!(format!(
            "magnet:?xt=urn:btih:{TEST_INFO_HASH}&dn={TEST_PACKAGE_DIGEST}.vibapp-candidate"
        ));
        cases.push(missing_tracker);

        let mut noncanonical_encoding = valid_locator_document();
        noncanonical_encoding["magnet_uri"] = json!(format!(
            "magnet:?xt=urn:btih:{TEST_INFO_HASH}&dn={TEST_PACKAGE_DIGEST}.vibapp-candidate&tr=wss%3a%2f%2ftracker.webtorrent.dev"
        ));
        cases.push(noncanonical_encoding);

        let mut unsafe_path = valid_locator_document();
        unsafe_path["files"][0]["path"] = json!("../candidate.json");
        cases.push(unsafe_path);

        let mut duplicate_path = valid_locator_document();
        duplicate_path["files"][1]["path"] = json!("candidate.json");
        cases.push(duplicate_path);

        let mut wrong_total = valid_locator_document();
        wrong_total["size_bytes"] = json!(3);
        cases.push(wrong_total);

        let mut uppercase_digest = valid_locator_document();
        uppercase_digest["files"][0]["sha256"] =
            json!("CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC");
        cases.push(uppercase_digest);

        for value in cases {
            write_locator_value(&root, &value);
            let error = read_package_locator(&root, TEST_PACKAGE_DIGEST).unwrap_err();
            assert!(!error.contains(root.to_str().unwrap()));
        }

        let exact = serde_json::to_string(&valid_locator_document()).unwrap();
        let duplicate_key = exact.replacen(
            "\"schema_version\":",
            &format!("\"schema_version\":\"{PACKAGE_LOCATOR_SCHEMA}\",\"schema_version\":"),
            1,
        );
        let path = root
            .join("roomhash/package-locators")
            .join(format!("{TEST_PACKAGE_DIGEST}.json"));
        fs::write(&path, duplicate_key).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());

        fs::write(&path, vec![b' '; MAX_LOCATOR_BYTES + 1]).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn package_locator_rejects_links_and_non_regular_files() {
        use std::os::unix::fs::symlink;

        let root = test_root("package-locator-file-types");
        let directory = root.join("roomhash/package-locators");
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join(format!("{TEST_PACKAGE_DIGEST}.json"));
        let target = root.join("outside-locator.json");
        fs::write(
            &target,
            serde_json::to_vec(&valid_locator_document()).unwrap(),
        )
        .unwrap();

        symlink(&target, &path).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());
        fs::remove_file(&path).unwrap();

        fs::create_dir(&path).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());
        fs::remove_dir(&path).unwrap();

        fs::hard_link(&target, &path).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn package_locator_rejects_symlinked_storage_directories() {
        use std::os::unix::fs::symlink;

        let root = test_root("package-locator-linked-directories");
        let outside_roomhash = root.join("outside-roomhash");
        let outside_locators = outside_roomhash.join("package-locators");
        fs::create_dir_all(&outside_locators).unwrap();
        fs::write(
            outside_locators.join(format!("{TEST_PACKAGE_DIGEST}.json")),
            serde_json::to_vec(&valid_locator_document()).unwrap(),
        )
        .unwrap();

        symlink(&outside_roomhash, root.join("roomhash")).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());
        fs::remove_file(root.join("roomhash")).unwrap();

        fs::create_dir(root.join("roomhash")).unwrap();
        symlink(&outside_locators, root.join("roomhash/package-locators")).unwrap();
        assert!(read_package_locator(&root, TEST_PACKAGE_DIGEST).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn package_locator_persist_rejects_symlinked_directory_chain() {
        use std::os::unix::fs::symlink;

        let root = test_root("package-locator-persist-linked-directories");
        let outside = test_root("package-locator-persist-outside");
        let roomhash = root.join("roomhash");

        symlink(&outside, &roomhash).unwrap();
        assert!(persist_package_locator(&root, &valid_locator_receipt()).is_err());
        assert!(!outside.join("package-locators").exists());
        fs::remove_file(&roomhash).unwrap();

        fs::create_dir(&roomhash).unwrap();
        let locator_link = roomhash.join("package-locators");
        symlink(&outside, &locator_link).unwrap();
        assert!(persist_package_locator(&root, &valid_locator_receipt()).is_err());
        assert!(!outside.join(format!("{TEST_PACKAGE_DIGEST}.json")).exists());
        fs::remove_file(&locator_link).unwrap();

        let alias = root
            .parent()
            .unwrap()
            .join(format!("package-locator-root-alias-{}", unix_time_millis()));
        symlink(&root, &alias).unwrap();
        assert!(persist_package_locator(&alias, &valid_locator_receipt()).is_err());
        fs::remove_file(alias).unwrap();

        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(outside).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn package_locator_persist_rejects_link_targets_and_atomically_replaces_regular_file() {
        use std::os::unix::fs::symlink;

        let root = test_root("package-locator-persist-replacement");
        let destination = persist_package_locator(&root, &valid_locator_receipt()).unwrap();
        let sentinel = root.join("do-not-overwrite.txt");
        let sentinel_bytes = b"sentinel-content";
        fs::write(&sentinel, sentinel_bytes).unwrap();

        fs::remove_file(&destination).unwrap();
        symlink(&sentinel, &destination).unwrap();
        assert!(persist_package_locator(&root, &valid_locator_receipt()).is_err());
        assert!(
            fs::symlink_metadata(&destination)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read(&sentinel).unwrap(), sentinel_bytes);
        fs::remove_file(&destination).unwrap();

        fs::hard_link(&sentinel, &destination).unwrap();
        assert!(persist_package_locator(&root, &valid_locator_receipt()).is_err());
        assert_eq!(fs::read(&sentinel).unwrap(), sentinel_bytes);
        assert_eq!(fs::read(&destination).unwrap(), sentinel_bytes);
        fs::remove_file(&destination).unwrap();

        persist_package_locator(&root, &valid_locator_receipt()).unwrap();
        let alternate_info_hash = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
        let mut alternate = valid_locator_receipt();
        alternate["info_hash"] = json!(alternate_info_hash);
        alternate["magnet_uri"] = json!(format!(
            "magnet:?xt=urn:btih:{alternate_info_hash}&dn={TEST_PACKAGE_DIGEST}.vibapp-candidate&tr=wss%3A%2F%2Ftracker.webtorrent.dev"
        ));
        persist_package_locator(&root, &alternate).unwrap();
        let locator = read_package_locator(&root, TEST_PACKAGE_DIGEST)
            .unwrap()
            .unwrap();
        assert_eq!(locator["info_hash"], alternate_info_hash);
        assert_eq!(
            fs::metadata(&destination).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let entries = fs::read_dir(destination.parent().unwrap())
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect::<Vec<_>>();
        assert_eq!(entries, vec![destination.file_name().unwrap()]);

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rust_controller_drives_the_real_development_roomhash_host() {
        let root = test_root("real-roomhash-host");
        let config = RoomHashConfig::try_from_value(&json!({
            "upload_limit_bps": 1_048_576,
            "download_limit_bps": 4_194_304,
            "max_conns": 4,
            "dht": false,
            "lsd": false,
            "pex": false,
            "tracker_urls": [],
            "turn": null
        }))
        .unwrap();
        let mut host = RoomHashHost::spawn(&root).unwrap();
        let started = host.start(&config).unwrap();
        assert_eq!(started["started"], true);
        assert_eq!(
            started["implementation"],
            "roomhash-current-headless-webtorrent-3.0.16"
        );
        assert_eq!(started["collaboration"]["available"], true);
        let channel_id = "20000000-0000-4000-8000-000000000001";
        let expires_at = unix_time_millis() + 60_000;
        let grant = json!({
            "schema": COLLABORATION_GRANT_SCHEMA,
            "grant_id": "10000000-0000-4000-8000-000000000001",
            "approved": true,
            "operation": "create",
            "channel_id": channel_id,
            "expires_at": expires_at,
        });
        let created = host
            .collaboration_create(channel_id, expires_at, &grant)
            .unwrap();
        let session_id = created["session_id"].as_str().unwrap().to_string();
        assert_eq!(created["channel_id"], channel_id);
        assert_eq!(
            host.collaboration_send(&session_id, &[1, 2, 3]).unwrap()["accepted_bytes"],
            3
        );
        assert_eq!(host.collaboration_status().unwrap()["session_count"], 1);
        assert_eq!(
            host.collaboration_receive(&session_id, 4).unwrap()["messages"],
            json!([])
        );
        assert!(host.collaboration_receive(&session_id, 5).is_err());
        let replay = host
            .collaboration_create(channel_id, expires_at, &grant)
            .unwrap_err();
        assert!(replay.contains("[grant-replayed]"));
        assert!(host.is_running());
        assert_eq!(
            host.collaboration_leave(&session_id).unwrap()["status"],
            "left"
        );
        assert_eq!(host.status().unwrap()["status"], "running");
        assert_eq!(host.shutdown().unwrap()["status"], "stopped");
        fs::remove_dir_all(root).unwrap();
    }
}
