//! Generic support for the exact experimental-v0 UI-only world. No app-domain logic.
#![allow(dead_code)]

pub use crate::exports::vibapp::experimental_v0::guest;
pub use crate::vibapp::experimental_v0::{common, settings, ui};
use alloc::{string::ToString, vec, vec::Vec};
use core::{
    alloc::{GlobalAlloc, Layout},
    sync::atomic::{AtomicUsize, Ordering},
};

/// Absolute linear-memory ceiling, including stack/static data. Host limits still apply.
pub const MAX_MEMORY_BYTES: usize = 16 * 1024 * 1024;
const PAGE: usize = 65_536;
static NEXT: AtomicUsize = AtomicUsize::new(0);
unsafe extern "C" {
    static __heap_base: u8;
}

/// Monotonic allocator: bounded, with no reclamation or unsafe per-call reset.
pub struct Allocator;
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        loop {
            let observed = NEXT.load(Ordering::Relaxed);
            let start = if observed == 0 {
                core::ptr::addr_of!(__heap_base) as usize
            } else {
                observed
            };
            let Some(padded) = start.checked_add(layout.align() - 1) else {
                return core::ptr::null_mut();
            };
            let aligned = padded & !(layout.align() - 1);
            let Some(end) = aligned.checked_add(layout.size().max(1)) else {
                return core::ptr::null_mut();
            };
            if end > MAX_MEMORY_BYTES {
                return core::ptr::null_mut();
            }
            let available = core::arch::wasm32::memory_size(0) * PAGE;
            if end > available
                && core::arch::wasm32::memory_grow(0, (end - available).div_ceil(PAGE))
                    == usize::MAX
            {
                return core::ptr::null_mut();
            }
            if NEXT
                .compare_exchange(observed, end, Ordering::Relaxed, Ordering::Relaxed)
                .is_ok()
            {
                return aligned as *mut u8;
            }
        }
    }

    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {}

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if new_size <= layout.size() {
            return pointer;
        }
        let Ok(new_layout) = Layout::from_size_align(new_size, layout.align()) else {
            return core::ptr::null_mut();
        };
        let next = unsafe { self.alloc(new_layout) };
        if !next.is_null() {
            unsafe { core::ptr::copy_nonoverlapping(pointer, next, layout.size()) };
        }
        next
    }
}

pub fn trap() -> ! {
    core::arch::wasm32::unreachable()
}

/// Canonical ABI realloc preserves the old prefix; invalid layouts/OOM trap.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn cabi_realloc(
    old: *mut u8,
    old_size: usize,
    alignment: usize,
    new_size: usize,
) -> *mut u8 {
    let Ok(layout) = Layout::from_size_align(old_size.max(1), alignment) else {
        trap()
    };
    if new_size == 0 {
        return alignment as *mut u8;
    }
    let pointer = if old_size == 0 {
        let Ok(new_layout) = Layout::from_size_align(new_size, alignment) else {
            trap()
        };
        unsafe { Allocator.alloc(new_layout) }
    } else {
        if old.is_null() {
            trap();
        }
        unsafe { Allocator.realloc(old, layout, new_size) }
    };
    if pointer.is_null() {
        trap();
    }
    pointer
}

/// Volatile byte reads prevent LLVM from lowering this body back to memcmp.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn memcmp(left: *const u8, right: *const u8, count: usize) -> i32 {
    for index in 0..count {
        let a = unsafe { left.add(index).read_volatile() };
        let b = unsafe { right.add(index).read_volatile() };
        if a != b {
            return i32::from(a) - i32::from(b);
        }
    }
    0
}

pub fn error(message: &str) -> common::AppError {
    common::AppError {
        code: common::ErrorCode::InvalidArgument,
        message: message.to_string(),
        retryable: false,
    }
}

pub fn ui_descriptor(
    id: &str,
    version: &str,
    display_name: &str,
    entrypoint: &str,
    label: &str,
    route: &str,
) -> guest::AppDescriptor {
    guest::AppDescriptor {
        id: id.to_string(),
        version: version.to_string(),
        kind: common::AppKind::Ui,
        display_name: display_name.to_string(),
        entrypoints: vec![guest::EntrypointDescriptor {
            id: entrypoint.to_string(),
            kind: common::EntrypointKind::LauncherUi,
            label: label.to_string(),
            initial_route: Some(route.to_string()),
        }],
    }
}

/// Only for apps declaring no settings schema; rejects supplied settings.
pub fn no_settings(
    proposed: &settings::SettingsSnapshot,
) -> Result<settings::SettingsValidation, common::AppError> {
    if !proposed.values.is_empty() {
        return Err(error("this app has no settings"));
    }
    Ok(settings::SettingsValidation {
        accepted: true,
        field_errors: Vec::new(),
        service_restart_required: false,
    })
}

/// App-reported readiness, never a verifier or dependency-health assertion.
pub fn healthy(message: &str) -> guest::HealthReport {
    guest::HealthReport {
        status: guest::HealthStatus::Healthy,
        checks: vec![guest::HealthCheck {
            name: "app".to_string(),
            status: guest::HealthStatus::Healthy,
            message: message.to_string(),
        }],
    }
}

pub fn unchanged_migration(
    request: &guest::MigrationRequest,
    supported_schema: u32,
) -> Result<guest::MigrationResult, common::AppError> {
    if request.from_schema != supported_schema || request.to_schema != supported_schema {
        return Err(error("unsupported state schema migration"));
    }
    Ok(guest::MigrationResult {
        status: guest::MigrationStatus::Unchanged,
        target_state_revision: request.target_state_revision,
    })
}

pub fn empty_output() -> guest::EventOutput {
    guest::EventOutput {
        surfaces: Vec::new(),
        diagnostic: None,
    }
}
pub fn output(surface: ui::SurfaceUpdate) -> guest::EventOutput {
    guest::EventOutput {
        surfaces: vec![surface],
        diagnostic: None,
    }
}

/// Borrow only the host-supplied IDs in this event; never invent surface/session IDs.
pub struct SurfaceTarget<'a> {
    pub session: &'a str,
    pub surface: &'a str,
    pub route: &'a str,
}
impl SurfaceTarget<'_> {
    pub fn update(&self, view: ui::View) -> ui::SurfaceUpdate {
        ui::SurfaceUpdate {
            session: self.session.to_string(),
            surface: self.surface.to_string(),
            route: self.route.to_string(),
            view,
        }
    }
}
pub fn surface_target(event: &guest::AppEvent) -> Option<SurfaceTarget<'_>> {
    let guest::AppEvent::Launcher(event) = event else {
        return None;
    };
    let (session, surface, route) = match event {
        guest::LauncherEvent::Launch(e) => (&e.session, &e.surface, &e.route),
        guest::LauncherEvent::Open(e) => (&e.session, &e.surface, &e.route),
        guest::LauncherEvent::Restore(e) => (&e.session, &e.surface, &e.route),
        guest::LauncherEvent::Action(e) => (&e.session, &e.surface, &e.route),
        guest::LauncherEvent::Close(_) | guest::LauncherEvent::Focus(_) => return None,
    };
    Some(SurfaceTarget {
        session,
        surface,
        route,
    })
}
pub fn action_id(event: &guest::AppEvent) -> Option<&str> {
    match event {
        guest::AppEvent::Launcher(guest::LauncherEvent::Action(e)) => Some(&e.action),
        _ => None,
    }
}
pub fn changed_fields(event: &guest::AppEvent) -> &[ui::FieldChange] {
    match event {
        guest::AppEvent::Launcher(guest::LauncherEvent::Action(e)) => &e.fields,
        guest::AppEvent::Launcher(guest::LauncherEvent::Restore(e)) => &e.safe_fields,
        _ => &[],
    }
}
pub fn field_value<'a>(
    fields: &'a [ui::FieldChange],
    key: &str,
) -> Result<Option<&'a ui::FieldValue>, common::AppError> {
    let mut found = None;
    for item in fields {
        if item.field == key {
            if found.is_some() {
                return Err(error("duplicate field change"));
            }
            found = Some(&item.value);
        }
    }
    Ok(found)
}
pub fn text_value<'a>(
    fields: &'a [ui::FieldChange],
    key: &str,
) -> Result<Option<&'a str>, common::AppError> {
    match field_value(fields, key)? {
        None => Ok(None),
        Some(ui::FieldValue::Text(value)) => Ok(Some(value)),
        _ => Err(error("expected a text field value")),
    }
}
pub fn choice_value<'a>(
    fields: &'a [ui::FieldChange],
    key: &str,
) -> Result<Option<&'a str>, common::AppError> {
    match field_value(fields, key)? {
        None => Ok(None),
        Some(ui::FieldValue::Choice(value)) => Ok(Some(value)),
        _ => Err(error("expected a choice field value")),
    }
}

pub fn node(id: &str, parent: Option<&str>, kind: ui::NodeKind) -> ui::Node {
    ui::Node {
        id: id.to_string(),
        parent: parent.map(ToString::to_string),
        kind,
    }
}
pub fn list(id: &str, parent: Option<&str>, label: Option<&str>) -> ui::Node {
    node(
        id,
        parent,
        ui::NodeKind::ListContainer(ui::ListNode {
            label: label.map(ToString::to_string),
        }),
    )
}
pub fn text(id: &str, parent: Option<&str>, value: &str, style: ui::TextStyle) -> ui::Node {
    node(
        id,
        parent,
        ui::NodeKind::Text(ui::TextNode {
            text: value.to_string(),
            style,
        }),
    )
}
pub fn button(
    id: &str,
    parent: Option<&str>,
    label: &str,
    action: &str,
    style: ui::ButtonStyle,
    disabled: bool,
) -> ui::Node {
    node(
        id,
        parent,
        ui::NodeKind::Button(ui::ButtonNode {
            label: label.to_string(),
            action: action.to_string(),
            style,
            disabled,
        }),
    )
}
pub fn field(id: &str, parent: Option<&str>, value: ui::FieldNode) -> ui::Node {
    node(id, parent, ui::NodeKind::Field(value))
}
pub fn text_field(id: &str, parent: Option<&str>, key: &str, label: &str, value: &str) -> ui::Node {
    field(
        id,
        parent,
        ui::FieldNode {
            field: key.to_string(),
            label: label.to_string(),
            kind: ui::FieldKind::Text,
            value: ui::FieldValue::Text(value.to_string()),
            required: false,
            sensitive: false,
            choices: Vec::new(),
            validation_message: None,
        },
    )
}
pub fn choice(value: &str, label: &str) -> ui::ChoiceOption {
    ui::ChoiceOption {
        value: value.to_string(),
        label: label.to_string(),
    }
}
pub fn view(title: &str, root: &str, nodes: Vec<ui::Node>) -> ui::View {
    ui::View {
        title: title.to_string(),
        root: root.to_string(),
        nodes,
    }
}

/// Retain every selected-world import without performing calls in normal Guest methods.
/// This core export is not a Guest entrypoint. Never call it from application logic.
#[unsafe(export_name = "__vibapp_force_declared_imports")]
pub extern "C" fn retain_ui_world_imports() {
    use crate::vibapp::experimental_v0::{clock, host_info, kv, log, settings};
    let _ = clock::monotonic_now();
    let _ = kv::get("");
    let _ = log::write(log::Level::Debug, "", &[], "support-import-retention");
    let _ = host_info::describe_host();
    let _ = settings::current();
}
