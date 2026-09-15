use serde_json::json;
use sha2::{Digest, Sha256};
use std::env;
#[cfg(any(target_os = "macos", target_os = "linux"))]
use std::ffi::CStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wasmtime::component::{Component, HasSelf, Linker};
use wasmtime::{Config, Engine, Store, StoreLimits, StoreLimitsBuilder};

mod bindings {
    wasmtime::component::bindgen!({
        world: "ui-only-reference",
        path: "../../../wit/experimental-v0/contract.wit",
        additional_derives: [serde::Serialize],
    });
}

use bindings::exports::vibapp::experimental_v0::guest;
use bindings::vibapp::experimental_v0::{
    clock, common, host_info, kv, log, scheduler, settings, ui,
};

const MAX_COMPONENT_BYTES: u64 = 16 * 1024 * 1024;
const MAX_LINEAR_MEMORY_BYTES: usize = 64 * 1024 * 1024;
const FUEL_BUDGET: u64 = 10_000_000;
include!(concat!(env!("OUT_DIR"), "/desktop_build_input_receipt.rs"));

struct RuntimeState {
    started: Instant,
    limits: StoreLimits,
}

impl common::Host for RuntimeState {}
impl ui::Host for RuntimeState {}

fn permission_denied(message: &str) -> common::HostError {
    common::HostError {
        code: common::ErrorCode::PermissionDenied,
        message: message.to_string(),
        retryable: false,
    }
}

fn canonical_utc_from_unix_seconds(unix_seconds: i64) -> Result<String, String> {
    const SECONDS_PER_DAY: i64 = 86_400;
    let days = unix_seconds.div_euclid(SECONDS_PER_DAY);
    let seconds_of_day = unix_seconds.rem_euclid(SECONDS_PER_DAY);

    // Proleptic Gregorian conversion from days since 1970-01-01. Keeping it in
    // this small pure helper makes the exact whole-second wire format testable
    // without depending on the host clock or locale.
    let shifted_days = days
        .checked_add(719_468)
        .ok_or_else(|| "UTC day count is outside the supported range".to_string())?;
    let era = shifted_days.div_euclid(146_097);
    let day_of_era = shifted_days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    if !(0..=9_999).contains(&year) {
        return Err("UTC year is outside the four-digit contract range".to_string());
    }
    let hour = seconds_of_day / 3_600;
    let minute = seconds_of_day % 3_600 / 60;
    let second = seconds_of_day % 60;
    let instant = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z");
    if instant.len() != 20 {
        return Err("UTC instant did not satisfy the canonical wire length".to_string());
    }
    Ok(instant)
}

fn current_unix_seconds() -> Result<i64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock is before the Unix epoch".to_string())?;
    i64::try_from(elapsed.as_secs())
        .map_err(|_| "system clock is outside the supported UTC range".to_string())
}

fn zoneinfo_name_from_path(path: &Path) -> Option<String> {
    let value = path.to_string_lossy();
    let (_, candidate) = value.rsplit_once("/zoneinfo/")?;
    let valid = !candidate.is_empty()
        && candidate.len() <= 128
        && candidate.split('/').all(|segment| {
            !segment.is_empty()
                && segment != "."
                && segment != ".."
                && segment
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || "_+-".contains(character))
        });
    valid.then(|| candidate.to_string())
}

fn system_time_zone_name() -> Option<String> {
    ["/etc/localtime", "/var/db/timezone/localtime"]
        .into_iter()
        .filter_map(|path| fs::read_link(path).ok())
        .find_map(|path| zoneinfo_name_from_path(&path))
}

fn offset_time_zone_name(offset_seconds: i32) -> String {
    if offset_seconds == 0 {
        return "UTC".to_string();
    }
    let absolute = i64::from(offset_seconds).abs();
    let sign = if offset_seconds < 0 { '-' } else { '+' };
    let hours = absolute / 3_600;
    let minutes = absolute % 3_600 / 60;
    let seconds = absolute % 60;
    if seconds == 0 {
        format!("UTC{sign}{hours:02}:{minutes:02}")
    } else {
        format!("UTC{sign}{hours:02}:{minutes:02}:{seconds:02}")
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn local_clock_context(unix_seconds: i64) -> Result<(String, i32), String> {
    let raw: libc::time_t = unix_seconds
        .try_into()
        .map_err(|_| "system clock is outside the local-time range".to_string())?;
    // SAFETY: `local` is fully initialized before it is read, `raw` and
    // `local` remain alive for the call, and localtime_r writes only to the
    // supplied libc::tm value.
    let mut local = unsafe { std::mem::zeroed::<libc::tm>() };
    if unsafe { libc::localtime_r(&raw, &mut local) }.is_null() {
        return Err("host local time could not be resolved".to_string());
    }
    let offset_seconds = i32::try_from(local.tm_gmtoff)
        .map_err(|_| "host UTC offset is outside the contract range".to_string())?;
    let abbreviation = if local.tm_zone.is_null() {
        None
    } else {
        // SAFETY: a successful localtime_r call supplies a NUL-terminated,
        // process-owned timezone abbreviation for the lifetime of this call.
        unsafe { CStr::from_ptr(local.tm_zone) }
            .to_str()
            .ok()
            .filter(|value| !value.is_empty() && value.len() <= 32)
            .map(str::to_string)
    };
    let time_zone = system_time_zone_name()
        .or(abbreviation)
        .unwrap_or_else(|| offset_time_zone_name(offset_seconds));
    Ok((time_zone, offset_seconds))
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn local_clock_context(_unix_seconds: i64) -> Result<(String, i32), String> {
    Ok(("UTC".to_string(), 0))
}

fn current_wall_clock() -> Result<(String, String, i32), String> {
    let seconds = current_unix_seconds()?;
    let now_utc = canonical_utc_from_unix_seconds(seconds)?;
    let (time_zone, utc_offset_seconds) = local_clock_context(seconds)?;
    Ok((now_utc, time_zone, utc_offset_seconds))
}

impl clock::Host for RuntimeState {
    fn wall_now(&mut self) -> Result<clock::WallClock, common::HostError> {
        let (now_utc, time_zone, utc_offset_seconds) =
            current_wall_clock().map_err(|message| common::HostError {
                code: common::ErrorCode::Internal,
                message,
                retryable: true,
            })?;
        Ok(clock::WallClock {
            now_utc,
            time_zone,
            utc_offset_seconds,
        })
    }

    fn monotonic_now(&mut self) -> u64 {
        self.started
            .elapsed()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX)
    }
}

impl kv::Host for RuntimeState {
    fn get(&mut self, _key: String) -> Result<Option<kv::Entry>, common::HostError> {
        Ok(None)
    }

    fn scan_prefix(
        &mut self,
        _prefix: String,
        _limit: u32,
    ) -> Result<Vec<kv::Entry>, common::HostError> {
        Ok(Vec::new())
    }

    fn transact(
        &mut self,
        _request: kv::TransactionRequest,
    ) -> Result<kv::TransactionResult, common::HostError> {
        Ok(kv::TransactionResult {
            state_revision: 1,
            deduplicated: false,
        })
    }
}

impl log::Host for RuntimeState {
    fn write(
        &mut self,
        _level: log::Level,
        _message: String,
        _fields: Vec<log::Field>,
        _idempotency_key: String,
    ) -> Result<(), common::HostError> {
        Ok(())
    }
}

impl host_info::Host for RuntimeState {
    fn describe_host(&mut self) -> host_info::HostDescription {
        host_info::HostDescription {
            profile: common::ExecutionProfile::Desktop,
            background: host_info::BackgroundReliability::ForegroundOnly,
            capabilities: ["clock", "kv", "log", "host-info", "settings"]
                .into_iter()
                .map(|name| host_info::CapabilityState {
                    interface_name: name.to_string(),
                    availability: host_info::Availability::Brokered,
                    granted: true,
                    detail: "bounded native preview host".to_string(),
                })
                .collect(),
        }
    }
}

impl settings::Host for RuntimeState {
    fn current(&mut self) -> Result<settings::SettingsSnapshot, common::HostError> {
        Ok(settings::SettingsSnapshot {
            schema_revision: 1,
            config_revision: 1,
            values: Vec::new(),
        })
    }
}

impl scheduler::Host for RuntimeState {
    fn upsert(
        &mut self,
        _request: scheduler::ScheduleRequest,
    ) -> Result<scheduler::ScheduleRecord, common::HostError> {
        Err(permission_denied(
            "scheduler is not granted to this UI-only preview",
        ))
    }

    fn disable(&mut self, _id: String, _idempotency_key: String) -> Result<(), common::HostError> {
        Err(permission_denied(
            "scheduler is not granted to this UI-only preview",
        ))
    }

    fn enable(
        &mut self,
        _id: String,
        _idempotency_key: String,
    ) -> Result<scheduler::ScheduleRecord, common::HostError> {
        Err(permission_denied(
            "scheduler is not granted to this UI-only preview",
        ))
    }

    fn list_schedules(&mut self) -> Result<Vec<scheduler::ScheduleRecord>, common::HostError> {
        Err(permission_denied(
            "scheduler is not granted to this UI-only preview",
        ))
    }

    fn acknowledge_occurrence(
        &mut self,
        _occurrence: String,
        _outcome: scheduler::OccurrenceOutcome,
        _idempotency_key: String,
    ) -> Result<(), common::HostError> {
        Err(permission_denied(
            "scheduler is not granted to this UI-only preview",
        ))
    }
}

fn component_arguments() -> Result<(PathBuf, String), String> {
    let mut arguments = env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--component")) {
        return Err(
            "usage: vibapp-runtime --component <component.wasm> --expected-sha256 <digest>"
                .to_string(),
        );
    }
    let path = arguments
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| "missing component path".to_string())?;
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--expected-sha256")) {
        return Err("missing --expected-sha256".to_string());
    }
    let expected = arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .ok_or_else(|| "missing expected component digest".to_string())?;
    if expected.len() != 64
        || !expected
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(
            "expected component digest must be 64 lowercase hexadecimal characters".to_string(),
        );
    }
    if arguments.next().is_some() {
        return Err("unexpected extra runtime arguments".to_string());
    }
    let path = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve component: {error}"))?;
    Ok((path, expected))
}

fn read_component(path: &Path, expected_digest: &str) -> Result<Vec<u8>, String> {
    let metadata = path
        .metadata()
        .map_err(|error| format!("cannot inspect component: {error}"))?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_COMPONENT_BYTES {
        return Err("component must be a non-empty regular file no larger than 16 MiB".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("cannot read component: {error}"))?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    if digest != expected_digest {
        return Err(format!(
            "component digest mismatch: expected {expected_digest}, got {digest}"
        ));
    }
    Ok(bytes)
}

fn run() -> Result<serde_json::Value, String> {
    let (component_path, expected_digest) = component_arguments()?;
    let component_bytes = read_component(&component_path, &expected_digest)?;

    let mut config = Config::new();
    config.wasm_component_model(true);
    config.consume_fuel(true);
    config.epoch_interruption(true);
    config.max_wasm_stack(2 * 1024 * 1024);
    let engine =
        Engine::new(&config).map_err(|error| format!("engine configuration failed: {error}"))?;
    let component = Component::new(&engine, &component_bytes)
        .map_err(|error| format!("component compilation failed: {error}"))?;

    let mut linker = Linker::new(&engine);
    bindings::UiOnlyReference::add_to_linker::<_, HasSelf<_>>(&mut linker, |state| state)
        .map_err(|error| format!("host interface linking failed: {error}"))?;

    let limits = StoreLimitsBuilder::new()
        .memory_size(MAX_LINEAR_MEMORY_BYTES)
        .instances(8)
        .tables(8)
        .memories(4)
        .trap_on_grow_failure(true)
        .build();
    let mut store = Store::new(
        &engine,
        RuntimeState {
            started: Instant::now(),
            limits,
        },
    );
    store.limiter(|state| &mut state.limits);
    store
        .set_fuel(FUEL_BUDGET)
        .map_err(|error| format!("fuel setup failed: {error}"))?;
    store.set_epoch_deadline(1);

    let deadline_engine = engine.clone();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(2));
        deadline_engine.increment_epoch();
    });

    let bindings = bindings::UiOnlyReference::instantiate(&mut store, &component, &linker)
        .map_err(|error| format!("component instantiation failed: {error}"))?;
    let guest_api = bindings.vibapp_experimental_v0_guest();

    let descriptor = guest_api
        .call_describe(&mut store)
        .map_err(|error| format!("describe call trapped: {error}"))?
        .map_err(|error| format!("guest rejected describe: {}", error.message))?;

    let context = common::CallContext {
        event_id: "launch-event-1".to_string(),
        idempotency_key: "launch-idempotency-1".to_string(),
        cancellation: "launch-cancellation-1".to_string(),
        generation: "hello-generation-1".to_string(),
        profile: common::ExecutionProfile::Desktop,
        deadline_monotonic_ms: 2_000,
    };
    let event = guest::AppEvent::Launcher(guest::LauncherEvent::Launch(guest::LaunchEvent {
        entrypoint: "main".to_string(),
        session: "native-session-1".to_string(),
        surface: "main-surface-1".to_string(),
        route: "home".to_string(),
        reason: guest::OpenReason::User,
    }));
    let event_output = guest_api
        .call_handle_event(&mut store, &context, &event)
        .map_err(|error| format!("launch event trapped: {error}"))?
        .map_err(|error| format!("guest rejected launch: {}", error.message))?;

    let health = guest_api
        .call_health(
            &mut store,
            &context,
            &guest::HealthRequest {
                service_entrypoint: None,
                include_dependencies: false,
            },
        )
        .map_err(|error| format!("health call trapped: {error}"))?
        .map_err(|error| format!("guest rejected health: {}", error.message))?;

    let surface = event_output
        .surfaces
        .first()
        .ok_or_else(|| "guest returned no launcher surface".to_string())?;
    Ok(json!({
        "schema_version": "vibapp.runtime-launch.experimental.v1",
        "runtime_process": "vibapp-runtime",
        "isolation": {
            "separate_process": true,
            "ambient_wasi_linked": false,
            "memory_limit_bytes": MAX_LINEAR_MEMORY_BYTES,
            "fuel_budget": FUEL_BUDGET,
            "epoch_deadline_seconds": 2
        },
        "component_sha256": expected_digest,
        "descriptor": descriptor,
        "health": health,
        "surface": surface,
    }))
}

fn main() {
    std::hint::black_box(&DESKTOP_BUILD_INPUT_RECEIPT);
    match run() {
        Ok(value) => match serde_json::to_string(&value) {
            Ok(encoded) if encoded.len() <= 256 * 1024 => println!("{encoded}"),
            Ok(_) => {
                eprintln!("runtime output exceeded 256 KiB");
                std::process::exit(74);
            }
            Err(error) => {
                eprintln!("runtime output encoding failed: {error}");
                std::process::exit(74);
            }
        },
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(70);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{canonical_utc_from_unix_seconds, local_clock_context, zoneinfo_name_from_path};
    use std::path::Path;

    #[test]
    fn utc_formatter_is_canonical_across_epoch_leap_day_and_2038() {
        let cases = [
            (-1, "1969-12-31T23:59:59Z"),
            (0, "1970-01-01T00:00:00Z"),
            (86_400, "1970-01-02T00:00:00Z"),
            (951_782_400, "2000-02-29T00:00:00Z"),
            (2_147_483_647, "2038-01-19T03:14:07Z"),
            (253_402_300_799, "9999-12-31T23:59:59Z"),
        ];
        for (seconds, expected) in cases {
            let actual = canonical_utc_from_unix_seconds(seconds).unwrap();
            assert_eq!(actual, expected);
            assert_eq!(actual.len(), 20);
            assert!(actual.ends_with('Z'));
        }
    }

    #[test]
    fn utc_formatter_rejects_values_outside_the_four_digit_contract() {
        assert!(canonical_utc_from_unix_seconds(253_402_300_800).is_err());
    }

    #[test]
    fn zoneinfo_path_yields_a_bounded_iana_name() {
        assert_eq!(
            zoneinfo_name_from_path(Path::new("/var/db/timezone/zoneinfo/Asia/Shanghai")),
            Some("Asia/Shanghai".to_string())
        );
        assert_eq!(
            zoneinfo_name_from_path(Path::new("/tmp/zoneinfo/../escape")),
            None
        );
    }

    #[test]
    fn local_clock_context_has_a_valid_contract_offset() {
        let (time_zone, offset) = local_clock_context(1_787_888_000).unwrap();
        assert!(!time_zone.is_empty());
        assert!((-86_400..=86_400).contains(&offset));
    }
}
