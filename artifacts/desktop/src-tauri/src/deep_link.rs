//! Untrusted OS app links -> bounded public intake -> trusted Launcher consent.
use serde_json::{Value, json};
use std::sync::Mutex;
use tauri::{AppHandle, Manager, WebviewWindow, WebviewWindowBuilder, WebviewUrl, WindowEvent};
use crate::{app_data_dir, local_product, open_known_app_window, startup};

#[derive(Default)]
pub struct StoreOpenState(pub Mutex<Value>);
const INSTALL_WINDOW: &str = "store-install";

fn confirmation_window(window: &WebviewWindow) -> Result<(), String> {
    if window.label() != INSTALL_WINDOW { return Err("App-link confirmation belongs to the install window".into()); }
    Ok(())
}

fn show_confirmation(app: &AppHandle) -> Result<(), String> {
    let window = if let Some(window) = app.get_webview_window(INSTALL_WINDOW) { window } else {
        let window = WebviewWindowBuilder::new(app, INSTALL_WINDOW,
            WebviewUrl::App("index.html?storeInstall=1".into()))
            .title("VibApp · Install").inner_size(480.0, 480.0)
            .min_inner_size(360.0, 360.0).resizable(true).maximizable(false)
            .minimizable(false).center().prevent_overflow()
            .build().map_err(|e| format!("Cannot open install window: {e}"))?;
        let handle = app.clone();
        window.on_window_event(move |event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = handle.state::<StoreOpenState>();
                if let Ok(mut pending) = state.0.lock() {
                    match pending["status"].as_str() {
                        Some("installing") => api.prevent_close(),
                        Some("downloading") => pending["cancelled"] = json!(true),
                        _ => *pending = Value::Null,
                    }
                }
            }
        });
        window
    };
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())
}

pub fn receive(app: &AppHandle, url: &str) -> Result<(), String> {
    let id = startup::app_id_from_url(url)?;
    let token = uuid::Uuid::new_v4().to_string();
    {
        let state = app.state::<StoreOpenState>();
        let mut pending = state.0.lock().map_err(|_| "App link state unavailable")?;
        // One request at a time, including while downloading/confirming. A second
        // URL cannot replace the app/digest the user is currently reviewing.
        if !pending.is_null() {
            pending["cancelled"] = json!(false);
            drop(pending);
            return show_confirmation(app);
        }
        *pending = json!({"request_id":token, "app_id":id, "status":"downloading"});
    }
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let result = (|| -> Result<Value, String> {
            let data = app_data_dir(&app)?;
            if let Some(item) = local_product::catalog(&data)?.into_iter().find(|a| a["app_id"] == id)
                && startup::require_installed_application(&item).is_ok()
            {
                open_known_app_window(&app, &id, true)?;
                return Ok(Value::Null);
            }
            show_confirmation(&app)?;
            let downloaded = local_product::fetch_public_store_app(&data, &id)?;
            Ok(json!({"request_id":token, "app_id":id, "status":"ready",
                "record":downloaded["record"], "source_url":downloaded["public_source_url"]}))
        })();
        let state = app.state::<StoreOpenState>();
        if let Ok(mut pending) = state.0.lock() {
            if pending["cancelled"] == true {
                *pending = Value::Null;
                return;
            }
            *pending = result.unwrap_or_else(|error| json!({"request_id":token,
                "app_id":id, "status":"error", "message":error.chars().take(1000).collect::<String>()}));
            let failed = pending["status"] == "error";
            drop(pending);
            if failed { let _ = show_confirmation(&app); }
        }
    });
    Ok(())
}

#[tauri::command]
pub fn get_store_open_request(app: AppHandle, window: WebviewWindow) -> Result<Value, String> {
    confirmation_window(&window)?;
    Ok(app.state::<StoreOpenState>().0.lock().map_err(|_| "App link state unavailable")?.clone())
}

#[tauri::command]
pub fn dismiss_store_open_request(app: AppHandle, window: WebviewWindow, request_id: String) -> Result<(), String> {
    confirmation_window(&window)?;
    let state = app.state::<StoreOpenState>();
    let mut pending = state.0.lock().map_err(|_| "App link state unavailable")?;
    if pending["request_id"] != request_id || pending["status"] == "installing" {
        return Err("Wait for the current app operation to finish".into());
    }
    if pending["status"] == "downloading" { pending["cancelled"] = json!(true); }
    else { *pending = Value::Null; }
    drop(pending);
    window.close().map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn confirm_store_open_request(app: AppHandle, window: WebviewWindow, request_id: String) -> Result<Value, String> {
    confirmation_window(&window)?;
    let request = {
        let state = app.state::<StoreOpenState>();
        let mut pending = state.0.lock().map_err(|_| "App link state unavailable")?;
        if pending["request_id"] != request_id || pending["status"] != "ready" {
            return Err("App link confirmation is stale; review the current application".into());
        }
        let request = pending.clone();
        pending["status"] = json!("installing");
        request
    };
    tauri::async_runtime::spawn_blocking(move || {
        let result = (|| -> Result<Value, String> {
            let data = app_data_dir(&app)?;
            let id = request["app_id"].as_str().ok_or("Missing app ID")?;
            let digest = request["record"]["digests"]["package_sha256"].as_str().ok_or("Missing package digest")?;
            local_product::install(&data, id, digest)?;
            let input = serde_json::from_value(json!({"app_id":id, "action":"enable"})).map_err(|_| "Invalid enable request")?;
            local_product::lifecycle(&data, input)?;
            open_known_app_window(&app, id, true)
        })();
        let state = app.state::<StoreOpenState>();
        if let Ok(mut pending) = state.0.lock() {
            *pending = match &result {
                Ok(_) => Value::Null,
                Err(error) => json!({"request_id":request_id, "app_id":request["app_id"],
                    "status":"error", "message":error.chars().take(1000).collect::<String>()}),
            };
        }
        if result.is_ok() {
            if let Some(window) = app.get_webview_window(INSTALL_WINDOW) { let _ = window.close(); }
        }
        result
    }).await.map_err(|_| "Store install worker failed".to_string())?
}
