//! Checked catalog of the accepted `experimental-v0` WIT worlds.

use crate::model::World;

pub const CONTRACT_VERSION: &str = "vibapp:experimental-v0@0.0.1";

pub const CLOCK: &str = "vibapp:experimental-v0/clock@0.0.1";
pub const SCHEDULER: &str = "vibapp:experimental-v0/scheduler@0.0.1";
pub const NOTIFICATION: &str = "vibapp:experimental-v0/notification@0.0.1";
pub const KV: &str = "vibapp:experimental-v0/kv@0.0.1";
pub const LOG: &str = "vibapp:experimental-v0/log@0.0.1";
pub const HOST_INFO: &str = "vibapp:experimental-v0/host-info@0.0.1";
pub const SETTINGS: &str = "vibapp:experimental-v0/settings@0.0.1";
pub const SYSTEM_METRICS: &str = "vibapp:experimental-v0/system-metrics@0.0.1";
pub const HTTP: &str = "vibapp:experimental-v0/http@0.0.1";

pub const APP_INTERFACES: [&str; 9] = [
    CLOCK,
    SCHEDULER,
    NOTIFICATION,
    KV,
    LOG,
    HOST_INFO,
    SETTINGS,
    SYSTEM_METRICS,
    HTTP,
];

const UI_ONLY_IMPORTS: [&str; 5] = [CLOCK, KV, LOG, HOST_INFO, SETTINGS];
const SERVICE_ONLY_IMPORTS: [&str; 8] = [
    CLOCK,
    SCHEDULER,
    KV,
    LOG,
    HOST_INFO,
    SETTINGS,
    SYSTEM_METRICS,
    HTTP,
];
const HYBRID_IMPORTS: [&str; 7] = [CLOCK, SCHEDULER, NOTIFICATION, KV, LOG, HOST_INFO, SETTINGS];

#[must_use]
pub fn is_app_interface(interface: &str) -> bool {
    APP_INTERFACES.contains(&interface)
}

/// Return the imports declared by the accepted WIT world, in WIT declaration order.
#[must_use]
pub const fn imports_for_world(world: World) -> &'static [&'static str] {
    match world {
        World::UiOnly => &UI_ONLY_IMPORTS,
        World::ServiceOnly | World::WebPreview => &SERVICE_ONLY_IMPORTS,
        World::Hybrid => &HYBRID_IMPORTS,
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WitCatalogMismatch {
    pub missing_from_manifest: Vec<String>,
    pub absent_from_world: Vec<String>,
}

/// Reconcile a manifest's imports with the fixed WIT world before instantiation.
pub fn reconcile_world_imports(
    world: World,
    manifest_imports: &[String],
) -> Result<(), WitCatalogMismatch> {
    let world_imports = imports_for_world(world);
    let missing_from_manifest = world_imports
        .iter()
        .filter(|interface| !manifest_imports.iter().any(|value| value == **interface))
        .map(|interface| (*interface).to_owned())
        .collect::<Vec<_>>();
    let absent_from_world = manifest_imports
        .iter()
        .filter(|interface| !world_imports.contains(&interface.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    if missing_from_manifest.is_empty() && absent_from_world.is_empty() {
        Ok(())
    } else {
        Err(WitCatalogMismatch {
            missing_from_manifest,
            absent_from_world,
        })
    }
}
