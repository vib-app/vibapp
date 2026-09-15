#![no_std]
#![allow(unsafe_code)]

extern crate alloc;
extern crate self as wit_bindgen;

pub mod rt {
    use alloc::alloc::{alloc, dealloc, handle_alloc_error, realloc, Layout};
    use core::ptr::{self, NonNull};

    pub fn maybe_link_cabi_realloc() {}
    pub fn run_ctors_once() {}

    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn cabi_realloc(
        old_pointer: *mut u8,
        old_size: usize,
        align: usize,
        new_size: usize,
    ) -> *mut u8 {
        if old_size == 0 {
            if new_size == 0 { return align as *mut u8; }
            let layout = unsafe { Layout::from_size_align_unchecked(new_size, align) };
            let pointer = unsafe { alloc(layout) };
            if pointer.is_null() { handle_alloc_error(layout); }
            return pointer;
        }
        if new_size == 0 {
            let layout = unsafe { Layout::from_size_align_unchecked(old_size, align) };
            unsafe { dealloc(old_pointer, layout) };
            return ptr::null_mut();
        }
        let layout = unsafe { Layout::from_size_align_unchecked(old_size, align) };
        let pointer = unsafe { realloc(old_pointer, layout, new_size) };
        if pointer.is_null() { handle_alloc_error(layout); }
        pointer
    }

    pub struct Cleanup { ptr: NonNull<u8>, layout: Layout }
    impl Cleanup {
        pub fn new(layout: Layout) -> (*mut u8, Option<Self>) {
            if layout.size() == 0 { return (ptr::null_mut(), None); }
            let pointer = unsafe { alloc(layout) };
            let pointer = NonNull::new(pointer).unwrap_or_else(|| handle_alloc_error(layout));
            (pointer.as_ptr(), Some(Self { ptr: pointer, layout }))
        }
        pub fn forget(self) { core::mem::forget(self); }
    }
    impl Drop for Cleanup {
        fn drop(&mut self) { unsafe { dealloc(self.ptr.as_ptr(), self.layout) }; }
    }
}

use core::alloc::{GlobalAlloc, Layout};
use core::ptr;

struct BoundedBumpAllocator;
static mut NEXT_ALLOCATION: usize = 0;
unsafe extern "C" { static __heap_base: u8; }

unsafe impl GlobalAlloc for BoundedBumpAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let heap_base = ptr::addr_of!(__heap_base) as usize;
        let current = unsafe { if NEXT_ALLOCATION == 0 { heap_base } else { NEXT_ALLOCATION } };
        let aligned = match current.checked_add(layout.align() - 1) {
            Some(value) => value & !(layout.align() - 1), None => return ptr::null_mut(),
        };
        let end = match aligned.checked_add(layout.size()) {
            Some(value) => value, None => return ptr::null_mut(),
        };
        let current_bytes = core::arch::wasm32::memory_size(0) * 65_536;
        if end > current_bytes {
            let pages = (end - current_bytes + 65_535) / 65_536;
            if core::arch::wasm32::memory_grow(0, pages) == usize::MAX { return ptr::null_mut(); }
        }
        unsafe { NEXT_ALLOCATION = end };
        aligned as *mut u8
    }
    unsafe fn dealloc(&self, _pointer: *mut u8, _layout: Layout) {}
}

#[global_allocator]
static GLOBAL_ALLOCATOR: BoundedBumpAllocator = BoundedBumpAllocator;

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo<'_>) -> ! { core::arch::wasm32::unreachable() }

include!("ui_only_reference.rs");

use alloc::borrow::ToOwned;
use alloc::string::String;
use alloc::vec;
use alloc::vec::Vec;
use crate::exports::vibapp::experimental_v0::guest as guest;
use crate::vibapp::experimental_v0::common::{AppError, AppKind, EntrypointKind, ErrorCode};
use crate::vibapp::experimental_v0::settings;
use crate::vibapp::experimental_v0::ui;

const APP_ID: &str = __APP_ID__;
const APP_VERSION: &str = __APP_VERSION__;
const DISPLAY_NAME: &str = __DISPLAY_NAME__;
const HEADLINE: &str = __HEADLINE__;
const BODY: &str = __BODY__;

#[unsafe(export_name = "__vibapp_force_declared_imports")]
pub extern "C" fn force_declared_imports() {
    use crate::vibapp::experimental_v0::{clock, host_info, kv, log};
    let _ = clock::monotonic_now();
    let _ = kv::get("");
    let _ = log::write(log::Level::Debug, "", &[], "local-codeagent-link");
    let _ = host_info::describe_host();
    let _ = settings::current();
}

struct GeneratedApp;

fn app_error(message: &str) -> AppError {
    AppError { code: ErrorCode::InvalidArgument, message: message.to_owned(), retryable: false }
}

fn view(session: String, surface: String, route: String) -> ui::SurfaceUpdate {
    let root = "root".to_owned();
    ui::SurfaceUpdate {
        session, surface, route,
        view: ui::View {
            title: DISPLAY_NAME.to_owned(),
            root: root.clone(),
            nodes: vec![
                ui::Node { id: root.clone(), parent: None, kind: ui::NodeKind::ListContainer(ui::ListNode { label: None }) },
                ui::Node { id: "headline".to_owned(), parent: Some(root.clone()), kind: ui::NodeKind::Text(ui::TextNode { text: HEADLINE.to_owned(), style: ui::TextStyle::Title }) },
                ui::Node { id: "body".to_owned(), parent: Some(root), kind: ui::NodeKind::Text(ui::TextNode { text: BODY.to_owned(), style: ui::TextStyle::Body }) },
            ],
        },
    }
}

impl guest::Guest for GeneratedApp {
    fn describe() -> Result<guest::AppDescriptor, AppError> {
        Ok(guest::AppDescriptor {
            id: APP_ID.to_owned(), version: APP_VERSION.to_owned(), kind: AppKind::Ui,
            display_name: DISPLAY_NAME.to_owned(),
            entrypoints: vec![guest::EntrypointDescriptor {
                id: "main".to_owned(), kind: EntrypointKind::LauncherUi,
                label: DISPLAY_NAME.to_owned(), initial_route: Some("home".to_owned()),
            }],
        })
    }

    fn get_settings_schema() -> Result<Option<guest::SettingsSchema>, AppError> { Ok(None) }

    fn validate_settings(
        _context: guest::CallContext,
        proposed: guest::SettingsSnapshot,
    ) -> Result<guest::SettingsValidation, AppError> {
        if !proposed.values.is_empty() { return Err(app_error("this UI has no settings")); }
        Ok(settings::SettingsValidation { accepted: true, field_errors: Vec::new(), service_restart_required: false })
    }

    fn handle_event(
        _context: guest::CallContext,
        event: guest::AppEvent,
    ) -> Result<guest::EventOutput, AppError> {
        let surface = match event {
            guest::AppEvent::Launcher(guest::LauncherEvent::Launch(event)) => Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Open(event)) => Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Restore(event)) => Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Action(event)) => Some((event.session, event.surface, event.route)),
            guest::AppEvent::Launcher(guest::LauncherEvent::Focus(event)) if event.focused => Some((event.session, event.surface, "home".to_owned())),
            _ => None,
        };
        Ok(guest::EventOutput { surfaces: surface.map(|(session, surface, route)| vec![view(session, surface, route)]).unwrap_or_default(), diagnostic: None })
    }

    fn health(
        _context: guest::CallContext,
        _request: guest::HealthRequest,
    ) -> Result<guest::HealthReport, AppError> {
        Ok(guest::HealthReport {
            status: guest::HealthStatus::Healthy,
            checks: vec![guest::HealthCheck { name: "local-generated-ui".to_owned(), status: guest::HealthStatus::Healthy, message: "bounded declarative UI is ready".to_owned() }],
        })
    }

    fn migrate(
        _context: guest::CallContext,
        request: guest::MigrationRequest,
    ) -> Result<guest::MigrationResult, AppError> {
        if request.from_schema != 1 || request.to_schema != 1 { return Err(app_error("only state schema 1 is supported")); }
        Ok(guest::MigrationResult { status: guest::MigrationStatus::Unchanged, target_state_revision: request.target_state_revision })
    }
}

export!(GeneratedApp);
