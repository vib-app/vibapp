"""Independent mathematical and interaction oracles for the real converter.

This constructs test inputs only, never app source. Selectors are supplied after
reading the actual guest's advertised UI. Expected conversions are fixed here.
"""


def text(value):
    return {"field": "value", "value": {"tag": "text", "value": value}}


def boundary_plan(convert="换算", error_node="error", result_node="result"):
    """Reject oversized inputs, remain correctable, refresh and persist safely."""
    plan = []
    for value in ("中" * 86, "9" * 256, "9" * 257):
        plan.extend([
            {"operation": "action", "label": convert, "fields": [text(value)]},
            {"operation": "assert-node-text", "node": error_node, "contains": "过长"},
            {"operation": "refresh", "after_seconds": 1.1},
            {"operation": "reopen"},
            {"operation": "action", "label": convert, "fields": [text("1")]},
            {"operation": "assert-node-text", "node": result_node, "equals": "3.28084"},
        ])
    plan.extend([
        {"operation": "action", "label": convert, "fields": [{"field": "value", "value": {"tag": "empty"}}]},
        {"operation": "assert-field", "field": "value", "value": ""},
        {"operation": "assert-node-text", "node": error_node, "contains": "请输入"},
        {"operation": "refresh", "after_seconds": 1.1},
        {"operation": "reopen"},
        {"operation": "assert-field", "field": "value", "value": ""},
        {"operation": "action", "label": convert, "fields": [text("1")]},
        {"operation": "assert-node-text", "node": result_node, "equals": "3.28084"},
    ])
    return plan


def immediate_plan(convert="换算", weight="重量", temperature="温度", length="长度", result_node="result"):
    plan = [{"operation": "action", "label": convert, "fields": [text("1")]}]
    for label, expected in ((weight, "2.20462"), (temperature, "33.8"), (length, "3.28084")):
        plan.extend([
            {"operation": "action", "label": label},
            {"operation": "assert-node-text", "node": result_node, "equals": expected},
        ])
    return plan
