"""Catalog contract checks only; never generate, execute, or accept an app."""
import json
from pathlib import Path
import re
import sys
import unittest


CATALOG = Path(__file__).resolve().parents[1] / "everyday_app_scenarios.json"
SCHEMA_VERSION = "vibapp.everyday-app-scenarios.experimental-v1"
KEYS = (
    "calculator", "converter", "clock", "notepad", "sticky-notes", "todo",
    "calendar", "contacts", "image-viewer", "drawing", "recorder", "file-manager",
)
READY_KEYS = {"calculator", "converter", "clock", "sticky-notes", "todo", "calendar", "contacts"}
APP_FIELDS = {
    "key", "name", "description", "capabilities", "acceptance", "preferred_window",
    "generation_ready", "blocker",
}


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate catalog key: {key}")
        result[key] = value
    return result


def validate_catalog(document):
    if not isinstance(document, dict) or set(document) != {"schema_version", "apps"}:
        raise ValueError("catalog top-level fields changed")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("catalog version changed")
    apps = document["apps"]
    if not isinstance(apps, list) or len(apps) != 12:
        raise ValueError("exactly twelve approved applications required")
    for index, app in enumerate(apps):
        if not isinstance(app, dict) or set(app) != APP_FIELDS:
            raise ValueError("application fields changed")
        if app["key"] != KEYS[index]:
            raise ValueError("application key/order changed")
        for field, minimum, maximum in (("name", 2, 30), ("description", 40, 700), ("acceptance", 80, 1000)):
            text = app[field]
            if not isinstance(text, str) or not minimum <= len(text) <= maximum or not re.search(r"[\u4e00-\u9fff]", text):
                raise ValueError(f"bounded Chinese {field} required")
        capabilities = app["capabilities"]
        if (not isinstance(capabilities, list) or not capabilities
                or any(not isinstance(item, str) or item not in {"clock", "kv", "settings"} for item in capabilities)
                or len(capabilities) != len(set(capabilities)) or "kv" not in capabilities):
            raise ValueError("only existing explicit capabilities permitted")
        expected = ["clock", "kv", "settings"] if app["key"] in {"clock", "calendar"} else ["kv", "settings"]
        if capabilities != expected:
            raise ValueError("application capability ceiling changed")
        window = app["preferred_window"]
        if (not isinstance(window, dict) or set(window) != {"width", "height"}
                or type(window["width"]) is not int or not 360 <= window["width"] <= 1200
                or type(window["height"]) is not int or not 360 <= window["height"] <= 900):
            raise ValueError("bounded preferred window required")
        ready = app["key"] in READY_KEYS
        if type(app["generation_ready"]) is not bool or app["generation_ready"] != ready:
            raise ValueError("only seven applications with available host capabilities are generation ready")
        if ready:
            if app["blocker"] is not None:
                raise ValueError("ready application must have a null blocker")
        elif not isinstance(app["blocker"], str) or not 20 <= len(app["blocker"]) <= 400 or "宿主" not in app["blocker"]:
            raise ValueError("pending application requires an explicit host API blocker")
    return apps


class EverydayScenariosTests(unittest.TestCase):
    def test_ready_foreground_scenarios_do_not_request_excluded_reminders(self):
        # Run real product interpretation on all approved foreground scenarios,
        # rather than weakening the prose or testing only the first failure.
        sys.path.insert(0, str(CATALOG.parents[1] / "cloud-agent"))
        from cloud_agent import need_requires_reminder
        for app in validate_catalog(json.loads(CATALOG.read_bytes())):
            if app["generation_ready"]:
                with self.subTest(app=app["key"]):
                    self.assertFalse(need_requires_reminder(app["description"] + "\n" + app["acceptance"]))

    def setUp(self):
        self.document = json.loads(CATALOG.read_text(encoding="utf8"), object_pairs_hook=strict_object)
        self.apps = {app["key"]: app for app in self.document["apps"]}

    def test_exact_version_shape_order_and_bounded_readiness(self):
        apps = validate_catalog(self.document)
        self.assertEqual(len(apps), 12)
        self.assertEqual(sum(app["generation_ready"] for app in apps), 7)
        self.assertEqual(sum(not app["generation_ready"] for app in apps), 5)
        self.assertEqual(len({app["name"] for app in apps}), 12)

    def test_duplicate_unknown_fields_and_fake_capabilities_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            json.loads('{"apps":[],"apps":[]}', object_pairs_hook=strict_object)
        for mutate in (
            lambda doc: doc.update(source="not allowed"),
            lambda doc: doc["apps"][0].update(source="not allowed"),
            lambda doc: doc["apps"][0].update(capabilities=["kv", "filesystem"]),
            lambda doc: doc["apps"][0].update(generation_ready=1),
            lambda doc: doc["apps"][0].update(preferred_window={"width": True, "height": 600}),
            lambda doc: doc["apps"][8].update(generation_ready=True, blocker=None),
            lambda doc: doc["apps"][3].update(generation_ready=True, blocker=None),
            lambda doc: doc["apps"][8].update(blocker=""),
        ):
            changed = json.loads(json.dumps(self.document))
            mutate(changed)
            with self.assertRaises(ValueError):
                validate_catalog(changed)

    def test_all_applications_require_private_persistence_errors_empty_states_and_responsive_chrome(self):
        for app in self.apps.values():
            with self.subTest(app=app["key"]):
                self.assertIn("应用私有KV", app["description"])
                self.assertIn("宿主刷新", app["acceptance"])
                self.assertRegex(app["acceptance"], r"关闭重开|关闭重新打开")
                self.assertIn("360像素", app["acceptance"])
                self.assertIn("不重复宿主标题栏", app["description"])
                self.assertIn("不显示技术ID", app["description"])
                self.assertRegex(app["description"], r"错误|失败|拒绝")
                self.assertIn("空", app["description"] + app["acceptance"])

    def test_catalog_covers_each_approved_function_with_concrete_examples(self):
        required = {
            "calculator": ("括号", "百分", "历史", "2+3*4", "200*10%", "1/0"),
            "converter": ("长度", "重量", "面积", "体积", "温度", "1公顷=10000平方米", "273.15"),
            "clock": ("数字时钟", "秒表", "倒计时", "暂停", "3秒", "真实时间", "不承诺"),
            "notepad": ("多篇", "搜索", "自动保存", "购物", "会议", "换行"),
            "sticky-notes": ("编辑", "置顶", "上移", "下移", "C、B、A"),
            "todo": ("分类", "到期日期", "完成", "恢复", "2026-02-30"),
            "calendar": ("月历", "当天", "日程", "闰年", "2028", "2027-02-29"),
            "contacts": ("手动", "姓名", "电话", "邮箱", "分组", "搜索", "lin@example.com"),
        }
        for key, tokens in required.items():
            text = self.apps[key]["description"] + self.apps[key]["acceptance"]
            for token in tokens:
                with self.subTest(app=key, token=token):
                    self.assertIn(token, text)

    def test_pending_apps_state_real_media_requirements_without_claiming_current_authority(self):
        requirements = {
            "notepad": ("自动保存", "输入变更回调", "多行输入", "显式操作", "不能用保存按钮或刷新冒充自动保存"),
            "image-viewer": ("选择", "缩放", "旋转", "实际像素", "不透明"),
            "drawing": ("画笔", "橡皮擦", "颜色", "撤销", "PNG", "语义画布"),
            "recorder": ("录制", "暂停", "播放", "命名", "删除", "麦克风"),
            "file-manager": ("主动导入", "分类", "搜索", "导出", "系统原文件保持不变", "跨应用隔离"),
        }
        for key, tokens in requirements.items():
            app = self.apps[key]
            self.assertFalse(app["generation_ready"])
            self.assertIn("须先解除宿主API阻塞", app["acceptance"])
            text = app["description"] + app["acceptance"] + app["blocker"]
            for token in tokens:
                with self.subTest(app=key, token=token):
                    self.assertIn(token, text)

    def test_sticky_notes_use_explicit_save_and_do_not_promise_unsupported_colors(self):
        sticky = self.apps["sticky-notes"]
        self.assertTrue(sticky["generation_ready"])
        self.assertIn("明确保存操作", sticky["description"])
        self.assertIn("并保存", sticky["acceptance"])
        self.assertNotIn("颜色", sticky["description"] + sticky["acceptance"])


if __name__ == "__main__":
    unittest.main()
