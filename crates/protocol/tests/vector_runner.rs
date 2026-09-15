use std::collections::BTreeSet;

use vibapp_protocol::json::{JsonValue, parse_json};
use vibapp_protocol::wit_catalog::APP_INTERFACES;
use vibapp_protocol::{
    AdmissionDecision, AdmissionOutcome, AdmissionRequest, ConformanceCatalog, DataDisposition,
    HostTarget, LauncherOperation, LifecycleError, LifecycleState, OutputBounds, PackageOperation,
    PackageStatus, Profile, ProtocolErrorCode, RestoreAuthorization, RestoreToken, SafeFieldChange,
    SafeFieldValue, SessionId, SurfaceId, SurfaceOperation, SurfaceStatus, VectorDescriptor,
    ViewNode, ViewShape, admit, apply_package_operation, apply_session_operation,
    apply_surface_operation, deferred_owner, open_session, parse_manifest, restore_session,
    validate_output_size, validate_view_shape,
};

const UI_MANIFEST: &str =
    include_str!("../../../fixtures/conformance/experimental-v0/manifests/ui-only.valid.json");
const SERVICE_MANIFEST: &str =
    include_str!("../../../fixtures/conformance/experimental-v0/manifests/service-only.valid.json");
const HYBRID_MANIFEST: &str =
    include_str!("../../../fixtures/conformance/experimental-v0/manifests/hybrid.valid.json");
const VECTORS: &str = include_str!("../../../fixtures/conformance/experimental-v0/vectors.json");

struct GivenReader<'a> {
    vector_id: &'a str,
    root: &'a JsonValue,
    consumed: BTreeSet<String>,
}

impl<'a> GivenReader<'a> {
    fn new(vector: &'a VectorDescriptor) -> Self {
        Self {
            vector_id: &vector.id,
            root: &vector.given,
            consumed: BTreeSet::new(),
        }
    }

    fn value(&mut self, pointer: &str) -> JsonValue {
        let value = at(self.root, pointer)
            .unwrap_or_else(|| panic!("{} missing given field {pointer}", self.vector_id));
        collect_leaf_pointers(value, &format!("/given{pointer}"), &mut self.consumed);
        value.clone()
    }

    fn string(&mut self, pointer: &str) -> String {
        self.value(pointer)
            .as_str()
            .unwrap_or_else(|| panic!("{} {pointer} must be a string", self.vector_id))
            .to_owned()
    }

    fn boolean(&mut self, pointer: &str) -> bool {
        self.value(pointer)
            .as_bool()
            .unwrap_or_else(|| panic!("{} {pointer} must be a boolean", self.vector_id))
    }

    fn integer(&mut self, pointer: &str) -> i64 {
        self.value(pointer)
            .as_i64()
            .unwrap_or_else(|| panic!("{} {pointer} must be an integer", self.vector_id))
    }

    fn strings(&mut self, pointer: &str) -> Vec<String> {
        self.value(pointer)
            .as_array()
            .unwrap_or_else(|| panic!("{} {pointer} must be an array", self.vector_id))
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .unwrap_or_else(|| panic!("{} {pointer} item must be a string", self.vector_id))
                    .to_owned()
            })
            .collect()
    }

    fn finish(self) {
        let mut leaves = BTreeSet::new();
        collect_leaf_pointers(self.root, "/given", &mut leaves);
        let uncovered = leaves
            .into_iter()
            .filter(|pointer| {
                deferred_owner(self.vector_id, pointer).is_none()
                    && !self.consumed.contains(pointer)
            })
            .collect::<Vec<_>>();
        assert!(
            uncovered.is_empty(),
            "{} has unconsumed Tranche 1 given fields: {uncovered:?}",
            self.vector_id
        );
    }
}

#[test]
fn exact_tranche1_vectors_consume_owned_inputs_and_deep_compare_owned_results() {
    let catalog = ConformanceCatalog::parse(VECTORS)
        .unwrap_or_else(|error| panic!("catalog must parse: {error:?}"));
    let vectors = catalog.tranche1_vectors();
    assert_eq!(vectors.len(), 25);
    for vector in vectors {
        let mut given = GivenReader::new(vector);
        let actual = run_vector(vector, &mut given);
        given.finish();
        let owned_expected = project_owned(&vector.expected, &vector.id, "/expected")
            .expect("expected object cannot be entirely deferred");
        let shaped_actual = shape_like(&owned_expected, &actual, "/expected", &vector.id);
        assert_eq!(
            shaped_actual, owned_expected,
            "{} did not match its complete Tranche 1-owned expected projection",
            vector.id
        );
    }
}

fn run_vector(vector: &VectorDescriptor, given: &mut GivenReader<'_>) -> JsonValue {
    match vector.id.as_str() {
        "current-ui-only-desktop"
        | "current-service-only-linux-arm64-headless"
        | "current-hybrid-desktop" => run_current_admission(vector, given),
        "web-preview-service-uses-mocks" | "web-runtime-ui-is-foreground-only" => {
            run_browser_profile(vector, given)
        }
        "close-hybrid-ui-does-not-disable-service" => run_close_hybrid(given),
        "disable-service-retains-package-and-state" => run_disable_service(given),
        "uninstall-delete-data" | "uninstall-retain-data" => run_uninstall(vector, given),
        "valid-launcher-restoration" => run_restore(given),
        "uninstall-disposition-not-allowed" => run_disallowed_uninstall(given),
        "missing-host-import" => run_missing_host_import(given),
        "unknown-required-import" => run_unknown_import(given),
        "unsupported-contract-version" => run_unsupported_contract(given),
        "daemon-control-interface-is-not-an-app-capability" => run_control_import(given),
        "resource-request-over-limit" => run_resource_over_limit(given),
        "service-kind-forges-launcher-entrypoint" => run_service_launcher_forgery(given),
        "ui-kind-missing-launcher-entrypoint" => run_ui_missing_launcher(given),
        "component-import-not-declared-in-manifest" => run_extra_component_import(given),
        "manifest-import-not-present-in-component" => run_missing_component_import(given),
        "permission-denied-required-capability" => run_permission_denied(given),
        "malformed-ui-output" => run_malformed_output(given),
        "runtime-output-exceeds-declared-bound" => run_output_over_limit(given),
        "web-preview-requests-live-hardware" => run_live_preview(given),
        "browser-derived-digest-does-not-reference-canonical" => run_wrong_derivation(given),
        other => panic!("unhandled Tranche 1 vector {other}"),
    }
}

fn run_current_admission(vector: &VectorDescriptor, given: &mut GivenReader<'_>) -> JsonValue {
    let manifest = manifest(vector);
    let request = request(
        &manifest,
        &given.string("/host/os"),
        &given.string("/host/arch"),
        profile(&given.string("/host/profile")),
        given.strings("/actual_imports"),
    );
    assert_eq!(given.string("/grants"), "all-required");
    let outcome = admit(&manifest, &request);
    let mut actual = admission_json(&outcome);
    if vector.id == "current-hybrid-desktop" {
        let installed = apply_package_operation(
            LifecycleState::default(),
            manifest.app.kind,
            &manifest.lifecycle,
            PackageOperation::Install,
        )
        .expect("hybrid install must succeed");
        let enabled = apply_package_operation(
            installed,
            manifest.app.kind,
            &manifest.lifecycle,
            PackageOperation::Enable,
        )
        .expect("hybrid enable must succeed");
        insert(
            &mut actual,
            "service_active_after_enable",
            boolean(enabled.service_running),
        );
    }
    actual
}

fn run_browser_profile(vector: &VectorDescriptor, given: &mut GivenReader<'_>) -> JsonValue {
    let manifest = manifest(vector);
    let requested_profile = profile(&given.string("/host/profile"));
    assert_eq!(given.string("/host/os"), "browser");
    assert_eq!(given.string("/host/arch"), "wasm32");
    assert!(given.boolean("/derived_from_matches_canonical"));
    let derivation = manifest
        .artifacts
        .browser_derivations
        .iter()
        .find(|candidate| candidate.profile == requested_profile)
        .expect("browser derivation must exist");
    assert_eq!(
        derivation.derived_from_sha256,
        manifest.artifacts.canonical_component.sha256
    );
    if vector.id == "web-preview-service-uses-mocks" {
        let availability = given.value("/availability");
        for (short_name, expected) in availability
            .as_object()
            .expect("availability must be an object")
        {
            let capability = manifest
                .capabilities
                .iter()
                .find(|capability| capability.interface.contains(&format!("/{short_name}@")))
                .unwrap_or_else(|| panic!("missing capability {short_name}"));
            let support = capability
                .profiles
                .iter()
                .find(|support| support.profile == requested_profile)
                .expect("preview support must exist");
            assert_eq!(
                format!("{:?}", support.availability).to_lowercase(),
                expected.as_str().unwrap()
            );
        }
    }
    let outcome = admit(
        &manifest,
        &request(
            &manifest,
            "browser",
            "wasm32",
            requested_profile,
            manifest.runtime.required_imports.clone(),
        ),
    );
    let mut actual = admission_json(&outcome);
    if vector.id == "web-runtime-ui-is-foreground-only" {
        let support = manifest
            .runtime
            .profiles
            .iter()
            .find(|support| support.profile == requested_profile)
            .expect("web runtime profile must exist");
        insert(
            &mut actual,
            "background",
            string(match support.background {
                vibapp_protocol::model::Background::ForegroundOnly => "foreground-only",
                _ => "unexpected",
            }),
        );
    }
    actual
}

fn run_close_hybrid(given: &mut GivenReader<'_>) -> JsonValue {
    assert_eq!(given.string("/app_state"), "enabled");
    let manifest = parse_manifest(HYBRID_MANIFEST).expect("hybrid fixture must parse");
    let entrypoint = given.string("/launcher_event/close/entrypoint");
    let session_id = SessionId::from_host(given.string("/launcher_event/close/session"))
        .expect("host session ID must validate");
    let surface_id = SurfaceId::from_host(given.string("/launcher_event/close/surface"))
        .expect("host surface ID must validate");
    let lifecycle = LifecycleState {
        package: PackageStatus::Enabled,
        surface: SurfaceStatus::Open,
        service_running: true,
        package_retained: true,
        state_retained: true,
        schedules_active: true,
    };
    let session = open_session(
        &manifest,
        lifecycle,
        Profile::Desktop,
        &entrypoint,
        session_id.clone(),
        surface_id.clone(),
        "alarms",
    )
    .expect("exact hybrid session must open");
    let closed = apply_session_operation(
        &session,
        &LauncherOperation::Close {
            entrypoint_id: entrypoint,
            session_id,
            surface_id: surface_id.clone(),
        },
    )
    .expect("exact close must succeed");
    let closed_lifecycle = apply_surface_operation(
        lifecycle,
        &manifest,
        Profile::Desktop,
        SurfaceOperation::Close,
    )
    .expect("exact lifecycle surface close must succeed");
    let closed_surface = if closed.surface == SurfaceStatus::Closed
        && closed_lifecycle.surface == SurfaceStatus::Closed
    {
        string(closed.surface_id.as_str())
    } else {
        JsonValue::Null
    };
    object(vec![
        ("decision", string("accept")),
        ("code", JsonValue::Null),
        ("closed_surface", closed_surface),
        (
            "app_state",
            string(package_status(closed_lifecycle.package)),
        ),
    ])
}

fn run_disable_service(given: &mut GivenReader<'_>) -> JsonValue {
    assert_eq!(given.string("/app_state"), "enabled");
    assert_eq!(given.string("/operation"), "disable");
    let manifest = parse_manifest(SERVICE_MANIFEST).expect("service fixture must parse");
    let before = LifecycleState {
        package: PackageStatus::Enabled,
        surface: SurfaceStatus::Closed,
        service_running: true,
        package_retained: true,
        state_retained: true,
        schedules_active: true,
    };
    let after = apply_package_operation(
        before,
        manifest.app.kind,
        &manifest.lifecycle,
        PackageOperation::Disable,
    )
    .expect("disable must succeed");
    let service_instances = if after.service_running {
        vec![string("unexpected-running-service")]
    } else {
        Vec::new()
    };
    let new_effects_allowed =
        after.package == PackageStatus::Enabled || after.service_running || after.schedules_active;
    object(vec![
        ("decision", string("accept")),
        ("code", JsonValue::Null),
        ("app_state", string(package_status(after.package))),
        ("package_retained", boolean(after.package_retained)),
        ("service_instances", JsonValue::Array(service_instances)),
        ("new_effects_allowed", boolean(new_effects_allowed)),
    ])
}

fn run_uninstall(vector: &VectorDescriptor, given: &mut GivenReader<'_>) -> JsonValue {
    assert_eq!(given.string("/operation"), "uninstall");
    let disposition = match given.string("/data_disposition").as_str() {
        "delete" => DataDisposition::Delete,
        "retain" => DataDisposition::Retain,
        other => panic!("unexpected disposition {other}"),
    };
    let manifest = manifest(vector);
    let before = LifecycleState {
        package: PackageStatus::Enabled,
        surface: SurfaceStatus::Closed,
        service_running: true,
        package_retained: true,
        state_retained: true,
        schedules_active: true,
    };
    let after = apply_package_operation(
        before,
        manifest.app.kind,
        &manifest.lifecycle,
        PackageOperation::Uninstall { disposition },
    )
    .expect("allowed uninstall must succeed");
    let mut actual = object(vec![
        ("decision", string("accept")),
        ("code", JsonValue::Null),
        (
            "package_present",
            boolean(after.package != PackageStatus::Absent),
        ),
    ]);
    if disposition == DataDisposition::Delete {
        insert(
            &mut actual,
            "app_state_present",
            boolean(after.state_retained),
        );
    } else {
        insert(&mut actual, "state_archived", boolean(after.state_retained));
    }
    actual
}

fn run_restore(given: &mut GivenReader<'_>) -> JsonValue {
    assert!(
        given
            .value("/sensitive_fields")
            .as_array()
            .expect("sensitive_fields must be an array")
            .is_empty()
    );
    let manifest = parse_manifest(UI_MANIFEST).expect("UI fixture must parse");
    let session_id = SessionId::from_host(given.string("/host_issued/session"))
        .expect("session ID must validate");
    let surface_id = SurfaceId::from_host(given.string("/host_issued/surface"))
        .expect("surface ID must validate");
    let token = RestoreToken::from_host(given.string("/host_issued/restore_token"))
        .expect("restore token must validate");
    let event_entrypoint = given.string("/event/entrypoint");
    let event_session =
        SessionId::from_host(given.string("/event/session")).expect("event session must validate");
    let event_surface =
        SurfaceId::from_host(given.string("/event/surface")).expect("event surface must validate");
    let event_route = given.string("/event/route");
    let event_token =
        RestoreToken::from_host(given.string("/event/token")).expect("event token must validate");
    let fields = given.value("/event/safe_fields");
    let safe_fields = fields
        .as_array()
        .expect("safe_fields must be an array")
        .iter()
        .map(|field| {
            let object = field.as_object().expect("safe field must be an object");
            let field_id = object_field(object, "field")
                .as_str()
                .expect("field ID must be a string");
            let text = object_field(
                object_field(object, "value")
                    .as_object()
                    .expect("field value must be an object"),
                "text",
            )
            .as_str()
            .expect("text value must be a string");
            SafeFieldChange::from_host(field_id, SafeFieldValue::Text(text.to_owned()))
                .expect("safe field must validate")
        })
        .collect();
    let authorization = RestoreAuthorization {
        app_id: manifest.app.id.clone(),
        entrypoint_id: event_entrypoint.clone(),
        session_id,
        surface_id,
        route: event_route.clone(),
        token,
    };
    let restored = restore_session(
        &manifest,
        LifecycleState {
            package: PackageStatus::Enabled,
            package_retained: true,
            state_retained: true,
            ..LifecycleState::default()
        },
        Profile::Desktop,
        &authorization,
        &LauncherOperation::Restore {
            app_id: manifest.app.id.clone(),
            entrypoint_id: event_entrypoint,
            session_id: event_session,
            surface_id: event_surface,
            route: event_route,
            token: event_token,
            safe_fields,
        },
    )
    .expect("bounded restore must succeed");
    object(vec![
        ("decision", string("accept")),
        ("code", JsonValue::Null),
        (
            "surface_update_must_echo_host_ids",
            boolean(
                restored.session_id == authorization.session_id
                    && restored.surface_id == authorization.surface_id,
            ),
        ),
        ("raw_guest_memory_restored", boolean(false)),
    ])
}

fn run_disallowed_uninstall(given: &mut GivenReader<'_>) -> JsonValue {
    let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut raw, "/lifecycle/uninstall/allowed_data_dispositions") =
        given.value("/manifest_allowed_dispositions_override");
    let manifest = parse_manifest(&raw.to_canonical_json()).expect("mutated manifest must parse");
    let disposition = match given.string("/operation_disposition").as_str() {
        "retain" => DataDisposition::Retain,
        other => panic!("unexpected disposition {other}"),
    };
    let before = LifecycleState {
        package: PackageStatus::InstalledDisabled,
        package_retained: true,
        state_retained: true,
        ..LifecycleState::default()
    };
    let error = apply_package_operation(
        before,
        manifest.app.kind,
        &manifest.lifecycle,
        PackageOperation::Uninstall { disposition },
    )
    .expect_err("disallowed disposition must reject");
    object(vec![
        ("decision", string("reject")),
        ("code", code(Some(error.protocol_code()))),
        (
            "package_present",
            boolean(before.package != PackageStatus::Absent),
        ),
        ("state_unchanged", boolean(true)),
    ])
}

fn run_missing_host_import(given: &mut GivenReader<'_>) -> JsonValue {
    assert_eq!(given.string("/actual_imports"), "matches-manifest");
    let manifest = parse_manifest(HYBRID_MANIFEST).expect("hybrid fixture must parse");
    let excluded = given.strings("/host_supported_imports_excluding");
    let host_supported = APP_INTERFACES
        .iter()
        .filter(|interface| !excluded.iter().any(|excluded| excluded == **interface))
        .map(|interface| (*interface).to_owned())
        .collect();
    let mut request = request(
        &manifest,
        "linux",
        "x86-64",
        Profile::Desktop,
        manifest.runtime.required_imports.clone(),
    );
    request.host_supported_imports = host_supported;
    let outcome = admit(&manifest, &request);
    let mut actual = admission_json(&outcome);
    insert(
        &mut actual,
        "before_instantiation",
        boolean(!outcome.executable),
    );
    actual
}

fn run_unknown_import(given: &mut GivenReader<'_>) -> JsonValue {
    let additions = given.strings("/manifest_required_imports_add");
    let actual_additions = given.strings("/actual_imports_add");
    assert_eq!(additions, actual_additions);
    let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut raw, "/runtime/required_imports")
        .extend(additions.into_iter().map(|value| string(&value)));
    manifest_failure(raw, None)
}

fn run_unsupported_contract(given: &mut GivenReader<'_>) -> JsonValue {
    let replacements = given.value("/replace");
    let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    apply_replacements(&mut raw, &replacements);
    manifest_failure(raw, None)
}

fn run_control_import(given: &mut GivenReader<'_>) -> JsonValue {
    let imports = given.strings("/manifest_required_imports_add");
    let interface = given.string("/capabilities_add_interface");
    assert_eq!(imports, vec![interface.clone()]);
    let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut raw, "/runtime/required_imports")
        .extend(imports.into_iter().map(|value| string(&value)));
    let mut capability = at(&raw, "/capabilities/0")
        .expect("canonical capability must exist")
        .clone();
    *at_mut(&mut capability, "/interface") = string(&interface);
    array_mut(&mut raw, "/capabilities").push(capability);
    manifest_failure(raw, None)
}

fn run_resource_over_limit(given: &mut GivenReader<'_>) -> JsonValue {
    let replacements = given.value("/replace");
    let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    apply_replacements(&mut raw, &replacements);
    manifest_failure(raw, None)
}

fn run_service_launcher_forgery(given: &mut GivenReader<'_>) -> JsonValue {
    assert_eq!(given.string("/entrypoints_add_kind"), "launcher-ui");
    let ui = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    let launcher = at(&ui, "/entrypoints/0")
        .expect("UI launcher must exist")
        .clone();
    let mut raw = parse_json(SERVICE_MANIFEST).expect("service fixture JSON must parse");
    array_mut(&mut raw, "/entrypoints").push(launcher);
    manifest_failure(raw, None)
}

fn run_ui_missing_launcher(given: &mut GivenReader<'_>) -> JsonValue {
    assert_eq!(given.string("/remove_entrypoint_kind"), "launcher-ui");
    let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut raw, "/entrypoints").retain(|entrypoint| {
        at(entrypoint, "/kind").and_then(JsonValue::as_str) != Some("launcher-ui")
    });
    manifest_failure(raw, None)
}

fn run_extra_component_import(given: &mut GivenReader<'_>) -> JsonValue {
    let manifest = parse_manifest(UI_MANIFEST).expect("UI fixture must parse");
    let mut imports = manifest.runtime.required_imports.clone();
    imports.extend(given.strings("/actual_imports_add"));
    let outcome = admit(
        &manifest,
        &request(&manifest, "macos", "aarch64", Profile::Desktop, imports),
    );
    let mut actual = admission_json(&outcome);
    insert(
        &mut actual,
        "before_instantiation",
        boolean(!outcome.executable),
    );
    actual
}

fn run_missing_component_import(given: &mut GivenReader<'_>) -> JsonValue {
    let manifest = parse_manifest(HYBRID_MANIFEST).expect("hybrid fixture must parse");
    let removed = given.strings("/actual_imports_remove");
    let imports = manifest
        .runtime
        .required_imports
        .iter()
        .filter(|interface| !removed.contains(interface))
        .cloned()
        .collect();
    let outcome = admit(
        &manifest,
        &request(&manifest, "linux", "x86-64", Profile::Desktop, imports),
    );
    let mut actual = admission_json(&outcome);
    insert(
        &mut actual,
        "before_instantiation",
        boolean(!outcome.executable),
    );
    actual
}

fn run_permission_denied(given: &mut GivenReader<'_>) -> JsonValue {
    let manifest = parse_manifest(HYBRID_MANIFEST).expect("hybrid fixture must parse");
    let mut request = request(
        &manifest,
        "linux",
        "x86-64",
        Profile::Desktop,
        manifest.runtime.required_imports.clone(),
    );
    request.denied_permissions = given.strings("/denied");
    admission_json(&admit(&manifest, &request))
}

fn run_malformed_output(given: &mut GivenReader<'_>) -> JsonValue {
    let output = given.value("/output");
    let root = at(&output, "/root")
        .and_then(JsonValue::as_str)
        .expect("root must be a string")
        .to_owned();
    let nodes = at(&output, "/nodes")
        .and_then(JsonValue::as_array)
        .expect("nodes must be an array")
        .iter()
        .map(|node| ViewNode {
            id: at(node, "/id")
                .and_then(JsonValue::as_str)
                .expect("node ID must be a string")
                .to_owned(),
            parent: match at(node, "/parent").expect("parent must exist") {
                JsonValue::Null => None,
                JsonValue::String(parent) => Some(parent.clone()),
                _ => panic!("parent must be string or null"),
            },
        })
        .collect();
    let error =
        validate_view_shape(&ViewShape { root, nodes }).expect_err("malformed output must reject");
    object(vec![
        ("decision", string("reject")),
        ("code", code(Some(error))),
    ])
}

fn run_output_over_limit(given: &mut GivenReader<'_>) -> JsonValue {
    let declared = u64::try_from(given.integer("/declared_output_bytes"))
        .expect("declared bytes must be non-negative");
    let actual = u64::try_from(given.integer("/actual_output_bytes"))
        .expect("actual bytes must be non-negative");
    let error = validate_output_size(
        OutputBounds {
            declared_bytes: declared,
        },
        actual,
    )
    .expect_err("oversize output must reject");
    object(vec![
        ("decision", string("reject")),
        ("code", code(Some(error))),
    ])
}

fn run_live_preview(given: &mut GivenReader<'_>) -> JsonValue {
    let availability = given.string("/replace_web_preview_system_metrics_availability");
    let mut raw = parse_json(SERVICE_MANIFEST).expect("service fixture JSON must parse");
    set_capability_availability(
        &mut raw,
        "system-metrics",
        Profile::WebPreview,
        &availability,
    );
    manifest_failure(raw, Some("reason"))
}

fn run_wrong_derivation(given: &mut GivenReader<'_>) -> JsonValue {
    let replacements = given.value("/replace");
    let mut raw = parse_json(HYBRID_MANIFEST).expect("hybrid fixture JSON must parse");
    apply_replacements(&mut raw, &replacements);
    manifest_failure(raw, None)
}

fn manifest(vector: &VectorDescriptor) -> vibapp_protocol::Manifest {
    let text = match vector.manifest.as_str() {
        "manifests/ui-only.valid.json" => UI_MANIFEST,
        "manifests/service-only.valid.json" => SERVICE_MANIFEST,
        "manifests/hybrid.valid.json" => HYBRID_MANIFEST,
        other => panic!("unrecognized fixture {other}"),
    };
    parse_manifest(text)
        .unwrap_or_else(|error| panic!("{} fixture must parse: {error:?}", vector.id))
}

fn request(
    _manifest: &vibapp_protocol::Manifest,
    os: &str,
    arch: &str,
    profile: Profile,
    actual_component_imports: Vec<String>,
) -> AdmissionRequest {
    AdmissionRequest {
        host: HostTarget {
            os: os.to_owned(),
            arch: arch.to_owned(),
            profile,
        },
        actual_component_imports,
        host_supported_imports: APP_INTERFACES
            .iter()
            .map(|interface| (*interface).to_owned())
            .collect(),
        denied_permissions: Vec::new(),
    }
}

fn profile(value: &str) -> Profile {
    match value {
        "desktop" => Profile::Desktop,
        "web-preview" => Profile::WebPreview,
        "web-runtime" => Profile::WebRuntime,
        "headless" => Profile::Headless,
        other => panic!("unknown profile {other}"),
    }
}

fn package_status(value: PackageStatus) -> &'static str {
    match value {
        PackageStatus::Absent => "absent",
        PackageStatus::InstalledDisabled => "disabled",
        PackageStatus::Enabled => "enabled",
    }
}

fn admission_json(outcome: &AdmissionOutcome) -> JsonValue {
    object(vec![
        (
            "decision",
            string(match outcome.decision {
                AdmissionDecision::Accept => "accept",
                AdmissionDecision::Block => "block",
                AdmissionDecision::Reject => "reject",
            }),
        ),
        ("code", code(outcome.code)),
        ("executable", boolean(outcome.executable)),
        ("launcher_surface", boolean(outcome.launcher_surface)),
        ("preview_only", boolean(outcome.preview_only)),
    ])
}

fn manifest_failure(raw: JsonValue, detail_key: Option<&str>) -> JsonValue {
    let error = parse_manifest(&raw.to_canonical_json()).expect_err("mutated manifest must reject");
    let schema_valid = error
        .schema_valid()
        .expect("vector manifest rejection must have an explicit schema classification");
    let mut actual = object(vec![
        ("decision", string("reject")),
        ("code", code(Some(error.code))),
        ("schema_valid", boolean(schema_valid)),
        ("before_instantiation", boolean(true)),
    ]);
    if let Some(key) = detail_key {
        insert(&mut actual, key, string(&error.detail));
    }
    actual
}

fn code(value: Option<ProtocolErrorCode>) -> JsonValue {
    match value {
        None => JsonValue::Null,
        Some(value) => string(match value {
            ProtocolErrorCode::InvalidArgument => "invalid-argument",
            ProtocolErrorCode::IncompatibleContract => "incompatible-contract",
            ProtocolErrorCode::UnsupportedVersion => "unsupported-version",
            ProtocolErrorCode::InvalidManifest => "invalid-manifest",
            ProtocolErrorCode::UnknownInterface => "unknown-interface",
            ProtocolErrorCode::MissingInterface => "missing-interface",
            ProtocolErrorCode::PermissionDenied => "permission-denied",
            ProtocolErrorCode::ConsentRequired => "consent-required",
            ProtocolErrorCode::CapabilityUnavailable => "capability-unavailable",
            ProtocolErrorCode::UnsupportedSurface => "unsupported-surface",
            ProtocolErrorCode::ResourceLimit => "resource-limit",
            ProtocolErrorCode::DeadlineExceeded => "deadline-exceeded",
            ProtocolErrorCode::Cancelled => "cancelled",
            ProtocolErrorCode::AppDisabled => "app-disabled",
            ProtocolErrorCode::AppUninstalled => "app-uninstalled",
            ProtocolErrorCode::UpgradeInProgress => "upgrade-in-progress",
            ProtocolErrorCode::IntegrityFailure => "integrity-failure",
            ProtocolErrorCode::NotFound => "not-found",
            ProtocolErrorCode::Conflict => "conflict",
            ProtocolErrorCode::StaleRevision => "stale-revision",
            ProtocolErrorCode::MalformedOutput => "malformed-output",
            ProtocolErrorCode::ForgedIdentifier => "forged-identifier",
            ProtocolErrorCode::Internal => "internal",
        }),
    }
}

fn project_owned(value: &JsonValue, vector_id: &str, pointer: &str) -> Option<JsonValue> {
    if deferred_owner(vector_id, pointer).is_some() {
        return None;
    }
    Some(match value {
        JsonValue::Object(entries) => JsonValue::Object(
            entries
                .iter()
                .filter_map(|(key, value)| {
                    project_owned(value, vector_id, &format!("{pointer}/{key}"))
                        .map(|value| (key.clone(), value))
                })
                .collect(),
        ),
        JsonValue::Array(values) => JsonValue::Array(
            values
                .iter()
                .enumerate()
                .filter_map(|(index, value)| {
                    project_owned(value, vector_id, &format!("{pointer}/{index}"))
                })
                .collect(),
        ),
        other => other.clone(),
    })
}

fn shape_like(
    expected: &JsonValue,
    actual: &JsonValue,
    pointer: &str,
    vector_id: &str,
) -> JsonValue {
    match expected {
        JsonValue::Object(expected_entries) => {
            let actual_entries = actual
                .as_object()
                .unwrap_or_else(|| panic!("{vector_id} actual {pointer} must be an object"));
            JsonValue::Object(
                expected_entries
                    .iter()
                    .map(|(key, expected_value)| {
                        let actual_value = object_field(actual_entries, key);
                        (
                            key.clone(),
                            shape_like(
                                expected_value,
                                actual_value,
                                &format!("{pointer}/{key}"),
                                vector_id,
                            ),
                        )
                    })
                    .collect(),
            )
        }
        JsonValue::Array(expected_values) => {
            let actual_values = actual
                .as_array()
                .unwrap_or_else(|| panic!("{vector_id} actual {pointer} must be an array"));
            assert_eq!(
                actual_values.len(),
                expected_values.len(),
                "{vector_id} actual {pointer} array length"
            );
            JsonValue::Array(
                expected_values
                    .iter()
                    .zip(actual_values)
                    .enumerate()
                    .map(|(index, (expected, actual))| {
                        shape_like(expected, actual, &format!("{pointer}/{index}"), vector_id)
                    })
                    .collect(),
            )
        }
        _ => actual.clone(),
    }
}

fn collect_leaf_pointers(value: &JsonValue, pointer: &str, output: &mut BTreeSet<String>) {
    match value {
        JsonValue::Object(entries) if !entries.is_empty() => {
            for (key, value) in entries {
                collect_leaf_pointers(value, &format!("{pointer}/{key}"), output);
            }
        }
        JsonValue::Array(values) if !values.is_empty() => {
            for (index, value) in values.iter().enumerate() {
                collect_leaf_pointers(value, &format!("{pointer}/{index}"), output);
            }
        }
        _ => {
            output.insert(pointer.to_owned());
        }
    }
}

fn at<'a>(value: &'a JsonValue, pointer: &str) -> Option<&'a JsonValue> {
    let mut current = value;
    for segment in pointer.split('/').skip(1) {
        current = match current {
            JsonValue::Object(entries) => entries
                .iter()
                .find_map(|(key, value)| (key == segment).then_some(value))?,
            JsonValue::Array(values) => values.get(segment.parse::<usize>().ok()?)?,
            _ => return None,
        };
    }
    Some(current)
}

fn at_mut<'a>(value: &'a mut JsonValue, pointer: &str) -> &'a mut JsonValue {
    fn descend<'a>(value: &'a mut JsonValue, segments: &[&str]) -> &'a mut JsonValue {
        if segments.is_empty() {
            return value;
        }
        match value {
            JsonValue::Object(entries) => {
                let next = entries
                    .iter_mut()
                    .find_map(|(key, value)| (key == segments[0]).then_some(value))
                    .unwrap_or_else(|| panic!("missing object path segment {}", segments[0]));
                descend(next, &segments[1..])
            }
            JsonValue::Array(values) => {
                let index = segments[0]
                    .parse::<usize>()
                    .unwrap_or_else(|_| panic!("invalid array index {}", segments[0]));
                descend(&mut values[index], &segments[1..])
            }
            _ => panic!("cannot descend through scalar"),
        }
    }
    let segments = pointer.split('/').skip(1).collect::<Vec<_>>();
    descend(value, &segments)
}

fn array_mut<'a>(value: &'a mut JsonValue, pointer: &str) -> &'a mut Vec<JsonValue> {
    match at_mut(value, pointer) {
        JsonValue::Array(values) => values,
        _ => panic!("{pointer} must be an array"),
    }
}

fn apply_replacements(target: &mut JsonValue, replacements: &JsonValue) {
    for (pointer, value) in replacements
        .as_object()
        .expect("replacement map must be an object")
    {
        *at_mut(target, pointer) = value.clone();
    }
}

fn set_capability_availability(
    raw: &mut JsonValue,
    short_name: &str,
    profile: Profile,
    availability: &str,
) {
    let capabilities = array_mut(raw, "/capabilities");
    let capability = capabilities
        .iter_mut()
        .find(|capability| {
            at(capability, "/interface")
                .and_then(JsonValue::as_str)
                .is_some_and(|interface| interface.contains(&format!("/{short_name}@")))
        })
        .unwrap_or_else(|| panic!("missing capability {short_name}"));
    let profiles = array_mut(capability, "/profiles");
    let support = profiles
        .iter_mut()
        .find(|support| {
            at(support, "/profile").and_then(JsonValue::as_str) == Some(profile.as_str())
        })
        .expect("profile support must exist");
    *at_mut(support, "/availability") = string(availability);
}

fn object_field<'a>(entries: &'a [(String, JsonValue)], key: &str) -> &'a JsonValue {
    entries
        .iter()
        .find_map(|(candidate, value)| (candidate == key).then_some(value))
        .unwrap_or_else(|| panic!("missing object field {key}"))
}

fn object(entries: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(
        entries
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn insert(object: &mut JsonValue, key: &str, value: JsonValue) {
    match object {
        JsonValue::Object(entries) => entries.push((key.to_owned(), value)),
        _ => panic!("insert target must be an object"),
    }
}

fn string(value: &str) -> JsonValue {
    JsonValue::String(value.to_owned())
}

const fn boolean(value: bool) -> JsonValue {
    JsonValue::Bool(value)
}

#[test]
fn manifest_physical_paths_platforms_profiles_and_scopes_fail_closed() {
    for invalid_path in ["meta//provenance.json", "meta/./provenance.json", "meta/"] {
        let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
        *at_mut(&mut raw, "/artifacts/provenance/path") = string(invalid_path);
        assert_eq!(
            parse_manifest(&raw.to_canonical_json())
                .expect_err("non-canonical artifact path must reject")
                .code,
            ProtocolErrorCode::InvalidManifest
        );
    }

    let mut duplicate_path = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut duplicate_path, "/artifacts/sbom/path") = string("provenance.json");
    assert_eq!(
        parse_manifest(&duplicate_path.to_canonical_json())
            .expect_err("duplicate physical path must reject")
            .code,
        ProtocolErrorCode::InvalidManifest
    );

    let mut duplicate_tuple = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    let platform = at(&duplicate_tuple, "/runtime/platforms/0")
        .expect("platform must exist")
        .clone();
    array_mut(&mut duplicate_tuple, "/runtime/platforms").push(platform);
    assert_eq!(
        parse_manifest(&duplicate_tuple.to_canonical_json())
            .expect_err("duplicate platform tuple must reject")
            .code,
        ProtocolErrorCode::InvalidManifest
    );

    let mut absent_profile = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut absent_profile, "/runtime/platforms/0/profiles").push(string("headless"));
    assert_eq!(
        parse_manifest(&absent_profile.to_canonical_json())
            .expect_err("platform-only profile must reject")
            .code,
        ProtocolErrorCode::IncompatibleContract
    );

    let mut browser_on_native = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut browser_on_native, "/runtime/platforms/0/profiles").push(string("web-runtime"));
    assert_eq!(
        parse_manifest(&browser_on_native.to_canonical_json())
            .expect_err("browser profile on native tuple must reject")
            .code,
        ProtocolErrorCode::IncompatibleContract
    );

    let mut wrong_role = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut wrong_role, "/runtime/profiles/0/artifact_role") = string("browser-derived");
    assert_eq!(
        parse_manifest(&wrong_role.to_canonical_json())
            .expect_err("desktop browser-derived role must reject")
            .code,
        ProtocolErrorCode::IncompatibleContract
    );

    let mut scope_over_resource = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    let stored = at(&scope_over_resource, "/resources/stored_data_bytes")
        .and_then(JsonValue::as_i64)
        .expect("stored data bound must be integral");
    let capabilities = array_mut(&mut scope_over_resource, "/capabilities");
    let kv = capabilities
        .iter_mut()
        .find(|capability| {
            at(capability, "/interface")
                .and_then(JsonValue::as_str)
                .is_some_and(|interface| interface.contains("/kv@"))
        })
        .expect("KV capability must exist");
    *at_mut(kv, "/scope/maximum_storage_bytes") = JsonValue::Integer(stored + 1);
    assert_eq!(
        parse_manifest(&scope_over_resource.to_canonical_json())
            .expect_err("capability scope over resource request must reject")
            .code,
        ProtocolErrorCode::ResourceLimit
    );

    let mut schedule_over_resource =
        parse_json(SERVICE_MANIFEST).expect("service fixture JSON must parse");
    let capabilities = array_mut(&mut schedule_over_resource, "/capabilities");
    let scheduler = capabilities
        .iter_mut()
        .find(|capability| {
            at(capability, "/interface")
                .and_then(JsonValue::as_str)
                .is_some_and(|interface| interface.contains("/scheduler@"))
        })
        .expect("scheduler capability must exist");
    *at_mut(scheduler, "/scope/maximum_schedules") = JsonValue::Integer(5);
    assert_eq!(
        parse_manifest(&schedule_over_resource.to_canonical_json())
            .expect_err("schedule scope over resource request must reject")
            .code,
        ProtocolErrorCode::ResourceLimit
    );

    let ui = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    let mut extra_derivation =
        parse_json(SERVICE_MANIFEST).expect("service fixture JSON must parse");
    let mut web_runtime = at(&ui, "/artifacts/browser_derivations/1")
        .expect("UI web-runtime derivation must exist")
        .clone();
    let canonical_digest = at(&extra_derivation, "/artifacts/canonical_component/sha256")
        .and_then(JsonValue::as_str)
        .expect("service canonical digest must exist")
        .to_owned();
    *at_mut(&mut web_runtime, "/derived_from_sha256") = string(&canonical_digest);
    array_mut(&mut extra_derivation, "/artifacts/browser_derivations").push(web_runtime);
    assert_eq!(
        parse_manifest(&extra_derivation.to_canonical_json())
            .expect_err("derivation for an absent runtime profile must reject")
            .code,
        ProtocolErrorCode::IntegrityFailure
    );
}

#[test]
fn state_range_matches_the_frozen_gate2_rfc() {
    let mut accepted = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut accepted, "/state/schema") = JsonValue::Integer(2);
    *at_mut(&mut accepted, "/state/migratable_from_min") = JsonValue::Integer(1);
    *at_mut(&mut accepted, "/state/migratable_from_max") = JsonValue::Integer(3);
    assert!(parse_manifest(&accepted.to_canonical_json()).is_ok());

    let mut rejected = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut rejected, "/state/schema") = JsonValue::Integer(3);
    *at_mut(&mut rejected, "/state/migratable_from_min") = JsonValue::Integer(1);
    *at_mut(&mut rejected, "/state/migratable_from_max") = JsonValue::Integer(2);
    assert_eq!(
        parse_manifest(&rejected.to_canonical_json())
            .expect_err("schema above migration maximum must reject")
            .code,
        ProtocolErrorCode::IncompatibleContract
    );
}

#[test]
fn admission_enforces_profile_availability_revocation_and_profile_launcher_scope() {
    for (availability, expected_code) in [
        ("denied", ProtocolErrorCode::PermissionDenied),
        ("unavailable", ProtocolErrorCode::CapabilityUnavailable),
    ] {
        let mut raw = parse_json(HYBRID_MANIFEST).expect("hybrid fixture JSON must parse");
        set_capability_availability(&mut raw, "scheduler", Profile::Desktop, availability);
        let manifest = parse_manifest(&raw.to_canonical_json()).expect("mutation must stay valid");
        let outcome = admit(
            &manifest,
            &request(
                &manifest,
                "linux",
                "x86-64",
                Profile::Desktop,
                manifest.runtime.required_imports.clone(),
            ),
        );
        assert_eq!(outcome.decision, AdmissionDecision::Block);
        assert_eq!(outcome.code, Some(expected_code));
    }

    let mut revoked = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut revoked, "/verification/revocation") = string("revoked");
    let manifest = parse_manifest(&revoked.to_canonical_json()).expect("revoked manifest parses");
    let outcome = admit(
        &manifest,
        &request(
            &manifest,
            "macos",
            "aarch64",
            Profile::Desktop,
            manifest.runtime.required_imports.clone(),
        ),
    );
    assert_eq!(outcome.decision, AdmissionDecision::Reject);
    assert_eq!(outcome.code, Some(ProtocolErrorCode::IntegrityFailure));

    let mut profile_scoped = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    let launcher_profiles = array_mut(&mut profile_scoped, "/entrypoints/0/profiles");
    launcher_profiles.retain(|profile| profile.as_str() != Some("web-runtime"));
    let manifest =
        parse_manifest(&profile_scoped.to_canonical_json()).expect("profile-scoped UI parses");
    let outcome = admit(
        &manifest,
        &request(
            &manifest,
            "browser",
            "wasm32",
            Profile::WebRuntime,
            manifest.runtime.required_imports.clone(),
        ),
    );
    assert_eq!(outcome.decision, AdmissionDecision::Accept);
    assert!(!outcome.launcher_surface);
}

#[test]
fn scoped_restore_rejects_forgery_unsafe_fields_and_unscoped_bypass() {
    let manifest = parse_manifest(UI_MANIFEST).expect("UI fixture must parse");
    let session_id = SessionId::from_host("session-1").expect("session ID must validate");
    let surface_id = SurfaceId::from_host("surface-1").expect("surface ID must validate");
    let token = RestoreToken::from_host("restore-1").expect("token must validate");
    let authorization = RestoreAuthorization {
        app_id: manifest.app.id.clone(),
        entrypoint_id: "main".to_owned(),
        session_id: session_id.clone(),
        surface_id: surface_id.clone(),
        route: "note-detail".to_owned(),
        token: token.clone(),
    };
    let enabled = LifecycleState {
        package: PackageStatus::Enabled,
        package_retained: true,
        state_retained: true,
        ..LifecycleState::default()
    };
    let safe = SafeFieldChange::from_host("query", SafeFieldValue::Text("sample".to_owned()))
        .expect("safe text must validate");
    let exact = LauncherOperation::Restore {
        app_id: manifest.app.id.clone(),
        entrypoint_id: "main".to_owned(),
        session_id: session_id.clone(),
        surface_id: surface_id.clone(),
        route: "note-detail".to_owned(),
        token: token.clone(),
        safe_fields: vec![safe.clone()],
    };
    let mut forged = Vec::new();
    let mut wrong_app = exact.clone();
    if let LauncherOperation::Restore { app_id, .. } = &mut wrong_app {
        *app_id = "ai.vibapp.other".to_owned();
    }
    forged.push(wrong_app);
    let mut wrong_entrypoint = exact.clone();
    if let LauncherOperation::Restore { entrypoint_id, .. } = &mut wrong_entrypoint {
        *entrypoint_id = "settings".to_owned();
    }
    forged.push(wrong_entrypoint);
    let mut wrong_session = exact.clone();
    if let LauncherOperation::Restore { session_id, .. } = &mut wrong_session {
        *session_id = SessionId::from_host("session-other").expect("session ID must validate");
    }
    forged.push(wrong_session);
    let mut wrong_surface = exact.clone();
    if let LauncherOperation::Restore { surface_id, .. } = &mut wrong_surface {
        *surface_id = SurfaceId::from_host("surface-other").expect("surface ID must validate");
    }
    forged.push(wrong_surface);
    let mut wrong_route = exact.clone();
    if let LauncherOperation::Restore { route, .. } = &mut wrong_route {
        *route = "notes".to_owned();
    }
    forged.push(wrong_route);
    let mut wrong_token = exact.clone();
    if let LauncherOperation::Restore { token, .. } = &mut wrong_token {
        *token = RestoreToken::from_host("restore-other").expect("token must validate");
    }
    forged.push(wrong_token);
    for operation in forged {
        assert_eq!(
            restore_session(
                &manifest,
                enabled,
                Profile::Desktop,
                &authorization,
                &operation,
            ),
            Err(LifecycleError::ForgedSession)
        );
    }

    let unknown_route_authorization = RestoreAuthorization {
        route: "not-allowed".to_owned(),
        ..authorization.clone()
    };
    let unknown_route = LauncherOperation::Restore {
        app_id: manifest.app.id.clone(),
        entrypoint_id: "main".to_owned(),
        session_id: authorization.session_id.clone(),
        surface_id: authorization.surface_id.clone(),
        route: "not-allowed".to_owned(),
        token: authorization.token.clone(),
        safe_fields: Vec::new(),
    };
    assert_eq!(
        restore_session(
            &manifest,
            enabled,
            Profile::Desktop,
            &unknown_route_authorization,
            &unknown_route,
        ),
        Err(LifecycleError::RouteUnavailable)
    );

    let duplicate_fields = LauncherOperation::Restore {
        app_id: manifest.app.id.clone(),
        entrypoint_id: "main".to_owned(),
        session_id: authorization.session_id.clone(),
        surface_id: authorization.surface_id.clone(),
        route: authorization.route.clone(),
        token: authorization.token.clone(),
        safe_fields: vec![safe.clone(), safe.clone()],
    };
    assert_eq!(
        restore_session(
            &manifest,
            enabled,
            Profile::Desktop,
            &authorization,
            &duplicate_fields,
        ),
        Err(LifecycleError::UnsafeRestoration)
    );

    let mut route_only_raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut route_only_raw, "/entrypoints/0/restoration") = string("route-only");
    let route_only =
        parse_manifest(&route_only_raw.to_canonical_json()).expect("route-only manifest parses");
    let route_only_operation = LauncherOperation::Restore {
        app_id: route_only.app.id.clone(),
        entrypoint_id: "main".to_owned(),
        session_id: authorization.session_id.clone(),
        surface_id: authorization.surface_id.clone(),
        route: "note-detail".to_owned(),
        token,
        safe_fields: vec![safe],
    };
    assert_eq!(
        restore_session(
            &route_only,
            enabled,
            Profile::Desktop,
            &authorization,
            &route_only_operation,
        ),
        Err(LifecycleError::UnsafeRestoration)
    );
    assert_eq!(
        apply_surface_operation(
            enabled,
            &manifest,
            Profile::Desktop,
            SurfaceOperation::Restore,
        ),
        Err(LifecycleError::InvalidSurfaceTransition)
    );
}

#[test]
fn schema_package_contract_and_wasi_versions_are_all_fail_closed() {
    for (pointer, replacement) in [
        ("/schema_version", "vibapp.manifest.experimental-v0.0.2"),
        ("/package_format", "vibapp.package.experimental-v1"),
        ("/runtime/contract", "vibapp:experimental-v0@0.0.2"),
        ("/runtime/wasi", "0.3"),
    ] {
        let mut raw = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
        *at_mut(&mut raw, pointer) = string(replacement);
        assert_eq!(
            parse_manifest(&raw.to_canonical_json())
                .expect_err("unsupported version must reject")
                .code,
            ProtocolErrorCode::UnsupportedVersion
        );
    }
}

#[test]
fn manifest_errors_report_executed_schema_classification() {
    let mut control_interface = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut control_interface, "/runtime/required_imports")
        .push(string("vibapp:experimental-v0/control@0.0.1"));
    let control_error = parse_manifest(&control_interface.to_canonical_json())
        .expect_err("control interface is outside the manifest schema enum");
    assert_eq!(control_error.code, ProtocolErrorCode::UnknownInterface);
    assert_eq!(control_error.schema_valid(), Some(false));

    let mut resource_limit = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    *at_mut(&mut resource_limit, "/resources/linear_memory_bytes") = JsonValue::Integer(67_108_865);
    let resource_error = parse_manifest(&resource_limit.to_canonical_json())
        .expect_err("resource maximum is enforced by the manifest schema");
    assert_eq!(resource_error.code, ProtocolErrorCode::ResourceLimit);
    assert_eq!(resource_error.schema_valid(), Some(false));

    let ui = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    let launcher = at(&ui, "/entrypoints/0")
        .expect("UI launcher must exist")
        .clone();
    let mut forged_service = parse_json(SERVICE_MANIFEST).expect("service fixture JSON must parse");
    array_mut(&mut forged_service, "/entrypoints").push(launcher);
    let forged_service_error = parse_manifest(&forged_service.to_canonical_json())
        .expect_err("service packages cannot contain launcher entrypoints");
    assert_eq!(
        forged_service_error.code,
        ProtocolErrorCode::IncompatibleContract
    );
    assert_eq!(forged_service_error.schema_valid(), Some(false));

    let mut missing_launcher = parse_json(UI_MANIFEST).expect("UI fixture JSON must parse");
    array_mut(&mut missing_launcher, "/entrypoints").retain(|entrypoint| {
        at(entrypoint, "/kind").and_then(JsonValue::as_str) != Some("launcher-ui")
    });
    let missing_launcher_error = parse_manifest(&missing_launcher.to_canonical_json())
        .expect_err("UI packages require a launcher entrypoint");
    assert_eq!(
        missing_launcher_error.code,
        ProtocolErrorCode::IncompatibleContract
    );
    assert_eq!(missing_launcher_error.schema_valid(), Some(false));

    let mut live_preview = parse_json(SERVICE_MANIFEST).expect("service fixture JSON must parse");
    set_capability_availability(
        &mut live_preview,
        "system-metrics",
        Profile::WebPreview,
        "native",
    );
    let live_preview_error = parse_manifest(&live_preview.to_canonical_json())
        .expect_err("live hardware in web preview violates a semantic invariant");
    assert_eq!(
        live_preview_error.code,
        ProtocolErrorCode::IncompatibleContract
    );
    assert_eq!(live_preview_error.schema_valid(), Some(true));

    let mut wrong_derivation = parse_json(HYBRID_MANIFEST).expect("hybrid fixture JSON must parse");
    *at_mut(
        &mut wrong_derivation,
        "/artifacts/browser_derivations/0/derived_from_sha256",
    ) = string("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff");
    let derivation_error = parse_manifest(&wrong_derivation.to_canonical_json())
        .expect_err("wrong derivation digest violates a semantic invariant");
    assert_eq!(derivation_error.code, ProtocolErrorCode::IntegrityFailure);
    assert_eq!(derivation_error.schema_valid(), Some(true));
}
