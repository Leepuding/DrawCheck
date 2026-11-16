from PIL import Image
import pytest

from backend.api.review_status import list_issues
from backend.rules import RulesAService, RulesBService
from backend.store import BBox, Evidence, Repository, Source
from tests.backend.rules.test_reference_reliability import add, seed, unit


@pytest.mark.parametrize('summary_stub', [None, {},
    {'incomplete_count':0,'unreadable_count':0,'out_of_scope_count':0}])
def test_standalone_result_counts_keep_all_current_failure_categories(tmp_path,summary_stub):
    with Repository(tmp_path/'store') as repo:
        vid,oid=seed(repo,tmp_path)
        drawing=unit(repo,vid,oid,'P-001',1)
        task=repo.create_task(vid)
        repo.update_task(task['id'],status='已完成',checkpoints={},estimated_cost_micros=0,
            incomplete=[
                {'unit_id':None,'original_id':oid,'step':'extract','category':'unreadable','reason':'字体未恢复'},
                {'unit_id':drawing['id'],'step':'extract','category':'out_of_scope','reason':'不在审查范围'},
                {'unit_id':drawing['id'],'step':'semantic','category':'incomplete','reason':'语义候选未完整核实'}])
        view=list_issues(repo,vid,summary_stub=summary_stub)
        assert view['unreadable_count']==view['out_of_scope_count']==view['incomplete_count']==1
        assert len(view['incomplete_items'])==3


def test_cross_sheet_results_report_both_values_reason_and_comparison_scope(tmp_path):
    with Repository(tmp_path / "store") as repo:
        vid, oid = seed(repo, tmp_path)
        a = unit(repo, vid, oid, "P-001", 1, review_relative_path="left.png")
        b = unit(repo, vid, oid, "P-002", 2, review_relative_path="right.png")
        for name in ("left.png", "right.png"):
            Image.new("RGB", (10, 10)).save(repo.root / name)
        for sheet, length, spec in ((a, "100m", "YJV-3×70"), (b, "200m", "YJV-3×95")):
            add(repo, vid, sheet, length, category="尺寸", object_id="A-B", identity_confirmed=True)
            add(repo, vid, sheet, spec, y=.2, category="材料规格", object_id="线路1", identity_confirmed=True)
            add(repo, vid, sheet, "1.200m", y=.3, category="标高", object_id="F1", identity_confirmed=True)
            add(repo, vid, sheet, "AB01", y=.4, category="编号写法")
        RulesAService(repo).run(vid)
        RulesBService(repo).run(vid)
        bundle = list_issues(repo, vid)
        dimensions = next(row["finding"] for row in bundle["items"] if row["category"] == "尺寸")
        assert dimensions["label"] == "A-B"
        assert "100000" in dimensions["reason"]
        assert "同一对象或标注项目「A-B」" in dimensions["basis"]
        assert [dimensions[side]["value"] for side in ("left", "right")] == ["100m", "200m"]
        assert all(dimensions[side]["bbox"] for side in ("left", "right"))
        material = next(row["finding"] for row in bundle["items"] if row["category"] == "材料规格")
        assert material["label"] == "线路1"
        assert material["left"]["value"] == "YJV-3×70" and material["right"]["value"] == "YJV-3×95"
        assert bundle["comparison_details"] == []
        assert "engineering_comparisons" not in bundle and "local_readings" not in bundle
        coverage = {row["category"]: row for row in bundle["comparison_summary"]}
        assert coverage["尺寸"]["difference"] == 1 and coverage["标高"]["verified"]
        assert not coverage["编号写法"]["verified"] and not coverage["接线"]["verified"]
        assert all("match" not in row for row in coverage.values())
        assert bundle["verification"]["has_effective_comparison"]



def test_missing_reference_does_not_invent_a_second_location(tmp_path):
    with Repository(tmp_path / "store") as repo:
        vid, oid = seed(repo, tmp_path)
        a = unit(repo, vid, oid, "P-001", 1)
        box = BBox(.1,.1,.2,.2)
        repo.create_issue(vid, "引用详图未到", Evidence(a["id"], Source.CAD_TEXT, "详见P-099", box),
                          Evidence(a["id"], Source.VISION, "（不存在）", box),
                          rule_result={"category": "详图索引断链", "basis": "完整目录中已列，实到图纸缺少目标"})
        bundle = list_issues(repo, vid)
        assert bundle["items"] == []
        assert not bundle["verification"]["has_effective_comparison"]
        finding = bundle["comparison_details"][0]
        assert finding["outcome"] == "unverified" and "缺少第二张原图" in finding["reason"]
        assert finding["left"]["bbox"]
        assert finding["right"]["bbox"] is None and finding["right"]["image_url"] is None


def test_zero_difference_distinguishes_unverified_from_verified_scope(tmp_path):
    with Repository(tmp_path / "store") as repo:
        vid, oid = seed(repo, tmp_path)
        a = unit(repo, vid, oid, "P-001", 1)
        b = unit(repo, vid, oid, "P-002", 2)
        first = list_issues(repo, vid)
        assert first["items"] == []
        assert first["verification"]["zero_difference_message"] == "尚未形成有效对照"
        for sheet in (a, b):
            add(repo, vid, sheet, "100m", category="尺寸", object_id="A-B", identity_confirmed=True)
        second = list_issues(repo, vid)
        assert second["items"] == [] and second["comparison_details"] == []
        assert second["verification"]["zero_difference_message"] == "已核验范围内未发现差异"
        assert second["verification"]["scope_note"]


def test_unverified_pairs_never_become_differences_or_verified_scope(tmp_path):
    with Repository(tmp_path / "store") as repo:
        vid, oid = seed(repo, tmp_path)
        for i in (1, 2):
            sheet = unit(repo, vid, oid, f"P-00{i}", i)
            add(repo, vid, sheet, "100", category="尺寸", object_id="A-B", identity_confirmed=True)
        bundle = list_issues(repo, vid)
        assert bundle["items"] == []
        assert not bundle["verification"]["has_effective_comparison"]
        assert len(bundle["comparison_details"]) == 1
        assert bundle["comparison_details"][0]["outcome"] == "unverified"


@pytest.mark.parametrize("left,right,measurement,expected", [
    ("数量12台", "数量12套", None, "unverified"),
    ("数量12台", "数量12台", None, "match"),
    ("数量12台", "数量10台", None, "mismatch"),
    ("数量12", "数量12", None, "match"),
    ("数量12台/套", "数量12台", "台", "unverified"),
    ("数量12台，备用2台", "数量12台", "台", "unverified"),
    ("数量12台", "数量12台", "台", "match"),
    ("数量10", "数量12台", "台", "mismatch"),
])
def test_quantity_rules_feed_result_scope_using_visible_units(tmp_path, left, right, measurement, expected):
    with Repository(tmp_path / "store") as repo:
        vid, oid = seed(repo, tmp_path)
        for i, text in enumerate((left, right), 1):
            name = f"quantity-{i}.png"
            drawing = unit(repo, vid, oid, f"S-00{i}", i, review_relative_path=name)
            Image.new("RGB", (10, 10)).save(repo.root / name)
            add(repo, vid, drawing, text, numeric="12", measurement=measurement,
                category="数量与材料表", object_id="设备1",
                comparison_key="数量", identity_confirmed=True)
        rules = RulesBService(repo).run(vid)
        bundle = list_issues(repo, vid)
        coverage = next(row for row in bundle["comparison_summary"] if row["category"] == "数量与材料表")
        assert not rules.pendings and bundle["pending_count"] == 0
        assert coverage["verified"] == (expected != "unverified")
        assert coverage["difference"] == (expected == "mismatch")
        assert coverage["unverified"] == (expected == "unverified")
        assert bundle["verification"]["has_effective_comparison"] == (expected != "unverified")
        assert bool(bundle["items"]) == (expected == "mismatch")
        assert bool(bundle["comparison_details"]) == (expected == "unverified")
        if expected == "unverified":
            assert bundle["verification"]["zero_difference_message"] == "尚未形成有效对照"
            assert "计量单位不同" in bundle["comparison_details"][0]["basis"]
