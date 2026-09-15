use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::native_platform;

const SETTINGS_SCHEMA: &str = "vibapp.model-settings.experimental-v1";
const SETTINGS_FILE: &str = "model-settings.json";
const SECRETS_FILE: &str = "model-secrets.json";
const MAX_SETTINGS_BYTES: u64 = 32 * 1024;
const DEFAULT_GENERATION_URL: &str = "http://192.168.199.170:8081";
const DEFAULT_GENERATION_MODEL: &str = "qwen3.8-27b-uncensored-mtp-q4";
const DEFAULT_EMBEDDING_URL: &str = "http://192.168.199.170:8081";
const DEFAULT_EMBEDDING_MODEL: &str = "text-embedding-ada-002";

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredModelSettings {
    schema_version: String,
    generation: StoredGenerationSettings,
    embedding: StoredEmbeddingSettings,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredGenerationSettings {
    enabled: bool,
    base_url: String,
    model: String,
    protocol: String,
    timeout_seconds: u64,
    max_output_tokens: u64,
    temperature: f64,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredEmbeddingSettings {
    enabled: bool,
    base_url: String,
    model: String,
    dimensions: usize,
    timeout_seconds: u64,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct StoredSecrets {
    #[serde(default)]
    generation_api_key: Option<String>,
    #[serde(default)]
    embedding_api_key: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct SaveModelSettingsInput {
    generation: GenerationSettingsInput,
    embedding: EmbeddingSettingsInput,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct GenerationSettingsInput {
    enabled: bool,
    base_url: String,
    model: String,
    protocol: String,
    timeout_seconds: u64,
    max_output_tokens: u64,
    temperature: f64,
    #[serde(default)]
    api_key: Option<String>,
    #[serde(default)]
    clear_api_key: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct EmbeddingSettingsInput {
    enabled: bool,
    base_url: String,
    model: String,
    dimensions: usize,
    timeout_seconds: u64,
    #[serde(default)]
    api_key: Option<String>,
    #[serde(default)]
    clear_api_key: bool,
}

#[derive(Clone, Serialize)]
pub struct GenerationRuntimeSettings {
    pub enabled: bool,
    pub base_url: String,
    pub model: String,
    pub protocol: String,
    pub timeout_seconds: u64,
    pub max_output_tokens: u64,
    pub temperature: f64,
    pub api_key: Option<String>,
}

#[derive(Clone, Serialize)]
pub struct EmbeddingRuntimeSettings {
    pub enabled: bool,
    pub base_url: String,
    pub model: String,
    pub dimensions: usize,
    pub timeout_seconds: u64,
    pub api_key: Option<String>,
}

pub struct RuntimeModelSettings {
    pub generation: GenerationRuntimeSettings,
    pub embedding: EmbeddingRuntimeSettings,
}

impl Default for StoredModelSettings {
    fn default() -> Self {
        Self {
            schema_version: SETTINGS_SCHEMA.to_string(),
            generation: StoredGenerationSettings {
                enabled: true,
                base_url: DEFAULT_GENERATION_URL.to_string(),
                model: DEFAULT_GENERATION_MODEL.to_string(),
                protocol: "chat-completions".to_string(),
                timeout_seconds: 45,
                max_output_tokens: 512,
                temperature: 0.0,
            },
            embedding: StoredEmbeddingSettings {
                enabled: true,
                base_url: DEFAULT_EMBEDDING_URL.to_string(),
                model: DEFAULT_EMBEDDING_MODEL.to_string(),
                dimensions: 384,
                timeout_seconds: 5,
            },
        }
    }
}

fn settings_dir(data_dir: &Path) -> Result<PathBuf, String> {
    let path = data_dir.join("settings");
    fs::create_dir_all(&path).map_err(|error| format!("无法创建模型设置目录：{error}"))?;
    native_platform::protect_private_directory(&path)?;
    Ok(path)
}

fn read_bounded_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<Option<T>, String> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("无法读取模型设置元数据：{error}")),
    };
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err("模型设置必须是普通非符号链接文件。".to_string());
    }
    if metadata.len() > MAX_SETTINGS_BYTES {
        return Err("模型设置超过 32 KiB 上限。".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("无法读取模型设置：{error}"))?;
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|error| format!("模型设置不是有效 JSON：{error}"))
}

fn write_private_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let bytes = serde_json::to_vec(value).map_err(|error| format!("无法编码模型设置：{error}"))?;
    if bytes.is_empty() || bytes.len() as u64 > MAX_SETTINGS_BYTES {
        return Err("模型设置超过 32 KiB 上限。".to_string());
    }
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temp = path.with_extension(format!("tmp-{nonce:x}"));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temp)
        .map_err(|error| format!("无法创建模型设置临时文件：{error}"))?;
    let result = file
        .write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法持久化模型设置：{error}"));
    if let Err(error) = result {
        let _ = fs::remove_file(&temp);
        return Err(error);
    }
    fs::rename(&temp, path).map_err(|error| {
        let _ = fs::remove_file(&temp);
        format!("无法提交模型设置：{error}")
    })?;
    native_platform::protect_private_file(path)?;
    Ok(())
}

fn read_stored(data_dir: &Path) -> Result<StoredModelSettings, String> {
    let path = settings_dir(data_dir)?.join(SETTINGS_FILE);
    let settings = read_bounded_json::<StoredModelSettings>(&path)?.unwrap_or_default();
    validate_stored(&settings)?;
    Ok(settings)
}

fn read_secrets(data_dir: &Path) -> Result<StoredSecrets, String> {
    let path = settings_dir(data_dir)?.join(SECRETS_FILE);
    if path.exists() {
        native_platform::verify_private_file(&path)?;
    }
    let secrets = read_bounded_json::<StoredSecrets>(&path)?.unwrap_or_default();
    if let Some(value) = &secrets.generation_api_key {
        validate_secret(value)?;
    }
    if let Some(value) = &secrets.embedding_api_key {
        validate_secret(value)?;
    }
    Ok(secrets)
}

fn validate_text(value: &str, label: &str, maximum: usize) -> Result<(), String> {
    if value.is_empty()
        || value.len() > maximum
        || value
            .chars()
            .any(|character| character.is_control() || character == '\u{7f}')
    {
        return Err(format!("{label} 格式无效。"));
    }
    Ok(())
}

fn validate_secret(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 8192
        || value.chars().any(|character| character.is_control())
    {
        return Err("API Key 格式无效。".to_string());
    }
    Ok(())
}

fn validate_url(value: &str, label: &str) -> Result<(), String> {
    validate_text(value, label, 512)?;
    if value.contains(char::is_whitespace)
        || value.contains('@')
        || value.contains('#')
        || value.contains('?')
        || value.ends_with('/')
    {
        return Err(format!(
            "{label} 必须是不含凭据、查询或片段的服务 Base URL。"
        ));
    }
    let (scheme, authority_and_path) = value
        .split_once("://")
        .ok_or_else(|| format!("{label} 必须使用 http 或 https。"))?;
    if !matches!(scheme, "http" | "https") || authority_and_path.is_empty() {
        return Err(format!("{label} 必须使用 http 或 https。"));
    }
    let authority = authority_and_path.split('/').next().unwrap_or_default();
    if authority.is_empty() || authority.starts_with('.') || authority.ends_with('.') {
        return Err(format!("{label} 缺少有效主机名。"));
    }
    if scheme == "http" {
        let host = authority
            .trim_start_matches('[')
            .split(']')
            .next()
            .unwrap_or(authority)
            .split(':')
            .next()
            .unwrap_or_default()
            .to_ascii_lowercase();
        let private = host == "localhost"
            || host == "127.0.0.1"
            || host == "::1"
            || host.starts_with("10.")
            || host.starts_with("192.168.")
            || host
                .strip_prefix("172.")
                .and_then(|tail| tail.split('.').next())
                .and_then(|octet| octet.parse::<u8>().ok())
                .is_some_and(|octet| (16..=31).contains(&octet));
        if !private {
            return Err(format!(
                "{label} 的远程地址必须使用 HTTPS；HTTP 仅允许本机或私有局域网。"
            ));
        }
    }
    Ok(())
}

fn validate_stored(value: &StoredModelSettings) -> Result<(), String> {
    if value.schema_version != SETTINGS_SCHEMA {
        return Err("模型设置版本不受支持。".to_string());
    }
    validate_url(&value.generation.base_url, "生成模型地址")?;
    validate_text(&value.generation.model, "生成模型名称", 256)?;
    if !matches!(
        value.generation.protocol.as_str(),
        "chat-completions" | "responses"
    ) {
        return Err("生成模型协议必须是 Chat Completions 或 Responses。".to_string());
    }
    if !(5..=120).contains(&value.generation.timeout_seconds)
        || !(128..=4096).contains(&value.generation.max_output_tokens)
        || !value.generation.temperature.is_finite()
        || !(0.0..=2.0).contains(&value.generation.temperature)
    {
        return Err("生成模型资源参数超出允许范围。".to_string());
    }
    validate_url(&value.embedding.base_url, "向量模型地址")?;
    validate_text(&value.embedding.model, "向量模型名称", 256)?;
    if !(1..=8192).contains(&value.embedding.dimensions)
        || !(1..=30).contains(&value.embedding.timeout_seconds)
    {
        return Err("向量模型资源参数超出允许范围。".to_string());
    }
    Ok(())
}

fn public_view(settings: &StoredModelSettings, secrets: &StoredSecrets) -> Value {
    json!({
        "schemaVersion": SETTINGS_SCHEMA,
        "generation": {
            "enabled": settings.generation.enabled,
            "baseUrl": settings.generation.base_url,
            "model": settings.generation.model,
            "protocol": settings.generation.protocol,
            "timeoutSeconds": settings.generation.timeout_seconds,
            "maxOutputTokens": settings.generation.max_output_tokens,
            "temperature": settings.generation.temperature,
            "hasApiKey": secrets.generation_api_key.is_some()
        },
        "embedding": {
            "enabled": settings.embedding.enabled,
            "baseUrl": settings.embedding.base_url,
            "model": settings.embedding.model,
            "dimensions": settings.embedding.dimensions,
            "timeoutSeconds": settings.embedding.timeout_seconds,
            "hasApiKey": secrets.embedding_api_key.is_some()
        },
        "appliesTo": ["need-analysis", "registry-embedding"],
        "codeAgent": {
            "managedSeparately": true,
            "affectedByBringYourOwnModel": false
        }
    })
}

pub fn get(data_dir: &Path) -> Result<Value, String> {
    let settings = read_stored(data_dir)?;
    let secrets = read_secrets(data_dir)?;
    Ok(public_view(&settings, &secrets))
}

pub fn runtime(data_dir: &Path) -> Result<RuntimeModelSettings, String> {
    let settings = read_stored(data_dir)?;
    let secrets = read_secrets(data_dir)?;
    Ok(RuntimeModelSettings {
        generation: GenerationRuntimeSettings {
            enabled: settings.generation.enabled,
            base_url: settings.generation.base_url,
            model: settings.generation.model,
            protocol: settings.generation.protocol,
            timeout_seconds: settings.generation.timeout_seconds,
            max_output_tokens: settings.generation.max_output_tokens,
            temperature: settings.generation.temperature,
            api_key: secrets.generation_api_key,
        },
        embedding: EmbeddingRuntimeSettings {
            enabled: settings.embedding.enabled,
            base_url: settings.embedding.base_url,
            model: settings.embedding.model,
            dimensions: settings.embedding.dimensions,
            timeout_seconds: settings.embedding.timeout_seconds,
            api_key: secrets.embedding_api_key,
        },
    })
}

pub fn save(data_dir: &Path, input: SaveModelSettingsInput) -> Result<Value, String> {
    let settings = StoredModelSettings {
        schema_version: SETTINGS_SCHEMA.to_string(),
        generation: StoredGenerationSettings {
            enabled: input.generation.enabled,
            base_url: input.generation.base_url.trim_end_matches('/').to_string(),
            model: input.generation.model.trim().to_string(),
            protocol: input.generation.protocol,
            timeout_seconds: input.generation.timeout_seconds,
            max_output_tokens: input.generation.max_output_tokens,
            temperature: input.generation.temperature,
        },
        embedding: StoredEmbeddingSettings {
            enabled: input.embedding.enabled,
            base_url: input.embedding.base_url.trim_end_matches('/').to_string(),
            model: input.embedding.model.trim().to_string(),
            dimensions: input.embedding.dimensions,
            timeout_seconds: input.embedding.timeout_seconds,
        },
    };
    validate_stored(&settings)?;
    let mut secrets = read_secrets(data_dir)?;
    if input.generation.clear_api_key {
        secrets.generation_api_key = None;
    } else if let Some(value) = input
        .generation
        .api_key
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
    {
        validate_secret(&value)?;
        secrets.generation_api_key = Some(value);
    }
    if input.embedding.clear_api_key {
        secrets.embedding_api_key = None;
    } else if let Some(value) = input
        .embedding
        .api_key
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
    {
        validate_secret(&value)?;
        secrets.embedding_api_key = Some(value);
    }
    let directory = settings_dir(data_dir)?;
    write_private_json(&directory.join(SETTINGS_FILE), &settings)?;
    write_private_json(&directory.join(SECRETS_FILE), &secrets)?;
    Ok(public_view(&settings, &secrets))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "vibapp-model-settings-{label}-{}-{:x}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        fs::create_dir_all(&path).expect("create test directory");
        path
    }

    #[test]
    fn defaults_keep_code_agent_outside_byom() {
        let root = temp_dir("defaults");
        let view = get(&root).expect("default settings");
        assert_eq!(view["generation"]["model"], DEFAULT_GENERATION_MODEL);
        assert_eq!(view["embedding"]["model"], DEFAULT_EMBEDDING_MODEL);
        assert_eq!(view["codeAgent"]["affectedByBringYourOwnModel"], false);
        fs::remove_dir_all(root).expect("remove test directory");
    }

    #[test]
    fn public_view_never_returns_api_key() {
        let settings = StoredModelSettings::default();
        let secrets = StoredSecrets {
            generation_api_key: Some("generation-secret".to_string()),
            embedding_api_key: Some("embedding-secret".to_string()),
        };
        let encoded = serde_json::to_string(&public_view(&settings, &secrets)).unwrap();
        assert!(!encoded.contains("generation-secret"));
        assert!(!encoded.contains("embedding-secret"));
        assert!(encoded.contains("hasApiKey"));
    }

    #[test]
    fn cleartext_remote_http_is_rejected() {
        assert!(validate_url("http://example.com/v1", "test").is_err());
        assert!(validate_url("http://192.168.199.170:8081", "test").is_ok());
        assert!(validate_url("https://api.example.com/v1", "test").is_ok());
    }

    #[test]
    fn saved_secrets_are_write_only_and_runtime_receives_them() {
        let root = temp_dir("save");
        let view = save(
            &root,
            SaveModelSettingsInput {
                generation: GenerationSettingsInput {
                    enabled: true,
                    base_url: "https://api.example.test/v1".to_string(),
                    model: "custom-chat".to_string(),
                    protocol: "responses".to_string(),
                    timeout_seconds: 30,
                    max_output_tokens: 768,
                    temperature: 0.2,
                    api_key: Some("chat-secret".to_string()),
                    clear_api_key: false,
                },
                embedding: EmbeddingSettingsInput {
                    enabled: true,
                    base_url: "https://api.example.test/v1".to_string(),
                    model: "custom-embedding".to_string(),
                    dimensions: 1024,
                    timeout_seconds: 10,
                    api_key: Some("embedding-secret".to_string()),
                    clear_api_key: false,
                },
            },
        )
        .expect("save model settings");
        let encoded = serde_json::to_string(&view).unwrap();
        assert!(!encoded.contains("chat-secret"));
        assert!(!encoded.contains("embedding-secret"));
        assert_eq!(view["generation"]["hasApiKey"], true);
        assert_eq!(view["embedding"]["hasApiKey"], true);
        let runtime = runtime(&root).expect("runtime settings");
        assert_eq!(runtime.generation.api_key.as_deref(), Some("chat-secret"));
        assert_eq!(
            runtime.embedding.api_key.as_deref(),
            Some("embedding-secret")
        );
        assert_eq!(runtime.generation.protocol, "responses");
        assert_eq!(runtime.embedding.dimensions, 1024);
        #[cfg(unix)]
        {
            let mode = fs::metadata(root.join("settings/model-secrets.json"))
                .expect("secret metadata")
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600);
        }
        fs::remove_dir_all(root).expect("remove test directory");
    }
}
