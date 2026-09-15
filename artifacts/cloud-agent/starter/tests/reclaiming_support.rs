//! PROPOSAL ONLY: reclaiming allocator prefix; frozen support remains unchanged.
//! The checker appends the unchanged semantic helpers from frozen support.
#![allow(dead_code)]

pub use crate::exports::vibapp::experimental_v0::guest;
pub use crate::vibapp::experimental_v0::{clock, common, host_info, kv, log, settings, ui};
use alloc::{string::ToString, vec, vec::Vec};
use core::{
    alloc::{GlobalAlloc, Layout},
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};

pub const MAX_MEMORY_BYTES: usize = 16 * 1024 * 1024;
const PAGE: usize = 65_536;
const GRANULE: usize = 16;
const HEADER: usize = core::mem::size_of::<Allocation>();
static LOCK: AtomicBool = AtomicBool::new(false);
static START: AtomicUsize = AtomicUsize::new(0);
static END: AtomicUsize = AtomicUsize::new(0);
static HEAD: AtomicUsize = AtomicUsize::new(0);
unsafe extern "C" {
    static __heap_base: u8;
}

#[repr(C)]
#[derive(Clone, Copy)]
struct FreeBlock {
    size: usize,
    next: usize,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct Allocation {
    start: usize,
    size: usize,
}

struct Guard;
impl Drop for Guard {
    fn drop(&mut self) {
        LOCK.store(false, Ordering::Release);
    }
}
fn lock() -> Guard {
    while LOCK
        .compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed)
        .is_err()
    {
        core::hint::spin_loop();
    }
    Guard
}
pub fn trap() -> ! {
    core::arch::wasm32::unreachable()
}
fn align_up(value: usize, alignment: usize) -> Option<usize> {
    value
        .checked_add(alignment - 1)
        .map(|v| v & !(alignment - 1))
}
fn checked_end(start: usize, size: usize) -> usize {
    match start.checked_add(size) {
        Some(end) => end,
        None => trap(),
    }
}

/// All list/header access is under LOCK. Free-list metadata lives only in free blocks.
unsafe fn read_free(address: usize) -> FreeBlock {
    if address < START.load(Ordering::Relaxed)
        || address % GRANULE != 0
        || checked_end(address, core::mem::size_of::<FreeBlock>()) > END.load(Ordering::Relaxed)
    {
        trap();
    }
    let node = unsafe { (address as *const FreeBlock).read() };
    let end = checked_end(address, node.size);
    if node.size < GRANULE
        || node.size % GRANULE != 0
        || end > END.load(Ordering::Relaxed)
        || (node.next != 0 && node.next < end)
    {
        trap();
    }
    node
}
unsafe fn replace_link(previous: usize, next: usize) {
    if previous == 0 {
        HEAD.store(next, Ordering::Relaxed);
    } else {
        unsafe {
            (*(previous as *mut FreeBlock)).next = next;
        }
    }
}

/// Insert in address order, reject overlap, then coalesce both immediate neighbors.
unsafe fn release(start: usize, size: usize) {
    let end = checked_end(start, size);
    if start < START.load(Ordering::Relaxed)
        || start % GRANULE != 0
        || size < GRANULE
        || size % GRANULE != 0
        || end > END.load(Ordering::Relaxed)
    {
        trap();
    }
    let mut previous = 0;
    let mut current = HEAD.load(Ordering::Relaxed);
    while current != 0 && current < start {
        let node = unsafe { read_free(current) };
        previous = current;
        current = node.next;
    }
    if current != 0 && end > current {
        trap();
    }
    let mut merged_start = start;
    let mut merged_size = size;
    if previous != 0 {
        let node = unsafe { read_free(previous) };
        let previous_end = checked_end(previous, node.size);
        if previous_end > start {
            trap();
        }
        if previous_end == start {
            merged_start = previous;
            merged_size = checked_end(node.size, size);
        }
    }
    let mut next = current;
    if current != 0 && checked_end(merged_start, merged_size) == current {
        let node = unsafe { read_free(current) };
        merged_size = checked_end(merged_size, node.size);
        next = node.next;
    }
    unsafe {
        (merged_start as *mut FreeBlock).write(FreeBlock {
            size: merged_size,
            next,
        });
    }
    if merged_start != previous {
        unsafe {
            replace_link(previous, merged_start);
        }
    }
}

unsafe fn initialize() -> bool {
    if START.load(Ordering::Relaxed) != 0 {
        return true;
    }
    let Some(start) = align_up(core::ptr::addr_of!(__heap_base) as usize, GRANULE) else {
        return false;
    };
    let available = (core::arch::wasm32::memory_size(0) * PAGE).min(MAX_MEMORY_BYTES);
    if start == 0 || start > available {
        return false;
    }
    START.store(start, Ordering::Relaxed);
    END.store(available, Ordering::Relaxed);
    if available - start >= GRANULE {
        unsafe {
            release(start, available - start);
        }
    }
    true
}

unsafe fn extend(minimum: usize) -> bool {
    let previous_end = END.load(Ordering::Relaxed);
    if previous_end == MAX_MEMORY_BYTES {
        return false;
    }
    let available = (core::arch::wasm32::memory_size(0) * PAGE).min(MAX_MEMORY_BYTES);
    let next_end = if available > previous_end {
        available
    } else {
        let Some(growth) = align_up(minimum.max(PAGE), PAGE) else {
            return false;
        };
        let growth = growth.min(MAX_MEMORY_BYTES - previous_end);
        if growth == 0 || core::arch::wasm32::memory_grow(0, growth / PAGE) == usize::MAX {
            return false;
        }
        previous_end + growth
    };
    END.store(next_end, Ordering::Relaxed);
    unsafe {
        release(previous_end, next_end - previous_end);
    }
    true
}

unsafe fn allocate(layout: Layout) -> *mut u8 {
    let alignment = layout.align().max(GRANULE);
    let Some(minimum) = layout
        .size()
        .max(1)
        .checked_add(HEADER)
        .and_then(|n| n.checked_add(alignment - 1))
    else {
        return core::ptr::null_mut();
    };
    if layout.size() > MAX_MEMORY_BYTES || !unsafe { initialize() } {
        return core::ptr::null_mut();
    }
    loop {
        let mut previous = 0;
        let mut current = HEAD.load(Ordering::Relaxed);
        while current != 0 {
            let node = unsafe { read_free(current) };
            let block_end = checked_end(current, node.size);
            let payload = align_up(checked_end(current, HEADER), alignment);
            let used_end = payload
                .and_then(|p| p.checked_add(layout.size().max(1)))
                .and_then(|p| align_up(p, GRANULE));
            if let (Some(payload), Some(mut used_end)) = (payload, used_end) {
                if used_end <= block_end {
                    let replacement = if block_end - used_end >= GRANULE {
                        unsafe {
                            (used_end as *mut FreeBlock).write(FreeBlock {
                                size: block_end - used_end,
                                next: node.next,
                            });
                        }
                        used_end
                    } else {
                        used_end = block_end;
                        node.next
                    };
                    unsafe {
                        replace_link(previous, replacement);
                    }
                    unsafe {
                        ((payload - HEADER) as *mut Allocation).write(Allocation {
                            start: current,
                            size: used_end - current,
                        });
                    }
                    return payload as *mut u8;
                }
            }
            previous = current;
            current = node.next;
        }
        if !unsafe { extend(minimum) } {
            return core::ptr::null_mut();
        }
    }
}

unsafe fn allocation(pointer: *mut u8, layout: Layout) -> Allocation {
    let address = pointer as usize;
    if address < checked_end(START.load(Ordering::Relaxed), HEADER)
        || address % layout.align().max(GRANULE) != 0
        || address > END.load(Ordering::Relaxed)
    {
        trap();
    }
    let block = unsafe { ((address - HEADER) as *const Allocation).read() };
    if block.start < START.load(Ordering::Relaxed)
        || block.start % GRANULE != 0
        || checked_end(block.start, HEADER) > address
        || block.size < GRANULE
        || block.size % GRANULE != 0
        || checked_end(address, layout.size().max(1)) > checked_end(block.start, block.size)
        || checked_end(block.start, block.size) > END.load(Ordering::Relaxed)
    {
        trap();
    }
    block
}

pub struct Allocator;
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let _guard = lock();
        unsafe { allocate(layout) }
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        let _guard = lock();
        let block = unsafe { allocation(pointer, layout) };
        unsafe {
            release(block.start, block.size);
        }
    }
    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let Ok(next_layout) = Layout::from_size_align(new_size.max(1), layout.align()) else {
            return core::ptr::null_mut();
        };
        let _guard = lock();
        let mut block = unsafe { allocation(pointer, layout) };
        let address = pointer as usize;
        let Some(new_end) = address
            .checked_add(new_size.max(1))
            .and_then(|n| align_up(n, GRANULE))
        else {
            return core::ptr::null_mut();
        };
        let old_end = checked_end(block.start, block.size);
        if new_end <= old_end {
            if old_end - new_end >= GRANULE {
                block.size = new_end - block.start;
                unsafe {
                    ((address - HEADER) as *mut Allocation).write(block);
                }
                unsafe {
                    release(new_end, old_end - new_end);
                }
            }
            return pointer;
        }
        let next = unsafe { allocate(next_layout) };
        if !next.is_null() {
            unsafe {
                core::ptr::copy_nonoverlapping(pointer, next, layout.size().min(new_size));
            }
            unsafe {
                release(block.start, block.size);
            }
        }
        next
    }
}

/// Uses the same allocator as Rust containers; freeing returns storage to the list.
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
        if old_size != 0 {
            unsafe {
                Allocator.dealloc(old, layout);
            }
        }
        return alignment as *mut u8;
    }
    let pointer = if old_size == 0 {
        let Ok(next_layout) = Layout::from_size_align(new_size, alignment) else {
            trap()
        };
        unsafe { Allocator.alloc(next_layout) }
    } else {
        unsafe { Allocator.realloc(old, layout, new_size) }
    };
    if pointer.is_null() {
        trap();
    }
    pointer
}

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

/// Infrastructure diagnostics only: (free bytes, free blocks, mapped heap bytes).
pub fn heap_stats() -> (usize, usize, usize) {
    let _guard = lock();
    if !unsafe { initialize() } {
        return (0, 0, 0);
    }
    let mut current = HEAD.load(Ordering::Relaxed);
    let (mut bytes, mut count) = (0, 0);
    while current != 0 {
        let block = unsafe { read_free(current) };
        bytes += block.size;
        count += 1;
        current = block.next;
    }
    (
        bytes,
        count,
        END.load(Ordering::Relaxed) - START.load(Ordering::Relaxed),
    )
}
