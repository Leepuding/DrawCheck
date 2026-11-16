import pytest

from backend.evidence_selection import selected_facts
from backend.rules.design_parameters import design_parameter_candidates
from tests.backend.rules.test_reference_reliability import add, seed, unit


@pytest.mark.parametrize("scenario, count", [
    ("same_scope",2), ("same_values",0), ("different_component",0), ("different_project",0),
    ("unlocated",0), ("wind_return_period",0), ("no_scope",0),
])
def test_only_supported_design_parameter_pairs_become_candidates(repo,tmp_path,scenario,count):
    vid, oid = seed(repo,tmp_path)
    a = unit(repo,vid,oid,"33-NZ16181S-T0102-01",1)
    b = unit(repo,vid,oid,f"33-{'NC09811S' if scenario=='different_project' else 'NZ16181S'}-T0103-01",2)
    add(repo,vid,a,"本工程光伏支架的结构设计使用年限为25年，结构的安全等级为三级。")
    other = {
        "same_values": "光伏支架设计使用年限为25年，结构安全等级为3级。",
        "different_component": "建筑物的结构设计使用年限为50年，结构安全等级为二级。",
        "wind_return_period": "光伏支架计算采用50年一遇风压，安全等级为二级。",
        "no_scope": "设计使用年限为50年，结构安全等级为二级。",
    }.get(scenario,"光伏支架设计使用年限为50年，结构安全等级为二级。")
    add(repo,vid,b,other,region_bbox_unlocated=scenario=="unlocated")
    rows = design_parameter_candidates(*selected_facts(repo,vid))
    assert len(rows) == count
    assert all(row["outcome"] == "unverified" and "待确认候选" in row["label"]
               and row["left_evidence"]["bbox"] and row["right_evidence"]["bbox"] for row in rows)
    assert not repo.list_records("issues",version_id=vid)
    assert not repo.list_records("pending_confirmations",version_id=vid)
