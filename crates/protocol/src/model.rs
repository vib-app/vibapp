//! Typed, launcher-neutral Stage 0 protocol model.

use crate::json::JsonValue;

pub const SCHEMA_VERSION: &str = "vibapp.manifest.experimental-v0.0.1";
pub const PACKAGE_FORMAT: &str = "vibapp.package.experimental-v0";
pub const CONTRACT_VERSION: &str = "vibapp:experimental-v0@0.0.1";
pub const WASI_VERSION: &str = "0.2";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AppKind {
    Ui,
    Service,
    Hybrid,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Profile {
    Desktop,
    WebPreview,
    WebRuntime,
    Headless,
}

impl Profile {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Desktop => "desktop",
            Self::WebPreview => "web-preview",
            Self::WebRuntime => "web-runtime",
            Self::Headless => "headless",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum World {
    UiOnly,
    ServiceOnly,
    Hybrid,
    WebPreview,
}

impl World {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::UiOnly => "ui-only-reference",
            Self::ServiceOnly => "service-only-reference",
            Self::Hybrid => "hybrid-reference",
            Self::WebPreview => "web-preview-reference",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProfileMode {
    Full,
    Degraded,
    Preview,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Background {
    Daemon,
    Process,
    ForegroundOnly,
    NotApplicable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArtifactRole {
    CanonicalComponent,
    BrowserDerived,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Availability {
    Native,
    Brokered,
    Mock,
    Denied,
    Unavailable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Necessity {
    Required,
    Degradable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Grant {
    Automatic,
    User,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntrypointKind {
    LauncherUi,
    Service,
    Settings,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Restoration {
    None,
    RouteOnly,
    SafeFields,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EntrypointDetails {
    Launcher {
        initial_route: String,
        allowed_routes: Vec<String>,
        restoration: Restoration,
    },
    Service {
        triggers: Vec<String>,
        health_check_interval_seconds: u64,
    },
    Settings {
        schema_export: String,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum DataDisposition {
    Delete,
    Retain,
    ExportThenDelete,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Publisher {
    pub id: String,
    pub display_name: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct App {
    pub id: String,
    pub version: String,
    pub kind: AppKind,
    pub display_name: String,
    pub description: String,
    pub publisher: Publisher,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Artifact {
    pub path: String,
    pub media_type: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DerivedArtifact {
    pub profile: Profile,
    pub derived_from_sha256: String,
    pub entry: Artifact,
    pub files: Vec<Artifact>,
    pub derivation_attestation: Artifact,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Artifacts {
    pub canonical_component: Artifact,
    pub assets: Vec<Artifact>,
    pub browser_derivations: Vec<DerivedArtifact>,
    pub provenance: Artifact,
    pub sbom: Artifact,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProfileSupport {
    pub profile: Profile,
    pub mode: ProfileMode,
    pub background: Background,
    pub artifact_role: ArtifactRole,
    pub degradation: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlatformSupport {
    pub os: String,
    pub arch: String,
    pub profiles: Vec<Profile>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Runtime {
    pub world: World,
    pub required_imports: Vec<String>,
    pub profiles: Vec<ProfileSupport>,
    pub platforms: Vec<PlatformSupport>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entrypoint {
    pub id: String,
    pub kind: EntrypointKind,
    pub label: String,
    pub profiles: Vec<Profile>,
    pub details: EntrypointDetails,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilityProfile {
    pub profile: Profile,
    pub availability: Availability,
    pub behavior: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Capability {
    pub interface: String,
    pub necessity: Necessity,
    pub grant: Grant,
    pub reason: String,
    pub scope: CapabilityScope,
    pub profiles: Vec<CapabilityProfile>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CapabilityScope {
    pub maximum_requests_per_hour: Option<u64>,
    pub maximum_schedules: Option<u64>,
    pub maximum_storage_bytes: Option<u64>,
    pub methods: Vec<String>,
    pub metrics: Vec<String>,
    pub origins: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Resources {
    pub linear_memory_bytes: u64,
    pub event_wall_time_ms: u64,
    pub health_migration_wall_time_ms: u64,
    pub output_bytes: u64,
    pub stored_data_bytes: u64,
    pub durable_schedules: u64,
    pub log_bytes_per_day: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StateContract {
    pub schema: u32,
    pub migratable_from_min: u32,
    pub migratable_from_max: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LifecycleContract {
    pub allowed_data_dispositions: Vec<DataDisposition>,
    pub default_data_disposition: DataDisposition,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceContract {
    pub revision: String,
    pub cargo_lock_sha256: String,
    pub builder_image_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LicenseContract {
    pub spdx_expression: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NetworkAccess {
    None,
    ScopedBroker,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PrivacyContract {
    pub stores_personal_data: bool,
    pub network_access: NetworkAccess,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum VerificationStatus {
    Fixture,
    Unverified,
    Verified,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RevocationStatus {
    NotRevoked,
    Revoked,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VerificationContract {
    pub status: VerificationStatus,
    pub revocation: RevocationStatus,
}

/// Fully validated manifest. `raw` preserves every schema field for canonical round trips.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Manifest {
    pub app: App,
    pub artifacts: Artifacts,
    pub runtime: Runtime,
    pub entrypoints: Vec<Entrypoint>,
    pub capabilities: Vec<Capability>,
    pub resources: Resources,
    pub state: StateContract,
    pub lifecycle: LifecycleContract,
    pub source: SourceContract,
    pub license: LicenseContract,
    pub privacy: PrivacyContract,
    pub verification: VerificationContract,
    pub raw: JsonValue,
}

impl Manifest {
    #[must_use]
    pub fn canonical_json(&self) -> String {
        self.raw.to_canonical_json()
    }

    #[must_use]
    pub fn has_launcher_surface(&self) -> bool {
        self.entrypoints
            .iter()
            .any(|entrypoint| entrypoint.kind == EntrypointKind::LauncherUi)
    }

    #[must_use]
    pub fn supports_profile(&self, profile: Profile) -> bool {
        self.runtime
            .profiles
            .iter()
            .any(|support| support.profile == profile)
    }
}
