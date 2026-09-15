use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

const WINDOW_LABEL_PREFIX: &str = "vibapp-runtime-";
const PRESENTATION_SCHEMA: &str = "vibapp.host-presentation.experimental-v1";
const DEFAULT_WIDTH: u32 = 960;
const DEFAULT_HEIGHT: u32 = 720;
const DEFAULT_MINIMUM_WIDTH: u32 = 420;
const DEFAULT_MINIMUM_HEIGHT: u32 = 480;
const WIDTH_FLOOR: u32 = 360;
const HEIGHT_FLOOR: u32 = 220;
const WIDTH_CEILING: u32 = 1_600;
const HEIGHT_CEILING: u32 = 1_200;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WindowPresentation {
    preferred_width: u32,
    preferred_height: u32,
    minimum_width: u32,
    minimum_height: u32,
    resizable: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ValidatedWindowPresentation {
    presentation: WindowPresentation,
    active_package_digest_sha256: Option<String>,
}

impl Default for WindowPresentation {
    fn default() -> Self {
        Self {
            preferred_width: DEFAULT_WIDTH,
            preferred_height: DEFAULT_HEIGHT,
            minimum_width: DEFAULT_MINIMUM_WIDTH,
            minimum_height: DEFAULT_MINIMUM_HEIGHT,
            resizable: true,
        }
    }
}

impl WindowPresentation {
    pub fn from_host_metadata(value: &Value) -> Self {
        if value["schema_version"] != PRESENTATION_SCHEMA {
            return Self::default();
        }
        let preferred_width = bounded_dimension(
            value["preferred_width"].as_u64(),
            DEFAULT_WIDTH,
            WIDTH_FLOOR,
            WIDTH_CEILING,
        );
        let preferred_height = bounded_dimension(
            value["preferred_height"].as_u64(),
            DEFAULT_HEIGHT,
            HEIGHT_FLOOR,
            HEIGHT_CEILING,
        );
        let minimum_width = bounded_dimension(
            value["minimum_width"].as_u64(),
            DEFAULT_MINIMUM_WIDTH.min(preferred_width),
            WIDTH_FLOOR,
            preferred_width,
        );
        let minimum_height = bounded_dimension(
            value["minimum_height"].as_u64(),
            DEFAULT_MINIMUM_HEIGHT.min(preferred_height),
            HEIGHT_FLOOR,
            preferred_height,
        );
        Self {
            preferred_width,
            preferred_height,
            minimum_width,
            minimum_height,
            resizable: value["resizable"].as_bool().unwrap_or(true),
        }
    }

    fn as_value(&self) -> Value {
        json!({
            "schema_version": PRESENTATION_SCHEMA,
            "preferred_width": self.preferred_width,
            "preferred_height": self.preferred_height,
            "minimum_width": self.minimum_width,
            "minimum_height": self.minimum_height,
            "resizable": self.resizable,
        })
    }
}

impl ValidatedWindowPresentation {
    pub fn from_catalog_item(item: &Value) -> Result<Self, String> {
        let installation_state = item["installation_state"].as_str().unwrap_or_default();
        let active_package_digest_sha256 = if installation_state == "installed" {
            let active = item["active_package_digest_sha256"]
                .as_str()
                .filter(|value| valid_digest(value))
                .ok_or_else(|| "已安装应用缺少 daemon active package 绑定。".to_string())?;
            if item["package_digest_sha256"] != active {
                return Err("窗口 presentation 没有绑定当前 daemon active package。".to_string());
            }
            Some(active.to_string())
        } else {
            None
        };
        Ok(Self {
            presentation: WindowPresentation::from_host_metadata(&item["presentation"]),
            active_package_digest_sha256,
        })
    }
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn bounded_dimension(value: Option<u64>, fallback: u32, floor: u32, ceiling: u32) -> u32 {
    value
        .and_then(|value| u32::try_from(value).ok())
        .unwrap_or(fallback)
        .clamp(floor, ceiling)
}

#[derive(Clone)]
struct SurfaceBinding {
    app_id: String,
    session: String,
    surface: String,
}

#[derive(Default)]
pub struct AppWindowState {
    bindings: Mutex<HashMap<String, SurfaceBinding>>,
    pending_fit: Mutex<HashMap<String, InitialFit>>,
}

struct InitialFit {
    width: f64,
    height: f64,
    minimum_height: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct RegisterSurfaceInput {
    app_id: String,
    session: String,
    surface: String,
    pub content_height: Option<u32>,
}

fn digest_hex_prefix(value: &str, bytes: usize) -> String {
    let digest = Sha256::digest(value.as_bytes());
    digest
        .iter()
        .take(bytes)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub fn window_label(app_id: &str) -> String {
    format!("{WINDOW_LABEL_PREFIX}{}", digest_hex_prefix(app_id, 10))
}

fn percent_encode_query(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

fn runtime_url(app_id: &str) -> WebviewUrl {
    WebviewUrl::App(PathBuf::from(format!(
        "index.html?runtimeApp={}",
        percent_encode_query(app_id)
    )))
}

fn trusted_title(value: &str) -> String {
    let cleaned: String = value
        .chars()
        .filter(|character| !character.is_control())
        .take(80)
        .collect();
    let app_name = if cleaned.trim().is_empty() {
        "VibApp 应用".to_string()
    } else {
        cleaned
    };
    format!("{app_name} — VibApp")
}

fn deterministic_icon(app_id: &str) -> tauri::image::Image<'static> {
    const SIDE: u32 = 64;
    const RADIUS: i32 = 13;
    let digest = Sha256::digest(app_id.as_bytes());
    let base = [
        32_u8.saturating_add(digest[0] / 3),
        92_u8.saturating_add(digest[1] / 3),
        72_u8.saturating_add(digest[2] / 3),
    ];
    let mut rgba = vec![0_u8; (SIDE * SIDE * 4) as usize];
    for y in 0..SIDE as i32 {
        for x in 0..SIDE as i32 {
            let dx = if x < RADIUS {
                RADIUS - x
            } else if x >= SIDE as i32 - RADIUS {
                x - (SIDE as i32 - RADIUS - 1)
            } else {
                0
            };
            let dy = if y < RADIUS {
                RADIUS - y
            } else if y >= SIDE as i32 - RADIUS {
                y - (SIDE as i32 - RADIUS - 1)
            } else {
                0
            };
            let inside = dx == 0 || dy == 0 || dx * dx + dy * dy <= RADIUS * RADIUS;
            let index = ((y as u32 * SIDE + x as u32) * 4) as usize;
            if inside {
                let lift = ((x + y) / 10).clamp(0, 12) as u8;
                rgba[index] = base[0].saturating_add(lift);
                rgba[index + 1] = base[1].saturating_add(lift);
                rgba[index + 2] = base[2].saturating_add(lift);
                rgba[index + 3] = 255;
            }
        }
    }
    // A small host-owned V mark keeps fallback icons recognizable without trusting guest pixels.
    for y in 18_i32..47 {
        let offset = (y - 18) / 2;
        for center in [20 + offset, 44 - offset] {
            for thickness in -2_i32..=2 {
                let x = center + thickness;
                if (0..SIDE as i32).contains(&x) {
                    let index = ((y as u32 * SIDE + x as u32) * 4) as usize;
                    rgba[index..index + 4].copy_from_slice(&[242, 251, 247, 255]);
                }
            }
        }
    }
    tauri::image::Image::new_owned(rgba, SIDE, SIDE)
}

fn take_binding(state: &AppWindowState, label: &str) -> Option<SurfaceBinding> {
    state
        .bindings
        .lock()
        .ok()
        .and_then(|mut bindings| bindings.remove(label))
}

fn cleanup_binding(app: AppHandle, label: String, state: &AppWindowState) {
    if let Ok(mut fitted) = state.pending_fit.lock() {
        fitted.remove(&label);
    }
    let binding = take_binding(state, &label);
    let Some(binding) = binding else {
        return;
    };
    std::thread::spawn(move || {
        let Ok(data_dir) = crate::app_data_dir(&app) else {
            return;
        };
        let _ = crate::local_product::lifecycle(
            &data_dir,
            crate::local_product::LifecycleInput {
                app_id: binding.app_id,
                action: "surface-close".to_string(),
                entrypoint: None,
                disposition: None,
                session: Some(binding.session),
                surface: Some(binding.surface),
                trigger_id: None,
                payload: None,
                package_digest_sha256: None,
            },
        );
    });
}

pub fn register_surface(
    state: &AppWindowState,
    window_label: &str,
    payload: RegisterSurfaceInput,
) -> Result<(), String> {
    if window_label != self::window_label(&payload.app_id) {
        return Err("应用窗口与运行时 app_id 绑定不匹配。".to_string());
    }
    if payload.session.is_empty() || payload.surface.is_empty() {
        return Err("应用窗口缺少可信 session 或 surface 绑定。".to_string());
    }
    state
        .bindings
        .lock()
        .map_err(|_| "应用窗口状态锁已损坏。".to_string())?
        .insert(
            window_label.to_string(),
            SurfaceBinding {
                app_id: payload.app_id,
                session: payload.session,
                surface: payload.surface,
            },
        );
    Ok(())
}

pub fn close(window: &WebviewWindow, state: &AppWindowState) -> Result<(), String> {
    let label = window.label().to_string();
    if !label.starts_with(WINDOW_LABEL_PREFIX) {
        return Err("只有 VibApp 独立应用窗口可以使用此关闭操作。".to_string());
    }
    cleanup_binding(window.app_handle().clone(), label, state);
    window
        .close()
        .map_err(|error| format!("无法关闭应用窗口：{error}"))
}

fn initial_content_height(content: u32, minimum: f64, available: f64) -> f64 {
    // A small screen still scrolls; an untrusted measurement cannot create an
    // enormous window. Historical package metadata remains byte-for-byte intact.
    f64::from(content.clamp(HEIGHT_FLOOR, HEIGHT_CEILING))
        .max(minimum)
        .min(available.max(f64::from(HEIGHT_FLOOR)))
}

pub fn fit_initial_content(
    window: &WebviewWindow,
    state: &AppWindowState,
    content_height: Option<u32>,
) -> Result<(), String> {
    let Some(content_height) = content_height else { return Ok(()); };
    if !state.bindings.lock().map_err(|_| "窗口绑定锁不可用")?.contains_key(window.label()) {
        return Err("窗口尚未注册运行时 surface。".to_string());
    }
    let Some(initial) = state.pending_fit.lock().map_err(|_| "窗口尺寸锁不可用")?.remove(window.label())
        else { return Ok(()); };
    if !window.is_resizable().unwrap_or(false) { return Ok(()); }
    let scale = window.scale_factor().map_err(|error| error.to_string())?;
    let current = window.inner_size().map_err(|error| error.to_string())?.to_logical::<f64>(scale);
    // Preserve a user's resize made while the application was starting.
    if (current.width - initial.width).abs() > 2.0 || (current.height - initial.height).abs() > 2.0 {
        return Ok(());
    }
    let available = window.current_monitor().map_err(|error| error.to_string())?
        .map(|monitor| monitor.size().to_logical::<f64>(monitor.scale_factor()).height - 100.0)
        .unwrap_or(720.0);
    let height = initial_content_height(content_height, initial.minimum_height, available);
    window.set_size(tauri::LogicalSize::new(current.width, height)).map_err(|error| error.to_string())?;
    window.center().map_err(|error| error.to_string())
}

pub fn open(
    app: &AppHandle,
    app_id: &str,
    display_name: &str,
    validated: ValidatedWindowPresentation,
) -> Result<Value, String> {
    let presentation = validated.presentation;
    let label = window_label(app_id);
    let title = trusted_title(display_name);
    if let Some(window) = app.get_webview_window(&label) {
        window
            .set_title(&title)
            .map_err(|error| format!("无法更新应用窗口标题：{error}"))?;
        if window.is_minimized().unwrap_or(false) {
            window
                .unminimize()
                .map_err(|error| format!("无法恢复应用窗口：{error}"))?;
        }
        window
            .show()
            .map_err(|error| format!("无法显示应用窗口：{error}"))?;
        window
            .set_focus()
            .map_err(|error| format!("无法聚焦应用窗口：{error}"))?;
        return Ok(json!({
            "windowLabel": label,
            "created": false,
            "focused": true,
            "presentation": presentation.as_value(),
            "activePackageDigestSha256": validated.active_package_digest_sha256,
        }));
    }

    let builder = WebviewWindowBuilder::new(app, &label, runtime_url(app_id))
        .title(&title)
        .inner_size(
            f64::from(presentation.preferred_width),
            f64::from(presentation.preferred_height),
        )
        .min_inner_size(
            f64::from(presentation.minimum_width),
            f64::from(presentation.minimum_height),
        )
        .resizable(presentation.resizable)
        .center()
        .prevent_overflow();
    let builder = builder
        .icon(deterministic_icon(app_id))
        .map_err(|error| format!("无法设置应用窗口图标：{error}"))?;
    let window = builder
        .build()
        .map_err(|error| format!("无法创建独立应用窗口：{error}"))?;
    if let (Ok(size), Ok(scale)) = (window.inner_size(), window.scale_factor()) {
        let size = size.to_logical::<f64>(scale);
        if let Ok(mut pending) = app.state::<AppWindowState>().pending_fit.lock() {
            pending.insert(label.clone(), InitialFit {
                width: size.width,
                height: size.height,
                minimum_height: f64::from(presentation.minimum_height),
            });
        }
    }
    let cleanup_label = label.clone();
    let cleanup_app = app.clone();
    window.on_window_event(move |event| {
        if matches!(event, WindowEvent::Destroyed) {
            let state = cleanup_app.state::<AppWindowState>();
            cleanup_binding(cleanup_app.clone(), cleanup_label.clone(), &state);
        }
    });
    Ok(json!({
        "windowLabel": label,
        "created": true,
        "focused": true,
        "presentation": presentation.as_value(),
        "activePackageDigestSha256": validated.active_package_digest_sha256,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn window_identity_is_stable_and_does_not_expose_app_id() {
        let first = window_label("ai.vibapp.custom.clock");
        let second = window_label("ai.vibapp.custom.clock");
        assert_eq!(first, second);
        assert!(first.starts_with(WINDOW_LABEL_PREFIX));
        assert!(!first.contains("ai.vibapp.custom.clock"));
        assert_ne!(first, window_label("ai.vibapp.custom.other"));
    }

    #[test]
    fn runtime_url_encodes_query_delimiters() {
        let WebviewUrl::App(path) = runtime_url("ai.vibapp/a?b") else {
            panic!("runtime URL must remain an app URL");
        };
        assert_eq!(
            path.to_string_lossy(),
            "index.html?runtimeApp=ai.vibapp%2Fa%3Fb"
        );
    }

    #[test]
    fn native_title_keeps_trusted_host_identity() {
        assert_eq!(trusted_title("LED\nClock"), "LEDClock — VibApp");
        assert_eq!(trusted_title("\n\t"), "VibApp 应用 — VibApp");
    }

    #[test]
    fn legacy_or_unknown_presentation_uses_safe_regular_defaults() {
        assert_eq!(
            WindowPresentation::from_host_metadata(&Value::Null),
            WindowPresentation::default()
        );
        assert_eq!(
            WindowPresentation::from_host_metadata(&json!({
                "schema_version": "guest-controlled-v9",
                "preferred_width": 10_000,
                "preferred_height": 10_000,
            })),
            WindowPresentation::default()
        );
    }

    #[test]
    fn compact_host_presentation_is_preserved_within_bounds() {
        let presentation = WindowPresentation::from_host_metadata(&json!({
            "schema_version": PRESENTATION_SCHEMA,
            "preferred_width": 520,
            "preferred_height": 300,
            "minimum_width": 360,
            "minimum_height": 220,
            "resizable": true,
        }));
        assert_eq!(presentation.preferred_width, 520);
        assert_eq!(presentation.preferred_height, 300);
        assert_eq!(presentation.minimum_width, 360);
        assert_eq!(presentation.minimum_height, 220);
        assert!(presentation.resizable);
    }

    #[test]
    fn hostile_dimensions_are_clamped_and_minimum_never_exceeds_preferred() {
        let presentation = WindowPresentation::from_host_metadata(&json!({
            "schema_version": PRESENTATION_SCHEMA,
            "preferred_width": 1,
            "preferred_height": 99_999,
            "minimum_width": 99_999,
            "minimum_height": 99_999,
            "resizable": false,
        }));
        assert_eq!(presentation.preferred_width, WIDTH_FLOOR);
        assert_eq!(presentation.preferred_height, HEIGHT_CEILING);
        assert_eq!(presentation.minimum_width, presentation.preferred_width);
        assert_eq!(presentation.minimum_height, presentation.preferred_height);
        assert!(!presentation.resizable);
    }

    #[test]
    fn installed_presentation_requires_the_exact_daemon_active_package() {
        let digest = "a".repeat(64);
        let item = json!({
            "installation_state": "installed",
            "package_digest_sha256": digest,
            "active_package_digest_sha256": "a".repeat(64),
            "presentation": {
                "schema_version": PRESENTATION_SCHEMA,
                "preferred_width": 520,
                "preferred_height": 300,
                "minimum_width": 420,
                "minimum_height": 240,
                "resizable": true,
            }
        });
        let validated = ValidatedWindowPresentation::from_catalog_item(&item).unwrap();
        assert_eq!(validated.active_package_digest_sha256, Some("a".repeat(64)));
        assert_eq!(validated.presentation.preferred_width, 520);

        let mut stale = item;
        stale["active_package_digest_sha256"] = json!("b".repeat(64));
        assert!(
            ValidatedWindowPresentation::from_catalog_item(&stale)
                .unwrap_err()
                .contains("active package")
        );
    }

    #[test]
    fn preview_presentation_has_no_daemon_package_authority() {
        let validated = ValidatedWindowPresentation::from_catalog_item(&json!({
            "installation_state": "preview-only",
            "presentation": Value::Null,
        }))
        .unwrap();
        assert_eq!(validated.presentation, WindowPresentation::default());
        assert_eq!(validated.active_package_digest_sha256, None);
    }

    #[test]
    fn surface_registration_rejects_a_different_window_identity() {
        let state = AppWindowState::default();
        let error = register_surface(
            &state,
            &window_label("ai.vibapp.other"),
            RegisterSurfaceInput {
                app_id: "ai.vibapp.clock".to_string(),
                session: "session-1".to_string(),
                surface: "surface-1".to_string(),
                content_height: None,
            },
        )
        .unwrap_err();
        assert!(error.contains("绑定不匹配"));
    }

    #[test]
    fn taking_a_surface_binding_twice_is_idempotent() {
        let state = AppWindowState::default();
        let app_id = "ai.vibapp.clock";
        let label = window_label(app_id);
        register_surface(
            &state,
            &label,
            RegisterSurfaceInput {
                app_id: app_id.to_string(),
                session: "session-1".to_string(),
                surface: "surface-1".to_string(),
                content_height: None,
            },
        )
        .unwrap();
        assert!(take_binding(&state, &label).is_some());
        assert!(take_binding(&state, &label).is_none());
    }

    #[test]
    fn first_content_fit_is_bounded_and_honors_available_screen() {
        assert_eq!(initial_content_height(640, 240.0, 900.0), 640.0);
        assert_eq!(initial_content_height(100, 480.0, 900.0), 480.0);
        assert_eq!(initial_content_height(u32::MAX, 240.0, 800.0), 800.0);
        assert_eq!(initial_content_height(900, 480.0, 400.0), 400.0);
    }
}
