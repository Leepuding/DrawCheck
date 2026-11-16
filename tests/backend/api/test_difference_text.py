import pytest

from backend.api.difference_text import NO_DIFFERENCE, describe_difference
from backend.api.review_status import serialize_issue


@pytest.mark.parametrize("category, fields, expected", [
    ("目录与图签", {"left_value": "33-NZ16181S-T0102-04", "right_value": "33-NC09811S-T0102-04",
                  "match_basis": "unique_literal_name_in_source"},
     ["第 2 段 NZ16181S ↔ NC09811S", "其余 3 段一致", "可能沿用了其他项目的图框", "责任方"]),
    ("尺寸", {"left_value": "100mm", "right_value": "102mm", "max_abs_diff": "2", "tolerance_mm": 1},
     ["相差 2 mm", "允许偏差 1 mm"]),
    ("标高", {"left_value": "4.350m", "right_value": "4.360m", "abs_diff": ".010", "tolerance_m": .005},
     ["相差 0.010 m", "允许偏差 0.005 m"]),
    ("材料规格", {"left_value": "U40X60X2.5", "right_value": "U40X40X2.5"}, ["规格", "U40X60X2.5", "U40X40X2.5"]),
    ("数量与材料表", {"left_value": "长度440m", "right_value": "长度441m",
                  "quantity_difference": "1", "quantity_unit": "m"}, ["相差 1 m"]),
    ("编号写法", {"left_value": "编号 XB01", "right_value": "编号 XB-01",
               "compared_number_literals": {"left": "XB01", "right": "XB-01"}}, ["「XB01」", "「XB-01」"]),
])
def test_issue_reason_describes_readings_instead_of_echoing_pairing(category, fields, expected):
    row = {"id": "issue", "version_id": "v", "left_unit_id": "a", "right_unit_id": "b",
           "review_status": "疑似", "title": category, "rule_result": {
               "category": category, "basis": "按同一对象配对", **fields}}
    for side in ("left", "right"):
        row.update({f"{side}_{key}": value for key, value in zip(("x0", "y0", "x1", "y1"), (.1, .2, .3, .4))})
        row[f"{side}_text"] = fields[f"{side}_value"]
    finding = serialize_issue(row, {})["finding"]
    assert finding["reason"] != finding["basis"]
    assert all(snippet in finding["reason"] for snippet in expected)


@pytest.mark.parametrize("category", ["目录与图签", "尺寸", "标高", "材料规格", "未知"])
def test_equal_or_missing_readings_never_manufacture_a_difference(category):
    for rule in ({"left_value": "01", "right_value": "01"}, {"left_value": "01"}, {}):
        assert describe_difference(category, rule, "目录与原图按图名配对") == NO_DIFFERENCE
    reason = describe_difference("目录与图签",
        {"left_value": "33-NZ16181S-T0102-03", "right_value": "33-NZ16181S-T0102-04"}, "同名图")
    assert "第 4 段 03 ↔ 04" in reason and "可能沿用" not in reason
