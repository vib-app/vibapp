#![no_std]

//! Dry-run-only source fixture. A separate Builder and verifier must reject or accept it.

extern crate alloc;

use core::alloc::{GlobalAlloc, Layout};
use core::panic::PanicInfo;
use core::sync::atomic::{AtomicUsize, Ordering};

struct DryRunAllocator;
static NEXT: AtomicUsize = AtomicUsize::new(0);

unsafe extern "C" {
    static __heap_base: u8;
}

unsafe impl GlobalAlloc for DryRunAllocator {
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
static ALLOCATOR: DryRunAllocator = DryRunAllocator;

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

wit_bindgen::generate!({
    path: "wit",
    world: "ui-only-reference",
});

#[allow(dead_code)]
fn dry_run_action_button() -> vibapp::experimental_v0::ui::NodeKind {
    vibapp::experimental_v0::ui::NodeKind::Button(
        vibapp::experimental_v0::ui::ButtonNode {
            label: alloc::string::String::from("Prepare request"),
            action: alloc::string::String::from("prepare"),
            style: vibapp::experimental_v0::ui::ButtonStyle::Primary,
            disabled: false,
        },
    )
}

#[allow(dead_code)]
fn dry_run_handles_action(
    event: exports::vibapp::experimental_v0::guest::LauncherEvent,
) -> bool {
    matches!(
        event,
        exports::vibapp::experimental_v0::guest::LauncherEvent::Action(_)
    )
}

pub const PRODUCT_INTENT: &str = "private meeting coordination launcher surface";
