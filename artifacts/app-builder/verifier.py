#!/usr/bin/env python3
"""Logically independent verifier and sole candidate-ready promotion authority."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

try:
    import fcntl
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime-daemon"))
    from vibapp_daemon.host_storage import fcntl

from common import (
    APP_ID_RE,
    CAPABILITY_GRANT_POLICY,
    LOCAL_HOST_CAPABILITY_AVAILABILITY,
    SEMVER_RE,
    PipelineError,
    ProcessLimits,
    atomic_json,
    canonical_json,
    derive_host_presentation,
    ensure_private_directory,
    exclusive_copy,
    load_json,
    lstat_regular,
    normalized_relative_path,
    now_utc,
    package_digest,
    read_bounded,
    remove_tree,
    require_exact_object,
    require_sha256,
    run_bounded,
    sha256_bytes,
    sha256_file,
    source_tree_digest,
    validate_host_presentation,
)
from descriptor_reconciliation import (
    COMPONENT_INSPECTOR_SHA256,
    DEFAULT_COMPONENT_INSPECTOR,
    descriptor_inspector_preflight,
    run_guest_descriptor_reconciliation,
)


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
DEFAULT_WASM_TOOLS = (
    REPO
    / "generated/tool-layers/sha256-89f275ce8d6104f7932381986e619f34926eaac20e95d6ecb6ca1dc438d11980/bin/wasm-tools"
)
WASM_TOOLS_SHA256 = "e4372050bfdf733fd2c5d1f7287097862ab6d10c852107c66437e2cd395cdb41"
VERIFIER_VERSION = "app-verifier.experimental-v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_COMPONENT_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
EXPECTED_PACKAGE_FILES = {
    "manifest.json",
    "component.wasm",
    "provenance.json",
    "sbom.cdx.json",
}
APP_IMPORTS = {
    "vibapp:experimental-v0/clock@0.0.1",
    "vibapp:experimental-v0/scheduler@0.0.1",
    "vibapp:experimental-v0/notification@0.0.1",
    "vibapp:experimental-v0/kv@0.0.1",
    "vibapp:experimental-v0/log@0.0.1",
    "vibapp:experimental-v0/host-info@0.0.1",
    "vibapp:experimental-v0/settings@0.0.1",
    "vibapp:experimental-v0/system-metrics@0.0.1",
    "vibapp:experimental-v0/http@0.0.1",
}
TYPE_ONLY_IMPORTS = {
    "vibapp:experimental-v0/common@0.0.1",
    "vibapp:experimental-v0/ui@0.0.1",
    "vibapp:experimental-v0/scheduler@0.0.1",
}
WORLD_IMPORTS = {
    "ui-only-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
    ],
    "service-only-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
        "vibapp:experimental-v0/system-metrics@0.0.1",
        "vibapp:experimental-v0/http@0.0.1",
    ],
    "hybrid-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/notification@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
    ],
}
WORLD_KIND = {
    "ui-only-reference": "ui",
    "service-only-reference": "service",
    "hybrid-reference": "hybrid",
}
SERVICE_TRIGGER_ORDER = ("on-enable", "scheduler", "manual")
ENTRYPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _require_integer(value: Any, context: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise PipelineError("schema-invalid", f"{context} is outside {minimum}..{maximum}")
    return value


def _validate_manifest_schema(manifest: dict[str, Any]) -> None:
    if manifest.get("artifacts", {}).get("browser_derivations"):
        return _validate_product_browser_manifest(manifest)
    return _validate_native_manifest_schema(manifest)


def _validate_product_browser_manifest(manifest: dict[str, Any]) -> None:
    """A product extension, not a change to the frozen Stage 0 profile.

    Validate every new field before reducing to the already closed native
    manifest validator. Browser bytes still require independent rederivation.
    """
    value = copy.deepcopy(manifest)
    if value.get("app", {}).get("kind") != "ui" or value.get("privacy") != {
        "network_access": "none", "stores_personal_data": False
    }:
        raise PipelineError("incompatible-contract", "stateless browser policy requires a non-personal, offline UI")
    derivations = value["artifacts"].get("browser_derivations")
    if not isinstance(derivations, list) or len(derivations) != 1:
        raise PipelineError("schema-invalid", "one product web-runtime derivation is required")
    derivation = require_exact_object(derivations[0], {"profile", "format", "derived_from_sha256", "entry", "files", "derivation_attestation"}, "browser derivation")
    if derivation["profile"] != "web-runtime" or derivation["format"] != "jco-esm" or derivation["derived_from_sha256"] != value["artifacts"]["canonical_component"]["sha256"]:
        raise PipelineError("integrity-failure", "browser derivation canonical component/profile mismatch")
    if not isinstance(derivation["files"], list) or len(derivation["files"]) != 2:
        raise PipelineError("schema-invalid", "browser derivation requires entry and host adapter")
    paths = set()
    for descriptor in [*derivation["files"], derivation["derivation_attestation"]]:
        require_exact_object(descriptor, {"path", "media_type", "sha256", "size_bytes"}, "browser artifact")
        name = normalized_relative_path(descriptor["path"], "browser artifact path")
        if not re.fullmatch(r"web-runtime/(?:app-[0-9a-f]{64}\.jco\.mjs|host-adapter-[0-9a-f]{64}\.mjs|derivation-[0-9a-f]{64}\.json)", name) or name in paths:
            raise PipelineError("schema-invalid", "browser artifact path is invalid/duplicate")
        require_sha256(descriptor["sha256"], "browser artifact digest")
        if descriptor["sha256"] not in name or descriptor["media_type"] != ("application/json" if name.endswith(".json") else "text/javascript"):
            raise PipelineError("integrity-failure", "browser artifact content-address/media type mismatch")
        _require_integer(descriptor["size_bytes"], "browser artifact bytes", 1, 4 * 1024 * 1024)
        paths.add(name)
    if derivation["entry"] not in derivation["files"] or not derivation["entry"]["path"].endswith(".jco.mjs") or not derivation["derivation_attestation"]["path"].endswith(".json"):
        raise PipelineError("schema-invalid", "browser entry/attestation selector is invalid")
    profiles = value.get("runtime", {}).get("profiles", [])
    browser = [row for row in profiles if row.get("profile") == "web-runtime"]
    if len(browser) != 1 or browser[0] != {
        "profile": "web-runtime", "mode": "degraded", "background": "foreground-only", "artifact_role": "browser-derived",
        "degradation": "Foreground stateless UI only; read-only empty settings/state, no saved data or background service."
    }:
        raise PipelineError("incompatible-contract", "browser profile policy is not exact")
    value["runtime"]["profiles"] = [row for row in profiles if row.get("profile") != "web-runtime"]
    browser_platform = {"os": "browser", "arch": "wasm32", "profiles": ["web-runtime"]}
    if value["runtime"]["platforms"].count(browser_platform) != 1:
        raise PipelineError("incompatible-contract", "browser platform is missing")
    value["runtime"]["platforms"].remove(browser_platform)
    for row in value["entrypoints"]:
        if row["kind"] != "launcher-ui" or row["profiles"].count("web-runtime") != 1:
            raise PipelineError("incompatible-contract", "browser entrypoint must be launcher UI")
        row["profiles"].remove("web-runtime")
    for capability in value["capabilities"]:
        rows = [row for row in capability["profiles"] if row.get("profile") == "web-runtime"]
        if len(rows) != 1 or rows[0] != {"profile": "web-runtime", "availability": "brokered", "behavior": "Bounded stateless foreground host; no persistence, network or background authority."}:
            raise PipelineError("incompatible-contract", "browser capability policy is not exact")
        capability["profiles"].remove(rows[0])
    value["artifacts"]["browser_derivations"] = []
    _validate_native_manifest_schema(value)


def _validate_native_manifest_schema(manifest: dict[str, Any]) -> None:
    require_exact_object(
        manifest,
        {
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
        },
        "manifest",
    )
    if (
        manifest["schema_version"] != "vibapp.manifest.experimental-v0.0.1"
        or manifest["package_format"] != "vibapp.package.experimental-v0"
    ):
        raise PipelineError("unsupported-version", "manifest schema/package format is unsupported")
    app = require_exact_object(
        manifest["app"], {"id", "version", "kind", "display_name", "description", "publisher"}, "app"
    )
    if not isinstance(app["id"], str) or not APP_ID_RE.fullmatch(app["id"]):
        raise PipelineError("schema-invalid", "manifest app ID is invalid")
    if not isinstance(app["version"], str) or not SEMVER_RE.fullmatch(app["version"]):
        raise PipelineError("schema-invalid", "manifest app version is invalid")
    if app["kind"] not in {"ui", "service", "hybrid"}:
        raise PipelineError("schema-invalid", "manifest app kind is invalid")
    for key, limit in (("display_name", 80), ("description", 1000)):
        if not isinstance(app[key], str) or not app[key] or len(app[key].encode("utf-8")) > limit:
            raise PipelineError("schema-invalid", f"manifest app {key} is invalid")
    publisher = require_exact_object(app["publisher"], {"id", "display_name"}, "publisher")
    if not isinstance(publisher["id"], str) or not APP_ID_RE.fullmatch(publisher["id"]):
        raise PipelineError("schema-invalid", "publisher ID is invalid")
    if not isinstance(publisher["display_name"], str) or not publisher["display_name"]:
        raise PipelineError("schema-invalid", "publisher display name is invalid")

    artifacts = require_exact_object(
        manifest["artifacts"],
        {"canonical_component", "assets", "browser_derivations", "provenance", "sbom"},
        "artifacts",
    )
    if artifacts["assets"] != [] or artifacts["browser_derivations"] != []:
        raise PipelineError("schema-invalid", "product Builder v1 supports no assets/browser derivations")
    for name, expected_path, media_type in (
        ("canonical_component", "component.wasm", "application/wasm"),
        ("provenance", "provenance.json", "application/json"),
        ("sbom", "sbom.cdx.json", "application/vnd.cyclonedx+json"),
    ):
        descriptor = require_exact_object(
            artifacts[name], {"path", "media_type", "sha256", "size_bytes"}, f"artifacts.{name}"
        )
        if descriptor["path"] != expected_path or descriptor["media_type"] != media_type:
            raise PipelineError("schema-invalid", f"artifacts.{name} path/media type is invalid")
        require_sha256(descriptor["sha256"], f"artifacts.{name}.sha256")
        _require_integer(descriptor["size_bytes"], f"artifacts.{name}.size_bytes", 1, 64 * 1024 * 1024)

    runtime = require_exact_object(
        manifest["runtime"], {"contract", "wasi", "world", "required_imports", "profiles", "platforms"}, "runtime"
    )
    if runtime["contract"] != "vibapp:experimental-v0@0.0.1" or runtime["wasi"] != "0.2":
        raise PipelineError("unsupported-version", "manifest runtime contract/WASI is unsupported")
    if runtime["world"] not in WORLD_IMPORTS or runtime["required_imports"] != WORLD_IMPORTS[runtime["world"]]:
        raise PipelineError("incompatible-contract", "manifest world/import set is not exact")
    if app["kind"] != WORLD_KIND[runtime["world"]]:
        raise PipelineError("incompatible-contract", "manifest app kind/world disagree")
    profiles = runtime["profiles"]
    if not isinstance(profiles, list) or not profiles or len(profiles) > 2:
        raise PipelineError("schema-invalid", "manifest profiles are invalid")
    profile_names: list[str] = []
    for index, row in enumerate(profiles):
        row = require_exact_object(
            row, {"profile", "mode", "background", "artifact_role", "degradation"}, f"profile[{index}]"
        )
        if row["profile"] not in {"desktop", "headless"} or row["profile"] in profile_names:
            raise PipelineError("schema-invalid", "manifest profile is invalid/duplicate")
        if row["artifact_role"] != "canonical-component":
            raise PipelineError("incompatible-contract", "non-browser profile must use canonical component")
        if row["mode"] not in {"full", "degraded"} or row["background"] not in {
            "daemon",
            "process",
            "not-applicable",
        }:
            raise PipelineError("schema-invalid", "manifest profile mode/background is invalid")
        if not isinstance(row["degradation"], str) or len(row["degradation"].encode("utf-8")) > 500:
            raise PipelineError("schema-invalid", "manifest profile degradation is invalid")
        profile_names.append(row["profile"])
    platforms = runtime["platforms"]
    if platforms != [{"os": "macos", "arch": "aarch64", "profiles": profile_names}]:
        raise PipelineError("schema-invalid", "Builder v1 local platform claim is not exact")

    entrypoints = manifest["entrypoints"]
    if not isinstance(entrypoints, list) or not 1 <= len(entrypoints) <= 16:
        raise PipelineError("schema-invalid", "manifest entrypoints are invalid")
    entrypoint_ids: set[str] = set()
    kinds: set[str] = set()
    for index, row in enumerate(entrypoints):
        if not isinstance(row, dict) or row.get("kind") not in {"launcher-ui", "service", "settings"}:
            raise PipelineError("schema-invalid", f"entrypoint[{index}] kind is invalid")
        kind = row["kind"]
        expected = {"id", "kind", "label", "profiles"}
        expected |= (
            {"routes", "restoration"}
            if kind == "launcher-ui"
            else {"triggers", "health_check_interval_seconds"}
            if kind == "service"
            else {"schema_export"}
        )
        row = require_exact_object(row, expected, f"entrypoint[{index}]")
        if not isinstance(row["id"], str) or row["id"] in entrypoint_ids:
            raise PipelineError("schema-invalid", "entrypoint ID is invalid/duplicate")
        if row["profiles"] != profile_names:
            raise PipelineError("schema-invalid", "entrypoint profile coverage differs from runtime")
        if kind == "launcher-ui":
            routes = require_exact_object(row["routes"], {"initial", "allowed"}, "launcher routes")
            if not isinstance(routes["initial"], str) or routes["allowed"] != [routes["initial"]]:
                raise PipelineError("schema-invalid", "launcher routes are invalid")
            if row["restoration"] != "route-only":
                raise PipelineError("schema-invalid", "launcher restoration is invalid")
        elif kind == "service":
            triggers = row["triggers"]
            if (
                not isinstance(triggers, list)
                or not triggers
                or len(triggers) > len(SERVICE_TRIGGER_ORDER)
                or triggers
                != [trigger for trigger in SERVICE_TRIGGER_ORDER if trigger in triggers]
            ):
                raise PipelineError(
                    "schema-invalid",
                    "service triggers are missing, unknown, duplicated, or non-canonical",
                )
            _require_integer(row["health_check_interval_seconds"], "health interval", 10, 86400)
        elif row["schema_export"] != "get-settings-schema":
            raise PipelineError("schema-invalid", "settings export is invalid")
        entrypoint_ids.add(row["id"])
        kinds.add(kind)
    if app["kind"] == "ui" and ("launcher-ui" not in kinds or "service" in kinds):
        raise PipelineError("incompatible-contract", "UI entrypoints disagree with app kind")
    if app["kind"] == "service" and ("service" not in kinds or "launcher-ui" in kinds):
        raise PipelineError("incompatible-contract", "service entrypoints disagree with app kind")
    if app["kind"] == "hybrid" and not {"launcher-ui", "service"} <= kinds:
        raise PipelineError("incompatible-contract", "hybrid entrypoints are incomplete")

    capabilities = manifest["capabilities"]
    if not isinstance(capabilities, list) or len(capabilities) != len(runtime["required_imports"]):
        raise PipelineError("schema-invalid", "manifest capability count is invalid")
    seen_capabilities: set[str] = set()
    for index, row in enumerate(capabilities):
        row = require_exact_object(
            row, {"interface", "necessity", "grant", "reason", "scope", "profiles"}, f"capability[{index}]"
        )
        interface = row["interface"]
        if interface not in runtime["required_imports"] or interface in seen_capabilities:
            raise PipelineError("incompatible-contract", "capability/import reconciliation failed")
        grant = CAPABILITY_GRANT_POLICY.get(interface)
        host_availability = LOCAL_HOST_CAPABILITY_AVAILABILITY.get(interface)
        if grant is None or host_availability is None:
            raise PipelineError("unknown-interface", "capability lacks a local host policy")
        if row["necessity"] not in {"required", "degradable"} or row["grant"] != grant:
            raise PipelineError(
                "incompatible-contract",
                "capability necessity/grant is invalid for the app contract",
            )
        if not isinstance(row["scope"], dict) or not isinstance(row["reason"], str) or not row["reason"]:
            raise PipelineError("schema-invalid", "capability scope/reason is invalid")
        profile_rows = row["profiles"]
        if not isinstance(profile_rows, list) or [item.get("profile") for item in profile_rows if isinstance(item, dict)] != profile_names:
            raise PipelineError("incompatible-contract", "capability profile coverage is incomplete")
        for profile_row in profile_rows:
            require_exact_object(profile_row, {"profile", "availability", "behavior"}, "capability profile")
            if profile_row["availability"] not in {"native", "brokered", "mock", "denied", "unavailable"}:
                raise PipelineError("schema-invalid", "capability availability is invalid")
            expected_availability = (
                host_availability if row["necessity"] == "required" else "denied"
            )
            if profile_row["availability"] != expected_availability:
                raise PipelineError(
                    "incompatible-contract",
                    "capability availability contradicts app necessity or local host truth",
                )
            if not isinstance(profile_row["behavior"], str) or not profile_row["behavior"]:
                raise PipelineError("schema-invalid", "capability behavior is invalid")
        seen_capabilities.add(interface)
    if seen_capabilities != set(runtime["required_imports"]):
        raise PipelineError("incompatible-contract", "capabilities do not equal required imports")

    resources = require_exact_object(
        manifest["resources"],
        {
            "linear_memory_bytes",
            "event_wall_time_ms",
            "health_migration_wall_time_ms",
            "output_bytes",
            "stored_data_bytes",
            "durable_schedules",
            "log_bytes_per_day",
        },
        "resources",
    )
    for key, minimum, maximum in (
        ("linear_memory_bytes", 1_048_576, 67_108_864),
        ("event_wall_time_ms", 1, 250),
        ("health_migration_wall_time_ms", 1, 2000),
        ("output_bytes", 1, 262_144),
        ("stored_data_bytes", 0, 10_485_760),
        ("durable_schedules", 0, 128),
        ("log_bytes_per_day", 0, 1_048_576),
    ):
        _require_integer(resources[key], f"resources.{key}", minimum, maximum)
    state = require_exact_object(manifest["state"], {"schema", "migratable_from_min", "migratable_from_max"}, "state")
    values = [_require_integer(state[key], f"state.{key}", 1, 4_294_967_295) for key in state]
    if not values[1] <= values[0] <= values[2]:
        raise PipelineError("schema-invalid", "state migration range is invalid")
    lifecycle = require_exact_object(manifest["lifecycle"], {"disable", "uninstall"}, "lifecycle")
    if lifecycle["disable"] != {
        "retains_package": True,
        "retains_state": True,
        "stops_services": True,
        "cancels_schedules": True,
    }:
        raise PipelineError("schema-invalid", "disable lifecycle is invalid")
    uninstall = require_exact_object(
        lifecycle["uninstall"], {"allowed_data_dispositions", "default_data_disposition"}, "uninstall"
    )
    allowed = uninstall["allowed_data_dispositions"]
    if not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed) or any(
        value not in {"delete", "retain", "export-then-delete"} for value in allowed
    ) or uninstall["default_data_disposition"] not in allowed:
        raise PipelineError("schema-invalid", "uninstall data disposition is invalid")
    source = require_exact_object(
        manifest["source"], {"language", "revision", "cargo_lock_sha256", "builder_image_digest"}, "source"
    )
    if source["language"] != "rust" or not isinstance(source["revision"], str) or not source["revision"]:
        raise PipelineError("schema-invalid", "manifest source identity is invalid")
    require_sha256(source["cargo_lock_sha256"], "source.cargo_lock_sha256")
    if not isinstance(source["builder_image_digest"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", source["builder_image_digest"]
    ):
        raise PipelineError("schema-invalid", "builder identity digest is invalid")
    license_row = require_exact_object(manifest["license"], {"spdx_expression"}, "license")
    if license_row["spdx_expression"] != "Apache-2.0":
        raise PipelineError("schema-invalid", "Builder v1 license is not accepted")
    privacy = require_exact_object(manifest["privacy"], {"stores_personal_data", "network_access"}, "privacy")
    if not isinstance(privacy["stores_personal_data"], bool) or privacy["network_access"] != "none":
        raise PipelineError("schema-invalid", "Builder v1 privacy policy is invalid")
    if manifest["verification"] != {"status": "unverified", "revocation": "not-revoked"}:
        raise PipelineError("authority-invalid", "Builder manifest must remain unverified")


def _validate_consent_bound_entrypoints(
    value: Any, context: str
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise PipelineError("schema-invalid", f"{context} is not a bounded entrypoint list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        row = require_exact_object(
            item,
            {"id", "kind", "label", "initial_route", "triggers"},
            f"{context}[{index}]",
        )
        entrypoint_id = row["id"]
        kind = row["kind"]
        label = row["label"]
        route = row["initial_route"]
        triggers = row["triggers"]
        if (
            not isinstance(entrypoint_id, str)
            or not ENTRYPOINT_ID_RE.fullmatch(entrypoint_id)
            or entrypoint_id in seen
        ):
            raise PipelineError("schema-invalid", f"{context} ID is invalid or duplicated")
        if kind not in {"launcher-ui", "service", "settings"}:
            raise PipelineError("schema-invalid", f"{context} kind is invalid")
        if (
            not isinstance(label, str)
            or not label
            or len(label.encode("utf-8")) > 80
        ):
            raise PipelineError("schema-invalid", f"{context} label is invalid")
        if kind == "launcher-ui":
            if not isinstance(route, str):
                raise PipelineError("schema-invalid", f"{context} launcher route is invalid")
        elif route is not None:
            raise PipelineError("schema-invalid", f"{context} non-launcher route must be null")
        if kind == "service":
            if (
                not isinstance(triggers, list)
                or not triggers
                or triggers
                != [trigger for trigger in SERVICE_TRIGGER_ORDER if trigger in triggers]
            ):
                raise PipelineError("schema-invalid", f"{context} service triggers are invalid")
        elif triggers != []:
            raise PipelineError("schema-invalid", f"{context} non-service triggers must be empty")
        seen.add(entrypoint_id)
        normalized.append(
            {
                "id": entrypoint_id,
                "kind": kind,
                "label": label,
                "initial_route": route,
                "triggers": list(triggers),
            }
        )
    return normalized


def _manifest_consent_bound_entrypoints(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in manifest["entrypoints"]:
        kind = row["kind"]
        projected.append(
            {
                "id": row["id"],
                "kind": kind,
                "label": row["label"],
                "initial_route": (
                    row["routes"]["initial"] if kind == "launcher-ui" else None
                ),
                "triggers": list(row["triggers"]) if kind == "service" else [],
            }
        )
    return _validate_consent_bound_entrypoints(projected, "manifest entrypoint binding")


def _validate_package_tree(package_dir: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    metadata = package_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PipelineError("integrity-failure", "quarantine package is not a real directory")
    actual = {entry.name for entry in os.scandir(package_dir)}
    if actual != EXPECTED_PACKAGE_FILES:
        raise PipelineError("integrity-failure", f"quarantine package boundary mismatch: {sorted(actual)}")
    inventory = receipt["package_files"]
    if not isinstance(inventory, list) or len(inventory) != 4:
        raise PipelineError("schema-invalid", "quarantine package inventory is invalid")
    indexed: dict[str, dict[str, Any]] = {}
    total = 0
    for index, item in enumerate(inventory):
        row = require_exact_object(item, {"path", "sha256", "size_bytes"}, f"package_files[{index}]")
        name = normalized_relative_path(row["path"], f"package_files[{index}].path")
        if "/" in name or name not in EXPECTED_PACKAGE_FILES or name in indexed:
            raise PipelineError("integrity-failure", "quarantine inventory path is invalid/duplicate")
        require_sha256(row["sha256"], f"package_files[{index}].sha256")
        _require_integer(row["size_bytes"], f"package_files[{index}].size_bytes", 1, MAX_PACKAGE_BYTES)
        path = package_dir / name
        metadata = lstat_regular(path, MAX_PACKAGE_BYTES, name)
        if metadata.st_size != row["size_bytes"] or sha256_file(path, MAX_PACKAGE_BYTES, name) != row["sha256"]:
            raise PipelineError("integrity-failure", f"quarantine bytes changed: {name}")
        total += metadata.st_size
        indexed[name] = row
    if set(indexed) != EXPECTED_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
        raise PipelineError("resource-limit", "quarantine package inventory/size is invalid")
    manifest = load_json(package_dir / "manifest.json", MAX_JSON_BYTES, "manifest")
    _validate_manifest_schema(manifest)
    manifest_required = {
        capability["interface"]
        for capability in manifest["capabilities"]
        if capability["necessity"] == "required"
    }
    if manifest_required != set(receipt["required_capabilities"]):
        raise PipelineError(
            "integrity-failure",
            "manifest capability necessity differs from the consent-bound Builder input",
        )
    manifest_entrypoints = _manifest_consent_bound_entrypoints(manifest)
    if manifest_entrypoints != receipt["package_entrypoints"]:
        raise PipelineError(
            "integrity-failure",
            "manifest entrypoints or triggers differ from the consent-bound Builder input",
        )
    artifacts = manifest["artifacts"]
    descriptors = {
        "component.wasm": artifacts["canonical_component"],
        "provenance.json": artifacts["provenance"],
        "sbom.cdx.json": artifacts["sbom"],
    }
    for name, descriptor in descriptors.items():
        if descriptor["size_bytes"] != indexed[name]["size_bytes"] or descriptor["sha256"] != indexed[name]["sha256"]:
            raise PipelineError("integrity-failure", f"manifest descriptor differs from bytes: {name}")
    return manifest


def _validate_receipt(receipt_path: Path, output_root: Path) -> tuple[dict[str, Any], Path, Path]:
    receipt_path = receipt_path.resolve(strict=True)
    output_root = output_root.resolve(strict=True)
    jobs_root = (output_root / "jobs").resolve(strict=True)
    if jobs_root not in receipt_path.parents:
        raise PipelineError("integrity-failure", "quarantine receipt is outside output jobs")
    if receipt_path.name != "quarantine-receipt.json" or receipt_path.parent.name != "quarantine":
        raise PipelineError("integrity-failure", "quarantine receipt path is not canonical")
    job_root = receipt_path.parent.parent
    if job_root.parent != jobs_root:
        raise PipelineError("integrity-failure", "quarantine job nesting is invalid")
    receipt = load_json(receipt_path, MAX_JSON_BYTES, "quarantine receipt")
    receipt_fields = {
        "schema_version",
        "document_type",
        "state",
        "scope",
        "job_id",
        "need_spec_digest_sha256",
        "source_tree_sha256",
        "required_capabilities",
        "package_entrypoints",
        "source_directory",
        "package_directory",
        "package_digest_sha256",
        "package_files",
        "component_sha256",
        "manifest_sha256",
        "build_log_sha256",
        "builder",
        "limits",
        "terminal_outcome",
        "authority",
        "created_at_utc",
    }
    # Host presentation remains an optional advisory in the v2 receipt; when it
    # is absent, the verifier derives the same bounded sidecar independently.
    optional_receipt_fields = {"presentation"}
    if not receipt_fields <= set(receipt) or set(receipt) - receipt_fields - optional_receipt_fields:
        raise PipelineError("schema-invalid", "quarantine receipt fields mismatch")
    if "presentation" in receipt:
        receipt["presentation"] = validate_host_presentation(
            receipt["presentation"], "quarantine receipt.presentation"
        )
    required_capabilities = receipt["required_capabilities"]
    if (
        not isinstance(required_capabilities, list)
        or len(required_capabilities) > len(APP_IMPORTS)
        or any(
            not isinstance(interface, str) or interface not in APP_IMPORTS
            for interface in required_capabilities
        )
        or len(set(required_capabilities)) != len(required_capabilities)
    ):
        raise PipelineError(
            "schema-invalid", "quarantine required capabilities are invalid"
        )
    receipt["package_entrypoints"] = _validate_consent_bound_entrypoints(
        receipt["package_entrypoints"], "quarantine receipt.package_entrypoints"
    )
    if (
        receipt["schema_version"] != "vibapp.builder-quarantine.experimental-v2"
        or receipt["document_type"] != "builder-quarantine-receipt"
        or receipt["state"] != "quarantined-awaiting-verifier"
        or receipt["scope"] != "local-product-prototype"
        or receipt["source_directory"] != "source"
        or receipt["package_directory"] != "package"
        or receipt["terminal_outcome"] != "build-output-untrusted"
    ):
        raise PipelineError("schema-invalid", "quarantine receipt constants are unsupported")
    if receipt["job_id"] != job_root.name:
        raise PipelineError("integrity-failure", "receipt job ID differs from directory")
    for field in (
        "need_spec_digest_sha256",
        "source_tree_sha256",
        "package_digest_sha256",
        "component_sha256",
        "manifest_sha256",
        "build_log_sha256",
    ):
        require_sha256(receipt[field], field)
    if receipt["authority"] != {
        "builder": "quarantine-only",
        "verify": "independent-verifier",
        "install": "none",
        "publish": "none",
    }:
        raise PipelineError("authority-invalid", "quarantine authority is invalid")
    builder = require_exact_object(
        receipt["builder"],
        {"version", "runner_identity_sha256", "execution_mode", "source_execution_observed", "production_isolation"},
        "builder",
    )
    require_sha256(builder["runner_identity_sha256"], "builder.runner_identity_sha256")
    if not isinstance(builder["source_execution_observed"], bool) or not isinstance(builder["production_isolation"], bool):
        raise PipelineError("schema-invalid", "builder truth flags are invalid")
    limits = receipt["limits"]
    expected_limits = {
        "wall_seconds",
        "cpu_seconds",
        "memory_bytes",
        "pids",
        "disk_bytes",
        "stdout_bytes",
        "stderr_bytes",
        "open_files",
        "concurrent_jobs",
    }
    require_exact_object(limits, expected_limits, "limits")
    for key, value in limits.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PipelineError("schema-invalid", f"limit {key} is not finite positive")
    if limits["concurrent_jobs"] != 1:
        raise PipelineError("resource-limit", "Builder concurrency must be one")
    build_log = job_root / "build.log.json"
    if sha256_file(build_log, MAX_JSON_BYTES, "build log") != receipt["build_log_sha256"]:
        raise PipelineError("integrity-failure", "build log digest changed")
    source_root = job_root / "source"
    records: list[dict[str, Any]] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            if path.is_symlink():
                raise PipelineError("integrity-failure", "copied source contains a symlink directory")
            continue
        relative = path.relative_to(source_root).as_posix()
        metadata = lstat_regular(path, 16 * 1024 * 1024, relative)
        records.append(
            {
                "path": relative,
                "sha256": sha256_file(path, 16 * 1024 * 1024, relative),
                "size_bytes": metadata.st_size,
                "role": "authoritative-contract-copy" if relative == "wit/contract.wit" else "generated-source",
            }
        )
    if source_tree_digest(records) != receipt["source_tree_sha256"]:
        raise PipelineError("integrity-failure", "copied source tree digest changed")
    package_dir = receipt_path.parent / "package"
    return receipt, job_root, package_dir


def _validated_wasm_tools(wasm_tools: Path) -> tuple[Path, str]:
    try:
        wasm_tools = wasm_tools.resolve(strict=True)
    except OSError as error:
        raise PipelineError(
            "verification-unavailable",
            "Verifier wasm-tools executable is unavailable",
        ) from error
    metadata = lstat_regular(wasm_tools, 64 * 1024 * 1024, "wasm-tools")
    if not metadata.st_mode & 0o111:
        raise PipelineError("verification-unavailable", "wasm-tools is not executable")
    digest = sha256_file(wasm_tools, 64 * 1024 * 1024, "wasm-tools")
    if digest != WASM_TOOLS_SHA256:
        raise PipelineError("integrity-failure", "wasm-tools differs from the accepted local tool layer")
    return wasm_tools, digest


def verifier_preflight(
    wasm_tools: Path = DEFAULT_WASM_TOOLS,
    component_inspector: Path = DEFAULT_COMPONENT_INSPECTOR,
) -> dict[str, Any]:
    """Validate the exact Verifier executable without inspecting or promoting a package."""
    executable, digest = _validated_wasm_tools(wasm_tools)
    inspector = descriptor_inspector_preflight(component_inspector)
    return {
        "status": "ready",
        "verify_authority": "independent-verifier",
        "network": "none",
        "source_executed": False,
        "wasm_tools": str(executable),
        "wasm_tools_sha256": digest,
        "expected_wasm_tools_sha256": WASM_TOOLS_SHA256,
        "component_inspector": inspector["component_inspector"],
        "component_inspector_sha256": inspector["component_inspector_sha256"],
        "expected_component_inspector_sha256": COMPONENT_INSPECTOR_SHA256,
        "descriptor_live_host_effects": inspector["live_host_effects"],
        "verifier_version": VERIFIER_VERSION,
    }


def _run_wasm_checks(component: Path, manifest: dict[str, Any], wasm_tools: Path) -> list[dict[str, str]]:
    wasm_tools, _ = _validated_wasm_tools(wasm_tools)
    limits = ProcessLimits(
        wall_seconds=60,
        cpu_seconds=45,
        memory_bytes=1024 * 1024 * 1024,
        pids=16,
        disk_bytes=MAX_PACKAGE_BYTES,
        stdout_bytes=2 * 1024 * 1024,
        stderr_bytes=512 * 1024,
        open_files=128,
    )
    environment = {"PATH": "/usr/bin:/bin", "TZ": "UTC", "LANG": "C", "LC_ALL": "C"}
    run_bounded(
        [str(wasm_tools), "validate", str(component)],
        cwd=component.parent,
        environment=environment,
        limits=limits,
        disk_root=component.parent,
    )
    wit_result = run_bounded(
        [str(wasm_tools), "component", "wit", str(component)],
        cwd=component.parent,
        environment=environment,
        limits=limits,
        disk_root=component.parent,
    )
    metadata_result = run_bounded(
        [str(wasm_tools), "metadata", "show", str(component)],
        cwd=component.parent,
        environment=environment,
        limits=limits,
        disk_root=component.parent,
    )
    try:
        wit = wit_result.stdout.decode("utf-8")
        metadata_text = metadata_result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PipelineError("malformed-output", "wasm-tools output is not UTF-8") from error
    imports = set(re.findall(r"^\s*import\s+([^;]+);\s*$", wit, re.MULTILINE))
    required = set(manifest["runtime"]["required_imports"])
    missing = required - imports
    unexpected = imports - required - TYPE_ONLY_IMPORTS
    if missing or unexpected:
        raise PipelineError(
            "incompatible-contract",
            f"component import reconciliation failed missing={sorted(missing)} extra={sorted(unexpected)}",
        )
    if "vibapp:experimental-v0/guest@0.0.1" not in wit:
        raise PipelineError("incompatible-contract", "component lacks the guest export")
    prohibited = [
        "wasi:filesystem",
        "wasi:sockets",
        "wasi:cli/environment",
        "wasi:cli/run",
        "wasi:http",
    ]
    leaked = [name for name in prohibited if name in wit]
    if leaked:
        raise PipelineError("permission-denied", f"component exposes ambient WASI imports: {leaked}")
    if "wit-bindgen-rust [0.60.0]" not in metadata_text:
        raise PipelineError("incompatible-contract", "component metadata lacks wit-bindgen-rust 0.60.0")
    return [
        {"id": "wasm-tools-validate", "outcome": "pass", "tool": "wasm-tools 1.256.0", "detail": "exact component bytes validate"},
        {"id": "component-import-reconciliation", "outcome": "pass", "tool": "wasm-tools component wit", "detail": "required imports and guest export are exact; ambient WASI denied"},
        {"id": "component-tool-metadata", "outcome": "pass", "tool": "wasm-tools metadata show", "detail": "wit-bindgen-rust 0.60.0 metadata observed"},
    ]


def _verify_and_promote_impl(
    receipt_path: Path,
    output_root: Path,
    *,
    wasm_tools: Path = DEFAULT_WASM_TOOLS,
    component_inspector: Path = DEFAULT_COMPONENT_INSPECTOR,
    recheck_existing: bool = False,
) -> Path:
    output_root = ensure_private_directory(output_root)
    candidates = ensure_private_directory(output_root / "candidates")
    lock_descriptor = os.open(output_root / ".verifier.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PipelineError("busy", "another verifier job is active") from error
        receipt, _, package_dir = _validate_receipt(receipt_path, output_root)
        manifest = _validate_package_tree(package_dir, receipt)
        derived_presentation = derive_host_presentation(
            manifest["app"]["kind"],
            manifest["app"]["display_name"],
            manifest["app"]["description"],
        )
        if "presentation" in receipt and receipt["presentation"] != derived_presentation:
            raise PipelineError(
                "integrity-failure",
                "Builder presentation differs from the verifier-derived host policy",
            )
        digest = package_digest(manifest)
        if digest != receipt["package_digest_sha256"]:
            raise PipelineError("integrity-failure", "package digest differs from quarantine receipt")
        if sha256_file(package_dir / "component.wasm", MAX_COMPONENT_BYTES, "component") != receipt["component_sha256"]:
            raise PipelineError("integrity-failure", "component digest differs from quarantine receipt")
        if sha256_file(package_dir / "manifest.json", MAX_JSON_BYTES, "manifest") != receipt["manifest_sha256"]:
            raise PipelineError("integrity-failure", "manifest digest differs from quarantine receipt")
        checks = [
            {"id": "bounded-package-tree", "outcome": "pass", "tool": VERIFIER_VERSION, "detail": "four regular files; no links, extras, or oversized content"},
            {"id": "manifest-schema-and-semantics", "outcome": "pass", "tool": VERIFIER_VERSION, "detail": "strict product-v1 subset and frozen runtime relations validated"},
            {"id": "artifact-and-package-digests", "outcome": "pass", "tool": VERIFIER_VERSION, "detail": "descriptors, exact bytes, and package preimage rehashed"},
            {"id": "builder-authority-separation", "outcome": "pass", "tool": VERIFIER_VERSION, "detail": "builder remained quarantine-only; its success prose was not an oracle"},
            {"id": "host-presentation-bounds", "outcome": "pass", "tool": VERIFIER_VERSION, "detail": "window hints were independently derived and contain no native-window authority"},
        ]
        checks.extend(_run_wasm_checks(package_dir / "component.wasm", manifest, wasm_tools))
        checks.append(
            run_guest_descriptor_reconciliation(
                package_dir / "component.wasm", manifest, component_inspector
            )
        )
        receipt_digest = sha256_file(receipt_path, MAX_JSON_BYTES, "quarantine receipt")
        final = candidates / digest
        if (final.exists() or final.is_symlink()) and not recheck_existing:
            raise PipelineError("conflict", "candidate digest already exists")
        staging = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=candidates))
        staging.chmod(0o700)
        try:
            promoted_package = staging / "package"
            promoted_package.mkdir(mode=0o700)
            for name in sorted(EXPECTED_PACKAGE_FILES):
                exclusive_copy(package_dir / name, promoted_package / name, MAX_PACKAGE_BYTES)
            promoted_manifest = load_json(promoted_package / "manifest.json", MAX_JSON_BYTES, "promoted manifest")
            if package_digest(promoted_manifest) != digest:
                raise PipelineError("integrity-failure", "promoted package bytes changed during copy")
            for item in receipt["package_files"]:
                promoted = promoted_package / item["path"]
                if sha256_file(promoted, MAX_PACKAGE_BYTES, item["path"]) != item["sha256"]:
                    raise PipelineError("integrity-failure", "promoted package file changed during copy")
            record = {
                "schema_version": "vibapp.builder-candidate.experimental-v1",
                "document_type": "verifier-promoted-candidate",
                "state": "candidate-ready",
                "job_id": receipt["job_id"],
                "source_tree_sha256": receipt["source_tree_sha256"],
                "package_digest_sha256": digest,
                "package_directory": "package",
                "component": {
                    "path": "component.wasm",
                    "sha256": receipt["component_sha256"],
                    "size_bytes": (promoted_package / "component.wasm").stat().st_size,
                },
                "manifest": {
                    "path": "manifest.json",
                    "sha256": receipt["manifest_sha256"],
                    "size_bytes": (promoted_package / "manifest.json").stat().st_size,
                },
                "quarantine_receipt_sha256": receipt_digest,
                "presentation": derived_presentation,
                "verification": {
                    "authority": "independent-verifier",
                    "verifier_version": VERIFIER_VERSION,
                    "verified_at_utc": now_utc(),
                    "checks": checks,
                },
                "authority": {"install": "daemon", "publish": "none"},
            }
            if final.exists() or final.is_symlink():
                # Retry is only an opt-in after ALL quarantine, Wasm and guest
                # checks ran again. Preserve the original immutable timestamp.
                if (final.is_symlink() or not final.is_dir() or
                        set(p.name for p in final.iterdir()) != {"candidate.json", "package"} or
                        (final / "package").is_symlink()):
                    raise PipelineError("integrity-failure", "existing candidate tree differs")
                _validate_package_tree(final / "package", receipt)
                previous = load_json(final / "candidate.json", MAX_JSON_BYTES, "existing candidate")
                timestamp = previous.get("verification", {}).get("verified_at_utc")
                if not isinstance(timestamp, str) or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", timestamp):
                    raise PipelineError("integrity-failure", "existing candidate timestamp invalid")
                record["verification"]["verified_at_utc"] = timestamp
                if previous != record:
                    raise PipelineError("integrity-failure", "existing candidate receipt differs")
                for item in receipt["package_files"]:
                    if sha256_file(final / "package" / item["path"], MAX_PACKAGE_BYTES,
                                   item["path"]) != item["sha256"]:
                        raise PipelineError("integrity-failure", "existing candidate bytes differ")
                remove_tree(staging)
                return final / "candidate.json"
            atomic_json(staging / "candidate.json", record, mode=0o400)
            os.rename(staging, final)
            return final / "candidate.json"
        except Exception:
            remove_tree(staging)
            raise


def _record_rejection(output_root: Path, receipt_path: Path, error: Exception) -> Path:
    """Persist a bounded fail-closed decision without trusting receipt contents."""
    code = error.code if isinstance(error, PipelineError) else "internal"
    receipt_reference_sha256 = sha256_bytes(os.fsencode(str(receipt_path.absolute())))
    receipt_digest: str | None = None
    try:
        receipt_digest = sha256_file(receipt_path, MAX_JSON_BYTES, "rejected quarantine receipt")
    except (PipelineError, OSError, ValueError):
        pass
    decisions = ensure_private_directory(output_root / "verifier-decisions")
    decision_directory = ensure_private_directory(decisions / receipt_reference_sha256)
    decision = {
        "schema_version": "vibapp.verifier-decision.experimental-v1",
        "document_type": "verifier-decision",
        "state": "rejected",
        "receipt_reference_sha256": receipt_reference_sha256,
        "quarantine_receipt_sha256": receipt_digest,
        "error": {"code": code, "detail": str(error)[:4096]},
        "candidate_created": False,
        "decided_at_utc": now_utc(),
        "authority": {
            "verify": "independent-verifier",
            "install": "none",
            "publish": "none",
        },
    }
    decision_path = decision_directory / "decision.json"
    atomic_json(decision_path, decision, mode=0o400)
    return decision_path


def verify_and_promote(
    receipt_path: Path,
    output_root: Path,
    *,
    wasm_tools: Path = DEFAULT_WASM_TOOLS,
    component_inspector: Path = DEFAULT_COMPONENT_INSPECTOR,
    recheck_existing: bool = False,
) -> Path:
    """Verify exact quarantine bytes; record rejection or atomically promote."""
    output_root = ensure_private_directory(output_root)
    try:
        return _verify_and_promote_impl(
            receipt_path,
            output_root,
            wasm_tools=wasm_tools,
            component_inspector=component_inspector,
            recheck_existing=recheck_existing,
        )
    except (PipelineError, OSError, ValueError) as error:
        try:
            _record_rejection(output_root, receipt_path, error)
        except (PipelineError, OSError, ValueError) as decision_error:
            raise PipelineError(
                "decision-write-failed",
                f"verification rejected ({getattr(error, 'code', 'internal')}) and durable decision failed: {decision_error}",
            ) from error
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent VibApp candidate verifier")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("receipt", type=Path)
    verify_parser.add_argument("--output-root", type=Path, required=True)
    verify_parser.add_argument("--wasm-tools", type=Path, default=DEFAULT_WASM_TOOLS)
    verify_parser.add_argument("--recheck-existing", action="store_true")
    verify_parser.add_argument(
        "--component-inspector", type=Path, default=DEFAULT_COMPONENT_INSPECTOR
    )
    args = parser.parse_args()
    try:
        candidate = verify_and_promote(
            args.receipt,
            args.output_root,
            wasm_tools=args.wasm_tools,
            component_inspector=args.component_inspector,
            recheck_existing=args.recheck_existing,
        )
    except (PipelineError, OSError, ValueError) as error:
        code = error.code if isinstance(error, PipelineError) else "internal"
        print(f"VERIFY_REJECTED code={code} detail={str(error)[:4096]}", file=sys.stderr)
        return 1
    print(json.dumps({"state": "candidate-ready", "candidate": str(candidate)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
