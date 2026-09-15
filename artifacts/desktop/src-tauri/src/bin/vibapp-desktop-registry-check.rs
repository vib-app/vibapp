use serde_json::Value;
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

#[path = "../local_orchestrator.rs"]
mod local_orchestrator;
#[path = "../model_settings.rs"]
mod model_settings;
#[path = "../native_platform.rs"]
mod native_platform;
#[path = "../registry_integration.rs"]
mod registry_integration;

fn usage() -> ! {
    eprintln!(
        "usage: vibapp-desktop-registry-check --data-dir PATH --description TEXT [--embedding-consent] [--complete] [--remote-processing-consent] [--public-publication-consent] [--proceed-after-recommendation] [--task-preview-output PATH] [--submit-local-queue]"
    );
    std::process::exit(64);
}

fn main() {
    let mut args = std::env::args().skip(1);
    let mut data_dir: Option<PathBuf> = None;
    let mut description: Option<String> = None;
    let mut embedding_consent = false;
    let mut complete = false;
    let mut remote_processing_consent = false;
    let mut public_publication_consent = false;
    let mut proceed_after_recommendation = false;
    let mut task_preview_output: Option<PathBuf> = None;
    let mut submit_local_queue = false;
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--data-dir" => data_dir = args.next().map(PathBuf::from),
            "--description" => description = args.next(),
            "--embedding-consent" => embedding_consent = true,
            "--complete" => complete = true,
            "--remote-processing-consent" => remote_processing_consent = true,
            "--public-publication-consent" => public_publication_consent = true,
            "--proceed-after-recommendation" => proceed_after_recommendation = true,
            "--task-preview-output" => task_preview_output = args.next().map(PathBuf::from),
            "--submit-local-queue" => submit_local_queue = true,
            _ => usage(),
        }
    }
    let data_dir = data_dir.unwrap_or_else(|| usage());
    let description = description.unwrap_or_else(|| usage());
    if !(10..=2000).contains(&description.chars().count()) {
        usage();
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let model_settings = model_settings::runtime(&data_dir).unwrap_or_else(|error| {
        eprintln!("{error}");
        std::process::exit(2);
    });
    let submitted: Value = registry_integration::submit_need(
        &data_dir,
        "Registry integration check",
        &description,
        embedding_consent,
        now,
        &model_settings,
    )
    .unwrap_or_else(|error| {
        eprintln!("{error}");
        std::process::exit(2);
    });
    let mut value = if complete {
        let need_id = submitted["need"]["need_id"]
            .as_str()
            .unwrap_or_else(|| usage())
            .to_string();
        registry_integration::complete_need(
            &data_dir,
            registry_integration::CompleteNeedInput {
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
                permission_ceiling_confirmed: true,
                negative_constraints: "不得读取用户未选择的文件。".to_string(),
                negative_constraints_confirmed: true,
                network_mode: "offline".to_string(),
                package_id: "ai.vibapp.custom.focus".to_string(),
                package_name: "专注工作台".to_string(),
                package_version: "0.1.0".to_string(),
                acceptance_example: "用户创建一个专注事项后，关闭并重新打开应用仍能看到该事项。"
                    .to_string(),
                proceed_after_recommendation,
                registry_embedding_consent: embedding_consent,
                remote_processing_consent,
                public_publication_consent,
            },
            now.saturating_add(1),
            &model_settings.embedding,
        )
        .unwrap_or_else(|error| {
            eprintln!("{error}");
            std::process::exit(2);
        })
    } else {
        submitted
    };
    if let Some(path) = task_preview_output {
        let preview = &value["cloud_development"]["task_preparation"]["schema_preview"];
        if preview.is_null() {
            eprintln!("schema preview is unavailable");
            std::process::exit(3);
        }
        let mut bytes = serde_json::to_vec_pretty(preview).expect("serialize task preview");
        bytes.push(b'\n');
        fs::write(path, bytes).expect("write task preview");
    }
    if submit_local_queue {
        let preview = value["cloud_development"]["task_preparation"]["schema_preview"].clone();
        if preview.is_null() {
            eprintln!("schema preview is unavailable");
            std::process::exit(3);
        }
        let registry = value["registry"].clone();
        let queue_root = data_dir.join("orchestrator");
        value["local_queue_submission"] =
            local_orchestrator::submit(&queue_root, &preview, &registry, true).unwrap_or_else(
                |error| {
                    eprintln!("{error}");
                    std::process::exit(4);
                },
            );
        value["local_queue_jobs"] = Value::Array(local_orchestrator::queued_jobs(&queue_root));
    }
    serde_json::to_writer_pretty(std::io::stdout(), &value).expect("serialize result");
    println!();
}
