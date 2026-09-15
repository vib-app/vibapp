//! Contract-derived protocol types and deterministic validation for VibApp Stage 0.
//!
//! This crate is intentionally dependency-free in Tranche 1. Canonical definitions
//! remain in the accepted JSON Schema, RFC, and WIT files at the repository root.

#![forbid(unsafe_code)]

pub mod admission;
pub mod conformance;
pub mod json;
pub mod lifecycle;
pub mod manifest;
pub mod model;
pub mod output;
pub mod wit_catalog;

pub use admission::{
    AdmissionDecision, AdmissionOutcome, AdmissionRequest, HostTarget, OutputBounds, admit,
    validate_output_size,
};
pub use conformance::{
    ConformanceCatalog, DEFERRED_VECTOR_FIELDS, DeferredVectorField, TRANCHE1_VECTOR_IDS,
    VectorDescriptor, deferred_owner,
};
pub use lifecycle::{
    AppListing, IdentifierError, LauncherOperation, LifecycleError, LifecycleState,
    PackageOperation, PackageStatus, RestoreAuthorization, RestoreToken, SafeFieldChange,
    SafeFieldValue, SessionId, SessionState, SurfaceId, SurfaceOperation, SurfaceStatus,
    apply_package_operation, apply_session_operation, apply_surface_operation, list_apps,
    open_session, restore_session,
};
pub use manifest::{ManifestError, ProtocolErrorCode, parse_manifest};
pub use model::{
    AppKind, Availability, DataDisposition, EntrypointKind, Manifest, Necessity, Profile, World,
};
pub use output::{ViewNode, ViewShape, validate_view_shape};
