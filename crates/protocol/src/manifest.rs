//! Strict manifest parsing and schema-derived validation.

use std::collections::BTreeSet;
use std::fmt;

use crate::json::{JsonError, JsonValue, parse_json};
use crate::model::{
    App, AppKind, Artifact, ArtifactRole, Artifacts, Availability, Background, Capability,
    CapabilityProfile, CapabilityScope, DataDisposition, DerivedArtifact, Entrypoint,
    EntrypointDetails, EntrypointKind, Grant, LicenseContract, LifecycleContract, Manifest,
    Necessity, NetworkAccess, PACKAGE_FORMAT, PlatformSupport, PrivacyContract, Profile,
    ProfileMode, ProfileSupport, Publisher, Resources, Restoration, RevocationStatus, Runtime,
    SCHEMA_VERSION, SourceContract, StateContract, VerificationContract, VerificationStatus,
    WASI_VERSION, World,
};
use crate::wit_catalog::{CONTRACT_VERSION, is_app_interface, reconcile_world_imports};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProtocolErrorCode {
    InvalidArgument,
    IncompatibleContract,
    UnsupportedVersion,
    InvalidManifest,
    UnknownInterface,
    MissingInterface,
    PermissionDenied,
    ConsentRequired,
    CapabilityUnavailable,
    UnsupportedSurface,
    ResourceLimit,
    DeadlineExceeded,
    Cancelled,
    AppDisabled,
    AppUninstalled,
    UpgradeInProgress,
    IntegrityFailure,
    NotFound,
    Conflict,
    StaleRevision,
    MalformedOutput,
    ForgedIdentifier,
    Internal,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ManifestError {
    pub code: ProtocolErrorCode,
    pub path: String,
    pub detail: String,
    schema_valid: Option<bool>,
}

impl ManifestError {
    fn new(code: ProtocolErrorCode, path: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            code,
            path: path.into(),
            detail: detail.into(),
            schema_valid: Some(true),
        }
    }

    fn schema_invalid(
        code: ProtocolErrorCode,
        path: impl Into<String>,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            code,
            path: path.into(),
            detail: detail.into(),
            schema_valid: Some(false),
        }
    }

    fn invalid(path: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            code: ProtocolErrorCode::InvalidManifest,
            path: path.into(),
            detail: detail.into(),
            schema_valid: None,
        }
    }

    /// Returns an explicit JSON Schema classification when the rejecting
    /// branch has one. Unclassified generic validation errors return `None`
    /// instead of guessing across schema and semantic invariants.
    #[must_use]
    pub const fn schema_valid(&self) -> Option<bool> {
        self.schema_valid
    }
}

impl fmt::Display for ManifestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.path, self.detail)
    }
}

impl std::error::Error for ManifestError {}

impl From<JsonError> for ManifestError {
    fn from(error: JsonError) -> Self {
        Self::invalid(
            "$",
            format!(
                "strict JSON parse failed at byte {}: {:?}",
                error.byte_offset, error.kind
            ),
        )
    }
}

/// Parse, validate, and type a Stage 0 manifest before any component instantiation.
pub fn parse_manifest(input: &str) -> Result<Manifest, ManifestError> {
    let raw = parse_json(input)?;
    build_manifest(raw)
}

fn build_manifest(raw: JsonValue) -> Result<Manifest, ManifestError> {
    let root = exact_object(
        &raw,
        "$",
        &[
            "schema_version",
            "package_format",
            "app",
            "artifacts",
            "runtime",
            "entrypoints",
            "capabilities",
            "resources",
            "state",
            "lifecycle",
            "source",
            "license",
            "privacy",
            "verification",
        ],
        &[
            "schema_version",
            "package_format",
            "app",
            "artifacts",
            "runtime",
            "entrypoints",
            "capabilities",
            "resources",
            "state",
            "lifecycle",
            "source",
            "license",
            "privacy",
            "verification",
        ],
    )?;

    require_const_version(root, "schema_version", SCHEMA_VERSION, "$.schema_version")?;
    require_const_version(root, "package_format", PACKAGE_FORMAT, "$.package_format")?;

    let app = parse_app(required(root, "app", "$")?)?;
    let artifacts = parse_artifacts(required(root, "artifacts", "$")?)?;
    let runtime = parse_runtime(required(root, "runtime", "$")?)?;
    let entrypoints = parse_entrypoints(required(root, "entrypoints", "$")?)?;
    let capabilities = parse_capabilities(required(root, "capabilities", "$")?)?;
    let resources = parse_resources(required(root, "resources", "$")?)?;
    let state = parse_state(required(root, "state", "$")?)?;
    let lifecycle = parse_lifecycle(required(root, "lifecycle", "$")?)?;
    let source = parse_source(required(root, "source", "$")?)?;
    let license = parse_license(required(root, "license", "$")?)?;
    let privacy = parse_privacy(required(root, "privacy", "$")?)?;
    let verification = parse_verification(required(root, "verification", "$")?)?;

    validate_cross_contract(
        &app,
        &artifacts,
        &runtime,
        &entrypoints,
        &capabilities,
        &resources,
        &state,
        &lifecycle,
    )?;

    Ok(Manifest {
        app,
        artifacts,
        runtime,
        entrypoints,
        capabilities,
        resources,
        state,
        lifecycle,
        source,
        license,
        privacy,
        verification,
        raw,
    })
}

fn parse_app(value: &JsonValue) -> Result<App, ManifestError> {
    let path = "$.app";
    let object = exact_object(
        value,
        path,
        &[
            "id",
            "version",
            "kind",
            "display_name",
            "description",
            "publisher",
        ],
        &[
            "id",
            "version",
            "kind",
            "display_name",
            "description",
            "publisher",
        ],
    )?;
    let id = identifier(required_string(object, "id", path)?, "$.app.id")?;
    let version = required_string(object, "version", path)?.to_owned();
    if !is_semver(&version) {
        return Err(ManifestError::invalid(
            "$.app.version",
            "must be canonical SemVer",
        ));
    }
    let kind = match required_string(object, "kind", path)? {
        "ui" => AppKind::Ui,
        "service" => AppKind::Service,
        "hybrid" => AppKind::Hybrid,
        _ => return Err(enum_error("$.app.kind")),
    };
    let display_name = bounded_string(object, "display_name", path, 1, 80)?;
    let description = bounded_string(object, "description", path, 1, 1000)?;
    let publisher_value = required(object, "publisher", path)?;
    let publisher_object = exact_object(
        publisher_value,
        "$.app.publisher",
        &["id", "display_name"],
        &["id", "display_name"],
    )?;
    let publisher = Publisher {
        id: identifier(
            required_string(publisher_object, "id", "$.app.publisher")?,
            "$.app.publisher.id",
        )?,
        display_name: bounded_string(publisher_object, "display_name", "$.app.publisher", 1, 80)?,
    };
    Ok(App {
        id,
        version,
        kind,
        display_name,
        description,
        publisher,
    })
}

fn parse_artifacts(value: &JsonValue) -> Result<Artifacts, ManifestError> {
    let path = "$.artifacts";
    let object = exact_object(
        value,
        path,
        &[
            "canonical_component",
            "assets",
            "browser_derivations",
            "provenance",
            "sbom",
        ],
        &[
            "canonical_component",
            "assets",
            "browser_derivations",
            "provenance",
            "sbom",
        ],
    )?;
    let canonical_component = parse_artifact(
        required(object, "canonical_component", path)?,
        "$.artifacts.canonical_component",
    )?;
    if canonical_component.path != "component.wasm"
        || canonical_component.media_type != "application/wasm"
    {
        return Err(ManifestError::invalid(
            "$.artifacts.canonical_component",
            "canonical component must be component.wasm with application/wasm media type",
        ));
    }
    let assets = parse_artifact_array(
        required(object, "assets", path)?,
        "$.artifacts.assets",
        0,
        128,
    )?;
    let derivations_value = required_array(object, "browser_derivations", path)?;
    if derivations_value.len() > 2 {
        return Err(length_error("$.artifacts.browser_derivations", 0, 2));
    }
    let mut browser_derivations = Vec::with_capacity(derivations_value.len());
    for (index, derivation) in derivations_value.iter().enumerate() {
        browser_derivations.push(parse_derived_artifact(
            derivation,
            &format!("$.artifacts.browser_derivations[{index}]"),
        )?);
    }
    unique_by(
        browser_derivations.iter().map(|item| item.profile.as_str()),
        "$.artifacts.browser_derivations",
    )?;
    let provenance = parse_artifact(
        required(object, "provenance", path)?,
        "$.artifacts.provenance",
    )?;
    let sbom = parse_artifact(required(object, "sbom", path)?, "$.artifacts.sbom")?;

    // `entry` is a selector and must equal exactly one descriptor in `files`.
    // Every physical package path, including attestations, is otherwise global.
    let mut physical_paths = BTreeSet::new();
    let mut insert_path = |artifact: &Artifact, artifact_path: &str| {
        if physical_paths.insert(artifact.path.clone()) {
            Ok(())
        } else {
            Err(ManifestError::invalid(
                artifact_path,
                "physical artifact paths must be globally unique",
            ))
        }
    };
    insert_path(&canonical_component, "$.artifacts.canonical_component.path")?;
    for (index, asset) in assets.iter().enumerate() {
        insert_path(asset, &format!("$.artifacts.assets[{index}].path"))?;
    }
    for (derivation_index, derivation) in browser_derivations.iter().enumerate() {
        for (file_index, file) in derivation.files.iter().enumerate() {
            insert_path(
                file,
                &format!(
                    "$.artifacts.browser_derivations[{derivation_index}].files[{file_index}].path"
                ),
            )?;
        }
        insert_path(
            &derivation.derivation_attestation,
            &format!(
                "$.artifacts.browser_derivations[{derivation_index}].derivation_attestation.path"
            ),
        )?;
    }
    insert_path(&provenance, "$.artifacts.provenance.path")?;
    insert_path(&sbom, "$.artifacts.sbom.path")?;

    Ok(Artifacts {
        canonical_component,
        assets,
        browser_derivations,
        provenance,
        sbom,
    })
}

fn parse_artifact(value: &JsonValue, path: &str) -> Result<Artifact, ManifestError> {
    let object = exact_object(
        value,
        path,
        &["path", "media_type", "sha256", "size_bytes"],
        &["path", "media_type", "sha256", "size_bytes"],
    )?;
    let artifact_path_value = required_string(object, "path", path)?;
    if !is_artifact_path(artifact_path_value) {
        return Err(ManifestError::invalid(
            format!("{path}.path"),
            "must be a relative, traversal-free artifact path",
        ));
    }
    let media_type = bounded_string(object, "media_type", path, 1, 128)?;
    let sha256 = required_string(object, "sha256", path)?;
    if !is_sha256(sha256) {
        return Err(ManifestError::invalid(
            format!("{path}.sha256"),
            "must be 64 lowercase hex digits",
        ));
    }
    let size_bytes = bounded_u64(
        object,
        "size_bytes",
        path,
        1,
        67_108_864,
        ProtocolErrorCode::InvalidManifest,
    )?;
    Ok(Artifact {
        path: artifact_path_value.to_owned(),
        media_type,
        sha256: sha256.to_owned(),
        size_bytes,
    })
}

fn parse_artifact_array(
    value: &JsonValue,
    path: &str,
    minimum: usize,
    maximum: usize,
) -> Result<Vec<Artifact>, ManifestError> {
    let array = value
        .as_array()
        .ok_or_else(|| ManifestError::invalid(path, "must be an array"))?;
    if array.len() < minimum || array.len() > maximum {
        return Err(length_error(path, minimum, maximum));
    }
    array
        .iter()
        .enumerate()
        .map(|(index, item)| parse_artifact(item, &format!("{path}[{index}]")))
        .collect()
}

fn parse_derived_artifact(value: &JsonValue, path: &str) -> Result<DerivedArtifact, ManifestError> {
    let object = exact_object(
        value,
        path,
        &[
            "profile",
            "format",
            "derived_from_sha256",
            "entry",
            "files",
            "derivation_attestation",
        ],
        &[
            "profile",
            "format",
            "derived_from_sha256",
            "entry",
            "files",
            "derivation_attestation",
        ],
    )?;
    let profile = match required_string(object, "profile", path)? {
        "web-preview" => Profile::WebPreview,
        "web-runtime" => Profile::WebRuntime,
        _ => return Err(enum_error(format!("{path}.profile"))),
    };
    require_const(object, "format", "jco-esm", path)?;
    let digest = required_string(object, "derived_from_sha256", path)?;
    if !is_sha256(digest) {
        return Err(ManifestError::invalid(
            format!("{path}.derived_from_sha256"),
            "must be 64 lowercase hex digits",
        ));
    }
    let files = parse_artifact_array(
        required(object, "files", path)?,
        &format!("{path}.files"),
        1,
        32,
    )?;
    let entry = parse_artifact(required(object, "entry", path)?, &format!("{path}.entry"))?;
    if files.iter().filter(|file| *file == &entry).count() != 1 {
        return Err(ManifestError::new(
            ProtocolErrorCode::IntegrityFailure,
            format!("{path}.entry"),
            "derived entry must be present byte-for-byte in files",
        ));
    }
    Ok(DerivedArtifact {
        profile,
        derived_from_sha256: digest.to_owned(),
        entry,
        files,
        derivation_attestation: parse_artifact(
            required(object, "derivation_attestation", path)?,
            &format!("{path}.derivation_attestation"),
        )?,
    })
}

fn parse_runtime(value: &JsonValue) -> Result<Runtime, ManifestError> {
    let path = "$.runtime";
    let object = exact_object(
        value,
        path,
        &[
            "contract",
            "wasi",
            "world",
            "required_imports",
            "profiles",
            "platforms",
        ],
        &[
            "contract",
            "wasi",
            "world",
            "required_imports",
            "profiles",
            "platforms",
        ],
    )?;
    let contract = required_string(object, "contract", path)?;
    if contract != CONTRACT_VERSION {
        return Err(ManifestError::schema_invalid(
            ProtocolErrorCode::UnsupportedVersion,
            "$.runtime.contract",
            format!("unsupported contract {contract}"),
        ));
    }
    require_const_version(object, "wasi", WASI_VERSION, "$.runtime.wasi")?;
    let world = match required_string(object, "world", path)? {
        "ui-only-reference" => World::UiOnly,
        "service-only-reference" => World::ServiceOnly,
        "hybrid-reference" => World::Hybrid,
        "web-preview-reference" => World::WebPreview,
        _ => return Err(enum_error("$.runtime.world")),
    };
    let import_values = required_array(object, "required_imports", path)?;
    let mut required_imports = Vec::with_capacity(import_values.len());
    for (index, value) in import_values.iter().enumerate() {
        let interface = value.as_str().ok_or_else(|| {
            ManifestError::invalid(
                format!("$.runtime.required_imports[{index}]"),
                "must be a string",
            )
        })?;
        if !is_app_interface(interface) {
            return Err(ManifestError::schema_invalid(
                ProtocolErrorCode::UnknownInterface,
                format!("$.runtime.required_imports[{index}]"),
                format!("unknown app interface {interface}"),
            ));
        }
        required_imports.push(interface.to_owned());
    }
    unique_by(
        required_imports.iter().map(String::as_str),
        "$.runtime.required_imports",
    )?;

    let profile_values = required_array(object, "profiles", path)?;
    if profile_values.is_empty() || profile_values.len() > 4 {
        return Err(length_error("$.runtime.profiles", 1, 4));
    }
    let mut profiles = Vec::with_capacity(profile_values.len());
    for (index, item) in profile_values.iter().enumerate() {
        profiles.push(parse_profile_support(
            item,
            &format!("$.runtime.profiles[{index}]"),
        )?);
    }
    unique_by(
        profiles.iter().map(|item| item.profile.as_str()),
        "$.runtime.profiles",
    )?;

    let platform_values = required_array(object, "platforms", path)?;
    if platform_values.is_empty() || platform_values.len() > 16 {
        return Err(length_error("$.runtime.platforms", 1, 16));
    }
    let mut platforms = Vec::with_capacity(platform_values.len());
    for (index, item) in platform_values.iter().enumerate() {
        platforms.push(parse_platform(
            item,
            &format!("$.runtime.platforms[{index}]"),
        )?);
    }
    let mut platform_tuples = BTreeSet::new();
    for platform in &platforms {
        for profile in &platform.profiles {
            if !platform_tuples.insert((platform.os.as_str(), platform.arch.as_str(), *profile)) {
                return Err(ManifestError::invalid(
                    "$.runtime.platforms",
                    "OS/architecture/profile tuples must be unique",
                ));
            }
        }
    }
    Ok(Runtime {
        world,
        required_imports,
        profiles,
        platforms,
    })
}

fn parse_profile_support(value: &JsonValue, path: &str) -> Result<ProfileSupport, ManifestError> {
    let object = exact_object(
        value,
        path,
        &[
            "profile",
            "mode",
            "background",
            "artifact_role",
            "degradation",
        ],
        &[
            "profile",
            "mode",
            "background",
            "artifact_role",
            "degradation",
        ],
    )?;
    let profile = parse_profile(
        required_string(object, "profile", path)?,
        &format!("{path}.profile"),
    )?;
    let mode = match required_string(object, "mode", path)? {
        "full" => ProfileMode::Full,
        "degraded" => ProfileMode::Degraded,
        "preview" => ProfileMode::Preview,
        _ => return Err(enum_error(format!("{path}.mode"))),
    };
    let background = match required_string(object, "background", path)? {
        "daemon" => Background::Daemon,
        "process" => Background::Process,
        "foreground-only" => Background::ForegroundOnly,
        "not-applicable" => Background::NotApplicable,
        _ => return Err(enum_error(format!("{path}.background"))),
    };
    let artifact_role = match required_string(object, "artifact_role", path)? {
        "canonical-component" => ArtifactRole::CanonicalComponent,
        "browser-derived" => ArtifactRole::BrowserDerived,
        _ => return Err(enum_error(format!("{path}.artifact_role"))),
    };
    let degradation = bounded_string(object, "degradation", path, 0, 500)?;
    if profile == Profile::WebPreview
        && (mode != ProfileMode::Preview
            || background != Background::ForegroundOnly
            || artifact_role != ArtifactRole::BrowserDerived)
    {
        return Err(ManifestError::schema_invalid(
            ProtocolErrorCode::IncompatibleContract,
            path,
            "web-preview must be preview/browser-derived/foreground-only",
        ));
    }
    if profile == Profile::WebRuntime
        && (background != Background::ForegroundOnly
            || artifact_role != ArtifactRole::BrowserDerived)
    {
        return Err(ManifestError::schema_invalid(
            ProtocolErrorCode::IncompatibleContract,
            path,
            "web-runtime must be browser-derived and foreground-only",
        ));
    }
    Ok(ProfileSupport {
        profile,
        mode,
        background,
        artifact_role,
        degradation,
    })
}

fn parse_platform(value: &JsonValue, path: &str) -> Result<PlatformSupport, ManifestError> {
    let object = exact_object(
        value,
        path,
        &["os", "arch", "profiles"],
        &["os", "arch", "profiles"],
    )?;
    let os = required_string(object, "os", path)?;
    if !matches!(os, "macos" | "windows" | "linux" | "browser") {
        return Err(enum_error(format!("{path}.os")));
    }
    let arch = required_string(object, "arch", path)?;
    if !matches!(arch, "x86-64" | "aarch64" | "wasm32") {
        return Err(enum_error(format!("{path}.arch")));
    }
    if (os == "browser") != (arch == "wasm32") {
        return Err(ManifestError::new(
            ProtocolErrorCode::IncompatibleContract,
            path,
            "browser and wasm32 platform values must be paired",
        ));
    }
    let profiles = parse_profile_array(
        required(object, "profiles", path)?,
        &format!("{path}.profiles"),
    )?;
    Ok(PlatformSupport {
        os: os.to_owned(),
        arch: arch.to_owned(),
        profiles,
    })
}

fn parse_entrypoints(value: &JsonValue) -> Result<Vec<Entrypoint>, ManifestError> {
    let values = value
        .as_array()
        .ok_or_else(|| ManifestError::invalid("$.entrypoints", "must be an array"))?;
    if values.is_empty() || values.len() > 16 {
        return Err(length_error("$.entrypoints", 1, 16));
    }
    let mut output = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        output.push(parse_entrypoint(value, &format!("$.entrypoints[{index}]"))?);
    }
    unique_by(output.iter().map(|item| item.id.as_str()), "$.entrypoints")?;
    Ok(output)
}

fn parse_entrypoint(value: &JsonValue, path: &str) -> Result<Entrypoint, ManifestError> {
    let raw_object = value
        .as_object()
        .ok_or_else(|| ManifestError::invalid(path, "must be an object"))?;
    let kind_string = required_string(raw_object, "kind", path)?;
    let (allowed, required_fields, kind) = match kind_string {
        "launcher-ui" => (
            &["id", "kind", "label", "profiles", "routes", "restoration"][..],
            &["id", "kind", "label", "profiles", "routes", "restoration"][..],
            EntrypointKind::LauncherUi,
        ),
        "service" => (
            &[
                "id",
                "kind",
                "label",
                "profiles",
                "triggers",
                "health_check_interval_seconds",
            ][..],
            &[
                "id",
                "kind",
                "label",
                "profiles",
                "triggers",
                "health_check_interval_seconds",
            ][..],
            EntrypointKind::Service,
        ),
        "settings" => (
            &["id", "kind", "label", "profiles", "schema_export"][..],
            &["id", "kind", "label", "profiles", "schema_export"][..],
            EntrypointKind::Settings,
        ),
        _ => return Err(enum_error(format!("{path}.kind"))),
    };
    let object = exact_object(value, path, allowed, required_fields)?;
    let id = identifier(required_string(object, "id", path)?, &format!("{path}.id"))?;
    let label = bounded_string(object, "label", path, 1, 80)?;
    let profiles = parse_profile_array(
        required(object, "profiles", path)?,
        &format!("{path}.profiles"),
    )?;
    let details = match kind {
        EntrypointKind::LauncherUi => {
            let routes_path = format!("{path}.routes");
            let routes = exact_object(
                required(object, "routes", path)?,
                &routes_path,
                &["initial", "allowed"],
                &["initial", "allowed"],
            )?;
            let initial = identifier(
                required_string(routes, "initial", &routes_path)?,
                &format!("{routes_path}.initial"),
            )?;
            let allowed_values = required_array(routes, "allowed", &routes_path)?;
            if allowed_values.is_empty() {
                return Err(length_error(
                    format!("{routes_path}.allowed"),
                    1,
                    usize::MAX,
                ));
            }
            let mut allowed_routes = Vec::with_capacity(allowed_values.len());
            for (index, route) in allowed_values.iter().enumerate() {
                let route = route.as_str().ok_or_else(|| {
                    ManifestError::invalid(
                        format!("{routes_path}.allowed[{index}]"),
                        "must be a string",
                    )
                })?;
                allowed_routes.push(identifier(
                    route,
                    &format!("{routes_path}.allowed[{index}]"),
                )?);
            }
            unique_by(
                allowed_routes.iter().map(String::as_str),
                &format!("{routes_path}.allowed"),
            )?;
            if !allowed_routes.iter().any(|route| route == &initial) {
                return Err(ManifestError::new(
                    ProtocolErrorCode::IncompatibleContract,
                    format!("{routes_path}.initial"),
                    "initial route must appear in allowed routes",
                ));
            }
            let restoration = match required_string(object, "restoration", path)? {
                "none" => Restoration::None,
                "route-only" => Restoration::RouteOnly,
                "safe-fields" => Restoration::SafeFields,
                _ => return Err(enum_error(format!("{path}.restoration"))),
            };
            EntrypointDetails::Launcher {
                initial_route: initial,
                allowed_routes,
                restoration,
            }
        }
        EntrypointKind::Service => {
            let triggers = required_array(object, "triggers", path)?;
            if triggers.is_empty() {
                return Err(length_error(format!("{path}.triggers"), 1, usize::MAX));
            }
            let mut names = Vec::with_capacity(triggers.len());
            for (index, trigger) in triggers.iter().enumerate() {
                let trigger = trigger.as_str().ok_or_else(|| {
                    ManifestError::invalid(format!("{path}.triggers[{index}]"), "must be a string")
                })?;
                if !matches!(trigger, "on-enable" | "scheduler" | "manual") {
                    return Err(enum_error(format!("{path}.triggers[{index}]")));
                }
                names.push(trigger);
            }
            unique_by(names.iter().copied(), &format!("{path}.triggers"))?;
            let health_check_interval_seconds = bounded_u64(
                object,
                "health_check_interval_seconds",
                path,
                10,
                86_400,
                ProtocolErrorCode::InvalidManifest,
            )?;
            EntrypointDetails::Service {
                triggers: names.into_iter().map(str::to_owned).collect(),
                health_check_interval_seconds,
            }
        }
        EntrypointKind::Settings => {
            require_const(object, "schema_export", "get-settings-schema", path)?;
            EntrypointDetails::Settings {
                schema_export: "get-settings-schema".to_owned(),
            }
        }
    };
    Ok(Entrypoint {
        id,
        kind,
        label,
        profiles,
        details,
    })
}

fn parse_capabilities(value: &JsonValue) -> Result<Vec<Capability>, ManifestError> {
    let values = value
        .as_array()
        .ok_or_else(|| ManifestError::invalid("$.capabilities", "must be an array"))?;
    if values.len() > 32 {
        return Err(length_error("$.capabilities", 0, 32));
    }
    let mut output = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        output.push(parse_capability(
            value,
            &format!("$.capabilities[{index}]"),
        )?);
    }
    unique_by(
        output.iter().map(|item| item.interface.as_str()),
        "$.capabilities",
    )?;
    Ok(output)
}

fn parse_capability(value: &JsonValue, path: &str) -> Result<Capability, ManifestError> {
    let object = exact_object(
        value,
        path,
        &[
            "interface",
            "necessity",
            "grant",
            "reason",
            "scope",
            "profiles",
        ],
        &[
            "interface",
            "necessity",
            "grant",
            "reason",
            "scope",
            "profiles",
        ],
    )?;
    let interface = required_string(object, "interface", path)?;
    if !is_app_interface(interface) {
        return Err(ManifestError::schema_invalid(
            ProtocolErrorCode::UnknownInterface,
            format!("{path}.interface"),
            format!("unknown app interface {interface}"),
        ));
    }
    let necessity = match required_string(object, "necessity", path)? {
        "required" => Necessity::Required,
        "degradable" => Necessity::Degradable,
        _ => return Err(enum_error(format!("{path}.necessity"))),
    };
    let grant = match required_string(object, "grant", path)? {
        "automatic" => Grant::Automatic,
        "user" => Grant::User,
        _ => return Err(enum_error(format!("{path}.grant"))),
    };
    let reason = bounded_string(object, "reason", path, 1, 500)?;
    let scope_value = required(object, "scope", path)?;
    let scope = parse_capability_scope(scope_value, &format!("{path}.scope"))?;
    let profile_values = required_array(object, "profiles", path)?;
    if profile_values.is_empty() || profile_values.len() > 4 {
        return Err(length_error(format!("{path}.profiles"), 1, 4));
    }
    let mut profiles = Vec::with_capacity(profile_values.len());
    for (index, value) in profile_values.iter().enumerate() {
        let profile_path = format!("{path}.profiles[{index}]");
        let profile_object = exact_object(
            value,
            &profile_path,
            &["profile", "availability", "behavior"],
            &["profile", "availability", "behavior"],
        )?;
        let profile = parse_profile(
            required_string(profile_object, "profile", &profile_path)?,
            &format!("{profile_path}.profile"),
        )?;
        let availability = match required_string(profile_object, "availability", &profile_path)? {
            "native" => Availability::Native,
            "brokered" => Availability::Brokered,
            "mock" => Availability::Mock,
            "denied" => Availability::Denied,
            "unavailable" => Availability::Unavailable,
            _ => return Err(enum_error(format!("{profile_path}.availability"))),
        };
        profiles.push(CapabilityProfile {
            profile,
            availability,
            behavior: bounded_string(profile_object, "behavior", &profile_path, 1, 500)?,
        });
    }
    unique_by(
        profiles.iter().map(|item| item.profile.as_str()),
        &format!("{path}.profiles"),
    )?;
    Ok(Capability {
        interface: interface.to_owned(),
        necessity,
        grant,
        reason,
        scope,
        profiles,
    })
}

fn parse_capability_scope(value: &JsonValue, path: &str) -> Result<CapabilityScope, ManifestError> {
    let object = exact_object(
        value,
        path,
        &[
            "maximum_requests_per_hour",
            "maximum_schedules",
            "maximum_storage_bytes",
            "methods",
            "metrics",
            "origins",
        ],
        &[],
    )?;
    let maximum_requests_per_hour = optional_bounded_u64(
        object,
        "maximum_requests_per_hour",
        path,
        0,
        10_000,
        ProtocolErrorCode::InvalidManifest,
    )?;
    let maximum_schedules = optional_bounded_u64(
        object,
        "maximum_schedules",
        path,
        0,
        128,
        ProtocolErrorCode::ResourceLimit,
    )?;
    let maximum_storage_bytes = optional_bounded_u64(
        object,
        "maximum_storage_bytes",
        path,
        0,
        10_485_760,
        ProtocolErrorCode::ResourceLimit,
    )?;
    let methods = parse_enum_array(
        object,
        "methods",
        path,
        &["GET", "POST", "PUT", "PATCH", "DELETE"],
    )?;
    let metrics = parse_enum_array(
        object,
        "metrics",
        path,
        &[
            "cpu-temperature-celsius",
            "cpu-utilization-percent",
            "memory-utilization-percent",
        ],
    )?;
    let mut parsed_origins = Vec::new();
    if let Some(origins) = optional_array(object, "origins", path)? {
        let mut seen = BTreeSet::new();
        for (index, value) in origins.iter().enumerate() {
            let origin = value.as_str().ok_or_else(|| {
                ManifestError::invalid(format!("{path}.origins[{index}]"), "must be a string")
            })?;
            if !is_https_origin(origin) {
                return Err(ManifestError::invalid(
                    format!("{path}.origins[{index}]"),
                    "must be an HTTPS origin without path, query, fragment, or whitespace",
                ));
            }
            if !seen.insert(origin) {
                return Err(ManifestError::invalid(
                    format!("{path}.origins"),
                    "items must be unique",
                ));
            }
            parsed_origins.push(origin.to_owned());
        }
    }
    Ok(CapabilityScope {
        maximum_requests_per_hour,
        maximum_schedules,
        maximum_storage_bytes,
        methods,
        metrics,
        origins: parsed_origins,
    })
}

fn parse_resources(value: &JsonValue) -> Result<Resources, ManifestError> {
    let path = "$.resources";
    let fields = &[
        "linear_memory_bytes",
        "event_wall_time_ms",
        "health_migration_wall_time_ms",
        "output_bytes",
        "stored_data_bytes",
        "durable_schedules",
        "log_bytes_per_day",
    ];
    let object = exact_object(value, path, fields, fields)?;
    Ok(Resources {
        linear_memory_bytes: bounded_u64(
            object,
            "linear_memory_bytes",
            path,
            1_048_576,
            67_108_864,
            ProtocolErrorCode::ResourceLimit,
        )?,
        event_wall_time_ms: bounded_u64(
            object,
            "event_wall_time_ms",
            path,
            1,
            250,
            ProtocolErrorCode::ResourceLimit,
        )?,
        health_migration_wall_time_ms: bounded_u64(
            object,
            "health_migration_wall_time_ms",
            path,
            1,
            2_000,
            ProtocolErrorCode::ResourceLimit,
        )?,
        output_bytes: bounded_u64(
            object,
            "output_bytes",
            path,
            1,
            262_144,
            ProtocolErrorCode::ResourceLimit,
        )?,
        stored_data_bytes: bounded_u64(
            object,
            "stored_data_bytes",
            path,
            0,
            10_485_760,
            ProtocolErrorCode::ResourceLimit,
        )?,
        durable_schedules: bounded_u64(
            object,
            "durable_schedules",
            path,
            0,
            128,
            ProtocolErrorCode::ResourceLimit,
        )?,
        log_bytes_per_day: bounded_u64(
            object,
            "log_bytes_per_day",
            path,
            0,
            1_048_576,
            ProtocolErrorCode::ResourceLimit,
        )?,
    })
}

fn parse_state(value: &JsonValue) -> Result<StateContract, ManifestError> {
    let path = "$.state";
    let fields = &["schema", "migratable_from_min", "migratable_from_max"];
    let object = exact_object(value, path, fields, fields)?;
    let schema = bounded_u32(object, "schema", path)?;
    let migratable_from_min = bounded_u32(object, "migratable_from_min", path)?;
    let migratable_from_max = bounded_u32(object, "migratable_from_max", path)?;
    if migratable_from_min > schema || schema > migratable_from_max {
        return Err(ManifestError::new(
            ProtocolErrorCode::IncompatibleContract,
            path,
            "state migration range must satisfy min <= schema <= max",
        ));
    }
    Ok(StateContract {
        schema,
        migratable_from_min,
        migratable_from_max,
    })
}

fn parse_lifecycle(value: &JsonValue) -> Result<LifecycleContract, ManifestError> {
    let path = "$.lifecycle";
    let object = exact_object(
        value,
        path,
        &["disable", "uninstall"],
        &["disable", "uninstall"],
    )?;
    let disable_path = "$.lifecycle.disable";
    let disable_fields = &[
        "retains_package",
        "retains_state",
        "stops_services",
        "cancels_schedules",
    ];
    let disable = exact_object(
        required(object, "disable", path)?,
        disable_path,
        disable_fields,
        disable_fields,
    )?;
    for field in disable_fields {
        if required(disable, field, disable_path)?.as_bool() != Some(true) {
            return Err(ManifestError::invalid(
                format!("{disable_path}.{field}"),
                "must be true",
            ));
        }
    }
    let uninstall_path = "$.lifecycle.uninstall";
    let uninstall = exact_object(
        required(object, "uninstall", path)?,
        uninstall_path,
        &["allowed_data_dispositions", "default_data_disposition"],
        &["allowed_data_dispositions", "default_data_disposition"],
    )?;
    let values = required_array(uninstall, "allowed_data_dispositions", uninstall_path)?;
    if values.is_empty() {
        return Err(length_error(
            "$.lifecycle.uninstall.allowed_data_dispositions",
            1,
            usize::MAX,
        ));
    }
    let mut allowed_data_dispositions = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        let string = value.as_str().ok_or_else(|| {
            ManifestError::invalid(
                format!("$.lifecycle.uninstall.allowed_data_dispositions[{index}]"),
                "must be a string",
            )
        })?;
        allowed_data_dispositions.push(parse_disposition(
            string,
            &format!("$.lifecycle.uninstall.allowed_data_dispositions[{index}]"),
        )?);
    }
    if allowed_data_dispositions
        .iter()
        .collect::<BTreeSet<_>>()
        .len()
        != allowed_data_dispositions.len()
    {
        return Err(ManifestError::invalid(
            "$.lifecycle.uninstall.allowed_data_dispositions",
            "items must be unique",
        ));
    }
    let default_data_disposition = parse_disposition(
        required_string(uninstall, "default_data_disposition", uninstall_path)?,
        "$.lifecycle.uninstall.default_data_disposition",
    )?;
    if !allowed_data_dispositions.contains(&default_data_disposition) {
        return Err(ManifestError::new(
            ProtocolErrorCode::IncompatibleContract,
            "$.lifecycle.uninstall.default_data_disposition",
            "default disposition must be allowed",
        ));
    }
    Ok(LifecycleContract {
        allowed_data_dispositions,
        default_data_disposition,
    })
}

fn parse_source(value: &JsonValue) -> Result<SourceContract, ManifestError> {
    let path = "$.source";
    let fields = &[
        "language",
        "revision",
        "cargo_lock_sha256",
        "builder_image_digest",
    ];
    let object = exact_object(value, path, fields, fields)?;
    require_const(object, "language", "rust", path)?;
    let revision = bounded_string(object, "revision", path, 1, 128)?;
    let lock = required_string(object, "cargo_lock_sha256", path)?;
    if !is_sha256(lock) {
        return Err(ManifestError::invalid(
            "$.source.cargo_lock_sha256",
            "must be 64 lowercase hex digits",
        ));
    }
    let image = required_string(object, "builder_image_digest", path)?;
    if image.len() != 71 || !image.starts_with("sha256:") || !is_sha256(&image[7..]) {
        return Err(ManifestError::invalid(
            "$.source.builder_image_digest",
            "must be sha256:<64 lowercase hex digits>",
        ));
    }
    Ok(SourceContract {
        revision,
        cargo_lock_sha256: lock.to_owned(),
        builder_image_digest: image.to_owned(),
    })
}

fn parse_license(value: &JsonValue) -> Result<LicenseContract, ManifestError> {
    let path = "$.license";
    let object = exact_object(value, path, &["spdx_expression"], &["spdx_expression"])?;
    Ok(LicenseContract {
        spdx_expression: bounded_string(object, "spdx_expression", path, 1, 200)?,
    })
}

fn parse_privacy(value: &JsonValue) -> Result<PrivacyContract, ManifestError> {
    let path = "$.privacy";
    let object = exact_object(
        value,
        path,
        &["stores_personal_data", "network_access"],
        &["stores_personal_data", "network_access"],
    )?;
    let stores_personal_data = required(object, "stores_personal_data", path)?
        .as_bool()
        .ok_or_else(|| {
            ManifestError::invalid("$.privacy.stores_personal_data", "must be a boolean")
        })?;
    let network_access = match required_string(object, "network_access", path)? {
        "none" => NetworkAccess::None,
        "scoped-broker" => NetworkAccess::ScopedBroker,
        _ => return Err(enum_error("$.privacy.network_access")),
    };
    Ok(PrivacyContract {
        stores_personal_data,
        network_access,
    })
}

fn parse_verification(value: &JsonValue) -> Result<VerificationContract, ManifestError> {
    let path = "$.verification";
    let object = exact_object(
        value,
        path,
        &["status", "revocation"],
        &["status", "revocation"],
    )?;
    let status = match required_string(object, "status", path)? {
        "fixture" => VerificationStatus::Fixture,
        "unverified" => VerificationStatus::Unverified,
        "verified" => VerificationStatus::Verified,
        _ => return Err(enum_error("$.verification.status")),
    };
    let revocation = match required_string(object, "revocation", path)? {
        "not-revoked" => RevocationStatus::NotRevoked,
        "revoked" => RevocationStatus::Revoked,
        _ => return Err(enum_error("$.verification.revocation")),
    };
    Ok(VerificationContract { status, revocation })
}

fn validate_cross_contract(
    app: &App,
    artifacts: &Artifacts,
    runtime: &Runtime,
    entrypoints: &[Entrypoint],
    capabilities: &[Capability],
    resources: &Resources,
    state: &StateContract,
    lifecycle: &LifecycleContract,
) -> Result<(), ManifestError> {
    let launcher_count = entrypoints
        .iter()
        .filter(|entrypoint| entrypoint.kind == EntrypointKind::LauncherUi)
        .count();
    let service_count = entrypoints
        .iter()
        .filter(|entrypoint| entrypoint.kind == EntrypointKind::Service)
        .count();
    match app.kind {
        AppKind::Ui
            if launcher_count == 0 || service_count != 0 || runtime.world != World::UiOnly =>
        {
            return Err(ManifestError::schema_invalid(
                ProtocolErrorCode::IncompatibleContract,
                "$.app.kind",
                "ui packages require a launcher entrypoint, no service entrypoint, and ui-only-reference world",
            ));
        }
        AppKind::Service
            if service_count == 0
                || launcher_count != 0
                || !matches!(runtime.world, World::ServiceOnly | World::WebPreview) =>
        {
            return Err(ManifestError::schema_invalid(
                ProtocolErrorCode::IncompatibleContract,
                "$.app.kind",
                "service packages require a service entrypoint, no launcher entrypoint, and a service world",
            ));
        }
        AppKind::Hybrid
            if service_count == 0 || launcher_count == 0 || runtime.world != World::Hybrid =>
        {
            return Err(ManifestError::schema_invalid(
                ProtocolErrorCode::IncompatibleContract,
                "$.app.kind",
                "hybrid packages require launcher and service entrypoints with hybrid-reference world",
            ));
        }
        _ => {}
    }

    let declared: BTreeSet<_> = capabilities
        .iter()
        .map(|capability| capability.interface.as_str())
        .collect();
    let required: BTreeSet<_> = runtime
        .required_imports
        .iter()
        .map(String::as_str)
        .collect();
    if declared != required {
        return Err(ManifestError::new(
            ProtocolErrorCode::IncompatibleContract,
            "$.runtime.required_imports",
            "required imports and declared capability interfaces must match exactly",
        ));
    }
    if reconcile_world_imports(runtime.world, &runtime.required_imports).is_err() {
        return Err(ManifestError::new(
            ProtocolErrorCode::IncompatibleContract,
            "$.runtime.required_imports",
            "required imports do not match the selected WIT world",
        ));
    }

    let runtime_profiles: BTreeSet<_> = runtime
        .profiles
        .iter()
        .map(|support| support.profile)
        .collect();
    for support in &runtime.profiles {
        let browser_profile = matches!(support.profile, Profile::WebPreview | Profile::WebRuntime);
        let browser_role = support.artifact_role == ArtifactRole::BrowserDerived;
        if browser_profile != browser_role {
            return Err(ManifestError::new(
                ProtocolErrorCode::IncompatibleContract,
                "$.runtime.profiles",
                "browser profiles require browser-derived artifacts and desktop/headless require the canonical component",
            ));
        }
    }
    for platform in &runtime.platforms {
        for profile in &platform.profiles {
            if !runtime_profiles.contains(profile) {
                return Err(ManifestError::new(
                    ProtocolErrorCode::IncompatibleContract,
                    "$.runtime.platforms",
                    "platform profile is absent from runtime profiles",
                ));
            }
            let browser_platform = platform.os == "browser" && platform.arch == "wasm32";
            let browser_profile = matches!(profile, Profile::WebPreview | Profile::WebRuntime);
            if browser_platform != browser_profile {
                return Err(ManifestError::new(
                    ProtocolErrorCode::IncompatibleContract,
                    "$.runtime.platforms",
                    "browser profiles must use browser/wasm32 and non-browser profiles must not",
                ));
            }
        }
    }
    for entrypoint in entrypoints {
        if entrypoint
            .profiles
            .iter()
            .any(|profile| !runtime_profiles.contains(profile))
        {
            return Err(ManifestError::new(
                ProtocolErrorCode::IncompatibleContract,
                "$.entrypoints",
                "entrypoint profile is absent from runtime profiles",
            ));
        }
    }
    for capability in capabilities {
        let profiles: BTreeSet<_> = capability
            .profiles
            .iter()
            .map(|support| support.profile)
            .collect();
        if profiles != runtime_profiles {
            return Err(ManifestError::new(
                ProtocolErrorCode::IncompatibleContract,
                "$.capabilities",
                "each capability must explicitly describe every runtime profile",
            ));
        }
        for support in &capability.profiles {
            if support.profile == Profile::WebPreview
                && matches!(
                    capability.interface.as_str(),
                    "vibapp:experimental-v0/scheduler@0.0.1"
                        | "vibapp:experimental-v0/notification@0.0.1"
                        | "vibapp:experimental-v0/system-metrics@0.0.1"
                        | "vibapp:experimental-v0/http@0.0.1"
                )
                && !matches!(
                    support.availability,
                    Availability::Mock | Availability::Denied | Availability::Unavailable
                )
            {
                return Err(ManifestError::new(
                    ProtocolErrorCode::IncompatibleContract,
                    "$.capabilities",
                    "web-preview hardware and background capabilities must be mock, denied or unavailable",
                ));
            }
        }
        if capability
            .scope
            .maximum_storage_bytes
            .is_some_and(|maximum| maximum > resources.stored_data_bytes)
            || capability
                .scope
                .maximum_schedules
                .is_some_and(|maximum| maximum > resources.durable_schedules)
        {
            return Err(ManifestError::new(
                ProtocolErrorCode::ResourceLimit,
                "$.capabilities",
                "capability scope must not exceed the manifest resource request",
            ));
        }
    }

    let expected_derivations: BTreeSet<_> = runtime
        .profiles
        .iter()
        .filter_map(|support| {
            matches!(support.profile, Profile::WebPreview | Profile::WebRuntime)
                .then_some(support.profile)
        })
        .collect();
    let actual_derivations: BTreeSet<_> = artifacts
        .browser_derivations
        .iter()
        .map(|derivation| derivation.profile)
        .collect();
    if actual_derivations != expected_derivations {
        return Err(ManifestError::new(
            ProtocolErrorCode::IntegrityFailure,
            "$.artifacts.browser_derivations",
            "browser derivations must match browser runtime profiles exactly",
        ));
    }

    for support in &runtime.profiles {
        let platform_has_profile = runtime
            .platforms
            .iter()
            .any(|platform| platform.profiles.contains(&support.profile));
        if !platform_has_profile {
            return Err(ManifestError::new(
                ProtocolErrorCode::IncompatibleContract,
                "$.runtime.platforms",
                format!("profile {} has no platform", support.profile.as_str()),
            ));
        }
        if matches!(support.profile, Profile::WebPreview | Profile::WebRuntime) {
            let derivation = artifacts
                .browser_derivations
                .iter()
                .find(|derived| derived.profile == support.profile)
                .ok_or_else(|| {
                    ManifestError::new(
                        ProtocolErrorCode::IntegrityFailure,
                        "$.artifacts.browser_derivations",
                        format!("missing derivation for {}", support.profile.as_str()),
                    )
                })?;
            if derivation.derived_from_sha256 != artifacts.canonical_component.sha256 {
                return Err(ManifestError::new(
                    ProtocolErrorCode::IntegrityFailure,
                    "$.artifacts.browser_derivations",
                    "browser derivation must bind the canonical component digest",
                ));
            }
        }
    }
    debug_assert!(state.migratable_from_min <= state.schema);
    debug_assert!(state.schema <= state.migratable_from_max);
    if !lifecycle
        .allowed_data_dispositions
        .contains(&lifecycle.default_data_disposition)
    {
        return Err(ManifestError::new(
            ProtocolErrorCode::IncompatibleContract,
            "$.lifecycle.uninstall",
            "default data disposition is not allowed",
        ));
    }
    Ok(())
}

type ObjectEntries = [(String, JsonValue)];

fn exact_object<'a>(
    value: &'a JsonValue,
    path: &str,
    allowed: &[&str],
    required_fields: &[&str],
) -> Result<&'a ObjectEntries, ManifestError> {
    let object = value
        .as_object()
        .ok_or_else(|| ManifestError::invalid(path, "must be an object"))?;
    for (key, _) in object {
        if !allowed.contains(&key.as_str()) {
            return Err(ManifestError::invalid(
                format!("{path}.{key}"),
                "unknown property",
            ));
        }
    }
    for key in required_fields {
        if !has(object, key) {
            return Err(ManifestError::invalid(
                format!("{path}.{key}"),
                "required property is missing",
            ));
        }
    }
    Ok(object)
}

fn has(object: &ObjectEntries, key: &str) -> bool {
    object.iter().any(|(candidate, _)| candidate == key)
}

fn required<'a>(
    object: &'a ObjectEntries,
    key: &str,
    path: &str,
) -> Result<&'a JsonValue, ManifestError> {
    object
        .iter()
        .find_map(|(candidate, value)| (candidate == key).then_some(value))
        .ok_or_else(|| {
            ManifestError::invalid(format!("{path}.{key}"), "required property is missing")
        })
}

fn required_string<'a>(
    object: &'a ObjectEntries,
    key: &str,
    path: &str,
) -> Result<&'a str, ManifestError> {
    required(object, key, path)?
        .as_str()
        .ok_or_else(|| ManifestError::invalid(format!("{path}.{key}"), "must be a string"))
}

fn required_array<'a>(
    object: &'a ObjectEntries,
    key: &str,
    path: &str,
) -> Result<&'a [JsonValue], ManifestError> {
    required(object, key, path)?
        .as_array()
        .ok_or_else(|| ManifestError::invalid(format!("{path}.{key}"), "must be an array"))
}

fn optional_array<'a>(
    object: &'a ObjectEntries,
    key: &str,
    path: &str,
) -> Result<Option<&'a [JsonValue]>, ManifestError> {
    match object
        .iter()
        .find_map(|(candidate, value)| (candidate == key).then_some(value))
    {
        Some(value) => value
            .as_array()
            .map(Some)
            .ok_or_else(|| ManifestError::invalid(format!("{path}.{key}"), "must be an array")),
        None => Ok(None),
    }
}

fn bounded_string(
    object: &ObjectEntries,
    key: &str,
    path: &str,
    minimum: usize,
    maximum: usize,
) -> Result<String, ManifestError> {
    let value = required_string(object, key, path)?;
    let length = value.chars().count();
    if length < minimum || length > maximum {
        return Err(length_error(format!("{path}.{key}"), minimum, maximum));
    }
    Ok(value.to_owned())
}

fn bounded_u64(
    object: &ObjectEntries,
    key: &str,
    path: &str,
    minimum: u64,
    maximum: u64,
    code: ProtocolErrorCode,
) -> Result<u64, ManifestError> {
    let value = required(object, key, path)?
        .as_i64()
        .ok_or_else(|| ManifestError::invalid(format!("{path}.{key}"), "must be an integer"))?;
    let unsigned = u64::try_from(value).map_err(|_| {
        ManifestError::schema_invalid(
            code,
            format!("{path}.{key}"),
            format!("must be in {minimum}..={maximum}"),
        )
    })?;
    if unsigned < minimum || unsigned > maximum {
        return Err(ManifestError::schema_invalid(
            code,
            format!("{path}.{key}"),
            format!("must be in {minimum}..={maximum}"),
        ));
    }
    Ok(unsigned)
}

fn optional_bounded_u64(
    object: &ObjectEntries,
    key: &str,
    path: &str,
    minimum: u64,
    maximum: u64,
    code: ProtocolErrorCode,
) -> Result<Option<u64>, ManifestError> {
    if has(object, key) {
        bounded_u64(object, key, path, minimum, maximum, code).map(Some)
    } else {
        Ok(None)
    }
}

fn bounded_u32(object: &ObjectEntries, key: &str, path: &str) -> Result<u32, ManifestError> {
    let value = bounded_u64(
        object,
        key,
        path,
        1,
        u64::from(u32::MAX),
        ProtocolErrorCode::InvalidManifest,
    )?;
    u32::try_from(value)
        .map_err(|_| ManifestError::invalid(format!("{path}.{key}"), "must fit u32"))
}

fn require_const(
    object: &ObjectEntries,
    key: &str,
    expected: &str,
    path: &str,
) -> Result<(), ManifestError> {
    let actual = required_string(object, key, path)?;
    if actual != expected {
        return Err(ManifestError::invalid(
            format!("{path}.{key}"),
            format!("must equal {expected}"),
        ));
    }
    Ok(())
}

fn require_const_version(
    object: &ObjectEntries,
    key: &str,
    expected: &str,
    path: &str,
) -> Result<(), ManifestError> {
    let actual = required_string(
        object,
        key,
        path.rsplit_once('.').map_or("$", |(parent, _)| parent),
    )?;
    if actual != expected {
        return Err(ManifestError::schema_invalid(
            ProtocolErrorCode::UnsupportedVersion,
            path,
            format!("expected {expected}, found {actual}"),
        ));
    }
    Ok(())
}

fn parse_profile(value: &str, path: &str) -> Result<Profile, ManifestError> {
    match value {
        "desktop" => Ok(Profile::Desktop),
        "web-preview" => Ok(Profile::WebPreview),
        "web-runtime" => Ok(Profile::WebRuntime),
        "headless" => Ok(Profile::Headless),
        _ => Err(enum_error(path)),
    }
}

fn parse_profile_array(value: &JsonValue, path: &str) -> Result<Vec<Profile>, ManifestError> {
    let values = value
        .as_array()
        .ok_or_else(|| ManifestError::invalid(path, "must be an array"))?;
    if values.is_empty() {
        return Err(length_error(path, 1, usize::MAX));
    }
    let mut profiles = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        let string = value.as_str().ok_or_else(|| {
            ManifestError::invalid(format!("{path}[{index}]"), "must be a string")
        })?;
        profiles.push(parse_profile(string, &format!("{path}[{index}]"))?);
    }
    if profiles.iter().collect::<BTreeSet<_>>().len() != profiles.len() {
        return Err(ManifestError::invalid(path, "items must be unique"));
    }
    Ok(profiles)
}

fn parse_disposition(value: &str, path: &str) -> Result<DataDisposition, ManifestError> {
    match value {
        "delete" => Ok(DataDisposition::Delete),
        "retain" => Ok(DataDisposition::Retain),
        "export-then-delete" => Ok(DataDisposition::ExportThenDelete),
        _ => Err(enum_error(path)),
    }
}

fn parse_enum_array(
    object: &ObjectEntries,
    key: &str,
    path: &str,
    allowed: &[&str],
) -> Result<Vec<String>, ManifestError> {
    let mut parsed = Vec::new();
    if let Some(values) = optional_array(object, key, path)? {
        let mut seen = BTreeSet::new();
        for (index, value) in values.iter().enumerate() {
            let string = value.as_str().ok_or_else(|| {
                ManifestError::invalid(format!("{path}.{key}[{index}]"), "must be a string")
            })?;
            if !allowed.contains(&string) {
                return Err(enum_error(format!("{path}.{key}[{index}]")));
            }
            if !seen.insert(string) {
                return Err(ManifestError::invalid(
                    format!("{path}.{key}"),
                    "items must be unique",
                ));
            }
            parsed.push(string.to_owned());
        }
    }
    Ok(parsed)
}

fn unique_by<'a>(values: impl Iterator<Item = &'a str>, path: &str) -> Result<(), ManifestError> {
    let mut seen = BTreeSet::new();
    for value in values {
        if !seen.insert(value) {
            return Err(ManifestError::invalid(path, "items must be unique"));
        }
    }
    Ok(())
}

fn identifier(value: &str, path: &str) -> Result<String, ManifestError> {
    if value.is_empty()
        || value.len() > 128
        || !value.is_ascii()
        || !value
            .as_bytes()
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase())
        || value
            .split(|character| character == '.' || character == '-')
            .any(|segment| {
                segment.is_empty()
                    || !segment
                        .bytes()
                        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            })
    {
        return Err(ManifestError::invalid(
            path,
            "must be a lowercase dotted identifier",
        ));
    }
    Ok(value.to_owned())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_artifact_path(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 240
        && !value.starts_with('/')
        && !value.contains('\\')
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'/' | b'-'))
        && !value
            .split('/')
            .any(|segment| segment.is_empty() || matches!(segment, "." | ".."))
}

fn is_https_origin(value: &str) -> bool {
    let Some(authority) = value.strip_prefix("https://") else {
        return false;
    };
    !authority.is_empty()
        && !authority.chars().any(char::is_whitespace)
        && !authority
            .chars()
            .any(|character| matches!(character, '/' | '?' | '#'))
}

fn is_semver(value: &str) -> bool {
    let core_end = value
        .find(|character| character == '-' || character == '+')
        .unwrap_or(value.len());
    let core = &value[..core_end];
    let mut parts = core.split('.');
    let valid_numeric = |part: &str| {
        !part.is_empty()
            && part.bytes().all(|byte| byte.is_ascii_digit())
            && (part == "0" || !part.starts_with('0'))
    };
    if !matches!(
        (parts.next(), parts.next(), parts.next(), parts.next()),
        (Some(major), Some(minor), Some(patch), None)
            if valid_numeric(major) && valid_numeric(minor) && valid_numeric(patch)
    ) {
        return false;
    }
    let suffix = &value[core_end..];
    if suffix.is_empty() {
        return true;
    }
    let mut remainder = suffix;
    if let Some(pre) = remainder.strip_prefix('-') {
        let end = pre.find('+').unwrap_or(pre.len());
        if !valid_semver_identifiers(&pre[..end]) {
            return false;
        }
        remainder = &pre[end..];
    }
    if let Some(build) = remainder.strip_prefix('+') {
        valid_semver_identifiers(build)
    } else {
        remainder.is_empty()
    }
}

fn valid_semver_identifiers(value: &str) -> bool {
    !value.is_empty()
        && value.split('.').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
}

fn enum_error(path: impl Into<String>) -> ManifestError {
    ManifestError::invalid(path, "value is outside the closed enum")
}

fn length_error(path: impl Into<String>, minimum: usize, maximum: usize) -> ManifestError {
    ManifestError::invalid(path, format!("length must be in {minimum}..={maximum}"))
}
