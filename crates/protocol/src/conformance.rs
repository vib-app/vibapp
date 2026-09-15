//! Deterministic inventory of the hand-authored Stage 0 conformance vectors.

use std::collections::BTreeSet;

use crate::json::{JsonError, JsonValue, parse_json};

pub const VECTOR_SCHEMA: &str = "vibapp.conformance.experimental-v0.0.1";

/// Vectors whose host-independent validation or typing is owned by Tranche 1.
/// A vector is credited only when the exact runner consumes all non-deferred fields.
pub const TRANCHE1_VECTOR_IDS: [&str; 25] = [
    "current-ui-only-desktop",
    "current-service-only-linux-arm64-headless",
    "current-hybrid-desktop",
    "web-preview-service-uses-mocks",
    "web-runtime-ui-is-foreground-only",
    "close-hybrid-ui-does-not-disable-service",
    "disable-service-retains-package-and-state",
    "uninstall-delete-data",
    "uninstall-retain-data",
    "uninstall-disposition-not-allowed",
    "missing-host-import",
    "unknown-required-import",
    "unsupported-contract-version",
    "daemon-control-interface-is-not-an-app-capability",
    "resource-request-over-limit",
    "service-kind-forges-launcher-entrypoint",
    "ui-kind-missing-launcher-entrypoint",
    "component-import-not-declared-in-manifest",
    "manifest-import-not-present-in-component",
    "permission-denied-required-capability",
    "malformed-ui-output",
    "runtime-output-exceeds-declared-bound",
    "web-preview-requests-live-hardware",
    "browser-derived-digest-does-not-reference-canonical",
    "valid-launcher-restoration",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DeferredVectorField {
    pub vector_id: &'static str,
    /// JSON Pointer prefix rooted at the vector object (`/given` or `/expected`).
    pub pointer_prefix: &'static str,
    pub later_owner: &'static str,
}

/// Runtime effects deliberately not credited to Tranche 1. A prefix owns its
/// complete subtree; every other leaf in the 25-vector set is Tranche 1-owned.
pub const DEFERRED_VECTOR_FIELDS: &[DeferredVectorField] = &[
    DeferredVectorField {
        vector_id: "current-service-only-linux-arm64-headless",
        pointer_prefix: "/given/host/control_surface",
        later_owner: "launcher-cli",
    },
    DeferredVectorField {
        vector_id: "current-service-only-linux-arm64-headless",
        pointer_prefix: "/expected/cli_operations",
        later_owner: "launcher-cli",
    },
    DeferredVectorField {
        vector_id: "web-preview-service-uses-mocks",
        pointer_prefix: "/expected/real_hardware_reads",
        later_owner: "browser-poc",
    },
    DeferredVectorField {
        vector_id: "web-preview-service-uses-mocks",
        pointer_prefix: "/expected/real_network_requests",
        later_owner: "browser-poc",
    },
    DeferredVectorField {
        vector_id: "web-preview-service-uses-mocks",
        pointer_prefix: "/expected/closed_page_reliability_claim",
        later_owner: "browser-poc",
    },
    DeferredVectorField {
        vector_id: "web-runtime-ui-is-foreground-only",
        pointer_prefix: "/given/page_state",
        later_owner: "browser-poc",
    },
    DeferredVectorField {
        vector_id: "web-runtime-ui-is-foreground-only",
        pointer_prefix: "/expected/real_ui_state_allowed",
        later_owner: "browser-poc",
    },
    DeferredVectorField {
        vector_id: "web-runtime-ui-is-foreground-only",
        pointer_prefix: "/expected/closed_page_reliability_claim",
        later_owner: "browser-poc",
    },
    DeferredVectorField {
        vector_id: "close-hybrid-ui-does-not-disable-service",
        pointer_prefix: "/given/service_instance",
        later_owner: "service-host",
    },
    DeferredVectorField {
        vector_id: "close-hybrid-ui-does-not-disable-service",
        pointer_prefix: "/given/active_schedule_ids",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "close-hybrid-ui-does-not-disable-service",
        pointer_prefix: "/expected/service_instance",
        later_owner: "service-host",
    },
    DeferredVectorField {
        vector_id: "close-hybrid-ui-does-not-disable-service",
        pointer_prefix: "/expected/active_schedule_ids",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/given/package_digest",
        later_owner: "package-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/given/state_revision",
        later_owner: "state-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/given/active_schedule_ids",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/given/pending_occurrence_ids",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/expected/state_revision",
        later_owner: "state-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/expected/active_schedule_ids",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/expected/schedule_definitions_and_projections_retained",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "disable-service-retains-package-and-state",
        pointer_prefix: "/expected/pending_occurrence_ids_retained_but_quiesced",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-delete-data",
        pointer_prefix: "/given/state_revision",
        later_owner: "state-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-delete-data",
        pointer_prefix: "/expected/settings_present",
        later_owner: "settings-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-delete-data",
        pointer_prefix: "/expected/schedules_present",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-delete-data",
        pointer_prefix: "/expected/effects_allowed",
        later_owner: "effect-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-delete-data",
        pointer_prefix: "/expected/security_audit_retained_under_host_policy",
        later_owner: "audit-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-retain-data",
        pointer_prefix: "/given/state_revision",
        later_owner: "state-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-retain-data",
        pointer_prefix: "/expected/archived_state_accessible_to_guest",
        later_owner: "state-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-retain-data",
        pointer_prefix: "/expected/schedules_present",
        later_owner: "scheduler-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-retain-data",
        pointer_prefix: "/expected/effects_allowed",
        later_owner: "effect-host",
    },
    DeferredVectorField {
        vector_id: "uninstall-retain-data",
        pointer_prefix: "/expected/restore_requires_same_publisher_app_and_user_consent",
        later_owner: "state-host",
    },
    DeferredVectorField {
        vector_id: "missing-host-import",
        pointer_prefix: "/expected/previous_generation_unchanged",
        later_owner: "activation-host",
    },
    DeferredVectorField {
        vector_id: "permission-denied-required-capability",
        pointer_prefix: "/expected/activated",
        later_owner: "activation-host",
    },
    DeferredVectorField {
        vector_id: "permission-denied-required-capability",
        pointer_prefix: "/expected/service_started",
        later_owner: "service-host",
    },
    DeferredVectorField {
        vector_id: "permission-denied-required-capability",
        pointer_prefix: "/expected/previous_generation_unchanged",
        later_owner: "activation-host",
    },
    DeferredVectorField {
        vector_id: "malformed-ui-output",
        pointer_prefix: "/expected/candidate_generation_disposed",
        later_owner: "activation-host",
    },
    DeferredVectorField {
        vector_id: "malformed-ui-output",
        pointer_prefix: "/expected/previous_generation_unchanged",
        later_owner: "activation-host",
    },
    DeferredVectorField {
        vector_id: "runtime-output-exceeds-declared-bound",
        pointer_prefix: "/expected/guest_store_disposed",
        later_owner: "runtime-host",
    },
    DeferredVectorField {
        vector_id: "runtime-output-exceeds-declared-bound",
        pointer_prefix: "/expected/partial_output_rendered",
        later_owner: "launcher-host",
    },
    DeferredVectorField {
        vector_id: "browser-derived-digest-does-not-reference-canonical",
        pointer_prefix: "/expected/before_loading_derived_code",
        later_owner: "browser-host",
    },
];

#[must_use]
pub fn deferred_owner(vector_id: &str, pointer: &str) -> Option<&'static str> {
    DEFERRED_VECTOR_FIELDS.iter().find_map(|field| {
        let subtree = pointer
            .strip_prefix(field.pointer_prefix)
            .is_some_and(|suffix| suffix.is_empty() || suffix.starts_with('/'));
        (field.vector_id == vector_id && subtree).then_some(field.later_owner)
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VectorDescriptor {
    pub id: String,
    pub class: String,
    pub phase: String,
    pub manifest: String,
    pub given: JsonValue,
    pub expected: JsonValue,
    pub expected_decision: String,
    pub expected_code: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConformanceCatalog {
    pub vectors: Vec<VectorDescriptor>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CatalogError {
    Json(JsonError),
    Shape(String),
    DuplicateId(String),
}

impl From<JsonError> for CatalogError {
    fn from(error: JsonError) -> Self {
        Self::Json(error)
    }
}

impl ConformanceCatalog {
    pub fn parse(input: &str) -> Result<Self, CatalogError> {
        let root = parse_json(input)?;
        let root_object = root
            .as_object()
            .ok_or_else(|| CatalogError::Shape("catalog root must be an object".to_owned()))?;
        if field_string(root_object, "vector_schema")? != VECTOR_SCHEMA {
            return Err(CatalogError::Shape("unsupported vector_schema".to_owned()));
        }
        let values = field(root_object, "vectors")?
            .as_array()
            .ok_or_else(|| CatalogError::Shape("vectors must be an array".to_owned()))?;
        let mut seen = BTreeSet::new();
        let mut vectors = Vec::with_capacity(values.len());
        for value in values {
            let object = value
                .as_object()
                .ok_or_else(|| CatalogError::Shape("vector must be an object".to_owned()))?;
            let id = field_string(object, "id")?.to_owned();
            if !seen.insert(id.clone()) {
                return Err(CatalogError::DuplicateId(id));
            }
            let expected = field(object, "expected")?
                .as_object()
                .ok_or_else(|| CatalogError::Shape("expected must be an object".to_owned()))?;
            let expected_code = match field(expected, "code")? {
                JsonValue::Null => None,
                JsonValue::String(code) => Some(code.clone()),
                _ => {
                    return Err(CatalogError::Shape(
                        "expected.code must be string or null".to_owned(),
                    ));
                }
            };
            vectors.push(VectorDescriptor {
                id,
                class: field_string(object, "class")?.to_owned(),
                phase: field_string(object, "phase")?.to_owned(),
                manifest: field_string(object, "manifest")?.to_owned(),
                given: field(object, "given")?.clone(),
                expected: field(object, "expected")?.clone(),
                expected_decision: field_string(expected, "decision")?.to_owned(),
                expected_code,
            });
        }
        Ok(Self { vectors })
    }

    #[must_use]
    pub fn tranche1_vectors(&self) -> Vec<&VectorDescriptor> {
        self.vectors
            .iter()
            .filter(|vector| TRANCHE1_VECTOR_IDS.contains(&vector.id.as_str()))
            .collect()
    }
}

type ObjectEntries = [(String, JsonValue)];

fn field<'a>(object: &'a ObjectEntries, key: &str) -> Result<&'a JsonValue, CatalogError> {
    object
        .iter()
        .find_map(|(candidate, value)| (candidate == key).then_some(value))
        .ok_or_else(|| CatalogError::Shape(format!("missing {key}")))
}

fn field_string<'a>(object: &'a ObjectEntries, key: &str) -> Result<&'a str, CatalogError> {
    field(object, key)?
        .as_str()
        .ok_or_else(|| CatalogError::Shape(format!("{key} must be a string")))
}
