//! Win32 host primitives. Handles never cross the component boundary.
use std::{ffi::c_void, fs::{File, OpenOptions}, io, path::Path};
use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};

type Handle = *mut c_void;
#[repr(C)]
#[derive(Default)]
struct BasicLimits { process_time: i64, job_time: i64, flags: u32, minimum_working_set: usize, maximum_working_set: usize, active_processes: u32, affinity: usize, priority: u32, scheduling: u32 }
#[repr(C)]
#[derive(Default)]
struct IoCounters { read_ops: u64, write_ops: u64, other_ops: u64, read_bytes: u64, write_bytes: u64, other_bytes: u64 }
#[repr(C)]
#[derive(Default)]
struct ExtendedLimits { basic: BasicLimits, io: IoCounters, process_memory: usize, job_memory: usize, peak_process: usize, peak_job: usize }
#[repr(C)]
#[derive(Default, PartialEq)]
struct FileId { volume: u64, id: [u8; 16] }
#[repr(C)]
#[derive(Default)]
struct SystemTime { year: u16, month: u16, weekday: u16, day: u16, hour: u16, minute: u16, second: u16, millis: u16 }

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
    fn SetInformationJobObject(job: Handle, kind: i32, info: *const c_void, size: u32) -> i32;
    fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
    fn GetFileInformationByHandleEx(file: Handle, kind: i32, info: *mut c_void, size: u32) -> i32;
    fn FileTimeToSystemTime(filetime: *const u64, time: *mut SystemTime) -> i32;
    fn SystemTimeToFileTime(time: *const SystemTime, filetime: *mut u64) -> i32;
    fn SystemTimeToTzSpecificLocalTime(zone: *const c_void, utc: *const SystemTime, local: *mut SystemTime) -> i32;
}

pub fn process_limits(memory: u64) -> Result<(), String> {
    let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if job.is_null() { return Err(io::Error::last_os_error().to_string()); }
    let mut limits = ExtendedLimits::default();
    limits.basic.flags = 0x100 | 0x8 | 0x2000; // process memory, active process, kill-on-close
    limits.basic.active_processes = 1;
    limits.process_memory = usize::try_from(memory).map_err(|_| "memory limit overflow")?;
    let accepted = unsafe {
        SetInformationJobObject(job, 9, &limits as *const _ as _, std::mem::size_of::<ExtendedLimits>() as u32) != 0
            && AssignProcessToJobObject(job, GetCurrentProcess()) != 0
    };
    if !accepted {
        let error = io::Error::last_os_error();
        unsafe { CloseHandle(job); }
        return Err(format!("cannot enforce Windows runtime Job Object limits: {error}"));
    }
    // Keep the sole handle alive for the process lifetime. Closing it early
    // would correctly kill this process because KILL_ON_JOB_CLOSE is enabled.
    Ok(())
}

pub fn open_directory(path: &Path) -> io::Result<File> {
    use std::os::windows::fs::MetadataExt;
    let file = OpenOptions::new().read(true).custom_flags(0x02000000 | 0x00200000).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "state directory is a reparse point"));
    }
    Ok(file)
}

pub fn same_directory(first: &File, second: &File) -> Result<bool, String> {
    fn id(file: &File) -> Result<FileId, String> {
        let mut info = FileId::default();
        if unsafe { GetFileInformationByHandleEx(file.as_raw_handle(), 18, &mut info as *mut _ as _, std::mem::size_of::<FileId>() as u32) } == 0 {
            return Err(io::Error::last_os_error().to_string());
        }
        Ok(info)
    }
    Ok(id(first)? == id(second)?)
}

pub fn clock_context(unix_seconds: i64) -> Result<(String, i32), String> {
    let raw = unix_seconds.checked_add(11_644_473_600).and_then(|n| n.checked_mul(10_000_000)).and_then(|n| u64::try_from(n).ok()).ok_or("local clock overflow")?;
    let mut utc = SystemTime::default();
    let mut local = SystemTime::default();
    let mut shifted = 0u64;
    if unsafe { FileTimeToSystemTime(&raw, &mut utc) == 0 || SystemTimeToTzSpecificLocalTime(std::ptr::null(), &utc, &mut local) == 0 || SystemTimeToFileTime(&local, &mut shifted) == 0 } {
        return Err(io::Error::last_os_error().to_string());
    }
    let offset = i32::try_from((i128::from(shifted) - i128::from(raw)) / 10_000_000).map_err(|_| "local UTC offset overflow")?;
    let sign = if offset < 0 { '-' } else { '+' };
    let absolute = offset.unsigned_abs();
    Ok((format!("UTC{sign}{:02}:{:02}", absolute / 3600, absolute % 3600 / 60), offset))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn directory_identity_survives_rename_but_rejects_another_directory() {
        let unique = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("vibapp-file-id-{}-{unique}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(root.join("staged")).unwrap();
        std::fs::create_dir(root.join("foreign")).unwrap();
        let first = open_directory(&root.join("staged")).unwrap();
        std::fs::rename(root.join("staged"), root.join("active")).unwrap();
        let active = open_directory(&root.join("active")).unwrap();
        let foreign = open_directory(&root.join("foreign")).unwrap();
        assert!(same_directory(&first, &active).unwrap());
        assert!(!same_directory(&first, &foreign).unwrap());
        drop((first, active, foreign));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn local_clock_returns_a_bounded_actual_offset() {
        let (zone, offset) = clock_context(1_700_000_000).unwrap();
        assert!(zone.starts_with("UTC"));
        assert!((-86_400..=86_400).contains(&offset));
        assert!(clock_context(i64::MAX).is_err());
    }
}
