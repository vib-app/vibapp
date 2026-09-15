use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use crate::native_platform;

const SCHEMA_VERSION: &str = "vibapp.codeagent-settings.experimental-v1";
const MAX_SETTINGS_BYTES: u64 = 64 * 1024;
const DEFAULT_CODEX_MODEL: &str = "gpt-5.6-sol";
const RETIRED_CODEX_MODEL: &str = "gpt-5.6-codex";

#[derive(Clone, Copy)]
struct ProviderDefinition {
    id: &'static str,
    task_provider: &'static str,
    display_name: &'static str,
    executable: &'static str,
    execution_supported: bool,
}

const PROVIDERS: &[ProviderDefinition] = &[
    ProviderDefinition {
        id: "codex",
        task_provider: "openai-codex",
        display_name: "OpenAI Codex",
        executable: "codex",
        execution_supported: true,
    },
    ProviderDefinition {
        id: "claude-code",
        task_provider: "anthropic-claude-code",
        display_name: "Claude Code",
        executable: "claude",
        execution_supported: false,
    },
    ProviderDefinition {
        id: "opencode",
        task_provider: "opencode",
        display_name: "OpenCode",
        executable: "opencode",
        execution_supported: true,
    },
    ProviderDefinition {
        id: "gemini-cli",
        task_provider: "google-gemini-cli",
        display_name: "Gemini CLI",
        executable: "gemini",
        execution_supported: false,
    },
];

#[derive(Clone, Debug, Deserialize, Serialize)]
struct StoredSettings {
    schema_version: String,
    selected_provider: String,
    #[serde(default)]
    model_by_provider: BTreeMap<String, String>,
}

impl Default for StoredSettings {
    fn default() -> Self {
        let mut model_by_provider = BTreeMap::new();
        model_by_provider.insert("codex".to_string(), DEFAULT_CODEX_MODEL.to_string());
        Self {
            schema_version: SCHEMA_VERSION.to_string(),
            selected_provider: "codex".to_string(),
            model_by_provider,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SaveCodeAgentSettingsInput {
    pub selected_provider: String,
    #[serde(default)]
    pub model_by_provider: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimeCodeAgentSettings {
    pub provider_id: String,
    pub task_provider: String,
    pub model: String,
}

fn definition(id: &str) -> Option<ProviderDefinition> {
    PROVIDERS.iter().copied().find(|item| item.id == id)
}

fn valid_model(model: &str) -> bool {
    !model.is_empty()
        && model.chars().count() <= 256
        && !model.chars().any(char::is_control)
        && !model.starts_with('-')
}

fn settings_path(data_dir: &Path) -> PathBuf {
    data_dir.join("settings/codeagent.json")
}

fn migrate_retired_model(settings: &mut StoredSettings) {
    if settings.model_by_provider.get("codex").map(String::as_str) == Some(RETIRED_CODEX_MODEL) {
        settings
            .model_by_provider
            .insert("codex".to_string(), DEFAULT_CODEX_MODEL.to_string());
    }
}

fn read(data_dir: &Path) -> Result<StoredSettings, String> {
    let path = settings_path(data_dir);
    let Ok(metadata) = path.symlink_metadata() else {
        return Ok(StoredSettings::default());
    };
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > MAX_SETTINGS_BYTES
    {
        return Err("CodeAgent 设置文件不是安全的有界普通文件。".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("无法读取 CodeAgent 设置：{error}"))?;
    let mut value: StoredSettings = serde_json::from_slice(&bytes)
        .map_err(|error| format!("CodeAgent 设置不是有效 JSON：{error}"))?;
    migrate_retired_model(&mut value);
    validate(&value)?;
    Ok(value)
}

fn validate(value: &StoredSettings) -> Result<(), String> {
    if value.schema_version != SCHEMA_VERSION {
        return Err("CodeAgent 设置版本不受支持。".to_string());
    }
    if definition(&value.selected_provider).is_none() {
        return Err("选择了不受支持的 CodeAgent。".to_string());
    }
    for (provider, model) in &value.model_by_provider {
        if definition(provider).is_none() || !valid_model(model) || model.trim() != model {
            return Err("CodeAgent 模型配置无效。".to_string());
        }
    }
    if !value
        .model_by_provider
        .get(&value.selected_provider)
        .is_some_and(|model| valid_model(model) && model.trim() == model)
    {
        return Err("所选 CodeAgent 必须配置明确的模型名称。".to_string());
    }
    Ok(())
}

fn executable(definition: ProviderDefinition) -> Option<PathBuf> {
    native_platform::trusted_helper_path(if matches!(definition.id, "codex" | "opencode") { "docker" } else { definition.executable })
}

fn public_view(settings: &StoredSettings) -> Value {
    json!({
        "schemaVersion": SCHEMA_VERSION,
        "selectedProvider": settings.selected_provider,
        "modelByProvider": settings.model_by_provider,
        "providers": PROVIDERS.iter().map(|item| {
            let installed = executable(*item);
            let installed_flag = installed.is_some();
            let executable_path = installed.as_ref().map(|path| path.display().to_string());
            json!({
                "id": item.id,
                "taskProvider": item.task_provider,
                "displayName": item.display_name,
                "installed": installed_flag,
                "executablePath": executable_path,
                "executionSupported": item.execution_supported,
                "identityObservable": installed_flag,
                "preferenceConfigurable": true,
                "status": if !item.execution_supported {
                    if installed_flag {
                        "local-live-containment-unavailable"
                    } else {
                        "executable-not-found"
                    }
                } else if installed_flag {
                    "ready-for-preflight"
                } else {
                    "executable-not-found"
                },
                "sourceAuthoringOnly": true,
                "compileAuthority": false,
                "verifyAuthority": false,
                "installAuthority": false,
                "publishAuthority": false
            })
        }).collect::<Vec<_>>()
    })
}

pub fn get(data_dir: &Path) -> Result<Value, String> {
    Ok(public_view(&read(data_dir)?))
}

pub fn save(data_dir: &Path, input: SaveCodeAgentSettingsInput) -> Result<Value, String> {
    let settings = StoredSettings {
        schema_version: SCHEMA_VERSION.to_string(),
        selected_provider: input.selected_provider.trim().to_string(),
        model_by_provider: input
            .model_by_provider
            .into_iter()
            .map(|(key, value)| {
                let mut value = value.trim().to_string();
                if key == "codex" && value == RETIRED_CODEX_MODEL {
                    value = DEFAULT_CODEX_MODEL.to_string();
                }
                (key, value)
            })
            .filter(|(_, value)| !value.is_empty())
            .collect(),
    };
    validate(&settings)?;
    let directory = settings_path(data_dir)
        .parent()
        .ok_or_else(|| "无法确定 CodeAgent 设置目录。".to_string())?
        .to_path_buf();
    fs::create_dir_all(&directory)
        .map_err(|error| format!("无法创建 CodeAgent 设置目录：{error}"))?;
    let encoded = serde_json::to_vec(&settings)
        .map_err(|error| format!("无法序列化 CodeAgent 设置：{error}"))?;
    if encoded.is_empty() || encoded.len() as u64 > MAX_SETTINGS_BYTES {
        return Err("CodeAgent 设置超过 64 KiB 上限。".to_string());
    }
    let path = settings_path(data_dir);
    let temporary = directory.join(format!(".codeagent.{}.tmp", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|error| format!("无法创建 CodeAgent 临时设置：{error}"))?;
    let result = file
        .write_all(&encoded)
        .and_then(|_| file.sync_all())
        .and_then(|_| fs::rename(&temporary, &path));
    if let Err(error) = result {
        let _ = fs::remove_file(&temporary);
        return Err(format!("无法持久化 CodeAgent 设置：{error}"));
    }
    Ok(public_view(&settings))
}

pub fn runtime(data_dir: &Path) -> Result<RuntimeCodeAgentSettings, String> {
    let settings = read(data_dir)?;
    let selected = definition(&settings.selected_provider)
        .ok_or_else(|| "选择了不受支持的 CodeAgent。".to_string())?;
    Ok(RuntimeCodeAgentSettings {
        provider_id: selected.id.to_string(),
        task_provider: selected.task_provider.to_string(),
        model: settings
            .model_by_provider
            .get(selected.id)
            .cloned()
            .ok_or_else(|| "所选 CodeAgent 缺少明确的模型名称。".to_string())?,
    })
}

fn bound_runtime_from_task(task: &Value) -> Result<RuntimeCodeAgentSettings, String> {
    let task_provider = task["provider"]
        .as_str()
        .ok_or_else(|| "旧任务未绑定 CodeAgent provider；请重新确认需求和授权。".to_string())?;
    let selected = PROVIDERS
        .iter()
        .copied()
        .find(|item| item.task_provider == task_provider)
        .ok_or_else(|| "任务绑定了不受支持的 CodeAgent provider。".to_string())?;
    let model = match task.get("model") {
        Some(Value::String(model)) if valid_model(model) && model.trim() == model => model.clone(),
        None => {
            return Err(
                "旧任务未绑定 immutable CodeAgent model；请重新确认需求和授权。".to_string(),
            );
        }
        _ => return Err("任务绑定的 CodeAgent model 无效。".to_string()),
    };
    let consent = task["consent"]
        .as_object()
        .ok_or_else(|| "任务缺少 CodeAgent 授权绑定。".to_string())?;
    if consent.get("provider") != Some(&Value::String(task_provider.to_string()))
        || consent.get("model") != task.get("model")
    {
        return Err("任务与授权的 CodeAgent provider/model 绑定不一致。".to_string());
    }
    Ok(RuntimeCodeAgentSettings {
        provider_id: selected.id.to_string(),
        task_provider: task_provider.to_string(),
        model,
    })
}

pub fn runtime_from_task(task: &Value) -> Result<RuntimeCodeAgentSettings, String> {
    let runtime = bound_runtime_from_task(task)?;
    let selected = definition(&runtime.provider_id)
        .ok_or_else(|| "任务绑定了不受支持的 CodeAgent provider。".to_string())?;
    if !selected.execution_supported {
        return Err(format!(
            "local-live-containment-unavailable: {} 的本机真实执行已暂停；需要可证明清理全部子进程的隔离后端。",
            selected.display_name
        ));
    }
    Ok(runtime)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn root(label: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../target/test-artifacts")
            .join(format!("codeagent-settings-{label}-{suffix:x}"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn codex_and_opencode_docker_are_supported_and_other_providers_remain_configurable() {
        let value = get(&root("default")).unwrap();
        assert_eq!(value["selectedProvider"], "codex");
        assert_eq!(value["modelByProvider"]["codex"], DEFAULT_CODEX_MODEL);
        assert_eq!(value["providers"].as_array().unwrap().len(), 4);
        let providers = value["providers"].as_array().unwrap();
        assert_eq!(providers[0]["executionSupported"], true);
        assert_eq!(providers[1]["executionSupported"], false);
        assert_eq!(providers[2]["executionSupported"], true);
        assert_eq!(providers[3]["executionSupported"], false);
        assert!(providers.iter().all(|provider| provider["preferenceConfigurable"] == true));
        assert_eq!(providers[0]["taskProvider"], "openai-codex");
        assert_eq!(providers[1]["taskProvider"], "anthropic-claude-code");
        assert_eq!(providers[2]["taskProvider"], "opencode");
        assert_eq!(providers[3]["taskProvider"], "google-gemini-cli");
    }

    #[test]
    fn settings_round_trip_and_reject_unknown_provider() {
        let data = root("round-trip");
        let mut models = BTreeMap::new();
        models.insert("codex".to_string(), "gpt-5.6-sol".to_string());
        let saved = save(
            &data,
            SaveCodeAgentSettingsInput {
                selected_provider: "codex".to_string(),
                model_by_provider: models,
            },
        )
        .unwrap();
        assert_eq!(saved["modelByProvider"]["codex"], "gpt-5.6-sol");
        assert_eq!(runtime(&data).unwrap().task_provider, "openai-codex");
        assert!(
            save(
                &data,
                SaveCodeAgentSettingsInput {
                    selected_provider: "shell".to_string(),
                    model_by_provider: BTreeMap::new(),
                }
            )
            .is_err()
        );
    }

    #[test]
    fn retired_default_model_is_migrated_without_changing_task_bindings() {
        let data = root("retired-default");
        let mut models = BTreeMap::new();
        models.insert("codex".to_string(), RETIRED_CODEX_MODEL.to_string());
        let saved = save(
            &data,
            SaveCodeAgentSettingsInput {
                selected_provider: "codex".to_string(),
                model_by_provider: models,
            },
        )
        .unwrap();
        assert_eq!(saved["modelByProvider"]["codex"], DEFAULT_CODEX_MODEL);
        assert_eq!(runtime(&data).unwrap().model, DEFAULT_CODEX_MODEL);
    }

    #[test]
    fn every_provider_round_trips_its_own_model_and_task_binding() {
        for provider in PROVIDERS {
            let data = root(provider.id);
            let model = format!("{}/synthetic-model", provider.id);
            let mut models = BTreeMap::new();
            models.insert(provider.id.to_string(), model.clone());
            save(
                &data,
                SaveCodeAgentSettingsInput {
                    selected_provider: provider.id.to_string(),
                    model_by_provider: models,
                },
            )
            .unwrap();
            let selected = runtime(&data).unwrap();
            assert_eq!(selected.provider_id, provider.id);
            assert_eq!(selected.task_provider, provider.task_provider);
            assert_eq!(selected.model, model);
        }
    }

    #[test]
    fn authorized_task_provider_and_model_are_independent_of_current_settings() {
        let data = root("immutable-task");
        let mut models = BTreeMap::new();
        models.insert("codex".to_string(), "gpt-current-setting".to_string());
        save(
            &data,
            SaveCodeAgentSettingsInput {
                selected_provider: "codex".to_string(),
                model_by_provider: models,
            },
        )
        .unwrap();

        let task = json!({
            "provider": "anthropic-claude-code",
            "model": "claude-authorized",
            "consent": {
                "provider": "anthropic-claude-code",
                "model": "claude-authorized"
            }
        });
        let bound = bound_runtime_from_task(&task).unwrap();
        assert_eq!(bound.provider_id, "claude-code");
        assert_eq!(bound.model, "claude-authorized");
        assert_ne!(bound, runtime(&data).unwrap());
        assert!(
            runtime_from_task(&task)
                .unwrap_err()
                .contains("local-live-containment-unavailable")
        );
    }

    #[test]
    fn legacy_or_mismatched_task_model_fails_closed() {
        assert!(
            runtime_from_task(&json!({
                "provider": "openai-codex",
                "consent": {"provider": "openai-codex"}
            }))
            .unwrap_err()
            .contains("immutable")
        );
        assert!(
            runtime_from_task(&json!({
                "provider": "openai-codex",
                "model": "gpt-authorized",
                "consent": {"provider": "openai-codex", "model": "gpt-other"}
            }))
            .is_err()
        );
        for model in [Value::Null, json!(""), json!("   ")] {
            let mut task = json!({
                "provider": "openai-codex",
                "model": "gpt-authorized",
                "consent": {"provider": "openai-codex", "model": "gpt-authorized"}
            });
            task["model"] = model.clone();
            task["consent"]["model"] = model;
            assert!(runtime_from_task(&task).is_err());
        }
    }

    #[test]
    fn selected_provider_requires_an_explicit_model_setting() {
        let data = root("missing-selected-model");
        assert!(
            save(
                &data,
                SaveCodeAgentSettingsInput {
                    selected_provider: "claude-code".to_string(),
                    model_by_provider: BTreeMap::new(),
                }
            )
            .unwrap_err()
            .contains("明确的模型")
        );
    }
}
