#!/usr/bin/env python3
"""Consent-gated, bounded cloud CodeAgent source worker.

This worker may ask Codex to author Rust/WASI source.  It deliberately has no build,
verification, installation, signing, or publication authority.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any, Protocol
from urllib.parse import urlsplit


BASE = Path(__file__).resolve().parent
REPO = BASE.parents[1]
CONTRACT = REPO / "wit/experimental-v0/contract.wit"
TASK_SCHEMA = BASE / "schemas/cloud-codeagent-task.schema.json"
LEGACY_TASK_SCHEMA = BASE / "schemas/cloud-codeagent-task.experimental-v2.schema.json"
PROVIDER_SCHEMA = BASE / "schemas/provider-result.schema.json"
HANDOFF_SCHEMA = BASE / "schemas/source-handoff.schema.json"
DRY_RUN_SOURCE = BASE / "fixtures/dry-run/source"
DRY_RUN_RESULT = BASE / "fixtures/dry-run/provider-result.json"
CODEX_BIN = Path("/opt/homebrew/bin/codex")

TASK_SCHEMA_VERSION = "vibapp.cloud-codeagent-task.experimental-v3"
LEGACY_TASK_SCHEMA_VERSION = "vibapp.cloud-codeagent-task.experimental-v2"
POLICY_VERSION = "vibapp.cloud-codeagent-policy.experimental-v7"
LEGACY_POLICY_VERSION = "vibapp.cloud-codeagent-policy.experimental-v6"
PROVIDER_REQUEST_VERSION = "vibapp.cloud-codeagent-provider-request.experimental-v7"
LEGACY_PROVIDER_REQUEST_VERSION = "vibapp.cloud-codeagent-provider-request.experimental-v6"
CONSENT_CLAIM_VERSION = "vibapp.cloud-codeagent-consent-claim.experimental-v7"
SOURCE_HANDOFF_VERSION = "vibapp.codeagent-source-handoff.experimental-v2"
CARGO_SCAFFOLD_POLICY_VERSION = "vibapp.cloud-codeagent-cargo-scaffold.experimental-v1"
CONSENT_TTL_SECONDS = 3600
CONTRACT_DIGEST_PIN = "ec94f06652886cf9eea0aba8afc028165b135b91103faccf62a2ccc6828d3c38"
PROVIDER_INSTRUCTIONS = """You are the source-authoring CodeAgent for one VibApp job.

Hard boundary:
- Author Rust source only below source/. Do not compile, test, fetch dependencies, install, verify, sign, publish, or run generated code.
- Do not modify contracts/contract.wit, source/wit/contract.wit, or the pre-seeded source/Cargo.toml.
- Target the exact WIT world and imports in provider_request. Do not add ambient filesystem, network, environment, process, credential, device, or native-library authority.
- source/Cargo.toml is a read-only deterministic policy scaffold already derived from package_intent and bound by provider_request.cargo_scaffold. Do not edit, remove, chmod, or recreate it. Its closed package/lib/dependencies shape fixes edition 2024, publish=false, all Cargo auto-targets=false, crate-type cdylib, and only exact wit-bindgen =0.60.0 with default features disabled and features bitflags, macro-string, macros, realloc (never std).
- src/lib.rs must use #![no_std], extern crate alloc, an explicit bounded global allocator, and an explicit panic handler. Do not use or link std; it introduces ambient WASI CLI/I/O imports that the independent Verifier rejects.
- Implement the allocator over wasm linear memory beginning at __heap_base with AtomicUsize plus core::arch::wasm32::memory_size/memory_grow. Do not allocate a static byte-array heap or use UnsafeCell as heap storage. Export cabi_realloc with #[unsafe(no_mangle)] and satisfy any compiler-lowered memory primitive locally; in particular define a no_std #[unsafe(no_mangle)] C-ABI memcmp implementation so the component has no env::memcmp import.
- The frozen Stage 0 world requires the component to retain every interface in target.required_imports even when product behavior does not otherwise call it. Define an exported __vibapp_force_declared_imports function which makes one inert, bounded typed call through every required import (following the exact WIT signatures); the host never calls this linker-retention export automatically. Do not omit declared imports as unused and do not add any interface outside target.required_imports.
- With wit-bindgen 0.60, ErrorCode and all common-owned types live at vibapp::experimental_v0::common; never use exports::vibapp::experimental_v0::guest::ErrorCode or any guest::ErrorCode path.
- Host behavior: non-sensitive FieldKind::Text is an expanding multiline editor. Preserve embedded and leading newlines in FieldValue::Text and KV bytes; do not trim note bodies. No new WIT field kind is needed.
- The foreground Client refreshes active bound surfaces about once per second by delivering LauncherEvent::Open with reason Restore. Re-render clocks on Open as well as Launch and Action. Calculate elapsed time from host clock differences, never event counts; do not claim that only manual refresh is available. Closed or hidden views do not imply reliable background execution.
- clock::monotonic_now is relative to the current guest instance, not a durable epoch. Never persist its anchor in KV or compare it across close/reopen, process restart, or replacement instances. Persist elapsed duration plus a wall-clock anchor for running-state recovery; any monotonic anchor must stay volatile and be rebased from that durable state on a fresh instance. Comparing new_monotonic >= saved_monotonic cannot detect restarts and can reset the stopwatch when Pause is pressed after reopening.
- KV optimistic concurrency: use the exact revision returned by kv::get for an existing key, and expected_revision: None when no entry exists. Some(0) is not a create-if-absent sentinel and will fail with stale-revision. Do not replace an absent revision with zero. Use the transaction result's state_revision when retaining a revision after writing.
- Do not create Cargo.lock, build.rs, target/, .cargo/, native C/C++/assembly, archives, binaries, executable files, target-specific dependencies, or out-of-tree paths.
- Source may contain the pre-seeded Cargo.toml and wit/contract.wit, plus authored src/**/*.rs, tests/**/*.rs, README.md, and manifest.intent.json only.
- Finish the smallest complete source tree in this one authoring pass. Compilation, tests, and independent verification intentionally belong to later Builder and Verifier stages.
- You MUST physically create source/src/lib.rs with the built-in apply_patch editing tool before the final response. Returning only the result JSON, or merely listing files that do not exist, is invalid. source/Cargo.toml and source/wit/contract.wit are already present and must not be rewritten.
- Your final response must exactly satisfy the supplied output schema. List every file below source/ relative to source/, including wit/contract.wit. unresolved is only for a concrete missing source requirement you could not author. Do not list lack of compilation/testing, inability to use a shell, uncertainty, or pending Builder/Verifier work as unresolved; use an empty array when the authored source is complete by static inspection and ready to hand off.
- You may use the shell only for read-only inspection inside the current workspace with pwd, ls, find, sed, or rg. Use apply_patch for source edits. Do not invoke any compiler, test runner, formatter, package manager, network client, executable from the authored source, other subprocess, MCP server, plugin, skill, hook, subagent, or any path outside this workspace.
- Before returning, write the exact vibapp.provider-result.experimental-v1 JSON result to provider-last-message.json at the workspace root. The CLI also captures the final structured response, but that does not replace physically authoring the source files first.
"""
INSTRUCTIONS_DIGEST = hashlib.sha256(PROVIDER_INSTRUCTIONS.encode("utf-8")).hexdigest()
SUPPORTED_TASK_PROVIDERS = (
    "openai-codex",
    "anthropic-claude-code",
    "opencode",
    "google-gemini-cli",
)
# Each new task binds the current instruction bytes. Other reviewed
# adapters expose equivalent file-editing tools but not Codex's named
# ``apply_patch`` tool, so their consent binds an otherwise identical prompt with
# only those two tool-specific sentences generalized.
OTHER_PROVIDER_INSTRUCTIONS = PROVIDER_INSTRUCTIONS.replace(
    "with the built-in apply_patch editing tool",
    "with the provider's reviewed file-editing tool",
).replace(
    "Use apply_patch for source edits.",
    "Use only the provider's reviewed file-editing tools for source edits.",
)
OTHER_PROVIDER_INSTRUCTIONS_DIGEST = hashlib.sha256(
    OTHER_PROVIDER_INSTRUCTIONS.encode("utf-8")
).hexdigest()
DISCLOSED_DATA_CLASSES = [
    "need",
    "package-intent",
    "target-contract",
    "execution-limits",
    "generation-policy",
    "authoritative-contract",
    "provider-execution-identity",
]
LEGACY_DISCLOSED_DATA_CLASSES = DISCLOSED_DATA_CLASSES[:-1]
CODEX_PIN = {
    "configured_path": str(CODEX_BIN),
    "resolved_path": "/opt/homebrew/Caskroom/codex/0.149.1/bin/codex",
    "version": "codex-cli 0.149.1",
    "sha256": "f0d8762236594359b60cfbe17f4c7e945a3ce8d1c91e74778838c968d250fb6c",
}
ISOLATION_POLICY_VERSION = "vibapp.provider-runner-isolation.experimental-v1"
RUNNER_REQUEST_VERSION = "vibapp.provider-runner-request.experimental-v2"
RUNNER_RECEIPT_VERSION = "vibapp.provider-runner-receipt.experimental-v3"
RUNNER_ATTESTATION_TYPE = "detached-signature-placeholder-v1"

MAX_TASK_BYTES = 128 * 1024
MAX_PROVIDER_RESULT_BYTES = 256 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RUNNER_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
APP_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UTC = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$")
SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ATTESTATION_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{43,1024}={0,2}$")
ATTEMPT_IDENTIFIER = re.compile(r"^attempt-[0-9]{4}-[0-9a-f]{16}$")
HTTPS_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
)
PROVIDER_MANAGED_ENDPOINT_PROVIDERS = {
    "openai-codex",
    "anthropic-claude-code",
    "google-gemini-cli",
}

# These bounded lexical classifiers intentionally recognize only product behavior
# that the frozen NeedSpec already states explicitly.  They are a fail-closed
# handoff gate, not a replacement for runtime acceptance tests or a general natural
# language interpreter.
MULTILINE_NEED = re.compile(
    r"\b(?:multi[\s-]?line|textarea|line[\s-]?breaks?|newlines?)\b|多行|换行|多段文本",
    re.IGNORECASE,
)
EXPLICIT_UI_ACTION_NEED = re.compile(
    r"\b(?:click|clicked|clicking|tap|tapped|tapping|press|pressed|pressing|"
    r"submit|submitted|submitting|save|saved|saving|add|added|adding|delete|deleted|"
    r"deleting|remove|removed|removing|"
    r"calculate|calculated|calculating|compute|computed|computing|toggle|toggled|"
    r"toggling|mark|marked|marking|button)\b|"
    r"点击|按下|按钮|提交|保存|添加|新增|删除|移除|计算|切换|勾选|标记",
    re.IGNORECASE,
)
UI_INPUT_NEED = re.compile(
    r"\b(?:input|enter|entered|fill|filled|type|typed|choose|chosen|select|selected|form|field)\b|"
    r"输入|填写|录入|选择|表单|字段",
    re.IGNORECASE,
)
UI_EFFECT_NEED = re.compile(
    r"\b(?:show|shown|display|displayed|render|rendered|calculate|compute|summary|result|"
    r"save|persist|add|delete|remove|toggle|mark)\b|"
    r"显示|渲染|计算|汇总|结果|保存|持久|添加|新增|删除|移除|切换|标记",
    re.IGNORECASE,
)
REMINDER_NEED = re.compile(
    r"\b(?:reminder|alarm|scheduled (?:alert|notification)|"
    r"notify(?: me)? (?:at|after|every|when))\b|提醒|闹钟|定时通知|"
    r"通知.{0,24}(?:时间|工作日|星期|每天|每周)",
    re.IGNORECASE,
)
# Only recognize explicit exclusions with a small object-list grammar. Unknown
# modifiers, predicates, conditions, and double negatives retain the existing
# fail-closed reminder classification; this is not a general prose parser.
_REMINDER_NEGATIVE_EN_OBJECT = (
    r"(?:(?:a|an|any|the)\s+)?"
    r"(?:(?:background|foreground|system|local|remote|scheduled)\s+){0,4}"
    r"(?:timing|alarm|reminder|notification|alert)s?"
    r"(?:\s+(?:feature|service)s?)?"
)
_REMINDER_NEGATIVE_ZH_OBJECT = (
    r"(?:(?:后台|前台|系统|本地|远程|定时|任何|额外|"
    r"(?:关闭|退出)(?:窗口|应用)后(?:的)?)){0,4}"
    r"(?:计时|闹钟|提醒|通知)(?:功能|服务|承诺)?"
)
REMINDER_EXCLUSION = re.compile(
    r"\b(?:no|without|(?:do|does|must|should|will|shall|can)\s+not\s+"
    r"(?:provide|create|support|promise|implement|allow|require)|"
    r"never\s+(?:provide|create|support|promise|implement|allow))\s+"
    + _REMINDER_NEGATIVE_EN_OBJECT
    # An explicit final conjunction distinguishes an object list from a comma
    # splice such as "No alarm, a reminder is required" (positive/ambiguous).
    + r"(?:(?:\s*,\s*" + _REMINDER_NEGATIVE_EN_OBJECT + r"){0,7}"
    r"\s*,?\s+(?:or\s+|and\s+(?!(?:a|an|the)\s+))"
    + _REMINDER_NEGATIVE_EN_OBJECT + r")?"
    r"(?:\s+(?:is|are|will be)\s+(?:provided|created|supported|needed|required|allowed))?"
    r"(?=\s*(?:[.!?;。；\n]|$|,\s*(?:but|however|yet)\b|(?:but|however|yet)\b))"
    r"|(?:不(?:承诺|提供|支持|实现|需要|允许)|禁止|无需|无须|而不是|"
    r"不得(?:创建|提供|实现|设置)?)"
    + _REMINDER_NEGATIVE_ZH_OBJECT
    + r"(?:(?:、|和|或|以及)" + _REMINDER_NEGATIVE_ZH_OBJECT + r"){0,8}"
    r"(?=[。；，,.!?;\n]|$|但是|但|然而|不过|却)"
    r"|\b(?:do|does|must|should|will|shall|can)\s+not\s+"
    r"notify(?: me)? (?:at|after|every|when)\b",
    re.IGNORECASE,
)
REMINDER_EXCLUSION_UNCERTAIN_PREFIX = re.compile(
    r"\b(?:if|unless|when|whenever|until|whether|not|never|cannot|can't)\b|"
    r"如果|假如|若|除非|并非|不是|不能|不可|不得|没有|未必|并不|"
    r"不要|不应|不该|不必|无需|无须|禁止|取消|撤销|解除|不$",
    re.IGNORECASE,
)
# Mixed exclusions may contain arbitrary unrelated objects. Recognize their
# bounded list syntax, not an application/domain vocabulary. Keep uncertain
# predicates as positive evidence instead of extending negation across them.
REMINDER_MIXED_EXCLUSION = re.compile(
    r"\b(?:no|without|(?:do|does|must|should|will|shall|can)\s+not\s+"
    r"(?:provide|create|support|promise|implement|allow|require)|"
    r"never\s+(?:provide|create|support|promise|implement|allow))\s+"
    r"(?P<english>[a-z0-9][a-z0-9 ,_/-]{0,511}?)"
    r"(?=\s*(?:[.!?;。；\n]|$|,\s*(?:but|however|yet)\b|(?:but|however|yet)\b))"
    r"|(?:不(?:承诺|提供|支持|实现|需要|允许)|禁止|无需|无须|而不是|"
    r"不得(?:创建|提供|实现|设置)?)"
    r"(?P<chinese>[\w 、/-]{1,256}?)"
    r"(?=[。；，,.!?;\n]|$|但是|但|然而|不过|却)",
    re.IGNORECASE,
)
REMINDER_LIST_PREDICATE = re.compile(
    r"\b(?:is|are|was|were|must|needs?|needed|requires?|required|should|shall|will|"
    r"can|would|could|may|might|set|create|send|notify|provide|support|promise|allow|"
    r"implement|receive|enable|disable|remove|delete|miss|missed|not|no|never|if|"
    r"unless|when|only|but|however|yet|rather|instead)\b|"
    r"必须|需要|要求|希望|想要|^要|应当|应该|不得|不要|不能|不必|无需|无须|"
    r"禁止|取消|撤销|解除|设置|创建|发送|触发|启用|禁用|删除|关闭|错过|"
    r"但是|但|然而|不过|却|如果|假如|除非|否则|不只是",
    re.IGNORECASE,
)
TIME_CONFIGURATION_NEED = re.compile(
    r"\b(?:choose|select|set|configure|configured|change|adjust|specify|specified)\b"
    r".{0,80}\b(?:time|weekday|day|schedule)\b|"
    r"\b(?:time|weekday|day|schedule)\b.{0,80}"
    r"\b(?:choose|select|set|configure|configured|change|adjust|specify|specified)\b|"
    r"(?:选择|指定|设置|配置|修改|调整).{0,24}(?:时间|工作日|星期|日期|计划)|"
    r"(?:时间|工作日|星期|日期|计划).{0,24}(?:选择|指定|设置|配置|修改|调整)",
    re.IGNORECASE | re.DOTALL,
)
PAUSE_NEED = re.compile(r"\b(?:pause|disable)\b|暂停|禁用", re.IGNORECASE)
RESUME_NEED = re.compile(r"\b(?:resume|enable)\b|恢复|启用", re.IGNORECASE)

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
    "web-preview-reference": [
        "vibapp:experimental-v0/clock@0.0.1",
        "vibapp:experimental-v0/scheduler@0.0.1",
        "vibapp:experimental-v0/kv@0.0.1",
        "vibapp:experimental-v0/log@0.0.1",
        "vibapp:experimental-v0/host-info@0.0.1",
        "vibapp:experimental-v0/settings@0.0.1",
        "vibapp:experimental-v0/system-metrics@0.0.1",
        "vibapp:experimental-v0/http@0.0.1",
    ],
}

WORLD_KIND = {
    "ui-only-reference": "ui",
    "service-only-reference": "service",
    "hybrid-reference": "hybrid",
}
SERVICE_TRIGGER_ORDER = ("on-enable", "scheduler", "manual")


class WorkerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        external_request_attempted: bool = False,
        external_request_observed: bool = False,
        gateway_request_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.external_request_attempted = external_request_attempted
        self.external_request_observed = external_request_observed
        self.gateway_request_id = gateway_request_id


@dataclass(frozen=True)
class ProviderRunnerReceipt:
    schema_version: str
    document_type: str
    job_id: str
    execution_attempt: dict[str, Any]
    immutable_task_digest_sha256: str
    provider_execution_identity_sha256: str
    input_bundle_sha256: str
    output_bundle_sha256: str | None
    runner_id: str
    runner_identity_sha256: str
    isolation_policy_sha256: str
    external_request_attempted: bool
    external_request_observed: bool
    gateway_request_id: str | None
    whole_job_quiescent: bool
    exit_code: int
    stdout_bytes: int
    stderr_bytes: int
    executed_executable_sha256: str | None
    attestation_type: str
    attestation_key_id: str
    receipt_digest_sha256: str
    attestation_signature: str


@dataclass(frozen=True)
class ProviderRunnerRequest:
    schema_version: str
    document_type: str
    job_id: str
    execution_attempt: dict[str, Any]
    immutable_task_digest_sha256: str
    provider_execution_identity: dict[str, Any]
    provider_execution_identity_sha256: str
    provider: str
    model: str
    executable_identity: dict[str, Any]
    command: list[str]
    prompt: bytes
    workspace: Path
    limits: dict[str, int]
    isolation_policy: dict[str, Any]
    isolation_policy_sha256: str
    input_bundle_sha256: str


@dataclass(frozen=True)
class AcceptedRunnerProfile:
    runner_id: str
    runner_identity_sha256: str
    attestation_key_id: str


def validate_accepted_runner_profile(profile: Any) -> AcceptedRunnerProfile:
    """Reject syntactically valid but unprovisioned runner trust material."""
    if type(profile) is not AcceptedRunnerProfile:
        raise WorkerError("runner-attestation-invalid", "accepted runner profile is untyped")
    for value, context in (
        (profile.runner_id, "accepted runner ID"),
        (profile.attestation_key_id, "accepted runner attestation key ID"),
    ):
        if not isinstance(value, str) or not RUNNER_IDENTIFIER.fullmatch(value):
            raise WorkerError("runner-attestation-invalid", f"{context} is invalid")
        compact = re.sub(r"[^a-z0-9]", "", value.casefold())
        if compact in {"unprovisioned", "placeholder", "replaceme", "replacewithrealvalue"} or set(compact) == {"0"}:
            raise WorkerError("runner-attestation-invalid", f"{context} is an unprovisioned sentinel")
    sha256_value(profile.runner_identity_sha256, "accepted_runner.runner_identity_sha256")
    if profile.runner_identity_sha256 == "0" * 64:
        raise WorkerError("runner-attestation-invalid", "accepted runner identity digest is unprovisioned")
    return profile


@dataclass(frozen=True)
class SyntheticProcessHandle:
    pid: int
    uid: int
    start_token: str


class ReceiptSignatureVerifier(Protocol):
    def verify(self, digest: bytes, signature: str, key_id: str) -> bool: ...


class ProviderRunner(Protocol):
    def preflight(self, required_policy: dict[str, Any]) -> dict[str, Any]: ...

    def execute(self, request: ProviderRunnerRequest) -> ProviderRunnerReceipt: ...


class UnavailableProviderRunner:
    def preflight(self, required_policy: dict[str, Any]) -> dict[str, Any]:
        raise WorkerError(
            "external-isolation-unavailable",
            "live mode requires an independently accepted one-job VM/container provider runner",
        )

    def execute(self, request: ProviderRunnerRequest) -> ProviderRunnerReceipt:
        raise WorkerError("external-isolation-unavailable", "provider runner is unavailable")


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise WorkerError("schema-invalid", f"duplicate JSON key: {key}")
        value[key] = item
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def provider_receipt_body(receipt: ProviderRunnerReceipt) -> dict[str, Any]:
    """Return the exact signed receipt body (digest and signature excluded)."""
    return {
        "schema_version": receipt.schema_version,
        "document_type": receipt.document_type,
        "job_id": receipt.job_id,
        "execution_attempt": receipt.execution_attempt,
        "immutable_task_digest_sha256": receipt.immutable_task_digest_sha256,
        "provider_execution_identity_sha256": receipt.provider_execution_identity_sha256,
        "input_bundle_sha256": receipt.input_bundle_sha256,
        "output_bundle_sha256": receipt.output_bundle_sha256,
        "runner_id": receipt.runner_id,
        "runner_identity_sha256": receipt.runner_identity_sha256,
        "isolation_policy_sha256": receipt.isolation_policy_sha256,
        "external_request_attempted": receipt.external_request_attempted,
        "external_request_observed": receipt.external_request_observed,
        "gateway_request_id": receipt.gateway_request_id,
        "whole_job_quiescent": receipt.whole_job_quiescent,
        "exit_code": receipt.exit_code,
        "stdout_bytes": receipt.stdout_bytes,
        "stderr_bytes": receipt.stderr_bytes,
        "executed_executable_sha256": receipt.executed_executable_sha256,
        "attestation_type": receipt.attestation_type,
        "attestation_key_id": receipt.attestation_key_id,
    }


def provider_receipt_digest(receipt: ProviderRunnerReceipt) -> str:
    return sha256_bytes(canonical_json(provider_receipt_body(receipt)))


def validate_provider_receipt(
    receipt: Any,
    request: ProviderRunnerRequest,
    accepted_runner: AcceptedRunnerProfile | None,
    signature_verifier: ReceiptSignatureVerifier | None,
) -> ProviderRunnerReceipt:
    """Validate every receipt field and its detached-attestation placeholder."""
    if type(receipt) is not ProviderRunnerReceipt:
        raise WorkerError("runner-receipt-invalid", "provider runner returned an untyped receipt")
    if accepted_runner is None or signature_verifier is None:
        raise WorkerError("runner-attestation-unavailable", "no accepted runner identity/signature verifier is configured")
    accepted_runner = validate_accepted_runner_profile(accepted_runner)
    if receipt.schema_version != RUNNER_RECEIPT_VERSION or receipt.document_type != "provider-runner-receipt":
        raise WorkerError("runner-receipt-invalid", "runner receipt schema/document type is unsupported")
    identifier(receipt.job_id, "receipt.job_id")
    try:
        validate_attempt(receipt.execution_attempt, "receipt.execution_attempt")
    except WorkerError as error:
        raise WorkerError("runner-receipt-invalid", str(error)) from error
    for value, context in (
        (receipt.runner_id, "receipt.runner_id"),
        (receipt.attestation_key_id, "receipt.attestation_key_id"),
    ):
        if not isinstance(value, str) or not RUNNER_IDENTIFIER.fullmatch(value):
            raise WorkerError("runner-receipt-invalid", f"{context} is not a bounded runner identifier")
    for value, context in (
        (receipt.immutable_task_digest_sha256, "receipt.immutable_task_digest_sha256"),
        (
            receipt.provider_execution_identity_sha256,
            "receipt.provider_execution_identity_sha256",
        ),
        (receipt.input_bundle_sha256, "receipt.input_bundle_sha256"),
        (receipt.runner_identity_sha256, "receipt.runner_identity_sha256"),
        (receipt.isolation_policy_sha256, "receipt.isolation_policy_sha256"),
        (receipt.receipt_digest_sha256, "receipt.receipt_digest_sha256"),
    ):
        sha256_value(value, context)
    for value, context in (
        (receipt.output_bundle_sha256, "receipt.output_bundle_sha256"),
        (receipt.executed_executable_sha256, "receipt.executed_executable_sha256"),
    ):
        if value is not None:
            sha256_value(value, context)
    bool_value(receipt.external_request_attempted, "receipt.external_request_attempted")
    bool_value(receipt.external_request_observed, "receipt.external_request_observed")
    bool_value(receipt.whole_job_quiescent, "receipt.whole_job_quiescent")
    int_range(receipt.exit_code, "receipt.exit_code", 0, 255)
    int_range(receipt.stdout_bytes, "receipt.stdout_bytes", 0, 1024 * 1024)
    int_range(receipt.stderr_bytes, "receipt.stderr_bytes", 0, 1024 * 1024)
    if receipt.gateway_request_id is not None:
        if not isinstance(receipt.gateway_request_id, str) or not RUNNER_IDENTIFIER.fullmatch(receipt.gateway_request_id):
            raise WorkerError("runner-receipt-invalid", "receipt.gateway_request_id is not a bounded runner identifier")
    if receipt.attestation_type != RUNNER_ATTESTATION_TYPE:
        raise WorkerError("runner-receipt-invalid", "runner receipt attestation type is unsupported")
    if (
        not isinstance(receipt.attestation_signature, str)
        or len(receipt.attestation_signature) > 1024
        or not ATTESTATION_SIGNATURE.fullmatch(receipt.attestation_signature)
    ):
        raise WorkerError("runner-receipt-invalid", "runner receipt signature encoding/length is invalid")
    if receipt.job_id != request.job_id:
        raise WorkerError("runner-receipt-invalid", "runner receipt is bound to another job")
    if receipt.execution_attempt != request.execution_attempt:
        raise WorkerError("runner-receipt-invalid", "runner receipt is bound to another execution attempt")
    if receipt.immutable_task_digest_sha256 != request.immutable_task_digest_sha256:
        raise WorkerError("runner-receipt-invalid", "runner receipt task digest is not bound to the request")
    if receipt.provider_execution_identity_sha256 != request.provider_execution_identity_sha256:
        raise WorkerError(
            "runner-receipt-invalid",
            "runner receipt provider execution identity is not bound to the request",
        )
    if receipt.input_bundle_sha256 != request.input_bundle_sha256:
        raise WorkerError("runner-receipt-invalid", "runner receipt input bundle is not bound to the request")
    if receipt.isolation_policy_sha256 != request.isolation_policy_sha256:
        raise WorkerError("runner-receipt-invalid", "runner receipt isolation policy digest is not bound to the request")
    if receipt.runner_id != accepted_runner.runner_id:
        raise WorkerError("runner-attestation-invalid", "runner ID is not independently accepted")
    if receipt.runner_identity_sha256 != accepted_runner.runner_identity_sha256:
        raise WorkerError("runner-attestation-invalid", "runner identity digest is not independently accepted")
    if receipt.attestation_key_id != accepted_runner.attestation_key_id:
        raise WorkerError("runner-attestation-invalid", "runner attestation key ID is not independently accepted")
    if receipt.external_request_observed:
        if not receipt.external_request_attempted or receipt.gateway_request_id is None:
            raise WorkerError("runner-receipt-invalid", "observed egress requires an attempted request and gateway ID")
        if receipt.executed_executable_sha256 is None or receipt.output_bundle_sha256 is None:
            raise WorkerError("runner-receipt-invalid", "observed execution requires executable and output bundle digests")
    elif receipt.gateway_request_id is not None:
        raise WorkerError("runner-receipt-invalid", "unobserved execution cannot carry a gateway request ID")
    if not receipt.external_request_attempted and (
        receipt.external_request_observed
        or receipt.executed_executable_sha256 is not None
        or receipt.output_bundle_sha256 is not None
    ):
        raise WorkerError("runner-receipt-invalid", "unattempted execution cannot claim output or executable identity")
    expected_digest = provider_receipt_digest(receipt)
    if receipt.receipt_digest_sha256 != expected_digest:
        raise WorkerError("runner-attestation-invalid", "runner receipt body digest is invalid")
    try:
        signature_valid = signature_verifier.verify(
            bytes.fromhex(expected_digest),
            receipt.attestation_signature,
            receipt.attestation_key_id,
        )
    except Exception as error:
        raise WorkerError("runner-attestation-invalid", "runner receipt signature verifier failed closed") from error
    if signature_valid is not True:
        raise WorkerError("runner-attestation-invalid", "runner receipt signature is not accepted")
    if receipt.whole_job_quiescent is not True:
        raise WorkerError(
            "process-tree-not-quiescent",
            "accepted runner receipt does not prove whole-job quiescence",
            external_request_attempted=receipt.external_request_attempted,
            external_request_observed=receipt.external_request_observed,
            gateway_request_id=receipt.gateway_request_id,
        )
    return receipt


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ui_support_inputs(provider: str = "opencode") -> dict[str, bytes]:
    """Reviewed platform input, not generated application behavior.

    Exact bytes enter the provider's consent-bound instruction hash. They are also
    rechecked after authoring, so a file replacement is never silently accepted.
    """
    inputs = {}
    instruction_file = "codex_instructions.txt" if provider == "openai-codex" else "opencode_instructions.txt"
    for name in (instruction_file, "vibapp_support.rs", "README.md"):
        path = BASE / "starter" / name
        try:
            metadata = path.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_mode & 0o022 or not 0 < metadata.st_size <= 64 * 1024):
                raise WorkerError("integrity-failure", "UI support input metadata is unsafe")
            payload = path.read_bytes()
            payload.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise WorkerError("integrity-failure", "UI support input is unavailable") from error
        if len(payload) != metadata.st_size:
            raise WorkerError("integrity-failure", "UI support input changed while reading")
        inputs[name] = payload
    return inputs


def uses_ui_support(task: dict[str, Any]) -> bool:
    if task.get("target", {}).get("wit_world") != "ui-only-reference":
        return False
    if task.get("provider") == "opencode":
        return True
    # The already identity-bound Docker adapter is the opt-in boundary. Preserve
    # native/legacy Codex and non-UI instruction bytes and source scaffolds.
    return (task.get("schema_version") == TASK_SCHEMA_VERSION
            and task.get("provider") == "openai-codex"
            and task.get("provider_execution_identity", {}).get("runtime", {}).get("package_id")
            == "openai-codex-cli-docker"
            # Archived Docker tasks already consented to the original no-support
            # policy remain readable/revisable without reinterpreting their hash.
            # Only that exact known digest selects the historical path. Unknown
            # digests use current inputs and fail the ordinary consent check.
            and task.get("consent", {}).get("instructions_digest_sha256") != INSTRUCTIONS_DIGEST)


def ui_support_instructions(provider: str, inputs: dict[str, bytes]) -> str:
    instruction_file = "codex_instructions.txt" if provider == "openai-codex" else "opencode_instructions.txt"
    return (inputs[instruction_file].decode("utf-8")
            + "\nUI support SHA256: " + sha256_bytes(inputs["vibapp_support.rs"])
            + "\nUI guide SHA256: " + sha256_bytes(inputs["README.md"]) + "\n")


def consent_bound_ui_support_inputs(task: dict[str, Any]) -> dict[str, bytes]:
    inputs = ui_support_inputs(task["provider"])
    instructions, _ = authoring_instruction_bundle(task["provider"], task, support_inputs=inputs)
    digest = sha256_bytes(instructions.encode("utf-8"))
    if digest != task.get("consent", {}).get("instructions_digest_sha256"):
        raise WorkerError("consent-required", "immutable UI support inputs differ from consent")
    return inputs


def legacy_provider_instructions(
    provider: str, task: dict[str, Any] | None = None,
    *, support_inputs: dict[str, bytes] | None = None,
) -> str:
    """Exact pre-UI/UX-policy bytes; only their known digest selects history."""
    if provider == "openai-codex" and task is not None and uses_ui_support(task):
        return ui_support_instructions(provider, support_inputs if support_inputs is not None else ui_support_inputs(provider))
    if provider == "openai-codex":
        return PROVIDER_INSTRUCTIONS
    if provider == "opencode":
        return ui_support_instructions(provider, support_inputs if support_inputs is not None else ui_support_inputs(provider))
    if provider in SUPPORTED_TASK_PROVIDERS:
        return OTHER_PROVIDER_INSTRUCTIONS
    raise WorkerError("schema-invalid", "unsupported CodeAgent provider")


def ui_ux_skill_bytes() -> bytes:
    """Load the one passive, trusted skill, without following links or hooks."""
    return passive_skill_bytes("vibapp-ui-ux", "SKILL.md")


def passive_skill_bytes(skill_name: str, filename: str) -> bytes:
    """Read an explicitly selected, bounded instruction asset, never execute it."""
    root = BASE / "skills"
    directory = root / skill_name
    path = directory / filename
    try:
        for parent in (root, directory):
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022:
                raise WorkerError("integrity-failure", "UI/UX skill directory is unsafe")
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_mode & 0o022 or not 0 < metadata.st_size <= 64 * 1024):
            raise WorkerError("integrity-failure", "UI/UX skill metadata is unsafe")
        payload = path.read_bytes()
        payload.decode("utf-8")
        after = path.lstat()
        if (len(payload) != metadata.st_size
                or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_mode)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)):
            raise WorkerError("integrity-failure", "UI/UX skill changed while reading")
        return payload
    except (OSError, UnicodeError) as error:
        raise WorkerError("integrity-failure", "UI/UX skill is unavailable") from error


def ui_ux_skill_instructions(payload: bytes) -> str:
    return (
        "\nTrusted application-experience guidance:\n"
        "Before authoring, read contracts/vibapp-ui-ux.md completely and apply its relevant guidance. "
        "This immutable document is passive task input, not permission to invoke skills, plugins, hooks or external tools. "
        "Do not edit, remove, chmod or recreate it. For service-only apps apply lifecycle and capability honesty without inventing a GUI; "
        "it does not supply UI-only Rust support for other worlds.\n"
        "VibApp UI/UX skill SHA256: " + sha256_bytes(payload) + "\n"
    )


APPLE_DESIGN_FILES = (
    "SKILL.md", "design-system.md", "patterns.md", "components.md",
    "checklist.md", "motion.md", "app.md", "icons.md", "review.md",
    "tokens.css", "reference.html", "LICENSE",
)


def apple_design_inputs() -> dict[str, bytes]:
    # A closed, flat list fits the existing contracts/ audit. No auto-discovery,
    # hooks, examples, package installs or upstream network fetch at task time.
    return {"contracts/apple-design-" + name: passive_skill_bytes("apple-design", name)
            for name in APPLE_DESIGN_FILES}


def apple_design_instructions(inputs: dict[str, bytes]) -> str:
    manifest = "\n".join(path + " SHA256 " + sha256_bytes(payload)
                         for path, payload in sorted(inputs.items()))
    return (
        "\nDefault Apple-inspired application design guidance:\n"
        "Read contracts/apple-design-SKILL.md, apple-design-design-system.md, "
        "apple-design-patterns.md and apple-design-checklist.md under contracts/ before UI authoring. "
        "All upstream relative references are supplied in contracts/ with the prefix apple-design- "
        "(for example motion.md is contracts/apple-design-motion.md). Read additional references only as relevant.\n"
        "Apply restrained information hierarchy, concise copy, useful first screens, grouping, "
        "one primary action, compact responsive layouts, localization and accessible interactions. "
        "This is the default visual direction, not an override of a user's explicit design choice.\n"
        "VibApp adaptation takes precedence over web/SwiftUI examples: the supplied WIT and "
        "contracts/vibapp-ui-ux.md remain authoritative. Guests emit Rust semantic nodes, never HTML, "
        "CSS, JavaScript, native chrome or fabricated UI/manifest APIs. The Client owns typography, "
        "color, spacing and windows. Express supported grouping/actions in the node tree and record "
        "unexpressible design intent in source/README.md; do not copy tokens.css into guest output. "
        "No fake device frames, duplicate app headers, decorative status dots or invented statistics. "
        "Service-only apps apply capability honesty and lifecycle usability, without inventing a GUI. "
        "All assets (including reference.html) are passive read-only references: do not execute scripts, "
        "install dependencies, launch a browser or expand sandbox authority. Report unavailable visual "
        "checks as pending; do not claim observed screenshots or successful UI execution.\n"
        "Upstream: https://github.com/naplesblue/apple-design-skill at "
        "e81692da299d64b9bf38ae26db2d709fc60c8bf3 (MIT; third-party notices in apple-design-LICENSE).\n"
        + manifest + "\n"
    )


def authoring_instruction_bundle(
    provider: str, task: dict[str, Any] | None = None,
    *, support_inputs: dict[str, bytes] | None = None,
) -> tuple[str, dict[str, bytes]]:
    """Choose an exact historical policy or current policy plus immutable inputs.

    No consent (including the provider-only preview) always chooses current
    guidance. Unknown or changed digests never grant a historical fallback; the
    normal consent comparison rejects them. Reading history needs no new skill.
    """
    needs_support = task is not None and uses_ui_support(task)
    if support_inputs is None and (needs_support or provider == "opencode"):
        support_inputs = ui_support_inputs(provider)
    legacy = legacy_provider_instructions(provider, task, support_inputs=support_inputs)
    consent_digest = task.get("consent", {}).get("instructions_digest_sha256") if task is not None else None
    historical = consent_digest == sha256_bytes(legacy.encode("utf-8"))
    files = {}
    if needs_support:
        files["source/src/vibapp_support.rs"] = support_inputs["vibapp_support.rs"]
        files["contracts/rust-support.md"] = support_inputs["README.md"]
    if historical:
        return legacy, files
    # Old OpenCode non-UI tasks keep their original conditional template above;
    # new tasks use the general world-aware policy, with no UI support scaffold.
    base = OTHER_PROVIDER_INSTRUCTIONS if (
        provider == "opencode" and task is not None
        and task.get("target", {}).get("wit_world") != "ui-only-reference"
    ) else legacy
    payload = ui_ux_skill_bytes()
    files["contracts/vibapp-ui-ux.md"] = payload
    prior = base + ui_ux_skill_instructions(payload)
    # Preserve already-consented UI/UX-only tasks byte-for-byte. New tasks always
    # receive Apple guidance; a changed or unknown digest never selects history.
    if consent_digest == sha256_bytes(prior.encode("utf-8")):
        return prior, files
    design_inputs = apple_design_inputs()
    files.update(design_inputs)
    return prior + apple_design_instructions(design_inputs), files


def provider_instructions(provider: str, task: dict[str, Any] | None = None) -> str:
    return authoring_instruction_bundle(provider, task)[0]


def consent_bound_authoring_inputs(task: dict[str, Any]) -> dict[str, bytes]:
    instructions, files = authoring_instruction_bundle(task["provider"], task)
    if sha256_bytes(instructions.encode("utf-8")) != task.get("consent", {}).get("instructions_digest_sha256"):
        raise WorkerError("consent-required", "immutable authoring inputs differ from consent")
    return files


def provider_instructions_digest(provider: str, task: dict[str, Any] | None = None) -> str:
    return sha256_bytes(provider_instructions(provider, task).encode("utf-8"))


def cargo_package_name(package_intent: dict[str, Any]) -> str:
    """Derive one deterministic Cargo-safe name solely from package_intent.app_id."""
    app_id = package_intent.get("app_id")
    if not isinstance(app_id, str) or not APP_IDENTIFIER.fullmatch(app_id):
        raise WorkerError("schema-invalid", "package_intent.app_id is not a VibApp identifier")
    candidate = app_id.replace(".", "_")
    if len(candidate) <= 64:
        return candidate
    suffix = sha256_bytes(app_id.encode("ascii"))[:12]
    return f"{candidate[:51]}-{suffix}"


def canonical_cargo_manifest(task: dict[str, Any]) -> bytes:
    """Return the exact closed Cargo.toml scaffold bound to the immutable task."""
    package = task.get("package_intent")
    if not isinstance(package, dict):
        raise WorkerError("schema-invalid", "task.package_intent must be an object")
    version = package.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise WorkerError("schema-invalid", "package_intent.version must be a simple semantic version")
    app_id = package.get("app_id")
    name = cargo_package_name(package)
    return (
        f"# Generated by {CARGO_SCAFFOLD_POLICY_VERSION}.\n"
        f"# Source: consent-bound package_intent.app_id={app_id} and package_intent.version={version}.\n"
        "# Provider edits are rejected by exact post-execution byte and digest validation.\n"
        "[package]\n"
        f"name = \"{name}\"\n"
        f"version = \"{version}\"\n"
        "edition = \"2024\"\n"
        "publish = false\n"
        "autobins = false\n"
        "autoexamples = false\n"
        "autotests = false\n"
        "autobenches = false\n"
        "\n"
        "[lib]\n"
        "crate-type = [\"cdylib\"]\n"
        "\n"
        "[dependencies]\n"
        "wit-bindgen = { version = \"=0.60.0\", default-features = false, features = [\"bitflags\", \"macro-string\", \"macros\", \"realloc\"] }\n"
    ).encode("utf-8")


def provider_request(task: dict[str, Any], contract_digest: str | None = None) -> dict[str, Any]:
    """Return the exact immutable object exposed to the provider."""
    contract_digest = contract_digest or sha256_file(CONTRACT)
    cargo_manifest = canonical_cargo_manifest(task)
    legacy = task.get("schema_version") == LEGACY_TASK_SCHEMA_VERSION
    request = {
        "schema_version": LEGACY_PROVIDER_REQUEST_VERSION if legacy else PROVIDER_REQUEST_VERSION,
        "policy_version": LEGACY_POLICY_VERSION if legacy else POLICY_VERSION,
        "job_id": task["job_id"],
        "provider": task["provider"],
        "model": task["model"],
        "need_spec_current_revision": task["need_spec_current_revision"],
        "need_spec": task["need_spec"],
        "package_intent": task["package_intent"],
        "target": task["target"],
        "limits": task["limits"],
        "contract_digest_sha256": contract_digest,
        "instructions_digest_sha256": provider_instructions_digest(task["provider"], task),
        "cargo_scaffold": {
            "policy_version": CARGO_SCAFFOLD_POLICY_VERSION,
            "source": "task.package_intent.app_id+version",
            "path": "source/Cargo.toml",
            "package_name": cargo_package_name(task["package_intent"]),
            "package_version": task["package_intent"]["version"],
            "sha256": sha256_bytes(cargo_manifest),
        },
    }
    if not legacy:
        request["provider_execution_identity"] = task["provider_execution_identity"]
    return request


def immutable_task_digest(task: dict[str, Any], contract_digest: str | None = None) -> str:
    return sha256_bytes(canonical_json(provider_request(task, contract_digest)))


def resolve_executable_identity(
    configured_path: Path,
    pin: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a bounded symlink chain and bind the final regular executable bytes."""
    pin = pin or CODEX_PIN
    if str(configured_path) != pin["configured_path"]:
        raise WorkerError("executable-identity-invalid", "configured executable path is not the pinned path")
    current = configured_path
    chain: list[str] = []
    for _ in range(16):
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise WorkerError("executable-identity-invalid", "configured executable path is missing") from error
        chain.append(str(current))
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(current)
            target_path = Path(target)
            if not target_path.is_absolute():
                target_path = current.parent / target_path
            current = Path(os.path.normpath(str(target_path)))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerError("executable-identity-invalid", "resolved executable is not a regular file")
        break
    else:
        raise WorkerError("executable-identity-invalid", "executable symlink chain exceeds 16 links")
    resolved = current.resolve(strict=True)
    try:
        reviewed_resolved = Path(pin["resolved_path"]).resolve(strict=True)
    except FileNotFoundError as error:
        raise WorkerError("executable-identity-invalid", "reviewed executable path is missing") from error
    if resolved != reviewed_resolved:
        raise WorkerError("executable-identity-invalid", "resolved executable path differs from the reviewed pin")
    metadata = resolved.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkerError("executable-identity-invalid", "final executable identity is not a regular file")
    if metadata.st_uid not in {0, os.getuid()} or metadata.st_mode & 0o022:
        raise WorkerError("executable-identity-invalid", "resolved executable owner/mode is unsafe")
    if not os.access(resolved, os.X_OK):
        raise WorkerError("executable-identity-invalid", "resolved executable is not executable")
    digest = sha256_file(resolved)
    if digest != pin["sha256"]:
        raise WorkerError("executable-identity-invalid", "resolved executable digest differs from the reviewed pin")
    version = pin.get("version")
    if not version or version.split()[-1] not in resolved.parts:
        raise WorkerError("executable-identity-invalid", "resolved executable path does not bind the pinned version")
    return {
        "configured_path": str(configured_path),
        "resolved_path": str(resolved),
        "symlink_chain": chain,
        "version": version,
        "sha256": digest,
        "owner_uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: Any, context: str) -> dt.datetime:
    if not isinstance(value, str) or not UTC.fullmatch(value):
        raise WorkerError("schema-invalid", f"{context} must be canonical whole-second UTC")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as error:
        raise WorkerError("schema-invalid", f"{context} is not a calendar UTC instant") from error


def load_json(path: Path, maximum_bytes: int, context: str) -> Any:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise WorkerError("not-found", f"{context} does not exist") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise WorkerError("schema-invalid", f"{context} must be a regular non-symlink file")
    if metadata.st_size > maximum_bytes:
        raise WorkerError("resource-limit", f"{context} exceeds {maximum_bytes} bytes")
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerError("schema-invalid", f"{context} is not strict UTF-8 JSON: {error}") from error


SCHEMA_ANNOTATION_KEYS = {"$schema", "$id", "title", "description", "$comment"}
SCHEMA_ASSERTION_KEYS = {
    "$defs",
    "$ref",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "oneOf",
    "anyOf",
}
SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _schema_unavailable(message: str) -> WorkerError:
    return WorkerError("schema-validator-unavailable", message)


def lint_authoritative_schema(schema: Any, *, context: str = "task schema") -> dict[str, Any]:
    """Reject every unsupported Draft 2020-12 assertion/applicator before use.

    This product-layer validator deliberately implements only the closed keyword set
    used by the versioned task schemas. Unknown keywords are never treated as
    annotations or silently ignored.
    """
    if not isinstance(schema, dict):
        raise _schema_unavailable(f"{context} root must be a JSON Schema object")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise _schema_unavailable(f"{context} does not select Draft 2020-12")

    def walk(node: Any, path: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise _schema_unavailable(f"{context} {path} is not a schema object/boolean")
        unknown = set(node) - SCHEMA_ANNOTATION_KEYS - SCHEMA_ASSERTION_KEYS
        if unknown:
            raise _schema_unavailable(
                f"{context} {path} uses unsupported schema keyword(s): {sorted(unknown)}"
            )
        reference = node.get("$ref")
        if reference is not None and (
            not isinstance(reference, str) or not reference.startswith("#/")
        ):
            raise _schema_unavailable(f"{context} {path} uses a non-local or malformed $ref")
        declared_type = node.get("type")
        if declared_type is not None:
            declared_types = [declared_type] if isinstance(declared_type, str) else declared_type
            if (
                not isinstance(declared_types, list)
                or not declared_types
                or any(item not in SCHEMA_TYPES for item in declared_types)
                or len(set(declared_types)) != len(declared_types)
            ):
                raise _schema_unavailable(f"{context} {path}.type is unsupported")
        for keyword in ("minItems", "maxItems", "minLength", "maxLength"):
            if keyword in node and (type(node[keyword]) is not int or node[keyword] < 0):
                raise _schema_unavailable(f"{context} {path}.{keyword} must be a non-negative integer")
        for keyword in ("minimum", "maximum"):
            if keyword in node and (
                not isinstance(node[keyword], (int, float)) or isinstance(node[keyword], bool)
            ):
                raise _schema_unavailable(f"{context} {path}.{keyword} must be numeric")
        if "pattern" in node:
            if not isinstance(node["pattern"], str):
                raise _schema_unavailable(f"{context} {path}.pattern must be a string")
            try:
                re.compile(node["pattern"])
            except re.error as error:
                raise _schema_unavailable(f"{context} {path}.pattern is invalid: {error}") from error
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise _schema_unavailable(f"{context} {path}.enum must be a non-empty array")
        if "required" in node and (
            not isinstance(node["required"], list)
            or any(not isinstance(item, str) for item in node["required"])
            or len(set(node["required"])) != len(node["required"])
        ):
            raise _schema_unavailable(f"{context} {path}.required must contain unique strings")
        if "uniqueItems" in node and type(node["uniqueItems"]) is not bool:
            raise _schema_unavailable(f"{context} {path}.uniqueItems must be boolean")
        for mapping_key in ("$defs", "properties"):
            if mapping_key in node:
                mapping = node[mapping_key]
                if not isinstance(mapping, dict):
                    raise _schema_unavailable(f"{context} {path}.{mapping_key} must be an object")
                for key, child in mapping.items():
                    walk(child, f"{path}.{mapping_key}.{key}")
        additional = node.get("additionalProperties")
        if additional is not None:
            if not isinstance(additional, (bool, dict)):
                raise _schema_unavailable(
                    f"{context} {path}.additionalProperties must be boolean or schema"
                )
            if isinstance(additional, dict):
                walk(additional, f"{path}.additionalProperties")
        items = node.get("items")
        if items is not None:
            if not isinstance(items, (bool, dict)):
                raise _schema_unavailable(f"{context} {path}.items must be boolean or schema")
            if isinstance(items, dict):
                walk(items, f"{path}.items")
        for list_key in ("prefixItems", "oneOf", "anyOf"):
            if list_key in node:
                children = node[list_key]
                if not isinstance(children, list) or not children:
                    raise _schema_unavailable(f"{context} {path}.{list_key} must be non-empty")
                for index, child in enumerate(children):
                    walk(child, f"{path}.{list_key}[{index}]")

    walk(schema, "#")
    return schema


def _resolve_local_schema_ref(root: dict[str, Any], reference: str, context: str) -> Any:
    target: Any = root
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or segment not in target:
            raise _schema_unavailable(f"{context} has unresolved local $ref {reference}")
        target = target[segment]
    if not isinstance(target, (dict, bool)):
        raise _schema_unavailable(f"{context} local $ref {reference} does not select a schema")
    return target


def _schema_equal(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def _schema_type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": type(value) is bool,
        "null": value is None,
    }[expected]


def validate_json_schema_instance(
    instance: Any,
    schema: dict[str, Any],
    *,
    context: str = "task",
) -> None:
    """Validate an instance against the linted, closed task-schema subset."""
    root = lint_authoritative_schema(schema, context=f"{context} authoritative schema")

    def fail(path: str, detail: str) -> None:
        raise WorkerError("schema-invalid", f"{context} {path}: {detail}")

    def matches(value: Any, node: Any, path: str) -> bool:
        try:
            check(value, node, path)
            return True
        except WorkerError as error:
            if error.code != "schema-invalid":
                raise
            return False

    def check(value: Any, node: Any, path: str) -> None:
        if node is True:
            return
        if node is False:
            fail(path, "false schema rejects the value")
        if "$ref" in node:
            check(value, _resolve_local_schema_ref(root, node["$ref"], context), path)
        if "type" in node:
            types = [node["type"]] if isinstance(node["type"], str) else node["type"]
            if not any(_schema_type_matches(value, expected) for expected in types):
                fail(path, f"expected type {types}")
        if "const" in node and not _schema_equal(value, node["const"]):
            fail(path, "does not equal const")
        if "enum" in node and not any(_schema_equal(value, item) for item in node["enum"]):
            fail(path, "is not in enum")
        if "oneOf" in node:
            count = sum(matches(value, child, path) for child in node["oneOf"])
            if count != 1:
                fail(path, f"must match exactly one oneOf branch (matched {count})")
        if "anyOf" in node and not any(matches(value, child, path) for child in node["anyOf"]):
            fail(path, "must match at least one anyOf branch")
        if isinstance(value, dict):
            required = node.get("required", [])
            missing = [key for key in required if key not in value]
            if missing:
                fail(path, f"missing required properties {missing}")
            properties = node.get("properties", {})
            for key, child in properties.items():
                if key in value:
                    check(value[key], child, f"{path}.{key}")
            extras = set(value) - set(properties)
            additional = node.get("additionalProperties", True)
            if extras and additional is False:
                fail(path, f"additional properties are forbidden: {sorted(extras)}")
            if isinstance(additional, dict):
                for key in extras:
                    check(value[key], additional, f"{path}.{key}")
        if isinstance(value, list):
            if "minItems" in node and len(value) < node["minItems"]:
                fail(path, f"contains fewer than {node['minItems']} items")
            if "maxItems" in node and len(value) > node["maxItems"]:
                fail(path, f"contains more than {node['maxItems']} items")
            if node.get("uniqueItems"):
                encoded = [canonical_json(item) for item in value]
                if len(set(encoded)) != len(encoded):
                    fail(path, "contains duplicate items")
            prefix = node.get("prefixItems", [])
            for index, child in enumerate(prefix):
                if index < len(value):
                    check(value[index], child, f"{path}[{index}]")
            if "items" in node:
                items = node["items"]
                for index in range(len(prefix), len(value)):
                    check(value[index], items, f"{path}[{index}]")
        if isinstance(value, str):
            if "minLength" in node and len(value) < node["minLength"]:
                fail(path, f"is shorter than {node['minLength']} characters")
            if "maxLength" in node and len(value) > node["maxLength"]:
                fail(path, f"is longer than {node['maxLength']} characters")
            if "pattern" in node and re.search(node["pattern"], value) is None:
                fail(path, "does not match pattern")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in node and value < node["minimum"]:
                fail(path, f"is below minimum {node['minimum']}")
            if "maximum" in node and value > node["maximum"]:
                fail(path, f"is above maximum {node['maximum']}")

    check(instance, root, "$")


def authoritative_task_schema(version: str) -> dict[str, Any]:
    paths = {
        TASK_SCHEMA_VERSION: TASK_SCHEMA,
        LEGACY_TASK_SCHEMA_VERSION: LEGACY_TASK_SCHEMA,
    }
    path = paths.get(version)
    if path is None:
        raise WorkerError("schema-invalid", "unsupported cloud task schema_version")
    try:
        schema = load_json(path, 512 * 1024, f"{version} authoritative schema")
    except WorkerError as error:
        raise _schema_unavailable(f"cannot load {version} authoritative schema: {error}") from error
    return lint_authoritative_schema(schema, context=version)


def exact_object(value: Any, keys: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkerError("schema-invalid", f"{context} must be an object")
    actual = set(value)
    if actual != keys:
        raise WorkerError(
            "schema-invalid",
            f"{context} fields mismatch missing={sorted(keys - actual)} extra={sorted(actual - keys)}",
        )
    return value


def text_value(value: Any, context: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise WorkerError("schema-invalid", f"{context} must be a string length {minimum}..{maximum}")
    if any(ord(character) < 0x20 and character not in "\t\n" for character in value):
        raise WorkerError("schema-invalid", f"{context} contains a control character")
    return value


def validate_model(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value.strip() != value
        or value.startswith("-")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise WorkerError("model-invalid", f"{context} is not a bounded CodeAgent model selection")
    return value


def validate_attempt(value: Any, context: str = "task.execution_attempt") -> dict[str, Any]:
    attempt = exact_object(value, {"attempt_id", "ordinal"}, context)
    if not isinstance(attempt["attempt_id"], str) or not ATTEMPT_IDENTIFIER.fullmatch(
        attempt["attempt_id"]
    ):
        raise WorkerError(
            "schema-invalid",
            f"{context}.attempt_id must match attempt-NNNN-<16 lowercase hex>",
        )
    ordinal = int_range(attempt["ordinal"], f"{context}.ordinal", 1, 9999)
    if attempt["attempt_id"][8:12] != f"{ordinal:04}":
        raise WorkerError(
            "schema-invalid",
            f"{context}.attempt_id prefix does not match ordinal",
        )
    return attempt


def canonical_https_endpoint(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise WorkerError("schema-invalid", "provider endpoint must be a bounded string")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise WorkerError("schema-invalid", "provider endpoint has an invalid port") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise WorkerError(
            "schema-invalid",
            "provider endpoint must be HTTPS without userinfo, query, or fragment",
        )
    hostname = parsed.hostname
    if (
        not hostname.isascii()
        or hostname != hostname.casefold()
        or hostname.endswith(".")
        or not HTTPS_HOST.fullmatch(hostname)
    ):
        raise WorkerError("schema-invalid", "provider endpoint host is not canonical lowercase ASCII")
    if port is not None and not 1 <= port <= 65535:
        raise WorkerError("schema-invalid", "provider endpoint port is out of range")
    canonical_port = "" if port in {None, 443} else f":{port}"
    path = parsed.path
    if (
        "\\" in path
        or "//" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
        or not path.isascii()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
        or re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@/%\-]*", path) is None
    ):
        raise WorkerError("schema-invalid", "provider endpoint path is not canonical")
    for match in re.finditer("%", path):
        escape = path[match.start() + 1 : match.start() + 3]
        if len(escape) != 2 or not re.fullmatch(r"[0-9A-F]{2}", escape):
            raise WorkerError("schema-invalid", "provider endpoint percent escape is not canonical")
    canonical_path = path.rstrip("/")
    canonical = f"https://{hostname}{canonical_port}{canonical_path}"
    if value != canonical:
        raise WorkerError("schema-invalid", f"provider endpoint is not canonical; expected {canonical}")
    return canonical


def validate_provider_execution_identity(
    value: Any,
    provider: str,
    context: str = "task.provider_execution_identity",
) -> dict[str, Any]:
    identity = exact_object(
        value,
        {
            "schema_version",
            "endpoint",
            "runtime",
            "non_secret_config_sha256",
            "identity_sha256",
        },
        context,
    )
    if identity["schema_version"] != "vibapp.provider-execution-identity.experimental-v1":
        raise WorkerError("schema-invalid", f"{context}.schema_version is unsupported")
    endpoint = exact_object(
        identity["endpoint"],
        {"kind", "canonical_endpoint", "endpoint_sha256"},
        f"{context}.endpoint",
    )
    if endpoint["kind"] == "https":
        canonical_https_endpoint(endpoint["canonical_endpoint"])
    elif endpoint["kind"] == "fixed-lan-http":
        # Explicit development-only LocalAI binding, not general HTTP/SSRF access.
        if provider != "opencode" or endpoint["canonical_endpoint"] != "http://192.168.199.170:8081/v1/chat/completions":
            raise WorkerError("schema-invalid", "fixed LAN identity is limited to OpenCode and the selected LocalAI endpoint")
    elif endpoint["kind"] == "provider-managed":
        if endpoint["canonical_endpoint"] != "provider-managed":
            raise WorkerError(
                "schema-invalid",
                f"{context}.endpoint canonical value must be provider-managed",
            )
        if provider not in PROVIDER_MANAGED_ENDPOINT_PROVIDERS:
            raise WorkerError(
                "schema-invalid",
                f"provider {provider} requires an explicit HTTPS endpoint identity",
            )
    else:
        raise WorkerError("schema-invalid", f"{context}.endpoint.kind is unsupported")
    endpoint_digest = sha256_value(
        endpoint["endpoint_sha256"], f"{context}.endpoint.endpoint_sha256"
    )
    endpoint_body = {
        "kind": endpoint["kind"],
        "canonical_endpoint": endpoint["canonical_endpoint"],
    }
    if endpoint_digest != sha256_bytes(canonical_json(endpoint_body)):
        raise WorkerError("integrity-failure", "provider endpoint identity digest is invalid")

    runtime = exact_object(
        identity["runtime"],
        {
            "adapter_id",
            "adapter_version",
            "adapter_sha256",
            "package_id",
            "package_version",
            "executable_sha256",
        },
        f"{context}.runtime",
    )
    for field in ("adapter_id", "adapter_version", "package_id", "package_version"):
        text = runtime[field]
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= 128
            or text.strip() != text
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
        ):
            raise WorkerError("schema-invalid", f"{context}.runtime.{field} is invalid")
    sha256_value(runtime["adapter_sha256"], f"{context}.runtime.adapter_sha256")
    sha256_value(runtime["executable_sha256"], f"{context}.runtime.executable_sha256")
    sha256_value(identity["non_secret_config_sha256"], f"{context}.non_secret_config_sha256")
    actual_identity_digest = sha256_value(identity["identity_sha256"], f"{context}.identity_sha256")
    identity_body = {
        "schema_version": identity["schema_version"],
        "endpoint": endpoint,
        "runtime": runtime,
        "non_secret_config_sha256": identity["non_secret_config_sha256"],
    }
    if actual_identity_digest != sha256_bytes(canonical_json(identity_body)):
        raise WorkerError("integrity-failure", "provider execution identity digest is invalid")
    return identity


def identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise WorkerError("schema-invalid", f"{context} is not a bounded identifier")
    return value


def sha256_value(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise WorkerError("schema-invalid", f"{context} is not lowercase SHA-256")
    return value


def bool_value(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise WorkerError("schema-invalid", f"{context} must be boolean")
    return value


def int_range(value: Any, context: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkerError("schema-invalid", f"{context} must be integer {minimum}..{maximum}")
    return value


def string_array(value: Any, context: str, maximum: int, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise WorkerError("schema-invalid", f"{context} must be an array with at most {maximum} items")
    result: list[str] = []
    for index, item in enumerate(value):
        item = text_value(item, f"{context}[{index}]", 1, 1000)
        if allowed is not None and item not in allowed:
            raise WorkerError("schema-invalid", f"{context}[{index}] has unsupported value")
        result.append(item)
    if len(set(result)) != len(result):
        raise WorkerError("schema-invalid", f"{context} contains duplicates")
    return result


def semantic_need_text(task: dict[str, Any]) -> str:
    """Return goal and must-have prose, including any inline exclusions."""
    need = task["need_spec"]
    values = [need["goal"]]
    for requirement in need["requirements"]:
        if requirement["priority"] != "must-have":
            continue
        values.append(requirement["text"])
        values.extend(requirement["acceptance_examples"])
    return "\n".join(values)


def _mixed_reminder_exclusion(excluded: re.Match[str]) -> bool:
    """Accept noun-like coordinated lists, never arbitrary negative sentences."""
    english = excluded.group("english")
    if english is not None:
        objects = re.sub(
            r"\s+(?:is|are|will be)\s+(?:provided|created|supported|needed|required|allowed)$",
            "", english.strip(), flags=re.IGNORECASE,
        )
        separators = list(re.finditer(r"\s*,\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+", objects, re.IGNORECASE))
        # A bare comma can begin a new affirmative subject. Require an explicit
        # final conjunction, and preserve ambiguous "and a reminder is required".
        if not separators or not re.search(r"\b(?:or|and)\b", separators[-1].group(), re.IGNORECASE):
            return False
        if re.search(r"\band\b", separators[-1].group(), re.IGNORECASE) and re.match(
            r"(?:a|an|the)\s+", objects[separators[-1].end():], re.IGNORECASE,
        ):
            return False
    else:
        objects = excluded.group("chinese")
        separators = list(re.finditer(r"、|以及|和|或", objects))
    if not 1 <= len(separators) <= 8:
        return False
    starts = [0, *(separator.end() for separator in separators)]
    ends = [*(separator.start() for separator in separators), len(objects)]
    for start, end in zip(starts, ends):
        item = objects[start:end].strip()
        if not re.fullmatch(r"[\w /-]{1,64}", item) or REMINDER_LIST_PREDICATE.search(item):
            return False
        # Unrelated objects need no domain vocabulary, but a reminder-bearing
        # item must be an explicit object, not a new affirmative subject/clause
        # ("and my alarm rings every morning"). Do not guess its predicate.
        if REMINDER_NEED.search(item) and not re.fullmatch(
            _REMINDER_NEGATIVE_EN_OBJECT if english is not None else _REMINDER_NEGATIVE_ZH_OBJECT,
            item, flags=re.IGNORECASE,
        ):
            return False
    return True


def need_requires_reminder(need_text: str) -> bool:
    """Keep each reminder occurrence unless its own clause explicitly excludes it.

    A negative goal, another requirement, or a forbidden capability never erases
    positive/ambiguous prose elsewhere. Both admission and source validation use
    this classifier so a corrected admission cannot defer the same false failure
    until after remote execution.
    """
    exclusions = []
    candidates = list(REMINDER_EXCLUSION.finditer(need_text))
    candidates.extend(excluded for excluded in REMINDER_MIXED_EXCLUSION.finditer(need_text)
                      if _mixed_reminder_exclusion(excluded))
    for excluded in candidates:
        # Negation in a condition ("if no alarm is set") or a double negative is
        # not an exclusion of the requested behavior. Bound context to its own
        # clause; a previous sentence/NeedSpec field cannot change polarity.
        prefix = re.split(r"[.!?;。；，,\n]", need_text[:excluded.start()])[-1]
        if REMINDER_EXCLUSION_UNCERTAIN_PREFIX.search(prefix):
            continue
        exclusions.append((excluded.start(), excluded.end()))
    return any(
        not any(start <= occurrence.start() < end for start, end in exclusions)
        for occurrence in REMINDER_NEED.finditer(need_text)
    )


def need_requires_user_action(task: dict[str, Any], need_text: str | None = None) -> bool:
    if task["target"]["app_kind"] not in {"ui", "hybrid"}:
        return False
    need_text = semantic_need_text(task) if need_text is None else need_text
    return bool(
        EXPLICIT_UI_ACTION_NEED.search(need_text)
        or (UI_INPUT_NEED.search(need_text) and UI_EFFECT_NEED.search(need_text))
        # A paired pause/resume requirement is an interactive control.  A lone
        # "resume" is deliberately not: it commonly describes lifecycle behavior
        # such as returning to the foreground, rather than a user-visible button.
        or (PAUSE_NEED.search(need_text) and RESUME_NEED.search(need_text))
    )


def validate_need_contract_semantics(task: dict[str, Any]) -> None:
    """Reject explicit NeedSpec behavior that the selected frozen world cannot express."""
    need_text = semantic_need_text(task)
    # Text is a WIT string, not a single-line ABI type. The shared host renders
    # non-sensitive Text fields as expanding textareas and preserves newlines.
    if need_requires_reminder(need_text):
        capabilities = set(task["target"]["required_capabilities"])
        required = {
            "vibapp:experimental-v0/scheduler@0.0.1",
            "vibapp:experimental-v0/notification@0.0.1",
        }
        if task["target"]["app_kind"] != "hybrid" or not required <= capabilities:
            raise WorkerError(
                "semantic-reminder-contract-mismatch",
                "semantic requirement reminder.background requires the hybrid world "
                "with scheduler and notification as app-required capabilities",
            )


def rust_code_projection(source: str) -> str:
    """Mask Rust comments and literals so semantic evidence must be executable tokens.

    This is deliberately a small deterministic lexer rather than a Rust parser.  It
    handles nested block comments, ordinary/byte strings, raw strings, and character
    literals; everything masked is replaced by spaces so token adjacency cannot be
    manufactured by removal.
    """
    output: list[str] = []
    copied_through = 0
    index = 0
    length = len(source)

    def mask(start: int, end: int) -> None:
        nonlocal copied_through
        end = min(end, length)
        output.append(source[copied_through:start])
        output.append(re.sub(r"[^\n]", " ", source[start:end]))
        copied_through = end

    while index < length:
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = length if end < 0 else end
            mask(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < length and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            mask(start, index)
            continue

        raw = re.match(r"(?:br|r)(#{0,255})\"", source[index:])
        if raw:
            start = index
            hashes = raw.group(1)
            index += raw.end()
            closing = f'\"{hashes}'
            end = source.find(closing, index)
            index = length if end < 0 else end + len(closing)
            mask(start, index)
            continue

        prefix = 1 if source.startswith('b"', index) else 0
        if source[index + prefix:index + prefix + 1] == '"':
            start = index
            index += prefix + 1
            escaped = False
            while index < length:
                character = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    break
            mask(start, index)
            continue

        # A lifetime such as 'a has no closing quote; only mask a bounded character
        # literal when an unescaped closing quote occurs on the same line.
        if source[index] == "'":
            cursor = index + 1
            escaped = False
            closing = -1
            while cursor < min(length, index + 16) and source[cursor] != "\n":
                character = source[cursor]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == "'":
                    closing = cursor
                    break
                cursor += 1
            if closing >= 0:
                mask(index, closing + 1)
                index = closing + 1
                continue
        index += 1
    output.append(source[copied_through:])
    return "".join(output)


def validate_generated_semantics(task: dict[str, Any], rust_sources: dict[str, str]) -> None:
    """Reject missing structural wiring; real runtime tests still prove behavior."""
    need_text = semantic_need_text(task)
    code = "\n".join(
        rust_code_projection(rust_sources[path])
        for path in sorted(rust_sources, key=lambda value: value.encode("utf-8"))
        if not (uses_ui_support(task) and path == "src/vibapp_support.rs")
    ).casefold()

    has_button = bool(re.search(r"\bnodekind\s*::\s*button\s*\(", code))
    handles_action = bool(re.search(r"\blauncherevent\s*::\s*action\s*\(", code))
    if uses_ui_support(task):
        # Having a generic helper on disk is not application-side wiring. Count
        # actual calls through its module or explicitly declared local aliases.
        aliases = {"vibapp_support", "crate::vibapp_support"}
        aliases.update(re.findall(r"\buse\s+(?:crate\s*::\s*)?vibapp_support\s+as\s+([a-z_][a-z0-9_]*)\s*;", code))
        for alias in aliases:
            prefix = r"\b" + r"\s*::\s*".join(re.escape(part) for part in alias.split("::"))
            has_button |= bool(re.search(prefix + r"\s*::\s*button\s*\(", code))
            handles_action |= bool(re.search(prefix + r"\s*::\s*action_id\s*\(", code))

    if need_requires_reminder(need_text):
        has_alarm = bool(re.search(r"\bschedulepurpose\s*::\s*alarm\b", code))
        has_upsert = bool(re.search(r"\bscheduler\s*::\s*upsert\s*\(", code))
        has_fire_once = bool(re.search(r"\bmissedpolicy\s*::\s*fireonce\b", code))
        if not (has_alarm and has_upsert and has_fire_once):
            missing = [
                name
                for name, present in (
                    ("schedule-purpose=alarm", has_alarm),
                    ("scheduler.upsert", has_upsert),
                    ("missed-policy=fire-once", has_fire_once),
                )
                if not present
            ]
            raise WorkerError(
                "semantic-reminder-policy-invalid",
                "semantic requirement reminder.background lacks " + ", ".join(missing),
            )
        if TIME_CONFIGURATION_NEED.search(need_text):
            has_time_control = bool(re.search(r"\bfieldkind\s*::\s*time\b", code))
            if not has_time_control:
                raise WorkerError(
                    "semantic-reminder-controls-missing",
                    "semantic requirement reminder.configurable-time lacks a time field or setting",
                )
        if PAUSE_NEED.search(need_text) and RESUME_NEED.search(need_text):
            has_disable = bool(re.search(r"\bscheduler\s*::\s*disable\s*\(", code))
            has_enable = bool(re.search(r"\bscheduler\s*::\s*enable\s*\(", code))
            if not (has_button and handles_action and has_disable and has_enable):
                missing = [
                    name
                    for name, present in (
                        ("button", has_button),
                        ("launcher-action-handler", handles_action),
                        ("scheduler.disable", has_disable),
                        ("scheduler.enable", has_enable),
                    )
                    if not present
                ]
                raise WorkerError(
                    "semantic-reminder-controls-missing",
                    "semantic requirement reminder.pause-resume lacks " + ", ".join(missing),
                )

    if need_requires_user_action(task, need_text) and not (has_button and handles_action):
        missing = [
            name
            for name, present in (
                ("ui.button-node", has_button),
                ("guest.launcher-action-handler", handles_action),
            )
            if not present
        ]
        raise WorkerError(
            "semantic-ui-action-unreachable",
            "semantic requirement ui.user-action lacks " + ", ".join(missing),
        )

    if task["target"]["app_kind"] in {"service", "hybrid"}:
        handles_service = bool(re.search(r"\bappevent\s*::\s*service\s*\(", code))
        handles_start = bool(re.search(r"\bserviceevent\s*::\s*start\s*\(", code))
        handles_trigger = bool(re.search(r"\bserviceevent\s*::\s*trigger\s*\(", code))
        declared_triggers = {
            trigger
            for entrypoint in task["package_intent"]["entrypoints"]
            if entrypoint["kind"] == "service"
            for trigger in entrypoint.get("triggers", ["manual"])
        }
        needs_start = bool(declared_triggers & {"on-enable", "manual"})
        needs_trigger = "scheduler" in declared_triggers
        if (
            not handles_service
            or (needs_start and not handles_start)
            or (needs_trigger and not handles_trigger)
        ):
            missing = [
                name
                for name, present in (
                    ("guest.app-event=service", handles_service),
                    ("guest.service-event=start", not needs_start or handles_start),
                    ("guest.service-event=trigger", not needs_trigger or handles_trigger),
                )
                if not present
            ]
            raise WorkerError(
                "semantic-service-unimplemented",
                "declared service entrypoint lacks " + ", ".join(missing),
            )


def validate_principal(value: Any, context: str) -> dict[str, Any]:
    principal = exact_object(value, {"principal_id", "principal_kind"}, context)
    identifier(principal["principal_id"], f"{context}.principal_id")
    if principal["principal_kind"] not in {"user", "service"}:
        raise WorkerError("schema-invalid", f"{context}.principal_kind is unsupported")
    return principal


def validate_need_spec(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "document_type", "need_id", "owner", "goal", "requirements",
        "negative_constraints", "platforms", "profiles", "permission_ceiling",
        "privacy_requirement", "created_at_utc", "revision",
    }
    need = exact_object(value, keys, "need_spec")
    if need["schema_version"] != "vibapp.need-spec.product-v0.0.1" or need["document_type"] != "need-spec":
        raise WorkerError("schema-invalid", "unsupported NeedSpec schema/document type")
    identifier(need["need_id"], "need_spec.need_id")
    validate_principal(need["owner"], "need_spec.owner")
    text_value(need["goal"], "need_spec.goal", 1, 4000)
    parse_utc(need["created_at_utc"], "need_spec.created_at_utc")
    int_range(need["revision"], "need_spec.revision", 1, 2**63 - 1)
    if need["privacy_requirement"] not in {"local-private", "remote-private", "public"}:
        raise WorkerError("schema-invalid", "need_spec.privacy_requirement is unsupported")

    requirements = need["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 64:
        raise WorkerError("schema-invalid", "need_spec.requirements must contain 1..64 items")
    requirement_ids: list[str] = []
    for index, item in enumerate(requirements):
        item = exact_object(item, {"requirement_id", "text", "priority", "acceptance_examples"}, f"requirement[{index}]")
        requirement_ids.append(identifier(item["requirement_id"], f"requirement[{index}].requirement_id"))
        text_value(item["text"], f"requirement[{index}].text", 1, 1000)
        if item["priority"] not in {"must-have", "nice-to-have"}:
            raise WorkerError("schema-invalid", f"requirement[{index}].priority is unsupported")
        examples = string_array(item["acceptance_examples"], f"requirement[{index}].acceptance_examples", 16)
        if item["priority"] == "must-have" and not examples:
            raise WorkerError("need-incomplete", f"must-have requirement {item['requirement_id']} lacks an acceptance example")
    if len(set(requirement_ids)) != len(requirement_ids):
        raise WorkerError("schema-invalid", "NeedSpec requirement IDs must be unique")

    constraints = need["negative_constraints"]
    if not isinstance(constraints, list) or len(constraints) > 64:
        raise WorkerError("schema-invalid", "need_spec.negative_constraints must have at most 64 items")
    constraint_ids: list[str] = []
    for index, item in enumerate(constraints):
        item = exact_object(item, {"constraint_id", "kind", "value", "source"}, f"negative_constraint[{index}]")
        constraint_ids.append(identifier(item["constraint_id"], f"negative_constraint[{index}].constraint_id"))
        if item["kind"] not in {
            "forbidden-capability", "forbidden-publisher", "forbidden-package", "privacy",
            "platform", "profile", "other",
        }:
            raise WorkerError("schema-invalid", f"negative_constraint[{index}].kind is unsupported")
        text_value(item["value"], f"negative_constraint[{index}].value", 1, 500)
        if item["source"] not in {"user", "rejection-feedback", "policy"}:
            raise WorkerError("schema-invalid", f"negative_constraint[{index}].source is unsupported")
    if len(set(constraint_ids)) != len(constraint_ids):
        raise WorkerError("schema-invalid", "NeedSpec negative-constraint IDs must be unique")

    profiles = string_array(
        need["profiles"], "need_spec.profiles", 4,
        {"desktop", "web-preview", "web-runtime", "headless"},
    )
    if not profiles:
        raise WorkerError("schema-invalid", "need_spec.profiles cannot be empty")
    platforms = need["platforms"]
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= 16:
        raise WorkerError("schema-invalid", "need_spec.platforms must contain 1..16 items")
    seen_platforms: set[tuple[str, str, str]] = set()
    for index, item in enumerate(platforms):
        item = exact_object(item, {"os", "arch", "profile"}, f"platform[{index}]")
        if item["os"] not in {"macos", "windows", "linux", "browser"}:
            raise WorkerError("schema-invalid", f"platform[{index}].os is unsupported")
        if item["arch"] not in {"aarch64", "x86-64", "wasm32"}:
            raise WorkerError("schema-invalid", f"platform[{index}].arch is unsupported")
        if item["profile"] not in profiles:
            raise WorkerError("schema-invalid", f"platform[{index}].profile is absent from NeedSpec profiles")
        seen_platforms.add((item["os"], item["arch"], item["profile"]))
    if len(seen_platforms) != len(platforms):
        raise WorkerError("schema-invalid", "NeedSpec platforms contain duplicates")
    covered_profiles = {profile for _, _, profile in seen_platforms}
    if covered_profiles != set(profiles):
        raise WorkerError("need-incomplete", "every NeedSpec profile requires at least one platform tuple")

    ceiling = exact_object(
        need["permission_ceiling"],
        {"allowed_interfaces", "forbidden_interfaces", "maximum_scope_digests"},
        "need_spec.permission_ceiling",
    )
    allowed = string_array(ceiling["allowed_interfaces"], "permission_ceiling.allowed_interfaces", 32)
    forbidden = string_array(ceiling["forbidden_interfaces"], "permission_ceiling.forbidden_interfaces", 32)
    if set(allowed) & set(forbidden):
        raise WorkerError("schema-invalid", "permission allowed/forbidden interfaces overlap")
    digests = string_array(ceiling["maximum_scope_digests"], "permission_ceiling.maximum_scope_digests", 32)
    for index, digest in enumerate(digests):
        sha256_value(digest, f"permission_ceiling.maximum_scope_digests[{index}]")
    return need


def validate_task_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkerError("schema-invalid", "task must be an object")
    version = value.get("schema_version")
    if not isinstance(version, str):
        raise WorkerError("schema-invalid", "unsupported cloud task schema_version")
    schema = authoritative_task_schema(version)
    validate_json_schema_instance(value, schema, context=version)
    legacy = version == LEGACY_TASK_SCHEMA_VERSION
    keys = {
        "schema_version", "document_type", "job_id", "need_spec_complete",
        "need_spec_current_revision", "need_spec_digest_sha256", "immutable_task_digest_sha256",
        "need_spec", "package_intent",
        "remote_processing_consent", "consent", "provider", "model", "target", "limits",
    }
    if not legacy:
        keys.update({"execution_attempt", "provider_execution_identity"})
    task = exact_object(value, keys, "task")
    if task["document_type"] != "cloud-codeagent-task":
        raise WorkerError("schema-invalid", "unsupported cloud task document_type")
    identifier(task["job_id"], "task.job_id")
    if not legacy:
        validate_attempt(task["execution_attempt"])
    bool_value(task["need_spec_complete"], "task.need_spec_complete")
    bool_value(task["remote_processing_consent"], "task.remote_processing_consent")
    int_range(task["need_spec_current_revision"], "task.need_spec_current_revision", 1, 2**63 - 1)
    expected_need_digest = sha256_value(task["need_spec_digest_sha256"], "task.need_spec_digest_sha256")
    sha256_value(task["immutable_task_digest_sha256"], "task.immutable_task_digest_sha256")
    need = validate_need_spec(task["need_spec"])
    if sha256_bytes(canonical_json(need)) != expected_need_digest:
        raise WorkerError("integrity-failure", "NeedSpec digest does not match the canonical payload")
    package = exact_object(
        task["package_intent"],
        {"app_id", "version", "display_name", "description", "entrypoints"},
        "task.package_intent",
    )
    if not isinstance(package["app_id"], str) or not APP_IDENTIFIER.fullmatch(package["app_id"]):
        raise WorkerError("schema-invalid", "package_intent.app_id is not a VibApp identifier")
    if not isinstance(package["version"], str) or not SEMVER.fullmatch(package["version"]):
        raise WorkerError("schema-invalid", "package_intent.version must be a simple semantic version")
    text_value(package["display_name"], "package_intent.display_name", 1, 80)
    text_value(package["description"], "package_intent.description", 1, 1000)
    entrypoints = package["entrypoints"]
    if not isinstance(entrypoints, list) or not 1 <= len(entrypoints) <= 16:
        raise WorkerError("schema-invalid", "package_intent.entrypoints must contain 1..16 items")
    entrypoint_ids: list[str] = []
    for index, entrypoint in enumerate(entrypoints):
        entrypoint_keys = {"id", "kind", "label", "initial_route"}
        if isinstance(entrypoint, dict) and "triggers" in entrypoint:
            entrypoint_keys.add("triggers")
        entrypoint = exact_object(entrypoint, entrypoint_keys, f"entrypoint[{index}]")
        entrypoint_ids.append(identifier(entrypoint["id"], f"entrypoint[{index}].id"))
        if entrypoint["kind"] not in {"launcher-ui", "service", "settings"}:
            raise WorkerError("schema-invalid", f"entrypoint[{index}].kind is unsupported")
        text_value(entrypoint["label"], f"entrypoint[{index}].label", 1, 80)
        route = entrypoint["initial_route"]
        if route is not None:
            identifier(route, f"entrypoint[{index}].initial_route")
        if entrypoint["kind"] == "launcher-ui" and route is None:
            raise WorkerError("need-incomplete", "launcher-ui entrypoint requires an initial route")
        if entrypoint["kind"] == "service" and route is not None:
            raise WorkerError("schema-invalid", "service entrypoint cannot have an initial route")
        if entrypoint["kind"] == "service":
            triggers = entrypoint.get("triggers", ["manual"])
            if (
                not isinstance(triggers, list)
                or not triggers
                or len(triggers) > len(SERVICE_TRIGGER_ORDER)
                or triggers
                != [trigger for trigger in SERVICE_TRIGGER_ORDER if trigger in triggers]
            ):
                raise WorkerError(
                    "schema-invalid",
                    f"entrypoint[{index}].triggers must be a canonical non-empty service trigger subset",
                )
        elif "triggers" in entrypoint:
            raise WorkerError(
                "schema-invalid",
                f"entrypoint[{index}].triggers is valid only for a service entrypoint",
            )
    if len(set(entrypoint_ids)) != len(entrypoint_ids):
        raise WorkerError("schema-invalid", "package_intent entrypoint IDs must be unique")
    if task["provider"] not in SUPPORTED_TASK_PROVIDERS:
        raise WorkerError("schema-invalid", "CodeAgent provider is unsupported")
    validate_model(task["model"], "task.model")

    consent_keys = {
        "consent_id", "consent_type", "decision", "subject", "job_id", "provider", "model",
        "payload_digest_sha256", "uploaded_data_classes", "single_use", "issued_at_utc",
        "expires_at_utc", "policy_version", "contract_digest_sha256", "instructions_digest_sha256",
    }
    if not legacy:
        consent_keys.update({"attempt_id", "provider_execution_identity_sha256"})
    consent = exact_object(task["consent"], consent_keys, "task.consent")
    identifier(consent["consent_id"], "consent.consent_id")
    if consent["consent_type"] != "remote-processing":
        raise WorkerError("schema-invalid", "consent.consent_type must be remote-processing")
    if consent["decision"] not in {"granted", "denied", "revoked"}:
        raise WorkerError("schema-invalid", "consent.decision is unsupported")
    validate_principal(consent["subject"], "consent.subject")
    identifier(consent["job_id"], "consent.job_id")
    if consent["provider"] not in SUPPORTED_TASK_PROVIDERS:
        raise WorkerError("schema-invalid", "consent.provider is unsupported")
    validate_model(consent["model"], "consent.model")
    if not legacy:
        if (
            not isinstance(consent["attempt_id"], str)
            or not ATTEMPT_IDENTIFIER.fullmatch(consent["attempt_id"])
        ):
            raise WorkerError("schema-invalid", "consent.attempt_id is invalid")
        sha256_value(
            consent["provider_execution_identity_sha256"],
            "consent.provider_execution_identity_sha256",
        )
    sha256_value(consent["payload_digest_sha256"], "consent.payload_digest_sha256")
    sha256_value(consent["contract_digest_sha256"], "consent.contract_digest_sha256")
    sha256_value(consent["instructions_digest_sha256"], "consent.instructions_digest_sha256")
    identifier(consent["policy_version"], "consent.policy_version")
    disclosed_classes = LEGACY_DISCLOSED_DATA_CLASSES if legacy else DISCLOSED_DATA_CLASSES
    classes = string_array(
        consent["uploaded_data_classes"], "consent.uploaded_data_classes", len(disclosed_classes),
        set(disclosed_classes),
    )
    if not classes:
        raise WorkerError("schema-invalid", "consent.uploaded_data_classes cannot be empty")
    bool_value(consent["single_use"], "consent.single_use")
    parse_utc(consent["issued_at_utc"], "consent.issued_at_utc")
    parse_utc(consent["expires_at_utc"], "consent.expires_at_utc")
    if not legacy:
        validate_provider_execution_identity(
            task["provider_execution_identity"], task["provider"]
        )

    target_keys = {
        "contract",
        "wasi",
        "rust_target",
        "wit_world",
        "app_kind",
        "profiles",
        "required_imports",
        "required_capabilities",
    }
    target = exact_object(task["target"], target_keys, "task.target")
    if target["contract"] != "vibapp:experimental-v0@0.0.1":
        raise WorkerError("schema-invalid", "unsupported target contract")
    if target["wasi"] != "0.2" or target["rust_target"] != "wasm32-wasip2":
        raise WorkerError("schema-invalid", "target must be Rust wasm32-wasip2 / WASI 0.2")
    world = target["wit_world"]
    if world not in WORLD_IMPORTS:
        raise WorkerError("schema-invalid", "unsupported WIT world")
    if target["app_kind"] not in {"ui", "service", "hybrid"}:
        raise WorkerError("schema-invalid", "unsupported app kind")
    if world in WORLD_KIND and target["app_kind"] != WORLD_KIND[world]:
        raise WorkerError("schema-invalid", "WIT world and app kind disagree")
    entrypoint_kinds = {entrypoint["kind"] for entrypoint in package["entrypoints"]}
    if target["app_kind"] == "ui" and ("launcher-ui" not in entrypoint_kinds or "service" in entrypoint_kinds):
        raise WorkerError("need-incomplete", "UI package intent must have launcher UI and no service entrypoint")
    if target["app_kind"] == "service" and ("service" not in entrypoint_kinds or "launcher-ui" in entrypoint_kinds):
        raise WorkerError("need-incomplete", "service package intent must have service and no launcher entrypoint")
    if target["app_kind"] == "hybrid" and not {"launcher-ui", "service"} <= entrypoint_kinds:
        raise WorkerError("need-incomplete", "hybrid package intent requires launcher and service entrypoints")
    target_profiles = string_array(
        target["profiles"], "target.profiles", 4,
        {"desktop", "web-preview", "web-runtime", "headless"},
    )
    if not target_profiles:
        raise WorkerError("schema-invalid", "target.profiles cannot be empty")
    imports = string_array(target["required_imports"], "target.required_imports", 8)
    if imports != WORLD_IMPORTS[world]:
        raise WorkerError("schema-invalid", "target.required_imports must exactly match the selected WIT world")
    required_capabilities = string_array(
        target["required_capabilities"], "target.required_capabilities", 8
    )
    if any(interface not in imports for interface in required_capabilities):
        raise WorkerError(
            "schema-invalid",
            "target.required_capabilities must be a subset of structural required_imports",
        )
    validate_need_contract_semantics(task)

    limit_keys = {
        "wall_time_seconds", "cpu_seconds", "memory_bytes", "pids", "workspace_bytes",
        "stdout_bytes", "stderr_bytes", "source_bytes", "source_files",
    }
    limits = exact_object(task["limits"], limit_keys, "task.limits")
    limit_schema = schema["$defs"]["limits"]["properties"]
    for field in sorted(limit_keys):
        field_schema = limit_schema[field]
        int_range(
            limits[field],
            f"limits.{field}",
            field_schema["minimum"],
            field_schema["maximum"],
        )
    if limits["cpu_seconds"] > limits["wall_time_seconds"]:
        raise WorkerError("schema-invalid", "limits.cpu_seconds cannot exceed wall_time_seconds")
    if limits["workspace_bytes"] < limits["source_bytes"]:
        raise WorkerError("schema-invalid", "workspace_bytes must cover source_bytes")
    return task


def validate_authorization(task: dict[str, Any], clock: dt.datetime | None = None) -> None:
    clock = clock or dt.datetime.now(dt.timezone.utc)
    if task.get("schema_version") == LEGACY_TASK_SCHEMA_VERSION:
        raise WorkerError(
            "legacy-provider-identity-unbound",
            "historical v2 task is schema-readable but cannot consume consent or execute because provider identity was not bound",
        )
    if task.get("schema_version") != TASK_SCHEMA_VERSION:
        raise WorkerError("schema-invalid", "unsupported cloud task schema_version")
    consent = task["consent"]
    need = task["need_spec"]
    if task["need_spec_complete"] is not True:
        raise WorkerError("need-incomplete", "NeedSpec completeness has not been asserted")
    if task["remote_processing_consent"] is not True:
        raise WorkerError("consent-required", "remote_processing_consent is not true")
    if consent["decision"] != "granted" or consent["single_use"] is not True:
        raise WorkerError("consent-required", "a granted single-use remote-processing consent is required")
    if (
        consent["job_id"] != task["job_id"]
        or consent["attempt_id"] != task["execution_attempt"]["attempt_id"]
        or consent["provider"] != task["provider"]
        or consent["model"] != task["model"]
    ):
        raise WorkerError(
            "consent-required",
            "consent is not bound to this job, execution attempt, provider and model",
        )
    if consent["subject"] != need["owner"]:
        raise WorkerError("consent-required", "consent subject does not own the NeedSpec")
    if task["need_spec_current_revision"] != need["revision"]:
        raise WorkerError("need-incomplete", "NeedSpec revision is not the asserted current revision")
    if sha256_file(CONTRACT) != CONTRACT_DIGEST_PIN:
        raise WorkerError("integrity-failure", "authoritative contract digest differs from the reviewed pin")
    digest = immutable_task_digest(task, CONTRACT_DIGEST_PIN)
    if task["immutable_task_digest_sha256"] != digest or consent["payload_digest_sha256"] != digest:
        raise WorkerError("consent-required", "consent is not bound to the complete immutable provider request")
    if consent["policy_version"] != POLICY_VERSION:
        raise WorkerError("consent-required", "consent policy version differs from the provider request")
    if consent["contract_digest_sha256"] != CONTRACT_DIGEST_PIN:
        raise WorkerError("consent-required", "consent contract digest differs from the provider request")
    if consent["instructions_digest_sha256"] != provider_instructions_digest(task["provider"], task):
        raise WorkerError("consent-required", "consent instructions digest differs from the provider request")
    if (
        consent["provider_execution_identity_sha256"]
        != task["provider_execution_identity"]["identity_sha256"]
    ):
        raise WorkerError(
            "consent-required",
            "consent provider execution identity differs from the immutable provider request",
        )
    if consent["uploaded_data_classes"] != DISCLOSED_DATA_CLASSES:
        raise WorkerError("consent-required", "consent does not exactly disclose every provider-visible data class")
    issued = parse_utc(consent["issued_at_utc"], "consent.issued_at_utc")
    expires = parse_utc(consent["expires_at_utc"], "consent.expires_at_utc")
    if issued > clock:
        raise WorkerError("consent-required", "consent issue time is in the future")
    if expires <= issued or expires - issued > dt.timedelta(seconds=CONSENT_TTL_SECONDS):
        raise WorkerError("consent-required", "consent lifetime must be positive and at most one hour")
    if expires <= clock:
        raise WorkerError("consent-required", "remote-processing consent has expired")
    if need["privacy_requirement"] != "remote-private":
        raise WorkerError("consent-required", "cloud jobs require privacy_requirement=remote-private")
    if set(task["target"]["profiles"]) != set(need["profiles"]):
        raise WorkerError("need-incomplete", "target profiles do not equal the complete NeedSpec profiles")
    allowed = set(need["permission_ceiling"]["allowed_interfaces"])
    forbidden = set(need["permission_ceiling"]["forbidden_interfaces"])
    required_capabilities = set(task["target"]["required_capabilities"])
    if required_capabilities - allowed or required_capabilities & forbidden:
        raise WorkerError(
            "need-incomplete",
            "app-required capabilities exceed the NeedSpec permission ceiling",
        )
    platform_facts = {
        value
        for item in need["platforms"]
        for value in (item["os"], item["arch"], item["profile"], f"{item['os']}/{item['arch']}/{item['profile']}")
    }
    for constraint in need["negative_constraints"]:
        kind = constraint["kind"]
        value = constraint["value"]
        if kind == "forbidden-capability" and value in required_capabilities:
            raise WorkerError(
                "need-incomplete",
                "an app-required capability conflicts with a negative constraint",
            )
        if kind == "profile" and value in task["target"]["profiles"]:
            raise WorkerError("need-incomplete", "target profile conflicts with a negative constraint")
        if kind == "platform" and value in platform_facts:
            raise WorkerError("need-incomplete", "target platform conflicts with a negative constraint")
        if kind == "forbidden-package" and value == task["package_intent"]["app_id"]:
            raise WorkerError("need-incomplete", "package intent conflicts with a forbidden package")
        if kind == "forbidden-publisher" and (
            task["package_intent"]["app_id"] == value or task["package_intent"]["app_id"].startswith(f"{value}.")
        ):
            raise WorkerError("need-incomplete", "package intent conflicts with a forbidden publisher")
        if kind == "privacy" and value in {"local-private", "no-remote-processing"}:
            raise WorkerError("consent-required", "privacy negative constraint forbids cloud processing")


def validate_task_file(path: Path, clock: dt.datetime | None = None) -> dict[str, Any]:
    task = validate_task_schema(load_json(path, MAX_TASK_BYTES, "cloud task"))
    validate_authorization(task, clock)
    return task


def safe_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise WorkerError("integrity-failure", f"operational root is not a real directory: {path}")
    return path.resolve()


def copy_regular_file(source: Path, destination: Path, mode: int = 0o444) -> None:
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise WorkerError("integrity-failure", f"source is not a regular file: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=64 * 1024)
    destination.chmod(mode)


def prepare_workspace(workspace: Path, task: dict[str, Any]) -> str:
    authoring_inputs = consent_bound_authoring_inputs(task)
    metadata = CONTRACT.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_size > 1024 * 1024:
        raise WorkerError("integrity-failure", "authoritative contract is not a bounded regular file")
    payload = CONTRACT.read_bytes()
    contract_digest = sha256_bytes(payload)
    if contract_digest != CONTRACT_DIGEST_PIN:
        raise WorkerError("integrity-failure", "authoritative contract differs from the reviewed digest pin")
    for destination in (workspace / "contracts/contract.wit", workspace / "source/wit/contract.wit"):
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o444)
    cargo_payload = canonical_cargo_manifest(task)
    cargo_path = workspace / "source/Cargo.toml"
    with cargo_path.open("xb") as handle:
        handle.write(cargo_payload)
        handle.flush()
        os.fsync(handle.fileno())
    cargo_path.chmod(0o444)
    for relative, payload in authoring_inputs.items():
        destination = workspace / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(payload)
        destination.chmod(0o444)
    return contract_digest


def create_prompt(task: dict[str, Any], contract_digest: str) -> bytes:
    instructions = provider_instructions(task["provider"], task)
    request_value = provider_request(task, contract_digest)
    digest = sha256_bytes(instructions.encode("utf-8"))
    if (digest != task.get("consent", {}).get("instructions_digest_sha256")
            or digest != request_value["instructions_digest_sha256"]):
        raise WorkerError("consent-required", "authoring prompt differs from consent")
    request = canonical_json(request_value).decode("utf-8")
    prompt = f"{instructions}\nThe exact consent-bound provider_request is:\n{request}\n"
    encoded = prompt.encode("utf-8")
    if len(encoded) > MAX_TASK_BYTES + 16 * 1024:
        raise WorkerError("resource-limit", "provider prompt is unexpectedly large")
    return encoded


def codex_command(codex_bin: Path, workspace: Path, model: str) -> list[str]:
    model = validate_model(model, "task.model")
    command = [
        str(codex_bin),
        "exec",
        "--model",
        model,
        "--ephemeral",
        "-s",
        "workspace-write",
        "-C",
        str(workspace),
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        str(PROVIDER_SCHEMA),
        "--json",
        "-o",
        str(workspace / "provider-last-message.json"),
        "-",
    ]
    return command


def validate_runner_request_model(request: ProviderRunnerRequest) -> None:
    if (
        request.schema_version != RUNNER_REQUEST_VERSION
        or request.document_type != "provider-runner-request"
    ):
        raise WorkerError(
            "runner-request-invalid",
            "provider runner request schema/document type is unsupported",
        )
    validate_attempt(request.execution_attempt, "provider_runner_request.execution_attempt")
    identity_digest = sha256_value(
        request.provider_execution_identity_sha256,
        "provider_runner_request.provider_execution_identity_sha256",
    )
    identity = validate_provider_execution_identity(
        request.provider_execution_identity,
        request.provider,
        "provider_runner_request.provider_execution_identity",
    )
    if identity["identity_sha256"] != identity_digest:
        raise WorkerError(
            "provider-identity-mismatch",
            "provider runner request identity payload differs from its bound digest",
        )
    model = validate_model(request.model, "provider_runner_request.model")
    if request.provider != "openai-codex":
        raise WorkerError("provider-mismatch", "cloud provider runner supports only openai-codex")
    positions = [index for index, value in enumerate(request.command) if value == "--model"]
    if len(positions) != 1 or positions[0] + 1 >= len(request.command):
        raise WorkerError("model-mismatch", "provider command must contain exactly one explicit --model")
    if request.command[positions[0] + 1] != model:
        raise WorkerError("model-mismatch", "provider command model differs from the authorized runner request")


def required_isolation_policy(task: dict[str, Any]) -> dict[str, Any]:
    limits = task["limits"]
    return {
        "schema_version": ISOLATION_POLICY_VERSION,
        "one_job_per_isolate": True,
        "non_root": True,
        "read_only_base": True,
        "environment_inheritance": "none",
        "credential_delivery": "broker-only",
        "network_policy": "provider-gateway-only",
        "host_home_mounted": False,
        "host_repository_mounted": False,
        "host_ssh_mounted": False,
        "host_cloud_credentials_mounted": False,
        "host_container_socket_mounted": False,
        "whole_job_kill": True,
        "cpu_seconds": limits["cpu_seconds"],
        "rss_bytes": limits["memory_bytes"],
        "pids": limits["pids"],
        "disk_bytes": limits["workspace_bytes"],
    }


def validate_resolved_provider_identity(
    task: dict[str, Any], executable_identity: dict[str, Any]
) -> None:
    """Bind the locally observed CLI bytes/version before a consent can be claimed."""
    runtime = task["provider_execution_identity"]["runtime"]
    if (
        runtime["executable_sha256"] != executable_identity.get("sha256")
        or runtime["package_version"] != executable_identity.get("version")
    ):
        raise WorkerError(
            "provider-identity-mismatch",
            "resolved provider executable bytes/version differ from the consent-bound execution identity",
        )


def validate_runner_attestation(attestation: Any, required: dict[str, Any]) -> None:
    if not isinstance(attestation, dict) or attestation != required:
        raise WorkerError(
            "external-isolation-unavailable",
            "provider runner did not attest the complete required isolation policy",
        )


def tighten_limit(resource_id: int, soft: int, hard: int | None = None) -> None:
    """Lower a limit portably; Darwin rejects lowering soft+hard in one call."""
    current_soft, current_hard = resource.getrlimit(resource_id)
    requested_hard = soft if hard is None else hard
    target_hard = min(requested_hard, current_hard)
    target_soft = min(soft, target_hard)
    resource.setrlimit(resource_id, (target_soft, current_hard))
    try:
        resource.setrlimit(resource_id, (target_soft, target_hard))
    except ValueError:
        # Darwin refuses a hard AS/RSS ceiling below the Python supervisor's
        # already-mapped virtual address space.  The inherited soft ceiling still
        # stops accidental growth; the external production supervisor must own the
        # non-bypassable cgroup/VM ceiling.
        if sys.platform != "darwin" or resource_id not in {
            resource.RLIMIT_AS,
            getattr(resource, "RLIMIT_RSS", -1),
        }:
            raise


def child_limits(limits: dict[str, int]) -> None:
    tighten_limit(resource.RLIMIT_CORE, 0)
    tighten_limit(resource.RLIMIT_NOFILE, 256)
    tighten_limit(resource.RLIMIT_FSIZE, limits["source_bytes"])
    cpu = max(1, min(limits.get("cpu_seconds", limits["wall_time_seconds"]), 1800))
    tighten_limit(resource.RLIMIT_CPU, cpu, cpu + 1)
    if sys.platform != "darwin":
        tighten_limit(resource.RLIMIT_AS, limits["memory_bytes"])
        if hasattr(resource, "RLIMIT_RSS"):
            tighten_limit(resource.RLIMIT_RSS, limits["memory_bytes"])


def process_snapshot() -> dict[int, dict[str, int | str]]:
    """Return one bounded host-process snapshot for synthetic supervision only."""
    try:
        observation = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,uid=,rss=,state=,lstart="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkerError("process-observation-failed", "cannot observe synthetic provider process tree") from error
    if observation.returncode != 0 or len(observation.stdout) > 2 * 1024 * 1024:
        raise WorkerError("process-observation-failed", "synthetic provider process observation failed closed")
    snapshot: dict[int, dict[str, int | str]] = {}
    for line in observation.stdout.splitlines():
        try:
            pid_text, pgid_text, uid_text, rss_text, state, start_token = line.decode("ascii").split(None, 5)
            snapshot[int(pid_text)] = {
                "pgid": int(pgid_text),
                "uid": int(uid_text),
                "rss": int(rss_text) * 1024,
                "state": state,
                "start_token": start_token,
            }
        except (UnicodeDecodeError, ValueError) as error:
            raise WorkerError("process-observation-failed", "synthetic provider process observation was malformed") from error
    return snapshot


def capture_synthetic_handle(pid: int, maximum_wait_seconds: float = 0.25) -> SyntheticProcessHandle:
    deadline = time.monotonic() + maximum_wait_seconds
    while time.monotonic() < deadline:
        item = process_snapshot().get(pid)
        if item is not None:
            return SyntheticProcessHandle(pid=pid, uid=int(item["uid"]), start_token=str(item["start_token"]))
        time.sleep(0.01)
    raise WorkerError("synthetic-handshake-invalid", f"cannot capture process identity for registered PID {pid}")


def handle_is_active(snapshot: dict[int, dict[str, int | str]], handle: SyntheticProcessHandle) -> bool:
    item = snapshot.get(handle.pid)
    return bool(
        item is not None
        and int(item["uid"]) == handle.uid
        and str(item["start_token"]) == handle.start_token
        and not str(item["state"]).startswith("Z")
    )


def active_supervised_members(
    snapshot: dict[int, dict[str, int | str]],
    process_group: int,
    handles: dict[int, SyntheticProcessHandle],
) -> set[int]:
    return {
        pid
        for pid, item in snapshot.items()
        if (
            item["pgid"] == process_group
            or (pid in handles and handle_is_active(snapshot, handles[pid]))
        )
        and not str(item["state"]).startswith("Z")
    }


def register_synthetic_child(
    record: bytes,
    nonce: str,
    root_pid: int,
    handles: dict[int, SyntheticProcessHandle],
    maximum_pids: int,
) -> None:
    """Accept a capability-pipe record only while the child is stopped before work."""
    try:
        verb, record_nonce, pid_text = record.decode("ascii").split(" ")
        pid = int(pid_text)
    except (UnicodeDecodeError, ValueError) as error:
        raise WorkerError("synthetic-handshake-invalid", "synthetic child registration is malformed") from error
    if verb != "REGISTER" or record_nonce != nonce or pid <= 1 or pid == root_pid:
        raise WorkerError("synthetic-handshake-invalid", "synthetic child registration capability is invalid")
    if pid in handles or len(handles) >= maximum_pids:
        raise WorkerError("synthetic-handshake-invalid", "synthetic child registration is duplicate or over PID limit")
    handle = capture_synthetic_handle(pid)
    handles[pid] = handle
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        item = process_snapshot().get(pid)
        if item is not None and handle_is_active({pid: item}, handle) and str(item["state"]).startswith("T"):
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError as error:
                raise WorkerError("synthetic-handshake-invalid", "registered child vanished before acknowledgement") from error
            return
        time.sleep(0.01)
    raise WorkerError("synthetic-handshake-invalid", "registered child did not stop before its first work")


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    handles: dict[int, SyntheticProcessHandle],
) -> dict[int, SyntheticProcessHandle]:
    """Terminate the root process group plus pre-work registered child handles."""
    process_group = process.pid
    deadline = time.monotonic() + 3.0
    sent_kill = False
    quiet_observations = 0
    while time.monotonic() < deadline:
        snapshot = process_snapshot()
        targets = active_supervised_members(snapshot, process_group, handles)
        if not targets:
            quiet_observations += 1
            if quiet_observations >= 2:
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
                return handles
            time.sleep(0.025)
            continue
        quiet_observations = 0
        chosen_signal = signal.SIGKILL if sent_kill or deadline - time.monotonic() < 2.4 else signal.SIGTERM
        group_active = any(
            int(item["pgid"]) == process_group and not str(item["state"]).startswith("Z")
            for item in snapshot.values()
        )
        if group_active:
            try:
                os.killpg(process_group, chosen_signal)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                raise WorkerError("process-tree-not-quiescent", "cannot signal provider process group") from error
        for pid in targets:
            handle = handles.get(pid)
            if handle is not None and not handle_is_active(snapshot, handle):
                continue
            try:
                os.kill(pid, chosen_signal)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                raise WorkerError("process-tree-not-quiescent", f"cannot signal provider descendant {pid}") from error
        if chosen_signal == signal.SIGKILL:
            sent_kill = True
        time.sleep(0.05)
    raise WorkerError("process-tree-not-quiescent", "provider descendants remained after bounded termination")


def workspace_usage(workspace: Path, limits: dict[str, int]) -> tuple[int, int]:
    maximum_files = limits["source_files"] + 512
    maximum_bytes = limits.get("workspace_bytes", limits["source_bytes"] + MAX_PROVIDER_RESULT_BYTES)
    files = 0
    total_bytes = 0
    for root, directories, names in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in directories:
            metadata = (root_path / name).lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise WorkerError("workspace-output-limit", "provider workspace contains an unsafe directory entry")
        for name in names:
            metadata = (root_path / name).lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise WorkerError("workspace-output-limit", "provider workspace contains an unsafe file entry")
            files += 1
            total_bytes += metadata.st_size
            if files > maximum_files or total_bytes > maximum_bytes:
                raise WorkerError("workspace-output-limit", "provider workspace exceeded its aggregate file/byte budget")
    return files, total_bytes


def workspace_tree_digest(workspace: Path) -> str:
    """Digest a quiescent regular-file transfer tree for runner bundle receipts."""
    records: list[tuple[str, str, int]] = []
    for root, directories, names in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in directories:
            metadata = (root_path / name).lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise WorkerError("integrity-failure", "runner bundle contains an unsafe directory")
        for name in names:
            path = root_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise WorkerError("integrity-failure", "runner bundle contains an unsafe file")
            records.append((path.relative_to(workspace).as_posix(), sha256_file(path), metadata.st_size))
    digest = hashlib.sha256()
    digest.update(b"VIBAPP-PROVIDER-RUNNER-BUNDLE\0")
    for relative, file_digest, size in sorted(records, key=lambda record: record[0].encode("utf-8")):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
        digest.update(size.to_bytes(8, "big"))
    return digest.hexdigest()


def run_bounded_provider(
    command: list[str],
    prompt: bytes,
    limits: dict[str, int],
    workspace: Path,
) -> dict[str, int]:
    """Run local synthetic fixtures with pre-work child registration; never live jobs."""
    synthetic_home = workspace / ".synthetic-home"
    synthetic_tmp = workspace / ".synthetic-tmp"
    synthetic_home.mkdir(mode=0o700, exist_ok=True)
    synthetic_tmp.mkdir(mode=0o700, exist_ok=True)
    synthetic_input = workspace / ".synthetic-input"
    synthetic_input.write_bytes(prompt)
    synthetic_input.chmod(0o400)
    registry_read_fd, registry_write_fd = os.pipe()
    os.set_inheritable(registry_write_fd, True)
    registry_nonce = secrets.token_hex(32)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "HOME": str(synthetic_home),
        "CODEX_HOME": str(synthetic_home / "codex"),
        "TMPDIR": str(synthetic_tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "VIBAPP_SYNTHETIC_REGISTRY_FD": str(registry_write_fd),
        "VIBAPP_SYNTHETIC_REGISTRY_NONCE": registry_nonce,
    }
    try:
        input_handle = synthetic_input.open("rb")
        process = subprocess.Popen(
            command,
            stdin=input_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=lambda: child_limits(limits),
            cwd=workspace,
            env=environment,
            close_fds=True,
            pass_fds=(registry_write_fd,),
        )
        input_handle.close()
        os.close(registry_write_fd)
    except (OSError, subprocess.SubprocessError) as error:
        try:
            input_handle.close()
        except (NameError, OSError):
            pass
        for descriptor in (registry_read_fd, registry_write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise WorkerError("provider-start-failed", f"cannot start CodeAgent adapter: {error}") from error

    assert process.stdout is not None and process.stderr is not None
    registry_stream = os.fdopen(registry_read_fd, "rb", buffering=0)
    selector = selectors.DefaultSelector()
    streams = {process.stdout: ("stdout", limits["stdout_bytes"]), process.stderr: ("stderr", limits["stderr_bytes"])}
    counts = {"stdout": 0, "stderr": 0}
    for stream, (name, maximum) in streams.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, ("output", name, maximum))
    os.set_blocking(registry_stream.fileno(), False)
    selector.register(registry_stream, selectors.EVENT_READ, ("registry", None, None))
    deadline = time.monotonic() + limits["wall_time_seconds"]
    # Popen is the root capability; registered child handles are separate. Do
    # not make startup depend on catching an instant-exiting leader in `ps`.
    handles: dict[int, SyntheticProcessHandle] = {}
    registry_buffer = bytearray()
    return_code: int | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                handles = terminate_process_tree(process, handles)
                raise WorkerError("deadline-exceeded", "CodeAgent process tree exceeded the wall-time limit")
            for key, _ in selector.select(timeout=min(0.05, remaining)):
                stream = key.fileobj
                kind, name, maximum = key.data
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    if kind == "registry" and registry_buffer:
                        handles = terminate_process_tree(process, handles)
                        raise WorkerError("synthetic-handshake-invalid", "synthetic registry ended with a partial record")
                    selector.unregister(stream)
                    stream.close()
                    continue
                if kind == "registry":
                    registry_buffer.extend(chunk)
                    if len(registry_buffer) > 4096:
                        handles = terminate_process_tree(process, handles)
                        raise WorkerError("synthetic-handshake-invalid", "synthetic child registry exceeded 4096 bytes")
                    while b"\n" in registry_buffer:
                        record, _, remainder = registry_buffer.partition(b"\n")
                        registry_buffer[:] = remainder
                        register_synthetic_child(
                            record,
                            registry_nonce,
                            process.pid,
                            handles,
                            limits.get("pids", 64),
                        )
                    continue
                assert name is not None and maximum is not None
                counts[name] += len(chunk)
                if counts[name] > maximum:
                    handles = terminate_process_tree(process, handles)
                    raise WorkerError("provider-output-limit", f"CodeAgent {name} exceeded {maximum} bytes")
            snapshot = process_snapshot()
            active = active_supervised_members(snapshot, process.pid, handles)
            if len(active) > limits.get("pids", 64):
                handles = terminate_process_tree(process, handles)
                raise WorkerError("pid-limit", "CodeAgent process tree exceeded the PID limit")
            rss = sum(int(snapshot[pid]["rss"]) for pid in active)
            if rss > limits["memory_bytes"]:
                handles = terminate_process_tree(process, handles)
                raise WorkerError("memory-limit", "CodeAgent process-group RSS exceeded the task limit")
            try:
                workspace_usage(workspace, limits)
            except WorkerError:
                handles = terminate_process_tree(process, handles)
                raise
            polled = process.poll()
            if polled is not None:
                return_code = polled
                handles = terminate_process_tree(process, handles)
        if return_code is None:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                handles = terminate_process_tree(process, handles)
                raise WorkerError("deadline-exceeded", "CodeAgent process tree exceeded the wall-time limit") from error
            handles = terminate_process_tree(process, handles)
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
        if not registry_stream.closed:
            registry_stream.close()
        snapshot = process_snapshot()
        if active_supervised_members(snapshot, process.pid, handles):
            terminate_process_tree(process, handles)
    if return_code != 0:
        raise WorkerError(
            "provider-failed",
            f"CodeAgent exited nonzero ({return_code}); bounded stdout={counts['stdout']} stderr={counts['stderr']}",
        )
    return counts


def normalized_source_path(value: Any, source_root: Path, context: str) -> tuple[str, Path]:
    """Validate one canonical segmented path and prove it resolves below source_root."""
    value = text_value(value, context, 1, 240)
    if "\\" in value or value.startswith("/") or value.endswith("/") or "//" in value:
        raise WorkerError("provider-schema-invalid", f"{context} is not a canonical relative source path")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} or not SOURCE_SEGMENT.fullmatch(segment) for segment in segments):
        raise WorkerError("provider-schema-invalid", f"{context} contains an invalid source-path segment")
    canonical = "/".join(segments)
    root = source_root.resolve(strict=False)
    try:
        resolved = root.joinpath(*segments).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise WorkerError("provider-schema-invalid", f"{context} resolves outside the source root") from error
    return canonical, resolved


def validate_provider_result(value: Any, task: dict[str, Any], source_root: Path) -> dict[str, Any]:
    keys = {
        "schema_version", "document_type", "job_id", "status", "summary", "wit_world",
        "app_kind", "declared_capabilities", "source_files", "unresolved",
    }
    result = exact_object(value, keys, "provider result")
    if result["schema_version"] != "vibapp.cloud-codeagent-provider-result.experimental-v1":
        raise WorkerError("provider-schema-invalid", "provider result schema_version is unsupported")
    if result["document_type"] != "cloud-codeagent-provider-result" or result["status"] != "source-generated":
        raise WorkerError("provider-schema-invalid", "provider result document/status is unsupported")
    if result["job_id"] != task["job_id"]:
        raise WorkerError("provider-schema-invalid", "provider result job_id is not bound to the task")
    text_value(result["summary"], "provider result summary", 1, 1000)
    if result["wit_world"] != task["target"]["wit_world"] or result["app_kind"] != task["target"]["app_kind"]:
        raise WorkerError("provider-schema-invalid", "provider result target disagrees with the task")
    # Informational provider metadata only; handoff authority remains task.target.
    capabilities = result["declared_capabilities"]
    if not isinstance(capabilities, list) or not 5 <= len(capabilities) <= 8:
        raise WorkerError(
            "provider-schema-invalid",
            "provider declared_capabilities must contain 5..8 bounded strings",
        )
    for index, capability in enumerate(capabilities):
        if (
            not isinstance(capability, str)
            or not 1 <= len(capability) <= 200
            or any(ord(character) < 0x20 and character not in "\t\n" for character in capability)
        ):
            raise WorkerError(
                "provider-schema-invalid",
                f"provider declared_capabilities[{index}] is not a bounded string",
            )
    if len(set(capabilities)) != len(capabilities):
        raise WorkerError("provider-schema-invalid", "provider declared_capabilities contains duplicates")
    files = string_array(result["source_files"], "provider result source_files", task["limits"]["source_files"])
    if len(files) < 2:
        raise WorkerError("provider-schema-invalid", "provider must report at least two source files")
    for index, item in enumerate(files):
        normalized_source_path(item, source_root, f"provider result source_files[{index}]")
    unresolved = string_array(result["unresolved"], "provider result unresolved", 32)
    if unresolved:
        summary = "; ".join(unresolved[:3])[:900]
        raise WorkerError(
            "source-incomplete",
            f"provider reported unresolved source issues: {summary}",
        )
    return result


def validate_cargo_manifest(path: Path, task: dict[str, Any] | None = None) -> None:
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise WorkerError("source-invalid", f"Cargo.toml is invalid: {error}") from error
    if set(manifest) != {"package", "lib", "dependencies"}:
        raise WorkerError("source-invalid", "Cargo.toml permits only [package], [lib], and [dependencies]")
    package = manifest["package"]
    library = manifest["lib"]
    dependencies = manifest["dependencies"]
    package_keys = {
        "name", "version", "edition", "publish", "autobins", "autoexamples", "autotests", "autobenches",
    }
    if not isinstance(package, dict) or set(package) != package_keys:
        raise WorkerError("source-invalid", "Cargo [package] fields are not the closed policy shape")
    if not isinstance(package["name"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", package["name"]):
        raise WorkerError("source-invalid", "Cargo package.name is invalid")
    if not isinstance(package["version"], str) or not SEMVER.fullmatch(package["version"]):
        raise WorkerError("source-invalid", "Cargo package.version is invalid")
    if package["edition"] != "2024" or package["publish"] is not False:
        raise WorkerError("source-invalid", "Cargo package must use edition=2024 and publish=false")
    if any(package[key] is not False for key in ("autobins", "autoexamples", "autotests", "autobenches")):
        raise WorkerError("source-invalid", "all Cargo automatic target discovery must be disabled")
    if not isinstance(library, dict) or set(library) != {"crate-type"} or library["crate-type"] != ["cdylib"]:
        raise WorkerError("source-invalid", "Cargo [lib] must contain only crate-type=[cdylib]")
    if not isinstance(dependencies, dict) or set(dependencies) != {"wit-bindgen"}:
        raise WorkerError("source-invalid", "Cargo.toml may depend only on exact wit-bindgen")
    wit = dependencies["wit-bindgen"]
    expected_features = ["bitflags", "macro-string", "macros", "realloc"]
    if not isinstance(wit, dict):
        raise WorkerError("source-invalid", "wit-bindgen dependency must use a strict table")
    if set(wit) != {"version", "default-features", "features"}:
        raise WorkerError("source-invalid", "wit-bindgen dependency fields are not exact")
    if wit["version"] != "=0.60.0" or wit["default-features"] is not False or wit["features"] != expected_features:
        raise WorkerError("source-invalid", "wit-bindgen pin/features disagree with the accepted toolchain")
    if task is not None:
        if package["name"] != cargo_package_name(task["package_intent"]):
            raise WorkerError("source-invalid", "Cargo package.name does not match task.package_intent.app_id")
        if package["version"] != task["package_intent"]["version"]:
            raise WorkerError("source-invalid", "Cargo package.version does not match task.package_intent.version")


def validate_cargo_scaffold(path: Path, task: dict[str, Any]) -> str:
    """Revalidate worker-owned Cargo bytes after untrusted provider execution."""
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise WorkerError("source-invalid", "deterministic Cargo.toml scaffold is missing") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o444
    ):
        raise WorkerError("source-invalid", "deterministic Cargo.toml scaffold metadata was modified")
    expected = canonical_cargo_manifest(task)
    if metadata.st_size != len(expected):
        raise WorkerError("source-invalid", "deterministic Cargo.toml scaffold size was modified")
    actual = path.read_bytes()
    expected_digest = sha256_bytes(expected)
    actual_digest = sha256_bytes(actual)
    if actual != expected or actual_digest != expected_digest:
        raise WorkerError("source-invalid", "deterministic Cargo.toml scaffold bytes were modified")
    validate_cargo_manifest(path, task)
    return actual_digest


def audit_workspace(workspace: Path, task: dict[str, Any], provider_result: dict[str, Any], contract_digest: str) -> list[dict[str, Any]]:
    authoring_inputs = consent_bound_authoring_inputs(task)
    allowed_top = {"contracts", "source", "provider-last-message.json"}
    actual_top = {item.name for item in workspace.iterdir()}
    if not actual_top <= allowed_top:
        raise WorkerError("source-invalid", f"CodeAgent created unexpected workspace entries: {sorted(actual_top - allowed_top)}")
    contract_directory = workspace / "contracts"
    if contract_directory.is_symlink() or not contract_directory.is_dir():
        raise WorkerError("source-invalid", "contracts/ must remain a real directory")
    if sha256_file(workspace / "contracts/contract.wit") != contract_digest:
        raise WorkerError("integrity-failure", "CodeAgent modified the authoritative input contract")
    contract_entries = list((workspace / "contracts").iterdir())
    expected_contract_names = {"contract.wit"} | {Path(path).name for path in authoring_inputs if path.startswith("contracts/")}
    if {item.name for item in contract_entries} != expected_contract_names or any(item.is_symlink() for item in contract_entries):
        raise WorkerError("source-invalid", "contracts/ contains an unexpected provider-created entry")
    for relative, expected in authoring_inputs.items():
        path = workspace / relative
        try:
            metadata = path.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o444
                    or metadata.st_size != len(expected) or path.read_bytes() != expected):
                raise WorkerError("integrity-failure", "CodeAgent changed immutable authoring input")
        except OSError as error:
            raise WorkerError("integrity-failure", "CodeAgent removed immutable authoring input") from error
    source = workspace / "source"
    if not source.is_dir() or source.is_symlink():
        raise WorkerError("source-invalid", "source/ must remain a real directory")
    allowed_exact = {"Cargo.toml", "README.md", "manifest.intent.json", "wit/contract.wit"}
    records: list[dict[str, Any]] = []
    total_bytes = 0
    actual_paths: list[str] = []
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        for directory in list(directories):
            directory_path = root_path / directory
            metadata = directory_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise WorkerError("source-invalid", f"source tree contains unsafe directory: {directory_path}")
            relative_directory = directory_path.relative_to(source).as_posix()
            normalized_source_path(
                relative_directory,
                source,
                f"audited source directory {relative_directory}",
            )
            if relative_directory.startswith(".") or directory in {"target", ".git", ".cargo", "node_modules"}:
                raise WorkerError("source-invalid", f"source tree contains forbidden directory: {relative_directory}")
        for filename in files:
            path = root_path / filename
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
                raise WorkerError("source-invalid", f"source tree contains non-regular or linked file: {path}")
            if metadata.st_mode & 0o111:
                raise WorkerError("source-invalid", f"source file is executable: {path}")
            if metadata.st_size == 0:
                raise WorkerError("source-invalid", f"source file is empty: {path}")
            relative = path.relative_to(source).as_posix()
            normalized, resolved = normalized_source_path(relative, source, f"audited source path {relative}")
            if normalized != relative or resolved != path.resolve(strict=True):
                raise WorkerError("source-invalid", f"source path does not resolve canonically below source/: {relative}")
            pure = PurePosixPath(relative)
            allowed = relative in allowed_exact or (
                len(pure.parts) >= 2 and pure.parts[0] in {"src", "tests"} and pure.suffix == ".rs"
            )
            if not allowed or filename in {"build.rs", "Cargo.lock"}:
                raise WorkerError("source-invalid", f"source path is outside the handoff allowlist: {relative}")
            total_bytes += metadata.st_size
            if total_bytes > task["limits"]["source_bytes"]:
                raise WorkerError("source-output-limit", "source tree exceeds the task byte limit")
            actual_paths.append(relative)
            records.append(
                {
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size_bytes": metadata.st_size,
                    "role": "authoritative-contract-copy" if relative == "wit/contract.wit" else "generated-source",
                }
            )
    if len(records) > task["limits"]["source_files"]:
        raise WorkerError("source-output-limit", "source tree exceeds the task file-count limit")
    if "Cargo.toml" not in actual_paths or "src/lib.rs" not in actual_paths:
        raise WorkerError(
            "source-invalid",
            "source tree requires physical Cargo.toml and src/lib.rs; "
            f"observed={sorted(actual_paths)} reported={provider_result['source_files'][:8]}",
        )
    if sha256_file(source / "wit/contract.wit") != contract_digest:
        raise WorkerError("integrity-failure", "source WIT is not the authoritative contract copy")
    reported_paths = provider_result["source_files"]
    if sorted(actual_paths) != sorted(reported_paths):
        raise WorkerError("provider-schema-invalid", "provider source_files does not exactly inventory source/")
    validate_cargo_scaffold(source / "Cargo.toml", task)
    rust_sources: dict[str, str] = {}
    for record in records:
        relative = record["path"]
        if not relative.endswith(".rs"):
            continue
        try:
            rust_sources[relative] = (source / relative).read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise WorkerError("source-invalid", f"{relative} must be UTF-8") from error
        if "guest::ErrorCode" in rust_sources[relative]:
            raise WorkerError(
                "source-invalid",
                f"{relative} uses guest::ErrorCode; wit-bindgen 0.60 owns ErrorCode in "
                "vibapp::experimental_v0::common",
            )
    library_source = rust_sources["src/lib.rs"]
    required_no_std_markers = ("#![no_std]", "extern crate alloc", "#[global_allocator]", "#[panic_handler]")
    missing_no_std = [marker for marker in required_no_std_markers if marker not in library_source]
    if missing_no_std or "extern crate std" in library_source or "std::" in library_source:
        raise WorkerError(
            "source-invalid",
            f"src/lib.rs violates the no_std/alloc source policy missing={missing_no_std}",
        )
    validate_generated_semantics(task, rust_sources)
    records.sort(key=lambda record: record["path"].encode("utf-8"))
    return records


def source_tree_digest(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"VIBAPP-CODEAGENT-SOURCE\0")
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(record["sha256"]))
        digest.update(record["size_bytes"].to_bytes(8, "big"))
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    payload = canonical_json(value) + b"\n"
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def claim_consent(output_root: Path, task: dict[str, Any]) -> Path:
    consent_root = safe_private_directory(output_root / "consents")
    claim = consent_root / f"{task['consent']['consent_id']}.json"
    value = {
        "schema_version": CONSENT_CLAIM_VERSION,
        "consent_id": task["consent"]["consent_id"],
        "job_id": task["job_id"],
        "attempt_id": task["execution_attempt"]["attempt_id"],
        "immutable_task_digest_sha256": task["immutable_task_digest_sha256"],
        "provider_execution_identity_sha256": task["provider_execution_identity"]["identity_sha256"],
        "policy_version": POLICY_VERSION,
        "contract_digest_sha256": CONTRACT_DIGEST_PIN,
        "instructions_digest_sha256": provider_instructions_digest(task["provider"], task),
        "provider": task["provider"],
        "model": task["model"],
        "state": "consumed-before-provider-start",
        "consumed_at_utc": now_utc(),
    }
    try:
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as error:
        raise WorkerError("consent-replayed", "single-use remote-processing consent was already consumed") from error
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(canonical_json(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return claim


def install_dry_run_fixture(workspace: Path, task: dict[str, Any]) -> None:
    if (DRY_RUN_SOURCE / "Cargo.toml").read_bytes() != canonical_cargo_manifest(task):
        raise WorkerError("integrity-failure", "checked-in dry-run Cargo scaffold differs from task-derived bytes")
    for source in sorted(DRY_RUN_SOURCE.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(DRY_RUN_SOURCE)
        if relative.as_posix() == "Cargo.toml":
            continue
        copy_regular_file(source, workspace / "source" / relative, mode=0o444)
    copy_regular_file(DRY_RUN_RESULT, workspace / "provider-last-message.json", mode=0o444)


def persist_handoff(
    output_root: Path,
    workspace: Path,
    task: dict[str, Any],
    records: list[dict[str, Any]],
    provider_execution: dict[str, Any],
) -> Path:
    runs = safe_private_directory(output_root / "runs")
    final = runs / task["job_id"]
    if final.exists() or final.is_symlink():
        raise WorkerError("conflict", "a run already exists for this job_id")
    staging = Path(tempfile.mkdtemp(prefix=f".{task['job_id']}-", dir=runs))
    try:
        destination_source = staging / "source"
        destination_source.mkdir(mode=0o700)
        for record in records:
            relative, source_path = normalized_source_path(
                record["path"],
                workspace / "source",
                "handoff file path",
            )
            destination_relative, destination_path = normalized_source_path(
                relative,
                destination_source,
                "handoff destination path",
            )
            if relative != destination_relative:
                raise WorkerError("source-invalid", "handoff path normalization changed the source path")
            copy_regular_file(source_path, destination_path, mode=0o444)
        handoff = {
            "schema_version": SOURCE_HANDOFF_VERSION,
            "document_type": "codeagent-source-handoff",
            "status": "untrusted-source-awaiting-builder",
            "job_id": task["job_id"],
            "need_spec_digest_sha256": task["need_spec_digest_sha256"],
            "source_tree_sha256": source_tree_digest(records),
            "source_directory": "source",
            "files": records,
            "package_intent": task["package_intent"],
            "target": task["target"],
            "provider_execution": provider_execution,
            "authority": {
                "compile": "separate-builder",
                "verify": "separate-verifier",
                "install": "none",
                "publish": "none",
            },
            "created_at_utc": now_utc(),
        }
        atomic_json(staging / "handoff.json", handoff)
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final / "handoff.json"


class CloudAgentWorker:
    def __init__(
        self,
        codex_bin: Path = CODEX_BIN,
        provider_runner: ProviderRunner | None = None,
        executable_pin: dict[str, str] | None = None,
        clock: dt.datetime | None = None,
        accepted_runner: AcceptedRunnerProfile | None = None,
        signature_verifier: ReceiptSignatureVerifier | None = None,
    ):
        self.codex_bin = codex_bin
        self.provider_runner = provider_runner or UnavailableProviderRunner()
        self.executable_pin = executable_pin or CODEX_PIN
        self.clock = clock
        self.accepted_runner = accepted_runner
        self.signature_verifier = signature_verifier

    def execute(
        self,
        task_path: Path,
        output_root: Path,
        *,
        dry_run: bool,
        confirm_job: str | None = None,
        confirm_consent: str | None = None,
        acknowledge_external_cost: bool = False,
    ) -> Path:
        task = validate_task_file(task_path, self.clock)
        output_root = safe_private_directory(output_root)
        executable_identity: dict[str, Any] | None = None
        isolation_policy: dict[str, Any] | None = None
        if not dry_run:
            if confirm_job != task["job_id"]:
                raise WorkerError("external-opt-in-required", "--confirm-job must exactly match job_id")
            if confirm_consent != task["consent"]["consent_id"]:
                raise WorkerError("external-opt-in-required", "--confirm-consent must exactly match consent_id")
            if not acknowledge_external_cost:
                raise WorkerError("external-opt-in-required", "--acknowledge-external-cost is required")
            executable_identity = resolve_executable_identity(self.codex_bin, self.executable_pin)
            isolation_policy = required_isolation_policy(task)
            if self.accepted_runner is None or self.signature_verifier is None:
                if type(self.provider_runner) is UnavailableProviderRunner:
                    self.provider_runner.preflight(isolation_policy)
                raise WorkerError(
                    "runner-attestation-unavailable",
                    "live mode requires an independently accepted runner identity and receipt verifier",
                )
            validate_accepted_runner_profile(self.accepted_runner)
            validate_runner_attestation(self.provider_runner.preflight(isolation_policy), isolation_policy)
            validate_resolved_provider_identity(task, executable_identity)
        workspaces = safe_private_directory(output_root / "workspaces")
        workspace = Path(tempfile.mkdtemp(prefix=f"{task['job_id']}-", dir=workspaces))
        try:
            contract_digest = prepare_workspace(workspace, task)
            if dry_run:
                install_dry_run_fixture(workspace, task)
                provider_execution = {
                    "mode": "dry-run-fixture",
                    "adapter": "local-static-fixture",
                    "provider_id": "static-fixture",
                    "external_request_attempted": False,
                    "external_request_observed": False,
                    "gateway_request_id": None,
                    "executable_sha256": None,
                    "isolation_policy_version": None,
                    "runner_id": None,
                    "runner_identity_sha256": None,
                    "isolation_policy_sha256": None,
                    "receipt_digest_sha256": None,
                }
            else:
                claim_consent(output_root, task)
                assert executable_identity is not None and isolation_policy is not None
                command = codex_command(
                    Path(executable_identity["resolved_path"]),
                    workspace,
                    model=task["model"],
                )
                isolation_policy_digest = sha256_bytes(canonical_json(isolation_policy))
                request = ProviderRunnerRequest(
                    schema_version=RUNNER_REQUEST_VERSION,
                    document_type="provider-runner-request",
                    job_id=task["job_id"],
                    execution_attempt=task["execution_attempt"],
                    immutable_task_digest_sha256=task["immutable_task_digest_sha256"],
                    provider_execution_identity=task["provider_execution_identity"],
                    provider_execution_identity_sha256=task["provider_execution_identity"]["identity_sha256"],
                    provider=task["provider"],
                    model=task["model"],
                    executable_identity=executable_identity,
                    command=command,
                    prompt=create_prompt(task, contract_digest),
                    workspace=workspace,
                    limits=task["limits"],
                    isolation_policy=isolation_policy,
                    isolation_policy_sha256=isolation_policy_digest,
                    input_bundle_sha256=workspace_tree_digest(workspace),
                )
                validate_runner_request_model(request)
                receipt = validate_provider_receipt(
                    self.provider_runner.execute(request),
                    request,
                    self.accepted_runner,
                    self.signature_verifier,
                )
                if not receipt.external_request_attempted:
                    raise WorkerError("provider-failed", "provider runner returned without attempting the request")
                if not receipt.external_request_observed or not receipt.gateway_request_id:
                    raise WorkerError(
                        "provider-egress-unobserved",
                        "provider execution lacks trusted gateway egress observation",
                        external_request_attempted=True,
                    )
                if receipt.executed_executable_sha256 != executable_identity["sha256"]:
                    raise WorkerError(
                        "executable-identity-invalid",
                        "provider runner did not attest execution of the reviewed executable digest",
                        external_request_attempted=True,
                        external_request_observed=True,
                        gateway_request_id=receipt.gateway_request_id,
                    )
                if receipt.exit_code != 0:
                    raise WorkerError(
                        "provider-failed",
                        f"isolated provider runner exited nonzero ({receipt.exit_code})",
                        external_request_attempted=True,
                        external_request_observed=True,
                        gateway_request_id=receipt.gateway_request_id,
                    )
                if receipt.stdout_bytes > task["limits"]["stdout_bytes"] or receipt.stderr_bytes > task["limits"]["stderr_bytes"]:
                    raise WorkerError(
                        "provider-output-limit",
                        "isolated runner receipt reports an output limit violation",
                        external_request_attempted=True,
                        external_request_observed=True,
                        gateway_request_id=receipt.gateway_request_id,
                    )
                workspace_usage(workspace, task["limits"])
                if receipt.output_bundle_sha256 != workspace_tree_digest(workspace):
                    raise WorkerError(
                        "integrity-failure",
                        "provider runner output bundle digest does not match the quiescent staging workspace",
                        external_request_attempted=True,
                        external_request_observed=True,
                        gateway_request_id=receipt.gateway_request_id,
                    )
                provider_execution = {
                    "mode": "live-explicit-opt-in",
                    "adapter": "external-isolated-provider-runner",
                    "provider_id": "codex",
                    "external_request_attempted": True,
                    "external_request_observed": True,
                    "gateway_request_id": receipt.gateway_request_id,
                    "executable_sha256": executable_identity["sha256"],
                    "isolation_policy_version": ISOLATION_POLICY_VERSION,
                    "runner_id": receipt.runner_id,
                    "runner_identity_sha256": receipt.runner_identity_sha256,
                    "isolation_policy_sha256": receipt.isolation_policy_sha256,
                    "receipt_digest_sha256": receipt.receipt_digest_sha256,
                }
            provider_path = workspace / "provider-last-message.json"
            provider_result = validate_provider_result(
                load_json(provider_path, min(MAX_PROVIDER_RESULT_BYTES, task["limits"]["stdout_bytes"]), "provider result"),
                task,
                workspace / "source",
            )
            records = audit_workspace(workspace, task, provider_result, contract_digest)
            return persist_handoff(output_root, workspace, task, records, provider_execution)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


def json_status(status: str, **values: Any) -> None:
    print(json.dumps({"status": status, **values}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded VibApp cloud CodeAgent source worker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    schema_validate = subcommands.add_parser(
        "schema-validate",
        help="validate a versioned task against its authoritative schema and product semantics without consuming consent",
    )
    schema_validate.add_argument("task", type=Path)
    validate = subcommands.add_parser("validate", help="validate schema, completeness, digest and consent without external work")
    validate.add_argument("task", type=Path)
    dry_run = subcommands.add_parser("dry-run", help="exercise deterministic fixture handoff without a provider request")
    dry_run.add_argument("task", type=Path)
    dry_run.add_argument("--output-root", type=Path, required=True)
    run = subcommands.add_parser("run", help="make one explicitly confirmed real Codex request")
    run.add_argument("task", type=Path)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--confirm-job", required=True)
    run.add_argument("--confirm-consent", required=True)
    run.add_argument("--acknowledge-external-cost", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "schema-validate":
            task = validate_task_schema(load_json(arguments.task, MAX_TASK_BYTES, "cloud task"))
            json_status(
                "schema-valid-task",
                schema_version=task["schema_version"],
                job_id=task["job_id"],
                external_request_attempted=False,
                external_request_observed=False,
                gateway_request_id=None,
            )
            return 0
        if arguments.command == "validate":
            task = validate_task_file(arguments.task)
            json_status(
                "valid-consented-task",
                job_id=task["job_id"],
                external_request_attempted=False,
                external_request_observed=False,
                gateway_request_id=None,
            )
            return 0
        worker = CloudAgentWorker()
        if arguments.command == "dry-run":
            handoff = worker.execute(arguments.task, arguments.output_root, dry_run=True)
        else:
            handoff = worker.execute(
                arguments.task,
                arguments.output_root,
                dry_run=False,
                confirm_job=arguments.confirm_job,
                confirm_consent=arguments.confirm_consent,
                acknowledge_external_cost=arguments.acknowledge_external_cost,
            )
        created = load_json(handoff, MAX_PROVIDER_RESULT_BYTES, "source handoff")
        execution = created["provider_execution"]
        json_status(
            "source-handoff-created",
            handoff=str(handoff),
            external_request_attempted=execution["external_request_attempted"],
            external_request_observed=execution["external_request_observed"],
            gateway_request_id=execution["gateway_request_id"],
        )
        return 0
    except WorkerError as error:
        json_status(
            "rejected",
            code=error.code,
            message=str(error),
            external_request_attempted=error.external_request_attempted,
            external_request_observed=error.external_request_observed,
            gateway_request_id=error.gateway_request_id,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
