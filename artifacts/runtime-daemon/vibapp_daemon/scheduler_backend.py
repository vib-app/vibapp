"""Host-owned alarm ledger; no timers, guest execution, or implicit OS adapter.

S1 integration contract:
* Construct over the *current* core transaction state. Mutations replace only its
  ``scheduler`` member; never re-enter core.execute/_locked_state from a host call.
* Commit advance_due/claim_* before external I/O. Adapter put uses the persisted
  app-namespaced adapter_id and projection (notification_id is the logical audit
  ID); finish_notification is a separate host transaction.
* Run a guest against staged KV. Its acknowledge call and finish_guest(success)
  must commit in the SAME core transaction as that KV, or both must roll back.
* owner is a unique daemon incarnation, NOT a PID. recover_owner is allowed only
  after the coordinator has proved that incarnation and its effects are quiescent.
* host_context is privileged lifecycle/permission input, never guest-controlled.
  A view close has no operation here. Unknown notification permission is denied.

The default notification adapter is unavailable. Importing this module does not
advertise a capability, start a pump, or constitute macOS notification acceptance.
PrivateJsonStore is a small standalone/test owner, not a second core lock/store.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "vibapp.scheduler-ledger.experimental-v1"
UTC = dt.timezone.utc
GRACE_SECONDS = 900
RETRY_OFFSETS = (0, 2, 10)
MAX_BYTES = 8 * 1024 * 1024
MAX_APPS = 64
MAX_SCHEDULES = 64
MAX_ROWS = 4096
MAX_PAYLOAD = 16 * 1024
MAX_BATCH = 128
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class SchedulerError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _error(code: str, message: str) -> None:
    raise SchedulerError(code, message)


def _object(value: Any, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        _error("invalid-argument", "record fields do not match the frozen scheduler contract")
    return value


def _text(value: Any, name: str, limit: int = 256, *, empty: bool = False) -> str:
    try:
        valid = isinstance(value, str) and (empty or bool(value)) and len(value.encode("utf-8")) <= limit and not any(ord(c) < 32 for c in value)
    except UnicodeError:
        valid = False
    if not valid:
        _error("invalid-argument", f"invalid or oversized {name}")
    return value


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _error("invalid-argument", "integer outside supported range")
    return value


def parse_utc(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        _error("invalid-argument", "timestamp must be canonical YYYY-MM-DDTHH:MM:SSZ")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        _error("invalid-argument", "timestamp is not a calendar instant")


def utc(value: dt.datetime) -> str:
    # strftime %Y is not four digits for years < 1000 on all supported systems.
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value: Any) -> dt.date:
    _object(value, {"year", "month", "day"})
    try:
        return dt.date(_integer(value["year"], 1, 9999), _integer(value["month"], 1, 12), _integer(value["day"], 1, 31))
    except ValueError:
        _error("invalid-argument", "invalid civil date")


def _time(value: Any) -> dt.time:
    _object(value, {"hour", "minute", "second"})
    return dt.time(_integer(value["hour"], 0, 23), _integer(value["minute"], 0, 59), _integer(value["second"], 0, 59))


def _zone(name: Any) -> ZoneInfo:
    _text(name, "IANA time zone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        _error("invalid-argument", "unknown IANA time zone")


def resolve_civil(date: dt.date, time: dt.time, zone: ZoneInfo) -> dt.datetime:
    """Earlier fold; nonexistent local time shifted by the exact transition gap."""
    local = dt.datetime.combine(date, time)
    try:
        choices = [local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
        valid = [v for v in choices if v.astimezone(zone).replace(tzinfo=None) == local]
    except (OverflowError, ValueError):
        _error("invalid-argument", "civil time exceeds representable UTC calendar")
    if valid:
        return min(valid)
    # PEP 495 gives both sides of a gap. The forward round trip shifts by the
    # entire gap (including non-hour/date-line transitions), not to its boundary.
    forward = [v for v in choices if v.astimezone(zone).replace(tzinfo=None) > local]
    if not forward:
        _error("invalid-argument", "civil gap cannot be resolved")
    return min(forward)


def validate_schedule(schedule: Any) -> dict:
    _object(schedule, {"tag", "value"})
    value = schedule["value"]
    if schedule["tag"] == "one-shot":
        _object(value, {"when", "gap", "overlap"})
        when = _object(value["when"], {"date", "time", "time_zone"})
        _date(when["date"]); _time(when["time"]); _zone(when["time_zone"])
    elif schedule["tag"] == "recurring":
        _object(value, {"kind", "time", "start_date", "time_zone", "weekdays", "gap", "overlap"})
        _date(value["start_date"]); _time(value["time"]); _zone(value["time_zone"])
        days = value["weekdays"]
        if not isinstance(days, list) or len(days) > 7 or any(day not in WEEKDAYS for day in days) or len(set(days)) != len(days):
            _error("invalid-argument", "weekdays must be unique enum names")
        if value["kind"] not in ("daily", "weekly") or (value["kind"] == "daily" and days) or (value["kind"] == "weekly" and not days):
            _error("invalid-argument", "unsupported recurrence or weekday set")
    else:
        _error("invalid-argument", "unsupported alarm schedule")
    if value["gap"] != "next-valid" or value["overlap"] != "earlier":
        _error("invalid-argument", "Stage 0 requires next-valid gap and earlier overlap")
    return copy.deepcopy(schedule)


def validate_projection(value: Any) -> dict:
    _object(value, {"title", "body", "standard_sound"})
    _text(value["title"], "notification title", 1024)
    # Body may contain line breaks, but not arbitrary extra fields/opaque data.
    try:
        valid_body = isinstance(value["body"], str) and len(value["body"].encode("utf-8")) <= 8192
    except UnicodeError:
        valid_body = False
    if not valid_body or type(value["standard_sound"]) is not bool:
        _error("invalid-argument", "invalid notification projection")
    return copy.deepcopy(value)


def next_civil(schedule: dict, after: dt.datetime, *, after_date: str | None = None) -> tuple[str, str] | None:
    value = schedule["value"]
    if schedule["tag"] == "one-shot":
        when = value["when"]
        date = _date(when["date"])
        resolved = resolve_civil(date, _time(when["time"]), _zone(when["time_zone"]))
        return (utc(resolved), date.isoformat()) if resolved > after else None
    zone = _zone(value["time_zone"])
    try:
        date = max(_date(value["start_date"]), dt.date.fromisoformat(after_date) + dt.timedelta(days=1)) if after_date else max(_date(value["start_date"]), after.astimezone(zone).date())
        # At most the next week is needed; gap/fold policy remains civil-based.
        for _ in range(9):
            if value["kind"] == "daily" or WEEKDAYS[date.weekday()] in value["weekdays"]:
                resolved = resolve_civil(date, _time(value["time"]), zone)
                if resolved > after:
                    return utc(resolved), date.isoformat()
            date += dt.timedelta(days=1)
    except (OverflowError, ValueError):
        _error("resource-limit", "recurrence exceeds representable calendar")
    _error("invalid-argument", "no supported civil occurrence")


def _encode(value: Any) -> bytes:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _error("integrity-failure", "scheduler state is not bounded JSON")
    if len(result) > MAX_BYTES:
        _error("resource-limit", "scheduler ledger byte budget exhausted")
    return result


def _empty() -> dict:
    return {"schema_version": SCHEMA, "apps": {}}


class SchedulerBackend:
    def __init__(self, current_state: dict, *, tzdata_label: str):
        self.state = current_state
        self.tzdata_label = _text(tzdata_label, "trusted tzdata label")
        self._check_layout(current_state.get("scheduler", _empty()))

    @staticmethod
    def _check_layout(ledger: Any) -> None:
        if not isinstance(ledger, dict) or set(ledger) != {"schema_version", "apps"} or ledger["schema_version"] != SCHEMA or not isinstance(ledger["apps"], dict):
            _error("integrity-failure", "unsupported scheduler ledger")
        collections = {"alarms", "occurrences", "outbox", "guest_events", "commands", "actions"}
        for row in ledger["apps"].values():
            if not isinstance(row, dict) or set(row) != collections | {"context"} or any(not isinstance(row[name], dict) for name in collections):
                _error("integrity-failure", "invalid persisted app scheduler record")
            context = row["context"]
            if not isinstance(context, dict) or set(context) != {"active_generation", "enabled", "scheduler_granted", "notification_permission", "rollback_state"}:
                _error("integrity-failure", "invalid persisted scheduler authority context")
            if type(context["enabled"]) is not bool or type(context["scheduler_granted"]) is not bool or context["notification_permission"] not in ("authorized", "denied", "not-determined") or context["rollback_state"] not in ("none", "observation-window", "rolling-back"):
                _error("integrity-failure", "invalid persisted scheduler authority values")
            required = {
                "alarms": {"alarm_id", "schedule_id", "schedule", "notification", "enabled", "revision", "next_occurrence_id", "permission_health", "purpose", "payload", "missed", "spec"},
                "occurrences": {"occurrence", "alarm_id", "revision", "due_at_utc", "civil_date", "tzdata_label", "state", "classification", "notification_id", "parent_occurrence_id"},
                "outbox": {"notification_id", "adapter_id", "occurrence_id", "projection", "state", "first_eligible_at_utc", "first_attempt_tolerance", "adapter_attempt_count", "attempts", "claim"},
                "guest_events": {"guest_event_id", "occurrence_id", "schedule", "key", "scheduled_for_utc", "classification", "payload", "state", "attempts", "claim", "routed_generation", "dispatched_at_utc", "ack_requested"},
                "commands": {"fingerprint", "response"},
                "actions": {"os_action_id", "parent_occurrence_id", "snooze_occurrence_id", "duration_seconds", "action_received_at_utc", "deduplicated", "result"},
            }
            for name, fields in required.items():
                if any(not isinstance(value, dict) or not fields.issubset(value) for value in row[name].values()):
                    _error("integrity-failure", f"incomplete persisted scheduler {name} record")
        _encode(ledger)

    @contextlib.contextmanager
    def _edit(self) -> Iterator[dict]:
        original = self.state.get("scheduler", _empty())
        self._check_layout(original)
        ledger = copy.deepcopy(original)
        yield ledger
        if len(ledger["apps"]) > MAX_APPS:
            _error("resource-limit", "scheduler app limit reached")
        for app in ledger["apps"].values():
            for name in ("alarms", "occurrences", "outbox", "guest_events", "commands", "actions"):
                if len(app[name]) > (MAX_SCHEDULES if name == "alarms" else MAX_ROWS):
                    _error("resource-limit", f"scheduler {name} limit reached; audit is not silently evicted")
        _encode(ledger)
        self.state["scheduler"] = ledger

    def _app(self, app: str, ledger: dict | None = None) -> dict:
        _text(app, "app ID")
        value = (ledger or self.state.get("scheduler", _empty()))["apps"].get(app)
        if value is None:
            _error("not-found", "scheduler app context absent")
        return value

    def host_context(self, app: str, *, active_generation: str | None, enabled: bool,
                     scheduler_granted: bool, notification_permission: str,
                     rollback_state: str = "none") -> None:
        """Privileged core transaction hook, not a public or guest RPC method."""
        _text(app, "app ID")
        if active_generation is not None:
            _text(active_generation, "generation")
        if type(enabled) is not bool or type(scheduler_granted) is not bool or notification_permission not in ("authorized", "denied", "not-determined") or rollback_state not in ("none", "observation-window", "rolling-back"):
            _error("invalid-argument", "invalid trusted app context")
        with self._edit() as ledger:
            row = ledger["apps"].setdefault(app, {name: {} for name in ("alarms", "occurrences", "outbox", "guest_events", "commands", "actions")})
            row["context"] = {"active_generation": active_generation, "enabled": enabled,
                "scheduler_granted": scheduler_granted, "notification_permission": notification_permission,
                "rollback_state": rollback_state}
            for alarm in row["alarms"].values():
                alarm["permission_health"] = notification_permission
                if not enabled and alarm["enabled"]:
                    self._disable(row, alarm)

    @staticmethod
    def _authority(row: dict, generation: str | None, *, mutation: bool = True, host_action: bool = False) -> None:
        context = row["context"]
        if not context["enabled"]:
            _error("app-disabled", "app-disabled")
        if generation is not None and generation != context["active_generation"]:
            _error("permission-denied", "inactive-generation")
        if not context["scheduler_granted"]:
            _error("permission-denied", "scheduler permission is not granted")
        if mutation and context["rollback_state"] != "none" and not host_action:
            _error("upgrade-in-progress", "upgrade-in-progress")

    @staticmethod
    def _alarm(row: dict, alarm_id: str) -> dict:
        _text(alarm_id, "alarm ID", 128)
        if alarm_id not in row["alarms"]:
            _error("not-found", "alarm does not exist")
        return row["alarms"][alarm_id]

    @staticmethod
    def _cancel_pending(row: dict, alarm: dict) -> None:
        for occ in row["occurrences"].values():
            if occ["alarm_id"] == alarm["alarm_id"] and occ["state"] == "scheduled":
                occ["state"] = "cancelled"
        alarm["next_occurrence_id"] = None

    def _disable(self, row: dict, alarm: dict) -> str:
        if not alarm["enabled"]:
            return "unchanged"
        self._cancel_pending(row, alarm)
        alarm["enabled"] = False
        alarm["revision"] = _integer(alarm["revision"] + 1, 1, 2**64 - 1)
        return "disabled"

    def _materialize(self, row: dict, alarm: dict, candidate: tuple[str, str], *, occurrence_id: str | None = None, parent: str | None = None) -> str:
        due, civil_date = candidate
        oid = occurrence_id or f"occ:{alarm['alarm_id']}:r{alarm['revision']}:{due}"
        if oid in row["occurrences"]:
            _error("integrity-failure", "attempted to rematerialize an occurrence")
        row["occurrences"][oid] = {"occurrence": oid, "alarm_id": alarm["alarm_id"],
            "revision": alarm["revision"], "due_at_utc": due, "civil_date": civil_date,
            "tzdata_label": self.tzdata_label, "state": "scheduled", "classification": None,
            "notification_id": None, "parent_occurrence_id": parent}
        if parent is None:
            alarm["next_occurrence_id"] = oid
        return oid

    @staticmethod
    def _result(alarm: dict, outcome: str, snooze: str | None = None) -> dict:
        return {"alarm_id": alarm["alarm_id"], "outcome": outcome, "revision": alarm["revision"],
                "enabled": alarm["enabled"], "next_occurrence_id": alarm["next_occurrence_id"],
                "snooze_occurrence_id": snooze, "deduplicated": False}

    def _configure(self, row: dict, request: dict, now: dt.datetime, *, purpose: str = "alarm", payload: list | None = None, missed: str = "fire-once", spec: dict | None = None) -> dict:
        _object(request, {"alarm_id", "expected_revision", "schedule", "notification"})
        alarm_id = _text(request["alarm_id"], "alarm ID", 128)
        schedule = validate_schedule(request["schedule"])
        notification = validate_projection(request["notification"]) if purpose == "alarm" else None
        previous = row["alarms"].get(alarm_id)
        expected = request["expected_revision"]
        if expected is not None:
            _integer(expected, 0, 2**64 - 1)
        if expected is not None and expected != (previous["revision"] if previous else 0):
            _error("stale-revision", "alarm revision does not match")
        if previous and previous["purpose"] != purpose:
            _error("conflict", "schedule purpose cannot be changed")
        candidate = next_civil(schedule, now)
        if candidate is None:
            _error("invalid-argument", "one-shot must resolve strictly after commit")
        if previous:
            self._cancel_pending(row, previous)
        alarm = {"alarm_id": alarm_id, "schedule_id": f"schedule:{alarm_id}", "schedule": schedule,
                 "notification": notification, "enabled": True,
                 "revision": _integer(previous["revision"] + 1 if previous else 1, 1, 2**64 - 1),
                 "next_occurrence_id": None, "permission_health": row["context"]["notification_permission"],
                 "purpose": purpose, "payload": copy.deepcopy(payload or []), "missed": missed, "spec": copy.deepcopy(spec)}
        row["alarms"][alarm_id] = alarm
        self._materialize(row, alarm, candidate)
        return self._result(alarm, "updated" if previous else "created")

    def _toggle(self, row: dict, request: dict, now: dt.datetime, enable: bool) -> dict:
        _object(request, {"alarm_id", "expected_revision"})
        alarm = self._alarm(row, request["alarm_id"])
        if request["expected_revision"] is not None and _integer(request["expected_revision"], 0, 2**64 - 1) != alarm["revision"]:
            _error("stale-revision", "alarm revision does not match")
        outcome = "unchanged"
        if not enable:
            outcome = self._disable(row, alarm)
        elif not alarm["enabled"]:
            candidate = next_civil(alarm["schedule"], now)
            if candidate is None:
                _error("conflict", "expired-one-shot")
            alarm["revision"] = _integer(alarm["revision"] + 1, 1, 2**64 - 1)
            alarm["enabled"] = True
            self._materialize(row, alarm, candidate)
            outcome = "enabled"
        return self._result(alarm, outcome)

    def _snooze(self, row: dict, request: dict) -> dict:
        _object(request, {"parent_occurrence_id", "os_action_id", "action_received_at_utc", "duration_seconds"})
        parent_id = _text(request["parent_occurrence_id"], "parent occurrence", 2048)
        action_id = _text(request["os_action_id"], "OS action ID")
        received = parse_utc(request["action_received_at_utc"])
        duration = _integer(request["duration_seconds"], 60, 3600)
        prior = row["actions"].get(f"action:{action_id}")
        if prior:
            if prior["parent_occurrence_id"] != parent_id or prior["duration_seconds"] != duration:
                _error("conflict", "idempotency-conflict")
            prior["deduplicated"] = True
            result = copy.deepcopy(prior["result"])
            result["deduplicated"] = True
            return result
        parent = row["occurrences"].get(parent_id)
        if not parent or parent["state"] != "notification-accepted":
            _error("conflict", "snooze requires a delivered parent occurrence")
        alarm = self._alarm(row, parent["alarm_id"])
        if not alarm["enabled"] or parent["revision"] != alarm["revision"]:
            _error("conflict", "snooze parent is no longer current and enabled")
        sequence = 1 + sum(a["parent_occurrence_id"] == parent_id for a in row["actions"].values())
        try:
            due = received + dt.timedelta(seconds=duration)
        except OverflowError:
            _error("invalid-argument", "snooze exceeds representable UTC calendar")
        oid = self._materialize(row, alarm, (utc(due), due.date().isoformat()), occurrence_id=f"snooze:{parent_id}:{sequence}", parent=parent_id)
        result = self._result(alarm, "snoozed", oid)
        row["actions"][f"action:{action_id}"] = {"os_action_id": action_id, "parent_occurrence_id": parent_id,
            "snooze_occurrence_id": oid, "duration_seconds": duration, "action_received_at_utc": request["action_received_at_utc"],
            "deduplicated": False, "result": result}
        return result

    def _mutating_call(self, app: str, operation: str, payload: dict, key: str, generation: str | None,
                       callback: Any, *, host_action: bool = False) -> dict:
        """Semantic failures are durable command receipts, not partial mutations."""
        if not isinstance(payload, dict):
            _error("invalid-argument", "command payload must be a record")
        _text(key, "idempotency key")
        semantic_payload = {k: v for k, v in payload.items() if not (operation == "alarm-snooze" and k == "action_received_at_utc")}
        fingerprint = hashlib.sha256(_encode({"operation": operation, "payload": semantic_payload})).hexdigest()
        with self._edit() as ledger:
            row = self._app(app, ledger)
            try:
                self._authority(row, generation, host_action=host_action)
            except SchedulerError as exc:
                # A stale guest cannot replay a prior authorized success.
                response = {"ok": False, "error": exc.as_dict()}
                row["commands"].setdefault(f"cmd:{key}", {"fingerprint": fingerprint, "response": copy.deepcopy(response)})
                return response
            previous = row["commands"].get(f"cmd:{key}")
            if previous:
                if previous["fingerprint"] != fingerprint:
                    return {"ok": False, "error": SchedulerError("conflict", "idempotency-conflict").as_dict()}
                result = copy.deepcopy(previous["response"])
                if result["ok"] and isinstance(result["value"], dict) and "deduplicated" in result["value"]:
                    result["value"]["deduplicated"] = True
                if operation == "alarm-snooze" and f"action:{payload.get('os_action_id')}" in row["actions"]:
                    row["actions"][f"action:{payload['os_action_id']}"]["deduplicated"] = True
                return result
            updated = copy.deepcopy(row)
            try:
                response = {"ok": True, "value": callback(updated)}
            except SchedulerError as exc:
                response = {"ok": False, "error": exc.as_dict()}
            else:
                row = updated
                ledger["apps"][app] = row
            row["commands"][f"cmd:{key}"] = {"fingerprint": fingerprint, "response": copy.deepcopy(response)}
        return copy.deepcopy(response)

    def command(self, app: str, command: dict, *, idempotency_key: str, now_utc: str,
                caller_generation: str | None = None, host_action: bool = False) -> dict:
        try:
            _object(command, {"tag", "value"})
            tag, value = command["tag"], command["value"]
            now = parse_utc(now_utc)
            if tag == "alarm-status":
                _object(value, {"alarm_id", "occurrence_limit", "effect_limit"})
                return {"ok": True, "value": self.status(app, **value)}
            if tag == "alarm-snooze" and (not host_action or caller_generation is not None):
                _error("permission-denied", "snooze requires a host-verified notification action")
            if tag not in ("alarm-configure", "alarm-enable", "alarm-disable", "alarm-cancel", "alarm-snooze"):
                _error("invalid-argument", "unsupported alarm command")
            def apply(row: dict) -> dict:
                if tag == "alarm-configure":
                    return self._configure(row, value, now)
                if tag == "alarm-snooze":
                    return self._snooze(row, value)
                return self._toggle(row, value, now, tag == "alarm-enable")
            return self._mutating_call(app, tag, value, idempotency_key, caller_generation, apply, host_action=tag == "alarm-snooze")
        except SchedulerError as exc:
            return {"ok": False, "error": exc.as_dict()}

    def advance_due(self, now_utc: str, *, recovery: bool = False, limit: int = MAX_BATCH) -> dict:
        now = parse_utc(now_utc)
        _integer(limit, 1, MAX_BATCH)
        if type(recovery) is not bool:
            _error("invalid-argument", "recovery must be a trusted boolean")
        processed = []
        with self._edit() as ledger:
            for _ in range(limit):
                candidates = [(o["due_at_utc"], app_id, oid) for app_id, row in ledger["apps"].items()
                    for oid, o in row["occurrences"].items() if o["state"] == "scheduled" and o["due_at_utc"] <= now_utc]
                if not candidates:
                    break
                _, app_id, oid = min(candidates)
                row = ledger["apps"][app_id]
                occ = row["occurrences"][oid]
                alarm = row["alarms"][occ["alarm_id"]]
                if not row["context"]["enabled"] or not row["context"]["scheduler_granted"] or not alarm["enabled"] or alarm["revision"] != occ["revision"]:
                    occ["state"] = "cancelled"
                    if alarm["next_occurrence_id"] == oid:
                        alarm["next_occurrence_id"] = None
                    processed.append(oid)
                    continue
                lateness = (now - parse_utc(occ["due_at_utc"])).total_seconds()
                missed = lateness > GRACE_SECONDS or (alarm["purpose"] == "service-trigger" and alarm["missed"] == "skip" and recovery)
                classification = "missed" if missed else ("catch-up" if recovery else "on-time")
                permission = row["context"]["notification_permission"]
                if not missed and alarm["purpose"] == "alarm" and permission != "authorized":
                    classification = "permission-denied"
                occ["classification"] = classification
                occ["state"] = classification if classification in ("missed", "permission-denied") else ("delivery-pending" if alarm["purpose"] == "alarm" else "guest-pending")
                if occ["state"] == "delivery-pending":
                    nid = f"notify:{oid}"
                    occ["notification_id"] = nid
                    row["outbox"][nid] = {"notification_id": nid, "adapter_id": "vibapp:" + hashlib.sha256(_encode([app_id, nid])).hexdigest(), "occurrence_id": oid,
                        "projection": copy.deepcopy(alarm["notification"]), "state": "pending",
                        "first_eligible_at_utc": now_utc, "first_attempt_tolerance": 5 if recovery else 2,
                        "adapter_attempt_count": 0, "attempts": [], "claim": None}
                gid = f"guest:{oid}"
                row["guest_events"][gid] = {"guest_event_id": gid, "occurrence_id": oid,
                    "schedule": alarm["schedule_id"], "key": alarm["alarm_id"], "scheduled_for_utc": occ["due_at_utc"],
                    "classification": classification, "payload": copy.deepcopy(alarm["payload"]),
                    "state": "pending", "attempts": 0, "claim": None, "routed_generation": None,
                    "dispatched_at_utc": None, "ack_requested": None}
                if occ["parent_occurrence_id"] is None:
                    alarm["next_occurrence_id"] = None
                    if alarm["schedule"]["tag"] == "recurring":
                        candidate = next_civil(alarm["schedule"], parse_utc(occ["due_at_utc"]), after_date=occ["civil_date"])
                        self._materialize(row, alarm, candidate)
                processed.append(oid)
            more = any(o["state"] == "scheduled" and o["due_at_utc"] <= now_utc for row in ledger["apps"].values() for o in row["occurrences"].values())
        return {"processed": processed, "more_due": more}

    @staticmethod
    def _failed_notification(row: dict, effect: dict, reason: str) -> None:
        effect["state"], effect["claim"], effect["failure"] = "failed", None, reason
        row["occurrences"][effect["occurrence_id"]]["state"] = "notification-failed"

    def claim_notification(self, app: str, *, owner: str, now_utc: str) -> dict | None:
        _text(owner, "daemon incarnation")
        now = parse_utc(now_utc)
        result = None
        with self._edit() as ledger:
            row = self._app(app, ledger)
            for effect in sorted(row["outbox"].values(), key=lambda e: (e["first_eligible_at_utc"], e["notification_id"])):
                if effect["state"] != "pending" or effect["claim"] is not None:
                    continue
                occ = row["occurrences"][effect["occurrence_id"]]
                due = parse_utc(occ["due_at_utc"])
                if now < due:
                    continue
                if row["context"]["notification_permission"] != "authorized":
                    self._failed_notification(row, effect, "permission-denied")
                    continue
                count = effect["adapter_attempt_count"]
                first = parse_utc(effect["first_eligible_at_utc"])
                # Recovery at due+900 may make its FIRST attempt by readiness+5.
                # Remaining retries retain the original due+900 cutoff.
                deadline = due + dt.timedelta(seconds=GRACE_SECONDS)
                if count == 0:
                    deadline = max(deadline, first + dt.timedelta(seconds=effect["first_attempt_tolerance"]))
                if count >= 3 or now > deadline:
                    self._failed_notification(row, effect, "retry-deadline-exhausted")
                    continue
                if now < first + dt.timedelta(seconds=RETRY_OFFSETS[count]):
                    continue
                token = hashlib.sha256(_encode([app, effect["notification_id"], owner, count + 1])).hexdigest()
                effect["claim"] = {"owner": owner, "token": token, "at_utc": now_utc}
                effect["adapter_attempt_count"] += 1
                effect["attempts"].append({"at_utc": now_utc, "result": "unknown"})
                result = {"notification_id": effect["notification_id"], "adapter_id": effect["adapter_id"], "projection": copy.deepcopy(effect["projection"]), "token": token}
                break
        return result

    def finish_notification(self, app: str, notification_id: str, token: str, *, result: str, now_utc: str) -> None:
        now = parse_utc(now_utc)
        if result not in ("accepted", "transient-failure", "unknown", "unavailable"):
            _error("invalid-argument", "unsupported notification adapter result")
        with self._edit() as ledger:
            row = self._app(app, ledger)
            effect = row["outbox"].get(notification_id)
            if not effect or not effect["claim"] or effect["claim"]["token"] != token:
                _error("conflict", "stale notification claim")
            # Wall time may move backward while an OS call is in flight. Its
            # accepted result still commits; adapter timeout uses host monotonic
            # time outside this ledger, never this audit timestamp ordering.
            effect["attempts"][-1].update({"result": result, "completed_at_utc": now_utc})
            effect["claim"] = None
            if result == "accepted":
                effect["state"] = "accepted"
                row["occurrences"][effect["occurrence_id"]]["state"] = "notification-accepted"
            elif effect["adapter_attempt_count"] >= 3 or now > parse_utc(row["occurrences"][effect["occurrence_id"]]["due_at_utc"]) + dt.timedelta(seconds=GRACE_SECONDS):
                self._failed_notification(row, effect, "capability-unavailable" if result == "unavailable" else "retry-exhausted")

    def claim_guest(self, app: str, *, owner: str, now_utc: str) -> dict | None:
        _text(owner, "daemon incarnation")
        parse_utc(now_utc)
        result = None
        with self._edit() as ledger:
            row = self._app(app, ledger)
            context = row["context"]
            if not context["enabled"] or not context["scheduler_granted"] or not context["active_generation"] or context["rollback_state"] != "none":
                return None
            for event in sorted(row["guest_events"].values(), key=lambda e: (e["scheduled_for_utc"], e["guest_event_id"])):
                if event["state"] != "pending" or event["claim"] is not None or event["scheduled_for_utc"] > now_utc:
                    continue
                if event["attempts"] >= 3:
                    event["state"] = "dead-lettered"
                    continue
                event["attempts"] += 1
                token = hashlib.sha256(_encode([app, event["guest_event_id"], owner, event["attempts"]])).hexdigest()
                event["claim"] = {"owner": owner, "token": token, "generation": context["active_generation"]}
                event["routed_generation"] = context["active_generation"]
                event["dispatched_at_utc"] = now_utc
                event["ack_requested"] = None
                delivery = {key: copy.deepcopy(event[key]) for key in ("schedule", "key", "scheduled_for_utc", "dispatched_at_utc", "classification", "payload")}
                delivery.update({"occurrence": event["occurrence_id"], "attempt": event["attempts"]})
                result = {"guest_event_id": event["guest_event_id"], "generation": context["active_generation"], "token": token, "event": delivery}
                break
        return result

    def finish_guest(self, app: str, guest_event_id: str, token: str, *, result: str) -> None:
        """success requires staged explicit ack; caller atomically commits KV too."""
        if result not in ("success", "trap", "timeout"):
            _error("invalid-argument", "unsupported guest result")
        with self._edit() as ledger:
            row = self._app(app, ledger)
            event = row["guest_events"].get(guest_event_id)
            if not event or not event["claim"] or event["claim"]["token"] != token:
                _error("conflict", "stale guest claim")
            if result == "success":
                self._authority(row, event["claim"]["generation"])
                if event["ack_requested"] not in ("observed", "applied", "failed"):
                    _error("conflict", "successful guest commit requires an occurrence acknowledgment")
                # A handler that returned normally and explicitly reported a
                # business failure still acknowledged receipt. Only a trap or
                # timeout is an unacknowledged delivery retry.
                event["state"] = "acked"
                event["guest_outcome"] = event["ack_requested"]
                occ = row["occurrences"][event["occurrence_id"]]
                if occ["state"] == "guest-pending" and event["state"] == "acked":
                    occ["state"] = "guest-acked"
            else:
                event["state"] = "dead-lettered" if event["attempts"] >= 3 else "pending"
            event["claim"], event["ack_requested"] = None, None

    def recover_owner(self, owner: str) -> None:
        """Only after proving the old incarnation cannot finish its effects."""
        _text(owner, "daemon incarnation")
        with self._edit() as ledger:
            for row in ledger["apps"].values():
                for effect in row["outbox"].values():
                    if effect["claim"] and effect["claim"]["owner"] == owner:
                        effect["claim"] = None
                        if effect["adapter_attempt_count"] >= 3:
                            self._failed_notification(row, effect, "last-attempt-owner-lost")
                for event in row["guest_events"].values():
                    if event["claim"] and event["claim"]["owner"] == owner:
                        event["claim"], event["ack_requested"] = None, None
                        if event["attempts"] >= 3:
                            event["state"] = "dead-lettered"

    @staticmethod
    def _schedule_record(alarm: dict) -> dict:
        spec = alarm["spec"] or {"tag": "at-local" if alarm["schedule"]["tag"] == "one-shot" else "recurring-local", "value": alarm["schedule"]["value"]}
        return {"id": alarm["schedule_id"], "key": alarm["alarm_id"], "purpose": alarm["purpose"],
                "spec": copy.deepcopy(spec), "revision": alarm["revision"], "enabled": alarm["enabled"],
                "next_occurrence_id": alarm["next_occurrence_id"], "notification": copy.deepcopy(alarm["notification"])}

    def scheduler_upsert(self, app: str, request: dict, *, caller_generation: str, now_utc: str) -> dict:
        """Exact WIT upsert input; internal response is {ok,value|error}."""
        try:
            _text(caller_generation, "caller generation")
            _object(request, {"key", "purpose", "spec", "missed", "payload", "notification", "idempotency_key"})
            _text(request["key"], "schedule key", 128)
            purpose, missed = request["purpose"], request["missed"]
            if purpose not in ("alarm", "service-trigger") or missed not in ("skip", "fire-once"):
                _error("invalid-argument", "invalid scheduler purpose or missed policy")
            if purpose == "alarm" and missed != "fire-once":
                _error("incompatible-contract", "Stage 0 alarms require fire-once catch-up semantics")
            if purpose == "service-trigger" and request["notification"] is not None:
                _error("invalid-argument", "service-trigger cannot carry a notification")
            payload = request["payload"]
            if not isinstance(payload, list) or len(payload) > MAX_PAYLOAD or any(type(v) is not int or not 0 <= v <= 255 for v in payload):
                _error("invalid-argument", "payload must be a bounded list of bytes")
            spec = _object(request["spec"], {"tag", "value"})
            if spec["tag"] == "at-instant":
                instant = parse_utc(spec["value"])
                schedule = {"tag": "one-shot", "value": {"when": {"date": {"year": instant.year, "month": instant.month, "day": instant.day}, "time": {"hour": instant.hour, "minute": instant.minute, "second": instant.second}, "time_zone": "UTC"}, "gap": "next-valid", "overlap": "earlier"}}
            elif spec["tag"] in ("at-local", "recurring-local"):
                schedule = {"tag": "one-shot" if spec["tag"] == "at-local" else "recurring", "value": spec["value"]}
            else:
                _error("invalid-argument", "unknown scheduler spec")
            now = parse_utc(now_utc)
            def apply(row: dict) -> dict:
                prior = row["alarms"].get(request["key"])
                value = {"alarm_id": request["key"], "expected_revision": prior["revision"] if prior else None, "schedule": schedule, "notification": request["notification"]}
                self._configure(row, value, now, purpose=purpose, payload=payload, missed=missed, spec=spec)
                return self._schedule_record(row["alarms"][request["key"]])
            fingerprint_payload = {k: v for k, v in request.items() if k != "idempotency_key"}
            return self._mutating_call(app, "scheduler.upsert", fingerprint_payload, request["idempotency_key"], caller_generation, apply)
        except SchedulerError as exc:
            return {"ok": False, "error": exc.as_dict()}

    def _scheduler_toggle(self, app: str, schedule_id: str, key: str, generation: str, now_utc: str, enable: bool) -> dict:
        try:
            _text(generation, "caller generation")
            _text(schedule_id, "schedule ID")
            now = parse_utc(now_utc)
            def apply(row: dict) -> dict | None:
                alarm = next((v for v in row["alarms"].values() if v["schedule_id"] == schedule_id), None)
                if alarm is None:
                    _error("not-found", "schedule does not exist")
                self._toggle(row, {"alarm_id": alarm["alarm_id"], "expected_revision": alarm["revision"]}, now, enable)
                return self._schedule_record(alarm) if enable else None
            return self._mutating_call(app, "scheduler.enable" if enable else "scheduler.disable", {"id": schedule_id}, key, generation, apply)
        except SchedulerError as exc:
            return {"ok": False, "error": exc.as_dict()}

    def scheduler_disable(self, app: str, schedule_id: str, idempotency_key: str, *, caller_generation: str, now_utc: str) -> dict:
        return self._scheduler_toggle(app, schedule_id, idempotency_key, caller_generation, now_utc, False)

    def scheduler_enable(self, app: str, schedule_id: str, idempotency_key: str, *, caller_generation: str, now_utc: str) -> dict:
        return self._scheduler_toggle(app, schedule_id, idempotency_key, caller_generation, now_utc, True)

    def scheduler_list_schedules(self, app: str, *, caller_generation: str) -> dict:
        try:
            _text(caller_generation, "caller generation")
            row = self._app(app)
            self._authority(row, caller_generation, mutation=False)
            return {"ok": True, "value": [self._schedule_record(a) for _, a in sorted(row["alarms"].items())]}
        except SchedulerError as exc:
            return {"ok": False, "error": exc.as_dict()}

    def scheduler_acknowledge_occurrence(self, app: str, occurrence: str, outcome: str, idempotency_key: str,
                                       *, caller_generation: str, delivery_token: str) -> dict:
        """Token is host-only delivery context, NOT an added guest WIT field."""
        try:
            _text(caller_generation, "caller generation")
            if outcome not in ("observed", "applied", "failed"):
                _error("invalid-argument", "invalid occurrence outcome")
            def apply(row: dict) -> None:
                event = row["guest_events"].get(f"guest:{occurrence}")
                if not event or not event["claim"] or event["claim"]["token"] != delivery_token or event["claim"]["generation"] != caller_generation:
                    _error("conflict", "acknowledgment requires the current guest delivery claim")
                event["ack_requested"] = outcome
            # Token is authority context, not an extra WIT semantic field. These
            # receipts commit only with the guest's staged KV and successful ack.
            return self._mutating_call(app, "scheduler.acknowledge-occurrence", {"occurrence": occurrence, "outcome": outcome}, idempotency_key, caller_generation, apply)
        except SchedulerError as exc:
            return {"ok": False, "error": exc.as_dict()}

    def status(self, app: str, alarm_id: str, occurrence_limit: int = 32, effect_limit: int = 64) -> dict:
        """Read-only: no initialization, polling, claims, or dedup writes."""
        _integer(occurrence_limit, 1, MAX_BATCH)
        _integer(effect_limit, 1, MAX_BATCH)
        row = self._app(app)
        alarm = self._alarm(row, alarm_id)
        if alarm["purpose"] != "alarm":
            _error("invalid-argument", "alarm-status does not project a service-trigger")
        occurrences = sorted((o for o in row["occurrences"].values() if o["alarm_id"] == alarm_id), key=lambda o: (o["due_at_utc"], o["occurrence"]), reverse=True)
        occurrence_ids = {o["occurrence"] for o in occurrences}
        effects = []
        for effect in row["outbox"].values():
            if effect["occurrence_id"] in occurrence_ids:
                effects.append((effect["first_eligible_at_utc"], effect["notification_id"], {"tag": "notification", "value": {"notification_id": effect["notification_id"], "occurrence": effect["occurrence_id"], "outcome": effect["state"], "attempts": effect["adapter_attempt_count"]}}))
        for event in row["guest_events"].values():
            if event["occurrence_id"] in occurrence_ids:
                effects.append((event["dispatched_at_utc"] or event["scheduled_for_utc"], event["guest_event_id"], {"tag": "guest-event", "value": {"guest_event_id": event["guest_event_id"], "occurrence": event["occurrence_id"], "outcome": event["state"], "attempts": event["attempts"]}}))
        for action in row["actions"].values():
            if action["parent_occurrence_id"] in occurrence_ids:
                effects.append((action["action_received_at_utc"], action["os_action_id"], {"tag": "snooze-action", "value": {k: action[k] for k in ("os_action_id", "parent_occurrence_id", "snooze_occurrence_id", "deduplicated")}}))
        effects.sort(key=lambda e: (e[0], e[1]), reverse=True)
        result = {k: copy.deepcopy(alarm[k]) for k in ("alarm_id", "schedule", "notification", "enabled", "revision", "next_occurrence_id", "permission_health")}
        result.update({"occurrences": [{k: o[k] for k in ("occurrence", "due_at_utc", "state", "classification", "notification_id")} for o in occurrences[:occurrence_limit]],
                       "occurrences_truncated": len(occurrences) > occurrence_limit,
                       "effects": [e[2] for e in effects[:effect_limit]], "effects_truncated": len(effects) > effect_limit})
        return {"app": app, "active_generation": row["context"]["active_generation"], "rollback_state": row["context"]["rollback_state"], "alarm": result}


class UnavailableNotificationAdapter:
    """Explicit non-adapter. Never returns a fake accepted receipt."""
    available = False

    def put(self, notification_id: str, projection: dict) -> str:
        raise SchedulerError("capability-unavailable", "no verified OS notification adapter is installed")


def _strict_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _error("integrity-failure", "duplicate persisted JSON member")
        result[key] = value
    return result


class PrivateJsonStore:
    """Owner-private bounded standalone store. No GUI or production root default.

    Transactions must be short and MUST NOT contain external I/O or guest work.
    Lock contention returns conflict immediately; nested transactions are rejected.
    """
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _error("permission-denied", "scheduler store must be an owner-private 0700 directory")
        self._local = threading.local()

    def _open(self, name: str, flags: int) -> int:
        fd = os.open(self.root / name, flags | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            os.close(fd)
            _error("permission-denied", "scheduler store member is not owner-private regular storage")
        return fd

    def read(self) -> dict:
        try:
            fd = self._open("state.json", os.O_RDONLY)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            _error("resource-limit", "persisted scheduler state exceeds byte bound")
        try:
            value = json.loads(raw, object_pairs_hook=_strict_pairs, parse_constant=lambda _: _error("integrity-failure", "non-finite persisted JSON"))
        except (ValueError, UnicodeError, RecursionError):
            _error("integrity-failure", "invalid persisted scheduler JSON")
        if not isinstance(value, dict):
            _error("integrity-failure", "scheduler store root is not an object")
        return value

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        if getattr(self._local, "active", False):
            _error("conflict", "scheduler store transaction cannot be re-entered")
        self._local.active = True
        fd = None
        try:
            fd = self._open("owner.lock", os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _error("conflict", "scheduler store already has a transaction owner")
            state = self.read()
            before = _encode(state)
            yield state
            encoded = _encode(state)
            if encoded == before:
                return
            temp_fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=self.root)
            try:
                with os.fdopen(temp_fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.root / "state.json")
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        finally:
            if fd is not None:
                os.close(fd)
            self._local.active = False
