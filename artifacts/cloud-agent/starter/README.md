# Rust support: UI-only experimental-v0

The supplied src/vibapp_support.rs and this guide are immutable generic platform
inputs, bound to this task's consent. Reuse the module unchanged.
Write the requested application yourself; never copy the synthetic test application.
The exact WIT and package intent are authoritative.

## Minimal src/lib.rs

```rust
#![no_std]
extern crate alloc;
mod vibapp_support;
mod app;
use vibapp_support as s;
use s::guest::*; // Includes AppError, CallContext and Settings* aliases.
#[global_allocator]
static ALLOCATOR: s::Allocator = s::Allocator;
#[panic_handler]
fn panic(_: &core::panic::PanicInfo<'_>) -> ! { s::trap() }
wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" });

struct App;
impl Guest for App {
    fn describe() -> Result<AppDescriptor, AppError> {
        Ok(s::ui_descriptor(APP_ID, VERSION, DISPLAY_NAME,
            ENTRYPOINT_ID, ENTRYPOINT_LABEL, INITIAL_ROUTE))
    }
    fn get_settings_schema() -> Result<Option<SettingsSchema>, AppError> { Ok(None) }
    fn validate_settings(_: CallContext, value: SettingsSnapshot)
        -> Result<SettingsValidation, AppError> { s::no_settings(&value) }
    fn handle_event(context: CallContext, event: AppEvent)
        -> Result<EventOutput, AppError> { app::handle_event(context, event) }
    fn health(_: CallContext, _: HealthRequest) -> Result<HealthReport, AppError> {
        Ok(s::healthy("ready"))
    }
    fn migrate(_: CallContext, request: MigrationRequest)
        -> Result<MigrationResult, AppError> { s::unchanged_migration(&request, 1) }
}
export!(App);
```

Define uppercase constants from the EXACT package intent. Settings defaults above
are only for apps with no settings schema; migration supports only state schema 1.
The application implementation belongs in `app.rs` and may use more app-owned modules.
Do not add another allocator, panic handler, memory primitive or import-retention
export. Never call `retain_ui_world_imports` from app logic. No new dependencies/std.

In EACH Rust module using them, import `alloc::string::{String, ToString}`,
`alloc::vec::Vec`, `alloc::vec`, and/or `alloc::format`. Imports do not propagate into
child modules. For `format!("{:?}", value)`, the app-owned type must derive Debug;
prefer explicit user-facing labels. Do not combine `guest::*` with `ui::*`,
`settings::*`, or `common::*`: generated aliases collide. Use qualified names or
explicit imports.

## Exact namespaces and generic KV

The support reexports `s::{guest, common, ui, settings, kv, clock, host_info, log}`.
Use **s::kv::get** and **s::kv::transact**. There is no `ui::kv_get`, `ui::kv_transact`,
`s::kv_get` or `s::kv_transact`; `ui` contains semantic UI types only.

This generic app-scoped byte-storage example may live in an app-owned module:

```rust
use alloc::{string::ToString, vec, vec::Vec};
use crate::vibapp_support::{common::{AppError, CallContext, HostError}, kv};

fn host_error(error: HostError) -> AppError {
    AppError { code: error.code, message: error.message, retryable: error.retryable }
}

pub fn load(key: &str) -> Result<Option<kv::Entry>, AppError> {
    kv::get(key).map_err(host_error)
}

pub fn save(context: &CallContext, key: &str, bytes: Vec<u8>,
            observed_revision: Option<u64>) -> Result<kv::TransactionResult, AppError> {
    kv::transact(&kv::TransactionRequest {
        operations: vec![kv::WriteOperation::Put(kv::PutOperation {
            key: key.to_string(), value: bytes, expected_revision: observed_revision,
        })],
        idempotency_key: context.idempotency_key.clone(),
    }).map_err(host_error)
}
```

`load` returns `Ok(None)` for an absent key: initialize your app default state only
then. For an existing Entry, decode `entry.value` with strict size/type checks and
save using `Some(entry.revision)`, not zero or the settings revision. An absent key
uses `None`; **expected_revision=None skips the revision check, not an atomic
create-if-absent assertion**. Keep related writes in ONE transaction per event using
the supplied context idempotency key. Reusing that key with different payloads fails.
Propagate host errors; never pretend a failed save succeeded. These are app-scoped
host bytes, not filesystem paths or arbitrary cross-app storage.

## Event handling and refresh persistence

```rust
pub fn handle_event(context: s::common::CallContext, event: s::guest::AppEvent)
    -> Result<s::guest::EventOutput, s::common::AppError>
```

Inside the app handler, load/validate app state, then use:

```rust
let Some(target) = s::surface_target(&event) else { return Ok(s::empty_output()); };
let changes = s::changed_fields(&event);
let action = s::action_id(&event);
// Apply partial field changes to current state, implement requested action,
// persist app state when changed, then construct your complete semantic view.
Ok(s::output(target.update(your_view)))
```

`surface_target` preserves the event's host-issued session/surface/route and covers
Launch/Open/Restore/Action. Close/Focus/Management/Service/Scheduled return None;
handle needed cleanup/management separately. Closing is not disable.
`changed_fields` returns Action.fields or Restore.safe_fields. Each element is
`FieldChange { field, value }`, not a tuple or key/value pair. The button record is
`ActionEvent.action`; helper `action_id` returns its `Option<&str>`.

IMPORTANT: Open can repeat during periodic host refresh. Do not unconditionally
reset results, validation errors, status, selections or other displayed app state on
Open. Reconstruct the complete view from saved state or deterministically recompute
derived values from it. Missing field changes mean “unchanged”, not empty. An empty
KV value or malformed serialization is not the same as a missing key. Read-only
refresh should not rewrite state unnecessarily.

`field_value(fields, key) -> Result<Option<&ui::FieldValue>, common::AppError>` rejects
duplicate keys. `text_value` and `choice_value` return `Result<Option<&str>, AppError>`
and accept ONLY Text or Choice respectively. Use `field_value` plus a typed match for
Integer/Decimal/Boolean/etc. Validate user bounds/choice membership; never interpret
SecretHandle as text.

## UI constructors

```rust
list(id: &str, parent: Option<&str>, label: Option<&str>) -> ui::Node
text(id: &str, parent: Option<&str>, value: &str, style: ui::TextStyle) -> ui::Node
button(id: &str, parent: Option<&str>, label: &str, action: &str,
       style: ui::ButtonStyle, disabled: bool) -> ui::Node
field(id: &str, parent: Option<&str>, value: ui::FieldNode) -> ui::Node
text_field(id: &str, parent: Option<&str>, key: &str, label: &str, value: &str) -> ui::Node
choice(value: &str, label: &str) -> ui::ChoiceOption
view(title: &str, root: &str, nodes: Vec<ui::Node>) -> ui::View
```

Start with `s::list("root", None, None)` and unique child IDs with `Some("root")`.
`text_field` is non-sensitive/non-required Text. Other FieldNode constructors need
ALL fields: `field`, `label`, `kind`, `value`, `required`, `sensitive`, `choices`,
`validation_message`. Progress/Confirmation and other exact types remain in WIT;
use `s::node(id, parent, ui::NodeKind::...)`. No raw HTML, invented variants or timers.

## Reclaiming allocator scope

The support uses a checked, address-ordered coalescing free-list over Wasm linear
memory starting at `__heap_base`, capped at 16 MiB total. Drop/dealloc returns memory
to the list; canonical realloc-to-zero releases its allocation. Realloc preserves
the old prefix and, on failure, leaves the old allocation live. Do not reset the heap.
Mapped Wasm pages do not shrink; freed blocks become available for reuse.
First-fit operations are linear in the free-list length and still subject to host
fuel/time limits. This prototype does not claim thread/reentrancy testing, universal
fragmentation resistance, independent security acceptance, or a publication-ready SDK.
