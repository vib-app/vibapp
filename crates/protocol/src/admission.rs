//! Deterministic pre-instantiation admission and reconciliation.

use std::collections::BTreeSet;

use crate::manifest::ProtocolErrorCode;
use crate::model::{Availability, EntrypointKind, Manifest, Necessity, Profile, RevocationStatus};
use crate::wit_catalog::{APP_INTERFACES, reconcile_world_imports};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostTarget {
    pub os: String,
    pub arch: String,
    pub profile: Profile,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmissionRequest {
    pub host: HostTarget,
    pub actual_component_imports: Vec<String>,
    pub host_supported_imports: Vec<String>,
    pub denied_permissions: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionDecision {
    Accept,
    Block,
    Reject,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmissionOutcome {
    pub decision: AdmissionDecision,
    pub code: Option<ProtocolErrorCode>,
    pub executable: bool,
    pub launcher_surface: bool,
    pub preview_only: bool,
    pub detail: String,
}

impl AdmissionOutcome {
    fn accept(manifest: &Manifest, profile: Profile) -> Self {
        let launcher_surface = profile != Profile::Headless
            && manifest.entrypoints.iter().any(|entrypoint| {
                entrypoint.kind == EntrypointKind::LauncherUi
                    && entrypoint.profiles.contains(&profile)
            });
        Self {
            decision: AdmissionDecision::Accept,
            code: None,
            executable: true,
            launcher_surface,
            preview_only: profile == Profile::WebPreview,
            detail: "pre-instantiation checks passed".to_owned(),
        }
    }

    fn reject(code: ProtocolErrorCode, detail: impl Into<String>) -> Self {
        Self {
            decision: AdmissionDecision::Reject,
            code: Some(code),
            executable: false,
            launcher_surface: false,
            preview_only: false,
            detail: detail.into(),
        }
    }

    fn block(code: ProtocolErrorCode, detail: impl Into<String>) -> Self {
        Self {
            decision: AdmissionDecision::Block,
            code: Some(code),
            executable: false,
            launcher_surface: false,
            preview_only: false,
            detail: detail.into(),
        }
    }
}

/// Admit a validated package. This function is pure and must run before instantiation.
#[must_use]
pub fn admit(manifest: &Manifest, request: &AdmissionRequest) -> AdmissionOutcome {
    if manifest.verification.revocation == RevocationStatus::Revoked {
        return AdmissionOutcome::reject(
            ProtocolErrorCode::IntegrityFailure,
            "package verification status is revoked",
        );
    }

    if reconcile_world_imports(manifest.runtime.world, &manifest.runtime.required_imports).is_err()
    {
        return AdmissionOutcome::reject(
            ProtocolErrorCode::IncompatibleContract,
            "manifest imports do not match the selected accepted WIT world",
        );
    }

    let actual = request
        .actual_component_imports
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let declared = manifest
        .runtime
        .required_imports
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    if actual
        .iter()
        .any(|interface| !APP_INTERFACES.contains(interface))
    {
        return AdmissionOutcome::reject(
            ProtocolErrorCode::UnknownInterface,
            "component imports an interface outside the app capability allowlist",
        );
    }
    if actual != declared {
        return AdmissionOutcome::reject(
            ProtocolErrorCode::IncompatibleContract,
            "component imports and manifest required_imports differ",
        );
    }

    let supported = request
        .host_supported_imports
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    if let Some(missing) = declared
        .iter()
        .find(|interface| !supported.contains(**interface))
    {
        return AdmissionOutcome::reject(
            ProtocolErrorCode::MissingInterface,
            format!("host does not support {missing}"),
        );
    }

    let platform_supported = manifest.runtime.platforms.iter().any(|platform| {
        platform.os == request.host.os
            && platform.arch == request.host.arch
            && platform.profiles.contains(&request.host.profile)
    });
    if !platform_supported || !manifest.supports_profile(request.host.profile) {
        return AdmissionOutcome::reject(
            ProtocolErrorCode::IncompatibleContract,
            "package does not support the requested OS/architecture/profile tuple",
        );
    }

    for denied in &request.denied_permissions {
        if let Some(capability) = manifest
            .capabilities
            .iter()
            .find(|capability| capability.interface == *denied)
        {
            if capability.necessity == Necessity::Required {
                return AdmissionOutcome::block(
                    ProtocolErrorCode::PermissionDenied,
                    format!("required capability {denied} is denied"),
                );
            }
        }
    }

    for capability in &manifest.capabilities {
        let Some(support) = capability
            .profiles
            .iter()
            .find(|support| support.profile == request.host.profile)
        else {
            return AdmissionOutcome::reject(
                ProtocolErrorCode::IncompatibleContract,
                format!(
                    "capability {} does not declare the requested profile",
                    capability.interface
                ),
            );
        };
        if capability.necessity == Necessity::Required {
            match support.availability {
                Availability::Denied => {
                    return AdmissionOutcome::block(
                        ProtocolErrorCode::PermissionDenied,
                        format!(
                            "required capability {} is denied for the requested profile",
                            capability.interface
                        ),
                    );
                }
                Availability::Unavailable => {
                    return AdmissionOutcome::block(
                        ProtocolErrorCode::CapabilityUnavailable,
                        format!(
                            "required capability {} is unavailable for the requested profile",
                            capability.interface
                        ),
                    );
                }
                Availability::Native | Availability::Brokered | Availability::Mock => {}
            }
        }
    }

    if request.host.profile == Profile::WebPreview {
        for capability in &manifest.capabilities {
            if let Some(support) = capability
                .profiles
                .iter()
                .find(|support| support.profile == Profile::WebPreview)
            {
                let live_forbidden = matches!(
                    capability.interface.as_str(),
                    "vibapp:experimental-v0/scheduler@0.0.1"
                        | "vibapp:experimental-v0/notification@0.0.1"
                        | "vibapp:experimental-v0/system-metrics@0.0.1"
                        | "vibapp:experimental-v0/http@0.0.1"
                ) && matches!(
                    support.availability,
                    Availability::Native | Availability::Brokered
                );
                if live_forbidden {
                    return AdmissionOutcome::reject(
                        ProtocolErrorCode::IncompatibleContract,
                        "web-preview hardware and background capabilities must be mock, denied or unavailable",
                    );
                }
            }
        }
    }

    AdmissionOutcome::accept(manifest, request.host.profile)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OutputBounds {
    pub declared_bytes: u64,
}

pub fn validate_output_size(
    bounds: OutputBounds,
    actual_bytes: u64,
) -> Result<(), ProtocolErrorCode> {
    if actual_bytes > bounds.declared_bytes {
        Err(ProtocolErrorCode::ResourceLimit)
    } else {
        Ok(())
    }
}
