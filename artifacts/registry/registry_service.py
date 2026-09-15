#!/usr/bin/env python3
"""Bounded retrieval-first Registry service and CLI.

This is an ignored experimental product artifact. It has no install, build,
CodeAgent, signing, verification-promotion, or publication authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol


BASE = Path(__file__).resolve().parent
CATALOG_PATH = BASE / "fixtures" / "catalog.json"
STATUS = "experimental-product-hold"

PACKAGE_FORMAT = "vibapp.package.experimental-v0"
COMPONENT_CONTRACT = "vibapp:experimental-v0@0.0.1"
WASI = "0.2"
INTERFACE_PREFIX = "vibapp:experimental-v0/"
INTERFACE_SUFFIX = "@0.0.1"
EMBEDDING_URL = "http://192.168.199.170:8081/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-ada-002"
EMBEDDING_DIMENSIONS = 384

MAX_NEED_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 96 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_EMBEDDING_REQUEST_BYTES = 384 * 1024
MAX_EMBEDDING_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_SCAN_RECORDS = 50
MAX_RETURNED_MATCHES = 20
MAX_EMBEDDING_INPUTS = 128
MAX_EMBEDDING_TEXT_CHARS = 8 * 1024
MAX_EMBEDDING_TOTAL_CHARS = 256 * 1024
MAX_HTTP_CONCURRENCY = 4
MAX_EMBEDDING_CONCURRENCY = 2
EMBEDDING_TIMEOUT_SECONDS = 5.0
MINIMUM_HYBRID_SCORE = 0.50
MINIMUM_REQUIREMENT_SIMILARITY = 0.55
MAX_CACHE_VECTORS = 256

WORLDS = {
    "ui-only-reference": ["clock", "kv", "log", "host-info", "settings"],
    "service-only-reference": [
        "clock", "scheduler", "kv", "log", "host-info", "settings",
        "system-metrics", "http",
    ],
    "hybrid-reference": [
        "clock", "scheduler", "notification", "kv", "log", "host-info", "settings",
    ],
    "web-preview-reference": [
        "clock", "scheduler", "kv", "log", "host-info", "settings",
        "system-metrics", "http",
    ],
}
APP_KINDS = {"ui", "service", "hybrid"}
PROFILES = {"desktop", "web-preview", "web-runtime", "headless"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
TOKEN = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*|[\u3400-\u9fff]")
STOP_WORDS = {
    "a", "an", "and", "as", "at", "be", "for", "in", "is", "it", "of",
    "on", "or", "the", "this", "to", "with", "without",
}
REJECTION_ORDER = [
    "package-format", "contract", "wasi", "wit-world", "platform", "profile",
    "app-kind", "required-capability", "permission-ceiling", "negative-constraint",
    "publisher-trust", "verification", "revocation", "publication-state", "privacy",
]


class RegistryError(ValueError):
    """A bounded user-safe error."""


class EmbeddingError(RuntimeError):
    """A bounded embedding-provider failure."""


class Embedder(Protocol):
    provider_name: str
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def reject_constant(value: str) -> None:
    raise RegistryError(f"non-standard JSON constant is forbidden: {value}")


def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(payload: bytes, maximum_bytes: int) -> Any:
    if len(payload) > maximum_bytes:
        raise RegistryError(f"input exceeds {maximum_bytes} bytes")
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=no_duplicate_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RegistryError(f"invalid JSON: {exc}") from exc


def load_json(path: Path, maximum_bytes: int) -> Any:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise RegistryError(f"input exceeds {maximum_bytes} bytes")
        return decode_json(path.read_bytes(), maximum_bytes)
    except OSError as exc:
        raise RegistryError(f"cannot read {path}: {exc.strerror}") from exc


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise RegistryError(f"{label} missing fields: {', '.join(missing)}")
    if extra:
        raise RegistryError(f"{label} has unknown fields: {', '.join(extra)}")
    return value


def bounded_text(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RegistryError(f"{label} must be a string")
    if (not allow_empty and not value) or len(value) > maximum:
        raise RegistryError(f"{label} length is outside 1..{maximum}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RegistryError(f"{label} contains a control character")
    return value


def identifier(value: Any, label: str) -> str:
    text = bounded_text(value, label, 128)
    if not IDENTIFIER.fullmatch(text):
        raise RegistryError(f"{label} is not a valid identifier")
    return text


def unique_strings(value: Any, label: str, maximum_items: int, maximum_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise RegistryError(f"{label} must be an array of at most {maximum_items} items")
    result = [bounded_text(item, f"{label}[]", maximum_length) for item in value]
    if len(result) != len(set(result)):
        raise RegistryError(f"{label} must contain unique values")
    return result


def validate_need(need: Any) -> dict[str, Any]:
    need = exact_keys(need, {
        "schema_version", "document_type", "need_id", "owner", "goal", "requirements",
        "negative_constraints", "platforms", "profiles", "permission_ceiling",
        "privacy_requirement", "created_at_utc", "revision",
    }, "need")
    if need["schema_version"] != "vibapp.need-spec.product-v0.0.1":
        raise RegistryError("need.schema_version is unsupported")
    if need["document_type"] != "need-spec":
        raise RegistryError("need.document_type must be need-spec")
    identifier(need["need_id"], "need.need_id")
    owner = exact_keys(need["owner"], {"principal_id", "principal_kind"}, "need.owner")
    identifier(owner["principal_id"], "need.owner.principal_id")
    if owner["principal_kind"] not in {"user", "service", "auditor"}:
        raise RegistryError("need.owner.principal_kind is invalid")
    bounded_text(need["goal"], "need.goal", 4000)

    requirements = need["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 64:
        raise RegistryError("need.requirements must contain 1..64 entries")
    requirement_ids: list[str] = []
    for index, item in enumerate(requirements):
        item = exact_keys(item, {"requirement_id", "text", "priority", "acceptance_examples"}, f"need.requirements[{index}]")
        requirement_ids.append(identifier(item["requirement_id"], f"requirement[{index}].id"))
        bounded_text(item["text"], f"requirement[{index}].text", 1000)
        if item["priority"] not in {"must-have", "nice-to-have"}:
            raise RegistryError(f"requirement[{index}].priority is invalid")
        examples = item["acceptance_examples"]
        if not isinstance(examples, list) or len(examples) > 16:
            raise RegistryError(f"requirement[{index}].acceptance_examples is invalid")
        for example_index, example in enumerate(examples):
            bounded_text(example, f"requirement[{index}].acceptance_examples[{example_index}]", 1000)
    if len(requirement_ids) != len(set(requirement_ids)):
        raise RegistryError("need.requirement_id values must be unique")

    constraints = need["negative_constraints"]
    if not isinstance(constraints, list) or len(constraints) > 64:
        raise RegistryError("need.negative_constraints must contain at most 64 entries")
    constraint_ids: list[str] = []
    for index, item in enumerate(constraints):
        item = exact_keys(item, {"constraint_id", "kind", "value", "source"}, f"need.negative_constraints[{index}]")
        constraint_ids.append(identifier(item["constraint_id"], f"constraint[{index}].id"))
        if item["kind"] not in {
            "forbidden-capability", "forbidden-publisher", "forbidden-package",
            "privacy", "platform", "profile", "other",
        }:
            raise RegistryError(f"constraint[{index}].kind is invalid")
        bounded_text(item["value"], f"constraint[{index}].value", 500)
        if item["source"] not in {"user", "rejection-feedback", "policy"}:
            raise RegistryError(f"constraint[{index}].source is invalid")
    if len(constraint_ids) != len(set(constraint_ids)):
        raise RegistryError("need.constraint_id values must be unique")

    platforms = need["platforms"]
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= 16:
        raise RegistryError("need.platforms must contain 1..16 entries")
    platform_keys: list[tuple[str, str, str]] = []
    for index, item in enumerate(platforms):
        item = exact_keys(item, {"os", "arch", "profile"}, f"need.platforms[{index}]")
        key = (item["os"], item["arch"], item["profile"])
        if key[0] not in {"macos", "windows", "linux", "browser"} or key[1] not in {"aarch64", "x86-64", "wasm32"} or key[2] not in PROFILES:
            raise RegistryError(f"need.platforms[{index}] contains an unsupported enum")
        platform_keys.append(key)
    if len(platform_keys) != len(set(platform_keys)):
        raise RegistryError("need.platforms must contain unique tuples")
    profiles = unique_strings(need["profiles"], "need.profiles", 4, 32)
    if not profiles or not set(profiles) <= PROFILES:
        raise RegistryError("need.profiles must contain supported profiles")
    if any(profile not in profiles for _, _, profile in platform_keys):
        raise RegistryError("each platform profile must also appear in need.profiles")

    ceiling = exact_keys(need["permission_ceiling"], {"allowed_interfaces", "forbidden_interfaces", "maximum_scope_digests"}, "need.permission_ceiling")
    allowed = unique_strings(ceiling["allowed_interfaces"], "allowed_interfaces", 32, 200)
    forbidden = unique_strings(ceiling["forbidden_interfaces"], "forbidden_interfaces", 32, 200)
    scopes = unique_strings(ceiling["maximum_scope_digests"], "maximum_scope_digests", 32, 64)
    if set(allowed) & set(forbidden):
        raise RegistryError("permission interface cannot be both allowed and forbidden")
    if any(not re.fullmatch(r"[0-9a-f]{64}", scope) for scope in scopes):
        raise RegistryError("maximum_scope_digests contains a non-SHA-256 value")
    if need["privacy_requirement"] not in {"local-private", "remote-private", "public"}:
        raise RegistryError("need.privacy_requirement is invalid")
    timestamp = bounded_text(need["created_at_utc"], "need.created_at_utc", 20)
    if not UTC.fullmatch(timestamp):
        raise RegistryError("need.created_at_utc must be canonical whole-second UTC")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise RegistryError("need.created_at_utc is not a calendar instant") from exc
    if not isinstance(need["revision"], int) or isinstance(need["revision"], bool) or need["revision"] < 1:
        raise RegistryError("need.revision must be an integer of at least 1")
    return need


def default_constraints() -> dict[str, Any]:
    return {
        "acceptable_app_kinds": sorted(APP_KINDS),
        "required_interfaces": [],
        "minimum_publisher_verification": "verified",
        "acceptable_publication_states": ["published"],
        "require_verified_artifact": True,
        "require_not_revoked": True,
        "embedding_consent": "not-granted",
    }


def validate_constraints(value: Any) -> dict[str, Any]:
    value = exact_keys(value, set(default_constraints()), "constraints")
    kinds = unique_strings(value["acceptable_app_kinds"], "acceptable_app_kinds", 3, 16)
    if not kinds or not set(kinds) <= APP_KINDS:
        raise RegistryError("acceptable_app_kinds must contain supported kinds")
    unique_strings(value["required_interfaces"], "required_interfaces", 32, 200)
    if value["minimum_publisher_verification"] != "verified":
        raise RegistryError("only verified publishers are supported")
    states = unique_strings(value["acceptable_publication_states"], "acceptable_publication_states", 1, 32)
    if states != ["published"]:
        raise RegistryError("only published candidates are supported")
    if value["require_verified_artifact"] is not True or value["require_not_revoked"] is not True:
        raise RegistryError("verification and revocation checks cannot be relaxed")
    if value["embedding_consent"] not in {"granted", "not-granted"}:
        raise RegistryError("embedding_consent is invalid")
    return value


def interface_name(short_name: str) -> str:
    return f"{INTERFACE_PREFIX}{short_name}{INTERFACE_SUFFIX}"


def build_records() -> list[dict[str, Any]]:
    catalog = exact_keys(load_json(CATALOG_PATH, MAX_CATALOG_BYTES), {"catalog_version", "status", "apps"}, "catalog")
    if catalog["status"] != STATUS:
        raise RegistryError("catalog must remain experimental-product-hold")
    apps = catalog["apps"]
    if not isinstance(apps, list) or not 1 <= len(apps) <= MAX_SCAN_RECORDS:
        raise RegistryError(f"catalog apps must contain 1..{MAX_SCAN_RECORDS} entries")
    records: list[dict[str, Any]] = []
    for index, app in enumerate(apps):
        app = exact_keys(app, {"app_id", "version", "kind", "display_name", "summary", "wit_world", "profiles", "platforms", "tags", "capability_labels"}, f"catalog.apps[{index}]")
        if app["kind"] not in APP_KINDS or app["wit_world"] not in WORLDS:
            raise RegistryError(f"catalog.apps[{index}] has invalid kind or WIT world")
        permissions = [{
            "interface": interface_name(short),
            "necessity": "degradable" if short == "http" else "required",
            "scope_digest_sha256": sha256(f"{app['app_id']}:scope:{short}".encode()),
            "summary": f"Synthetic fixture declaration for {short}.",
        } for short in WORLDS[app["wit_world"]]]
        package_digest = sha256(f"{app['app_id']}:{app['version']}:package".encode())
        records.append({
            "record_id": f"registry.fixture.{index + 1}",
            "app": {
                "id": app["app_id"], "version": app["version"], "kind": app["kind"],
                "display_name": app["display_name"], "summary": app["summary"],
                "publisher": {"publisher_id": "fixture.vibapp", "verification_state": "verified"},
            },
            "package": {"app_id": app["app_id"], "version": app["version"], "package_digest_sha256": package_digest},
            "contract": {"package_format": PACKAGE_FORMAT, "component_contract": COMPONENT_CONTRACT, "wasi": WASI, "wit_world": app["wit_world"]},
            "compatibility": {"platforms": app["platforms"], "profiles": app["profiles"]},
            "permissions": permissions,
            "source": {"visibility": "public", "license_spdx": "Apache-2.0"},
            "verification": {"status": "verified", "revocation": "not-revoked", "evidence_id": f"fixture.verify.{index + 1}"},
            "publication": {"state": "published"},
            "search_metadata": {"tags": app["tags"], "capability_labels": app["capability_labels"]},
        })
    app_ids = [record["app"]["id"] for record in records]
    if len(app_ids) != len(set(app_ids)):
        raise RegistryError("catalog app IDs must be unique")
    return records


def tokens(text: str) -> set[str]:
    return {token for token in TOKEN.findall(text.casefold()) if token not in STOP_WORDS}


def candidate_fields(record: dict[str, Any]) -> list[tuple[str, str]]:
    fields = [
        ("/app/display_name", record["app"]["display_name"]),
        ("/app/summary", record["app"]["summary"]),
    ]
    fields.extend((f"/search_metadata/tags/{index}", value) for index, value in enumerate(record["search_metadata"]["tags"]))
    fields.extend((f"/search_metadata/capability_labels/{index}", value) for index, value in enumerate(record["search_metadata"]["capability_labels"]))
    return fields


def candidate_document(record: dict[str, Any]) -> str:
    return "\n".join(text for _, text in candidate_fields(record))


def requirement_document(requirement: dict[str, Any]) -> str:
    return "\n".join([requirement["text"]] + requirement["acceptance_examples"])


def need_document(need: dict[str, Any]) -> str:
    return "\n".join([need["goal"]] + [requirement_document(item) for item in need["requirements"]])


def hard_filter(need: dict[str, Any], constraints: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    facts: list[str] = []

    def check(condition: bool, reason: str) -> None:
        if not condition and reason not in reasons:
            reasons.append(reason)

    contract = record["contract"]
    facts.extend([f"/contract/package_format={contract['package_format']}", f"/contract/component_contract={contract['component_contract']}", f"/contract/wasi={contract['wasi']}", f"/contract/wit_world={contract['wit_world']}"])
    check(contract["package_format"] == PACKAGE_FORMAT, "package-format")
    check(contract["component_contract"] == COMPONENT_CONTRACT, "contract")
    check(contract["wasi"] == WASI, "wasi")
    check(contract["wit_world"] in WORLDS, "wit-world")

    candidate_platforms = {(item["os"], item["arch"], item["profile"]) for item in record["compatibility"]["platforms"]}
    need_platforms = {(item["os"], item["arch"], item["profile"]) for item in need["platforms"]}
    facts.extend(["/compatibility/platforms", "/compatibility/profiles", f"/app/kind={record['app']['kind']}"])
    check(need_platforms <= candidate_platforms, "platform")
    check(set(need["profiles"]) <= set(record["compatibility"]["profiles"]), "profile")
    check(record["app"]["kind"] in constraints["acceptable_app_kinds"], "app-kind")

    interfaces = {item["interface"] for item in record["permissions"]}
    scopes = {item["scope_digest_sha256"] for item in record["permissions"]}
    ceiling = need["permission_ceiling"]
    facts.extend(["/permissions", "/need/permission_ceiling", "/constraints/required_interfaces"])
    check(set(constraints["required_interfaces"]) <= interfaces, "required-capability")
    check(interfaces <= set(ceiling["allowed_interfaces"]) and not interfaces & set(ceiling["forbidden_interfaces"]), "permission-ceiling")
    check(not ceiling["maximum_scope_digests"] or scopes <= set(ceiling["maximum_scope_digests"]), "permission-ceiling")

    publisher = record["app"]["publisher"]
    verification = record["verification"]
    publication = record["publication"]
    facts.extend([f"/app/publisher/verification_state={publisher['verification_state']}", f"/verification/status={verification['status']}", f"/verification/revocation={verification['revocation']}", f"/publication/state={publication['state']}", f"/source/visibility={record['source']['visibility']}"])
    check(publisher["verification_state"] == "verified" and record["source"]["visibility"] == "public", "publisher-trust")
    check(verification["status"] == "verified", "verification")
    check(verification["revocation"] == "not-revoked", "revocation")
    check(publication["state"] in constraints["acceptable_publication_states"], "publication-state")

    http_interface = interface_name("http")
    facts.append(f"/need/privacy_requirement={need['privacy_requirement']}")
    check(not (need["privacy_requirement"] == "local-private" and http_interface in interfaces), "privacy")
    searchable = " ".join([record["app"]["id"], publisher["publisher_id"]] + record["search_metadata"]["tags"] + record["search_metadata"]["capability_labels"]).casefold()
    for constraint in need["negative_constraints"]:
        kind, value = constraint["kind"], constraint["value"].casefold()
        violated = (
            (kind == "forbidden-capability" and (value in {item.casefold() for item in interfaces} or value in {item.casefold() for item in record["search_metadata"]["capability_labels"]}))
            or (kind == "forbidden-publisher" and value == publisher["publisher_id"].casefold())
            or (kind == "forbidden-package" and value in {record["app"]["id"].casefold(), record["package"]["package_digest_sha256"]})
            or (kind == "platform" and any(value in item for item in candidate_platforms))
            or (kind == "profile" and value in {item.casefold() for item in record["compatibility"]["profiles"]})
            or (kind == "privacy" and value in {"network", "remote-processing", "http"} and http_interface in interfaces)
            or (kind == "other" and value in searchable.split())
        )
        if violated:
            check(False, "negative-constraint")
            facts.append(f"/need/negative_constraints/{constraint['constraint_id']}")
    ordered = [reason for reason in REJECTION_ORDER if reason in reasons]
    return {"eligible": not ordered, "rejected_reasons": ordered, "checked_facts": facts[:128]}


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def private_http_host(host: str | None) -> bool:
    if host in {"localhost", "127.0.0.1", "::1", "192.168.199.170"}:
        return True
    if not host:
        return False
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".", 2)[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


def embedding_endpoint(base_url: str) -> str:
    base_url = bounded_text(base_url, "embedding base_url", 512).rstrip("/")
    if any(char.isspace() for char in base_url):
        raise RegistryError("embedding base_url contains whitespace")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RegistryError("embedding base_url must use http or https without embedded credentials")
    if parsed.query or parsed.fragment:
        raise RegistryError("embedding base_url cannot contain a query or fragment")
    if parsed.scheme == "http" and not private_http_host(parsed.hostname):
        raise RegistryError("remote embedding endpoints must use HTTPS; HTTP is limited to private LAN or loopback")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/embeddings"):
        endpoint_path = path
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/embeddings"
    else:
        endpoint_path = f"{path}/v1/embeddings"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", ""))


def embedding_config(value: Any) -> dict[str, Any]:
    value = exact_keys(
        value,
        {"enabled", "base_url", "model", "dimensions", "timeout_seconds", "api_key"},
        "embedding config",
    )
    enabled = value["enabled"]
    timeout = value["timeout_seconds"]
    if not isinstance(enabled, bool):
        raise RegistryError("embedding enabled must be boolean")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 30:
        raise RegistryError("embedding timeout_seconds must be between 1 and 30")
    return {
        "enabled": enabled,
        "url": embedding_endpoint(value["base_url"]),
        "model": bounded_text(value["model"], "embedding model", 256),
        "dimensions": value["dimensions"],
        "timeout": float(timeout),
        "api_key": value["api_key"],
    }


class LocalAIEmbedder:
    def __init__(
        self,
        url: str = EMBEDDING_URL,
        timeout: float = EMBEDDING_TIMEOUT_SECONDS,
        *,
        model: str = EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        api_key: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.url = embedding_endpoint(url)
        self.timeout = max(0.25, min(float(timeout), 30.0))
        self.model = bounded_text(model, "embedding model", 256)
        if not isinstance(dimensions, int) or isinstance(dimensions, bool) or not 1 <= dimensions <= 8192:
            raise RegistryError("embedding dimensions must be between 1 and 8192")
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key or len(api_key) > 8192 or any(char.isspace() for char in api_key)
        ):
            raise RegistryError("embedding API key is invalid")
        if not isinstance(enabled, bool):
            raise RegistryError("embedding enabled must be boolean")
        self.dimensions = dimensions
        self.api_key = api_key
        self.enabled = enabled
        self.provider_name = (
            f"localai:{self.model}"
            if self.url == EMBEDDING_URL and self.model == EMBEDDING_MODEL
            else f"openai-compatible-byom:{self.model}"
        )
        self._semaphore = threading.BoundedSemaphore(MAX_EMBEDDING_CONCURRENCY)
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._opener = urllib.request.build_opener(NoRedirect())

    def _uncached(self, texts: list[str]) -> list[list[float]]:
        if not self.enabled:
            raise EmbeddingError("configured embedding provider is disabled")
        payload = canonical_bytes({"model": self.model, "input": texts})
        if len(payload) > MAX_EMBEDDING_REQUEST_BYTES:
            raise EmbeddingError("embedding request exceeds the byte limit")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        if not self._semaphore.acquire(blocking=False):
            raise EmbeddingError("embedding provider concurrency limit reached")
        try:
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                    if content_type != "application/json":
                        raise EmbeddingError("embedding response Content-Type is invalid")
                    header = response.headers.get("Content-Length")
                    if header is not None and (not header.isdigit() or int(header) > MAX_EMBEDDING_RESPONSE_BYTES):
                        raise EmbeddingError("embedding response exceeds the byte limit")
                    raw = response.read(MAX_EMBEDDING_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_EMBEDDING_RESPONSE_BYTES:
                        raise EmbeddingError("embedding response exceeds the byte limit")
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, OSError) as exc:
                raise EmbeddingError(f"embedding provider unavailable: {type(exc).__name__}") from exc
            decoded = decode_json(raw, MAX_EMBEDDING_RESPONSE_BYTES)
            if not isinstance(decoded, dict) or not isinstance(decoded.get("data"), list):
                raise EmbeddingError("embedding response shape is invalid")
            indexed: dict[int, list[float]] = {}
            for position, item in enumerate(decoded["data"]):
                if not isinstance(item, dict):
                    raise EmbeddingError("embedding item is invalid")
                index = item.get("index", position)
                vector = item.get("embedding")
                if not isinstance(index, int) or isinstance(index, bool) or index in indexed:
                    raise EmbeddingError("embedding item index is invalid")
                if not isinstance(vector, list) or len(vector) != self.dimensions:
                    raise EmbeddingError("embedding dimensions are invalid")
                clean: list[float] = []
                for value in vector:
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                        raise EmbeddingError("embedding contains a non-finite value")
                    clean.append(float(value))
                indexed[index] = clean
            if set(indexed) != set(range(len(texts))):
                raise EmbeddingError("embedding response count or ordering is invalid")
            return [indexed[index] for index in range(len(texts))]
        except RegistryError as exc:
            raise EmbeddingError(str(exc)) from exc
        finally:
            self._semaphore.release()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not 1 <= len(texts) <= MAX_EMBEDDING_INPUTS:
            raise EmbeddingError(f"embedding input count must be 1..{MAX_EMBEDDING_INPUTS}")
        if any(not isinstance(text, str) or not text or len(text) > MAX_EMBEDDING_TEXT_CHARS for text in texts):
            raise EmbeddingError("embedding text is empty or exceeds its character limit")
        if sum(len(text) for text in texts) > MAX_EMBEDDING_TOTAL_CHARS:
            raise EmbeddingError("embedding text total exceeds its character limit")
        keys = [sha256(text.encode("utf-8")) for text in texts]
        found: dict[str, list[float]] = {}
        with self._cache_lock:
            for key in keys:
                if key in self._cache:
                    found[key] = self._cache[key]
                    self._cache.move_to_end(key)
        missing_keys: list[str] = []
        missing_texts: list[str] = []
        for key, text in zip(keys, texts):
            if key not in found and key not in missing_keys:
                missing_keys.append(key)
                missing_texts.append(text)
        if missing_texts:
            fresh = self._uncached(missing_texts)
            with self._cache_lock:
                for key, vector in zip(missing_keys, fresh):
                    self._cache[key] = vector
                    found[key] = vector
                    self._cache.move_to_end(key)
                while len(self._cache) > MAX_CACHE_VECTORS:
                    self._cache.popitem(last=False)
        return [found[key] for key in keys]


def refinement_result(
    need: dict[str, Any],
    rejected: list[dict[str, Any]],
    reason_code: str,
    message: str,
    embedding_status: str,
    embedder: Embedder,
    *,
    eligible_count: int = 0,
) -> dict[str, Any]:
    return bounded_result({
        "schema_version": "vibapp.registry-route.experimental.2026-08-24.1",
        "status": STATUS,
        "route": "refinement",
        "request_id": f"registry.{sha256(need)[:24]}",
        "need_id": need["need_id"],
        "recommendations": [],
        "refinement": {"reason_code": reason_code, "message": message},
        "retrieval": {
            "mode": "hard-filter-then-keyword-plus-embedding",
            "embedding": {
                "status": embedding_status,
                "provider": embedder.provider_name,
                "model": embedder.model,
                "dimensions": embedder.dimensions,
            },
            "eligible_candidates": eligible_count,
            "rejected_candidates": len(rejected),
        },
        "rejected": rejected,
        "codeagent_handoff": {"created": False, "permitted": False, "reason": "Registry only recommends or requests refinement."},
    })


def bounded_result(value: dict[str, Any]) -> dict[str, Any]:
    if len(canonical_bytes(value)) > MAX_OUTPUT_BYTES:
        raise RegistryError(f"output exceeds {MAX_OUTPUT_BYTES} bytes")
    return value


def search(need: dict[str, Any], constraints: dict[str, Any], records: list[dict[str, Any]], embedder: Embedder, limit: int) -> dict[str, Any]:
    validate_need(need)
    validate_constraints(constraints)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RETURNED_MATCHES:
        raise RegistryError(f"limit must be between 1 and {MAX_RETURNED_MATCHES}")
    if len(records) > MAX_SCAN_RECORDS:
        raise RegistryError(f"corpus exceeds {MAX_SCAN_RECORDS} records")
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejected: list[dict[str, Any]] = []
    for record in records:
        hard = hard_filter(need, constraints, record)
        if hard["eligible"]:
            eligible.append((record, hard))
        else:
            rejected.append({
                "app_id": record["app"]["id"], "display_name": record["app"]["display_name"],
                "package_digest_sha256": record["package"]["package_digest_sha256"],
                "rejected_reasons": hard["rejected_reasons"], "checked_facts": hard["checked_facts"],
            })
    if not eligible:
        return refinement_result(need, rejected, "no-hard-filter-match", "No candidate passed every platform, kind, capability, permission, publication and trust constraint.", "not-needed", embedder)
    if constraints["embedding_consent"] != "granted":
        return refinement_result(need, rejected, "embedding-consent-required", "Semantic retrieval requires explicit remote embedding consent; no candidate was recommended.", "not-called", embedder, eligible_count=len(eligible))

    overall_text = need_document(need)
    requirement_texts = [requirement_document(item) for item in need["requirements"]]
    candidate_texts = [candidate_document(record) for record, _ in eligible]
    texts = [overall_text] + requirement_texts + candidate_texts
    try:
        vectors = embedder.embed(texts)
    except EmbeddingError as exc:
        return refinement_result(need, rejected, "embedding-provider-unavailable", str(exc), "degraded", embedder, eligible_count=len(eligible))
    if len(vectors) != len(texts):
        return refinement_result(need, rejected, "embedding-provider-invalid", "Embedding provider returned the wrong number of vectors.", "degraded", embedder, eligible_count=len(eligible))

    overall_vector = vectors[0]
    requirement_vectors = vectors[1:1 + len(requirement_texts)]
    candidate_vectors = vectors[1 + len(requirement_texts):]
    need_tokens = tokens(overall_text)
    ranked: list[dict[str, Any]] = []
    for (record, hard), candidate_vector in zip(eligible, candidate_vectors):
        fields = candidate_fields(record)
        candidate_tokens = set().union(*(tokens(text) for _, text in fields))
        lexical_score = len(need_tokens & candidate_tokens) / max(1, len(need_tokens))
        vector_score = cosine(overall_vector, candidate_vector)
        hybrid_score = 0.40 * lexical_score + 0.60 * max(0.0, vector_score)
        coverage: list[dict[str, Any]] = []
        must_have_complete = True
        for index, (requirement, requirement_vector) in enumerate(zip(need["requirements"], requirement_vectors)):
            req_tokens = tokens(requirement_texts[index])
            overlap = req_tokens & candidate_tokens
            lexical_coverage = len(overlap) / max(1, len(req_tokens))
            semantic_coverage = cosine(requirement_vector, candidate_vector)
            covered = lexical_coverage >= 0.30 or semantic_coverage >= MINIMUM_REQUIREMENT_SIMILARITY
            if requirement["priority"] == "must-have" and not covered:
                must_have_complete = False
            evidence_fields = [path for path, text in fields if tokens(text) & overlap]
            if not evidence_fields and covered:
                evidence_fields = ["/app/summary", "/search_metadata/tags"]
            coverage.append({
                "requirement_id": requirement["requirement_id"],
                "state": "covered" if covered else "uncovered",
                "lexical_score": round(lexical_coverage, 6),
                "semantic_score": round(semantic_coverage, 6),
                "evidence_fields": evidence_fields[:16],
            })
        acceptable = must_have_complete and hybrid_score >= MINIMUM_HYBRID_SCORE
        ranked.append({
            "app": record["app"],
            "package": record["package"],
            "decision": "acceptable" if acceptable else "insufficient-match",
            "scores": {"keyword": round(lexical_score, 6), "embedding_cosine": round(vector_score, 6), "hybrid": round(hybrid_score, 6)},
            "coverage": coverage,
            "required_permissions": record["permissions"],
            "explanation": {
                "summary": "Candidate passed every hard filter; ranking used keyword overlap and a real embedding cosine score.",
                "evidence_fields": sorted(set(hard["checked_facts"] + ["/app/display_name", "/app/summary", "/search_metadata/tags", "/search_metadata/capability_labels"]))[:128],
                "embedding_input_digest_sha256": sha256(candidate_document(record).encode("utf-8")),
            },
        })
    ranked.sort(key=lambda item: (0 if item["decision"] == "acceptable" else 1, -item["scores"]["hybrid"], item["app"]["id"], item["package"]["package_digest_sha256"]))
    acceptable = [item for item in ranked if item["decision"] == "acceptable"][:limit]
    if not acceptable:
        result = refinement_result(need, rejected, "no-acceptable-semantic-match", "Candidates passed hard filters but none met must-have coverage and the hybrid relevance threshold.", "live", embedder, eligible_count=len(eligible))
        result["considered"] = ranked[:limit]
        return bounded_result(result)
    return bounded_result({
        "schema_version": "vibapp.registry-route.experimental.2026-08-24.1",
        "status": STATUS,
        "route": "recommendation",
        "request_id": f"registry.{sha256(need)[:24]}",
        "need_id": need["need_id"],
        "recommendations": acceptable,
        "refinement": None,
        "retrieval": {
            "mode": "hard-filter-then-keyword-plus-embedding",
            "embedding": {"status": "live", "provider": embedder.provider_name, "model": embedder.model, "dimensions": embedder.dimensions},
            "eligible_candidates": len(eligible), "rejected_candidates": len(rejected),
            "thresholds": {"hybrid": MINIMUM_HYBRID_SCORE, "requirement_semantic": MINIMUM_REQUIREMENT_SIMILARITY},
        },
        "rejected": rejected,
        "codeagent_handoff": {"created": False, "permitted": False, "reason": "An acceptable existing application must be offered before development."},
    })


def parse_query(value: Any) -> tuple[dict[str, Any], dict[str, Any], int]:
    value = exact_keys(value, {"need", "constraints", "limit"}, "query")
    need = validate_need(value["need"])
    constraints = validate_constraints(value["constraints"])
    limit = value["limit"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RETURNED_MATCHES:
        raise RegistryError(f"limit must be between 1 and {MAX_RETURNED_MATCHES}")
    return need, constraints, limit


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], records: list[dict[str, Any]], embedder: Embedder) -> None:
        super().__init__(address, RegistryHandler)
        self.records = records
        self.embedder = embedder
        self.request_slots = threading.BoundedSemaphore(MAX_HTTP_CONCURRENCY)


class RegistryHandler(BaseHTTPRequestHandler):
    server: BoundedServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(2.0)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"registry_http {self.client_address[0]} {fmt % args}\n")

    def _send(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        payload = canonical_bytes(value) + b"\n"
        if len(payload) > MAX_OUTPUT_BYTES:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            payload = b'{"error":{"code":"resource-limit","message":"response exceeds output limit"}}\n'
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self._send(HTTPStatus.NOT_FOUND, {"error": {"code": "not-found", "message": "unknown endpoint"}})
            return
        self._send(HTTPStatus.OK, {"status": STATUS, "service": "vibapp-registry", "records": len(self.server.records), "embedding_provider": self.server.embedder.provider_name})

    def do_POST(self) -> None:
        if self.path != "/v1/registry/search":
            self._send(HTTPStatus.NOT_FOUND, {"error": {"code": "not-found", "message": "unknown endpoint"}})
            return
        if not self.server.request_slots.acquire(blocking=False):
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": {"code": "busy", "message": "request concurrency limit reached"}})
            return
        try:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            length_header = self.headers.get("Content-Length")
            if content_type != "application/json" or length_header is None or not length_header.isdigit():
                raise RegistryError("Content-Type application/json and numeric Content-Length are required")
            length = int(length_header)
            if length > MAX_REQUEST_BYTES:
                raise RegistryError(f"input exceeds {MAX_REQUEST_BYTES} bytes")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RegistryError("request body is truncated")
            need, constraints, limit = parse_query(decode_json(raw, MAX_REQUEST_BYTES))
            self._send(HTTPStatus.OK, search(need, constraints, self.server.records, self.server.embedder, limit))
        except RegistryError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid-argument", "message": str(exc), "retryable": False}})
        except (socket.timeout, TimeoutError):
            self._send(HTTPStatus.REQUEST_TIMEOUT, {"error": {"code": "deadline-exceeded", "message": "request body deadline exceeded", "retryable": True}})
        finally:
            self.server.request_slots.release()


def emit(value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_OUTPUT_BYTES:
        raise RegistryError(f"output exceeds {MAX_OUTPUT_BYTES} bytes")
    sys.stdout.buffer.write(payload)


def cli_constraints(args: argparse.Namespace) -> dict[str, Any]:
    value = default_constraints()
    if getattr(args, "kind", None):
        value["acceptable_app_kinds"] = sorted(set(args.kind))
    if getattr(args, "required_interface", None):
        value["required_interfaces"] = sorted(set(args.required_interface))
    if getattr(args, "embedding_consent", False):
        value["embedding_consent"] = "granted"
    return validate_constraints(value)


def cli_embedder(args: argparse.Namespace) -> LocalAIEmbedder:
    if getattr(args, "embedding_config_stdin", False):
        raw = sys.stdin.buffer.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            raise RegistryError("embedding config exceeds 32 KiB")
        config = embedding_config(decode_json(raw, 32 * 1024))
        return LocalAIEmbedder(**config)
    return LocalAIEmbedder(getattr(args, "embedding_url", EMBEDDING_URL))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search_parser = subparsers.add_parser("search", help="Run retrieval-first search")
    search_parser.add_argument("--need", required=True)
    search_parser.add_argument("--limit", type=int, default=5)
    search_parser.add_argument("--kind", action="append", choices=sorted(APP_KINDS))
    search_parser.add_argument("--required-interface", action="append")
    search_parser.add_argument("--embedding-consent", action="store_true")
    search_parser.add_argument("--embedding-url", default=EMBEDDING_URL, choices=[EMBEDDING_URL, "http://127.0.0.1:8081/v1/embeddings"])
    search_parser.add_argument("--embedding-config-stdin", action="store_true")
    search_parser.add_argument("--synthetic-fixtures", action="store_true", help="Explicit test-only corpus; not application availability")
    validate_parser = subparsers.add_parser("validate-need")
    validate_parser.add_argument("--need", required=True)
    serve_parser = subparsers.add_parser("serve", help="Serve loopback-only HTTP")
    serve_parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--synthetic-fixtures", action="store_true", help="Explicit test-only corpus; never use for product recommendations")
    serve_parser.add_argument("--embedding-url", default=EMBEDDING_URL, choices=[EMBEDDING_URL, "http://127.0.0.1:8081/v1/embeddings"])
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-need":
            need = validate_need(load_json(Path(args.need), MAX_NEED_BYTES))
            emit({"status": STATUS, "valid": True, "need_id": need["need_id"], "query_digest_sha256": sha256(need)})
            return 0
        # The conformance catalog has no executable packages or real publication
        # receipts. An empty product corpus must honestly route to refinement.
        records = build_records() if args.synthetic_fixtures else []
        if args.command == "search":
            need = validate_need(load_json(Path(args.need), MAX_NEED_BYTES))
            embedder = cli_embedder(args)
            emit(search(need, cli_constraints(args), records, embedder, args.limit))
            return 0
        if not 1 <= args.port <= 65535:
            raise RegistryError("port must be between 1 and 65535")
        server = BoundedServer((args.host, args.port), records, LocalAIEmbedder(args.embedding_url))
        sys.stderr.write(f"vibapp-registry listening on http://{args.host}:{args.port}\n")
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except (RegistryError, EmbeddingError) as exc:
        emit({"status": STATUS, "error": {"code": "invalid-argument", "message": str(exc), "retryable": False}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
