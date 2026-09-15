#![no_std]

extern crate alloc;

use alloc::format;
use alloc::string::ToString;
use alloc::vec;
use alloc::vec::Vec;
use core::alloc::{GlobalAlloc, Layout};
use core::panic::PanicInfo;
use core::sync::atomic::{AtomicUsize, Ordering};

struct BumpAllocator;

static NEXT: AtomicUsize = AtomicUsize::new(0);

unsafe extern "C" {
    static __heap_base: u8;
}

#[unsafe(no_mangle)]
unsafe extern "C" fn memcmp(left: *const u8, right: *const u8, length: usize) -> i32 {
    for index in 0..length {
        let left = unsafe { *left.add(index) };
        let right = unsafe { *right.add(index) };
        if left != right {
            return i32::from(left) - i32::from(right);
        }
    }
    0
}

unsafe impl GlobalAlloc for BumpAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        loop {
            let observed = NEXT.load(Ordering::Relaxed);
            let current = if observed == 0 {
                core::ptr::addr_of!(__heap_base) as usize
            } else {
                observed
            };
            let aligned = current.saturating_add(layout.align() - 1) & !(layout.align() - 1);
            let Some(end) = aligned.checked_add(layout.size()) else {
                return core::ptr::null_mut();
            };
            let available = core::arch::wasm32::memory_size(0) * 65_536;
            if end > available {
                let pages = (end - available).div_ceil(65_536);
                if core::arch::wasm32::memory_grow(0, pages) == usize::MAX {
                    return core::ptr::null_mut();
                }
            }
            match NEXT.compare_exchange(observed, end, Ordering::Relaxed, Ordering::Relaxed) {
                Ok(_) => return aligned as *mut u8,
                Err(_) => continue,
            }
        }
    }

    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {}
}

#[global_allocator]
static ALLOCATOR: BumpAllocator = BumpAllocator;

#[unsafe(no_mangle)]
unsafe extern "C" fn cabi_realloc(
    _old_pointer: *mut u8,
    _old_size: usize,
    alignment: usize,
    new_size: usize,
) -> *mut u8 {
    let Ok(layout) = Layout::from_size_align(new_size.max(1), alignment.max(1)) else {
        return core::ptr::null_mut();
    };
    unsafe { ALLOCATOR.alloc(layout) }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop {
        core::hint::spin_loop();
    }
}

#[cfg(any(
    all(feature = "service", feature = "hybrid"),
    all(feature = "service", feature = "ui"),
    all(feature = "hybrid", feature = "ui")
))]
compile_error!("select exactly one fixture world");
#[cfg(not(any(feature = "service", feature = "hybrid", feature = "ui")))]
compile_error!("select exactly one fixture world");

#[cfg(feature = "service")]
wit_bindgen::generate!({
    path: "../../../../wit/experimental-v0",
    world: "service-only-reference",
});

#[cfg(feature = "hybrid")]
wit_bindgen::generate!({
    path: "../../../../wit/experimental-v0",
    world: "hybrid-reference",
});

#[cfg(feature = "ui")]
wit_bindgen::generate!({
    path: "../../../../wit/experimental-v0",
    world: "ui-only-reference",
});

#[cfg(not(feature = "migration-fail"))]
use exports::vibapp::experimental_v0::guest::MigrationStatus;
use exports::vibapp::experimental_v0::guest::{
    AppDescriptor, AppError, AppEvent, EntrypointDescriptor, EventOutput, Guest, HealthCheck,
    HealthReport, HealthRequest, HealthStatus, MigrationRequest, MigrationResult,
};
use vibapp::experimental_v0::common::{AppKind, CallContext, EntrypointKind, ErrorCode};
use vibapp::experimental_v0::kv;
#[cfg(all(feature = "v2", not(feature = "migration-fail")))]
use vibapp::experimental_v0::kv::{
    DeleteOperation, PutOperation, TransactionRequest, WriteOperation,
};
use vibapp::experimental_v0::settings::{
    self, SettingsSchema, SettingsSnapshot, SettingsValidation,
};
#[cfg(any(feature = "ui", feature = "hybrid"))]
use vibapp::experimental_v0::ui::{
    ButtonNode, ButtonStyle, FieldKind, FieldNode, FieldValue, Node, NodeKind, SurfaceUpdate,
    TextNode, TextStyle, View,
};

struct Fixture;

fn internal(message: &str) -> AppError {
    AppError {
        code: ErrorCode::Internal,
        message: message.to_string(),
        retryable: false,
    }
}

fn host_failure(error: vibapp::experimental_v0::common::HostError) -> AppError {
    AppError {
        code: error.code,
        message: error.message,
        retryable: error.retryable,
    }
}

#[cfg(feature = "v2")]
fn bytes_equal(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .zip(right.iter())
            .all(|(left, right)| left == right)
}

impl Guest for Fixture {
    fn describe() -> Result<AppDescriptor, AppError> {
        #[allow(unused_mut)]
        let mut entrypoints = Vec::new();
        #[cfg(any(feature = "service", feature = "hybrid"))]
        entrypoints.push(EntrypointDescriptor {
            id: "main-service".to_string(),
            kind: EntrypointKind::Service,
            label: "Service".to_string(),
            initial_route: None,
        });
        #[cfg(any(feature = "service", feature = "hybrid"))]
        entrypoints.push(EntrypointDescriptor {
            id: "other-service".to_string(),
            kind: EntrypointKind::Service,
            label: "Other".to_string(),
            initial_route: None,
        });
        #[cfg(any(feature = "ui", feature = "hybrid"))]
        entrypoints.push(EntrypointDescriptor {
            id: "main-ui".to_string(),
            kind: EntrypointKind::LauncherUi,
            label: "Main".to_string(),
            initial_route: Some("home".to_string()),
        });
        Ok(AppDescriptor {
            id: if cfg!(feature = "app-b") {
                "ai.vibapp.runtime-service-fixture-b".to_string()
            } else {
                "ai.vibapp.runtime-service-fixture".to_string()
            },
            version: if cfg!(feature = "v2") {
                "0.2.0".to_string()
            } else {
                "0.1.0".to_string()
            },
            kind: if cfg!(feature = "hybrid") {
                AppKind::Hybrid
            } else if cfg!(feature = "ui") {
                AppKind::Ui
            } else {
                AppKind::Service
            },
            display_name: "Runtime Service Fixture".to_string(),
            entrypoints,
        })
    }

    fn get_settings_schema() -> Result<Option<SettingsSchema>, AppError> {
        Ok(None)
    }

    fn validate_settings(
        _context: CallContext,
        _proposed: SettingsSnapshot,
    ) -> Result<SettingsValidation, AppError> {
        Ok(SettingsValidation {
            accepted: true,
            field_errors: Vec::new(),
            service_restart_required: false,
        })
    }

    fn handle_event(context: CallContext, event: AppEvent) -> Result<EventOutput, AppError> {
        let snapshot = settings::current().map_err(host_failure)?;
        if snapshot.schema_revision != 1 || snapshot.config_revision != 1 {
            return Err(internal("settings snapshot revision is not host-bound"));
        }
        #[allow(unused_mut)]
        let mut surfaces = Vec::new();
        let diagnostic = match event {
            #[cfg(any(feature = "service", feature = "hybrid"))]
            AppEvent::Service(service) => match service {
                exports::vibapp::experimental_v0::guest::ServiceEvent::Start(event) => {
                    Some(format!("started:{}:{}", event.entrypoint, event.instance))
                }
                exports::vibapp::experimental_v0::guest::ServiceEvent::Trigger(event) => {
                    if event.trigger_id == "fail" {
                        return Err(internal("fixture requested failure"));
                    }
                    if event.trigger_id == "read-ui-kv" {
                        let entry = kv::get("ui/last-action")
                            .map_err(host_failure)?
                            .ok_or_else(|| internal("shared UI state is missing"))?;
                        if entry.value.as_slice() != b"increment" {
                            return Err(internal("shared UI state has unexpected bytes"));
                        }
                        Some(format!(
                            "shared-kv:{}:{}",
                            entry.revision,
                            entry.value.len()
                        ))
                    } else {
                        Some(format!(
                            "triggered:{}:{}",
                            event.trigger_id,
                            event.payload.len()
                        ))
                    }
                }
                exports::vibapp::experimental_v0::guest::ServiceEvent::Stop(event) => {
                    Some(format!("stopped:{}:{}", event.entrypoint, event.instance))
                }
            },
            #[cfg(any(feature = "ui", feature = "hybrid"))]
            AppEvent::Launcher(event) => {
                use exports::vibapp::experimental_v0::guest::LauncherEvent;
                let (session, surface, route, title) = match event {
                    LauncherEvent::Launch(event) => (
                        event.session,
                        event.surface,
                        event.route,
                        "Fixture ready".to_string(),
                    ),
                    LauncherEvent::Open(event) => (
                        event.session,
                        event.surface,
                        event.route,
                        "Fixture refreshed".to_string(),
                    ),
                    LauncherEvent::Action(event) => {
                        if event.action != "increment" {
                            return Err(internal("fixture received an unknown UI action"));
                        }
                        let result =
                            kv::transact(&vibapp::experimental_v0::kv::TransactionRequest {
                                operations: vec![vibapp::experimental_v0::kv::WriteOperation::Put(
                                    vibapp::experimental_v0::kv::PutOperation {
                                        key: "ui/last-action".to_string(),
                                        value: b"increment".to_vec(),
                                        expected_revision: None,
                                    },
                                )],
                                idempotency_key: context.idempotency_key.clone(),
                            })
                            .map_err(host_failure)?;
                        (
                            event.session,
                            event.surface,
                            event.route,
                            format!("Action committed at revision {}", result.state_revision),
                        )
                    }
                    _ => return Err(internal("fixture accepts only UI launch/action events")),
                };
                surfaces.push(SurfaceUpdate {
                    session,
                    surface,
                    route,
                    view: View {
                        title: title.clone(),
                        root: "root".to_string(),
                        nodes: vec![
                            Node {
                                id: "root".to_string(),
                                parent: None,
                                kind: NodeKind::Text(TextNode {
                                    text: title,
                                    style: TextStyle::Title,
                                }),
                            },
                            Node {
                                id: "name".to_string(),
                                parent: Some("root".to_string()),
                                kind: NodeKind::Field(FieldNode {
                                    field: "name".to_string(),
                                    label: "Name".to_string(),
                                    kind: FieldKind::Text,
                                    value: FieldValue::Text("Ada".to_string()),
                                    required: false,
                                    sensitive: false,
                                    choices: Vec::new(),
                                    validation_message: None,
                                }),
                            },
                            Node {
                                id: "increment".to_string(),
                                parent: Some("root".to_string()),
                                kind: NodeKind::Button(ButtonNode {
                                    label: "Increment".to_string(),
                                    action: "increment".to_string(),
                                    style: ButtonStyle::Primary,
                                    disabled: false,
                                }),
                            },
                        ],
                    },
                });
                Some("ui-event".to_string())
            }
            _ => return Err(internal("fixture accepts only service events")),
        };
        Ok(EventOutput {
            surfaces,
            diagnostic,
        })
    }

    fn health(_context: CallContext, request: HealthRequest) -> Result<HealthReport, AppError> {
        let entrypoint = request
            .service_entrypoint
            .unwrap_or_else(|| "all".to_string());
        #[cfg(feature = "unhealthy")]
        return Ok(HealthReport {
            status: HealthStatus::Unhealthy,
            checks: vec![HealthCheck {
                name: "fixture".to_string(),
                status: HealthStatus::Unhealthy,
                message: format!("{entrypoint} rejected activation"),
            }],
        });
        #[cfg(all(not(feature = "unhealthy"), feature = "v2"))]
        {
            let migrated = kv::get("profile/display-name")
                .map_err(host_failure)?
                .ok_or_else(|| internal("migrated profile state is missing"))?;
            if !bytes_equal(&migrated.value, b"migrated:alice") {
                return Ok(HealthReport {
                    status: HealthStatus::Unhealthy,
                    checks: vec![HealthCheck {
                        name: "migration-state".to_string(),
                        status: HealthStatus::Unhealthy,
                        message: "migrated profile state has unexpected bytes".to_string(),
                    }],
                });
            }
        }
        #[cfg(not(feature = "unhealthy"))]
        Ok(HealthReport {
            status: HealthStatus::Healthy,
            checks: vec![HealthCheck {
                name: "fixture".to_string(),
                status: HealthStatus::Healthy,
                message: format!("{entrypoint} is responsive"),
            }],
        })
    }

    fn migrate(
        _context: CallContext,
        request: MigrationRequest,
    ) -> Result<MigrationResult, AppError> {
        #[cfg(feature = "migration-fail")]
        {
            let _ = request;
            return Err(internal("fixture rejected state migration"));
        }
        #[cfg(all(not(feature = "migration-fail"), feature = "v2"))]
        {
            let source = kv::get("profile/name")
                .map_err(host_failure)?
                .ok_or_else(|| internal("source profile state is missing"))?;
            if !bytes_equal(&source.value, b"alice") {
                return Err(internal("source profile state has unexpected bytes"));
            }
            let mut migrated = b"migrated:".to_vec();
            migrated.extend_from_slice(&source.value);
            let result = kv::transact(&TransactionRequest {
                operations: vec![
                    WriteOperation::Put(PutOperation {
                        key: "profile/display-name".to_string(),
                        value: migrated,
                        expected_revision: None,
                    }),
                    WriteOperation::Delete(DeleteOperation {
                        key: "profile/name".to_string(),
                        expected_revision: Some(source.revision),
                    }),
                ],
                idempotency_key: "fixture-migrate-profile-v2".to_string(),
            })
            .map_err(host_failure)?;
            if result.state_revision != request.target_state_revision {
                return Err(internal("KV broker returned another target revision"));
            }
            return Ok(MigrationResult {
                status: MigrationStatus::Migrated,
                target_state_revision: request.target_state_revision,
            });
        }
        #[cfg(all(not(feature = "migration-fail"), not(feature = "v2")))]
        Ok(MigrationResult {
            status: MigrationStatus::Unchanged,
            target_state_revision: request.target_state_revision,
        })
    }
}

export!(Fixture);
