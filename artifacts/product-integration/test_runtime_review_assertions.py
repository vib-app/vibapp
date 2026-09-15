"""Synthetic assertion-unit tests, not application or release acceptance."""
import unittest
from unittest.mock import Mock
from review_runtime_acceptance import RuntimeReview


class TextAssertionTests(unittest.TestCase):
    def review(self, kind=None):
        review = RuntimeReview.__new__(RuntimeReview)
        review.binding = {"semantic_surface": {"view": {"title": "3.28084", "nodes": [
            {"id": "result", "kind": kind or {"text": {"text": "3.28084 英尺", "style": "title"}}}
        ]}}}
        review.snapshot = lambda label: None
        return review

    def test_exact_and_partial_rendered_text(self):
        for expectation in ({"equals": "3.28084 英尺"}, {"contains": "3.28084"}):
            self.review().step({"operation": "assert-node-text", "node": "result", **expectation})

    def test_metadata_does_not_satisfy_text_assertion(self):
        with self.assertRaises(AssertionError):
            self.review({"text": {"text": "0"}}).step({"operation": "assert-node-text", "node": "result", "contains": "3.28084"})

    def test_field_is_not_a_rendered_text_node(self):
        with self.assertRaises(ValueError):
            self.review({"field": {"value": {"text": "3.28084"}}}).step({"operation": "assert-node-text", "node": "result", "equals": "3.28084"})

    def test_missing_or_ambiguous_expectation_is_rejected(self):
        for expectation in ({}, {"equals": "x", "contains": "x"}):
            with self.assertRaises(ValueError):
                self.review().step({"operation": "assert-node-text", "node": "result", **expectation})

    def test_number_uses_real_text_and_explicit_unit_not_display_precision(self):
        self.review({"Text": {"text": "3.280839 英尺"}}).step({"operation": "assert-node-number", "node": "result", "expected": "3.28084", "suffix": " 英尺"})
        with self.assertRaises(AssertionError):
            self.review({"Text": {"text": "3.28 英尺"}}).step({"operation": "assert-node-number", "node": "result", "expected": "3.28084", "suffix": " 英尺"})
        with self.assertRaises(AssertionError):
            self.review({"Text": {"text": "3.28084 千克"}}).step({"operation": "assert-node-number", "node": "result", "expected": "3.28084", "suffix": " 英尺"})

    def test_number_rejects_nonfinite_loose_tolerances_and_unparsed_labels(self):
        for rendered in ("NaN", "Infinity", "-Infinity", "result=3.28084"):
            with self.subTest(rendered=rendered), self.assertRaises(ValueError):
                self.review({"Text": {"text": rendered}}).step({"operation": "assert-node-number", "node": "result", "expected": "3.28084"})
        for tolerance in ("NaN", "Infinity", "-1", "1"):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                self.review({"Text": {"text": "0"}}).step({"operation": "assert-node-number", "node": "result", "expected": "0", "absolute_tolerance": tolerance})


class AdvertisedNodeSelectionTests(unittest.TestCase):
    def review(self):
        review = RuntimeReview.__new__(RuntimeReview)
        review.binding = {"semantic_surface": {"view": {"nodes": [
            {"id": f"history.recall.{index}", "kind": {"Button": {"label": "回填", "action": f"history.{index}", "disabled": index == 2}}}
            for index in range(3)
        ] + [{"id": "history.text.0", "kind": {"Text": {"text": "2+3 = 5"}}}]}}}
        review.sequence = 1
        review.event_binding = lambda: {"surface": "trusted-surface"}
        review.call = Mock(return_value=review.binding)
        review.snapshot = lambda label: None
        return review

    def test_duplicate_labels_require_actual_enabled_node_and_matching_label(self):
        review = self.review()
        with self.assertRaises(ValueError):
            review.step({"operation": "action", "label": "回填"})
        review.call.assert_not_called()
        review.step({"operation": "action", "label": "回填", "node": "history.recall.1"})
        self.assertEqual(review.call.call_args.args[1]["action"], "history.1")
        self.assertEqual(review.call.call_args.args[1]["surface"], "trusted-surface")

    def test_nonadvertised_disabled_and_mismatched_nodes_cannot_dispatch(self):
        for node, label in (("missing", "回填"), ("history.recall.2", "回填"), ("history.recall.0", "伪造"), ("history.text.0", "回填"), (None, "回填"), ("", "回填")):
            with self.subTest(node=node, label=label):
                review = self.review()
                with self.assertRaises(ValueError):
                    review.step({"operation": "action", "label": label, "node": node})
                review.call.assert_not_called()

    def test_node_count_is_exact_and_can_require_semantic_kind(self):
        review = self.review()
        review.step({"operation": "assert-node-count", "prefix": "history.recall.", "expected": 3, "kind": "Button"})
        review.step({"operation": "assert-node-count", "prefix": "history.", "expected": 1, "kind": "Text"})
        review.step({"operation": "assert-node-count", "prefix": "missing.", "expected": 0})
        with self.assertRaises(AssertionError):
            review.step({"operation": "assert-node-count", "prefix": "history.", "expected": 2, "kind": "Text"})

    def test_node_count_rejects_unbounded_or_malformed_selectors(self):
        for invalid in ({"prefix": ""}, {"prefix": None}, {"prefix": "x" * 257}, {"expected": True}, {"expected": -1}, {"expected": 2049}, {"expected": "1"}, {"kind": "HTML"}, {"kind": []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.review().step({"operation": "assert-node-count", "prefix": "history.", "expected": 4, **invalid})


class StructuredRejectionTests(unittest.TestCase):
    def review(self, response):
        review = AdvertisedNodeSelectionTests().review()
        review.app_id = "ai.vibapp.test"
        review.daemon = Mock()
        review.daemon.execute.return_value = response
        review.snapshots = []
        return review

    def action(self, **changes):
        return {"operation": "assert-action-rejected", "label": "回填", "node": "history.recall.0",
                "code": "invalid-argument", "message_contains": "32", **changes}

    def test_exact_structured_rejection_retains_surface_and_never_replays(self):
        error = {"code": "invalid-argument", "message": "输入最多32个字符", "retryable": False}
        review = self.review({"error": error})
        binding = review.binding
        review.step(self.action())
        self.assertIs(review.binding, binding)
        review.daemon.execute.assert_called_once()
        self.assertEqual(review.sequence, 2)
        self.assertEqual(review.snapshots, [{"label": "structured-action-rejection", "error": error}])
        review.call.assert_not_called()

    def test_success_wrong_errors_retryable_and_malformed_cannot_pass(self):
        expected = {"code": "invalid-argument", "message": "limit32", "retryable": False}
        responses = [None, {}, {"outcome": {"value": {}}}, {"error": None}, {"error": "32"},
                     {"error": expected, "outcome": {}},
                     *({"error": {**expected, **change}} for change in (
                         {"code": "internal"}, {"retryable": True}, {"retryable": 0},
                         {"message": "different"}, {"message": 32}, {"extra": "32"}))]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(AssertionError):
                self.review(response).step(self.action())

    def test_unbounded_expectations_and_nonadvertised_actions_never_dispatch(self):
        for changes in ({"code": None}, {"code": ""}, {"code": "x" * 65},
                        {"message_contains": ""}, {"message_contains": "x" * 513},
                        {"node": "missing"}, {"node": "history.recall.2"}):
            review = self.review({})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review.step(self.action(**changes))
            review.daemon.execute.assert_not_called()


class DurationAndFieldErrorTests(unittest.TestCase):
    review = TextAssertionTests.review
    def duration(self, text, **expected):
        review = self.review({"Text": {"text": text}})
        review.saved = {"paused": '{"Text":{"text":"00:00:02"}}'}
        review.step({"operation": "assert-node-duration", "node": "result", "minimum_seconds": 2, "maximum_seconds": 3, **expected})

    def test_duration_requires_real_hms_and_explicit_range(self):
        self.duration("00:00:02")
        self.duration("00:00:02.500")
        self.duration("24:00:00", minimum_seconds=86400, maximum_seconds=86400)
        for text in ("00:00:00", "00:00:04"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                self.duration(text)
        for text in ("-00:00:01", "0:00:02", "00:60:00", "00:00:60", "NaN", "00:00:02 seconds", " 00:00:02", "1000:00:00"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.duration(text)

    def test_duration_baseline_compares_elapsed_not_merely_nonzero(self):
        self.duration("00:00:04", relative_to="paused")
        self.duration("00:00:01", relative_to="paused", minimum_seconds=-1, maximum_seconds=-1)
        with self.assertRaises(AssertionError):
            self.duration("00:00:02", relative_to="paused")
        with self.assertRaises(ValueError):
            self.duration("00:00:04", relative_to="missing")
        for bounds in ({"minimum_seconds": "NaN"}, {"maximum_seconds": "Infinity"}, {"minimum_seconds": 4}, {"maximum_seconds": 3600001}):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                self.duration("00:00:02", **bounds)

    def field(self, validation, **expected):
        review = self.review({"Field": {"field": "seconds", "value": {"Text": "请输入"}, "validation_message": validation}})
        review.step({"operation": "assert-field-error", "field": "seconds", **expected})

    def test_field_error_is_actual_validation_not_input_or_label(self):
        self.field("请输入整数", contains="整数")
        self.field("请输入整数", equals="请输入整数")
        for value in (None, "", False):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                self.field(value, contains="请输入")
        with self.assertRaises(AssertionError):
            self.field("错误", contains="请输入")
        for expectation in ({}, {"equals": "x", "contains": "x"}, {"contains": ""}):
            with self.subTest(expectation=expectation), self.assertRaises(ValueError):
                self.field("请输入", **expectation)

    def test_duplicate_fields_cannot_satisfy_validation_or_persistence(self):
        review = self.review({"Field": {"field": "seconds", "value": {"Text": "3"}, "validation_message": "错误"}})
        review.binding["semantic_surface"]["view"]["nodes"].append({"id": "other", "kind": {"Field": {"field": "seconds", "value": {"Text": "4"}, "validation_message": None}}})
        for operation, expected in (("assert-field", {"value": "3"}), ("assert-field-error", {"equals": "错误"})):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                review.step({"operation": operation, "field": "seconds", **expected})

    def test_choice_assertion_requires_exact_variant_and_value(self):
        review = self.review({"Field": {"field": "unit", "value": {"Choice": "m"}}})
        review.step({"operation": "assert-field", "field": "unit", "value_kind": "choice", "value": "m"})
        with self.assertRaises(AssertionError):
            review.step({"operation": "assert-field", "field": "unit", "value_kind": "choice", "value": "cm"})
        for expectation in ({"value": "m"}, {"value": None}, {"value_kind": "integer", "value": "m"}, {"value_kind": [], "value": "m"}):
            with self.subTest(expectation=expectation), self.assertRaises(ValueError):
                review.step({"operation": "assert-field", "field": "unit", **expectation})


if __name__ == "__main__":
    unittest.main()
