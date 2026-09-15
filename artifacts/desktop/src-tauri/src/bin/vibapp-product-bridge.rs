//! Headless product bridge shared by the local Website and Desktop product modules.
//!
//! One bounded JSON request is read from stdin and one JSON response is written to
//! stdout.  The Website never receives filesystem paths or provider credentials.

use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[path = "../codeagent_settings.rs"]
mod codeagent_settings;
#[path = "../delivery_integration.rs"]
mod delivery_integration;
#[path = "../local_orchestrator.rs"]
mod local_orchestrator;
#[path = "../local_product.rs"]
mod local_product;
#[path = "../model_settings.rs"]
mod model_settings;
#[path = "../native_platform.rs"]
mod native_platform;
#[path = "../network_settings.rs"]
mod network_settings;
#[path = "../public_registry_sync.rs"]
mod public_registry_sync;
#[path = "../registry_integration.rs"]
mod registry_integration;
#[path = "../roomhash_host.rs"]
mod roomhash_host;

const MAX_REQUEST_BYTES: usize = 512 * 1024;
const MAX_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
// The inner pipeline is 2700s. The bridge owns a separate 60s wait/cleanup
// margin and must cancel+join rather than returning when this limit expires.
const BRIDGE_LIFETIME_MARGIN_SECONDS: u64 = 60;
const MAX_JOB_LIFETIME: Duration = Duration::from_secs(
    delivery_integration::PIPELINE_TIMEOUT.as_secs() + BRIDGE_LIFETIME_MARGIN_SECONDS,
);
const BUILD_INPUTS_SHA256: &str = env!("VIBAPP_DESKTOP_BUILD_INPUTS_SHA256");
const CLOUD_CODEAGENT_TASK_SCHEMA: &str = "vibapp.cloud-codeagent-task.experimental-v3";
const CLOUD_CODEAGENT_TASK_SCHEMA_SHA256: &str =
    "fb2a49186349aa4542ab6bab9440fe2e5c48a9d7cc86085449a71e20ecdb73ab";
const CODEAGENT_ADAPTER_SCHEMA: &str = "vibapp.codeagent-adapter.experimental-v1";
const CODEAGENT_STATUS_SCHEMA: &str = "vibapp.codeagent-adapter-status.experimental-v2";
const CODEX_COMPATIBILITY_POLICY_SCHEMA: &str = "vibapp.codex-cli-compatibility.experimental-v1";
const PROVIDER_EXECUTION_IDENTITY_SCHEMA: &str =
    "vibapp.provider-execution-identity.experimental-v1";
const SOURCE_HANDOFF_SCHEMA: &str = "vibapp.codeagent-source-handoff.experimental-v2";
const DELIVERY_WORKER_SCHEMA: &str = "vibapp.desktop-delivery-worker.experimental-v2";
static SHUTDOWN_SIGNAL: AtomicI32 = AtomicI32::new(0);

#[derive(Clone, Debug)]
struct DeliveryWorkerBinding {
    task_id: String,
    attempt_id: String,
}

extern "C" fn record_shutdown_signal(signal: libc::c_int) {
    SHUTDOWN_SIGNAL.store(signal, Ordering::Release);
    delivery_integration::request_process_cancellation();
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(
            libc::SIGINT,
            record_shutdown_signal as *const () as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGTERM,
            record_shutdown_signal as *const () as libc::sighandler_t,
        );
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BridgeRequest {
    command: String,
    #[serde(default)]
    args: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SubmitNeedInput {
    title: String,
    description: String,
    embedding_consent: bool,
    #[serde(default, alias = "retryTaskId")]
    retry_task_id: Option<String>,
    #[serde(default, alias = "needId")]
    need_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DevelopmentSubmitInput {
    task: Value,
    registry: Value,
    explicit_user_submit: bool,
    acknowledge_external_cost: bool,
    #[serde(default, alias = "retryTaskId")]
    retry_task_id: Option<String>,
    #[serde(default, alias = "editedFromTaskId")]
    edited_from_task_id: Option<String>,
}

fn unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn parse<T: for<'de> Deserialize<'de>>(value: Value, label: &str) -> Result<T, String> {
    serde_json::from_value(value).map_err(|error| format!("{label} 参数无效：{error}"))
}

fn submit_need(data_dir: &Path, args: Value) -> Result<Value, String> {
    let input: SubmitNeedInput = parse(args, "submit_need")?;
    let title = input.title.trim();
    let description = input.description.trim();
    if title.is_empty() || title.chars().count() > 80 {
        return Err("需求名称需为 1–80 个字符。".to_string());
    }
    if !(10..=2000).contains(&description.chars().count()) {
        return Err("需求描述需为 10–2000 个字符。".to_string());
    }
    let settings = model_settings::runtime(data_dir)?;
    match (input.retry_task_id.as_deref(), input.need_id.as_deref()) {
        (None, None) => registry_integration::submit_need(
            data_dir,
            title,
            description,
            input.embedding_consent,
            unix_ms(),
            &settings,
        ),
        (Some(task_id), Some(need_id)) => {
            let status = delivery_integration::status(data_dir, task_id)?;
            registry_integration::submit_need_retry(
                data_dir,
                title,
                description,
                input.embedding_consent,
                unix_ms(),
                &settings,
                task_id,
                need_id,
                &status,
            )
        }
        _ => Err("修改失败任务时必须同时提供 retryTaskId 与 needId。".to_string()),
    }
}

fn complete_need(data_dir: &Path, args: Value) -> Result<Value, String> {
    let input: registry_integration::CompleteNeedInput = parse(args, "complete_need")?;
    let model = model_settings::runtime(data_dir)?;
    let codeagent = codeagent_settings::runtime(data_dir)?;
    let provider_observation = local_orchestrator::codeagent_identity_observation(
        &codeagent.provider_id,
        &codeagent.model,
    )?;
    registry_integration::complete_need_with_provider(
        data_dir,
        input,
        unix_ms(),
        &model.embedding,
        &codeagent.task_provider,
        &codeagent.model,
        &provider_observation,
    )
}

fn merge_job_feeds(mut primary: Vec<Value>, additional: Vec<Value>) -> Vec<Value> {
    primary.extend(additional);
    let mut task_ids = BTreeSet::new();
    primary.retain(|job| {
        job["task_id"]
            .as_str()
            .is_none_or(|task_id| task_ids.insert(task_id.to_string()))
    });
    primary.sort_by(|left, right| {
        right["updated_at_utc"]
            .as_str()
            .cmp(&left["updated_at_utc"].as_str())
            .then_with(|| right["task_id"].as_str().cmp(&left["task_id"].as_str()))
    });
    primary
}

fn bridge_job_feed(data_dir: &Path, feed_errors: &mut Vec<Value>) -> Vec<Value> {
    let delivery = match delivery_integration::jobs(data_dir) {
        Ok(jobs) => jobs,
        Err(error) => {
            feed_errors.push(json!({
                "source": "automatic-delivery-controller",
                "message": error
            }));
            Vec::new()
        }
    };
    let assessments = match registry_integration::product_assessment_jobs(data_dir) {
        Ok(jobs) => jobs,
        Err(error) => {
            feed_errors.push(json!({
                "source": "product-capability-assessment-history",
                "message": error
            }));
            Vec::new()
        }
    };
    merge_job_feeds(delivery, assessments)
}

fn get_state(data_dir: &Path) -> Result<Value, String> {
    let mut feed_errors = Vec::new();
    let jobs = bridge_job_feed(data_dir, &mut feed_errors);
    let mut apps = match public_registry_sync::sync(data_dir) {
        Ok(public_apps) => public_apps,
        Err(error) => {
            feed_errors.push(json!({
                "source": "public-registry-locator-sync",
                "message": error
            }));
            Vec::new()
        }
    };
    for local_app in local_product::catalog(data_dir)? {
        let app_id = local_app["app_id"].as_str();
        apps.retain(|existing| existing["app_id"].as_str() != app_id);
        apps.push(local_app);
    }
    let installation_performed = apps.iter().any(|app| {
        matches!(
            app["installation_state"].as_str(),
            Some("installed") | Some("installed-disabled")
        )
    });
    // Never advertise a queue that this process cannot actually drive.  The
    // adapter owns the platform live-execution gate and currently fails closed
    // on hosts where process containment cannot be guaranteed.
    let local_codeagent_connected = local_orchestrator::codeagent_adapter_available();
    Ok(json!({
        "meta": {
            "schema_version": "vibapp.product-bridge-state.experimental-v1",
            "runtime_mode": "trusted-headless-product-bridge",
            "ecosystem_role": "launcher-appstore-runtime",
            "cloud_agent_connected": false,
            "local_codeagent_connected": local_codeagent_connected,
            "codeagent_queue_enabled": local_codeagent_connected,
            "codeagent_external_runner_configured": false,
            "delivery_controller": "durable-automatic-v1",
            "builder_configuration": delivery_integration::builder_configuration(),
            "installation_performed": installation_performed
        },
        "jobs": jobs,
        "apps": apps,
        "feed_errors": feed_errors
    }))
}

fn submit_development_task(
    data_dir: &Path,
    args: Value,
) -> Result<(Value, Option<DeliveryWorkerBinding>), String> {
    let input: DevelopmentSubmitInput = parse(args, "submit_development_task")?;
    // The browser/GUI projection is untrusted. It may echo the evidence shown by
    // complete_need, but only the exact host-persisted Registry result is passed to
    // the controller. A forged or merely well-shaped no-match has no authority.
    let trusted_registry = registry_integration::load_trusted_registry_development_evidence(
        data_dir,
        &input.task,
        &input.registry,
    )?;
    let codeagent = codeagent_settings::runtime_from_task(&input.task)?;
    let result = delivery_integration::submit_and_start_with_lineage(
        data_dir,
        &input.task,
        &trusted_registry,
        input.explicit_user_submit,
        input.acknowledge_external_cost,
        input.retry_task_id.as_deref(),
        input.edited_from_task_id.as_deref(),
        &codeagent,
    )?;
    let worker = if result["codeagent_adapter"]["started"] == true {
        Some(DeliveryWorkerBinding {
            task_id: result["receipt"]["task_id"]
                .as_str()
                .ok_or_else(|| "交付回执缺少 task_id。".to_string())?
                .to_string(),
            attempt_id: result["receipt"]["attempt_id"]
                .as_str()
                .ok_or_else(|| "交付回执缺少 attempt_id。".to_string())?
                .to_string(),
        })
    } else {
        None
    };
    Ok((result, worker))
}

fn dispatch(
    data_dir: &Path,
    request: BridgeRequest,
) -> Result<(Value, Option<DeliveryWorkerBinding>), String> {
    match request.command.as_str() {
        "health" => Ok((
            json!({
                "status": "ok",
                "service": "vibapp-product-bridge",
                "schema_version": "vibapp.product-bridge.experimental-v1",
                "build_input_receipt": {
                    "schema_version": "vibapp.desktop-build-input-receipt.experimental-v1",
                    "sha256": BUILD_INPUTS_SHA256
                },
                "contracts": {
                    "cloud_codeagent_task": CLOUD_CODEAGENT_TASK_SCHEMA,
                    "cloud_codeagent_task_schema_sha256": CLOUD_CODEAGENT_TASK_SCHEMA_SHA256,
                    "codeagent_adapter": CODEAGENT_ADAPTER_SCHEMA,
                    "codeagent_status": CODEAGENT_STATUS_SCHEMA,
                    "codex_compatibility_policy": CODEX_COMPATIBILITY_POLICY_SCHEMA,
                    "provider_execution_identity": PROVIDER_EXECUTION_IDENTITY_SCHEMA,
                    "source_handoff": SOURCE_HANDOFF_SCHEMA,
                    "delivery_worker": DELIVERY_WORKER_SCHEMA
                },
                "pipeline_budget_seconds": delivery_integration::PIPELINE_TIMEOUT.as_secs(),
                "bridge_lifetime_seconds": MAX_JOB_LIFETIME.as_secs(),
                "bridge_lifetime_margin_seconds": BRIDGE_LIFETIME_MARGIN_SECONDS,
                "worker_cleanup_budget_seconds": delivery_integration::PIPELINE_CLEANUP_TIMEOUT.as_secs(),
                "lifecycle": "cancel-join-confirm"
            }),
            None,
        )),
        "get_state" => Ok((get_state(data_dir)?, None)),
        "submit_need" => Ok((submit_need(data_dir, request.args)?, None)),
        "complete_need" => Ok((complete_need(data_dir, request.args)?, None)),
        "get_model_settings" => Ok((model_settings::get(data_dir)?, None)),
        "save_model_settings" => Ok((
            model_settings::save(data_dir, parse(request.args, "save_model_settings")?)?,
            None,
        )),
        "get_codeagent_settings" => Ok((codeagent_settings::get(data_dir)?, None)),
        "save_codeagent_settings" => Ok((
            codeagent_settings::save(data_dir, parse(request.args, "save_codeagent_settings")?)?,
            None,
        )),
        "get_network_settings" => Ok((network_settings::get(data_dir)?, None)),
        "save_network_settings" => Ok((
            network_settings::save(data_dir, parse(request.args, "save_network_settings")?)?,
            None,
        )),
        "submit_development_task" => submit_development_task(data_dir, request.args),
        _ => Err("Web 产品桥接器不支持这个命令。".to_string()),
    }
}

fn response(ok: bool, result: Value, error: Option<String>) -> Vec<u8> {
    let value = json!({
        "schema_version": "vibapp.product-bridge-response.experimental-v1",
        "ok": ok,
        "result": result,
        "error": error
    });
    let mut encoded = serde_json::to_vec(&value).unwrap_or_else(|_| {
        br#"{"schema_version":"vibapp.product-bridge-response.experimental-v1","ok":false,"result":null,"error":"response-serialization-failed"}"#.to_vec()
    });
    if encoded.len() > MAX_RESPONSE_BYTES {
        encoded = br#"{"schema_version":"vibapp.product-bridge-response.experimental-v1","ok":false,"result":null,"error":"response-limit-exceeded"}"#.to_vec();
    }
    encoded.push(b'\n');
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_binds_the_current_build_and_delivery_contracts() {
        let (health, worker) = dispatch(
            Path::new("unused-for-health"),
            BridgeRequest {
                command: "health".to_string(),
                args: json!({}),
            },
        )
        .expect("health response");
        assert!(worker.is_none());
        assert_eq!(health["build_input_receipt"]["sha256"], BUILD_INPUTS_SHA256);
        assert_eq!(
            health["contracts"]["cloud_codeagent_task"],
            CLOUD_CODEAGENT_TASK_SCHEMA
        );
        assert_eq!(
            health["contracts"]["cloud_codeagent_task_schema_sha256"],
            CLOUD_CODEAGENT_TASK_SCHEMA_SHA256
        );
        assert_eq!(health["contracts"]["source_handoff"], SOURCE_HANDOFF_SCHEMA);
        assert_eq!(health["lifecycle"], "cancel-join-confirm");
    }

    #[test]
    fn bridge_lifetime_includes_pipeline_cleanup_margin() {
        assert_eq!(
            MAX_JOB_LIFETIME,
            delivery_integration::PIPELINE_TIMEOUT
                + Duration::from_secs(BRIDGE_LIFETIME_MARGIN_SECONDS)
        );
        assert!(MAX_JOB_LIFETIME > delivery_integration::PIPELINE_TIMEOUT);
        assert!(
            Duration::from_secs(BRIDGE_LIFETIME_MARGIN_SECONDS)
                > delivery_integration::PIPELINE_CLEANUP_TIMEOUT
        );
    }

    #[test]
    fn development_dto_distinguishes_edited_replacement_lineage() {
        let input: DevelopmentSubmitInput = serde_json::from_value(json!({
            "task": {},
            "registry": {},
            "explicit_user_submit": true,
            "acknowledge_external_cost": true,
            "edited_from_task_id": "development-0123456789abcdef0123456789abcdef"
        }))
        .unwrap();
        assert!(input.retry_task_id.is_none());
        assert_eq!(
            input.edited_from_task_id.as_deref(),
            Some("development-0123456789abcdef0123456789abcdef")
        );
    }

    #[test]
    fn development_submit_rejects_unpersisted_client_registry_claim_before_queueing() {
        use sha2::{Digest, Sha256};

        let root = test_root("unpersisted-registry");
        let need_spec = json!({
            "schema_version": "vibapp.need-spec.product-v0.0.1",
            "document_type": "need-spec",
            "need_id": "need-deadbeef",
            "revision": 2
        });
        let canonical_need = br#"{"document_type":"need-spec","need_id":"need-deadbeef","revision":2,"schema_version":"vibapp.need-spec.product-v0.0.1"}"#;
        let need_digest = format!("{:x}", Sha256::digest(canonical_need));
        let registry = json!({
            "schema_version": "vibapp.registry-route.experimental.2026-08-24.1",
            "status": "experimental-product-hold",
            "route": "refinement",
            "request_id": format!("registry.{}", &need_digest[..24]),
            "need_id": "need-deadbeef",
            "recommendations": [],
            "refinement": {
                "reason_code": "no-hard-filter-match",
                "message": "Client supplied a plausible but unpersisted result."
            },
            "retrieval": {"mode": "hard-filter-then-keyword-plus-embedding"},
            "rejected": [],
            "codeagent_handoff": {
                "created": false,
                "permitted": false,
                "reason": "Registry only recommends or requests refinement."
            }
        });
        let task = json!({
            "schema_version": "vibapp.cloud-codeagent-task.experimental-v3",
            "need_spec_complete": true,
            "need_spec_digest_sha256": need_digest,
            "immutable_task_digest_sha256": "a".repeat(64),
            "need_spec": need_spec
        });
        let error = submit_development_task(
            &root,
            json!({
                "task": task,
                "registry": registry,
                "explicit_user_submit": true,
                "acknowledge_external_cost": true
            }),
        )
        .unwrap_err();
        assert!(error.contains("找不到与开发任务绑定的 Registry 可信证据"));
        assert!(!root.join("delivery-controller/tasks").exists());
    }

    fn test_root(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "vibapp-product-bridge-{label}-{}-{nonce:x}",
            std::process::id()
        ));
        std::fs::create_dir_all(&root).expect("create isolated bridge test root");
        root
    }

    fn write_assessment(root: &Path, provider_process_started: bool) {
        let digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let task_id = "assessment-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let attempt_id = "assessment-attempt-0001-aaaaaaaaaaaaaaaa";
        let need_id = "need-deadbeef";
        let binding = json!({
            "schema_version": "vibapp.need-spec.product-v0.0.1",
            "need_id": need_id,
            "revision": 1,
            "state": "draft",
            "canonical_digest_sha256": digest,
            "source_relative_path": "registry-requests/need-deadbeef.json"
        });
        let summary = json!({
            "title": "Unsupported UI + HTTP",
            "goal": "Show live weather in a visible application.",
            "package_id": "ai.vibapp.audit.weather",
            "requested_app_kind": "ui",
            "requested_capabilities": ["http"],
            "requested_allowed_permissions": ["http"],
            "requested_forbidden_permissions": [],
            "request_values_truncated": false,
            "network_mode": "scoped-network",
            "completion_request_digest_sha256": digest
        });
        let created_at = "2026-08-28T09:00:00Z";
        let attempt = json!({
            "schema_version": "vibapp.product-assessment-attempt.experimental-v1",
            "document_type": "product-assessment-attempt",
            "task_kind": "product-assessment",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "attempt_number": 1,
            "need_id": need_id,
            "reason_code": "unsupported-capability-combination",
            "status": "unsupported",
            "stage": "product-capability-assessment",
            "progress_percent": 100,
            "terminal": true,
            "assessment_digest_sha256": digest,
            "need_spec_binding": binding,
            "requirement_summary": summary,
            "reasons": ["ui-only-reference does not provide HTTP."],
            "codeagent_task_created": false,
            "external_request_made": false,
            "provider_process_started": provider_process_started,
            "authorities": {
                "codeagent": "not-invoked",
                "provider": "not-invoked",
                "builder": "not-invoked",
                "verifier": "not-invoked",
                "publication": "not-performed"
            },
            "created_at_utc": created_at,
            "updated_at_utc": created_at
        });
        let task = json!({
            "schema_version": "vibapp.product-assessment-task.experimental-v1",
            "document_type": "product-assessment-task",
            "task_kind": "product-assessment",
            "task_id": task_id,
            "need_id": need_id,
            "reason_code": "unsupported-capability-combination",
            "status": "unsupported",
            "assessment_digest_sha256": digest,
            "need_spec_binding": binding,
            "requirement_summary": summary,
            "attempt_count": 1,
            "current_attempt_id": attempt_id,
            "attempt_ids": [attempt_id],
            "codeagent_task_created": false,
            "external_request_made": false,
            "provider_process_started": false,
            "created_at_utc": created_at,
            "updated_at_utc": created_at
        });
        let task_root = root.join("product-assessments/tasks").join(task_id);
        let attempt_root = task_root.join("attempts").join(attempt_id);
        std::fs::create_dir_all(&attempt_root).expect("create assessment fixture");
        std::fs::write(
            task_root.join("task.json"),
            serde_json::to_vec(&task).expect("encode task"),
        )
        .expect("write task");
        std::fs::write(
            attempt_root.join("attempt.json"),
            serde_json::to_vec(&attempt).expect("encode attempt"),
        )
        .expect("write attempt");
    }

    #[test]
    fn unsupported_assessment_is_visible_in_headless_bridge_feed() {
        let root = test_root("assessment-feed");
        write_assessment(&root, false);
        let mut errors = Vec::new();
        let jobs = bridge_job_feed(&root, &mut errors);
        assert!(errors.is_empty());
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0]["task_kind"], "product-assessment");
        assert_eq!(jobs[0]["status"], "unsupported");
        assert_eq!(jobs[0]["codeagent_task_created"], false);
        assert_eq!(jobs[0]["external_request_made"], false);
        assert_eq!(jobs[0]["assessment"]["provider_process_started"], false);
        assert_eq!(jobs[0]["assessment"]["terminal"], true);
        std::fs::remove_dir_all(root).expect("remove isolated bridge test root");
    }

    #[test]
    fn malformed_assessment_is_isolated_as_feed_error() {
        let root = test_root("assessment-error");
        write_assessment(&root, true);
        let mut errors = Vec::new();
        let jobs = bridge_job_feed(&root, &mut errors);
        assert!(jobs.is_empty());
        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0]["source"], "product-capability-assessment-history");
        std::fs::remove_dir_all(root).expect("remove isolated bridge test root");
    }

    #[test]
    fn bridge_feed_deduplicates_and_sorts_stably() {
        let primary = vec![
            json!({"task_id": "task-b", "updated_at_utc": "2026-08-28T09:00:00Z", "source": "primary"}),
            json!({"task_id": "task-a", "updated_at_utc": "2026-08-28T10:00:00Z", "source": "primary"}),
        ];
        let additional = vec![
            json!({"task_id": "task-b", "updated_at_utc": "2026-08-28T11:00:00Z", "source": "duplicate"}),
            json!({"task_id": "task-c", "updated_at_utc": "2026-08-28T09:00:00Z", "source": "additional"}),
        ];
        let jobs = merge_job_feeds(primary, additional);
        assert_eq!(jobs.len(), 3);
        assert_eq!(jobs[0]["task_id"], "task-a");
        assert_eq!(jobs[1]["task_id"], "task-c");
        assert_eq!(jobs[2]["task_id"], "task-b");
        assert_eq!(jobs[2]["source"], "primary");
    }
}

fn main() {
    install_signal_handlers();
    let mut arguments = std::env::args_os().skip(1);
    let Some(data_dir) = arguments.next().map(PathBuf::from) else {
        eprintln!("usage: vibapp-product-bridge DATA_DIR");
        std::process::exit(64);
    };
    if arguments.next().is_some() || data_dir.as_os_str().is_empty() {
        eprintln!("usage: vibapp-product-bridge DATA_DIR");
        std::process::exit(64);
    }
    let mut input = Vec::new();
    if std::io::stdin()
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut input)
        .is_err()
        || input.is_empty()
        || input.len() > MAX_REQUEST_BYTES
    {
        let _ = std::io::stdout().write_all(&response(
            false,
            Value::Null,
            Some("request-limit-exceeded".to_string()),
        ));
        std::process::exit(2);
    }
    let parsed = serde_json::from_slice::<BridgeRequest>(&input)
        .map_err(|error| format!("桥接请求不是有效 JSON：{error}"))
        .and_then(|request| dispatch(&data_dir, request));
    let (encoded, worker) = match parsed {
        Ok((result, worker)) => (response(true, result, None), worker),
        Err(error) => (response(false, Value::Null, Some(error)), None),
    };
    let mut stdout = std::io::stdout().lock();
    let _ = stdout.write_all(&encoded);
    let _ = stdout.flush();
    drop(stdout);

    // Delivery runs in a bounded worker owned by this process. Wait for the
    // exact task/attempt thread, not a broad jobs() observation. Every terminal
    // path either joins it or records that cancellation could not be confirmed.
    let mut bridge_exit = 0;
    if let Some(worker) = worker {
        if let Err(error) = delivery_integration::wait_for_worker(
            &worker.task_id,
            &worker.attempt_id,
            MAX_JOB_LIFETIME,
        ) {
            if error.starts_with(delivery_integration::WORKER_JOINED_CLEANUP_UNCONFIRMED) {
                eprintln!("{error}");
                bridge_exit = 70;
            } else {
                let signal = SHUTDOWN_SIGNAL.load(Ordering::Acquire);
                let reason = if signal != 0 {
                    format!("bridge received signal {signal}")
                } else {
                    format!("bridge lifetime ended: {error}")
                };
                if let Err(cancel_error) = delivery_integration::cancel_and_join_worker(
                    &worker.task_id,
                    &worker.attempt_id,
                    &reason,
                    delivery_integration::PIPELINE_CLEANUP_TIMEOUT,
                ) {
                    eprintln!("{cancel_error}");
                    bridge_exit = 70;
                } else {
                    bridge_exit = if signal == libc::SIGINT {
                        130
                    } else if signal == libc::SIGTERM {
                        143
                    } else {
                        124
                    };
                }
            }
        }
    }
    let signal = SHUTDOWN_SIGNAL.load(Ordering::Acquire);
    if bridge_exit == 0 && signal != 0 {
        bridge_exit = if signal == libc::SIGINT { 130 } else { 143 };
    }
    if bridge_exit != 0 {
        std::process::exit(bridge_exit);
    }
}
