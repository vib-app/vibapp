#!/usr/bin/env python3
"""Bounded Guest descriptor inspection and RFC 0001 section 4 reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import unicodedata

from common import (
    APP_ID_RE,
    SEMVER_RE,
    PipelineError,
    ProcessLimits,
    lstat_regular,
    require_exact_object,
    run_bounded,
    sha256_file,
    strict_object,
)


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
_BUNDLED_COMPONENT_INSPECTOR = (
    BASE.parent / "runtime-daemon/service-runtime/vibapp-service-runtime"
)
_REPOSITORY_COMPONENT_INSPECTOR = (
    REPO
    / "artifacts/runtime-daemon/target-service-1_98/release/vibapp-service-runtime"
)
DEFAULT_COMPONENT_INSPECTOR = (
    _BUNDLED_COMPONENT_INSPECTOR
    if _BUNDLED_COMPONENT_INSPECTOR.exists()
    else _REPOSITORY_COMPONENT_INSPECTOR
)

# This digest is refreshed only after the fixed inspector source has been rebuilt
# with the repository's accepted absolute offline toolchain. An arbitrary binary
# supplied at the CLI cannot replace these trusted bytes.
COMPONENT_INSPECTOR_SHA256 = (
    "229a1f7cf68d8565500c53b311ae4cc48497e010396a3cd3ee84d740683ed23f"
)
INSPECTOR_SCHEMA = "vibapp.component-inspector.protocol.experimental-v1"
MAX_INSPECTOR_BYTES = 16 * 1024 * 1024
MAX_INSPECTOR_OUTPUT_BYTES = 256 * 1024
INSPECTION_MEMORY_BYTES = 64 * 1024 * 1024
INSPECTION_FUEL = 20_000_000
INSPECTION_DEADLINE_MS = 2_000


def validated_component_inspector(
    inspector: Path = DEFAULT_COMPONENT_INSPECTOR,
) -> tuple[Path, str]:
    """Resolve the one accepted executable and reject path or byte substitution."""
    try:
        resolved = inspector.resolve(strict=True)
    except OSError as error:
        raise PipelineError(
            "verification-unavailable",
            "Verifier Guest descriptor inspector is unavailable",
        ) from error
    metadata = lstat_regular(resolved, MAX_INSPECTOR_BYTES, "Guest descriptor inspector")
    if not metadata.st_mode & 0o111:
        raise PipelineError(
            "verification-unavailable", "Guest descriptor inspector is not executable"
        )
    digest = sha256_file(
        resolved, MAX_INSPECTOR_BYTES, "Guest descriptor inspector"
    )
    if digest != COMPONENT_INSPECTOR_SHA256:
        raise PipelineError(
            "integrity-failure",
            "Guest descriptor inspector differs from the accepted fixed executable",
        )
    return resolved, digest


def descriptor_inspector_preflight(
    inspector: Path = DEFAULT_COMPONENT_INSPECTOR,
) -> dict[str, Any]:
    executable, digest = validated_component_inspector(inspector)
    return {
        "status": "ready",
        "network": "none",
        "live_host_effects": False,
        "component_inspector": str(executable),
        "component_inspector_sha256": digest,
        "expected_component_inspector_sha256": COMPONENT_INSPECTOR_SHA256,
    }


def _bounded_text(value: Any, context: str, maximum_characters: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_characters
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise PipelineError(
            "malformed-output", f"Guest descriptor {context} is outside its bound"
        )
    return value


def _validated_descriptor(value: Any) -> dict[str, Any]:
    descriptor = require_exact_object(
        value,
        {"id", "version", "kind", "display_name", "entrypoints"},
        "Guest descriptor",
    )
    app_id = _bounded_text(descriptor["id"], "id", 128)
    if not APP_ID_RE.fullmatch(app_id):
        raise PipelineError("malformed-output", "Guest descriptor app id is invalid")
    version = _bounded_text(descriptor["version"], "version", 128)
    if not SEMVER_RE.fullmatch(version):
        raise PipelineError("malformed-output", "Guest descriptor version is invalid")
    if descriptor["kind"] not in {"ui", "service", "hybrid"}:
        raise PipelineError("malformed-output", "Guest descriptor app kind is invalid")
    display_name = _bounded_text(descriptor["display_name"], "display_name", 80)
    entrypoints = descriptor["entrypoints"]
    if not isinstance(entrypoints, list) or not 1 <= len(entrypoints) <= 16:
        raise PipelineError(
            "malformed-output", "Guest descriptor entrypoint count is invalid"
        )
    normalized_entrypoints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(entrypoints):
        row = require_exact_object(
            item,
            {"id", "kind", "label", "initial_route"},
            f"Guest descriptor entrypoint[{index}]",
        )
        entrypoint_id = _bounded_text(row["id"], f"entrypoint[{index}].id", 128)
        if not APP_ID_RE.fullmatch(entrypoint_id) or entrypoint_id in seen:
            raise PipelineError(
                "malformed-output",
                "Guest descriptor entrypoint id is invalid or duplicated",
            )
        if row["kind"] not in {"launcher-ui", "service", "settings"}:
            raise PipelineError(
                "malformed-output", "Guest descriptor entrypoint kind is invalid"
            )
        label = _bounded_text(row["label"], f"entrypoint[{index}].label", 80)
        initial_route = row["initial_route"]
        if initial_route is not None:
            initial_route = _bounded_text(
                initial_route, f"entrypoint[{index}].initial_route", 128
            )
            if not APP_ID_RE.fullmatch(initial_route):
                raise PipelineError(
                    "malformed-output",
                    "Guest descriptor initial route is invalid",
                )
        normalized_entrypoints.append(
            {
                "id": entrypoint_id,
                "kind": row["kind"],
                "label": label,
                "initial_route": initial_route,
            }
        )
        seen.add(entrypoint_id)
    return {
        "id": app_id,
        "version": version,
        "kind": descriptor["kind"],
        "display_name": display_name,
        "entrypoints": normalized_entrypoints,
    }


def expected_guest_descriptor(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project only the manifest fields governed by RFC 0001 invariant 9."""
    app = manifest["app"]
    entrypoints = []
    for item in manifest["entrypoints"]:
        entrypoints.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "label": item["label"],
                "initial_route": (
                    item["routes"]["initial"]
                    if item["kind"] == "launcher-ui"
                    else None
                ),
            }
        )
    return {
        "id": app["id"],
        "version": app["version"],
        "kind": app["kind"],
        "display_name": app["display_name"],
        "entrypoints": entrypoints,
    }


def reconcile_guest_descriptor(
    manifest: dict[str, Any], descriptor_value: Any
) -> dict[str, Any]:
    """Require an exact one-to-one descriptor match without relying on list order."""
    actual = _validated_descriptor(descriptor_value)
    expected = expected_guest_descriptor(manifest)
    for field in ("id", "version", "kind", "display_name"):
        if actual[field] != expected[field]:
            raise PipelineError(
                "incompatible-contract",
                f"Guest descriptor {field} differs from manifest",
            )

    actual_by_id = {item["id"]: item for item in actual["entrypoints"]}
    expected_by_id = {item["id"]: item for item in expected["entrypoints"]}
    missing = sorted(set(expected_by_id) - set(actual_by_id))
    extra = sorted(set(actual_by_id) - set(expected_by_id))
    if missing or extra:
        raise PipelineError(
            "incompatible-contract",
            f"Guest descriptor entrypoint set differs missing={missing} extra={extra}",
        )
    for entrypoint_id in sorted(expected_by_id):
        if actual_by_id[entrypoint_id] != expected_by_id[entrypoint_id]:
            raise PipelineError(
                "incompatible-contract",
                f"Guest descriptor entrypoint {entrypoint_id} differs from manifest",
            )
    return actual


def _strict_inspector_output(payload: bytes) -> dict[str, Any]:
    if (
        not payload
        or len(payload) > MAX_INSPECTOR_OUTPUT_BYTES
        or not payload.endswith(b"\n")
        or payload.count(b"\n") != 1
    ):
        raise PipelineError(
            "malformed-output", "Guest descriptor inspector output framing is invalid"
        )
    try:
        value = json.loads(payload, object_pairs_hook=strict_object)
    except PipelineError as error:
        raise PipelineError(
            "malformed-output", "Guest descriptor inspector emitted duplicate JSON fields"
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineError(
            "malformed-output", "Guest descriptor inspector output is not strict JSON"
        ) from error
    return require_exact_object(
        value,
        {"schema_version", "component_sha256", "world", "descriptor", "isolation"},
        "Guest descriptor inspector output",
    )


def run_guest_descriptor_reconciliation(
    component: Path,
    manifest: dict[str, Any],
    inspector: Path = DEFAULT_COMPONENT_INSPECTOR,
) -> dict[str, str]:
    executable, inspector_digest = validated_component_inspector(inspector)
    limits = ProcessLimits(
        wall_seconds=15,
        cpu_seconds=10,
        memory_bytes=1024 * 1024 * 1024,
        pids=4,
        disk_bytes=64 * 1024 * 1024,
        stdout_bytes=MAX_INSPECTOR_OUTPUT_BYTES,
        stderr_bytes=64 * 1024,
        open_files=64,
    )
    result = run_bounded(
        [
            str(executable),
            "--inspect-descriptor",
            "--component",
            str(component),
            "--expected-sha256",
            manifest["artifacts"]["canonical_component"]["sha256"],
            "--world",
            manifest["runtime"]["world"],
        ],
        cwd=component.parent,
        environment={"PATH": "/usr/bin:/bin", "TZ": "UTC", "LANG": "C", "LC_ALL": "C"},
        limits=limits,
        disk_root=component.parent,
    )
    value = _strict_inspector_output(result.stdout)
    if value["schema_version"] != INSPECTOR_SCHEMA:
        raise PipelineError(
            "malformed-output", "Guest descriptor inspector schema is unsupported"
        )
    if (
        value["component_sha256"]
        != manifest["artifacts"]["canonical_component"]["sha256"]
        or value["world"] != manifest["runtime"]["world"]
    ):
        raise PipelineError(
            "integrity-failure",
            "Guest descriptor inspector output is not bound to the exact component/world",
        )
    isolation = require_exact_object(
        value["isolation"],
        {
            "separate_process",
            "ambient_wasi_linked",
            "live_host_effects",
            "linear_memory_limit_bytes",
            "fuel_budget",
            "deadline_ms",
        },
        "Guest descriptor inspector isolation",
    )
    if isolation != {
        "separate_process": True,
        "ambient_wasi_linked": False,
        "live_host_effects": False,
        "linear_memory_limit_bytes": INSPECTION_MEMORY_BYTES,
        "fuel_budget": INSPECTION_FUEL,
        "deadline_ms": INSPECTION_DEADLINE_MS,
    }:
        raise PipelineError(
            "integrity-failure",
            "Guest descriptor inspector did not attest the fixed describe-only isolation",
        )
    reconcile_guest_descriptor(manifest, value["descriptor"])
    return {
        "id": "guest-descriptor-reconciliation",
        "outcome": "pass",
        "tool": f"vibapp-component-inspector sha256:{inspector_digest[:12]}",
        "detail": (
            "app identity/display name and every entrypoint id/kind/label/initial route "
            "match one-to-one; no extra Guest entrypoints"
        ),
    }
