use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
#[cfg(any(target_os = "macos", target_os = "linux"))]
use std::ffi::CStr;
use std::fs::{self, OpenOptions};
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wasmtime::component::{Component, HasSelf, Linker};
use wasmtime::{Config, Engine, Store, StoreLimits, StoreLimitsBuilder};

mod bindings {
    wasmtime::component::bindgen!({
        world: "service-only-reference",
        path: "../../../wit/experimental-v0/contract.wit",
        additional_derives: [serde::Serialize],
    });
}

mod hybrid_bindings {
    wasmtime::component::bindgen!({
        world: "hybrid-reference",
        path: "../../../wit/experimental-v0/contract.wit",
        additional_derives: [serde::Serialize],
        with: {
            "vibapp:experimental-v0/common@0.0.1": super::bindings::vibapp::experimental_v0::common,
            "vibapp:experimental-v0/ui@0.0.1": super::bindings::vibapp::experimental_v0::ui,
            "vibapp:experimental-v0/settings@0.0.1": super::bindings::vibapp::experimental_v0::settings,
            "vibapp:experimental-v0/clock@0.0.1": super::bindings::vibapp::experimental_v0::clock,
            "vibapp:experimental-v0/scheduler@0.0.1": super::bindings::vibapp::experimental_v0::scheduler,
            "vibapp:experimental-v0/kv@0.0.1": super::bindings::vibapp::experimental_v0::kv,
            "vibapp:experimental-v0/log@0.0.1": super::bindings::vibapp::experimental_v0::log,
            "vibapp:experimental-v0/host-info@0.0.1": super::bindings::vibapp::experimental_v0::host_info,
            "vibapp:experimental-v0/guest@0.0.1": super::bindings::exports::vibapp::experimental_v0::guest,
        },
    });
}

mod ui_bindings {
    wasmtime::component::bindgen!({
        world: "ui-only-reference",
        path: "../../../wit/experimental-v0/contract.wit",
        additional_derives: [serde::Serialize],
        with: {
            "vibapp:experimental-v0/common@0.0.1": super::bindings::vibapp::experimental_v0::common,
            "vibapp:experimental-v0/ui@0.0.1": super::bindings::vibapp::experimental_v0::ui,
            "vibapp:experimental-v0/settings@0.0.1": super::bindings::vibapp::experimental_v0::settings,
            "vibapp:experimental-v0/clock@0.0.1": super::bindings::vibapp::experimental_v0::clock,
            "vibapp:experimental-v0/scheduler@0.0.1": super::bindings::vibapp::experimental_v0::scheduler,
            "vibapp:experimental-v0/kv@0.0.1": super::bindings::vibapp::experimental_v0::kv,
            "vibapp:experimental-v0/log@0.0.1": super::bindings::vibapp::experimental_v0::log,
            "vibapp:experimental-v0/host-info@0.0.1": super::bindings::vibapp::experimental_v0::host_info,
            "vibapp:experimental-v0/guest@0.0.1": super::bindings::exports::vibapp::experimental_v0::guest,
        },
    });
}

use bindings::exports::vibapp::experimental_v0::guest;
use bindings::vibapp::experimental_v0::{
    clock, common, host_info, http, kv, log, scheduler, settings, system_metrics, ui,
};
use hybrid_bindings::vibapp::experimental_v0::notification;

const PROTOCOL_SCHEMA: &str = "vibapp.service-runtime.protocol.experimental-v1";
const INSPECTOR_SCHEMA: &str = "vibapp.component-inspector.protocol.experimental-v1";
const MAX_COMPONENT_BYTES: u64 = 16 * 1024 * 1024;
const MAX_LINEAR_MEMORY_BYTES: usize = 64 * 1024 * 1024;
const MAX_COMMAND_BYTES: usize = 64 * 1024;
const MAX_OUTPUT_BYTES: usize = 256 * 1024;
const MAX_TRIGGER_BYTES: usize = 64 * 1024;
const MAX_PROCESS_MEMORY_BYTES: u64 = 768 * 1024 * 1024;
const EVENT_FUEL_BUDGET: u64 = 10_000_000;
const HEALTH_FUEL_BUDGET: u64 = 20_000_000;
const MIGRATION_FUEL_BUDGET: u64 = 50_000_000;
const EVENT_DEADLINE: Duration = Duration::from_millis(250);
const HEALTH_DEADLINE: Duration = Duration::from_secs(2);
const MIGRATION_DEADLINE: Duration = Duration::from_secs(2);
const EPOCH_TICK: Duration = Duration::from_millis(10);
const KV_SCHEMA: &str = "vibapp.kv-state.experimental-v1";
const MAX_KV_ENTRIES: usize = 4096;
const MAX_KV_KEY_BYTES: usize = 256;
const MAX_KV_VALUE_BYTES: usize = 256 * 1024;
const MAX_KV_OPERATIONS: usize = 256;
const MAX_KV_IDEMPOTENCY_RECORDS: usize = 4096;
const MAX_KV_SCAN_LIMIT: u32 = 1024;
const SETTINGS_SCHEMA: &str = "vibapp.settings-snapshot.experimental-v1";
const MAX_SETTINGS_BYTES: u64 = 256 * 1024;
const MAX_SETTINGS_VALUES: usize = 256;
const MAX_SETTING_KEY_BYTES: usize = 256;
const MAX_SETTING_VALUE_CHARS: usize = 16 * 1024;
const MAX_UI_NODES: usize = 2048;
const MAX_UI_FIELDS: usize = 256;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredKvEntry {
    value: Vec<u8>,
    revision: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredKvTransaction {
    request_sha256: String,
    state_revision: u64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct KvDocument {
    schema_version: String,
    state_revision: u64,
    entries: BTreeMap<String, StoredKvEntry>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    transactions: BTreeMap<String, StoredKvTransaction>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct SettingsDocument {
    schema_version: String,
    app_id: String,
    package_digest_sha256: String,
    generation: String,
    state_revision: u64,
    schema_revision: u64,
    config_revision: u64,
    values: Vec<StoredSettingValue>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredSettingValue {
    key: String,
    value: StoredFieldValue,
}

#[derive(Clone, Deserialize)]
#[serde(tag = "tag", content = "value", rename_all = "kebab-case")]
enum StoredFieldValue {
    Empty,
    Text(String),
    SecretHandle(String),
    Integer(i64),
    Decimal(String),
    Boolean(bool),
    Date(StoredDate),
    Time(StoredTime),
    TimeZone(String),
    Choice(String),
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredDate {
    year: u16,
    month: u8,
    day: u8,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredTime {
    hour: u8,
    minute: u8,
    second: u8,
}

struct SettingsBroker {
    snapshot: settings::SettingsSnapshot,
}

#[derive(Clone)]
struct UiSurfaceState {
    session: String,
    surface: String,
    route: String,
    actions: BTreeMap<String, bool>,
    fields: BTreeMap<String, UiFieldDeclaration>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CanonicalFieldKind {
    Text,
    Integer,
    Decimal,
    Boolean,
    Date,
    Time,
    TimeZone,
    Choice,
}

impl CanonicalFieldKind {
    fn from_ui(kind: &ui::FieldKind) -> Self {
        match kind {
            ui::FieldKind::Text => Self::Text,
            ui::FieldKind::Integer => Self::Integer,
            ui::FieldKind::Decimal => Self::Decimal,
            ui::FieldKind::Boolean => Self::Boolean,
            ui::FieldKind::Date => Self::Date,
            ui::FieldKind::Time => Self::Time,
            ui::FieldKind::TimeZone => Self::TimeZone,
            ui::FieldKind::Choice => Self::Choice,
        }
    }
}

#[derive(Clone)]
struct UiFieldDeclaration {
    kind: CanonicalFieldKind,
    sensitive: bool,
}

struct KvBroker {
    enabled: bool,
    path: PathBuf,
    maximum_bytes: usize,
    document: KvDocument,
    minimum_state_revision: u64,
    migration_target_revision: Option<u64>,
    write_sequence: u64,
    directory: fs::File,
}

impl KvBroker {
    fn load(
        state_directory: &Path,
        state_revision: u64,
        enabled: bool,
        maximum_bytes: usize,
    ) -> Result<Self, String> {
        let metadata = fs::symlink_metadata(state_directory)
            .map_err(|error| format!("cannot inspect state directory: {error}"))?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err("state directory must be a real directory".to_string());
        }
        let state_directory = state_directory
            .canonicalize()
            .map_err(|error| format!("cannot resolve state directory: {error}"))?;
        let directory = open_state_directory(&state_directory)
            .map_err(|error| format!("cannot open state directory: {error}"))?;
        let path = state_directory.join("kv-state.json");
        let document = if enabled && path.exists() {
            let metadata = fs::symlink_metadata(&path)
                .map_err(|error| format!("cannot inspect KV state: {error}"))?;
            if !metadata.file_type().is_file()
                || metadata.file_type().is_symlink()
                || metadata.len() == 0
                || metadata.len() > maximum_bytes as u64
            {
                return Err("KV state must be one bounded regular non-link file".to_string());
            }
            let bytes =
                fs::read(&path).map_err(|error| format!("cannot read KV state: {error}"))?;
            serde_json::from_slice::<KvDocument>(&bytes)
                .map_err(|error| format!("KV state is not canonical typed JSON: {error}"))?
        } else {
            KvDocument {
                schema_version: KV_SCHEMA.to_string(),
                state_revision,
                entries: BTreeMap::new(),
                transactions: BTreeMap::new(),
            }
        };
        if !Self::valid_document(&document, state_revision, true) {
            return Err("KV state schema, revision, or entry bounds are invalid".to_string());
        }
        let mut result = Self {
            enabled,
            path,
            maximum_bytes,
            document,
            minimum_state_revision: state_revision,
            migration_target_revision: None,
            write_sequence: 0,
            directory,
        };
        if enabled && !result.path.exists() {
            let _lock = result.lock()?;
            result.persist()?;
        }
        Ok(result)
    }

    fn valid_document(document: &KvDocument, minimum_revision: u64, exact: bool) -> bool {
        document.schema_version == KV_SCHEMA
            && document.state_revision > 0
            && if exact {
                document.state_revision == minimum_revision
            } else {
                document.state_revision >= minimum_revision
            }
            && document.entries.len() <= MAX_KV_ENTRIES
            && document.transactions.len() <= MAX_KV_IDEMPOTENCY_RECORDS
            && document.entries.iter().all(|(key, entry)| {
                !key.is_empty()
                    && key.len() <= MAX_KV_KEY_BYTES
                    && !key.chars().any(char::is_control)
                    && entry.value.len() <= MAX_KV_VALUE_BYTES
                    && entry.revision > 0
                    && entry.revision <= document.state_revision
            })
            && document.transactions.iter().all(|(key, transaction)| {
                !key.is_empty()
                    && key.len() <= 128
                    && !key.chars().any(char::is_control)
                    && transaction.request_sha256.len() == 64
                    && transaction
                        .request_sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                    && transaction.state_revision > 0
                    && transaction.state_revision <= document.state_revision
            })
    }

    fn lock(&self) -> Result<fs::File, String> {
        let lock_path = self.path.with_file_name("kv-state.lock");
        #[cfg(unix)]
        let file = {
            use std::os::unix::fs::OpenOptionsExt;
            OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&lock_path)
        };
        #[cfg(not(unix))]
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(&lock_path);
        let file = file.map_err(|error| format!("cannot open KV transaction lock: {error}"))?;
        let metadata = file
            .metadata()
            .map_err(|error| format!("cannot inspect KV transaction lock: {error}"))?;
        if !metadata.file_type().is_file() {
            return Err("KV transaction lock is not a regular file".to_string());
        }
        #[cfg(unix)]
        if unsafe { libc::flock(std::os::fd::AsRawFd::as_raw_fd(&file), libc::LOCK_EX) } != 0 {
            return Err(format!(
                "cannot acquire KV transaction lock: {}",
                io::Error::last_os_error()
            ));
        }
        #[cfg(windows)]
        file.lock().map_err(|error| format!("cannot acquire Windows KV lock: {error}"))?;
        Ok(file)
    }

    fn read_document(&self) -> Result<KvDocument, String> {
        let metadata = fs::symlink_metadata(&self.path)
            .map_err(|error| format!("cannot inspect KV state: {error}"))?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() == 0
            || metadata.len() > self.maximum_bytes as u64
        {
            return Err("KV state must be one bounded regular non-link file".to_string());
        }
        let bytes =
            fs::read(&self.path).map_err(|error| format!("cannot read KV state: {error}"))?;
        let document = serde_json::from_slice::<KvDocument>(&bytes)
            .map_err(|error| format!("KV state is not canonical typed JSON: {error}"))?;
        if !Self::valid_document(&document, self.minimum_state_revision, false) {
            return Err("KV state schema, revision, or entry bounds are invalid".to_string());
        }
        Ok(document)
    }

    fn refresh(&mut self) -> Result<(), String> {
        if !self.enabled {
            return Ok(());
        }
        let _lock = self.lock()?;
        self.document = self.read_document()?;
        Ok(())
    }

    fn persist(&mut self) -> Result<(), String> {
        let mut encoded = serde_json::to_vec(&self.document)
            .map_err(|error| format!("cannot encode KV state: {error}"))?;
        encoded.push(b'\n');
        if encoded.len() > self.maximum_bytes {
            return Err("KV state exceeds its manifest storage bound".to_string());
        }
        self.write_sequence = self.write_sequence.saturating_add(1);
        let temporary = self.path.with_file_name(format!(
            ".kv-state-{}-{}.tmp",
            std::process::id(),
            self.write_sequence
        ));
        let write_result = (|| -> Result<(), String> {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)
                .map_err(|error| format!("cannot create KV transaction file: {error}"))?;
            file.write_all(&encoded)
                .map_err(|error| format!("cannot write KV transaction: {error}"))?;
            file.sync_all()
                .map_err(|error| format!("cannot sync KV transaction: {error}"))?;
            fs::rename(&temporary, &self.path)
                .map_err(|error| format!("cannot commit KV transaction: {error}"))?;
            #[cfg(not(windows))]
            let directory = fs::File::open(
                self.path
                    .parent()
                    .ok_or_else(|| "KV state has no parent directory".to_string())?,
            )
            .map_err(|error| format!("cannot open KV state directory: {error}"))?;
            #[cfg(not(windows))]
            return directory
                .sync_all()
                .map_err(|error| format!("cannot sync KV state directory: {error}"));
            #[cfg(windows)]
            Ok(())
        })();
        if write_result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        write_result
    }

    fn begin_migration(&mut self, source: u64, target: u64) -> Result<(), String> {
        if !self.enabled {
            return Ok(());
        }
        self.refresh()?;
        if self.document.state_revision != source || target != source.saturating_add(1) {
            return Err("KV migration is not bound to the copied source revision".to_string());
        }
        self.migration_target_revision = Some(target);
        Ok(())
    }

    fn finish_migration(&mut self, target: u64) -> Result<(), String> {
        if !self.enabled {
            return Ok(());
        }
        if self.migration_target_revision != Some(target) {
            return Err("KV migration target revision is not active".to_string());
        }
        if self.document.state_revision != target {
            let _lock = self.lock()?;
            self.document = self.read_document()?;
            self.document.state_revision = target;
            self.persist()?;
        }
        self.migration_target_revision = None;
        self.minimum_state_revision = target;
        Ok(())
    }

    fn abort_migration(&mut self) {
        self.migration_target_revision = None;
    }

    fn rebind(&mut self, state_directory: &Path) -> Result<(), String> {
        let metadata = fs::symlink_metadata(state_directory)
            .map_err(|error| format!("cannot inspect rebound state directory: {error}"))?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err("rebound state directory is not a real directory".to_string());
        }
        let rebound = open_state_directory(state_directory)
            .map_err(|error| format!("cannot open rebound state directory: {error}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let old = self
                .directory
                .metadata()
                .map_err(|error| format!("cannot inspect original state directory: {error}"))?;
            let new = rebound
                .metadata()
                .map_err(|error| format!("cannot inspect rebound state directory: {error}"))?;
            if old.dev() != new.dev() || old.ino() != new.ino() {
                return Err(
                    "rebound state directory is not the activated candidate inode".to_string(),
                );
            }
        }
        #[cfg(windows)]
        if !windows::same_directory(&self.directory, &rebound)? {
            return Err("rebound state directory is not the activated candidate file ID".to_string());
        }
        #[cfg(not(any(unix, windows)))]
        return Err("secure state rebinding is unavailable on this host".to_string());
        self.directory = rebound;
        self.path = state_directory.join("kv-state.json");
        self.refresh()
    }
}

impl SettingsBroker {
    fn load(
        path: &Path,
        app_id: &str,
        package_digest_sha256: &str,
        generation: &str,
        state_revision: u64,
    ) -> Result<Self, String> {
        let metadata = fs::symlink_metadata(path)
            .map_err(|error| format!("cannot inspect settings snapshot: {error}"))?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() == 0
            || metadata.len() > MAX_SETTINGS_BYTES
        {
            return Err("settings snapshot must be one bounded regular non-link file".to_string());
        }
        let document = serde_json::from_slice::<SettingsDocument>(
            &fs::read(path).map_err(|error| format!("cannot read settings snapshot: {error}"))?,
        )
        .map_err(|error| format!("settings snapshot is not strict typed JSON: {error}"))?;
        if document.schema_version != SETTINGS_SCHEMA
            || document.app_id != app_id
            || document.package_digest_sha256 != package_digest_sha256
            || document.generation != generation
            || document.state_revision != state_revision
            || document.schema_revision == 0
            || document.config_revision == 0
            || document.values.len() > MAX_SETTINGS_VALUES
        {
            return Err("settings snapshot authority binding or revision is invalid".to_string());
        }
        let mut keys = BTreeMap::new();
        let mut values = Vec::with_capacity(document.values.len());
        for item in document.values {
            if item.key.is_empty()
                || item.key.len() > MAX_SETTING_KEY_BYTES
                || item.key.chars().any(char::is_control)
                || keys.insert(item.key.clone(), ()).is_some()
            {
                return Err("settings snapshot contains an invalid or duplicate key".to_string());
            }
            let value = decode_field_value(item.value)?;
            values.push(settings::SettingValue {
                key: item.key,
                value,
            });
        }
        Ok(Self {
            snapshot: settings::SettingsSnapshot {
                schema_revision: document.schema_revision,
                config_revision: document.config_revision,
                values,
            },
        })
    }

    fn text(value: String) -> Result<String, String> {
        if value.len() > MAX_SETTING_VALUE_CHARS || value.chars().any(char::is_control) {
            return Err("settings snapshot contains an out-of-bounds string".to_string());
        }
        Ok(value)
    }
}

fn decode_field_value(value: StoredFieldValue) -> Result<ui::FieldValue, String> {
    Ok(match value {
        StoredFieldValue::Empty => ui::FieldValue::Empty,
        StoredFieldValue::Text(value) => {
            // Text is user content, not an identifier/secret handle. Permit
            // ordinary line breaks and tabs without admitting other controls.
            if value.len() > MAX_SETTING_VALUE_CHARS
                || value.chars().any(|character| character.is_control() && !matches!(character, '\n' | '\r' | '\t'))
            {
                return Err("text field contains an out-of-bounds value".to_string());
            }
            ui::FieldValue::Text(value)
        }
        StoredFieldValue::SecretHandle(value) => {
            ui::FieldValue::SecretHandle(SettingsBroker::text(value)?)
        }
        StoredFieldValue::Integer(value) => ui::FieldValue::Integer(value),
        StoredFieldValue::Decimal(value) => ui::FieldValue::Decimal(SettingsBroker::text(value)?),
        StoredFieldValue::Boolean(value) => ui::FieldValue::Boolean(value),
        StoredFieldValue::Date(value) => {
            if value.year == 0 || !(1..=12).contains(&value.month) || !(1..=31).contains(&value.day)
            {
                return Err("field value contains an invalid date".to_string());
            }
            ui::FieldValue::Date(common::LocalDate {
                year: value.year,
                month: value.month,
                day: value.day,
            })
        }
        StoredFieldValue::Time(value) => {
            if value.hour > 23 || value.minute > 59 || value.second > 59 {
                return Err("field value contains an invalid time".to_string());
            }
            ui::FieldValue::Time(common::LocalTime {
                hour: value.hour,
                minute: value.minute,
                second: value.second,
            })
        }
        StoredFieldValue::TimeZone(value) => ui::FieldValue::TimeZone(SettingsBroker::text(value)?),
        StoredFieldValue::Choice(value) => ui::FieldValue::Choice(SettingsBroker::text(value)?),
    })
}

struct RuntimeState {
    started: Instant,
    limits: StoreLimits,
    kv: KvBroker,
    settings: SettingsBroker,
    ui_surface: Option<UiSurfaceState>,
    // Count attempts, not commits: a failed persistence operation may already
    // have changed durable state. Such a call must never be treated as a clean
    // input rejection, even if the in-memory revision was restored.
    kv_transaction_attempts: u64,
}

/// A descriptor-only Store has no app state, settings snapshot, clock value, or
/// effect broker. Every imported interface is still linked for Component Model
/// type checking, but Guest calls receive only typed unavailable/static results.
struct InspectionState {
    limits: StoreLimits,
}

#[cfg(unix)]
fn apply_process_limits() -> Result<(), String> {
    #[cfg(target_os = "linux")]
    type Resource = libc::__rlimit_resource_t;
    #[cfg(not(target_os = "linux"))]
    type Resource = libc::c_int;
    fn set(resource: Resource, value: u64, name: &str) -> Result<(), String> {
        let mut current = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // SAFETY: `current` is valid writable storage for the synchronous
        // `getrlimit` result.
        if unsafe { libc::getrlimit(resource, &mut current) } != 0 {
            return Err(format!(
                "cannot inspect {name} process limit: {}",
                io::Error::last_os_error()
            ));
        }
        let requested = value as libc::rlim_t;
        let limit = libc::rlimit {
            rlim_cur: current.rlim_cur.min(requested).min(current.rlim_max),
            rlim_max: current.rlim_max,
        };
        // SAFETY: `limit` is a valid in-process rlimit value and `setrlimit`
        // copies it synchronously. No guest pointer or handle crosses here.
        if unsafe { libc::setrlimit(resource, &limit) } != 0 {
            return Err(format!(
                "cannot apply {name} process limit: {}",
                io::Error::last_os_error()
            ));
        }
        Ok(())
    }
    // Darwin rejects lowering RLIMIT_AS/RLIMIT_DATA for this JIT process after
    // dyld mappings exist. The daemon supervisor applies a sampled RSS ceiling
    // there; Linux keeps this hard address-space limit in the child itself.
    #[cfg(not(target_os = "macos"))]
    set(libc::RLIMIT_AS, MAX_PROCESS_MEMORY_BYTES, "address-space")?;
    set(libc::RLIMIT_NOFILE, 64, "file-descriptor")?;
    set(libc::RLIMIT_CORE, 0, "core-dump")
}

#[cfg(windows)]
mod windows;

fn open_state_directory(path: &Path) -> io::Result<fs::File> {
    #[cfg(windows)]
    { windows::open_directory(path) }
    #[cfg(not(windows))]
    { fs::File::open(path) }
}

#[cfg(windows)]
fn apply_process_limits() -> Result<(), String> {
    windows::process_limits(MAX_PROCESS_MEMORY_BYTES)
}

#[cfg(not(any(unix, windows)))]
fn apply_process_limits() -> Result<(), String> {
    Err("OS process limits are unavailable on this runtime build".to_string())
}

impl common::Host for RuntimeState {}
impl ui::Host for RuntimeState {}

fn host_error(code: common::ErrorCode, message: &str, retryable: bool) -> common::HostError {
    common::HostError {
        code,
        message: message.to_string(),
        retryable,
    }
}

fn capability_unavailable(message: &str) -> common::HostError {
    host_error(common::ErrorCode::CapabilityUnavailable, message, false)
}

fn canonical_utc_from_unix_seconds(unix_seconds: i64) -> Result<String, String> {
    const SECONDS_PER_DAY: i64 = 86_400;
    let days = unix_seconds.div_euclid(SECONDS_PER_DAY);
    let seconds_of_day = unix_seconds.rem_euclid(SECONDS_PER_DAY);

    // Proleptic Gregorian conversion from days since 1970-01-01. This pure
    // helper keeps the exact whole-second WIT representation deterministic in
    // tests and independent of process locale or ambient timezone settings.
    let shifted_days = days
        .checked_add(719_468)
        .ok_or_else(|| "UTC day count is outside the supported range".to_string())?;
    let era = shifted_days.div_euclid(146_097);
    let day_of_era = shifted_days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    if !(0..=9_999).contains(&year) {
        return Err("UTC year is outside the four-digit contract range".to_string());
    }
    let hour = seconds_of_day / 3_600;
    let minute = seconds_of_day % 3_600 / 60;
    let second = seconds_of_day % 60;
    let instant = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z");
    if instant.len() != 20 {
        return Err("UTC instant did not satisfy the canonical wire length".to_string());
    }
    Ok(instant)
}

fn current_unix_seconds() -> Result<i64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock is before the Unix epoch".to_string())?;
    i64::try_from(elapsed.as_secs())
        .map_err(|_| "system clock is outside the supported UTC range".to_string())
}

fn zoneinfo_name_from_path(path: &Path) -> Option<String> {
    let value = path.to_string_lossy();
    let (_, candidate) = value.rsplit_once("/zoneinfo/")?;
    let valid = !candidate.is_empty()
        && candidate.len() <= 128
        && candidate.split('/').all(|segment| {
            !segment.is_empty()
                && segment != "."
                && segment != ".."
                && segment
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || "_+-".contains(character))
        });
    valid.then(|| candidate.to_string())
}

fn system_time_zone_name() -> Option<String> {
    ["/etc/localtime", "/var/db/timezone/localtime"]
        .into_iter()
        .filter_map(|path| fs::read_link(path).ok())
        .find_map(|path| zoneinfo_name_from_path(&path))
}

fn offset_time_zone_name(offset_seconds: i32) -> String {
    if offset_seconds == 0 {
        return "UTC".to_string();
    }
    let absolute = i64::from(offset_seconds).abs();
    let sign = if offset_seconds < 0 { '-' } else { '+' };
    let hours = absolute / 3_600;
    let minutes = absolute % 3_600 / 60;
    let seconds = absolute % 60;
    if seconds == 0 {
        format!("UTC{sign}{hours:02}:{minutes:02}")
    } else {
        format!("UTC{sign}{hours:02}:{minutes:02}:{seconds:02}")
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn local_clock_context(unix_seconds: i64) -> Result<(String, i32), String> {
    let raw: libc::time_t = unix_seconds
        .try_into()
        .map_err(|_| "system clock is outside the local-time range".to_string())?;
    // SAFETY: `local` is fully initialized before it is read, `raw` and
    // `local` remain alive for the call, and localtime_r writes only to the
    // supplied libc::tm value.
    let mut local = unsafe { std::mem::zeroed::<libc::tm>() };
    if unsafe { libc::localtime_r(&raw, &mut local) }.is_null() {
        return Err("host local time could not be resolved".to_string());
    }
    let offset_seconds = i32::try_from(local.tm_gmtoff)
        .map_err(|_| "host UTC offset is outside the contract range".to_string())?;
    let abbreviation = if local.tm_zone.is_null() {
        None
    } else {
        // SAFETY: a successful localtime_r call supplies a NUL-terminated,
        // process-owned timezone abbreviation for the lifetime of this call.
        unsafe { CStr::from_ptr(local.tm_zone) }
            .to_str()
            .ok()
            .filter(|value| !value.is_empty() && value.len() <= 32)
            .map(str::to_string)
    };
    let time_zone = system_time_zone_name()
        .or(abbreviation)
        .unwrap_or_else(|| offset_time_zone_name(offset_seconds));
    Ok((time_zone, offset_seconds))
}

#[cfg(windows)]
fn local_clock_context(unix_seconds: i64) -> Result<(String, i32), String> {
    windows::clock_context(unix_seconds)
}

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
fn local_clock_context(_unix_seconds: i64) -> Result<(String, i32), String> {
    Ok(("UTC".to_string(), 0))
}

fn current_wall_clock() -> Result<(String, String, i32), String> {
    let seconds = current_unix_seconds()?;
    let now_utc = canonical_utc_from_unix_seconds(seconds)?;
    let (time_zone, utc_offset_seconds) = local_clock_context(seconds)?;
    Ok((now_utc, time_zone, utc_offset_seconds))
}

impl clock::Host for RuntimeState {
    fn wall_now(&mut self) -> Result<clock::WallClock, common::HostError> {
        let (now_utc, time_zone, utc_offset_seconds) = current_wall_clock()
            .map_err(|message| host_error(common::ErrorCode::Internal, &message, true))?;
        Ok(clock::WallClock {
            now_utc,
            time_zone,
            utc_offset_seconds,
        })
    }

    fn monotonic_now(&mut self) -> u64 {
        self.started
            .elapsed()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX)
    }
}

impl scheduler::Host for RuntimeState {
    fn upsert(
        &mut self,
        _request: scheduler::ScheduleRequest,
    ) -> Result<scheduler::ScheduleRecord, common::HostError> {
        Err(capability_unavailable("scheduler broker is not configured"))
    }

    fn disable(&mut self, _id: String, _key: String) -> Result<(), common::HostError> {
        Err(capability_unavailable("scheduler broker is not configured"))
    }

    fn enable(
        &mut self,
        _id: String,
        _key: String,
    ) -> Result<scheduler::ScheduleRecord, common::HostError> {
        Err(capability_unavailable("scheduler broker is not configured"))
    }

    fn list_schedules(&mut self) -> Result<Vec<scheduler::ScheduleRecord>, common::HostError> {
        Err(capability_unavailable("scheduler broker is not configured"))
    }

    fn acknowledge_occurrence(
        &mut self,
        _occurrence: String,
        _outcome: scheduler::OccurrenceOutcome,
        _key: String,
    ) -> Result<(), common::HostError> {
        Err(capability_unavailable("scheduler broker is not configured"))
    }
}

impl kv::Host for RuntimeState {
    fn get(&mut self, key: String) -> Result<Option<kv::Entry>, common::HostError> {
        if !self.kv.enabled {
            return Err(capability_unavailable("durable KV broker is not granted"));
        }
        if key.is_empty() || key.len() > MAX_KV_KEY_BYTES || key.chars().any(char::is_control) {
            return Err(host_error(
                common::ErrorCode::InvalidArgument,
                "KV key is outside its bound",
                false,
            ));
        }
        self.kv
            .refresh()
            .map_err(|error| host_error(common::ErrorCode::IntegrityFailure, &error, false))?;
        Ok(self.kv.document.entries.get(&key).map(|entry| kv::Entry {
            key,
            value: entry.value.clone(),
            revision: entry.revision,
        }))
    }

    fn scan_prefix(
        &mut self,
        prefix: String,
        limit: u32,
    ) -> Result<Vec<kv::Entry>, common::HostError> {
        if !self.kv.enabled {
            return Err(capability_unavailable("durable KV broker is not granted"));
        }
        if prefix.len() > MAX_KV_KEY_BYTES
            || prefix.chars().any(char::is_control)
            || limit == 0
            || limit > MAX_KV_SCAN_LIMIT
        {
            return Err(host_error(
                common::ErrorCode::InvalidArgument,
                "KV prefix or scan limit is outside its bound",
                false,
            ));
        }
        self.kv
            .refresh()
            .map_err(|error| host_error(common::ErrorCode::IntegrityFailure, &error, false))?;
        Ok(self
            .kv
            .document
            .entries
            .range(prefix.clone()..)
            .take_while(|(key, _entry)| key.starts_with(&prefix))
            .take(limit as usize)
            .map(|(key, entry)| kv::Entry {
                key: key.clone(),
                value: entry.value.clone(),
                revision: entry.revision,
            })
            .collect())
    }

    fn transact(
        &mut self,
        request: kv::TransactionRequest,
    ) -> Result<kv::TransactionResult, common::HostError> {
        self.kv_transaction_attempts = self.kv_transaction_attempts.saturating_add(1);
        if !self.kv.enabled {
            return Err(capability_unavailable("durable KV broker is not granted"));
        }
        if request.idempotency_key.is_empty()
            || request.idempotency_key.len() > 128
            || request.idempotency_key.chars().any(char::is_control)
            || request.operations.is_empty()
            || request.operations.len() > MAX_KV_OPERATIONS
        {
            return Err(host_error(
                common::ErrorCode::InvalidArgument,
                "KV transaction identity or operation count is outside its bound",
                false,
            ));
        }
        let request_bytes = serde_json::to_vec(&request).map_err(|error| {
            host_error(
                common::ErrorCode::MalformedOutput,
                &format!("KV transaction cannot be normalized: {error}"),
                false,
            )
        })?;
        let request_sha256 = format!("{:x}", Sha256::digest(request_bytes));
        let _lock = self
            .kv
            .lock()
            .map_err(|error| host_error(common::ErrorCode::Internal, &error, true))?;
        self.kv.document = self
            .kv
            .read_document()
            .map_err(|error| host_error(common::ErrorCode::IntegrityFailure, &error, false))?;
        if let Some(previous) = self.kv.document.transactions.get(&request.idempotency_key) {
            if previous.request_sha256 != request_sha256 {
                return Err(host_error(
                    common::ErrorCode::Conflict,
                    "KV idempotency key was reused with another transaction",
                    false,
                ));
            }
            return Ok(kv::TransactionResult {
                state_revision: previous.state_revision,
                deduplicated: true,
            });
        }
        if self.kv.document.transactions.len() >= MAX_KV_IDEMPOTENCY_RECORDS {
            return Err(host_error(
                common::ErrorCode::ResourceLimit,
                "KV idempotency ledger is full",
                false,
            ));
        }
        let target_revision = match self.kv.migration_target_revision {
            Some(target) if self.kv.document.state_revision <= target => target,
            Some(_) => {
                return Err(host_error(
                    common::ErrorCode::StaleRevision,
                    "KV migration target revision is stale",
                    false,
                ));
            }
            None => self
                .kv
                .document
                .state_revision
                .checked_add(1)
                .ok_or_else(|| {
                    host_error(
                        common::ErrorCode::ResourceLimit,
                        "KV state revision is exhausted",
                        false,
                    )
                })?,
        };
        let mut next = self.kv.document.clone();
        for operation in request.operations {
            match operation {
                kv::WriteOperation::Put(operation) => {
                    if operation.key.is_empty()
                        || operation.key.len() > MAX_KV_KEY_BYTES
                        || operation.key.chars().any(char::is_control)
                        || operation.value.len() > MAX_KV_VALUE_BYTES
                    {
                        return Err(host_error(
                            common::ErrorCode::ResourceLimit,
                            "KV put exceeds its key or value bound",
                            false,
                        ));
                    }
                    if operation.expected_revision.is_some_and(|expected| {
                        next.entries.get(&operation.key).map(|entry| entry.revision)
                            != Some(expected)
                    }) {
                        return Err(host_error(
                            common::ErrorCode::StaleRevision,
                            "KV put expected revision does not match",
                            false,
                        ));
                    }
                    next.entries.insert(
                        operation.key,
                        StoredKvEntry {
                            value: operation.value,
                            revision: target_revision,
                        },
                    );
                }
                kv::WriteOperation::Delete(operation) => {
                    if operation.key.is_empty()
                        || operation.key.len() > MAX_KV_KEY_BYTES
                        || operation.key.chars().any(char::is_control)
                    {
                        return Err(host_error(
                            common::ErrorCode::InvalidArgument,
                            "KV delete key is outside its bound",
                            false,
                        ));
                    }
                    if operation.expected_revision.is_some_and(|expected| {
                        next.entries.get(&operation.key).map(|entry| entry.revision)
                            != Some(expected)
                    }) {
                        return Err(host_error(
                            common::ErrorCode::StaleRevision,
                            "KV delete expected revision does not match",
                            false,
                        ));
                    }
                    next.entries.remove(&operation.key);
                }
            }
        }
        if next.entries.len() > MAX_KV_ENTRIES {
            return Err(host_error(
                common::ErrorCode::ResourceLimit,
                "KV transaction exceeds the entry count bound",
                false,
            ));
        }
        next.state_revision = target_revision;
        next.transactions.insert(
            request.idempotency_key,
            StoredKvTransaction {
                request_sha256,
                state_revision: target_revision,
            },
        );
        let previous = std::mem::replace(&mut self.kv.document, next);
        if let Err(error) = self.kv.persist() {
            self.kv.document = previous;
            return Err(host_error(common::ErrorCode::Internal, &error, true));
        }
        self.kv.minimum_state_revision = target_revision;
        Ok(kv::TransactionResult {
            state_revision: target_revision,
            deduplicated: false,
        })
    }
}

impl log::Host for RuntimeState {
    fn write(
        &mut self,
        _level: log::Level,
        _message: String,
        _fields: Vec<log::Field>,
        _idempotency_key: String,
    ) -> Result<(), common::HostError> {
        Ok(())
    }
}

fn host_capability_states(kv_enabled: bool) -> Vec<host_info::CapabilityState> {
    [
        ("clock", host_info::Availability::Brokered, true),
        ("log", host_info::Availability::Brokered, true),
        ("host-info", host_info::Availability::Brokered, true),
        ("settings", host_info::Availability::Brokered, true),
        ("scheduler", host_info::Availability::Unavailable, false),
        ("notification", host_info::Availability::Unavailable, false),
        (
            "kv",
            if kv_enabled {
                host_info::Availability::Brokered
            } else {
                host_info::Availability::Unavailable
            },
            kv_enabled,
        ),
        (
            "system-metrics",
            host_info::Availability::Unavailable,
            false,
        ),
        ("http", host_info::Availability::Unavailable, false),
    ]
    .into_iter()
    .map(|(name, availability, granted)| host_info::CapabilityState {
        interface_name: name.to_string(),
        availability,
        granted,
        detail: if granted {
            "bounded daemon broker".to_string()
        } else {
            "capability is linked as a typed unavailable stub".to_string()
        },
    })
    .collect()
}

impl host_info::Host for RuntimeState {
    fn describe_host(&mut self) -> host_info::HostDescription {
        host_info::HostDescription {
            profile: common::ExecutionProfile::Desktop,
            background: host_info::BackgroundReliability::Daemon,
            capabilities: host_capability_states(self.kv.enabled),
        }
    }
}

impl settings::Host for RuntimeState {
    fn current(&mut self) -> Result<settings::SettingsSnapshot, common::HostError> {
        Ok(self.settings.snapshot.clone())
    }
}

impl system_metrics::Host for RuntimeState {
    fn read(
        &mut self,
        _metrics: Vec<system_metrics::MetricName>,
    ) -> Result<Vec<system_metrics::Sample>, common::HostError> {
        Err(capability_unavailable(
            "system metrics broker is not configured",
        ))
    }
}

impl http::Host for RuntimeState {
    fn send(&mut self, _request: http::Request) -> Result<http::Response, common::HostError> {
        Err(capability_unavailable("live HTTP is unavailable"))
    }
}

impl notification::Host for RuntimeState {
    fn show(
        &mut self,
        _request: notification::NotificationRequest,
    ) -> Result<notification::NotificationReceipt, common::HostError> {
        Err(capability_unavailable(
            "notification broker is not configured",
        ))
    }
}

impl common::Host for InspectionState {}
impl ui::Host for InspectionState {}

impl clock::Host for InspectionState {
    fn wall_now(&mut self) -> Result<clock::WallClock, common::HostError> {
        Err(capability_unavailable(
            "clock is unavailable during descriptor inspection",
        ))
    }

    fn monotonic_now(&mut self) -> u64 {
        0
    }
}

impl scheduler::Host for InspectionState {
    fn upsert(
        &mut self,
        _request: scheduler::ScheduleRequest,
    ) -> Result<scheduler::ScheduleRecord, common::HostError> {
        Err(capability_unavailable(
            "scheduler is unavailable during descriptor inspection",
        ))
    }

    fn disable(&mut self, _id: String, _key: String) -> Result<(), common::HostError> {
        Err(capability_unavailable(
            "scheduler is unavailable during descriptor inspection",
        ))
    }

    fn enable(
        &mut self,
        _id: String,
        _key: String,
    ) -> Result<scheduler::ScheduleRecord, common::HostError> {
        Err(capability_unavailable(
            "scheduler is unavailable during descriptor inspection",
        ))
    }

    fn list_schedules(&mut self) -> Result<Vec<scheduler::ScheduleRecord>, common::HostError> {
        Err(capability_unavailable(
            "scheduler is unavailable during descriptor inspection",
        ))
    }

    fn acknowledge_occurrence(
        &mut self,
        _occurrence: String,
        _outcome: scheduler::OccurrenceOutcome,
        _key: String,
    ) -> Result<(), common::HostError> {
        Err(capability_unavailable(
            "scheduler is unavailable during descriptor inspection",
        ))
    }
}

impl kv::Host for InspectionState {
    fn get(&mut self, _key: String) -> Result<Option<kv::Entry>, common::HostError> {
        Err(capability_unavailable(
            "KV is unavailable during descriptor inspection",
        ))
    }

    fn scan_prefix(
        &mut self,
        _prefix: String,
        _limit: u32,
    ) -> Result<Vec<kv::Entry>, common::HostError> {
        Err(capability_unavailable(
            "KV is unavailable during descriptor inspection",
        ))
    }

    fn transact(
        &mut self,
        _request: kv::TransactionRequest,
    ) -> Result<kv::TransactionResult, common::HostError> {
        Err(capability_unavailable(
            "KV is unavailable during descriptor inspection",
        ))
    }
}

impl log::Host for InspectionState {
    fn write(
        &mut self,
        _level: log::Level,
        _message: String,
        _fields: Vec<log::Field>,
        _idempotency_key: String,
    ) -> Result<(), common::HostError> {
        Err(capability_unavailable(
            "log is unavailable during descriptor inspection",
        ))
    }
}

impl host_info::Host for InspectionState {
    fn describe_host(&mut self) -> host_info::HostDescription {
        let capabilities = [
            "clock",
            "scheduler",
            "notification",
            "kv",
            "log",
            "host-info",
            "settings",
            "system-metrics",
            "http",
        ]
        .into_iter()
        .map(|interface_name| host_info::CapabilityState {
            interface_name: interface_name.to_string(),
            availability: host_info::Availability::Unavailable,
            granted: false,
            detail: "describe-only inspection; no live host authority".to_string(),
        })
        .collect();
        host_info::HostDescription {
            profile: common::ExecutionProfile::Desktop,
            background: host_info::BackgroundReliability::NotApplicable,
            capabilities,
        }
    }
}

impl settings::Host for InspectionState {
    fn current(&mut self) -> Result<settings::SettingsSnapshot, common::HostError> {
        Err(capability_unavailable(
            "settings are unavailable during descriptor inspection",
        ))
    }
}

impl system_metrics::Host for InspectionState {
    fn read(
        &mut self,
        _metrics: Vec<system_metrics::MetricName>,
    ) -> Result<Vec<system_metrics::Sample>, common::HostError> {
        Err(capability_unavailable(
            "system metrics are unavailable during descriptor inspection",
        ))
    }
}

impl http::Host for InspectionState {
    fn send(&mut self, _request: http::Request) -> Result<http::Response, common::HostError> {
        Err(capability_unavailable(
            "HTTP is unavailable during descriptor inspection",
        ))
    }
}

impl notification::Host for InspectionState {
    fn show(
        &mut self,
        _request: notification::NotificationRequest,
    ) -> Result<notification::NotificationReceipt, common::HostError> {
        Err(capability_unavailable(
            "notification is unavailable during descriptor inspection",
        ))
    }
}

#[derive(Clone)]
struct Arguments {
    component: PathBuf,
    expected_sha256: String,
    package_digest_sha256: String,
    app_id: String,
    app_version: String,
    entrypoint: String,
    entrypoint_kind: String,
    instance: String,
    generation: String,
    runtime_world: String,
    state_directory: PathBuf,
    state_revision: u64,
    kv_enabled: bool,
    kv_maximum_bytes: usize,
    settings_snapshot: PathBuf,
}

fn next_argument(
    arguments: &mut impl Iterator<Item = std::ffi::OsString>,
    flag: &str,
) -> Result<String, String> {
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new(flag)) {
        return Err(format!("missing {flag}"));
    }
    arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .filter(|value| {
            !value.is_empty() && value.len() <= 512 && !value.chars().any(char::is_control)
        })
        .ok_or_else(|| format!("invalid {flag}"))
}

fn arguments() -> Result<Arguments, String> {
    let mut values = env::args_os().skip(1);
    let component = PathBuf::from(next_argument(&mut values, "--component")?);
    let expected_sha256 = next_argument(&mut values, "--expected-sha256")?;
    let package_digest_sha256 = next_argument(&mut values, "--package-digest-sha256")?;
    let app_id = next_argument(&mut values, "--app-id")?;
    let app_version = next_argument(&mut values, "--app-version")?;
    let entrypoint = next_argument(&mut values, "--entrypoint")?;
    let entrypoint_kind = next_argument(&mut values, "--entrypoint-kind")?;
    let instance = next_argument(&mut values, "--instance")?;
    let generation = next_argument(&mut values, "--generation")?;
    let runtime_world = next_argument(&mut values, "--world")?;
    let state_directory = PathBuf::from(next_argument(&mut values, "--state-directory")?);
    let state_revision = next_argument(&mut values, "--state-revision")?
        .parse::<u64>()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| "invalid --state-revision".to_string())?;
    let kv_enabled = match next_argument(&mut values, "--kv-enabled")?.as_str() {
        "true" => true,
        "false" => false,
        _ => return Err("invalid --kv-enabled".to_string()),
    };
    let kv_maximum_bytes = next_argument(&mut values, "--kv-maximum-bytes")?
        .parse::<usize>()
        .ok()
        .filter(|value| *value > 0 && *value <= 10 * 1024 * 1024)
        .ok_or_else(|| "invalid --kv-maximum-bytes".to_string())?;
    let settings_snapshot = PathBuf::from(next_argument(&mut values, "--settings-snapshot")?);
    if values.next().is_some() {
        return Err("unexpected runtime argument".to_string());
    }
    if entrypoint_kind != "service" && entrypoint_kind != "launcher-ui" {
        return Err("invalid --entrypoint-kind".to_string());
    }
    if [expected_sha256.as_str(), package_digest_sha256.as_str()]
        .into_iter()
        .any(|digest| {
            digest.len() != 64
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
    {
        return Err("runtime digest binding is not lowercase SHA-256".to_string());
    }
    Ok(Arguments {
        component,
        expected_sha256,
        package_digest_sha256,
        app_id,
        app_version,
        entrypoint,
        entrypoint_kind,
        instance,
        generation,
        runtime_world,
        state_directory,
        state_revision,
        kv_enabled,
        kv_maximum_bytes,
        settings_snapshot,
    })
}

struct InspectionArguments {
    component: PathBuf,
    expected_sha256: String,
    runtime_world: String,
}

fn lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn inspection_arguments() -> Result<InspectionArguments, String> {
    let mut values = env::args_os().skip(1);
    if values.next().as_deref() != Some(std::ffi::OsStr::new("--inspect-descriptor")) {
        return Err("missing --inspect-descriptor".to_string());
    }
    let component = PathBuf::from(next_argument(&mut values, "--component")?);
    let expected_sha256 = next_argument(&mut values, "--expected-sha256")?;
    let runtime_world = next_argument(&mut values, "--world")?;
    if values.next().is_some() {
        return Err("unexpected descriptor inspection argument".to_string());
    }
    if !lowercase_sha256(&expected_sha256) {
        return Err("inspection digest binding is not lowercase SHA-256".to_string());
    }
    if !matches!(
        runtime_world.as_str(),
        "ui-only-reference" | "service-only-reference" | "hybrid-reference"
    ) {
        return Err("inspection world is unsupported".to_string());
    }
    Ok(InspectionArguments {
        component,
        expected_sha256,
        runtime_world,
    })
}

fn bounded_descriptor_text(value: String, field: &str, maximum: usize) -> Result<String, String> {
    if value.is_empty() || value.chars().count() > maximum || value.chars().any(char::is_control) {
        return Err(format!("Guest descriptor {field} is outside its bound"));
    }
    Ok(value)
}

fn descriptor_json(
    id: String,
    version: String,
    kind: &'static str,
    display_name: String,
    entrypoints: Vec<(String, &'static str, String, Option<String>)>,
) -> Result<Value, String> {
    if entrypoints.is_empty() || entrypoints.len() > 16 {
        return Err("Guest descriptor entrypoint count is outside 1..16".to_string());
    }
    let mut rows = Vec::with_capacity(entrypoints.len());
    for (entrypoint_id, entrypoint_kind, label, initial_route) in entrypoints {
        let initial_route = initial_route
            .map(|route| bounded_descriptor_text(route, "initial route", 128))
            .transpose()?;
        rows.push(json!({
            "id": bounded_descriptor_text(entrypoint_id, "entrypoint id", 128)?,
            "kind": entrypoint_kind,
            "label": bounded_descriptor_text(label, "entrypoint label", 80)?,
            "initial_route": initial_route,
        }));
    }
    Ok(json!({
        "id": bounded_descriptor_text(id, "app id", 128)?,
        "version": bounded_descriptor_text(version, "version", 128)?,
        "kind": kind,
        "display_name": bounded_descriptor_text(display_name, "display name", 80)?,
        "entrypoints": rows,
    }))
}

enum RuntimeBindings {
    Service(bindings::ServiceOnlyReference),
    Hybrid(hybrid_bindings::HybridReference),
    Ui(ui_bindings::UiOnlyReference),
}

fn validate_descriptor(
    runtime_bindings: &RuntimeBindings,
    store: &mut Store<RuntimeState>,
    arguments: &Arguments,
) -> Result<(), String> {
    let (descriptor_id, descriptor_version, descriptor_kind, descriptor_has_entrypoint) =
        match runtime_bindings {
            RuntimeBindings::Service(runtime) => {
                let descriptor = runtime
                    .vibapp_experimental_v0_guest()
                    .call_describe(store)
                    .map_err(|error| format!("describe trapped: {error}"))?
                    .map_err(|error| {
                        format!(
                            "guest rejected describe: {}",
                            bounded_message(&error.message)
                        )
                    })?;
                (
                    descriptor.id,
                    descriptor.version,
                    matches!(descriptor.kind, common::AppKind::Service),
                    descriptor.entrypoints.iter().any(|item| {
                        item.id == arguments.entrypoint
                            && arguments.entrypoint_kind == "service"
                            && matches!(item.kind, common::EntrypointKind::Service)
                    }),
                )
            }
            RuntimeBindings::Hybrid(runtime) => {
                let descriptor = runtime
                    .vibapp_experimental_v0_guest()
                    .call_describe(store)
                    .map_err(|error| format!("describe trapped: {error}"))?
                    .map_err(|error| {
                        format!(
                            "guest rejected describe: {}",
                            bounded_message(&error.message)
                        )
                    })?;
                use hybrid_bindings::exports::vibapp::experimental_v0::guest::{
                    AppKind, EntrypointKind,
                };
                (
                    descriptor.id,
                    descriptor.version,
                    matches!(descriptor.kind, AppKind::Hybrid),
                    descriptor.entrypoints.iter().any(|item| {
                        item.id == arguments.entrypoint
                            && matches!(
                                (arguments.entrypoint_kind.as_str(), &item.kind),
                                ("service", EntrypointKind::Service)
                                    | ("launcher-ui", EntrypointKind::LauncherUi)
                            )
                    }),
                )
            }
            RuntimeBindings::Ui(runtime) => {
                let descriptor = runtime
                    .vibapp_experimental_v0_guest()
                    .call_describe(store)
                    .map_err(|error| format!("describe trapped: {error}"))?
                    .map_err(|error| {
                        format!(
                            "guest rejected describe: {}",
                            bounded_message(&error.message)
                        )
                    })?;
                use ui_bindings::exports::vibapp::experimental_v0::guest::{
                    AppKind, EntrypointKind,
                };
                (
                    descriptor.id,
                    descriptor.version,
                    matches!(descriptor.kind, AppKind::Ui),
                    descriptor.entrypoints.iter().any(|item| {
                        item.id == arguments.entrypoint
                            && arguments.entrypoint_kind == "launcher-ui"
                            && matches!(item.kind, EntrypointKind::LauncherUi)
                    }),
                )
            }
        };
    if descriptor_id != arguments.app_id || descriptor_version != arguments.app_version {
        return Err("guest descriptor does not match installed app identity".to_string());
    }
    if !descriptor_kind || !descriptor_has_entrypoint {
        return Err("guest descriptor does not declare the requested entrypoint".to_string());
    }
    Ok(())
}

fn component_bytes(path: &Path, expected: &str) -> Result<Vec<u8>, String> {
    let metadata =
        fs::symlink_metadata(path).map_err(|error| format!("cannot inspect component: {error}"))?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_COMPONENT_BYTES
    {
        return Err("component must be one bounded regular non-link file".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("cannot read component: {error}"))?;
    let actual = format!("{:x}", Sha256::digest(&bytes));
    if actual != expected {
        return Err(format!(
            "component digest mismatch: expected {expected}, got {actual}"
        ));
    }
    Ok(bytes)
}

fn error_code(code: &common::ErrorCode) -> &'static str {
    match code {
        common::ErrorCode::InvalidArgument => "invalid-argument",
        common::ErrorCode::IncompatibleContract => "incompatible-contract",
        common::ErrorCode::UnsupportedVersion => "unsupported-version",
        common::ErrorCode::UnknownInterface => "unknown-interface",
        common::ErrorCode::MissingInterface => "missing-interface",
        common::ErrorCode::PermissionDenied => "permission-denied",
        common::ErrorCode::ConsentRequired => "consent-required",
        common::ErrorCode::CapabilityUnavailable => "capability-unavailable",
        common::ErrorCode::UnsupportedSurface => "unsupported-surface",
        common::ErrorCode::ResourceLimit => "resource-limit",
        common::ErrorCode::DeadlineExceeded => "deadline-exceeded",
        common::ErrorCode::Cancelled => "cancelled",
        common::ErrorCode::AppDisabled => "app-disabled",
        common::ErrorCode::AppUninstalled => "app-uninstalled",
        common::ErrorCode::UpgradeInProgress => "upgrade-in-progress",
        common::ErrorCode::IntegrityFailure => "integrity-failure",
        common::ErrorCode::NotFound => "not-found",
        common::ErrorCode::Conflict => "conflict",
        common::ErrorCode::StaleRevision => "stale-revision",
        common::ErrorCode::MalformedOutput => "malformed-output",
        common::ErrorCode::ForgedIdentifier => "forged-identifier",
        common::ErrorCode::Internal => "internal",
    }
}

fn bounded_message(message: &str) -> String {
    message
        .chars()
        .filter(|character| !character.is_control())
        .take(512)
        .collect()
}

fn response_error(request_id: &str, code: &str, message: &str, retryable: bool) -> Value {
    json!({
        "schema_version": PROTOCOL_SCHEMA,
        "request_id": request_id,
        "error": {"code": code, "message": bounded_message(message), "retryable": retryable}
    })
}

fn guest_error(request_id: &str, error: &common::AppError) -> Value {
    response_error(
        request_id,
        error_code(&error.code),
        &error.message,
        error.retryable,
    )
}

fn ui_guest_error(
    request_id: &str,
    error: &common::AppError,
    operation: &str,
    context: &common::CallContext,
    binding: (&str, &str, &str),
    before: (u64, u64),
    after: (u64, u64),
) -> Value {
    let mut response = guest_error(request_id, error);
    // This marker is authored by the trusted host, never supplied by the Guest.
    // Other live effect brokers must participate in this check if introduced;
    // currently KV is the only non-read-only application effect broker.
    if operation == "UI action"
        && matches!(error.code, common::ErrorCode::InvalidArgument)
        && before == after
        && before.0 > 0
        && before.1 < u64::MAX
    {
        response["ui_input_rejection"] = json!({
            "origin": "typed-guest-ui-action",
            "generation": context.generation,
            "session": binding.0,
            "surface": binding.1,
            "route": binding.2,
            "event_id": context.event_id,
            "state_revision": after.0,
        });
    }
    response
}

fn response_is_fatal(response: &Value) -> bool {
    response.get("error").is_some() && response.get("ui_input_rejection").is_none()
}

fn runtime_error(request_id: &str, operation: &str, error: impl std::fmt::Display) -> Value {
    let detail = error.to_string();
    let code = if detail.contains("interrupt") || detail.contains("fuel") {
        "deadline-exceeded"
    } else {
        "internal"
    };
    response_error(
        request_id,
        code,
        &format!("{operation} trapped: {detail}"),
        false,
    )
}

fn validate_event_output<T: Serialize>(
    surfaces: usize,
    diagnostic: Option<&String>,
    output: &T,
) -> Result<(), String> {
    if surfaces != 0 {
        return Err("service event attempted to create a launcher surface".to_string());
    }
    if diagnostic.is_some_and(|value| value.chars().count() > 4096) {
        return Err("service diagnostic exceeds 4096 characters".to_string());
    }
    let bytes = serde_json::to_vec(output).map_err(|error| error.to_string())?;
    if bytes.len() > MAX_OUTPUT_BYTES {
        return Err("service event output exceeds 256 KiB".to_string());
    }
    Ok(())
}

fn command_text(command: &Value, key: &str, maximum: usize) -> Result<String, String> {
    command
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| {
            !value.is_empty() && value.len() <= maximum && !value.chars().any(char::is_control)
        })
        .map(str::to_string)
        .ok_or_else(|| format!("invalid bounded {key}"))
}

fn validate_ui_output<T: Serialize>(
    surfaces: &[ui::SurfaceUpdate],
    diagnostic: Option<&String>,
    output: &T,
    session: &str,
    surface: &str,
    route: &str,
) -> Result<UiSurfaceState, String> {
    if surfaces.len() != 1 {
        return Err("UI event must return exactly one bound surface update".to_string());
    }
    if diagnostic.is_some_and(|value| value.chars().count() > 4096) {
        return Err("UI diagnostic exceeds 4096 characters".to_string());
    }
    let update = &surfaces[0];
    if update.session != session || update.surface != surface || update.route != route {
        return Err("UI output forged or changed its session/surface/route binding".to_string());
    }
    if update.view.nodes.is_empty() || update.view.nodes.len() > MAX_UI_NODES {
        return Err("UI semantic tree node count is outside its bound".to_string());
    }
    let mut nodes = BTreeMap::new();
    let mut actions = BTreeMap::new();
    let mut fields = BTreeMap::new();
    for node in &update.view.nodes {
        if node.id.is_empty()
            || node.id.len() > 128
            || node.id.chars().any(char::is_control)
            || nodes.insert(node.id.clone(), node.parent.clone()).is_some()
        {
            return Err("UI semantic tree has an invalid or duplicate node id".to_string());
        }
        match &node.kind {
            ui::NodeKind::Button(button) => {
                if button.action.is_empty()
                    || button.action.len() > 128
                    || button.action.chars().any(char::is_control)
                    || actions
                        .insert(button.action.clone(), !button.disabled)
                        .is_some()
                {
                    return Err("UI semantic tree has an invalid or duplicate action".to_string());
                }
            }
            ui::NodeKind::Field(field) => {
                if field.field.is_empty()
                    || field.field.len() > 128
                    || field.field.chars().any(char::is_control)
                    || fields
                        .insert(
                            field.field.clone(),
                            UiFieldDeclaration {
                                kind: CanonicalFieldKind::from_ui(&field.kind),
                                sensitive: field.sensitive,
                            },
                        )
                        .is_some()
                {
                    return Err("UI semantic tree has an invalid or duplicate field".to_string());
                }
            }
            ui::NodeKind::Confirmation(confirmation) => {
                for action in [&confirmation.confirm_action, &confirmation.cancel_action] {
                    if action.is_empty()
                        || action.len() > 128
                        || action.chars().any(char::is_control)
                        || actions.insert(action.clone(), true).is_some()
                    {
                        return Err(
                            "UI semantic tree has an invalid or duplicate confirmation action"
                                .to_string(),
                        );
                    }
                }
            }
            _ => {}
        }
    }
    if !nodes.contains_key(&update.view.root) {
        return Err("UI semantic tree root is missing".to_string());
    }
    for (node, parent) in &nodes {
        let mut cursor = parent.as_ref();
        let mut depth = 0usize;
        while let Some(parent_id) = cursor {
            if parent_id == node || depth >= MAX_UI_NODES {
                return Err("UI semantic tree contains a cycle".to_string());
            }
            cursor = nodes
                .get(parent_id)
                .ok_or_else(|| "UI semantic tree parent is missing".to_string())?
                .as_ref();
            depth += 1;
        }
    }
    let bytes = serde_json::to_vec(output).map_err(|error| error.to_string())?;
    if bytes.len() > MAX_OUTPUT_BYTES {
        return Err("UI event output exceeds 256 KiB".to_string());
    }
    Ok(UiSurfaceState {
        session: session.to_string(),
        surface: surface.to_string(),
        route: route.to_string(),
        actions,
        fields,
    })
}

fn validate_action_field_value(
    declaration: &UiFieldDeclaration,
    value: &ui::FieldValue,
) -> Result<(), String> {
    if declaration.sensitive {
        return match value {
            ui::FieldValue::SecretHandle(handle) if !handle.is_empty() => Ok(()),
            ui::FieldValue::SecretHandle(_) => {
                Err("sensitive action field requires a non-empty opaque secret handle".to_string())
            }
            _ => Err("sensitive action field requires an opaque secret handle".to_string()),
        };
    }

    let actual = match value {
        ui::FieldValue::Empty => return Ok(()),
        ui::FieldValue::Text(_) => CanonicalFieldKind::Text,
        ui::FieldValue::SecretHandle(_) => {
            return Err("non-sensitive action field cannot carry a secret handle".to_string());
        }
        ui::FieldValue::Integer(_) => CanonicalFieldKind::Integer,
        ui::FieldValue::Decimal(_) => CanonicalFieldKind::Decimal,
        ui::FieldValue::Boolean(_) => CanonicalFieldKind::Boolean,
        ui::FieldValue::Date(_) => CanonicalFieldKind::Date,
        ui::FieldValue::Time(_) => CanonicalFieldKind::Time,
        ui::FieldValue::TimeZone(_) => CanonicalFieldKind::TimeZone,
        ui::FieldValue::Choice(_) => CanonicalFieldKind::Choice,
    };
    if actual != declaration.kind {
        return Err("action field type does not match the trusted surface declaration".to_string());
    }
    Ok(())
}

fn parse_action_fields(
    command: &Value,
    current: &UiSurfaceState,
) -> Result<(String, Vec<ui::FieldChange>), String> {
    let action = command_text(command, "action", 128)?;
    if current.actions.get(&action) != Some(&true) {
        return Err("action is stale, disabled, or absent from the trusted surface".to_string());
    }
    let items = command
        .get("fields")
        .and_then(Value::as_array)
        .filter(|items| items.len() <= MAX_UI_FIELDS)
        .ok_or_else(|| "action fields exceed their bound".to_string())?;
    let mut seen = BTreeMap::new();
    let mut fields = Vec::with_capacity(items.len());
    for item in items {
        let object = item
            .as_object()
            .filter(|value| {
                value.len() == 2 && value.contains_key("field") && value.contains_key("value")
            })
            .ok_or_else(|| "action field is not a strict field/value record".to_string())?;
        let field = object
            .get("field")
            .and_then(Value::as_str)
            .filter(|value| {
                !value.is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)
            })
            .ok_or_else(|| "action field id is invalid".to_string())?
            .to_string();
        let declaration = current
            .fields
            .get(&field)
            .ok_or_else(|| "action field is absent from the trusted surface".to_string())?;
        if seen.insert(field.clone(), ()).is_some() {
            return Err("action contains a duplicate field".to_string());
        }
        let stored = serde_json::from_value::<StoredFieldValue>(
            object
                .get("value")
                .cloned()
                .ok_or_else(|| "action field value is missing".to_string())?,
        )
        .map_err(|_| "action field value is not a canonical tagged value".to_string())?;
        let value = decode_field_value(stored)?;
        validate_action_field_value(declaration, &value)?;
        fields.push(ui::FieldChange { field, value });
    }
    Ok((action, fields))
}

macro_rules! call_event {
    ($api:expr, $store:expr, $context:expr, $event:expr, $request_id:expr, $operation:expr, $outcome_tag:expr) => {{
        let mut response = match $api.call_handle_event(&mut *($store), $context, &$event) {
            Ok(Ok(output)) => match validate_event_output(output.surfaces.len(), output.diagnostic.as_ref(), &output) {
                Ok(()) => json!({"schema_version": PROTOCOL_SCHEMA, "request_id": $request_id, "outcome": {"tag": $outcome_tag, "diagnostic": output.diagnostic}}),
                Err(error) => response_error($request_id, "resource-limit", &error, false),
            },
            Ok(Err(error)) => guest_error($request_id, &error),
            Err(error) => runtime_error($request_id, $operation, error),
        };
        if let Some(outcome) = response.get_mut("outcome").and_then(Value::as_object_mut) {
            outcome.insert(
                "state_revision".to_string(),
                json!(($store).data().kv.document.state_revision),
            );
        }
        response
    }};
}

macro_rules! call_ui_event {
    ($api:expr, $store:expr, $context:expr, $event:expr, $request_id:expr, $operation:expr, $session:expr, $surface:expr, $route:expr) => {{
        let before = (
            ($store).data().kv.document.state_revision,
            ($store).data().kv_transaction_attempts,
        );
        match $api.call_handle_event(&mut *($store), $context, &$event) {
            Ok(Ok(output)) => match validate_ui_output(
                &output.surfaces,
                output.diagnostic.as_ref(),
                &output,
                $session,
                $surface,
                $route,
            ) {
                Ok(binding) => {
                    ($store).data_mut().ui_surface = Some(binding);
                    json!({
                        "schema_version": PROTOCOL_SCHEMA,
                        "request_id": $request_id,
                        "outcome": {
                            "tag": "ui-updated",
                            "surfaces": output.surfaces,
                            "diagnostic": output.diagnostic,
                            "state_revision": ($store).data().kv.document.state_revision,
                        }
                    })
                }
                Err(error) => response_error($request_id, "malformed-output", &error, false),
            },
            Ok(Err(error)) => ui_guest_error(
                $request_id,
                &error,
                $operation,
                $context,
                ($session, $surface, $route),
                before,
                (
                    ($store).data().kv.document.state_revision,
                    ($store).data().kv_transaction_attempts,
                ),
            ),
            Err(error) => runtime_error($request_id, $operation, error),
        }
    }};
}

fn bind_state_revision(mut response: Value, store: &Store<RuntimeState>) -> Value {
    if let Some(outcome) = response.get_mut("outcome").and_then(Value::as_object_mut) {
        outcome.insert(
            "state_revision".to_string(),
            json!(store.data().kv.document.state_revision),
        );
    }
    response
}

fn health_value(report: &guest::HealthReport) -> Result<Value, String> {
    if report.checks.len() > 64 {
        return Err("health report has more than 64 checks".to_string());
    }
    let status = match report.status {
        guest::HealthStatus::Healthy => "healthy",
        guest::HealthStatus::Degraded => "degraded",
        guest::HealthStatus::Unhealthy => "unhealthy",
    };
    let checks = report
        .checks
        .iter()
        .map(|check| {
            if check.name.chars().count() > 128 || check.message.chars().count() > 512 {
                return Err("health check text exceeds its bound".to_string());
            }
            Ok(json!({
                "name": check.name,
                "status": match check.status {
                    guest::HealthStatus::Healthy => "healthy",
                    guest::HealthStatus::Degraded => "degraded",
                    guest::HealthStatus::Unhealthy => "unhealthy",
                },
                "message": check.message,
            }))
        })
        .collect::<Result<Vec<_>, String>>()?;
    let result = json!({"status": status, "checks": checks});
    if serde_json::to_vec(&result)
        .map_err(|error| error.to_string())?
        .len()
        > MAX_OUTPUT_BYTES
    {
        return Err("health report exceeds 256 KiB".to_string());
    }
    Ok(result)
}

fn hybrid_health_value(
    report: &hybrid_bindings::exports::vibapp::experimental_v0::guest::HealthReport,
) -> Result<Value, String> {
    use hybrid_bindings::exports::vibapp::experimental_v0::guest::HealthStatus;
    if report.checks.len() > 64 {
        return Err("health report has more than 64 checks".to_string());
    }
    let status = match report.status {
        HealthStatus::Healthy => "healthy",
        HealthStatus::Degraded => "degraded",
        HealthStatus::Unhealthy => "unhealthy",
    };
    let checks = report
        .checks
        .iter()
        .map(|check| {
            if check.name.chars().count() > 128 || check.message.chars().count() > 512 {
                return Err("health check text exceeds its bound".to_string());
            }
            Ok(json!({
                "name": check.name,
                "status": match check.status {
                    HealthStatus::Healthy => "healthy",
                    HealthStatus::Degraded => "degraded",
                    HealthStatus::Unhealthy => "unhealthy",
                },
                "message": check.message,
            }))
        })
        .collect::<Result<Vec<_>, String>>()?;
    let result = json!({"status": status, "checks": checks});
    if serde_json::to_vec(&result)
        .map_err(|error| error.to_string())?
        .len()
        > MAX_OUTPUT_BYTES
    {
        return Err("health report exceeds 256 KiB".to_string());
    }
    Ok(result)
}

fn ui_health_value(
    report: &ui_bindings::exports::vibapp::experimental_v0::guest::HealthReport,
) -> Result<Value, String> {
    use ui_bindings::exports::vibapp::experimental_v0::guest::HealthStatus;
    if report.checks.len() > 64 {
        return Err("health report has more than 64 checks".to_string());
    }
    let status = match report.status {
        HealthStatus::Healthy => "healthy",
        HealthStatus::Degraded => "degraded",
        HealthStatus::Unhealthy => "unhealthy",
    };
    let checks = report
        .checks
        .iter()
        .map(|check| {
            if check.name.chars().count() > 128 || check.message.chars().count() > 512 {
                return Err("health check text exceeds its bound".to_string());
            }
            Ok(json!({
                "name": check.name,
                "status": match check.status {
                    HealthStatus::Healthy => "healthy",
                    HealthStatus::Degraded => "degraded",
                    HealthStatus::Unhealthy => "unhealthy",
                },
                "message": check.message,
            }))
        })
        .collect::<Result<Vec<_>, String>>()?;
    let result = json!({"status": status, "checks": checks});
    if serde_json::to_vec(&result)
        .map_err(|error| error.to_string())?
        .len()
        > MAX_OUTPUT_BYTES
    {
        return Err("health report exceeds 256 KiB".to_string());
    }
    Ok(result)
}

fn emit(value: &Value) -> Result<(), String> {
    let encoded = serde_json::to_vec(value).map_err(|error| error.to_string())?;
    if encoded.len() > MAX_OUTPUT_BYTES {
        return Err("runtime protocol output exceeds 256 KiB".to_string());
    }
    let mut stdout = io::stdout().lock();
    stdout
        .write_all(&encoded)
        .map_err(|error| error.to_string())?;
    stdout.write_all(b"\n").map_err(|error| error.to_string())?;
    stdout.flush().map_err(|error| error.to_string())
}

fn context(
    arguments: &Arguments,
    request_id: &str,
    sequence: u64,
    deadline: Duration,
) -> common::CallContext {
    common::CallContext {
        event_id: format!("event-{sequence}"),
        idempotency_key: request_id.to_string(),
        cancellation: format!("cancel-{sequence}"),
        generation: arguments.generation.clone(),
        profile: common::ExecutionProfile::Desktop,
        deadline_monotonic_ms: deadline.as_millis().try_into().unwrap_or(u64::MAX),
    }
}

fn arm_deadline(
    _engine: &Engine,
    store: &mut Store<RuntimeState>,
    fuel: u64,
    deadline: Duration,
) -> Result<(), String> {
    store
        .set_fuel(fuel)
        .map_err(|error| format!("fuel setup failed: {error}"))?;
    let ticks = deadline.as_millis().div_ceil(EPOCH_TICK.as_millis()).max(1);
    store.set_epoch_deadline(ticks.try_into().unwrap_or(u64::MAX));
    Ok(())
}

fn arm_inspection_deadline(store: &mut Store<InspectionState>) -> Result<(), String> {
    store
        .set_fuel(HEALTH_FUEL_BUDGET)
        .map_err(|error| format!("inspection fuel setup failed: {error}"))?;
    let ticks = HEALTH_DEADLINE
        .as_millis()
        .div_ceil(EPOCH_TICK.as_millis())
        .max(1);
    store.set_epoch_deadline(ticks.try_into().unwrap_or(u64::MAX));
    Ok(())
}

fn run_descriptor_inspection() -> Result<(), String> {
    apply_process_limits()?;
    let arguments = inspection_arguments()?;
    let bytes = component_bytes(&arguments.component, &arguments.expected_sha256)?;

    let mut config = Config::new();
    config.wasm_component_model(true);
    config.consume_fuel(true);
    config.epoch_interruption(true);
    config.max_wasm_stack(2 * 1024 * 1024);
    // Wasmtime's 64-bit default reserves 4 GiB per memory, incompatible with
    // our 768 MiB process limit on Linux. Keep explicit bounds checks and the
    // existing Store limiter rather than weakening the process ceiling.
    config.memory_reservation(MAX_LINEAR_MEMORY_BYTES as u64);
    config.memory_guard_size(64 * 1024);
    config.memory_reservation_for_growth(0);
    let engine =
        Engine::new(&config).map_err(|error| format!("engine configuration failed: {error}"))?;
    let epoch_engine = engine.clone();
    thread::spawn(move || {
        loop {
            thread::sleep(EPOCH_TICK);
            epoch_engine.increment_epoch();
        }
    });
    let component = Component::new(&engine, &bytes)
        .map_err(|error| format!("component compilation failed: {error}"))?;
    let mut linker = Linker::new(&engine);
    match arguments.runtime_world.as_str() {
        "service-only-reference" => {
            bindings::ServiceOnlyReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| {
                state
            })
        }
        "hybrid-reference" => {
            hybrid_bindings::HybridReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| {
                state
            })
        }
        "ui-only-reference" => {
            ui_bindings::UiOnlyReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| state)
        }
        _ => unreachable!(),
    }
    .map_err(|error| format!("inspection host interface linking failed: {error}"))?;
    let limits = StoreLimitsBuilder::new()
        .memory_size(MAX_LINEAR_MEMORY_BYTES)
        .instances(8)
        .tables(8)
        .memories(4)
        .trap_on_grow_failure(true)
        .build();
    let mut store = Store::new(&engine, InspectionState { limits });
    store.limiter(|state| &mut state.limits);
    arm_inspection_deadline(&mut store)?;

    let descriptor = match arguments.runtime_world.as_str() {
        "service-only-reference" => {
            let runtime =
                bindings::ServiceOnlyReference::instantiate(&mut store, &component, &linker)
                    .map_err(|error| format!("component instantiation failed: {error}"))?;
            let value = runtime
                .vibapp_experimental_v0_guest()
                .call_describe(&mut store)
                .map_err(|error| format!("describe trapped: {error}"))?
                .map_err(|error| {
                    format!(
                        "Guest rejected describe: {}",
                        bounded_message(&error.message)
                    )
                })?;
            let kind = match value.kind {
                common::AppKind::Ui => "ui",
                common::AppKind::Service => "service",
                common::AppKind::Hybrid => "hybrid",
            };
            let entrypoints = value
                .entrypoints
                .into_iter()
                .map(|item| {
                    let item_kind = match item.kind {
                        common::EntrypointKind::LauncherUi => "launcher-ui",
                        common::EntrypointKind::Service => "service",
                        common::EntrypointKind::Settings => "settings",
                    };
                    (item.id, item_kind, item.label, item.initial_route)
                })
                .collect();
            descriptor_json(
                value.id,
                value.version,
                kind,
                value.display_name,
                entrypoints,
            )?
        }
        "hybrid-reference" => {
            let runtime =
                hybrid_bindings::HybridReference::instantiate(&mut store, &component, &linker)
                    .map_err(|error| format!("component instantiation failed: {error}"))?;
            let value = runtime
                .vibapp_experimental_v0_guest()
                .call_describe(&mut store)
                .map_err(|error| format!("describe trapped: {error}"))?
                .map_err(|error| {
                    format!(
                        "Guest rejected describe: {}",
                        bounded_message(&error.message)
                    )
                })?;
            use hybrid_bindings::exports::vibapp::experimental_v0::guest::{
                AppKind, EntrypointKind,
            };
            let kind = match value.kind {
                AppKind::Ui => "ui",
                AppKind::Service => "service",
                AppKind::Hybrid => "hybrid",
            };
            let entrypoints = value
                .entrypoints
                .into_iter()
                .map(|item| {
                    let item_kind = match item.kind {
                        EntrypointKind::LauncherUi => "launcher-ui",
                        EntrypointKind::Service => "service",
                        EntrypointKind::Settings => "settings",
                    };
                    (item.id, item_kind, item.label, item.initial_route)
                })
                .collect();
            descriptor_json(
                value.id,
                value.version,
                kind,
                value.display_name,
                entrypoints,
            )?
        }
        "ui-only-reference" => {
            let runtime =
                ui_bindings::UiOnlyReference::instantiate(&mut store, &component, &linker)
                    .map_err(|error| format!("component instantiation failed: {error}"))?;
            let value = runtime
                .vibapp_experimental_v0_guest()
                .call_describe(&mut store)
                .map_err(|error| format!("describe trapped: {error}"))?
                .map_err(|error| {
                    format!(
                        "Guest rejected describe: {}",
                        bounded_message(&error.message)
                    )
                })?;
            use ui_bindings::exports::vibapp::experimental_v0::guest::{AppKind, EntrypointKind};
            let kind = match value.kind {
                AppKind::Ui => "ui",
                AppKind::Service => "service",
                AppKind::Hybrid => "hybrid",
            };
            let entrypoints = value
                .entrypoints
                .into_iter()
                .map(|item| {
                    let item_kind = match item.kind {
                        EntrypointKind::LauncherUi => "launcher-ui",
                        EntrypointKind::Service => "service",
                        EntrypointKind::Settings => "settings",
                    };
                    (item.id, item_kind, item.label, item.initial_route)
                })
                .collect();
            descriptor_json(
                value.id,
                value.version,
                kind,
                value.display_name,
                entrypoints,
            )?
        }
        _ => unreachable!(),
    };

    emit(&json!({
        "schema_version": INSPECTOR_SCHEMA,
        "component_sha256": arguments.expected_sha256,
        "world": arguments.runtime_world,
        "descriptor": descriptor,
        "isolation": {
            "separate_process": true,
            "ambient_wasi_linked": false,
            "live_host_effects": false,
            "linear_memory_limit_bytes": MAX_LINEAR_MEMORY_BYTES,
            "fuel_budget": HEALTH_FUEL_BUDGET,
            "deadline_ms": HEALTH_DEADLINE.as_millis(),
        }
    }))
}

fn run() -> Result<(), String> {
    apply_process_limits()?;
    let arguments = arguments()?;
    let component_bytes = component_bytes(&arguments.component, &arguments.expected_sha256)?;

    let mut config = Config::new();
    config.wasm_component_model(true);
    config.consume_fuel(true);
    config.epoch_interruption(true);
    config.max_wasm_stack(2 * 1024 * 1024);
    config.memory_reservation(MAX_LINEAR_MEMORY_BYTES as u64);
    config.memory_guard_size(64 * 1024);
    config.memory_reservation_for_growth(0);
    let engine =
        Engine::new(&config).map_err(|error| format!("engine configuration failed: {error}"))?;
    let epoch_engine = engine.clone();
    thread::spawn(move || {
        loop {
            thread::sleep(EPOCH_TICK);
            epoch_engine.increment_epoch();
        }
    });
    let component = Component::new(&engine, &component_bytes)
        .map_err(|error| format!("component compilation failed: {error}"))?;
    let mut linker = Linker::new(&engine);
    match arguments.runtime_world.as_str() {
        "service-only-reference" => {
            bindings::ServiceOnlyReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| {
                state
            })
        }
        "hybrid-reference" => {
            hybrid_bindings::HybridReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| {
                state
            })
        }
        "ui-only-reference" => {
            ui_bindings::UiOnlyReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| state)
        }
        _ => return Err("runtime world is not service-capable".to_string()),
    }
    .map_err(|error| format!("host interface linking failed: {error}"))?;
    let limits = StoreLimitsBuilder::new()
        .memory_size(MAX_LINEAR_MEMORY_BYTES)
        .instances(8)
        .tables(8)
        .memories(4)
        .trap_on_grow_failure(true)
        .build();
    let kv = KvBroker::load(
        &arguments.state_directory,
        arguments.state_revision,
        arguments.kv_enabled,
        arguments.kv_maximum_bytes,
    )?;
    let settings = SettingsBroker::load(
        &arguments.settings_snapshot,
        &arguments.app_id,
        &arguments.package_digest_sha256,
        &arguments.generation,
        arguments.state_revision,
    )?;
    let mut store = Store::new(
        &engine,
        RuntimeState {
            started: Instant::now(),
            limits,
            kv,
            settings,
            ui_surface: None,
            kv_transaction_attempts: 0,
        },
    );
    store.limiter(|state| &mut state.limits);
    arm_deadline(&engine, &mut store, HEALTH_FUEL_BUDGET, HEALTH_DEADLINE)?;
    let runtime_bindings = match arguments.runtime_world.as_str() {
        "service-only-reference" => RuntimeBindings::Service(
            bindings::ServiceOnlyReference::instantiate(&mut store, &component, &linker)
                .map_err(|error| format!("component instantiation failed: {error}"))?,
        ),
        "hybrid-reference" => RuntimeBindings::Hybrid(
            hybrid_bindings::HybridReference::instantiate(&mut store, &component, &linker)
                .map_err(|error| format!("component instantiation failed: {error}"))?,
        ),
        "ui-only-reference" => RuntimeBindings::Ui(
            ui_bindings::UiOnlyReference::instantiate(&mut store, &component, &linker)
                .map_err(|error| format!("component instantiation failed: {error}"))?,
        ),
        _ => unreachable!(),
    };
    emit(&json!({
        "schema_version": PROTOCOL_SCHEMA,
        "tag": "ready",
        "component_sha256": arguments.expected_sha256,
        "package_digest_sha256": arguments.package_digest_sha256,
        "generation": arguments.generation,
        "entrypoint": arguments.entrypoint,
        "world": arguments.runtime_world,
        "isolation": {
            "separate_process": true,
            "ambient_wasi_linked": false,
            "memory_limit_bytes": MAX_LINEAR_MEMORY_BYTES,
            "native_address_space_limit_bytes": if cfg!(target_os = "linux") { json!(MAX_PROCESS_MEMORY_BYTES) } else { Value::Null },
            "native_process_commit_limit_bytes": if cfg!(windows) { json!(MAX_PROCESS_MEMORY_BYTES) } else { Value::Null },
            "event_fuel_budget": EVENT_FUEL_BUDGET,
            "health_fuel_budget": HEALTH_FUEL_BUDGET,
            "event_deadline_ms": EVENT_DEADLINE.as_millis(),
            "health_deadline_ms": HEALTH_DEADLINE.as_millis()
        }
    }))?;

    let stdin = io::stdin();
    let mut sequence = 0u64;
    for line in stdin.lock().lines() {
        let line = line.map_err(|error| format!("cannot read runtime command: {error}"))?;
        if line.is_empty() || line.len() > MAX_COMMAND_BYTES {
            emit(&response_error(
                "unknown",
                "resource-limit",
                "runtime command exceeds its bound",
                false,
            ))?;
            return Ok(());
        }
        let command: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(_) => {
                emit(&response_error(
                    "unknown",
                    "malformed-output",
                    "runtime command is not valid JSON",
                    false,
                ))?;
                return Ok(());
            }
        };
        let request_id = command
            .get("request_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty() && value.len() <= 128)
            .unwrap_or("unknown");
        let operation = command.get("command").and_then(Value::as_str).unwrap_or("");
        sequence = sequence.saturating_add(1);
        let result = match operation {
            "validate" => {
                arm_deadline(&engine, &mut store, HEALTH_FUEL_BUDGET, HEALTH_DEADLINE)?;
                match validate_descriptor(&runtime_bindings, &mut store, &arguments) {
                    Ok(()) => json!({
                        "schema_version": PROTOCOL_SCHEMA,
                        "request_id": request_id,
                        "outcome": {"tag": "validated"}
                    }),
                    Err(error) => {
                        response_error(request_id, "incompatible-contract", &error, false)
                    }
                }
            }
            "ui-launch" => {
                let parsed = (
                    command_text(&command, "session", 256),
                    command_text(&command, "surface", 256),
                    command_text(&command, "route", 128),
                );
                let (Ok(session), Ok(surface), Ok(route)) = parsed else {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "invalid UI launch binding",
                        false,
                    ))?;
                    continue;
                };
                arm_deadline(&engine, &mut store, EVENT_FUEL_BUDGET, EVENT_DEADLINE)?;
                let call_context = context(&arguments, request_id, sequence, EVENT_DEADLINE);
                match &runtime_bindings {
                    RuntimeBindings::Service(_) => response_error(
                        request_id,
                        "unsupported-surface",
                        "service-only generation cannot receive a UI launch",
                        false,
                    ),
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let event = hybrid_guest::AppEvent::Launcher(
                            hybrid_guest::LauncherEvent::Launch(hybrid_guest::LaunchEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                session: session.clone(),
                                surface: surface.clone(),
                                route: route.clone(),
                                reason: hybrid_guest::OpenReason::User,
                            }),
                        );
                        call_ui_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "UI launch",
                            &session,
                            &surface,
                            &route
                        )
                    }
                    RuntimeBindings::Ui(runtime) => {
                        use ui_bindings::exports::vibapp::experimental_v0::guest as ui_guest;
                        let event = ui_guest::AppEvent::Launcher(ui_guest::LauncherEvent::Launch(
                            ui_guest::LaunchEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                session: session.clone(),
                                surface: surface.clone(),
                                route: route.clone(),
                                reason: ui_guest::OpenReason::User,
                            },
                        ));
                        call_ui_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "UI launch",
                            &session,
                            &surface,
                            &route
                        )
                    }
                }
            }
            "ui-action" => {
                let parsed = (
                    command_text(&command, "session", 256),
                    command_text(&command, "surface", 256),
                    command_text(&command, "route", 128),
                );
                let (Ok(session), Ok(surface), Ok(route)) = parsed else {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "invalid UI action binding",
                        false,
                    ))?;
                    continue;
                };
                let Some(current) = store.data().ui_surface.clone() else {
                    emit(&response_error(
                        request_id,
                        "stale-revision",
                        "UI generation has no trusted current surface",
                        false,
                    ))?;
                    continue;
                };
                if current.session != session
                    || current.surface != surface
                    || current.route != route
                {
                    emit(&response_error(
                        request_id,
                        "forged-identifier",
                        "UI action does not match the current surface binding",
                        false,
                    ))?;
                    continue;
                }
                let (action, fields) = match parse_action_fields(&command, &current) {
                    Ok(value) => value,
                    Err(error) => {
                        emit(&response_error(
                            request_id,
                            "invalid-argument",
                            &error,
                            false,
                        ))?;
                        continue;
                    }
                };
                let event_id = match command_text(&command, "event_id", 128) {
                    Ok(value) => value,
                    Err(error) => {
                        emit(&response_error(
                            request_id,
                            "invalid-argument",
                            &error,
                            false,
                        ))?;
                        continue;
                    }
                };
                arm_deadline(&engine, &mut store, EVENT_FUEL_BUDGET, EVENT_DEADLINE)?;
                let mut call_context = context(&arguments, request_id, sequence, EVENT_DEADLINE);
                call_context.event_id = event_id.clone();
                call_context.idempotency_key = event_id;
                match &runtime_bindings {
                    RuntimeBindings::Service(_) => response_error(
                        request_id,
                        "unsupported-surface",
                        "service-only generation cannot receive a UI action",
                        false,
                    ),
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let event = hybrid_guest::AppEvent::Launcher(
                            hybrid_guest::LauncherEvent::Action(ui::ActionEvent {
                                session: session.clone(),
                                surface: surface.clone(),
                                route: route.clone(),
                                action,
                                fields,
                            }),
                        );
                        call_ui_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "UI action",
                            &session,
                            &surface,
                            &route
                        )
                    }
                    RuntimeBindings::Ui(runtime) => {
                        use ui_bindings::exports::vibapp::experimental_v0::guest as ui_guest;
                        let event = ui_guest::AppEvent::Launcher(ui_guest::LauncherEvent::Action(
                            ui::ActionEvent {
                                session: session.clone(),
                                surface: surface.clone(),
                                route: route.clone(),
                                action,
                                fields,
                            },
                        ));
                        call_ui_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "UI action",
                            &session,
                            &surface,
                            &route
                        )
                    }
                }
            }
            "ui-refresh" => {
                let parsed = (
                    command_text(&command, "session", 256),
                    command_text(&command, "surface", 256),
                    command_text(&command, "route", 128),
                    command_text(&command, "event_id", 128),
                );
                let (Ok(session), Ok(surface), Ok(route), Ok(event_id)) = parsed else {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "invalid UI refresh binding",
                        false,
                    ))?;
                    continue;
                };
                let Some(current) = store.data().ui_surface.clone() else {
                    emit(&response_error(
                        request_id,
                        "stale-revision",
                        "UI generation has no trusted current surface",
                        false,
                    ))?;
                    continue;
                };
                if current.session != session
                    || current.surface != surface
                    || current.route != route
                {
                    emit(&response_error(
                        request_id,
                        "forged-identifier",
                        "UI refresh does not match the current surface binding",
                        false,
                    ))?;
                    continue;
                }
                arm_deadline(&engine, &mut store, EVENT_FUEL_BUDGET, EVENT_DEADLINE)?;
                let mut call_context = context(&arguments, request_id, sequence, EVENT_DEADLINE);
                call_context.event_id = event_id.clone();
                call_context.idempotency_key = event_id;
                match &runtime_bindings {
                    RuntimeBindings::Service(_) => response_error(
                        request_id,
                        "unsupported-surface",
                        "service-only generation cannot refresh a UI surface",
                        false,
                    ),
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let event = hybrid_guest::AppEvent::Launcher(
                            hybrid_guest::LauncherEvent::Open(hybrid_guest::OpenEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                session: session.clone(),
                                surface: surface.clone(),
                                route: route.clone(),
                                reason: hybrid_guest::OpenReason::Restore,
                            }),
                        );
                        call_ui_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "UI refresh",
                            &session,
                            &surface,
                            &route
                        )
                    }
                    RuntimeBindings::Ui(runtime) => {
                        use ui_bindings::exports::vibapp::experimental_v0::guest as ui_guest;
                        let event = ui_guest::AppEvent::Launcher(ui_guest::LauncherEvent::Open(
                            ui_guest::OpenEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                session: session.clone(),
                                surface: surface.clone(),
                                route: route.clone(),
                                reason: ui_guest::OpenReason::Restore,
                            },
                        ));
                        call_ui_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "UI refresh",
                            &session,
                            &surface,
                            &route
                        )
                    }
                }
            }
            "start" => {
                arm_deadline(&engine, &mut store, EVENT_FUEL_BUDGET, EVENT_DEADLINE)?;
                let call_context = context(&arguments, request_id, sequence, EVENT_DEADLINE);
                match &runtime_bindings {
                    RuntimeBindings::Service(runtime) => {
                        let reason = match command.get("reason").and_then(Value::as_str) {
                            Some("enabled") => guest::ServiceStartReason::Enabled,
                            Some("host-restart") => guest::ServiceStartReason::HostRestart,
                            Some("update") => guest::ServiceStartReason::Update,
                            Some("manual") => guest::ServiceStartReason::Manual,
                            _ => {
                                emit(&response_error(
                                    request_id,
                                    "invalid-argument",
                                    "invalid service start reason",
                                    false,
                                ))?;
                                continue;
                            }
                        };
                        let event = guest::AppEvent::Service(guest::ServiceEvent::Start(
                            guest::ServiceStartEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                instance: arguments.instance.clone(),
                                reason,
                            },
                        ));
                        call_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "service start",
                            "started"
                        )
                    }
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let reason = match command.get("reason").and_then(Value::as_str) {
                            Some("enabled") => hybrid_guest::ServiceStartReason::Enabled,
                            Some("host-restart") => hybrid_guest::ServiceStartReason::HostRestart,
                            Some("update") => hybrid_guest::ServiceStartReason::Update,
                            Some("manual") => hybrid_guest::ServiceStartReason::Manual,
                            _ => {
                                emit(&response_error(
                                    request_id,
                                    "invalid-argument",
                                    "invalid service start reason",
                                    false,
                                ))?;
                                continue;
                            }
                        };
                        let event = hybrid_guest::AppEvent::Service(
                            hybrid_guest::ServiceEvent::Start(hybrid_guest::ServiceStartEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                instance: arguments.instance.clone(),
                                reason,
                            }),
                        );
                        call_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "service start",
                            "started"
                        )
                    }
                    RuntimeBindings::Ui(_) => response_error(
                        request_id,
                        "unsupported-surface",
                        "UI-only generation cannot receive a service start",
                        false,
                    ),
                }
            }
            "trigger" => {
                let trigger_id = command
                    .get("trigger_id")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty() && value.len() <= 128);
                let payload = command
                    .get("payload")
                    .and_then(Value::as_array)
                    .filter(|value| value.len() <= MAX_TRIGGER_BYTES)
                    .and_then(|items| {
                        items
                            .iter()
                            .map(|item| item.as_u64().and_then(|value| u8::try_from(value).ok()))
                            .collect::<Option<Vec<_>>>()
                    });
                let (Some(trigger_id), Some(payload)) = (trigger_id, payload) else {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "invalid bounded service trigger",
                        false,
                    ))?;
                    continue;
                };
                arm_deadline(&engine, &mut store, EVENT_FUEL_BUDGET, EVENT_DEADLINE)?;
                let call_context = context(&arguments, request_id, sequence, EVENT_DEADLINE);
                match &runtime_bindings {
                    RuntimeBindings::Service(runtime) => {
                        let event = guest::AppEvent::Service(guest::ServiceEvent::Trigger(
                            guest::ServiceTriggerEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                instance: arguments.instance.clone(),
                                trigger_id: trigger_id.to_string(),
                                payload,
                            },
                        ));
                        call_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "service trigger",
                            "triggered"
                        )
                    }
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let event =
                            hybrid_guest::AppEvent::Service(hybrid_guest::ServiceEvent::Trigger(
                                hybrid_guest::ServiceTriggerEvent {
                                    entrypoint: arguments.entrypoint.clone(),
                                    instance: arguments.instance.clone(),
                                    trigger_id: trigger_id.to_string(),
                                    payload,
                                },
                            ));
                        call_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "service trigger",
                            "triggered"
                        )
                    }
                    RuntimeBindings::Ui(_) => response_error(
                        request_id,
                        "unsupported-surface",
                        "UI-only generation cannot receive a service trigger",
                        false,
                    ),
                }
            }
            "health" => {
                arm_deadline(&engine, &mut store, HEALTH_FUEL_BUDGET, HEALTH_DEADLINE)?;
                let call_context = context(&arguments, request_id, sequence, HEALTH_DEADLINE);
                let response = match &runtime_bindings {
                    RuntimeBindings::Service(runtime) => {
                        let request = guest::HealthRequest {
                            service_entrypoint: Some(arguments.entrypoint.clone()),
                            include_dependencies: true,
                        };
                        match runtime.vibapp_experimental_v0_guest().call_health(
                            &mut store,
                            &call_context,
                            &request,
                        ) {
                            Ok(Ok(report)) => match health_value(&report) {
                                Ok(report) => {
                                    json!({"schema_version": PROTOCOL_SCHEMA, "request_id": request_id, "outcome": {"tag": "health", "report": report}})
                                }
                                Err(error) => {
                                    response_error(request_id, "resource-limit", &error, false)
                                }
                            },
                            Ok(Err(error)) => guest_error(request_id, &error),
                            Err(error) => runtime_error(request_id, "service health", error),
                        }
                    }
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let request = hybrid_guest::HealthRequest {
                            service_entrypoint: Some(arguments.entrypoint.clone()),
                            include_dependencies: true,
                        };
                        match runtime.vibapp_experimental_v0_guest().call_health(
                            &mut store,
                            &call_context,
                            &request,
                        ) {
                            Ok(Ok(report)) => match hybrid_health_value(&report) {
                                Ok(report) => {
                                    json!({"schema_version": PROTOCOL_SCHEMA, "request_id": request_id, "outcome": {"tag": "health", "report": report}})
                                }
                                Err(error) => {
                                    response_error(request_id, "resource-limit", &error, false)
                                }
                            },
                            Ok(Err(error)) => guest_error(request_id, &error),
                            Err(error) => runtime_error(request_id, "service health", error),
                        }
                    }
                    RuntimeBindings::Ui(runtime) => {
                        use ui_bindings::exports::vibapp::experimental_v0::guest as ui_guest;
                        let request = ui_guest::HealthRequest {
                            service_entrypoint: None,
                            include_dependencies: true,
                        };
                        match runtime.vibapp_experimental_v0_guest().call_health(
                            &mut store,
                            &call_context,
                            &request,
                        ) {
                            Ok(Ok(report)) => match ui_health_value(&report) {
                                Ok(report) => {
                                    json!({"schema_version": PROTOCOL_SCHEMA, "request_id": request_id, "outcome": {"tag": "health", "report": report}})
                                }
                                Err(error) => {
                                    response_error(request_id, "resource-limit", &error, false)
                                }
                            },
                            Ok(Err(error)) => guest_error(request_id, &error),
                            Err(error) => runtime_error(request_id, "UI health", error),
                        }
                    }
                };
                bind_state_revision(response, &store)
            }
            "migrate" => {
                let from_schema = command
                    .get("from_schema")
                    .and_then(Value::as_u64)
                    .and_then(|value| u32::try_from(value).ok());
                let to_schema = command
                    .get("to_schema")
                    .and_then(Value::as_u64)
                    .and_then(|value| u32::try_from(value).ok());
                let source_state_revision =
                    command.get("source_state_revision").and_then(Value::as_u64);
                let target_state_revision =
                    command.get("target_state_revision").and_then(Value::as_u64);
                let (
                    Some(from_schema),
                    Some(to_schema),
                    Some(source_state_revision),
                    Some(target_state_revision),
                ) = (
                    from_schema,
                    to_schema,
                    source_state_revision,
                    target_state_revision,
                )
                else {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "invalid migration revisions",
                        false,
                    ))?;
                    continue;
                };
                if from_schema == 0
                    || to_schema == 0
                    || source_state_revision == 0
                    || target_state_revision <= source_state_revision
                {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "migration revisions are outside the accepted range",
                        false,
                    ))?;
                    continue;
                }
                arm_deadline(
                    &engine,
                    &mut store,
                    MIGRATION_FUEL_BUDGET,
                    MIGRATION_DEADLINE,
                )?;
                if let Err(error) = store
                    .data_mut()
                    .kv
                    .begin_migration(source_state_revision, target_state_revision)
                {
                    emit(&response_error(request_id, "stale-revision", &error, false))?;
                    continue;
                }
                let call_context = context(&arguments, request_id, sequence, MIGRATION_DEADLINE);
                let mut response = match &runtime_bindings {
                    RuntimeBindings::Service(runtime) => {
                        let request = guest::MigrationRequest {
                            from_schema,
                            to_schema,
                            source_state_revision,
                            target_state_revision,
                        };
                        match runtime.vibapp_experimental_v0_guest().call_migrate(
                            &mut store,
                            &call_context,
                            request,
                        ) {
                            Ok(Ok(result))
                                if result.target_state_revision == target_state_revision =>
                            {
                                json!({
                                    "schema_version": PROTOCOL_SCHEMA,
                                    "request_id": request_id,
                                    "outcome": {
                                        "tag": "migration",
                                        "status": match result.status {
                                            guest::MigrationStatus::Unchanged => "unchanged",
                                            guest::MigrationStatus::Migrated => "migrated",
                                        },
                                        "target_state_revision": result.target_state_revision
                                    }
                                })
                            }
                            Ok(Ok(_)) => response_error(
                                request_id,
                                "malformed-output",
                                "guest migration returned another target state revision",
                                false,
                            ),
                            Ok(Err(error)) => guest_error(request_id, &error),
                            Err(error) => runtime_error(request_id, "state migration", error),
                        }
                    }
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let request = hybrid_guest::MigrationRequest {
                            from_schema,
                            to_schema,
                            source_state_revision,
                            target_state_revision,
                        };
                        match runtime.vibapp_experimental_v0_guest().call_migrate(
                            &mut store,
                            &call_context,
                            request,
                        ) {
                            Ok(Ok(result))
                                if result.target_state_revision == target_state_revision =>
                            {
                                json!({
                                    "schema_version": PROTOCOL_SCHEMA,
                                    "request_id": request_id,
                                    "outcome": {
                                        "tag": "migration",
                                        "status": match result.status {
                                            hybrid_guest::MigrationStatus::Unchanged => "unchanged",
                                            hybrid_guest::MigrationStatus::Migrated => "migrated",
                                        },
                                        "target_state_revision": result.target_state_revision
                                    }
                                })
                            }
                            Ok(Ok(_)) => response_error(
                                request_id,
                                "malformed-output",
                                "guest migration returned another target state revision",
                                false,
                            ),
                            Ok(Err(error)) => guest_error(request_id, &error),
                            Err(error) => runtime_error(request_id, "state migration", error),
                        }
                    }
                    RuntimeBindings::Ui(runtime) => {
                        use ui_bindings::exports::vibapp::experimental_v0::guest as ui_guest;
                        let request = ui_guest::MigrationRequest {
                            from_schema,
                            to_schema,
                            source_state_revision,
                            target_state_revision,
                        };
                        match runtime.vibapp_experimental_v0_guest().call_migrate(
                            &mut store,
                            &call_context,
                            request,
                        ) {
                            Ok(Ok(result))
                                if result.target_state_revision == target_state_revision =>
                            {
                                json!({
                                    "schema_version": PROTOCOL_SCHEMA,
                                    "request_id": request_id,
                                    "outcome": {
                                        "tag": "migration",
                                        "status": match result.status {
                                            ui_guest::MigrationStatus::Unchanged => "unchanged",
                                            ui_guest::MigrationStatus::Migrated => "migrated",
                                        },
                                        "target_state_revision": result.target_state_revision
                                    }
                                })
                            }
                            Ok(Ok(_)) => response_error(
                                request_id,
                                "malformed-output",
                                "guest migration returned another target state revision",
                                false,
                            ),
                            Ok(Err(error)) => guest_error(request_id, &error),
                            Err(error) => runtime_error(request_id, "state migration", error),
                        }
                    }
                };
                if response.get("error").is_some() {
                    store.data_mut().kv.abort_migration();
                } else if let Err(error) =
                    store.data_mut().kv.finish_migration(target_state_revision)
                {
                    response = response_error(request_id, "internal", &error, true);
                } else if let Some(outcome) =
                    response.get_mut("outcome").and_then(Value::as_object_mut)
                {
                    outcome.insert(
                        "kv_state_revision".to_string(),
                        json!(target_state_revision),
                    );
                }
                response
            }
            "rebind-state" => {
                let rebound = command
                    .get("state_directory")
                    .and_then(Value::as_str)
                    .filter(|value| {
                        !value.is_empty()
                            && value.len() <= 512
                            && !value.chars().any(char::is_control)
                    });
                let Some(rebound) = rebound else {
                    emit(&response_error(
                        request_id,
                        "invalid-argument",
                        "invalid rebound state directory",
                        false,
                    ))?;
                    continue;
                };
                match store.data_mut().kv.rebind(Path::new(rebound)) {
                    Ok(()) => json!({
                        "schema_version": PROTOCOL_SCHEMA,
                        "request_id": request_id,
                        "outcome": {
                            "tag": "state-rebound",
                            "state_revision": store.data().kv.document.state_revision,
                        }
                    }),
                    Err(error) => response_error(request_id, "integrity-failure", &error, false),
                }
            }
            "stop" => {
                arm_deadline(&engine, &mut store, EVENT_FUEL_BUDGET, EVENT_DEADLINE)?;
                let call_context = context(&arguments, request_id, sequence, EVENT_DEADLINE);
                let response = match &runtime_bindings {
                    RuntimeBindings::Service(runtime) => {
                        let reason = match command.get("reason").and_then(Value::as_str) {
                            Some("disabled") => guest::ServiceStopReason::Disabled,
                            Some("update") => guest::ServiceStopReason::Update,
                            Some("uninstall") => guest::ServiceStopReason::Uninstall,
                            Some("unhealthy") => guest::ServiceStopReason::Unhealthy,
                            Some("host-shutdown") => guest::ServiceStopReason::HostShutdown,
                            _ => {
                                emit(&response_error(
                                    request_id,
                                    "invalid-argument",
                                    "invalid service stop reason",
                                    false,
                                ))?;
                                continue;
                            }
                        };
                        let event = guest::AppEvent::Service(guest::ServiceEvent::Stop(
                            guest::ServiceStopEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                instance: arguments.instance.clone(),
                                reason,
                            },
                        ));
                        call_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "service stop",
                            "stopped"
                        )
                    }
                    RuntimeBindings::Hybrid(runtime) => {
                        use hybrid_bindings::exports::vibapp::experimental_v0::guest as hybrid_guest;
                        let reason = match command.get("reason").and_then(Value::as_str) {
                            Some("disabled") => hybrid_guest::ServiceStopReason::Disabled,
                            Some("update") => hybrid_guest::ServiceStopReason::Update,
                            Some("uninstall") => hybrid_guest::ServiceStopReason::Uninstall,
                            Some("unhealthy") => hybrid_guest::ServiceStopReason::Unhealthy,
                            Some("host-shutdown") => hybrid_guest::ServiceStopReason::HostShutdown,
                            _ => {
                                emit(&response_error(
                                    request_id,
                                    "invalid-argument",
                                    "invalid service stop reason",
                                    false,
                                ))?;
                                continue;
                            }
                        };
                        let event = hybrid_guest::AppEvent::Service(
                            hybrid_guest::ServiceEvent::Stop(hybrid_guest::ServiceStopEvent {
                                entrypoint: arguments.entrypoint.clone(),
                                instance: arguments.instance.clone(),
                                reason,
                            }),
                        );
                        call_event!(
                            runtime.vibapp_experimental_v0_guest(),
                            &mut store,
                            &call_context,
                            event,
                            request_id,
                            "service stop",
                            "stopped"
                        )
                    }
                    RuntimeBindings::Ui(_) => response_error(
                        request_id,
                        "unsupported-surface",
                        "UI-only generation cannot receive a service stop",
                        false,
                    ),
                };
                emit(&response)?;
                return Ok(());
            }
            _ => response_error(
                request_id,
                "invalid-argument",
                "unknown runtime command",
                false,
            ),
        };
        let fatal = response_is_fatal(&result);
        emit(&result)?;
        if fatal {
            return Ok(());
        }
    }
    Ok(())
}

fn main() {
    let inspection_mode =
        env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--inspect-descriptor"));
    let result = if inspection_mode {
        run_descriptor_inspection()
    } else {
        run()
    };
    if let Err(error) = result {
        if inspection_mode {
            eprintln!("INSPECT_REJECTED {}", bounded_message(&error));
        } else {
            let _ = emit(&response_error(
                "startup",
                "incompatible-contract",
                &error,
                false,
            ));
        }
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        InspectionState, StoreLimitsBuilder, UiSurfaceState, canonical_utc_from_unix_seconds,
        clock, common, host_capability_states, host_info, http, kv, local_clock_context, log,
        notification, parse_action_fields, scheduler, settings, system_metrics, ui,
        validate_ui_output, zoneinfo_name_from_path,
    };
    use serde_json::json;
    use std::path::Path;

    fn input_rejection(
        operation: &str,
        code: common::ErrorCode,
        before: (u64, u64),
        after: (u64, u64),
    ) -> serde_json::Value {
        super::ui_guest_error(
            "request-1",
            &common::AppError { code, message: "输入最多 32 个字符".to_string(), retryable: false },
            operation,
            &common::CallContext {
                event_id: "event-1".to_string(),
                idempotency_key: "event-1".to_string(),
                cancellation: "cancel-1".to_string(),
                generation: "generation-1".to_string(),
                profile: common::ExecutionProfile::Desktop,
                deadline_monotonic_ms: 250,
            },
            ("session-1", "surface-1", "home"),
            before,
            after,
        )
    }

    #[test]
    fn ui_rejection_clean_typed_input_keeps_event_loop_live_and_error_intact() {
        let response = input_rejection("UI action", common::ErrorCode::InvalidArgument, (7, 3), (7, 3));
        assert_eq!(response["error"]["code"], "invalid-argument");
        assert_eq!(response["error"]["message"], "输入最多 32 个字符");
        assert!(response.get("outcome").is_none());
        assert_eq!(response["ui_input_rejection"], json!({
            "origin": "typed-guest-ui-action", "generation": "generation-1",
            "session": "session-1", "surface": "surface-1", "route": "home",
            "event_id": "event-1", "state_revision": 7,
        }));
        assert!(!super::response_is_fatal(&response));
    }

    #[test]
    fn ui_rejection_any_write_attempt_or_revision_change_remains_fatal() {
        // Attempted write with no revision increment covers rejected/deduped
        // transactions and uncertain persistence failures, not only commits.
        for (before, after) in [((7, 3), (7, 4)), ((7, 3), (8, 3)),
                                ((7, 3), (6, 3)), ((0, 3), (0, 3)),
                                ((7, u64::MAX), (7, u64::MAX))] {
            let response = input_rejection("UI action", common::ErrorCode::InvalidArgument, before, after);
            assert!(response.get("ui_input_rejection").is_none());
            assert!(super::response_is_fatal(&response));
        }
    }

    #[test]
    #[cfg(unix)]
    fn ui_rejection_counts_kv_attempts_before_grant_or_argument_rejection() {
        let mut state = super::RuntimeState {
            started: std::time::Instant::now(),
            limits: StoreLimitsBuilder::new().build(),
            kv: super::KvBroker {
                enabled: false,
                path: std::path::PathBuf::new(),
                maximum_bytes: 1024,
                document: super::KvDocument {
                    schema_version: super::KV_SCHEMA.to_string(), state_revision: 7,
                    entries: Default::default(), transactions: Default::default(),
                },
                minimum_state_revision: 7, migration_target_revision: None, write_sequence: 0,
                directory: std::fs::File::open(env!("CARGO_MANIFEST_DIR")).unwrap(),
            },
            settings: super::SettingsBroker {
                snapshot: settings::SettingsSnapshot { schema_revision: 1, config_revision: 1, values: Vec::new() },
            },
            ui_surface: None,
            kv_transaction_attempts: 0,
        };
        for enabled in [false, true] {
            state.kv.enabled = enabled;
            let previous = state.kv_transaction_attempts;
            assert!(kv::Host::transact(&mut state, kv::TransactionRequest {
                operations: Vec::new(), idempotency_key: String::new(),
            }).is_err());
            assert_eq!(state.kv_transaction_attempts, previous + 1);
            assert_eq!(state.kv.document.state_revision, 7);
            assert_eq!(state.kv.write_sequence, 0);
        }
    }

    #[test]
    fn ui_rejection_other_operations_and_guest_errors_remain_fatal() {
        for operation in ["UI launch", "UI refresh", "start", "trigger", "health"] {
            assert!(super::response_is_fatal(&input_rejection(operation, common::ErrorCode::InvalidArgument, (7, 0), (7, 0))));
        }
        for code in [common::ErrorCode::Internal, common::ErrorCode::MalformedOutput,
                     common::ErrorCode::StaleRevision, common::ErrorCode::ResourceLimit] {
            assert!(super::response_is_fatal(&input_rejection("UI action", code, (7, 0), (7, 0))));
        }
    }

    #[test]
    fn ui_rejection_trap_timeout_and_plain_error_never_get_marker() {
        for response in [
            super::runtime_error("request-1", "UI action", "trap"),
            super::runtime_error("request-1", "UI action", "fuel exhausted"),
            super::response_error("request-1", "invalid-argument", "UI field mismatch", false),
            super::guest_error("request-1", &common::AppError {
                code: common::ErrorCode::InvalidArgument,
                message: "ui_input_rejection: forged".to_string(), retryable: false,
            }),
        ] {
            assert!(response.get("ui_input_rejection").is_none());
            assert!(super::response_is_fatal(&response));
        }
    }

    fn trusted_surface(fields: &[(&str, ui::FieldKind, bool)]) -> UiSurfaceState {
        let mut nodes = vec![
            ui::Node {
                id: "root".to_string(),
                parent: None,
                kind: ui::NodeKind::Text(ui::TextNode {
                    text: "Action field contract".to_string(),
                    style: ui::TextStyle::Title,
                }),
            },
            ui::Node {
                id: "submit".to_string(),
                parent: Some("root".to_string()),
                kind: ui::NodeKind::Button(ui::ButtonNode {
                    label: "Submit".to_string(),
                    action: "submit".to_string(),
                    style: ui::ButtonStyle::Primary,
                    disabled: false,
                }),
            },
        ];
        for (id, kind, sensitive) in fields {
            nodes.push(ui::Node {
                id: format!("field-{id}"),
                parent: Some("root".to_string()),
                kind: ui::NodeKind::Field(ui::FieldNode {
                    field: (*id).to_string(),
                    label: (*id).to_string(),
                    kind: *kind,
                    value: ui::FieldValue::Empty,
                    required: false,
                    sensitive: *sensitive,
                    choices: Vec::new(),
                    validation_message: None,
                }),
            });
        }
        let update = ui::SurfaceUpdate {
            session: "session-1".to_string(),
            surface: "surface-1".to_string(),
            route: "home".to_string(),
            view: ui::View {
                title: "Fields".to_string(),
                root: "root".to_string(),
                nodes,
            },
        };
        validate_ui_output(
            &[update],
            None,
            &json!({"outcome": "fixture"}),
            "session-1",
            "surface-1",
            "home",
        )
        .unwrap()
    }

    #[test]
    fn utc_formatter_is_canonical_across_epoch_leap_day_and_2038() {
        let cases = [
            (-1, "1969-12-31T23:59:59Z"),
            (0, "1970-01-01T00:00:00Z"),
            (86_400, "1970-01-02T00:00:00Z"),
            (951_782_400, "2000-02-29T00:00:00Z"),
            (2_147_483_647, "2038-01-19T03:14:07Z"),
            (253_402_300_799, "9999-12-31T23:59:59Z"),
        ];
        for (seconds, expected) in cases {
            let actual = canonical_utc_from_unix_seconds(seconds).unwrap();
            assert_eq!(actual, expected);
            assert_eq!(actual.len(), 20);
            assert!(actual.ends_with('Z'));
        }
    }

    #[test]
    fn utc_formatter_rejects_values_outside_the_four_digit_contract() {
        assert!(canonical_utc_from_unix_seconds(253_402_300_800).is_err());
    }

    #[test]
    fn zoneinfo_path_yields_a_bounded_iana_name() {
        assert_eq!(
            zoneinfo_name_from_path(Path::new("/var/db/timezone/zoneinfo/Asia/Shanghai")),
            Some("Asia/Shanghai".to_string())
        );
        assert_eq!(
            zoneinfo_name_from_path(Path::new("/tmp/zoneinfo/../escape")),
            None
        );
    }

    #[test]
    fn local_clock_context_has_a_valid_contract_offset() {
        let (time_zone, offset) = local_clock_context(1_787_888_000).unwrap();
        assert!(!time_zone.is_empty());
        assert!((-86_400..=86_400).contains(&offset));
    }

    #[test]
    fn host_info_reports_unconfigured_brokers_as_typed_unavailable() {
        let capabilities = host_capability_states(true);
        for interface in ["scheduler", "notification", "system-metrics", "http"] {
            let capability = capabilities
                .iter()
                .find(|capability| capability.interface_name == interface)
                .unwrap_or_else(|| panic!("{interface} capability row"));
            assert!(matches!(
                capability.availability,
                host_info::Availability::Unavailable
            ));
            assert!(!capability.granted);
        }
    }

    #[test]
    fn host_info_reports_only_configured_base_brokers_as_granted() {
        let capabilities = host_capability_states(true);
        for interface in ["clock", "log", "host-info", "settings", "kv"] {
            let capability = capabilities
                .iter()
                .find(|capability| capability.interface_name == interface)
                .unwrap_or_else(|| panic!("{interface} capability row"));
            assert!(matches!(
                capability.availability,
                host_info::Availability::Brokered
            ));
            assert!(capability.granted);
        }
    }

    #[test]
    fn descriptor_inspection_imports_have_no_live_effect_authority() {
        let mut state = InspectionState {
            limits: StoreLimitsBuilder::new()
                .memory_size(64 * 1024 * 1024)
                .build(),
        };
        assert!(clock::Host::wall_now(&mut state).is_err());
        assert_eq!(clock::Host::monotonic_now(&mut state), 0);
        assert!(scheduler::Host::list_schedules(&mut state).is_err());
        assert!(kv::Host::get(&mut state, "key".to_string()).is_err());
        assert!(
            log::Host::write(
                &mut state,
                log::Level::Info,
                "message".to_string(),
                Vec::new(),
                "inspection-log".to_string(),
            )
            .is_err()
        );
        assert!(settings::Host::current(&mut state).is_err());
        assert!(system_metrics::Host::read(&mut state, Vec::new()).is_err());
        assert!(
            http::Host::send(
                &mut state,
                http::Request {
                    method: http::Method::Get,
                    url: "https://example.invalid/".to_string(),
                    headers: Vec::new(),
                    body: Vec::new(),
                    timeout_ms: 1,
                    maximum_response_bytes: 1,
                    idempotency_key: "inspection-http".to_string(),
                },
            )
            .is_err()
        );
        assert!(
            notification::Host::show(
                &mut state,
                notification::NotificationRequest {
                    projection: common::NotificationProjection {
                        title: "inspection".to_string(),
                        body: "inspection".to_string(),
                        standard_sound: false,
                    },
                    idempotency_key: "inspection-notification".to_string(),
                },
            )
            .is_err()
        );
        let description = host_info::Host::describe_host(&mut state);
        assert!(description.capabilities.iter().all(|capability| {
            matches!(
                capability.availability,
                host_info::Availability::Unavailable
            ) && !capability.granted
        }));
    }

    #[test]
    fn action_fields_accept_each_declared_kind_empty_and_boolean_false() {
        let current = trusted_surface(&[
            ("text", ui::FieldKind::Text, false),
            ("integer", ui::FieldKind::Integer, false),
            ("decimal", ui::FieldKind::Decimal, false),
            ("boolean", ui::FieldKind::Boolean, false),
            ("date", ui::FieldKind::Date, false),
            ("time", ui::FieldKind::Time, false),
            ("time-zone", ui::FieldKind::TimeZone, false),
            ("choice", ui::FieldKind::Choice, false),
            ("optional", ui::FieldKind::Text, false),
        ]);
        let command = json!({
            "action": "submit",
            "fields": [
                {"field": "text", "value": {"tag": "text", "value": "Ada"}},
                {"field": "integer", "value": {"tag": "integer", "value": -3}},
                {"field": "decimal", "value": {"tag": "decimal", "value": "1.25"}},
                {"field": "boolean", "value": {"tag": "boolean", "value": false}},
                {"field": "date", "value": {"tag": "date", "value": {"year": 2026, "month": 8, "day": 28}}},
                {"field": "time", "value": {"tag": "time", "value": {"hour": 12, "minute": 34, "second": 56}}},
                {"field": "time-zone", "value": {"tag": "time-zone", "value": "Asia/Shanghai"}},
                {"field": "choice", "value": {"tag": "choice", "value": "daily"}},
                {"field": "optional", "value": {"tag": "empty"}}
            ]
        });

        let (_, fields) = parse_action_fields(&command, &current).unwrap();

        assert_eq!(fields.len(), 9);
        assert!(fields.iter().any(|field| {
            field.field == "boolean" && matches!(field.value, ui::FieldValue::Boolean(false))
        }));
        assert!(fields.iter().any(|field| {
            field.field == "optional" && matches!(field.value, ui::FieldValue::Empty)
        }));
    }

    #[test]
    fn action_fields_reject_a_nonempty_type_mismatch() {
        let current = trusted_surface(&[("name", ui::FieldKind::Text, false)]);
        let error = parse_action_fields(
            &json!({
                "action": "submit",
                "fields": [{"field": "name", "value": {"tag": "integer", "value": 7}}]
            }),
            &current,
        )
        .unwrap_err();

        assert!(error.contains("does not match"));
    }

    #[test]
    fn multiline_text_round_trips_but_identifiers_and_secret_handles_stay_strict() {
        let current = trusted_surface(&[("body", ui::FieldKind::Text, false), ("secret", ui::FieldKind::Text, true)]);
        let text = "\n第一行\n\t第二行\r\n";
        let command = json!({"action": "submit", "fields": [{"field": "body", "value": {"tag": "text", "value": text}}]});
        let (_, fields) = parse_action_fields(&command, &current).unwrap();
        assert!(matches!(&fields[0].value, ui::FieldValue::Text(value) if value == text));
        for value in ["nul\0", "escape\u{1b}", "delete\u{7f}"] {
            let invalid = json!({"action": "submit", "fields": [{"field": "body", "value": {"tag": "text", "value": value}}]});
            assert!(parse_action_fields(&invalid, &current).is_err());
        }
        let secret = json!({"action": "submit", "fields": [{"field": "secret", "value": {"tag": "secret-handle", "value": "handle\nforged"}}]});
        assert!(parse_action_fields(&secret, &current).is_err());
    }

    #[test]
    fn sensitive_action_fields_require_a_nonempty_opaque_handle() {
        let current = trusted_surface(&[("password", ui::FieldKind::Text, true)]);
        let (_, accepted) = parse_action_fields(
            &json!({
                "action": "submit",
                "fields": [{"field": "password", "value": {"tag": "secret-handle", "value": "secret:password:1"}}]
            }),
            &current,
        )
        .unwrap();
        assert!(matches!(
            accepted[0].value,
            ui::FieldValue::SecretHandle(ref handle) if handle == "secret:password:1"
        ));

        for value in [
            json!({"tag": "text", "value": "plaintext"}),
            json!({"tag": "empty"}),
            json!({"tag": "secret-handle", "value": ""}),
        ] {
            let error = parse_action_fields(
                &json!({
                    "action": "submit",
                    "fields": [{"field": "password", "value": value}]
                }),
                &current,
            )
            .unwrap_err();
            assert!(error.contains("secret handle"));
        }
    }

    #[test]
    fn action_fields_keep_structural_rejections() {
        let current = trusted_surface(&[("name", ui::FieldKind::Text, false)]);
        let undeclared = json!({
            "action": "submit",
            "fields": [{"field": "other", "value": {"tag": "text", "value": "Ada"}}]
        });
        assert!(parse_action_fields(&undeclared, &current).is_err());

        let duplicate = json!({
            "action": "submit",
            "fields": [
                {"field": "name", "value": {"tag": "text", "value": "Ada"}},
                {"field": "name", "value": {"tag": "text", "value": "Grace"}}
            ]
        });
        assert!(parse_action_fields(&duplicate, &current).is_err());

        let malformed = json!({
            "action": "submit",
            "fields": [{"field": "name", "value": {"tag": "unknown", "value": null}}]
        });
        assert!(parse_action_fields(&malformed, &current).is_err());
    }
}
