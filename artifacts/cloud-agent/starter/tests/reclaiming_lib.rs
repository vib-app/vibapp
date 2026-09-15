#![no_std]
extern crate alloc;
mod vibapp_support;
use alloc::{format, string::ToString, vec, vec::Vec};
use core::{
    alloc::{GlobalAlloc, Layout},
    sync::atomic::{AtomicUsize, Ordering},
};
use s::guest::*;
use vibapp_support as s;
#[global_allocator]
static ALLOCATOR: s::Allocator = s::Allocator;
#[panic_handler]
fn panic(_: &core::panic::PanicInfo<'_>) -> ! {
    s::trap()
}
wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" });

// Synthetic infrastructure only. No product ID, model output, or application logic.
struct ReclaimingSmoke;
static CYCLES: AtomicUsize = AtomicUsize::new(0);
fn checked_layout(size: usize, alignment: usize) -> Layout {
    Layout::from_size_align(size, alignment).unwrap()
}
fn edge_checks() {
    let initial = s::heap_stats();
    unsafe {
        let mut blocks = [core::ptr::null_mut(); 1024];
        for (index, pointer) in blocks.iter_mut().enumerate() {
            let layout = checked_layout(17 + index % 47, 1 << (index % 13));
            *pointer = ALLOCATOR.alloc(layout);
            assert!(!pointer.is_null());
            assert_eq!(*pointer as usize % layout.align(), 0);
            (*pointer).write((index % 251) as u8);
        }
        for index in (0..1024).step_by(2).rev() {
            assert_eq!(blocks[index].read(), (index % 251) as u8);
            ALLOCATOR.dealloc(
                blocks[index],
                checked_layout(17 + index % 47, 1 << (index % 13)),
            );
        }
        for index in (1..1024).step_by(2) {
            assert_eq!(blocks[index].read(), (index % 251) as u8);
            ALLOCATOR.dealloc(
                blocks[index],
                checked_layout(17 + index % 47, 1 << (index % 13)),
            );
        }
        let after = s::heap_stats();
        assert_eq!(after.2 - after.0, initial.2 - initial.0);
        assert_eq!(after.1, initial.1);

        let survivor_layout = checked_layout(32, 256);
        let survivor = ALLOCATOR.alloc(survivor_layout);
        assert!(!survivor.is_null());
        survivor.write(203);
        let mut large = [core::ptr::null_mut(); 512];
        let large_layout = checked_layout(65536, 16);
        let mut count = 0;
        loop {
            let pointer = ALLOCATOR.alloc(large_layout);
            if pointer.is_null() {
                break;
            }
            assert!(count < large.len());
            pointer.write((count % 251) as u8);
            large[count] = pointer;
            count += 1;
        }
        assert!(count > 100);
        assert_eq!(
            core::arch::wasm32::memory_size(0) * 65536,
            s::MAX_MEMORY_BYTES
        );
        let failed = ALLOCATOR.realloc(survivor, survivor_layout, s::MAX_MEMORY_BYTES);
        assert!(failed.is_null());
        assert_eq!(survivor.read(), 203);
        for index in (0..count).rev() {
            assert_eq!(large[index].read(), (index % 251) as u8);
            ALLOCATOR.dealloc(large[index], large_layout);
        }
        ALLOCATOR.dealloc(survivor, survivor_layout);
        let after = s::heap_stats();
        assert_eq!(after.2 - after.0, initial.2 - initial.0);
        assert_eq!(after.1, initial.1);
        let almost_whole_layout = checked_layout(after.0 - 8192, 4096);
        let almost_whole = ALLOCATOR.alloc(almost_whole_layout);
        assert!(!almost_whole.is_null());
        ALLOCATOR.dealloc(almost_whole, almost_whole_layout);

        let first = s::cabi_realloc(core::ptr::null_mut(), 0, 64, 7);
        for i in 0..7 {
            first.add(i).write((i + 31) as u8);
        }
        let next = s::cabi_realloc(first, 7, 64, 4097);
        for i in 0..7 {
            assert_eq!(next.add(i).read(), (i + 31) as u8);
        }
        let smaller = s::cabi_realloc(next, 4097, 64, 3);
        assert_eq!(smaller.read(), 31);
        assert_eq!(s::cabi_realloc(smaller, 3, 64, 0) as usize, 64);
        assert_eq!(
            s::cabi_realloc(core::ptr::null_mut(), 0, 128, 0) as usize,
            128
        );
        assert_eq!(s::memcmp(core::ptr::null(), core::ptr::null(), 0), 0);
    }
    {
        let mut values = Vec::with_capacity(1);
        for index in 0..4096 {
            values.push(index);
        }
        assert_eq!(values[0], 0);
        assert_eq!(values[4095], 4095);
        values.shrink_to_fit();
        let mut string = "prefix".to_string();
        for _ in 0..1024 {
            string.push('x');
        }
        assert!(string.starts_with("prefix"));
    }
    let after = s::heap_stats();
    assert_eq!(after.2 - after.0, initial.2 - initial.0);
    assert_eq!(after.1, initial.1);
}
fn cycle_batch() -> usize {
    let before = s::heap_stats();
    for index in 0..1024 {
        let size = 31 + index % 113;
        let alignment = 1 << (index % 10);
        let layout = checked_layout(size, alignment);
        unsafe {
            let pointer = ALLOCATOR.alloc(layout);
            assert!(!pointer.is_null());
            assert_eq!(pointer as usize % alignment, 0);
            pointer.write(137);
            pointer.add(size - 1).write(211);
            let grown_size = size * 2 + 7;
            let grown = ALLOCATOR.realloc(pointer, layout, grown_size);
            assert!(!grown.is_null());
            assert_eq!(grown as usize % alignment, 0);
            assert_eq!(grown.read(), 137);
            assert_eq!(grown.add(size - 1).read(), 211);
            let shrunk = ALLOCATOR.realloc(grown, checked_layout(grown_size, alignment), 7);
            assert!(!shrunk.is_null());
            assert_eq!(shrunk.read(), 137);
            ALLOCATOR.dealloc(shrunk, checked_layout(7, alignment));
            if index % 16 == 0 {
                let canonical = s::cabi_realloc(core::ptr::null_mut(), 0, 4, 41);
                canonical.write(73);
                let canonical = s::cabi_realloc(canonical, 41, 4, 91);
                assert_eq!(canonical.read(), 73);
                s::cabi_realloc(canonical, 91, 4, 0);
            }
        }
    }
    assert_eq!(s::heap_stats(), before);
    CYCLES.fetch_add(1024, Ordering::Relaxed) + 1024
}
impl Guest for ReclaimingSmoke {
    fn describe() -> Result<AppDescriptor, AppError> {
        edge_checks();
        Ok(s::ui_descriptor(
            "example.vibapp.reclaiming-smoke",
            "0.1.0",
            "Reclaiming Smoke",
            "main",
            "Reclaiming Smoke",
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
        let text = "synthetic allocator lifetime".repeat(128);
        let view = s::view(
            "Reclaiming Smoke",
            "root",
            vec![
                s::list("root", None, None),
                s::text("message", Some("root"), &text, s::ui::TextStyle::Body),
            ],
        );
        Ok(s::output(target.update(view)))
    }
    fn health(_: CallContext, _: HealthRequest) -> Result<HealthReport, AppError> {
        let count = cycle_batch();
        let (free, blocks, mapped) = s::heap_stats();
        Ok(s::healthy(&format!(
            "cycles={count};free={free};blocks={blocks};mapped={mapped}"
        )))
    }
    fn migrate(_: CallContext, request: MigrationRequest) -> Result<MigrationResult, AppError> {
        s::unchanged_migration(&request, 1)
    }
}
export!(ReclaimingSmoke);
