use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::native_platform;

const SETTINGS_SCHEMA: &str = "vibapp.p2p-network-settings.experimental-v1";
const SETTINGS_FILE: &str = "p2p-network.json";
const SECRETS_FILE: &str = "p2p-network-secrets.json";
const MAX_SETTINGS_BYTES: u64 = 32 * 1024;
const MAX_RATE_KIB_PER_SECOND: u64 = 1_048_576;
const MAX_CACHE_MIB: u64 = 1_048_576;
const MAX_TURN_URLS: usize = 2;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredNetworkSettings {
    schema_version: String,
    p2p_enabled: bool,
    seed_verified_apps: bool,
    allow_user_file_seeding: bool,
    upload_limit_kib_per_second: u64,
    download_limit_kib_per_second: u64,
    cache_limit_mib: u64,
    max_active_transfers: u64,
    rtc_enabled: bool,
    max_active_channels: u64,
    turn_enabled: bool,
    turn_urls: Vec<String>,
    turn_username: String,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredNetworkSecrets {
    #[serde(default)]
    turn_credential: Option<String>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct SaveNetworkSettingsInput {
    p2p_enabled: bool,
    seed_verified_apps: bool,
    allow_user_file_seeding: bool,
    upload_limit_kib_per_second: u64,
    download_limit_kib_per_second: u64,
    cache_limit_mib: u64,
    max_active_transfers: u64,
    rtc_enabled: bool,
    max_active_channels: u64,
    turn_enabled: bool,
    #[serde(default)]
    turn_urls: Vec<String>,
    #[serde(default)]
    turn_username: String,
    #[serde(default)]
    turn_credential: Option<String>,
    #[serde(default)]
    clear_turn_credential: bool,
}

impl Default for StoredNetworkSettings {
    fn default() -> Self {
        Self {
            schema_version: SETTINGS_SCHEMA.to_string(),
            p2p_enabled: true,
            seed_verified_apps: true,
            allow_user_file_seeding: false,
            upload_limit_kib_per_second: 1_024,
            download_limit_kib_per_second: 4_096,
            cache_limit_mib: 2_048,
            max_active_transfers: 8,
            rtc_enabled: true,
            max_active_channels: 8,
            turn_enabled: false,
            turn_urls: Vec::new(),
            turn_username: String::new(),
        }
    }
}

fn settings_dir(data_dir: &Path) -> Result<PathBuf, String> {
    let path = data_dir.join("settings");
    fs::create_dir_all(&path).map_err(|error| format!("无法创建 P2P 网络设置目录：{error}"))?;
    native_platform::protect_private_directory(&path)?;
    Ok(path)
}

fn read_bounded_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<Option<T>, String> {
    let metadata = match path.symlink_metadata() {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("无法读取 P2P 网络设置元数据：{error}")),
    };
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_SETTINGS_BYTES
    {
        return Err("P2P 网络设置必须是安全的有界普通文件。".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("无法读取 P2P 网络设置：{error}"))?;
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|error| format!("P2P 网络设置不是有效 JSON：{error}"))
}

fn write_private_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let bytes =
        serde_json::to_vec(value).map_err(|error| format!("无法编码 P2P 网络设置：{error}"))?;
    if bytes.is_empty() || bytes.len() as u64 > MAX_SETTINGS_BYTES {
        return Err("P2P 网络设置超过 32 KiB 上限。".to_string());
    }
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = path.with_extension(format!("tmp-{nonce:x}"));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|error| format!("无法创建 P2P 网络临时设置：{error}"))?;
    let result = file.write_all(&bytes).and_then(|_| file.sync_all());
    if let Err(error) = result {
        let _ = fs::remove_file(&temporary);
        return Err(format!("无法持久化 P2P 网络设置：{error}"));
    }
    fs::rename(&temporary, path).map_err(|error| {
        let _ = fs::remove_file(&temporary);
        format!("无法提交 P2P 网络设置：{error}")
    })?;
    native_platform::protect_private_file(path)?;
    Ok(())
}

fn validate_text(
    value: &str,
    maximum: usize,
    label: &str,
    allow_empty: bool,
) -> Result<(), String> {
    if (!allow_empty && value.is_empty())
        || value.chars().count() > maximum
        || value.chars().any(|character| character.is_control())
    {
        return Err(format!("{label} 格式无效。"));
    }
    Ok(())
}

fn validate_turn_url(value: &str) -> Result<(), String> {
    validate_text(value, 512, "TURN URL", false)?;
    if value.contains(char::is_whitespace)
        || value.contains('@')
        || value.contains('#')
        || value.contains('/')
        || !(value.starts_with("turn:") || value.starts_with("turns:"))
    {
        return Err("TURN URL 必须使用 turn: 或 turns:，且不能内嵌凭据。".to_string());
    }
    let authority = value
        .split_once(':')
        .map(|(_, tail)| tail.split('?').next().unwrap_or_default())
        .unwrap_or_default();
    if authority.is_empty() {
        return Err("TURN URL 缺少服务器地址。".to_string());
    }
    let port = if authority.starts_with('[') {
        let end = authority
            .find(']')
            .ok_or_else(|| "TURN URL 的 IPv6 地址缺少右方括号。".to_string())?;
        if end == 1 {
            return Err("TURN URL 缺少服务器地址。".to_string());
        }
        match &authority[end + 1..] {
            "" => None,
            suffix if suffix.starts_with(':') && suffix.len() > 1 => Some(&suffix[1..]),
            _ => return Err("TURN URL 的 IPv6 地址或端口格式无效。".to_string()),
        }
    } else {
        if authority.matches(':').count() > 1 {
            return Err("TURN URL 中的 IPv6 地址必须使用方括号。".to_string());
        }
        match authority.rsplit_once(':') {
            Some((host, port)) if !host.is_empty() && !port.is_empty() => Some(port),
            Some(_) => return Err("TURN URL 的服务器或端口格式无效。".to_string()),
            None => None,
        }
    };
    if let Some(port) = port {
        if port.parse::<u16>().ok().filter(|port| *port > 0).is_none() {
            return Err("TURN URL 端口无效。".to_string());
        }
    }
    if let Some(query) = value.split_once('?').map(|(_, query)| query)
        && !matches!(query, "transport=udp" | "transport=tcp")
    {
        return Err("TURN URL 仅支持 transport=udp 或 transport=tcp。".to_string());
    }
    Ok(())
}

fn validate_settings(
    value: &StoredNetworkSettings,
    secrets: &StoredNetworkSecrets,
) -> Result<(), String> {
    if value.schema_version != SETTINGS_SCHEMA {
        return Err("P2P 网络设置版本不受支持。".to_string());
    }
    if value.upload_limit_kib_per_second > MAX_RATE_KIB_PER_SECOND
        || value.download_limit_kib_per_second > MAX_RATE_KIB_PER_SECOND
    {
        return Err("P2P 上传或下载限速超出支持范围。".to_string());
    }
    if !(128..=MAX_CACHE_MIB).contains(&value.cache_limit_mib) {
        return Err("P2P 缓存上限必须在 128 MiB 到 1 TiB 之间。".to_string());
    }
    if !(1..=64).contains(&value.max_active_transfers) {
        return Err("同时传输任务数必须在 1 到 64 之间。".to_string());
    }
    if !(1..=32).contains(&value.max_active_channels) {
        return Err("同时协作频道数必须在 1 到 32 之间。".to_string());
    }
    if value.turn_urls.len() > MAX_TURN_URLS {
        return Err("TURN 服务器最多配置两个。".to_string());
    }
    for url in &value.turn_urls {
        validate_turn_url(url)?;
    }
    validate_text(
        &value.turn_username,
        256,
        "TURN 用户名",
        !value.turn_enabled,
    )?;
    if value.turn_enabled {
        if !value.rtc_enabled || value.turn_urls.is_empty() || secrets.turn_credential.is_none() {
            return Err("启用 TURN 时必须同时启用 RTC，并填写服务器、用户名和凭据。".to_string());
        }
    }
    if let Some(credential) = &secrets.turn_credential {
        validate_text(credential, 512, "TURN 凭据", false)?;
    }
    Ok(())
}

fn read_stored(data_dir: &Path) -> Result<(StoredNetworkSettings, StoredNetworkSecrets), String> {
    let directory = settings_dir(data_dir)?;
    let settings_path = directory.join(SETTINGS_FILE);
    let secrets_path = directory.join(SECRETS_FILE);
    for path in [&settings_path, &secrets_path] {
        match path.symlink_metadata() {
            Ok(_) => native_platform::verify_private_file(path)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("无法检查 P2P 网络设置权限：{error}")),
        }
    }
    let settings = read_bounded_json(&settings_path)?.unwrap_or_default();
    let secrets = read_bounded_json(&secrets_path)?.unwrap_or_default();
    validate_settings(&settings, &secrets)?;
    Ok((settings, secrets))
}

fn public_view(settings: &StoredNetworkSettings, secrets: &StoredNetworkSecrets) -> Value {
    json!({
        "schemaVersion": SETTINGS_SCHEMA,
        "p2pEnabled": settings.p2p_enabled,
        "seedVerifiedApps": settings.seed_verified_apps,
        "allowUserFileSeeding": settings.allow_user_file_seeding,
        "uploadLimitKibPerSecond": settings.upload_limit_kib_per_second,
        "downloadLimitKibPerSecond": settings.download_limit_kib_per_second,
        "cacheLimitMib": settings.cache_limit_mib,
        "maxActiveTransfers": settings.max_active_transfers,
        "rtcEnabled": settings.rtc_enabled,
        "maxActiveChannels": settings.max_active_channels,
        "turnEnabled": settings.turn_enabled,
        "turnUrls": settings.turn_urls,
        "turnUsername": settings.turn_username,
        "hasTurnCredential": secrets.turn_credential.is_some(),
        "restartRequired": true,
        "transport": {
            "implementation": "roomhash-current",
            "fileDistribution": "webtorrent",
            "realtimeCollaboration": "webrtc-data-channel",
            "packageIdentity": "vibapp-sha256-not-bittorrent-infohash"
        },
        "dataScopes": {
            "localPrivate": {
                "kind": "local-private",
                "networkJoinAllowed": false,
                "compatibilityNilUuid": "00000000-0000-0000-0000-000000000000"
            },
            "shared": {
                "kind": "temporary-channel",
                "requiresExplicitGrant": true
            }
        }
    })
}

pub fn get(data_dir: &Path) -> Result<Value, String> {
    let (settings, secrets) = read_stored(data_dir)?;
    Ok(public_view(&settings, &secrets))
}

/// Builds the private, typed configuration passed only to the trusted RoomHash
/// host process. Callers must never serialize this value to the GUI because it
/// may contain the write-only TURN credential.
pub fn roomhash_host_config(data_dir: &Path) -> Result<Value, String> {
    let (settings, secrets) = read_stored(data_dir)?;
    let upload_limit_bps = settings
        .upload_limit_kib_per_second
        .checked_mul(1_024)
        .ok_or_else(|| "P2P 上传限速无法转换为字节速率。".to_string())?;
    let download_limit_bps = settings
        .download_limit_kib_per_second
        .checked_mul(1_024)
        .ok_or_else(|| "P2P 下载限速无法转换为字节速率。".to_string())?;
    let max_conns = settings
        .max_active_transfers
        .saturating_mul(4)
        .clamp(4, 128);
    let turn = if settings.turn_enabled {
        json!({
            "urls": settings.turn_urls,
            "username": settings.turn_username,
            "credential": secrets.turn_credential
        })
    } else {
        Value::Null
    };
    Ok(json!({
        "upload_limit_bps": upload_limit_bps,
        "download_limit_bps": download_limit_bps,
        "max_conns": max_conns,
        "dht": true,
        "lsd": true,
        "pex": true,
        "tracker_urls": [
            "wss://tracker.webtorrent.dev",
            "wss://tracker.openwebtorrent.com",
            "wss://tracker.btorrent.xyz"
        ],
        "turn": turn
    }))
}

pub fn save(data_dir: &Path, input: SaveNetworkSettingsInput) -> Result<Value, String> {
    let directory = settings_dir(data_dir)?;
    let (_, mut secrets) = read_stored(data_dir)?;
    if input.clear_turn_credential {
        secrets.turn_credential = None;
    }
    if let Some(value) = input.turn_credential {
        let value = value.trim().to_string();
        if !value.is_empty() {
            secrets.turn_credential = Some(value);
        }
    }
    let settings = StoredNetworkSettings {
        schema_version: SETTINGS_SCHEMA.to_string(),
        p2p_enabled: input.p2p_enabled,
        seed_verified_apps: input.seed_verified_apps,
        allow_user_file_seeding: input.allow_user_file_seeding,
        upload_limit_kib_per_second: input.upload_limit_kib_per_second,
        download_limit_kib_per_second: input.download_limit_kib_per_second,
        cache_limit_mib: input.cache_limit_mib,
        max_active_transfers: input.max_active_transfers,
        rtc_enabled: input.rtc_enabled,
        max_active_channels: input.max_active_channels,
        turn_enabled: input.turn_enabled,
        turn_urls: input
            .turn_urls
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .collect(),
        turn_username: input.turn_username.trim().to_string(),
    };
    validate_settings(&settings, &secrets)?;
    write_private_json(&directory.join(SETTINGS_FILE), &settings)?;
    write_private_json(&directory.join(SECRETS_FILE), &secrets)?;
    Ok(public_view(&settings, &secrets))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root(label: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("p2p-network-settings-{label}-{suffix:x}"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn safe_bounded_defaults_are_visible_without_exposing_turn_secrets() {
        let value = get(&root("default")).unwrap();
        assert_eq!(value["p2pEnabled"], true);
        assert_eq!(value["seedVerifiedApps"], true);
        assert_eq!(value["allowUserFileSeeding"], false);
        assert_eq!(value["uploadLimitKibPerSecond"], 1_024);
        assert_eq!(value["hasTurnCredential"], false);
        assert_eq!(
            value["dataScopes"]["localPrivate"]["networkJoinAllowed"],
            false
        );
    }

    #[test]
    fn round_trip_preserves_limits_and_redacts_turn_credential() {
        let data = root("round-trip");
        let saved = save(
            &data,
            SaveNetworkSettingsInput {
                p2p_enabled: true,
                seed_verified_apps: true,
                allow_user_file_seeding: true,
                upload_limit_kib_per_second: 2_048,
                download_limit_kib_per_second: 8_192,
                cache_limit_mib: 4_096,
                max_active_transfers: 12,
                rtc_enabled: true,
                max_active_channels: 12,
                turn_enabled: true,
                turn_urls: vec!["turns:turn.example.com:5349?transport=tcp".to_string()],
                turn_username: "vibapp".to_string(),
                turn_credential: Some("private-turn-secret".to_string()),
                clear_turn_credential: false,
            },
        )
        .unwrap();
        assert_eq!(saved["uploadLimitKibPerSecond"], 2_048);
        assert_eq!(saved["hasTurnCredential"], true);
        assert!(!saved.to_string().contains("private-turn-secret"));
    }

    #[test]
    fn invalid_or_incomplete_turn_configuration_fails_closed() {
        let data = root("turn-invalid");
        assert!(
            save(
                &data,
                SaveNetworkSettingsInput {
                    p2p_enabled: true,
                    seed_verified_apps: true,
                    allow_user_file_seeding: false,
                    upload_limit_kib_per_second: 1_024,
                    download_limit_kib_per_second: 4_096,
                    cache_limit_mib: 2_048,
                    max_active_transfers: 8,
                    rtc_enabled: true,
                    max_active_channels: 8,
                    turn_enabled: true,
                    turn_urls: vec!["https://not-turn.example.com".to_string()],
                    turn_username: "vibapp".to_string(),
                    turn_credential: Some("secret".to_string()),
                    clear_turn_credential: false,
                },
            )
            .is_err()
        );
        assert!(validate_turn_url("turn:host.invalid:not-a-port").is_err());
        assert!(validate_turn_url("turn:[2001:db8::1]:3478?transport=udp").is_ok());
    }

    #[test]
    fn private_host_config_converts_rates_and_is_separate_from_public_view() {
        let data = root("host-config");
        let _ = save(
            &data,
            SaveNetworkSettingsInput {
                p2p_enabled: true,
                seed_verified_apps: true,
                allow_user_file_seeding: false,
                upload_limit_kib_per_second: 2_048,
                download_limit_kib_per_second: 8_192,
                cache_limit_mib: 2_048,
                max_active_transfers: 12,
                rtc_enabled: true,
                max_active_channels: 8,
                turn_enabled: true,
                turn_urls: vec!["turns:turn.example.com:5349?transport=tcp".to_string()],
                turn_username: "vibapp".to_string(),
                turn_credential: Some("write-only-secret".to_string()),
                clear_turn_credential: false,
            },
        )
        .unwrap();
        let host = roomhash_host_config(&data).unwrap();
        assert_eq!(host["upload_limit_bps"], 2_097_152);
        assert_eq!(host["download_limit_bps"], 8_388_608);
        assert_eq!(host["max_conns"], 48);
        assert_eq!(host["turn"]["credential"], "write-only-secret");
        assert!(
            !get(&data)
                .unwrap()
                .to_string()
                .contains("write-only-secret")
        );
    }
}
