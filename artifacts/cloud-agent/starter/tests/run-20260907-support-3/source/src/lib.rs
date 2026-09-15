#![no_std]
extern crate alloc;
mod vibapp_support;
use alloc::{string::ToString, vec, vec::Vec};
use core::alloc::{GlobalAlloc, Layout};
use s::guest::{
    AppDescriptor, AppError, AppEvent, CallContext, CloseEvent, EventOutput, Guest, HealthReport,
    HealthRequest, LauncherEvent, MigrationRequest, MigrationResult, SettingsSchema,
    SettingsSnapshot, SettingsValidation,
};
use s::settings::SettingValue;
use s::ui::{ActionEvent, ButtonStyle, FieldChange, FieldKind, FieldNode, FieldValue, TextStyle};
use vibapp_support as s;
#[global_allocator]
static ALLOCATOR: s::Allocator = s::Allocator;
#[panic_handler]
fn panic(_: &core::panic::PanicInfo<'_>) -> ! {
    s::trap()
}
wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" });

// Infrastructure-only, synthetic, non-public. Never a generated product application.
struct SupportSmoke;
fn support_checks() {
    unsafe {
        let first = s::cabi_realloc(core::ptr::null_mut(), 0, 32, 5);
        assert_eq!(first as usize % 32, 0);
        for i in 0..5 {
            first.add(i).write((i + 19) as u8);
        }
        let grown = s::cabi_realloc(first, 5, 32, 17);
        assert_eq!(grown as usize % 32, 0);
        for i in 0..5 {
            assert_eq!(grown.add(i).read(), (i + 19) as u8);
        }
        let shrunk = s::cabi_realloc(grown, 17, 32, 3);
        assert_eq!(shrunk, grown);
        assert_eq!(shrunk.add(2).read(), 21);
        assert_eq!(s::cabi_realloc(shrunk, 3, 32, 0) as usize, 32);
        assert!(
            ALLOCATOR
                .alloc(Layout::from_size_align(s::MAX_MEMORY_BYTES, 1).unwrap())
                .is_null()
        );
        assert_eq!(s::memcmp(core::ptr::null(), core::ptr::null(), 0), 0);
        let lhs = [0u8, 128, 255];
        let rhs = [0u8, 128, 254];
        assert!(s::memcmp(lhs.as_ptr(), rhs.as_ptr(), 3) > 0);
        assert!(s::memcmp(rhs.as_ptr(), lhs.as_ptr(), 3) < 0);
    }
    let mut bytes = Vec::with_capacity(1);
    for i in 0..4096 {
        bytes.push((i % 251) as u8);
    }
    for i in 0..4096 {
        assert_eq!(bytes[i], (i % 251) as u8);
    }
    let mut text = "prefix".to_string();
    for _ in 0..1024 {
        text.push('x');
    }
    assert!(text.starts_with("prefix"));
    assert_eq!(text.len(), 1030);
    let fields = vec![FieldChange {
        field: "input".to_string(),
        value: FieldValue::Text("fixture".to_string()),
    }];
    assert_eq!(s::text_value(&fields, "input").unwrap(), Some("fixture"));
    assert!(s::choice_value(&fields, "input").is_err());
    assert!(s::field_value(&fields, "absent").unwrap().is_none());
    let duplicates = vec![fields[0].clone(), fields[0].clone()];
    assert!(s::field_value(&duplicates, "input").is_err());
    let event = AppEvent::Launcher(LauncherEvent::Action(ActionEvent {
        session: "test-session".to_string(),
        surface: "test-surface".to_string(),
        route: "test-route".to_string(),
        action: "echo".to_string(),
        fields,
    }));
    assert_eq!(s::action_id(&event), Some("echo"));
    assert_eq!(s::changed_fields(&event).len(), 1);
    let target = s::surface_target(&event).unwrap();
    let view = s::view(
        "Fixture",
        "root",
        vec![
            s::list("root", None, None),
            s::text("text", Some("root"), "fixture", TextStyle::Body),
            s::button(
                "button",
                Some("root"),
                "Echo",
                "echo",
                ButtonStyle::Primary,
                false,
            ),
            s::text_field("input-node", Some("root"), "input", "Input", "fixture"),
            s::field(
                "choice-node",
                Some("root"),
                FieldNode {
                    field: "choice".to_string(),
                    label: "Choice".to_string(),
                    kind: FieldKind::Choice,
                    value: FieldValue::Choice("a".to_string()),
                    required: true,
                    sensitive: false,
                    choices: vec![s::choice("a", "A")],
                    validation_message: None,
                },
            ),
        ],
    );
    let output = s::output(target.update(view));
    assert_eq!(output.surfaces[0].session, "test-session");
    assert_eq!(output.surfaces[0].surface, "test-surface");
    assert_eq!(output.surfaces[0].route, "test-route");
    let closed = AppEvent::Launcher(LauncherEvent::Close(CloseEvent {
        entrypoint: "main".to_string(),
        session: "test-session".to_string(),
        surface: "test-surface".to_string(),
    }));
    assert!(s::surface_target(&closed).is_none());
    let settings = SettingsSnapshot {
        schema_revision: 1,
        config_revision: 0,
        values: vec![],
    };
    assert!(s::no_settings(&settings).unwrap().accepted);
    let bad_settings = SettingsSnapshot {
        values: vec![SettingValue {
            key: "unknown".to_string(),
            value: FieldValue::Empty,
        }],
        ..settings
    };
    assert!(s::no_settings(&bad_settings).is_err());
    let migration = MigrationRequest {
        from_schema: 1,
        to_schema: 1,
        source_state_revision: 9,
        target_state_revision: 11,
    };
    assert_eq!(
        s::unchanged_migration(&migration, 1)
            .unwrap()
            .target_state_revision,
        11
    );
    assert!(s::unchanged_migration(&migration, 2).is_err());
}
impl Guest for SupportSmoke {
    fn describe() -> Result<AppDescriptor, AppError> {
        support_checks();
        Ok(s::ui_descriptor(
            "example.vibapp.support-smoke",
            "0.1.0",
            "Support Smoke",
            "main",
            "Support Smoke",
            "home",
        ))
    }
    fn get_settings_schema() -> Result<Option<SettingsSchema>, AppError> {
        Ok(None)
    }
    fn validate_settings(
        _: CallContext,
        proposed: SettingsSnapshot,
    ) -> Result<SettingsValidation, AppError> {
        s::no_settings(&proposed)
    }
    fn handle_event(_: CallContext, event: AppEvent) -> Result<EventOutput, AppError> {
        let Some(target) = s::surface_target(&event) else {
            return Ok(s::empty_output());
        };
        let value = s::text_value(s::changed_fields(&event), "input")?.unwrap_or("fixture");
        Ok(s::output(target.update(s::view(
            "Support Smoke",
            "root",
            vec![
                s::list("root", None, None),
                s::text("text", Some("root"), value, TextStyle::Body),
            ],
        ))))
    }
    fn health(_: CallContext, _: HealthRequest) -> Result<HealthReport, AppError> {
        Ok(s::healthy("synthetic support checks"))
    }
    fn migrate(_: CallContext, request: MigrationRequest) -> Result<MigrationResult, AppError> {
        s::unchanged_migration(&request, 1)
    }
}
export!(SupportSmoke);
