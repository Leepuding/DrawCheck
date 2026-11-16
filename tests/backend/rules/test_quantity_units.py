"""Quantity equality requires a readable common unit or a complete bare count."""
import pytest

from backend.evidence_selection import selected_facts
from backend.pending import list_pending
from backend.rules import RulesBService
from backend.rules.common import evidence_from_fact
from backend.rules.rules_b import UNPAIRED_REASON
from tests.backend.rules.test_reference_reliability import add, seed, unit


@pytest.mark.parametrize("left,right,left_unit,right_unit,role,outcome", [
    ("数量12", "数量12", None, None, None, "match"),
    ("图上计数12", "材料表数量12", None, None, None, "match"),
    ("数量12", "数量10", None, None, None, "mismatch"),
    ("数量12 台", "数量12台", None, None, None, "match"),
    ("数量12台", "数量10台", None, None, None, "mismatch"),
    ("数量12米", "数量12m", None, None, None, "match"),
    ("数量12台", "数量12台", "台", None, None, "match"),
    ("12", "数量12台", "台", None, None, "match"),
    ("数量12台", "数量12台", "台", "台", None, "match"),
    ("数量10", "数量12台", "台", "台", None, "mismatch"),
    ("长度440m", "长度441m", "m", "m", None, "mismatch"),
    ("长度440mm", "长度440mm", "mm", "mm", None, "match"),
    ("数量12箱", "数量12箱", "箱", "箱", None, "match"),
    ("数量12台", "数量12套", None, None, None, "unverified"),
    ("数量12", "数量12台", None, None, None, "unverified"),
    ("数量12台/套", "数量12台", None, None, None, "unverified"),
    ("数量12台，备用2台", "数量12台", None, None, None, "unverified"),
    ("数量12台/套", "数量12台", "台", "台", None, "unverified"),
    ("数量12台，备用2台", "数量12台", "台", "台", None, "unverified"),
    ("数量12台/套", "数量12台/套", "台", "台", None, "unverified"),
    ("数量12台，备用2台", "数量12台，备用2台", "台", "台", None, "unverified"),
    ("数量12箱", "数量12箱", None, None, None, "unverified"),
    ("数量1 2台", "数量12台", None, None, None, "unverified"),
    ("数量12台", "数量12台", "套", "台", None, "unverified"),
    ("12", "12", None, None, "material_table", "unverified"),
])
def test_quantity_unit_evidence_controls_rule_and_read_only_projection(
    repo, tmp_path, left, right, left_unit, right_unit, role, outcome,
):
    vid, oid = seed(repo, tmp_path)
    units = [unit(repo, vid, oid, f"S-00{i}", i) for i in (1, 2)]
    for drawing, text, measurement in zip(units, (left, right), (left_unit, right_unit)):
        add(repo, vid, drawing, text, numeric="12", measurement=measurement,
            category="数量与材料表", object_id="设备1", comparison_key="数量",
            identity_confirmed=True, quantity_role=role)
    before = repo.list_records("facts", version_id=vid)
    result = RulesBService(repo).run(vid)
    assert len(result.issues) == (outcome == "mismatch")
    assert bool(result.incomplete) == (outcome == "unverified")
    assert not result.pendings
    facts = selected_facts(repo, vid)[1]
    projected = RulesBService(repo).run(vid, comparison_facts=facts)
    assert [row["outcome"] for row in projected.comparisons] == [outcome]
    assert repo.list_records("facts", version_id=vid) == before


@pytest.mark.parametrize("identity", [
    {"object_id": "构件编号1", "comparison_key": "构件编号1-名称"},
    {"object_id": "构件编号 １", "comparison_key": "数量"},
    {"comparison_key": "构件编号1-数量"},
    {"object_id": "檩条1", "comparison_key": "构件编号1-数量"},
])
@pytest.mark.parametrize("left,right", [
    ("1 前立柱 Q355B TUB100X50X3.0 6 件 热浸镀锌", "1 预埋件 Q235B 6470 件 热浸镀锌"),
    ("5件", "4件"),
    ("6件", "6件"),
])
def test_table_local_serial_cannot_pair_or_create_a_question(repo, tmp_path, identity, left, right):
    vid, oid = seed(repo, tmp_path)
    for ordinal, text, role in ((1, left, "material_table"), (2, right, None)):
        drawing = unit(repo, vid, oid, f"S-00{ordinal}", ordinal)
        add(repo, vid, drawing, text, category="数量与材料表", identity_confirmed=True,
            quantity_role=role, **identity)
    service = RulesBService(repo)
    for _ in range(2):
        result = service.run(vid)
        assert not (result.issues or result.incomplete or result.pendings)
    projected = service.run(vid, comparison_facts=selected_facts(repo, vid)[1])
    assert not projected.comparisons
    assert not repo.list_records("pending_confirmations", version_id=vid)


def test_table_serial_no_longer_keeps_an_old_object_question_active(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    a, b = [unit(repo, vid, oid, f"S-00{i}", i) for i in (1, 2)]
    left = add(repo, vid, a, "5件", category="数量与材料表", object_id="构件编号1",
               identity_confirmed=False, quantity_role="material_table")
    add(repo, vid, b, "4件", category="数量与材料表", object_id="构件编号1",
        identity_confirmed=False, quantity_role="material_table")
    old = repo.create_pending(vid, evidence_from_fact(left), UNPAIRED_REASON)
    assert list_pending(repo, vid)["pending_count"] == 0
    assert repo.get("pending_confirmations", old["id"]) == old
