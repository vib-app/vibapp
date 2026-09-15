"""Synthetic clock/adapter acceptance of the S1 ledger, NOT macOS OS acceptance.

No guest, model, compiler, native notification, or user runtime data is touched.
Canonical fixture constants/wire examples are read, not redefined as a new ABI.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vibapp_daemon import scheduler_backend as sb


REPO = Path(__file__).resolve().parents[3]
FIXTURES = json.loads((REPO / "fixtures/alarm/cases.json").read_text())
APP = "vibapp.alarm"
NOW = "2026-08-13T00:30:00Z"
DUE = "2026-08-13T01:00:00Z"
PROJECTION = FIXTURES["wire_examples"]["notification_projection"]


def daily(hour=9, minute=0, zone="Asia/Shanghai", start=(2026, 8, 13), days=None):
    return {"tag": "recurring", "value": {"kind": "daily" if days is None else "weekly",
        "time": {"hour": hour, "minute": minute, "second": 0},
        "start_date": dict(zip(("year", "month", "day"), start)), "time_zone": zone,
        "weekdays": days or [], "gap": "next-valid", "overlap": "earlier"}}


def once(date=(2026, 8, 13), time=(9, 0, 0), zone="Asia/Shanghai"):
    return {"tag": "one-shot", "value": {"when": {"date": dict(zip(("year", "month", "day"), date)),
        "time": dict(zip(("hour", "minute", "second"), time)), "time_zone": zone}, "gap": "next-valid", "overlap": "earlier"}}


class SyntheticIdempotentAdapter:
    """A test double of put semantics; never used/advertised as the OS sink."""
    def __init__(self):
        self.logical = {}
        self.calls = []

    def put(self, notification_id, projection):
        self.calls.append(notification_id)
        old = self.logical.setdefault(notification_id, copy.deepcopy(projection))
        if old != projection:
            raise AssertionError("logical notification changed across retry")
        return "accepted"


class SchedulerBackendTests(unittest.TestCase):
    def setUp(self):
        self.state = {"unrelated_core_state": {"must_survive": True}}
        self.backend = sb.SchedulerBackend(self.state, tzdata_label="synthetic-label:system-zoneinfo")
        self.context()

    def context(self, *, generation="gen-1", enabled=True, permission="authorized", granted=True, rollback="none", app=APP):
        self.backend.host_context(app, active_generation=generation, enabled=enabled, scheduler_granted=granted,
                                  notification_permission=permission, rollback_state=rollback)

    def command(self, tag, value, *, key=None, now=NOW, generation=None, host_action=False):
        return self.backend.command(APP, {"tag": tag, "value": value}, idempotency_key=key or tag,
                                    now_utc=now, caller_generation=generation, host_action=host_action)

    def configure(self, schedule=None, *, alarm="alarm-control", key="configure", now=NOW, expected=None):
        result = self.command("alarm-configure", {"alarm_id": alarm, "expected_revision": expected,
            "schedule": schedule or daily(), "notification": PROJECTION}, key=key, now=now)
        self.assertTrue(result["ok"], result)
        return result["value"]

    def row(self):
        return self.state["scheduler"]["apps"][APP]

    def accepted(self, *, when=DUE, owner="daemon-A"):
        claim = self.backend.claim_notification(APP, owner=owner, now_utc=when)
        self.assertIsNotNone(claim)
        self.backend.finish_notification(APP, claim["notification_id"], claim["token"], result="accepted", now_utc=when)
        return claim

    def test_fixture_constants_are_exact(self):
        self.assertEqual(sb.GRACE_SECONDS, FIXTURES["constants"]["catch_up_grace_seconds_inclusive"])
        self.assertEqual(list(sb.RETRY_OFFSETS), FIXTURES["constants"]["notification_retry_offsets_seconds"])
        self.assertEqual(len(FIXTURES["cases"]), 45)

    def test_canonical_gui_and_cli_are_semantically_identical(self):
        worlds = []
        for name in ("gui_alarm_configure", "cli_alarm_configure"):
            state = {}
            backend = sb.SchedulerBackend(state, tzdata_label="same-tzdata")
            backend.host_context(APP, active_generation="gen-1", enabled=True, scheduler_granted=True, notification_permission="authorized")
            request = FIXTURES["wire_examples"][name]
            result = backend.command(APP, request["command"], idempotency_key=request["idempotency_key"], now_utc=NOW)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["value"]["next_occurrence_id"], f"occ:alarm-control:r1:{DUE}")
            worlds.append(state)
        self.assertEqual(worlds[0], worlds[1])

    def test_strict_utc_and_invalid_calendar(self):
        for value in ("2026-08-13T01:00:00+00:00", "2026-08-13t01:00:00Z", "2026-08-13T01:00:00.1Z", "2026-02-30T01:00:00Z", "2026-08-13T01:00:60Z", "2026-8-13T01:00:00Z"):
            with self.subTest(value=value), self.assertRaises(sb.SchedulerError) as raised:
                sb.parse_utc(value)
            self.assertEqual(raised.exception.code, "invalid-argument")
        self.assertEqual(sb.utc(sb.parse_utc("0001-01-01T00:00:00Z")), "0001-01-01T00:00:00Z")

    def test_invalid_recurrence_and_extensions_rejected_without_alarm_mutation(self):
        variants = []
        for field, value in (("time_zone", "not/a/zone"), ("weekdays", [1]), ("kind", "cron"), ("gap", "reject"), ("overlap", "later")):
            schedule = daily(); schedule["value"][field] = value; variants.append(schedule)
        for schedule in (daily(days=[]), daily(days=["monday", "monday"]), daily(days=["monday"])):
            if schedule["value"]["weekdays"] == ["monday"]:
                schedule["value"]["interval"] = 2
            variants.append(schedule)
        for index, schedule in enumerate(variants):
            result = self.command("alarm-configure", {"alarm_id": "bad", "expected_revision": None, "schedule": schedule, "notification": PROJECTION}, key=f"bad-{index}")
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error"]["code"], "invalid-argument")
            self.assertEqual(self.row()["alarms"], {})

    def test_one_shot_expired_create_has_no_occurrence(self):
        result = self.command("alarm-configure", {"alarm_id": "old", "expected_revision": None, "schedule": once(), "notification": PROJECTION}, now=DUE)
        self.assertFalse(result["ok"])
        self.assertEqual(self.row()["occurrences"], {})

    def test_dst_gap_shifts_full_hour_and_fold_earlier_once(self):
        gap = self.configure(once((2026, 3, 8), (2, 30, 0), "America/New_York"), alarm="gap", key="gap", now="2026-03-07T00:00:00Z")
        fold = self.configure(once((2026, 11, 1), (1, 30, 0), "America/New_York"), alarm="fold", key="fold", now="2026-10-31T00:00:00Z")
        self.assertEqual(gap["next_occurrence_id"], "occ:gap:r1:2026-03-08T07:30:00Z")
        self.assertEqual(fold["next_occurrence_id"], "occ:fold:r1:2026-11-01T05:30:00Z")
        self.assertEqual(len(self.row()["occurrences"]), 2)

    def test_dst_half_hour_gap_not_assumed_to_be_one_hour(self):
        result = self.configure(once((2026, 10, 4), (2, 15, 0), "Australia/Lord_Howe"), now="2026-10-03T00:00:00Z")
        self.assertEqual(result["next_occurrence_id"], "occ:alarm-control:r1:2026-10-03T15:45:00Z")

    def test_materialized_occurrence_keeps_tzdata_label(self):
        result = self.configure()
        oid = result["next_occurrence_id"]
        self.backend = sb.SchedulerBackend(self.state, tzdata_label="updated-tzdata")
        self.backend.advance_due(DUE)
        self.assertEqual(self.row()["occurrences"][oid]["tzdata_label"], "synthetic-label:system-zoneinfo")
        next_id = self.row()["alarms"]["alarm-control"]["next_occurrence_id"]
        self.assertEqual(self.row()["occurrences"][next_id]["tzdata_label"], "updated-tzdata")

    def test_weekly_cursor_never_drifts_from_late_delivery(self):
        result = self.configure(daily(days=["monday", "tuesday"], start=(2026, 8, 10)), now="2026-08-09T00:00:00Z")
        self.assertEqual(result["next_occurrence_id"], "occ:alarm-control:r1:2026-08-10T01:00:00Z")
        self.backend.advance_due("2026-08-10T01:10:00Z", recovery=True)
        self.assertEqual(self.row()["alarms"]["alarm-control"]["next_occurrence_id"], "occ:alarm-control:r1:2026-08-11T01:00:00Z")

    def test_due_transaction_creates_one_outbox_and_inbox(self):
        self.configure()
        self.backend.advance_due(DUE)
        snapshot = copy.deepcopy(self.row())
        self.assertEqual(self.backend.advance_due(DUE)["processed"], [])
        self.assertEqual(self.row(), snapshot)
        self.assertEqual(len(self.row()["outbox"]), 1)
        self.assertEqual(len(self.row()["guest_events"]), 1)
        self.assertEqual(self.state["unrelated_core_state"], {"must_survive": True})

    def test_recovery_inclusive_900_first_attempt_at_905(self):
        result = self.configure()
        self.backend.advance_due("2026-08-13T01:15:00Z", recovery=True)
        self.accepted(when="2026-08-13T01:15:05Z")
        occurrence = self.row()["occurrences"][result["next_occurrence_id"]]
        self.assertEqual(occurrence["classification"], "catch-up")
        self.assertEqual(occurrence["state"], "notification-accepted")

    def test_recovery_901_is_missed_and_no_outbox(self):
        result = self.configure()
        self.backend.advance_due("2026-08-13T01:15:01Z", recovery=True)
        self.assertEqual(self.row()["occurrences"][result["next_occurrence_id"]]["state"], "missed")
        self.assertEqual(self.row()["outbox"], {})
        self.assertEqual(next(iter(self.row()["guest_events"].values()))["classification"], "missed")

    def test_many_elapsed_slots_are_individually_classified_and_bounded(self):
        self.configure()
        result = self.backend.advance_due("2026-08-15T01:10:00Z", recovery=True, limit=1)
        self.assertTrue(result["more_due"])
        result = self.backend.advance_due("2026-08-15T01:10:00Z", recovery=True, limit=2)
        self.assertFalse(result["more_due"])
        states = [v["classification"] for v in self.row()["guest_events"].values()]
        self.assertEqual(states, ["missed", "missed", "catch-up"])
        self.assertEqual(self.row()["alarms"]["alarm-control"]["next_occurrence_id"], "occ:alarm-control:r1:2026-08-16T01:00:00Z")

    def test_backward_clock_no_early_call_or_reopened_terminal(self):
        self.configure()
        self.assertEqual(self.backend.advance_due(NOW)["processed"], [])
        self.assertIsNone(self.backend.claim_notification(APP, owner="A", now_utc=NOW))
        self.backend.advance_due(DUE)
        self.assertIsNone(self.backend.claim_notification(APP, owner="A", now_utc=NOW))
        self.accepted()
        snapshot = copy.deepcopy(self.row()["outbox"])
        self.backend.advance_due(NOW, recovery=True)
        self.backend.advance_due(DUE)
        self.assertEqual(self.row()["outbox"], snapshot)

    def test_permission_denied_and_unknown_never_enqueues_or_prompts(self):
        for permission in ("denied", "not-determined"):
            with self.subTest(permission=permission):
                self.setUp(); self.context(permission=permission)
                result = self.configure()
                self.backend.advance_due(DUE)
                self.assertEqual(self.row()["outbox"], {})
                self.assertEqual(self.row()["occurrences"][result["next_occurrence_id"]]["state"], "permission-denied")
                self.assertEqual(self.backend.status(APP, "alarm-control")["alarm"]["permission_health"], permission)

    def test_grant_only_affects_future_and_revoke_rechecked_at_claim(self):
        self.context(permission="denied")
        old = self.configure()["next_occurrence_id"]
        self.backend.advance_due(DUE)
        self.context(permission="authorized")
        self.backend.advance_due("2026-08-14T01:00:00Z")
        self.assertEqual(self.row()["occurrences"][old]["state"], "permission-denied")
        self.assertEqual(len(self.row()["outbox"]), 1)
        self.context(permission="not-determined")
        self.assertIsNone(self.backend.claim_notification(APP, owner="A", now_utc="2026-08-14T01:00:00Z"))
        self.assertEqual(next(iter(self.row()["outbox"].values()))["failure"], "permission-denied")

    def test_fixed_three_retries_and_stable_projection(self):
        self.configure(); self.backend.advance_due(DUE)
        ids = []
        for second, outcome in ((0, "transient-failure"), (2, "transient-failure"), (10, "accepted")):
            when = f"2026-08-13T01:00:{second:02d}Z"
            claim = self.backend.claim_notification(APP, owner="A", now_utc=when)
            self.assertIsNotNone(claim)
            ids.append(claim["notification_id"])
            self.assertEqual(claim["projection"], PROJECTION)
            self.backend.finish_notification(APP, ids[-1], claim["token"], result=outcome, now_utc=when)
            self.assertIsNone(self.backend.claim_notification(APP, owner="A", now_utc=when))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(next(iter(self.row()["outbox"].values()))["adapter_attempt_count"], 3)

    def test_three_failures_terminal_but_recurrence_enabled(self):
        self.configure(); self.backend.advance_due(DUE)
        for second in (0, 2, 10):
            when = f"2026-08-13T01:00:{second:02d}Z"
            claim = self.backend.claim_notification(APP, owner="A", now_utc=when)
            self.backend.finish_notification(APP, claim["notification_id"], claim["token"], result="transient-failure", now_utc=when)
        self.assertEqual(next(iter(self.row()["outbox"].values()))["state"], "failed")
        self.assertTrue(self.row()["alarms"]["alarm-control"]["enabled"])

    def test_retry_is_clipped_but_first_accepted_completion_can_be_late(self):
        self.configure(); self.backend.advance_due("2026-08-13T01:15:00Z", recovery=True)
        claim = self.backend.claim_notification(APP, owner="A", now_utc="2026-08-13T01:15:00Z")
        self.backend.finish_notification(APP, claim["notification_id"], claim["token"], result="transient-failure", now_utc="2026-08-13T01:15:00Z")
        self.assertIsNone(self.backend.claim_notification(APP, owner="A", now_utc="2026-08-13T01:15:02Z"))
        self.assertEqual(next(iter(self.row()["outbox"].values()))["state"], "failed")

    def test_unknown_acceptance_restart_same_id_and_known_acceptance_no_resend(self):
        self.configure(); self.backend.advance_due(DUE)
        adapter = SyntheticIdempotentAdapter()
        claim = self.backend.claim_notification(APP, owner="old-incarnation", now_utc=DUE)
        adapter.put(claim["notification_id"], claim["projection"])
        # Synthetic crash after adapter accepted but before its durable marker.
        self.assertIsNone(self.backend.claim_notification(APP, owner="new", now_utc="2026-08-13T01:00:02Z"))
        self.backend.recover_owner("old-incarnation")
        retry = self.backend.claim_notification(APP, owner="new", now_utc="2026-08-13T01:00:02Z")
        self.assertEqual(retry["notification_id"], claim["notification_id"])
        result = adapter.put(retry["notification_id"], retry["projection"])
        self.backend.finish_notification(APP, retry["notification_id"], retry["token"], result=result, now_utc="2026-08-13T01:00:02Z")
        self.assertEqual(len(adapter.logical), 1)
        self.assertEqual(len(adapter.calls), 2)
        self.backend.recover_owner("new")
        self.assertIsNone(self.backend.claim_notification(APP, owner="third", now_utc="2026-08-13T01:00:10Z"))

    def test_idempotent_configure_revision_conflict_and_no_partial_edit(self):
        first = self.configure()
        replay = self.configure()
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["revision"], first["revision"])
        request = {"alarm_id": "alarm-control", "expected_revision": 1, "schedule": daily(hour=10), "notification": PROJECTION}
        conflict = self.command("alarm-configure", request, key="configure")
        self.assertEqual(conflict["error"]["message"], "idempotency-conflict")
        invalid = self.command("alarm-configure", {**request, "expected_revision": 2}, key="stale")
        self.assertEqual(invalid["error"]["code"], "stale-revision")
        self.assertEqual(self.row()["alarms"]["alarm-control"]["revision"], 1)
        self.assertEqual(len(self.row()["occurrences"]), 1)

    def test_edit_first_cancels_old_but_due_first_projection_survives(self):
        old = self.configure()["next_occurrence_id"]
        self.configure(daily(hour=10), expected=1, key="edit")
        self.backend.advance_due(DUE)
        self.assertEqual(self.row()["occurrences"][old]["state"], "cancelled")
        self.assertEqual(self.row()["outbox"], {})
        self.setUp(); self.configure(); self.backend.advance_due(DUE)
        self.configure(daily(hour=10), expected=1, key="edit", now=DUE)
        self.accepted()
        self.assertEqual(len(self.row()["outbox"]), 1)

    def test_disable_enable_cycle_and_expired_one_shot_no_revision(self):
        old = self.configure()["next_occurrence_id"]
        disabled = self.command("alarm-disable", {"alarm_id": "alarm-control", "expected_revision": 1})
        self.assertEqual(disabled["value"]["revision"], 2)
        repeat = self.command("alarm-cancel", {"alarm_id": "alarm-control", "expected_revision": 2})
        self.assertEqual(repeat["value"]["outcome"], "unchanged")
        enabled = self.command("alarm-enable", {"alarm_id": "alarm-control", "expected_revision": 2}, now="2026-08-13T02:00:00Z")
        self.assertEqual(enabled["value"]["next_occurrence_id"], "occ:alarm-control:r3:2026-08-14T01:00:00Z")
        self.assertEqual(self.row()["occurrences"][old]["state"], "cancelled")
        self.setUp(); self.configure(once())
        self.command("alarm-disable", {"alarm_id": "alarm-control", "expected_revision": 1})
        expired = self.command("alarm-enable", {"alarm_id": "alarm-control", "expected_revision": 2}, now=DUE)
        self.assertEqual(expired["error"]["message"], "expired-one-shot")
        self.assertEqual(self.row()["alarms"]["alarm-control"]["revision"], 2)

    def test_app_disable_preserves_due_effect_but_cancels_future_and_guest_authority(self):
        self.configure(); self.backend.advance_due(DUE)
        self.context(enabled=False)
        self.context(enabled=False)
        self.assertEqual(self.row()["alarms"]["alarm-control"]["revision"], 2)
        self.assertFalse(self.row()["alarms"]["alarm-control"]["enabled"])
        self.accepted()
        self.assertIsNone(self.backend.claim_guest(APP, owner="A", now_utc=DUE))
        denied = self.backend.scheduler_disable(APP, "schedule:alarm-control", "denied", caller_generation="gen-1", now_utc=DUE)
        self.assertEqual(denied["error"]["code"], "app-disabled")
        self.context(enabled=True)
        self.assertFalse(self.row()["alarms"]["alarm-control"]["enabled"])
        self.assertIsNone(self.row()["alarms"]["alarm-control"]["next_occurrence_id"])

    def test_snooze_dedup_chain_and_regular_same_due_are_distinct(self):
        oid = self.configure(daily(hour=1, zone="UTC"))["next_occurrence_id"]
        self.backend.advance_due(DUE); self.accepted()
        request = {"parent_occurrence_id": oid, "os_action_id": "action-1", "action_received_at_utc": "2026-08-13T01:00:10Z", "duration_seconds": 600}
        first = self.command("alarm-snooze", request, key="snooze", now="2026-08-13T01:00:10Z", host_action=True)
        self.assertTrue(first["ok"], first)
        replay = self.command("alarm-snooze", {**request, "action_received_at_utc": "2026-08-13T01:00:11Z"}, key="snooze", now="2026-08-13T01:00:11Z", host_action=True)
        self.assertTrue(replay["value"]["deduplicated"])
        child = first["value"]["snooze_occurrence_id"]
        self.assertEqual(self.row()["occurrences"][child]["due_at_utc"], "2026-08-13T01:10:10Z")
        self.assertEqual(len(self.row()["actions"]), 1)
        self.assertEqual(self.row()["alarms"]["alarm-control"]["next_occurrence_id"], "occ:alarm-control:r1:2026-08-14T01:00:00Z")
        self.backend.advance_due("2026-08-13T01:10:10Z"); self.accepted(when="2026-08-13T01:10:10Z")
        chain = self.command("alarm-snooze", {**request, "parent_occurrence_id": child, "os_action_id": "action-2", "action_received_at_utc": "2026-08-13T01:10:20Z"}, key="chain", now="2026-08-13T01:10:20Z", host_action=True)
        self.assertTrue(chain["ok"], chain)
        self.assertEqual(chain["value"]["snooze_occurrence_id"], f"snooze:{child}:1")

    def test_upgrade_blocks_guest_and_control_edits_but_host_snooze_survives(self):
        oid = self.configure()["next_occurrence_id"]
        self.backend.advance_due(DUE); self.accepted()
        self.context(generation="gen-2", rollback="observation-window")
        edit = self.command("alarm-disable", {"alarm_id": "alarm-control", "expected_revision": 1}, key="upgrade-edit", generation="gen-2", now=DUE)
        self.assertEqual(edit["error"]["code"], "upgrade-in-progress")
        self.assertIn("cmd:upgrade-edit", self.row()["commands"])
        snooze = self.command("alarm-snooze", {"parent_occurrence_id": oid, "os_action_id": "upgrade-snooze", "action_received_at_utc": "2026-08-13T01:00:20Z", "duration_seconds": 600}, key="snooze", now="2026-08-13T01:00:20Z", host_action=True)
        self.assertTrue(snooze["ok"], snooze)
        self.assertIsNone(self.backend.claim_guest(APP, owner="A", now_utc=DUE))
        self.context(generation="gen-2")
        guest = self.backend.claim_guest(APP, owner="A", now_utc=DUE)
        self.assertEqual(guest["generation"], "gen-2")
        self.context(generation="gen-1")
        self.assertEqual(self.row()["occurrences"][oid]["state"], "notification-accepted")
        self.assertIsNone(self.backend.claim_notification(APP, owner="A", now_utc="2026-08-13T01:00:30Z"))

    def test_stale_generation_and_permission_cannot_replay_success(self):
        self.configure()
        self.context(generation="gen-2")
        response = self.command("alarm-configure", {"alarm_id": "alarm-control", "expected_revision": None, "schedule": daily(), "notification": PROJECTION}, key="configure", generation="gen-1")
        self.assertEqual(response["error"]["message"], "inactive-generation")
        self.context(granted=False)
        response = self.backend.scheduler_list_schedules(APP, caller_generation="gen-1")
        self.assertEqual(response["error"]["code"], "permission-denied")

    def test_guest_three_traps_do_not_suppress_notification(self):
        self.configure(); self.backend.advance_due(DUE); self.accepted()
        for attempt in range(1, 4):
            claim = self.backend.claim_guest(APP, owner="A", now_utc=DUE)
            self.assertEqual(claim["event"]["attempt"], attempt)
            self.backend.finish_guest(APP, claim["guest_event_id"], claim["token"], result="trap")
        self.assertIsNone(self.backend.claim_guest(APP, owner="A", now_utc=DUE))
        self.assertEqual(next(iter(self.row()["guest_events"].values()))["state"], "dead-lettered")
        self.assertEqual(next(iter(self.row()["outbox"].values()))["state"], "accepted")

    def test_guest_ack_is_staged_until_successful_joint_kv_commit(self):
        self.configure(); self.backend.advance_due(DUE)
        claim = self.backend.claim_guest(APP, owner="A", now_utc=DUE)
        with self.assertRaises(sb.SchedulerError):
            self.backend.finish_guest(APP, claim["guest_event_id"], claim["token"], result="success")
        ack = self.backend.scheduler_acknowledge_occurrence(APP, claim["event"]["occurrence"], "applied", "ack", caller_generation="gen-1", delivery_token=claim["token"])
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(next(iter(self.row()["guest_events"].values()))["state"], "pending")
        self.state["synthetic_guest_kv"] = {"observed": claim["event"]["occurrence"]}
        self.backend.finish_guest(APP, claim["guest_event_id"], claim["token"], result="success")
        self.assertEqual(next(iter(self.row()["guest_events"].values()))["state"], "acked")

    def test_status_is_pure_bounded_and_detached(self):
        self.configure(); self.backend.advance_due(DUE)
        before = copy.deepcopy(self.state)
        response = self.command("alarm-status", {"alarm_id": "alarm-control", "occurrence_limit": 1, "effect_limit": 1}, key="must-not-record")
        self.assertTrue(response["ok"])
        self.assertTrue(response["value"]["alarm"]["occurrences_truncated"])
        self.assertTrue(response["value"]["alarm"]["effects_truncated"])
        response["value"]["alarm"]["notification"]["title"] = "not-live"
        self.assertEqual(self.state, before)
        with self.assertRaises(sb.SchedulerError):
            self.backend.status(APP, "alarm-control", occurrence_limit=True)

    def test_wit_upsert_disable_enable_list_and_payload(self):
        request = {"key": "timer", "purpose": "service-trigger", "spec": {"tag": "at-instant", "value": DUE}, "missed": "fire-once", "payload": [1, 2, 255], "notification": None, "idempotency_key": "wit-upsert"}
        result = self.backend.scheduler_upsert(APP, request, caller_generation="gen-1", now_utc=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"]["spec"], request["spec"])
        schedule_id = result["value"]["id"]
        self.assertEqual(self.backend.scheduler_upsert(APP, request, caller_generation="gen-1", now_utc=NOW)["value"]["revision"], 1)
        disabled = self.backend.scheduler_disable(APP, schedule_id, "disable", caller_generation="gen-1", now_utc=NOW)
        self.assertEqual(disabled, {"ok": True, "value": None})
        enabled = self.backend.scheduler_enable(APP, schedule_id, "enable", caller_generation="gen-1", now_utc=NOW)
        self.assertEqual(enabled["value"]["revision"], 3)
        before = copy.deepcopy(self.state)
        listed = self.backend.scheduler_list_schedules(APP, caller_generation="gen-1")
        self.assertEqual(listed["value"], [enabled["value"]])
        self.assertEqual(before, self.state)
        self.backend.advance_due(DUE)
        self.assertEqual(self.row()["outbox"], {})
        guest = self.backend.claim_guest(APP, owner="A", now_utc=DUE)
        self.assertEqual(guest["event"]["payload"], [1, 2, 255])
        self.assertEqual(guest["event"]["classification"], "on-time")

    def test_wit_alarm_skip_is_not_silently_approximated(self):
        request = {"key": "water", "purpose": "alarm", "spec": {"tag": "recurring-local", "value": daily()["value"]}, "missed": "skip", "payload": [], "notification": PROJECTION, "idempotency_key": "bad-alarm"}
        response = self.backend.scheduler_upsert(APP, request, caller_generation="gen-1", now_utc=NOW)
        self.assertEqual(response["error"]["code"], "incompatible-contract")
        self.assertEqual(self.row()["alarms"], {})

    def test_namespaces_do_not_leak_between_apps(self):
        self.configure()
        self.context(app="second.app")
        request = {"tag": "alarm-configure", "value": {"alarm_id": "alarm-control", "expected_revision": None, "schedule": daily(), "notification": PROJECTION}}
        second = self.backend.command("second.app", request, idempotency_key="configure", now_utc=NOW)
        self.assertTrue(second["ok"])
        self.backend.advance_due(DUE)
        a = self.backend.claim_notification(APP, owner="A", now_utc=DUE)
        b = self.backend.claim_notification("second.app", owner="A", now_utc=DUE)
        # Logical IDs are frozen; OS adapter MUST namespace them by trusted app.
        self.assertEqual(a["notification_id"], b["notification_id"])
        self.assertNotEqual(a["adapter_id"], b["adapter_id"])
        self.assertNotEqual(a["token"], b["token"])
        with self.assertRaises(sb.SchedulerError):
            self.backend.finish_notification(APP, a["notification_id"], b["token"], result="accepted", now_utc=DUE)
        self.assertEqual(len(self.state["scheduler"]["apps"]), 2)

    def test_optional_revision_wire_null_and_absent_zero(self):
        self.configure(expected=0)
        response = self.command("alarm-disable", {"alarm_id": "alarm-control", "expected_revision": None})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["value"]["revision"], 2)
        edited = self.configure(daily(hour=10), expected=None, key="unconditional-edit")
        self.assertEqual(edited["revision"], 3)

    def test_malformed_wire_values_fail_typed_not_python_exceptions(self):
        for index, payload in enumerate((None, [], "wrong", {}, {"alarm_id": [], "expected_revision": None, "schedule": daily(), "notification": PROJECTION})):
            response = self.command("alarm-configure", payload, key=f"bad-{index}")
            self.assertFalse(response["ok"], response)
            self.assertEqual(response["error"]["code"], "invalid-argument")
        request = {"key": [], "purpose": "alarm", "spec": {"tag": "at-instant", "value": DUE}, "missed": "fire-once", "payload": [], "notification": PROJECTION, "idempotency_key": "bad-key"}
        self.assertEqual(self.backend.scheduler_upsert(APP, request, caller_generation="gen-1", now_utc=NOW)["error"]["code"], "invalid-argument")

    def test_wire_inputs_and_returned_receipts_are_not_live_state_aliases(self):
        request = {"key": "timer", "purpose": "service-trigger", "spec": {"tag": "at-instant", "value": DUE}, "missed": "fire-once", "payload": [3], "notification": None, "idempotency_key": "upsert"}
        response = self.backend.scheduler_upsert(APP, request, caller_generation="gen-1", now_utc=NOW)
        snapshot = copy.deepcopy(self.state)
        request["payload"].append(4)
        request["spec"]["value"] = "changed"
        response["value"]["spec"]["value"] = "changed-return"
        self.assertEqual(snapshot, self.state)

    def test_wall_correction_during_adapter_does_not_discard_acceptance(self):
        self.configure(); self.backend.advance_due(DUE)
        claim = self.backend.claim_notification(APP, owner="A", now_utc=DUE)
        self.backend.finish_notification(APP, claim["notification_id"], claim["token"], result="accepted", now_utc=NOW)
        self.assertEqual(next(iter(self.row()["outbox"].values()))["state"], "accepted")

    def test_snooze_and_regular_same_due_deliver_both_logical_occurrences(self):
        # Host-verified action on yesterday's accepted occurrence arrives 10m
        # before today's civil recurrence. Snooze must not consume that slot.
        oid = self.configure()["next_occurrence_id"]
        self.backend.advance_due(DUE); self.accepted()
        response = self.command("alarm-snooze", {"parent_occurrence_id": oid, "os_action_id": "same-due", "action_received_at_utc": "2026-08-14T00:50:00Z", "duration_seconds": 600}, key="same-due", now="2026-08-14T00:50:00Z", host_action=True)
        self.assertTrue(response["ok"], response)
        self.backend.advance_due("2026-08-14T01:00:00Z")
        first = self.accepted(when="2026-08-14T01:00:00Z")
        second = self.accepted(when="2026-08-14T01:00:00Z")
        self.assertNotEqual(first["notification_id"], second["notification_id"])
        self.assertEqual(len(self.row()["outbox"]), 3)

    def test_guest_business_failure_ack_is_not_a_trap_or_notification_failure(self):
        self.configure(); self.backend.advance_due(DUE); self.accepted()
        claim = self.backend.claim_guest(APP, owner="A", now_utc=DUE)
        response = self.backend.scheduler_acknowledge_occurrence(APP, claim["event"]["occurrence"], "failed", "ack", caller_generation="gen-1", delivery_token=claim["token"])
        self.assertTrue(response["ok"])
        self.backend.finish_guest(APP, claim["guest_event_id"], claim["token"], result="success")
        event = self.row()["guest_events"][claim["guest_event_id"]]
        self.assertEqual(event["state"], "acked")
        self.assertEqual(event["guest_outcome"], "failed")
        self.assertEqual(next(iter(self.row()["outbox"].values()))["state"], "accepted")

    def test_incomplete_persisted_ledger_fails_closed(self):
        self.state["scheduler"]["apps"][APP]["alarms"]["incomplete"] = {"enabled": True}
        with self.assertRaises(sb.SchedulerError) as raised:
            sb.SchedulerBackend(self.state, tzdata_label="test")
        self.assertEqual(raised.exception.code, "integrity-failure")

    def test_unavailable_adapter_is_never_accepted(self):
        adapter = sb.UnavailableNotificationAdapter()
        self.assertFalse(adapter.available)
        with self.assertRaises(sb.SchedulerError) as raised:
            adapter.put("notify:test", PROJECTION)
        self.assertEqual(raised.exception.code, "capability-unavailable")

    def test_state_resource_bound_fails_atomically_without_evicting_audit(self):
        self.configure()
        before = copy.deepcopy(self.state)
        with patch.object(sb, "MAX_ROWS", 1):
            response = self.command("alarm-disable", {"alarm_id": "alarm-control", "expected_revision": 1}, key="would-exceed")
        self.assertEqual(response["error"]["code"], "resource-limit")
        self.assertEqual(self.state, before)


class PrivateJsonStoreTests(unittest.TestCase):
    def test_restart_atomic_rollback_private_modes_and_noop_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ledger"
            store = sb.PrivateJsonStore(root)
            with store.transaction() as state:
                backend = sb.SchedulerBackend(state, tzdata_label="test")
                backend.host_context(APP, active_generation="gen-1", enabled=True, scheduler_granted=True, notification_permission="authorized")
                command = FIXTURES["wire_examples"]["gui_alarm_configure"]["command"]
                self.assertTrue(backend.command(APP, command, idempotency_key="create", now_utc=NOW)["ok"])
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "state.json").stat().st_mode), 0o600)
            before = (root / "state.json").read_bytes()
            with self.assertRaises(RuntimeError):
                with store.transaction() as state:
                    state["bad"] = "must roll back"
                    raise RuntimeError("synthetic crash before commit")
            self.assertEqual((root / "state.json").read_bytes(), before)
            store = sb.PrivateJsonStore(root)
            state = store.read()
            self.assertEqual(state["scheduler"]["apps"][APP]["alarms"]["alarm-control"]["next_occurrence_id"], f"occ:alarm-control:r1:{DUE}")
            mtime = (root / "state.json").stat().st_mtime_ns
            with store.transaction() as state:
                sb.SchedulerBackend(state, tzdata_label="test").status(APP, "alarm-control")
            self.assertEqual((root / "state.json").stat().st_mtime_ns, mtime)

    def test_fsync_failure_before_replace_preserves_prior_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            store = sb.PrivateJsonStore(Path(temp) / "ledger")
            with store.transaction() as state:
                state["value"] = 1
            with patch.object(sb.os, "fsync", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError):
                    with store.transaction() as state:
                        state["value"] = 2
            self.assertEqual(store.read(), {"value": 1})
            self.assertEqual(list(store.root.glob(".state-*")), [])

    def test_nested_lock_and_second_owner_fail_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            store = sb.PrivateJsonStore(Path(temp) / "ledger")
            other = sb.PrivateJsonStore(store.root)
            with store.transaction():
                with self.assertRaises(sb.SchedulerError) as raised:
                    with store.transaction():
                        pass
                self.assertEqual(raised.exception.code, "conflict")
                with self.assertRaises(sb.SchedulerError):
                    with other.transaction():
                        pass

    def test_guest_kv_and_ack_both_rollback_then_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            store = sb.PrivateJsonStore(Path(temp) / "ledger")
            with store.transaction() as state:
                backend = sb.SchedulerBackend(state, tzdata_label="test")
                backend.host_context(APP, active_generation="gen-1", enabled=True, scheduler_granted=True, notification_permission="authorized")
                backend.command(APP, FIXTURES["wire_examples"]["gui_alarm_configure"]["command"], idempotency_key="create", now_utc=NOW)
                backend.advance_due(DUE)
                claim = backend.claim_guest(APP, owner="A", now_utc=DUE)
            def finish(state):
                backend = sb.SchedulerBackend(state, tzdata_label="test")
                state["synthetic_kv"] = {"count": 1}
                ack = backend.scheduler_acknowledge_occurrence(APP, claim["event"]["occurrence"], "applied", "ack", caller_generation="gen-1", delivery_token=claim["token"])
                self.assertTrue(ack["ok"])
                backend.finish_guest(APP, claim["guest_event_id"], claim["token"], result="success")
            with self.assertRaises(RuntimeError):
                with store.transaction() as state:
                    finish(state)
                    raise RuntimeError("synthetic crash before joint commit")
            self.assertNotIn("synthetic_kv", store.read())
            event = store.read()["scheduler"]["apps"][APP]["guest_events"][claim["guest_event_id"]]
            self.assertEqual(event["state"], "pending")
            self.assertIsNone(event["ack_requested"])
            with store.transaction() as state:
                finish(state)
            state = store.read()
            self.assertEqual(state["synthetic_kv"], {"count": 1})
            self.assertEqual(state["scheduler"]["apps"][APP]["guest_events"][claim["guest_event_id"]]["state"], "acked")

    def test_store_rejects_unsafe_root_and_symlink_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "public"
            root.mkdir(mode=0o755)
            with self.assertRaises(sb.SchedulerError):
                sb.PrivateJsonStore(root)
            store = sb.PrivateJsonStore(Path(temp) / "private")
            (store.root / "state.json").symlink_to(root / "other.json")
            with self.assertRaises(OSError):
                store.read()


if __name__ == "__main__":
    unittest.main()
