"""Test-plan shape/oracle tests, not app functionality acceptance."""
import unittest

from converter_runtime_plans import boundary_plan, immediate_plan


class ConverterPlanTests(unittest.TestCase):
    def test_boundary_payloads_reach_utf8_and_u8_failures_and_real_gui_clear(self):
        plan = boundary_plan()
        fields = [field["value"] for step in plan for field in step.get("fields", [])]
        payloads = [field["value"] for field in fields if field["tag"] == "text"]
        self.assertIn("中" * 86, payloads)
        self.assertEqual(len(("中" * 86).encode()), 258)
        self.assertIn("9" * 256, payloads)
        self.assertIn("9" * 257, payloads)
        self.assertIn({"tag": "empty"}, fields)
        self.assertLessEqual(len(plan), 64)
        for step in plan:
            for field in step.get("fields", []):
                self.assertEqual(field["field"], "value")
                self.assertIn(field["value"]["tag"], {"text", "empty"})

    def test_category_result_is_asserted_before_another_convert_or_refresh(self):
        plan = immediate_plan()
        self.assertEqual([step["equals"] for step in plan if "equals" in step], ["2.20462", "33.8", "3.28084"])
        self.assertEqual(sum(step.get("label") == "换算" for step in plan), 1)
        self.assertNotIn("refresh", [step["operation"] for step in plan])
        for index in (1, 3, 5):
            self.assertEqual(plan[index]["operation"], "action")
            self.assertEqual(plan[index + 1]["operation"], "assert-node-text")


if __name__ == "__main__":
    unittest.main()
