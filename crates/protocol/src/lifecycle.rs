//! Pure lifecycle transitions shared by Launcher, CLI, and headless hosts.

use crate::manifest::ProtocolErrorCode;
use crate::model::{
    AppKind, DataDisposition, EntrypointDetails, EntrypointKind, LifecycleContract, Manifest,
    Profile, Restoration,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IdentifierError {
    Empty,
    TooLong,
    ContainsControl,
}

macro_rules! host_identifier {
    ($name:ident) => {
        #[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
        pub struct $name(String);

        impl $name {
            pub fn from_host(value: impl Into<String>) -> Result<Self, IdentifierError> {
                let value = value.into();
                if value.is_empty() {
                    return Err(IdentifierError::Empty);
                }
                if value.chars().count() > 128 {
                    return Err(IdentifierError::TooLong);
                }
                if value.chars().any(char::is_control) {
                    return Err(IdentifierError::ContainsControl);
                }
                Ok(Self(value))
            }

            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
    };
}

host_identifier!(SessionId);
host_identifier!(SurfaceId);
host_identifier!(RestoreToken);

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SafeFieldValue {
    Empty,
    Text(String),
    Integer(i64),
    Decimal(String),
    Boolean(bool),
    Date { year: u16, month: u8, day: u8 },
    Time { hour: u8, minute: u8, second: u8 },
    TimeZone(String),
    Choice(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SafeFieldChange {
    field: String,
    value: SafeFieldValue,
}

impl SafeFieldChange {
    pub fn from_host(
        field: impl Into<String>,
        value: SafeFieldValue,
    ) -> Result<Self, LifecycleError> {
        let field = field.into();
        if field.is_empty() || field.chars().count() > 128 || field.chars().any(char::is_control) {
            return Err(LifecycleError::UnsafeRestoration);
        }
        let valid_value = match &value {
            SafeFieldValue::Empty | SafeFieldValue::Integer(_) | SafeFieldValue::Boolean(_) => true,
            SafeFieldValue::Text(value) => value.chars().count() <= 4_096,
            SafeFieldValue::Decimal(value) => {
                !value.is_empty()
                    && value.len() <= 128
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || matches!(byte, b'-' | b'.'))
            }
            SafeFieldValue::Date { year, month, day } => valid_date(*year, *month, *day),
            SafeFieldValue::Time {
                hour,
                minute,
                second,
            } => *hour <= 23 && *minute <= 59 && *second <= 59,
            SafeFieldValue::TimeZone(value) | SafeFieldValue::Choice(value) => {
                !value.is_empty()
                    && value.chars().count() <= 128
                    && !value.chars().any(char::is_control)
            }
        };
        if !valid_value {
            return Err(LifecycleError::UnsafeRestoration);
        }
        Ok(Self { field, value })
    }

    #[must_use]
    pub fn field(&self) -> &str {
        &self.field
    }

    #[must_use]
    pub fn value(&self) -> &SafeFieldValue {
        &self.value
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RestoreAuthorization {
    pub app_id: String,
    pub entrypoint_id: String,
    pub session_id: SessionId,
    pub surface_id: SurfaceId,
    pub route: String,
    pub token: RestoreToken,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AppListing {
    pub app_id: String,
    pub display_name: String,
    pub kind: AppKind,
    pub package: PackageStatus,
    pub launcher_entrypoints: Vec<String>,
    pub profiles: Vec<Profile>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionState {
    pub app_id: String,
    pub entrypoint_id: String,
    pub session_id: SessionId,
    pub surface_id: SurfaceId,
    pub route: String,
    pub surface: SurfaceStatus,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LauncherOperation {
    List,
    Open {
        app_id: String,
        entrypoint_id: String,
        session_id: SessionId,
        surface_id: SurfaceId,
        route: String,
    },
    Focus {
        entrypoint_id: String,
        session_id: SessionId,
        surface_id: SurfaceId,
    },
    Blur {
        entrypoint_id: String,
        session_id: SessionId,
        surface_id: SurfaceId,
    },
    Close {
        entrypoint_id: String,
        session_id: SessionId,
        surface_id: SurfaceId,
    },
    Restore {
        app_id: String,
        entrypoint_id: String,
        session_id: SessionId,
        surface_id: SurfaceId,
        route: String,
        token: RestoreToken,
        safe_fields: Vec<SafeFieldChange>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PackageStatus {
    Absent,
    InstalledDisabled,
    Enabled,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SurfaceStatus {
    Closed,
    Open,
    Focused,
    Blurred,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PackageOperation {
    Install,
    Enable,
    Disable,
    Configure,
    Uninstall { disposition: DataDisposition },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SurfaceOperation {
    Open,
    Focus,
    Blur,
    Close,
    Restore,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LifecycleState {
    pub package: PackageStatus,
    pub surface: SurfaceStatus,
    pub service_running: bool,
    pub package_retained: bool,
    pub state_retained: bool,
    pub schedules_active: bool,
}

impl Default for LifecycleState {
    fn default() -> Self {
        Self {
            package: PackageStatus::Absent,
            surface: SurfaceStatus::Closed,
            service_running: false,
            package_retained: false,
            state_retained: false,
            schedules_active: false,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LifecycleError {
    PackageAlreadyInstalled,
    PackageNotInstalled,
    AppDisabled,
    SurfaceUnavailable,
    InvalidSurfaceTransition,
    DataDispositionNotAllowed,
    EntrypointUnavailable,
    RouteUnavailable,
    ForgedSession,
    UnsafeRestoration,
}

impl LifecycleError {
    #[must_use]
    pub const fn protocol_code(self) -> ProtocolErrorCode {
        match self {
            Self::PackageAlreadyInstalled => ProtocolErrorCode::Conflict,
            Self::PackageNotInstalled => ProtocolErrorCode::AppUninstalled,
            Self::AppDisabled => ProtocolErrorCode::AppDisabled,
            Self::SurfaceUnavailable | Self::EntrypointUnavailable | Self::RouteUnavailable => {
                ProtocolErrorCode::UnsupportedSurface
            }
            Self::InvalidSurfaceTransition
            | Self::DataDispositionNotAllowed
            | Self::UnsafeRestoration => ProtocolErrorCode::InvalidArgument,
            Self::ForgedSession => ProtocolErrorCode::ForgedIdentifier,
        }
    }
}

/// Produce a deterministic launcher-neutral list without exposing engine handles.
#[must_use]
pub fn list_apps(packages: &[(&Manifest, LifecycleState)]) -> Vec<AppListing> {
    let mut listings = packages
        .iter()
        .filter(|(_, state)| state.package != PackageStatus::Absent)
        .map(|(manifest, state)| AppListing {
            app_id: manifest.app.id.clone(),
            display_name: manifest.app.display_name.clone(),
            kind: manifest.app.kind,
            package: state.package,
            launcher_entrypoints: manifest
                .entrypoints
                .iter()
                .filter(|entrypoint| entrypoint.kind == EntrypointKind::LauncherUi)
                .map(|entrypoint| entrypoint.id.clone())
                .collect(),
            profiles: manifest
                .runtime
                .profiles
                .iter()
                .map(|support| support.profile)
                .collect(),
        })
        .collect::<Vec<_>>();
    listings.sort_by(|left, right| left.app_id.cmp(&right.app_id));
    listings
}

pub fn open_session(
    manifest: &Manifest,
    lifecycle: LifecycleState,
    profile: Profile,
    entrypoint_id: &str,
    session_id: SessionId,
    surface_id: SurfaceId,
    route: &str,
) -> Result<SessionState, LifecycleError> {
    if lifecycle.package != PackageStatus::Enabled {
        return Err(LifecycleError::AppDisabled);
    }
    if profile == Profile::Headless || !manifest.supports_profile(profile) {
        return Err(LifecycleError::SurfaceUnavailable);
    }
    let entrypoint = manifest
        .entrypoints
        .iter()
        .find(|entrypoint| {
            entrypoint.id == entrypoint_id
                && entrypoint.kind == EntrypointKind::LauncherUi
                && entrypoint.profiles.contains(&profile)
        })
        .ok_or(LifecycleError::EntrypointUnavailable)?;
    let crate::model::EntrypointDetails::Launcher { allowed_routes, .. } = &entrypoint.details
    else {
        return Err(LifecycleError::EntrypointUnavailable);
    };
    if !allowed_routes.iter().any(|allowed| allowed == route) {
        return Err(LifecycleError::RouteUnavailable);
    }
    Ok(SessionState {
        app_id: manifest.app.id.clone(),
        entrypoint_id: entrypoint.id.clone(),
        session_id,
        surface_id,
        route: route.to_owned(),
        surface: SurfaceStatus::Open,
    })
}

pub fn restore_session(
    manifest: &Manifest,
    lifecycle: LifecycleState,
    profile: Profile,
    authorization: &RestoreAuthorization,
    operation: &LauncherOperation,
) -> Result<SessionState, LifecycleError> {
    if lifecycle.package != PackageStatus::Enabled {
        return Err(LifecycleError::AppDisabled);
    }
    let LauncherOperation::Restore {
        app_id,
        entrypoint_id,
        session_id,
        surface_id,
        route,
        token,
        safe_fields,
    } = operation
    else {
        return Err(LifecycleError::InvalidSurfaceTransition);
    };
    if app_id != &authorization.app_id
        || entrypoint_id != &authorization.entrypoint_id
        || session_id != &authorization.session_id
        || surface_id != &authorization.surface_id
        || route != &authorization.route
        || token != &authorization.token
        || app_id != &manifest.app.id
    {
        return Err(LifecycleError::ForgedSession);
    }
    if profile == Profile::Headless || !manifest.supports_profile(profile) {
        return Err(LifecycleError::SurfaceUnavailable);
    }
    let entrypoint = manifest
        .entrypoints
        .iter()
        .find(|candidate| {
            candidate.id == *entrypoint_id
                && candidate.kind == EntrypointKind::LauncherUi
                && candidate.profiles.contains(&profile)
        })
        .ok_or(LifecycleError::EntrypointUnavailable)?;
    let EntrypointDetails::Launcher {
        allowed_routes,
        restoration,
        ..
    } = &entrypoint.details
    else {
        return Err(LifecycleError::EntrypointUnavailable);
    };
    if !allowed_routes.iter().any(|allowed| allowed == route) {
        return Err(LifecycleError::RouteUnavailable);
    }
    match restoration {
        Restoration::None => return Err(LifecycleError::UnsafeRestoration),
        Restoration::RouteOnly if !safe_fields.is_empty() => {
            return Err(LifecycleError::UnsafeRestoration);
        }
        Restoration::RouteOnly | Restoration::SafeFields => {}
    }
    let mut field_ids = std::collections::BTreeSet::new();
    if safe_fields
        .iter()
        .any(|field| !field_ids.insert(field.field()))
    {
        return Err(LifecycleError::UnsafeRestoration);
    }
    Ok(SessionState {
        app_id: app_id.clone(),
        entrypoint_id: entrypoint_id.clone(),
        session_id: session_id.clone(),
        surface_id: surface_id.clone(),
        route: route.clone(),
        surface: SurfaceStatus::Open,
    })
}

pub fn apply_session_operation(
    session: &SessionState,
    operation: &LauncherOperation,
) -> Result<SessionState, LifecycleError> {
    let (candidate_entrypoint, candidate_session, candidate_surface, next_status) = match operation
    {
        LauncherOperation::Focus {
            entrypoint_id,
            session_id,
            surface_id,
        } => (
            entrypoint_id,
            session_id,
            surface_id,
            SurfaceStatus::Focused,
        ),
        LauncherOperation::Blur {
            entrypoint_id,
            session_id,
            surface_id,
        } => (
            entrypoint_id,
            session_id,
            surface_id,
            SurfaceStatus::Blurred,
        ),
        LauncherOperation::Close {
            entrypoint_id,
            session_id,
            surface_id,
        } => (entrypoint_id, session_id, surface_id, SurfaceStatus::Closed),
        _ => return Err(LifecycleError::InvalidSurfaceTransition),
    };
    if candidate_entrypoint != &session.entrypoint_id
        || candidate_session != &session.session_id
        || candidate_surface != &session.surface_id
    {
        return Err(LifecycleError::ForgedSession);
    }
    let transition_allowed = matches!(
        (session.surface, next_status),
        (
            SurfaceStatus::Open | SurfaceStatus::Blurred,
            SurfaceStatus::Focused
        ) | (
            SurfaceStatus::Open | SurfaceStatus::Focused,
            SurfaceStatus::Blurred
        ) | (
            SurfaceStatus::Open | SurfaceStatus::Focused | SurfaceStatus::Blurred,
            SurfaceStatus::Closed
        )
    );
    if !transition_allowed {
        return Err(LifecycleError::InvalidSurfaceTransition);
    }
    Ok(SessionState {
        surface: next_status,
        ..session.clone()
    })
}

pub fn apply_package_operation(
    state: LifecycleState,
    app_kind: AppKind,
    lifecycle: &LifecycleContract,
    operation: PackageOperation,
) -> Result<LifecycleState, LifecycleError> {
    match operation {
        PackageOperation::Install => {
            if state.package != PackageStatus::Absent {
                return Err(LifecycleError::PackageAlreadyInstalled);
            }
            Ok(LifecycleState {
                package: PackageStatus::InstalledDisabled,
                package_retained: true,
                state_retained: true,
                ..state
            })
        }
        PackageOperation::Enable => {
            if state.package == PackageStatus::Absent {
                return Err(LifecycleError::PackageNotInstalled);
            }
            Ok(LifecycleState {
                package: PackageStatus::Enabled,
                service_running: matches!(app_kind, AppKind::Service | AppKind::Hybrid),
                package_retained: true,
                state_retained: true,
                ..state
            })
        }
        PackageOperation::Disable => {
            if state.package == PackageStatus::Absent {
                return Err(LifecycleError::PackageNotInstalled);
            }
            Ok(LifecycleState {
                package: PackageStatus::InstalledDisabled,
                surface: SurfaceStatus::Closed,
                service_running: false,
                schedules_active: false,
                package_retained: true,
                state_retained: true,
            })
        }
        PackageOperation::Configure => {
            if state.package == PackageStatus::Absent {
                return Err(LifecycleError::PackageNotInstalled);
            }
            Ok(state)
        }
        PackageOperation::Uninstall { disposition } => {
            if state.package == PackageStatus::Absent {
                return Err(LifecycleError::PackageNotInstalled);
            }
            if !lifecycle.allowed_data_dispositions.contains(&disposition) {
                return Err(LifecycleError::DataDispositionNotAllowed);
            }
            Ok(LifecycleState {
                package: PackageStatus::Absent,
                surface: SurfaceStatus::Closed,
                service_running: false,
                schedules_active: false,
                package_retained: false,
                state_retained: disposition == DataDisposition::Retain,
            })
        }
    }
}

pub fn apply_surface_operation(
    state: LifecycleState,
    manifest: &Manifest,
    profile: Profile,
    operation: SurfaceOperation,
) -> Result<LifecycleState, LifecycleError> {
    if state.package != PackageStatus::Enabled {
        return Err(LifecycleError::AppDisabled);
    }
    if profile == Profile::Headless
        || !manifest.entrypoints.iter().any(|entrypoint| {
            entrypoint.kind == EntrypointKind::LauncherUi && entrypoint.profiles.contains(&profile)
        })
        || !manifest.supports_profile(profile)
    {
        return Err(LifecycleError::SurfaceUnavailable);
    }
    let surface = match (state.surface, operation) {
        (SurfaceStatus::Closed, SurfaceOperation::Open) => SurfaceStatus::Open,
        (SurfaceStatus::Open | SurfaceStatus::Blurred, SurfaceOperation::Focus) => {
            SurfaceStatus::Focused
        }
        (SurfaceStatus::Focused | SurfaceStatus::Open, SurfaceOperation::Blur) => {
            SurfaceStatus::Blurred
        }
        (
            SurfaceStatus::Open | SurfaceStatus::Focused | SurfaceStatus::Blurred,
            SurfaceOperation::Close,
        ) => SurfaceStatus::Closed,
        _ => return Err(LifecycleError::InvalidSurfaceTransition),
    };
    // Surface closure is intentionally independent from a hybrid service instance.
    Ok(LifecycleState { surface, ..state })
}

const fn valid_date(year: u16, month: u8, day: u8) -> bool {
    if year == 0 || month == 0 || month > 12 || day == 0 {
        return false;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let maximum = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    day <= maximum
}
