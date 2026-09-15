use vibapp_protocol::json::{JsonErrorKind, parse_json};
use vibapp_protocol::wit_catalog::{APP_INTERFACES, imports_for_world, reconcile_world_imports};
use vibapp_protocol::{
    AdmissionDecision, AdmissionRequest, AppKind, ConformanceCatalog, DataDisposition, HostTarget,
    LauncherOperation, LifecycleError, LifecycleState, OutputBounds, PackageOperation,
    PackageStatus, Profile, ProtocolErrorCode, SessionId, SurfaceId, SurfaceOperation,
    SurfaceStatus, ViewNode, ViewShape, admit, apply_package_operation, apply_session_operation,
    apply_surface_operation, list_apps, open_session, parse_manifest, validate_output_size,
    validate_view_shape,
};

const UI_MANIFEST: &str =
    include_str!("../../../fixtures/conformance/experimental-v0/manifests/ui-only.valid.json");
const SERVICE_MANIFEST: &str =
    include_str!("../../../fixtures/conformance/experimental-v0/manifests/service-only.valid.json");
const HYBRID_MANIFEST: &str =
    include_str!("../../../fixtures/conformance/experimental-v0/manifests/hybrid.valid.json");
const VECTORS: &str = include_str!("../../../fixtures/conformance/experimental-v0/vectors.json");
const WIT: &str = include_str!("../../../wit/experimental-v0/contract.wit");
const MANIFEST_SCHEMA: &str = include_str!("../../../schemas/manifest.experimental-v0.schema.json");

fn manifest_or_panic(input: &str) -> vibapp_protocol::Manifest {
    match parse_manifest(input) {
        Ok(manifest) => manifest,
        Err(error) => panic!("manifest should validate: {error:?}"),
    }
}

#[test]
fn strict_json_rejects_duplicate_keys_floats_and_trailing_data() {
    let duplicate = parse_json(r#"{"a":1,"a":2}"#);
    assert!(
        matches!(duplicate, Err(error) if matches!(error.kind, JsonErrorKind::DuplicateKey(_)))
    );
    assert!(matches!(
        parse_json(r#"{"a":1.0}"#),
        Err(error) if error.kind == JsonErrorKind::NonIntegralNumber
    ));
    assert!(matches!(
        parse_json(r#"{"a":1} false"#),
        Err(error) if error.kind == JsonErrorKind::TrailingData
    ));
}

#[test]
fn canonical_json_is_stable_and_uses_utf16_key_order() {
    let value = match parse_json(r#"{"z":0,"\u20ac":1,"\ud83d\ude00":2,"a":3}"#) {
        Ok(value) => value,
        Err(error) => panic!("JSON should parse: {error:?}"),
    };
    assert_eq!(
        value.to_canonical_json(),
        "{\"a\":3,\"z\":0,\"€\":1,\"😀\":2}"
    );
    let reparsed = match parse_json(&value.to_canonical_json()) {
        Ok(value) => value,
        Err(error) => panic!("canonical JSON should parse: {error:?}"),
    };
    assert_eq!(value, reparsed);
}

#[test]
fn all_canonical_manifests_validate_and_round_trip_meaning() {
    for input in [UI_MANIFEST, SERVICE_MANIFEST, HYBRID_MANIFEST] {
        let manifest = manifest_or_panic(input);
        let canonical = manifest.canonical_json();
        let reparsed = manifest_or_panic(&canonical);
        assert_eq!(manifest, reparsed);
        assert_eq!(canonical, reparsed.canonical_json());
    }
}

#[test]
fn conformance_catalog_is_complete_unique_and_has_explicit_tranche1_scope() {
    let catalog = match ConformanceCatalog::parse(VECTORS) {
        Ok(catalog) => catalog,
        Err(error) => panic!("catalog should validate: {error:?}"),
    };
    assert_eq!(catalog.vectors.len(), 63);
    assert_eq!(catalog.tranche1_vectors().len(), 25);
    assert_eq!(
        catalog
            .vectors
            .iter()
            .filter(|vector| vector.class == "positive")
            .count(),
        30
    );
    assert_eq!(
        catalog
            .vectors
            .iter()
            .filter(|vector| vector.class == "negative")
            .count(),
        33
    );
}

#[test]
fn package_kinds_and_worlds_are_typed() {
    let ui = manifest_or_panic(UI_MANIFEST);
    let service = manifest_or_panic(SERVICE_MANIFEST);
    let hybrid = manifest_or_panic(HYBRID_MANIFEST);
    assert_eq!(ui.app.kind, AppKind::Ui);
    assert_eq!(service.app.kind, AppKind::Service);
    assert_eq!(hybrid.app.kind, AppKind::Hybrid);
    for manifest in [&ui, &service, &hybrid] {
        assert_eq!(
            imports_for_world(manifest.runtime.world),
            manifest
                .runtime
                .required_imports
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
        );
        assert!(
            reconcile_world_imports(manifest.runtime.world, &manifest.runtime.required_imports)
                .is_ok()
        );
    }
}

fn wit_world_imports(world: &str) -> Vec<String> {
    let marker = format!("world {world} {{");
    let Some((_, remainder)) = WIT.split_once(&marker) else {
        panic!("world {world} must exist");
    };
    let Some((body, _)) = remainder.split_once('}') else {
        panic!("world {world} must close");
    };
    body.lines()
        .filter_map(|line| line.trim().strip_prefix("import "))
        .filter_map(|line| line.strip_suffix(';'))
        .map(|name| format!("vibapp:experimental-v0/{name}@0.0.1"))
        .collect()
}

#[test]
fn checked_world_catalog_matches_the_accepted_wit_source() {
    for manifest_text in [UI_MANIFEST, SERVICE_MANIFEST, HYBRID_MANIFEST] {
        let manifest = manifest_or_panic(manifest_text);
        assert_eq!(
            wit_world_imports(manifest.runtime.world.as_str()),
            manifest.runtime.required_imports
        );
    }
    assert_eq!(
        wit_world_imports("web-preview-reference"),
        imports_for_world(vibapp_protocol::World::WebPreview)
            .iter()
            .map(|interface| (*interface).to_owned())
            .collect::<Vec<_>>()
    );
    assert!(wit_world_imports("daemon-control-reference").is_empty());
}

#[test]
fn checked_interface_and_resource_catalog_matches_the_schema_source() {
    let schema = match parse_json(MANIFEST_SCHEMA) {
        Ok(schema) => schema,
        Err(error) => panic!("schema JSON should parse: {error:?}"),
    };
    let Some(definitions) = schema.get("$defs") else {
        panic!("schema must have $defs");
    };
    let Some(interface_values) = definitions
        .get("wit-interface")
        .and_then(|definition| definition.get("enum"))
        .and_then(|value| value.as_array())
    else {
        panic!("schema must expose the interface enum");
    };
    assert_eq!(
        interface_values
            .iter()
            .filter_map(|value| value.as_str())
            .collect::<Vec<_>>(),
        APP_INTERFACES.to_vec()
    );
    let Some(resource_properties) = definitions
        .get("resources")
        .and_then(|definition| definition.get("properties"))
    else {
        panic!("schema must expose resources");
    };
    assert_eq!(
        resource_properties
            .get("linear_memory_bytes")
            .and_then(|field| field.get("maximum"))
            .and_then(|value| value.as_i64()),
        Some(67_108_864)
    );
    assert_eq!(
        resource_properties
            .get("output_bytes")
            .and_then(|field| field.get("maximum"))
            .and_then(|value| value.as_i64()),
        Some(262_144)
    );
}

#[test]
fn unknown_properties_and_unsupported_versions_fail_closed() {
    let unknown = UI_MANIFEST.replacen(
        "\"schema_version\":",
        "\"unexpected\":true,\"schema_version\":",
        1,
    );
    assert!(matches!(
        parse_manifest(&unknown),
        Err(error) if error.code == ProtocolErrorCode::InvalidManifest
    ));
    let unsupported = UI_MANIFEST.replace(
        "vibapp:experimental-v0@0.0.1",
        "vibapp:experimental-v0@0.0.2",
    );
    assert!(matches!(
        parse_manifest(&unsupported),
        Err(error) if error.code == ProtocolErrorCode::UnsupportedVersion
    ));
}

#[test]
fn resource_limit_and_kind_entrypoint_mutations_are_rejected() {
    let over_limit = UI_MANIFEST.replacen(
        "\"linear_memory_bytes\": 16777216",
        "\"linear_memory_bytes\": 67108865",
        1,
    );
    assert!(matches!(
        parse_manifest(&over_limit),
        Err(error) if error.code == ProtocolErrorCode::ResourceLimit
    ));
    let forged_service =
        SERVICE_MANIFEST.replacen("\"kind\": \"service\",", "\"kind\": \"hybrid\",", 1);
    assert!(matches!(
        parse_manifest(&forged_service),
        Err(error) if error.code == ProtocolErrorCode::IncompatibleContract
    ));
}

fn request_for(
    manifest: &vibapp_protocol::Manifest,
    os: &str,
    arch: &str,
    profile: Profile,
) -> AdmissionRequest {
    AdmissionRequest {
        host: HostTarget {
            os: os.to_owned(),
            arch: arch.to_owned(),
            profile,
        },
        actual_component_imports: manifest.runtime.required_imports.clone(),
        host_supported_imports: APP_INTERFACES
            .iter()
            .map(|value| (*value).to_owned())
            .collect(),
        denied_permissions: Vec::new(),
    }
}

#[test]
fn canonical_admission_cases_match_expected_surface_semantics() {
    let ui = manifest_or_panic(UI_MANIFEST);
    let ui_outcome = admit(&ui, &request_for(&ui, "macos", "aarch64", Profile::Desktop));
    assert_eq!(ui_outcome.decision, AdmissionDecision::Accept);
    assert!(ui_outcome.executable);
    assert!(ui_outcome.launcher_surface);

    let service = manifest_or_panic(SERVICE_MANIFEST);
    let service_outcome = admit(
        &service,
        &request_for(&service, "linux", "aarch64", Profile::Headless),
    );
    assert_eq!(service_outcome.decision, AdmissionDecision::Accept);
    assert!(!service_outcome.launcher_surface);

    let hybrid = manifest_or_panic(HYBRID_MANIFEST);
    let hybrid_outcome = admit(
        &hybrid,
        &request_for(&hybrid, "linux", "x86-64", Profile::Desktop),
    );
    assert_eq!(hybrid_outcome.decision, AdmissionDecision::Accept);
    assert!(hybrid_outcome.launcher_surface);
}

#[test]
fn import_reconciliation_host_support_and_required_permission_fail_before_execution() {
    let hybrid = manifest_or_panic(HYBRID_MANIFEST);
    let mut undeclared = request_for(&hybrid, "linux", "x86-64", Profile::Desktop);
    undeclared
        .actual_component_imports
        .push("vibapp:experimental-v0/http@0.0.1".to_owned());
    let outcome = admit(&hybrid, &undeclared);
    assert_eq!(outcome.decision, AdmissionDecision::Reject);
    assert_eq!(outcome.code, Some(ProtocolErrorCode::IncompatibleContract));
    assert!(!outcome.executable);

    let mut missing_host = request_for(&hybrid, "linux", "x86-64", Profile::Desktop);
    missing_host
        .host_supported_imports
        .retain(|interface| interface != "vibapp:experimental-v0/scheduler@0.0.1");
    let outcome = admit(&hybrid, &missing_host);
    assert_eq!(outcome.code, Some(ProtocolErrorCode::MissingInterface));

    let mut denied = request_for(&hybrid, "linux", "x86-64", Profile::Desktop);
    denied
        .denied_permissions
        .push("vibapp:experimental-v0/scheduler@0.0.1".to_owned());
    let outcome = admit(&hybrid, &denied);
    assert_eq!(outcome.decision, AdmissionDecision::Block);
    assert_eq!(outcome.code, Some(ProtocolErrorCode::PermissionDenied));
}

#[test]
fn profile_and_derivation_constraints_fail_closed() {
    let service = manifest_or_panic(SERVICE_MANIFEST);
    let unsupported = admit(
        &service,
        &request_for(&service, "browser", "wasm32", Profile::WebRuntime),
    );
    assert_eq!(unsupported.decision, AdmissionDecision::Reject);

    let live_preview = SERVICE_MANIFEST.replacen(
        "\"availability\": \"mock\", \"behavior\": \"Returns labeled sample data with simulated=true.\"",
        "\"availability\": \"native\", \"behavior\": \"Returns labeled sample data with simulated=true.\"",
        1,
    );
    assert_ne!(
        live_preview, SERVICE_MANIFEST,
        "negative fixture must mutate"
    );
    assert!(matches!(
        parse_manifest(&live_preview),
        Err(error) if error.code == ProtocolErrorCode::IncompatibleContract
    ));

    let wrong_digest = HYBRID_MANIFEST.replacen(
        "\"derived_from_sha256\": \"3333333333333333333333333333333333333333333333333333333333333333\"",
        "\"derived_from_sha256\": \"4444444444444444444444444444444444444444444444444444444444444444\"",
        1,
    );
    assert!(matches!(
        parse_manifest(&wrong_digest),
        Err(error) if error.code == ProtocolErrorCode::IntegrityFailure
    ));
}

#[test]
fn lifecycle_keeps_surface_and_service_state_separate() {
    let hybrid = manifest_or_panic(HYBRID_MANIFEST);
    let installed = match apply_package_operation(
        LifecycleState::default(),
        hybrid.app.kind,
        &hybrid.lifecycle,
        PackageOperation::Install,
    ) {
        Ok(state) => state,
        Err(error) => panic!("install should succeed: {error:?}"),
    };
    let enabled = match apply_package_operation(
        installed,
        hybrid.app.kind,
        &hybrid.lifecycle,
        PackageOperation::Enable,
    ) {
        Ok(state) => state,
        Err(error) => panic!("enable should succeed: {error:?}"),
    };
    assert!(enabled.service_running);
    let open =
        match apply_surface_operation(enabled, &hybrid, Profile::Desktop, SurfaceOperation::Open) {
            Ok(state) => state,
            Err(error) => panic!("open should succeed: {error:?}"),
        };
    let closed =
        match apply_surface_operation(open, &hybrid, Profile::Desktop, SurfaceOperation::Close) {
            Ok(state) => state,
            Err(error) => panic!("close should succeed: {error:?}"),
        };
    assert_eq!(closed.surface, SurfaceStatus::Closed);
    assert!(closed.service_running);

    let disabled = match apply_package_operation(
        closed,
        hybrid.app.kind,
        &hybrid.lifecycle,
        PackageOperation::Disable,
    ) {
        Ok(state) => state,
        Err(error) => panic!("disable should succeed: {error:?}"),
    };
    assert_eq!(disabled.package, PackageStatus::InstalledDisabled);
    assert!(disabled.package_retained && disabled.state_retained);
    assert!(!disabled.service_running && !disabled.schedules_active);
}

#[test]
fn list_and_host_issued_session_transitions_are_typed_and_scoped() {
    let ui = manifest_or_panic(UI_MANIFEST);
    let hybrid = manifest_or_panic(HYBRID_MANIFEST);
    let enabled = LifecycleState {
        package: PackageStatus::Enabled,
        package_retained: true,
        state_retained: true,
        ..LifecycleState::default()
    };
    let listings = list_apps(&[(&ui, enabled), (&hybrid, enabled)]);
    assert_eq!(listings.len(), 2);
    assert_eq!(listings[0].app_id, "ai.vibapp.alarm");
    assert_eq!(listings[1].app_id, "ai.vibapp.notes");

    let session_id = match SessionId::from_host("session-001") {
        Ok(identifier) => identifier,
        Err(error) => panic!("host session ID should validate: {error:?}"),
    };
    let surface_id = match SurfaceId::from_host("surface-001") {
        Ok(identifier) => identifier,
        Err(error) => panic!("host surface ID should validate: {error:?}"),
    };
    let session = match open_session(
        &ui,
        enabled,
        Profile::Desktop,
        "main",
        session_id.clone(),
        surface_id.clone(),
        "notes",
    ) {
        Ok(session) => session,
        Err(error) => panic!("open should validate app/entrypoint/profile/route: {error:?}"),
    };
    let focused = match apply_session_operation(
        &session,
        &LauncherOperation::Focus {
            entrypoint_id: "main".to_owned(),
            session_id: session_id.clone(),
            surface_id: surface_id.clone(),
        },
    ) {
        Ok(session) => session,
        Err(error) => panic!("matching host IDs should focus: {error:?}"),
    };
    assert_eq!(focused.surface, SurfaceStatus::Focused);

    let forged_surface = match SurfaceId::from_host("surface-other") {
        Ok(identifier) => identifier,
        Err(error) => panic!("host surface ID should validate: {error:?}"),
    };
    assert_eq!(
        apply_session_operation(
            &focused,
            &LauncherOperation::Close {
                entrypoint_id: "main".to_owned(),
                session_id,
                surface_id: forged_surface,
            },
        ),
        Err(LifecycleError::ForgedSession)
    );
    let headless_session = match SessionId::from_host("headless-session") {
        Ok(identifier) => identifier,
        Err(error) => panic!("host session ID should validate: {error:?}"),
    };
    let headless_surface = match SurfaceId::from_host("headless-surface") {
        Ok(identifier) => identifier,
        Err(error) => panic!("host surface ID should validate: {error:?}"),
    };
    assert_eq!(
        open_session(
            &ui,
            enabled,
            Profile::Headless,
            "main",
            headless_session,
            headless_surface,
            "notes",
        ),
        Err(LifecycleError::SurfaceUnavailable)
    );
}

#[test]
fn uninstall_disposition_and_output_bounds_are_enforced() {
    let ui = manifest_or_panic(UI_MANIFEST);
    let installed = LifecycleState {
        package: PackageStatus::InstalledDisabled,
        package_retained: true,
        state_retained: true,
        ..LifecycleState::default()
    };
    let uninstalled = match apply_package_operation(
        installed,
        ui.app.kind,
        &ui.lifecycle,
        PackageOperation::Uninstall {
            disposition: DataDisposition::Delete,
        },
    ) {
        Ok(state) => state,
        Err(error) => panic!("allowed uninstall should succeed: {error:?}"),
    };
    assert_eq!(uninstalled.package, PackageStatus::Absent);
    assert!(!uninstalled.state_retained);
    assert_eq!(
        validate_output_size(
            OutputBounds {
                declared_bytes: 131_072
            },
            131_073
        ),
        Err(ProtocolErrorCode::ResourceLimit)
    );
}

#[test]
fn malformed_view_identity_parent_and_cycle_are_rejected() {
    let valid = ViewShape {
        root: "root".to_owned(),
        nodes: vec![
            ViewNode {
                id: "root".to_owned(),
                parent: None,
            },
            ViewNode {
                id: "child".to_owned(),
                parent: Some("root".to_owned()),
            },
        ],
    };
    assert!(validate_view_shape(&valid).is_ok());
    let duplicate = ViewShape {
        root: "missing-root".to_owned(),
        nodes: vec![
            ViewNode {
                id: "duplicate".to_owned(),
                parent: Some("duplicate".to_owned()),
            },
            ViewNode {
                id: "duplicate".to_owned(),
                parent: None,
            },
        ],
    };
    assert_eq!(
        validate_view_shape(&duplicate),
        Err(ProtocolErrorCode::MalformedOutput)
    );
}
