"""Candidate pruning must equal the former full combinations in values and order."""
from dataclasses import asdict
from itertools import combinations
import random

import pytest

from backend.rules.common import comparable_pairs


def legacy_pairs(facts):
    from backend.rules.rules_b import _same_object, _uncertain
    for left, right in combinations(facts, 2):
        if _uncertain(left) or _uncertain(right) or not _same_object(left, right):
            continue
        if left.get("comparison_key") and right.get("comparison_key") and left["comparison_key"] != right["comparison_key"]:
            continue
        yield left, right


def fact(index, **values):
    return {"id": str(index), "unit_id": f"u{index%3}", "text": "尺寸说明", "category": "尺寸",
            "confidence": 1.0, "unclear": False, **values}


def assert_equivalent(facts):
    before = repr(facts)
    actual, expected = list(comparable_pairs(facts)), list(legacy_pairs(facts))
    assert [(a["id"], b["id"]) for a, b in actual] == [(a["id"], b["id"]) for a, b in expected]
    assert all(a is x and b is y for (a, b), (x, y) in zip(actual, expected))
    assert repr(facts) == before
    return actual


def test_identity_paths_wildcards_precedence_and_table_local_guard():
    rows = [
        fact(0, object_id="P1", identity_confirmed=True, comparison_key="宽度"),
        fact(1, object_id="P1", identity_confirmed=True),
        fact(2, object_id="P1", identity_confirmed=True, comparison_key="深度"),
        fact(3, object_id="P2", identity_confirmed=True, comparison_key="宽度"),
        fact(4, comparison_key="桥架1跨度", identity_confirmed=True),
        fact(5, object_id="通用对象名", comparison_key="桥架1跨度", identity_confirmed=True),
        fact(6, object_id="P1", identity_confirmed=True, quantity_role="material_table", unit_id="same-table"),
        fact(7, object_id="P1", identity_confirmed=True, quantity_role="material_table", unit_id="same-table"),
        fact(8, object_id="P1", identity_confirmed=True, quantity_role="material_table", unit_id="other-table"),
        fact(9, object_id="未确认", identity_confirmed=True),
        fact(10, object_id="未确认", identity_confirmed=True),
    ]
    pairs = {(a["id"], b["id"]) for a, b in assert_equivalent(rows)}
    assert ("0", "1") in pairs and ("1", "2") in pairs and ("4", "5") in pairs
    assert ("0", "2") not in pairs and ("0", "3") not in pairs
    assert ("6", "7") not in pairs and ("6", "8") in pairs and ("9", "10") not in pairs


def test_canonical_code_branch_is_directional_and_does_not_override_explicit_identity():
    rows = [
        fact(0, text="xb-001a", category="编号写法"),
        fact(1, text="XB1A", category="材料规格"),
        fact(2, text="XB-1A", category="尺寸"),
        fact(3, text="XB1A", object_id="P1", identity_confirmed=False),
        fact(4, text="XB1A", object_id="P2", identity_confirmed=False),
        fact(5, text="XB2A", category="编号写法"),
    ]
    pairs = {(a["id"], b["id"]) for a, b in assert_equivalent(rows)}
    assert ("0", "1") in pairs and ("0", "2") in pairs
    assert ("1", "2") not in pairs and ("3", "4") not in pairs
    assert not any("5" in pair for pair in pairs)


def test_overlapping_index_paths_emit_each_positive_pair_once_in_original_order():
    rows = [fact(i, text="P-01", object_id="P1", comparison_key="P1-宽度", identity_confirmed=True)
            for i in range(20)]
    pairs = assert_equivalent(rows)
    assert len(pairs) == 190
    assert list(comparable_pairs(iter(rows))) == pairs


@pytest.mark.parametrize("seed", range(12))
def test_index_matches_legacy_for_mixed_valid_invalid_identity_confidence_and_categories(seed):
    rng = random.Random(seed)
    rows = [fact(i,
        text=rng.choice(["尺寸说明", "123", "XB-001", "xb1", "P1A", "P-1a", "AB-02", "other"]),
        category=rng.choice(["尺寸", "标高", "编号写法", "编号", "材料规格", "数量与材料表", ""]),
        object_id=rng.choice([None, "", "P1", "P2", " P1 ", "通用名", "序号1", "1_材料表", 3]),
        comparison_key=rng.choice([None, "", "P1-宽度", "P2-宽度", "宽度", "序号1", " P1-宽度 "]),
        identity_confirmed=rng.choice([False, True, 0, 1, "", "false"]),
        confidence=rng.choice([None, "bad", float("nan"), float("inf"), .54, .55, 1, 1.01, True]),
        unclear=rng.choice([False, False, True]),
        quantity_role=rng.choice([None, "material_table", "drawing_count"])) for i in range(120)]
    assert_equivalent(rows)


def test_large_unrelated_numeric_facts_do_not_call_pair_predicate(monkeypatch):
    from backend.rules import rules_b
    original = rules_b._same_object
    calls = []
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(rules_b, "_same_object", counted)
    rows = [fact(i, text=str(i)) for i in range(40000)]
    assert list(comparable_pairs(rows)) == []
    assert calls == []  # Former combinations would call the predicate 799,980,000 times.
    assert len(rows) == 40000


def test_rule_a_dry_run_outputs_equal_legacy_enumerator(repo, seeded_rule_a, monkeypatch):
    from backend.rules import RulesAService, measurement
    version_id, task_id = seeded_rule_a["version"]["id"], seeded_rule_a["task"]["id"]
    current = asdict(RulesAService(repo).run(version_id, task_id=task_id, persist=False))
    monkeypatch.setattr(measurement, "comparable_pairs", legacy_pairs)
    expected = asdict(RulesAService(repo).run(version_id, task_id=task_id, persist=False))
    assert current == expected
    assert current["detail"]["dimension"]["issue_count"] > 0
    assert current["detail"]["elevation"]["issue_count"] > 0
