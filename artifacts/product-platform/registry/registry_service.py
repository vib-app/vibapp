#!/usr/bin/env python3
"""Bounded deterministic local Registry + NeedSpec matching implementation.

The product schemas consumed here are authored candidates, not accepted contracts.
This process performs no network, install, publication, provider or credential work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
CATALOG_SOURCE = BASE / "fixtures" / "catalog.source.json"
STATUS = "product-platform-local-hold"
CORPUS_VERSION = "fixture-corpus.2026-08-24.1"
SNAPSHOT_TIME = "2026-08-24T02:00:00Z"

MAX_NEED_BYTES = 64 * 1024
MAX_CORPUS_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_SCAN_RECORDS = 50
MAX_RETURNED_MATCHES = 20

PACKAGE_FORMAT = "vibapp.package.experimental-v0"
COMPONENT_CONTRACT = "vibapp:experimental-v0@0.0.1"
WASI = "0.2"
SUPPORTED_WORLDS = {
    "ui-only-reference",
    "service-only-reference",
    "hybrid-reference",
    "web-preview-reference",
}
INTERFACE_PREFIX = "vibapp:experimental-v0/"
INTERFACE_SUFFIX = "@0.0.1"

WORLD_IMPORTS = {
    "ui-only-reference": ["clock", "kv", "log", "host-info", "settings"],
    "service-only-reference": [
        "clock", "scheduler", "kv", "log", "host-info", "settings",
        "system-metrics", "http",
    ],
    "hybrid-reference": [
        "clock", "scheduler", "notification", "kv", "log", "host-info",
        "settings",
    ],
    "web-preview-reference": [
        "clock", "scheduler", "kv", "log", "host-info", "settings",
        "system-metrics", "http",
    ],
}

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
APP_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*|[\u3400-\u9fff]")
STOP_WORDS = {
    "a", "an", "and", "as", "at", "be", "for", "in", "is", "it", "of",
    "on", "or", "the", "this", "to", "with", "without",
}
REJECTION_ORDER = [
    "package-format", "contract", "wasi", "wit-world", "platform", "profile",
    "permission-ceiling", "negative-constraint", "publisher-trust", "verification",
    "revocation", "publication-state", "privacy",
]


class InputError(ValueError):
    """A closed, user-safe validation failure."""


def reject_constant(value: str) -> None:
    raise InputError(f"non-standard JSON constant is forbidden: {value}")


def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, maximum_bytes: int) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc
    if size > maximum_bytes:
        raise InputError(f"input exceeds {maximum_bytes} bytes")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=no_duplicate_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InputError(f"invalid JSON: {exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise InputError(f"{label} missing fields: {', '.join(missing)}")
    if extra:
        raise InputError(f"{label} has unknown fields: {', '.join(extra)}")
    return value


def bounded_text(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InputError(f"{label} must be a string")
    if (not allow_empty and not value) or len(value) > maximum:
        raise InputError(f"{label} length is outside 1..{maximum}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InputError(f"{label} contains a control character")
    return value


def identifier(value: Any, label: str) -> str:
    text = bounded_text(value, label, 128)
    if not IDENTIFIER.fullmatch(text):
        raise InputError(f"{label} is not a valid identifier")
    return text


def unique_strings(value: Any, label: str, maximum_items: int, maximum_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise InputError(f"{label} must be an array of at most {maximum_items} items")
    result = [bounded_text(item, f"{label}[]", maximum_length) for item in value]
    if len(result) != len(set(result)):
        raise InputError(f"{label} must contain unique values")
    return result


def validate_utc(value: Any, label: str) -> None:
    text = bounded_text(value, label, 20)
    if not UTC.fullmatch(text):
        raise InputError(f"{label} must be canonical whole-second UTC")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise InputError(f"{label} is not a calendar instant") from exc


def validate_need(need: Any) -> dict[str, Any]:
    required = {
        "schema_version", "document_type", "need_id", "owner", "goal",
        "requirements", "negative_constraints", "platforms", "profiles",
        "permission_ceiling", "privacy_requirement", "created_at_utc", "revision",
    }
    need = exact_keys(need, required, "need")
    if need["schema_version"] != "vibapp.need-spec.product-v0.0.1":
        raise InputError("need.schema_version is unsupported")
    if need["document_type"] != "need-spec":
        raise InputError("need.document_type must be need-spec")
    identifier(need["need_id"], "need.need_id")
    owner = exact_keys(need["owner"], {"principal_id", "principal_kind"}, "need.owner")
    identifier(owner["principal_id"], "need.owner.principal_id")
    if owner["principal_kind"] not in {"user", "service", "auditor"}:
        raise InputError("need.owner.principal_kind is invalid")
    bounded_text(need["goal"], "need.goal", 4000)

    requirements = need["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 64:
        raise InputError("need.requirements must contain 1..64 entries")
    requirement_ids: list[str] = []
    for index, item in enumerate(requirements):
        item = exact_keys(
            item, {"requirement_id", "text", "priority", "acceptance_examples"},
            f"need.requirements[{index}]",
        )
        requirement_ids.append(identifier(item["requirement_id"], f"requirement[{index}].id"))
        bounded_text(item["text"], f"requirement[{index}].text", 1000)
        if item["priority"] not in {"must-have", "nice-to-have"}:
            raise InputError(f"requirement[{index}].priority is invalid")
        examples = item["acceptance_examples"]
        if not isinstance(examples, list) or len(examples) > 16:
            raise InputError(f"requirement[{index}].acceptance_examples is invalid")
        for example_index, example in enumerate(examples):
            bounded_text(example, f"requirement[{index}].acceptance_examples[{example_index}]", 1000)
    if len(requirement_ids) != len(set(requirement_ids)):
        raise InputError("need.requirement_id values must be unique")

    constraints = need["negative_constraints"]
    if not isinstance(constraints, list) or len(constraints) > 64:
        raise InputError("need.negative_constraints must contain at most 64 entries")
    constraint_ids: list[str] = []
    constraint_kinds = {
        "forbidden-capability", "forbidden-publisher", "forbidden-package",
        "privacy", "platform", "profile", "other",
    }
    for index, item in enumerate(constraints):
        item = exact_keys(
            item, {"constraint_id", "kind", "value", "source"},
            f"need.negative_constraints[{index}]",
        )
        constraint_ids.append(identifier(item["constraint_id"], f"constraint[{index}].id"))
        if item["kind"] not in constraint_kinds:
            raise InputError(f"constraint[{index}].kind is invalid")
        bounded_text(item["value"], f"constraint[{index}].value", 500)
        if item["source"] not in {"user", "rejection-feedback", "policy"}:
            raise InputError(f"constraint[{index}].source is invalid")
    if len(constraint_ids) != len(set(constraint_ids)):
        raise InputError("need.constraint_id values must be unique")

    platforms = need["platforms"]
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= 16:
        raise InputError("need.platforms must contain 1..16 entries")
    valid_os = {"macos", "windows", "linux", "browser"}
    valid_arch = {"aarch64", "x86-64", "wasm32"}
    valid_profiles = {"desktop", "web-preview", "web-runtime", "headless"}
    platform_keys: list[tuple[str, str, str]] = []
    for index, item in enumerate(platforms):
        item = exact_keys(item, {"os", "arch", "profile"}, f"need.platforms[{index}]")
        key = (item["os"], item["arch"], item["profile"])
        if key[0] not in valid_os or key[1] not in valid_arch or key[2] not in valid_profiles:
            raise InputError(f"need.platforms[{index}] contains an unsupported enum")
        platform_keys.append(key)
    if len(platform_keys) != len(set(platform_keys)):
        raise InputError("need.platforms must contain unique tuples")

    profiles = unique_strings(need["profiles"], "need.profiles", 4, 32)
    if not profiles or not set(profiles) <= valid_profiles:
        raise InputError("need.profiles must contain supported profiles")
    if any(profile not in profiles for _, _, profile in platform_keys):
        raise InputError("each platform profile must also appear in need.profiles")

    ceiling = exact_keys(
        need["permission_ceiling"],
        {"allowed_interfaces", "forbidden_interfaces", "maximum_scope_digests"},
        "need.permission_ceiling",
    )
    allowed = unique_strings(ceiling["allowed_interfaces"], "allowed_interfaces", 32, 200)
    forbidden = unique_strings(ceiling["forbidden_interfaces"], "forbidden_interfaces", 32, 200)
    scopes = unique_strings(ceiling["maximum_scope_digests"], "maximum_scope_digests", 32, 64)
    if set(allowed) & set(forbidden):
        raise InputError("permission interface cannot be both allowed and forbidden")
    if any(not re.fullmatch(r"[0-9a-f]{64}", scope) for scope in scopes):
        raise InputError("maximum_scope_digests contains a non-SHA-256 value")
    if need["privacy_requirement"] not in {"local-private", "remote-private", "public"}:
        raise InputError("need.privacy_requirement is invalid")
    validate_utc(need["created_at_utc"], "need.created_at_utc")
    if not isinstance(need["revision"], int) or isinstance(need["revision"], bool) or need["revision"] < 1:
        raise InputError("need.revision must be an integer of at least 1")
    return need


def interface_name(short_name: str) -> str:
    return f"{INTERFACE_PREFIX}{short_name}{INTERFACE_SUFFIX}"


def artifact_ref(label: str) -> dict[str, Any]:
    return {
        "sha256": digest(label.encode("utf-8")),
        "size_bytes": 128,
        "media_type": "application/json",
        "visibility": "public",
    }


def expand_record(source: dict[str, Any], index: int) -> dict[str, Any]:
    app_id = source["app_id"]
    version = source["version"]
    world = source["wit_world"]
    if world not in WORLD_IMPORTS:
        raise InputError(f"catalog app {app_id} has unknown WIT world")
    permissions = []
    for short_name in WORLD_IMPORTS[world]:
        full_name = interface_name(short_name)
        permissions.append({
            "interface": full_name,
            "necessity": "degradable" if short_name == "http" else "required",
            "scope_digest_sha256": digest(f"{app_id}:scope:{full_name}".encode("utf-8")),
            "summary": f"Synthetic fixture declaration for {short_name}.",
        })
    package_digest = digest(f"{app_id}:{version}:package".encode("utf-8"))
    return {
        "schema_version": "vibapp.registry-record.product-v0.0.1",
        "document_type": "registry-record",
        "record_id": f"registry.fixture.{source['fixture_id']}.1",
        "record_revision": 1,
        "app": {
            "id": app_id,
            "version": version,
            "kind": source["kind"],
            "display_name": source["display_name"],
            "summary": source["summary"],
            "publisher": {
                "publisher_id": "fixture.vibapp",
                "display_name": "VibApp Synthetic Fixtures",
                "verification_state": "verified",
            },
        },
        "package": {
            "app_id": app_id,
            "version": version,
            "package_digest_sha256": package_digest,
            "manifest_digest_sha256": digest(f"{app_id}:{version}:manifest".encode("utf-8")),
        },
        "contract": {
            "package_format": PACKAGE_FORMAT,
            "component_contract": COMPONENT_CONTRACT,
            "wasi": WASI,
            "wit_world": world,
        },
        "compatibility": {
            "platforms": source["platforms"],
            "profiles": source["profiles"],
        },
        "permissions": permissions,
        "source": {
            "visibility": "public",
            "source_digest_sha256": digest(f"{app_id}:{version}:source".encode("utf-8")),
            "license_spdx": "Apache-2.0",
        },
        "verification": {
            "status": "verified",
            "revocation": "not-revoked",
            "evidence": [{
                "evidence_id": f"fixture.verify.{index + 1}",
                "kind": "verifier-report",
                "sha256": digest(f"{app_id}:{version}:verify".encode("utf-8")),
            }],
            "provenance": artifact_ref(f"{app_id}:{version}:provenance"),
            "sbom": artifact_ref(f"{app_id}:{version}:sbom"),
            "scan": artifact_ref(f"{app_id}:{version}:scan"),
        },
        "publication": {
            "state": "published",
            "published_at_utc": SNAPSHOT_TIME,
            "revoked_at_utc": None,
            "public_metadata_digest_sha256": digest(f"{app_id}:{version}:metadata".encode("utf-8")),
        },
        "search_metadata": {
            "tags": source["tags"],
            "capability_labels": source["capability_labels"],
            "search_text_digest_sha256": digest({
                "name": source["display_name"], "summary": source["summary"],
                "tags": source["tags"], "capabilities": source["capability_labels"],
            }),
        },
        "created_at_utc": SNAPSHOT_TIME,
    }


def load_records() -> list[dict[str, Any]]:
    source = load_json(CATALOG_SOURCE, MAX_CORPUS_BYTES)
    source = exact_keys(source, {"fixture_version", "status", "apps"}, "catalog")
    if source["status"] != "synthetic-product-platform-local-hold":
        raise InputError("catalog is missing the synthetic HOLD status")
    apps = source["apps"]
    if not isinstance(apps, list) or not 1 <= len(apps) <= MAX_SCAN_RECORDS:
        raise InputError(f"catalog apps must contain 1..{MAX_SCAN_RECORDS} entries")
    records = [expand_record(app, index) for index, app in enumerate(apps)]
    app_ids = [record["app"]["id"] for record in records]
    if len(app_ids) != len(set(app_ids)):
        raise InputError("catalog app IDs must be unique")
    return records


def browser_artifact_bindings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build synthetic, Registry-owned browser bindings without widening record-v0."""
    source = load_json(CATALOG_SOURCE, MAX_CORPUS_BYTES)
    source = exact_keys(source, {"fixture_version", "status", "apps"}, "catalog")
    records_by_app = {record["app"]["id"]: record for record in records}
    bindings: list[dict[str, Any]] = []
    for index, app_source in enumerate(source["apps"]):
        declaration = app_source.get("browser_derivation")
        if declaration is None:
            continue
        declaration = exact_keys(
            declaration,
            {
                "profile", "artifact_path", "artifact_media_type", "artifact_format",
                "artifact_sha256", "artifact_size_bytes",
                "canonical_component_reference_sha256",
            },
            f"catalog.apps[{index}].browser_derivation",
        )
        app_id = app_source["app_id"]
        record = records_by_app.get(app_id)
        if record is None:
            raise InputError(f"browser derivation has no Registry record: {app_id}")
        profile = bounded_text(declaration["profile"], "browser_derivation.profile", 32)
        if profile not in {"web-preview", "web-runtime"}:
            raise InputError("browser_derivation.profile must be a browser profile")
        if profile not in record["compatibility"]["profiles"]:
            raise InputError("browser_derivation.profile is absent from Registry compatibility")
        artifact_path = bounded_text(
            declaration["artifact_path"], "browser_derivation.artifact_path", 256
        )
        if not artifact_path.startswith("/launcher/") or any(
            part in {"", ".", ".."} for part in artifact_path.split("/")[1:]
        ):
            raise InputError("browser_derivation.artifact_path must stay under /launcher")
        artifact_sha256 = declaration["artifact_sha256"]
        component_reference = declaration["canonical_component_reference_sha256"]
        if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
            raise InputError("browser_derivation.artifact_sha256 must be lowercase SHA-256")
        if not isinstance(component_reference, str) or not re.fullmatch(r"[0-9a-f]{64}", component_reference):
            raise InputError("browser_derivation canonical Component reference must be lowercase SHA-256")
        size_bytes = declaration["artifact_size_bytes"]
        if (
            not isinstance(size_bytes, int) or isinstance(size_bytes, bool)
            or not 1 <= size_bytes <= 16 * 1024 * 1024
        ):
            raise InputError("browser_derivation.artifact_size_bytes is outside 1..16777216")
        if declaration["artifact_media_type"] != "application/wasm":
            raise InputError("browser_derivation artifact must use application/wasm")
        if declaration["artifact_format"] != "core-wasm":
            raise InputError("browser_derivation fixture must declare core-wasm")

        binding_payload = {
            "app_id": app_id,
            "profile": profile,
            "registry_record_id": record["record_id"],
            "registry_record_revision": record["record_revision"],
            "canonical_package_digest_sha256": record["package"]["package_digest_sha256"],
            "canonical_component_reference_sha256": component_reference,
            "derived_from_sha256": component_reference,
            "artifact": {
                "path": artifact_path,
                "media_type": declaration["artifact_media_type"],
                "format": declaration["artifact_format"],
                "sha256": artifact_sha256,
                "size_bytes": size_bytes,
            },
        }
        bindings.append({
            "schema_version": "vibapp.browser-artifact-binding.synthetic-v1",
            **binding_payload,
            "attestation": {
                "kind": "registry-generated-synthetic-binding",
                "binding_payload_sha256": digest(binding_payload),
                "trusted_builder_policy": "synthetic-fixture-only",
                "verification_state": "verified-synthetic-fixture",
                "canonical_component_transformation_proven": False,
            },
        })
    bindings.sort(key=lambda item: (item["app_id"], item["profile"], item["artifact"]["sha256"]))
    return bindings


def tokens(value: str) -> set[str]:
    return {item for item in ASCII_TOKEN.findall(value.casefold()) if item not in STOP_WORDS}


def bigrams(value: str) -> set[str]:
    normalized = " ".join(sorted(tokens(value)))
    return {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}


def dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))


def append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def hard_filter(need: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    facts: list[str] = []
    contract = record["contract"]
    expected_contract = [
        ("package_format", PACKAGE_FORMAT, "package-format"),
        ("component_contract", COMPONENT_CONTRACT, "contract"),
        ("wasi", WASI, "wasi"),
    ]
    for field, expected, reason in expected_contract:
        facts.append(f"/contract/{field}={contract[field]}")
        if contract[field] != expected:
            append_reason(reasons, reason)
    facts.append(f"/contract/wit_world={contract['wit_world']}")
    if contract["wit_world"] not in SUPPORTED_WORLDS:
        append_reason(reasons, "wit-world")

    record_platforms = {
        (item["os"], item["arch"], item["profile"])
        for item in record["compatibility"]["platforms"]
    }
    required_platforms = {
        (item["os"], item["arch"], item["profile"])
        for item in need["platforms"]
    }
    facts.append("/compatibility/platforms")
    if not required_platforms <= record_platforms:
        append_reason(reasons, "platform")
    facts.append("/compatibility/profiles")
    if not set(need["profiles"]) <= set(record["compatibility"]["profiles"]):
        append_reason(reasons, "profile")

    ceiling = need["permission_ceiling"]
    allowed = set(ceiling["allowed_interfaces"])
    forbidden = set(ceiling["forbidden_interfaces"])
    maximum_scopes = set(ceiling["maximum_scope_digests"])
    interfaces = {permission["interface"] for permission in record["permissions"]}
    scopes = {permission["scope_digest_sha256"] for permission in record["permissions"]}
    facts.extend(["/permissions", "/permission_ceiling/allowed_interfaces", "/permission_ceiling/forbidden_interfaces"])
    if not interfaces <= allowed or interfaces & forbidden:
        append_reason(reasons, "permission-ceiling")
    if maximum_scopes and not scopes <= maximum_scopes:
        append_reason(reasons, "permission-ceiling")

    publisher = record["app"]["publisher"]
    facts.append(f"/app/publisher/verification_state={publisher['verification_state']}")
    if publisher["verification_state"] != "verified":
        append_reason(reasons, "publisher-trust")
    verification = record["verification"]
    facts.append(f"/verification/status={verification['status']}")
    if verification["status"] != "verified":
        append_reason(reasons, "verification")
    facts.append(f"/verification/revocation={verification['revocation']}")
    if verification["revocation"] != "not-revoked":
        append_reason(reasons, "revocation")
    facts.append(f"/publication/state={record['publication']['state']}")
    if record["publication"]["state"] != "published":
        append_reason(reasons, "publication-state")
    facts.append(f"/source/visibility={record['source']['visibility']}")
    if record["source"]["visibility"] != "public":
        append_reason(reasons, "publisher-trust")

    http_interface = interface_name("http")
    facts.append(f"/need/privacy_requirement={need['privacy_requirement']}")
    if need["privacy_requirement"] == "local-private" and http_interface in interfaces:
        append_reason(reasons, "privacy")

    searchable = " ".join(
        [record["app"]["id"], publisher["publisher_id"]]
        + record["search_metadata"]["tags"]
        + record["search_metadata"]["capability_labels"]
    ).casefold()
    for constraint in need["negative_constraints"]:
        kind = constraint["kind"]
        value = constraint["value"].casefold()
        violated = False
        if kind == "forbidden-capability":
            violated = value in {item.casefold() for item in interfaces} or value in {
                item.casefold() for item in record["search_metadata"]["capability_labels"]
            }
        elif kind == "forbidden-publisher":
            violated = value == publisher["publisher_id"].casefold()
        elif kind == "forbidden-package":
            violated = value in {
                record["app"]["id"].casefold(),
                record["package"]["package_digest_sha256"].casefold(),
            }
        elif kind == "platform":
            violated = any(value in {os_name, arch, profile} for os_name, arch, profile in record_platforms)
        elif kind == "profile":
            violated = value in {item.casefold() for item in record["compatibility"]["profiles"]}
        elif kind == "privacy":
            violated = value in {"network", "remote-processing", "http"} and http_interface in interfaces
        elif kind == "other":
            violated = value in searchable.split()
        if violated:
            append_reason(reasons, "negative-constraint")
            facts.append(f"/need/negative_constraints/{constraint['constraint_id']}")

    ordered_reasons = [reason for reason in REJECTION_ORDER if reason in reasons]
    return {"eligible": not ordered_reasons, "rejected_reasons": ordered_reasons, "checked_facts": facts[:128]}


def evidence_text(record: dict[str, Any]) -> list[tuple[str, str]]:
    result = [
        ("/app/display_name", record["app"]["display_name"]),
        ("/app/summary", record["app"]["summary"]),
    ]
    result.extend(
        (f"/search_metadata/tags/{index}", value)
        for index, value in enumerate(record["search_metadata"]["tags"])
    )
    result.extend(
        (f"/search_metadata/capability_labels/{index}", value)
        for index, value in enumerate(record["search_metadata"]["capability_labels"])
    )
    return result


def soft_match(need: dict[str, Any], record: dict[str, Any], hard: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = evidence_text(record)
    candidate_tokens = set().union(*(tokens(text) for _, text in fields))
    need_text = " ".join(
        [need["goal"]]
        + [requirement["text"] for requirement in need["requirements"]]
        + [example for requirement in need["requirements"] for example in requirement["acceptance_examples"]]
    )
    need_tokens = tokens(need_text)
    lexical = len(need_tokens & candidate_tokens) / max(1, len(need_tokens))
    vector = dice(bigrams(need_text), bigrams(" ".join(text for _, text in fields)))

    coverage: list[dict[str, Any]] = []
    matched: list[str] = []
    unmet: list[str] = []
    explanations: list[dict[str, Any]] = []
    for req_index, requirement in enumerate(need["requirements"]):
        requirement_text = " ".join([requirement["text"]] + requirement["acceptance_examples"])
        requirement_tokens = tokens(requirement_text)
        overlap = requirement_tokens & candidate_tokens
        ratio = len(overlap) / max(1, len(requirement_tokens))
        evidence_fields = [path for path, text in fields if tokens(text) & overlap]
        if ratio >= 0.30 and overlap:
            state = "covered"
            matched.append(requirement["requirement_id"])
        elif overlap:
            state = "partial"
            unmet.append(requirement["requirement_id"])
        else:
            state = "uncovered"
            unmet.append(requirement["requirement_id"])
        coverage.append({
            "requirement_id": requirement["requirement_id"],
            "state": state,
            "evidence_fields": evidence_fields[:32],
        })
        if evidence_fields:
            explanations.append({
                "message": f"{requirement['requirement_id']} matched candidate metadata ({state}).",
                "evidence_fields": [f"/need/requirements/{req_index}"] + evidence_fields[:8],
            })
        else:
            explanations.append({
                "message": f"{requirement['requirement_id']} has no supporting candidate metadata.",
                "evidence_fields": [f"/need/requirements/{req_index}", "/search_metadata"],
            })

    must_have_ids = {
        requirement["requirement_id"] for requirement in need["requirements"]
        if requirement["priority"] == "must-have"
    }
    uncovered_must = {
        item["requirement_id"] for item in coverage
        if item["requirement_id"] in must_have_ids and item["state"] != "covered"
    }
    decision = "partial" if uncovered_must else "compatible"
    explanation_fields = sorted({
        path for explanation in explanations for path in explanation["evidence_fields"]
    })
    match = {
        "schema_version": "vibapp.candidate-match.product-v0.0.1",
        "document_type": "candidate-match",
        "need_id": need["need_id"],
        "candidate": record["package"],
        "decision": decision,
        "hard_filter": hard,
        "coverage": coverage,
        "gaps": [f"Unmet requirement: {requirement_id}" for requirement_id in unmet],
        "required_permissions": record["permissions"],
        "compatibility_facts": ["/contract", "/compatibility/platforms", "/compatibility/profiles"],
        "source_facts": ["/source/visibility", "/app/publisher/verification_state", "/verification/status"],
        "explanation_evidence_fields": explanation_fields or ["/search_metadata"],
        "scores": {"lexical": round(lexical, 6), "vector": round(vector, 6)},
        "stable_order": 0,
    }
    consumer = {
        "record": record,
        "match": match,
        "matched": matched,
        "unmet": unmet,
        "explanation": explanations + [{
            "message": "Compatibility and trust passed before ranking.",
            "evidence_fields": [
                "/contract", "/compatibility/platforms", "/compatibility/profiles",
                "/verification/status", "/verification/revocation", "/publication/state",
            ],
        }],
    }
    return match, consumer


def search(need: dict[str, Any], records: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    validate_need(need)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RETURNED_MATCHES:
        raise InputError(f"limit must be between 1 and {MAX_RETURNED_MATCHES}")
    if len(records) > MAX_SCAN_RECORDS:
        raise InputError(f"corpus exceeds {MAX_SCAN_RECORDS} records")

    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejected: list[dict[str, Any]] = []
    for record in records:
        hard = hard_filter(need, record)
        if not hard["eligible"]:
            rejected.append({
                "candidate": record["package"],
                "display_name": record["app"]["display_name"],
                "rejected_reasons": hard["rejected_reasons"],
                "checked_facts": hard["checked_facts"],
            })
            continue
        eligible.append(soft_match(need, record, hard))

    def order(item: tuple[dict[str, Any], dict[str, Any]]) -> tuple[Any, ...]:
        match = item[0]
        score = 0.65 * match["scores"]["lexical"] + 0.35 * match["scores"]["vector"]
        return (
            0 if match["decision"] == "compatible" else 1,
            -score,
            match["candidate"]["app_id"],
            match["candidate"]["package_digest_sha256"],
        )

    eligible.sort(key=order)
    selected = eligible[:limit]
    for stable_order, (match, consumer) in enumerate(selected):
        match["stable_order"] = stable_order
        consumer["match"]["stable_order"] = stable_order
    matches = [item[0] for item in selected]
    consumer_matches = [item[1] for item in selected]
    request_digest = digest(need)
    request_id = f"search.{request_digest[:24]}"
    empty_reason = None if matches else "No candidate passed every hard filter."
    result = {
        "platform_status": STATUS,
        "schema_basis": "authored-unaccepted-product-v0-candidate",
        "search_response": {
            "schema_version": "vibapp.search-response.product-v0.0.1",
            "document_type": "search-response",
            "request_id": request_id,
            "query_digest_sha256": request_digest,
            "matches": matches,
            "empty_reason": empty_reason,
            "next_cursor": None,
        },
        "matched": consumer_matches,
        "rejected": rejected,
        "empty_reason": empty_reason,
        "limits": {
            "maximum_need_bytes": MAX_NEED_BYTES,
            "maximum_corpus_bytes": MAX_CORPUS_BYTES,
            "maximum_scanned_records": MAX_SCAN_RECORDS,
            "maximum_returned_matches": MAX_RETURNED_MATCHES,
            "maximum_output_bytes": MAX_OUTPUT_BYTES,
        },
    }
    if len(canonical_bytes(result)) > MAX_OUTPUT_BYTES:
        raise InputError(f"output exceeds {MAX_OUTPUT_BYTES} bytes")
    return result


def snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    sample_need = validate_need(load_json(BASE / "fixtures" / "needs" / "focus.valid.json", MAX_NEED_BYTES))
    value = {
        "snapshot_version": "vibapp.fast-registry-snapshot.2026-08-24.1",
        "platform_status": STATUS,
        "generated_at_utc": SNAPSHOT_TIME,
        "corpus_version": CORPUS_VERSION,
        "records": records,
        "browser_artifact_bindings": browser_artifact_bindings(records),
        "sample_search": search(sample_need, records, 5),
        "consumer_notes": {
            "record_contract": "candidate registry-record.product-v0",
            "match_contract": "candidate candidate-match.product-v0",
            "matched_field": "sample_search.matched[].matched",
            "unmet_field": "sample_search.matched[].unmet",
            "explanation_field": "sample_search.matched[].explanation",
            "install_authority": False,
            "publication_authority": False,
        },
    }
    if len(canonical_bytes(value)) > MAX_OUTPUT_BYTES:
        raise InputError(f"snapshot exceeds {MAX_OUTPUT_BYTES} bytes")
    return value


def emit(value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise InputError(f"output exceeds {MAX_OUTPUT_BYTES} bytes")
    sys.stdout.buffer.write(encoded)


def safe_output_path(raw: str) -> Path:
    path = Path(raw).resolve()
    try:
        path.relative_to(BASE)
    except ValueError as exc:
        raise InputError("snapshot output must stay inside the Registry directory") from exc
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-need")
    validate_parser.add_argument("--need", required=True)
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--need", required=True)
    search_parser.add_argument("--limit", type=int, default=5)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--output")
    args = parser.parse_args(argv)

    try:
        if args.command == "validate-need":
            need = validate_need(load_json(Path(args.need), MAX_NEED_BYTES))
            emit({"platform_status": STATUS, "valid": True, "need_id": need["need_id"], "query_digest_sha256": digest(need)})
            return 0
        records = load_records()
        if args.command == "search":
            need = validate_need(load_json(Path(args.need), MAX_NEED_BYTES))
            emit(search(need, records, args.limit))
            return 0
        value = snapshot(records)
        if args.output:
            output = safe_output_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
            if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise InputError(f"snapshot exceeds {MAX_OUTPUT_BYTES} bytes")
            output.write_text(payload, encoding="utf-8")
            emit({"platform_status": STATUS, "snapshot": str(output), "sha256": digest(payload.encode("utf-8")), "bytes": len(payload.encode("utf-8"))})
        else:
            emit(value)
        return 0
    except (InputError, KeyError, TypeError, ValueError) as exc:
        emit({
            "platform_status": STATUS,
            "error": {"code": "invalid-argument", "message": str(exc), "retryable": False},
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
