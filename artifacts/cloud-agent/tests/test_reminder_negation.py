"""NeedSpec polarity regressions; no provider, compiler, or saved-task mutation."""
from __future__ import annotations

import unittest

from test_cloud_agent import rebind, reminder_task_fixture, task_fixture
from cloud_agent import WorkerError, validate_generated_semantics, validate_task_schema


UI_ACTION_SOURCE = {
    "src/lib.rs": """
        let node = NodeKind::Button(ButtonNode { disabled: false });
        match event { LauncherEvent::Action(event) => update(event) }
    """,
}


def foreground_task(text: str, *, field: str = "text") -> dict:
    task = task_fixture()
    task["need_spec"]["goal"] = "A foreground time display."
    requirement = {
        "requirement_id": "req-foreground",
        "priority": "must-have",
        "text": "Display the current time while the view is open.",
        "acceptance_examples": ["The current time is visible."],
    }
    task["need_spec"]["requirements"] = [requirement]
    if field == "goal":
        task["need_spec"]["goal"] = text
    elif field == "text":
        requirement["text"] = text
    elif field == "example":
        requirement["acceptance_examples"] = [text]
    else:
        raise AssertionError(field)
    return rebind(task)


class ReminderNegationTests(unittest.TestCase):
    def assert_reminder_rejected(self, task: dict) -> None:
        with self.assertRaises(WorkerError) as caught:
            validate_task_schema(task)
        self.assertEqual(caught.exception.code, "semantic-reminder-contract-mismatch")

    def test_exact_foreground_clock_need_does_not_require_background_authority(self) -> None:
        # Verbatim user prose from the real failed admission, with no dependency
        # on private evidence, app IDs, consent timestamps, or generated output.
        task = foreground_task(
            "制作仅前台运行的数字时钟、秒表和倒计时。数字时钟使用宿主真实时间并明确显示时区；"
            "秒表支持开始、暂停、继续、重置，倒计时支持1秒至24小时的预设、开始、暂停、继续和重置，"
            "到零停止并显示完成。时间变化来自真实经过时间，不按按钮点击次数伪造。"
            "使用应用私有KV保存模式、倒计时预设和暂停值；关闭重开恢复保存值并处于暂停，"
            "不承诺关闭窗口后的计时、闹钟或后台通知。空值、负值和越界预设有明确错误。"
            "内容区不重复宿主标题栏、不显示技术ID，窄窗自适应。"
        )
        task["need_spec"]["requirements"][0]["acceptance_examples"] = [
            "设置3秒倒计时，前台等待后归零、显示完成且不出现负数；暂停/继续与重置都有效。"
            "暂停后宿主刷新及关闭重开恢复模式、预设、暂停值且不自动启动；"
            "初次进入显示清晰零值或设置提示，无后台通知承诺。"
        ]
        task["need_spec"]["permission_ceiling"]["forbidden_interfaces"].extend([
            "vibapp:experimental-v0/scheduler@0.0.1",
            "vibapp:experimental-v0/notification@0.0.1",
        ])
        validated = validate_task_schema(rebind(task))
        self.assertEqual(validated["target"]["app_kind"], "ui")
        validate_generated_semantics(validated, UI_ACTION_SOURCE)

    def test_explicit_negation_and_coordination_in_each_need_field(self) -> None:
        negatives = (
            "不承诺关闭窗口后的计时、闹钟或后台通知。",
            "不提供闹钟或提醒功能。",
            "禁止闹钟和定时通知。",
            "不得创建提醒。",
            "无需提醒。",
            "不支持闹钟，但需要显示当前时间。",
            "显示当前时间，而不是闹钟。",
            "No alarm or reminder is provided.",
            "No alarm, reminder, or scheduled notification is provided.",
            "A foreground display without an alarm or reminder.",
            "Do not provide an alarm or scheduled notification.",
            "Does not promise background timing, an alarm, or a reminder.",
            "Must not notify me at midnight.",
            "Never create a reminder.",
            "No alarm is provided, but the current time is displayed.",
        )
        for field in ("goal", "text", "example"):
            for text in negatives:
                with self.subTest(field=field, text=text):
                    task = validate_task_schema(foreground_task(text, field=field))
                    validate_generated_semantics(task, UI_ACTION_SOURCE)

    def test_positive_ambiguous_and_contradictory_reminders_still_fail_closed(self) -> None:
        positives = (
            "每天设置闹钟。",
            "无需联网，但每天设置闹钟。",
            "不提供闹钟，但需要每天提醒喝水。",
            "不提供闹钟；每天提醒喝水。",
            "不提供闹钟，然而每天定时通知用户。",
            "禁止闹钟。另一个要求是每天提醒喝水。",
            "不要错过每天的闹钟。",
            "不要删除闹钟。",
            "不要禁止闹钟。",
            "取消禁止闹钟。",
            "不能不提供闹钟。",
            "不只是闹钟，还要提醒。",
            "没有设置闹钟时显示提示。",
            "Set an alarm every morning.",
            "No network access, but set an alarm every morning.",
            "No alarm, but send a reminder every morning.",
            "No alarm; notify me at midnight.",
            "No alarm, however a reminder is required.",
            "No alarm, a reminder is required.",
            "No alarm, a scheduled notification is required.",
            "No alarm and a reminder is required.",
            "No alarm and the scheduled notification is required.",
            "No alarm is provided. A reminder is required.",
            "Do not forget to set an alarm.",
            "Do not disable the alarm.",
            "No missed alarm is allowed.",
            "Not only an alarm but also a reminder.",
            "If no alarm is set, display a prompt.",
            "Not without an alarm.",
        )
        for field in ("goal", "text", "example"):
            for text in positives:
                with self.subTest(field=field, text=text):
                    self.assert_reminder_rejected(foreground_task(text, field=field))

        # A negative goal, separate requirement, or permission prohibition must
        # never erase a positive occurrence elsewhere in the same NeedSpec.
        task = foreground_task("No alarm is provided.", field="goal")
        task["need_spec"]["requirements"][0]["acceptance_examples"] = [
            "Receive a reminder every morning."
        ]
        task["need_spec"]["requirements"].append({
            "requirement_id": "req-negative",
            "priority": "must-have",
            "text": "不提供闹钟或提醒。",
            "acceptance_examples": ["No alarm is created."],
        })
        task["need_spec"]["negative_constraints"].append({
            "constraint_id": "no-scheduler",
            "kind": "forbidden-capability",
            "value": "vibapp:experimental-v0/scheduler@0.0.1",
            "source": "user",
        })
        self.assert_reminder_rejected(rebind(task))

    def test_generic_mixed_object_exclusions_do_not_need_domain_vocabulary(self) -> None:
        negatives = (
            "不提供重复日程、外部日历同步、系统闹钟或后台通知。",
            "不提供云同步、数据导出、系统闹钟或后台通知。",
            "禁止数据导出和系统闹钟。",
            "Do not provide calendar synchronization, an alarm, or a reminder.",
            "Do not provide cloud sync, data export, or a system alarm.",
            "No cloud sync, data export, or scheduled notification is provided.",
            "A foreground display without cloud sync or an alarm.",
        )
        positives = (
            "不提供云同步、数据导出，但每天设置闹钟。",
            "不提供云同步、数据导出、系统闹钟；每天提醒喝水。",
            "不提供云同步、必须设置系统闹钟。",
            "不提供云同步、需要系统闹钟。",
            "不提供云同步、要闹钟。",
            "不能不提供云同步、系统闹钟或后台通知。",
            "不要禁止数据导出和系统闹钟。",
            "No cloud sync or data export, but set an alarm.",
            "No cloud sync, data export, or alarm. A reminder is required.",
            "Do not provide cloud sync, but send a reminder every morning.",
            "No cloud sync, an alarm is required.",
            "No cloud sync and a reminder is required.",
            "No cloud sync or missed alarm is allowed.",
            "Do not disable cloud sync or the alarm.",
            "Not without cloud sync or an alarm.",
            "No cloud sync, and my alarm rings every morning.",
            "No cloud sync, and each alarm repeats daily.",
            "Do not provide cloud sync, and our reminder runs every morning.",
            "No data export, and the alarm sounds at midnight.",
            "No cloud sync, and an alarm rings daily.",
            "不提供云同步、我的闹钟每天响铃。",
            "不提供云同步、每个闹钟每天重复。",
            "不提供云同步、我们的提醒每天运行。",
            "不提供数据导出、系统闹钟每天鸣响。",
            "不提供云同步和闹钟每天响铃。",
        )
        for field in ("goal", "text", "example"):
            for text in negatives:
                with self.subTest(field=field, negative=text):
                    task = foreground_task(text, field=field)
                    validate_task_schema(task)
                    validate_generated_semantics(task, UI_ACTION_SOURCE)
            for text in positives:
                with self.subTest(field=field, positive=text):
                    task = foreground_task(text, field=field)
                    self.assert_reminder_rejected(task)
                    with self.assertRaises(WorkerError) as caught:
                        validate_generated_semantics(task, UI_ACTION_SOURCE)
                    self.assertEqual(caught.exception.code, "semantic-reminder-policy-invalid")

    def test_generated_semantic_gate_uses_the_same_occurrence_polarity(self) -> None:
        # Exercise this independently: fixing admission alone must not merely
        # defer the same false failure until after a paid model invocation.
        task = foreground_task("不承诺关闭窗口后的计时、闹钟或后台通知。")
        validate_generated_semantics(task, UI_ACTION_SOURCE)
        task = foreground_task("No alarm, but a daily reminder is required.")
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(task, UI_ACTION_SOURCE)
        self.assertEqual(caught.exception.code, "semantic-reminder-policy-invalid")

    def test_real_hybrid_reminder_controls_survive_nearby_negation(self) -> None:
        task = reminder_task_fixture()
        task["need_spec"]["goal"] = "No unrelated alarm. " + task["need_spec"]["goal"]
        task = validate_task_schema(rebind(task))
        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(task, {"src/lib.rs": """
                scheduler::upsert(ScheduleRequest {
                    purpose: SchedulePurpose::Alarm, missed: MissedPolicy::Skip,
                });
            """})
        self.assertEqual(caught.exception.code, "semantic-reminder-policy-invalid")
        self.assertIn("missed-policy=fire-once", str(caught.exception))

        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(task, {"src/lib.rs": """
                scheduler::upsert(ScheduleRequest {
                    purpose: SchedulePurpose::Alarm, missed: MissedPolicy::FireOnce,
                });
            """})
        self.assertEqual(caught.exception.code, "semantic-reminder-controls-missing")
        self.assertIn("configurable-time", str(caught.exception))

        with self.assertRaises(WorkerError) as caught:
            validate_generated_semantics(task, {"src/lib.rs": """
                scheduler::upsert(ScheduleRequest {
                    purpose: SchedulePurpose::Alarm, missed: MissedPolicy::FireOnce,
                });
                let field = FieldKind::Time;
                let node = NodeKind::Button(ButtonNode { disabled: false });
                match event { LauncherEvent::Action(event) => update(event) }
            """})
        self.assertEqual(caught.exception.code, "semantic-reminder-controls-missing")
        self.assertIn("scheduler.disable", str(caught.exception))
        self.assertIn("scheduler.enable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
