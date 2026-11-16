import json

from backend.rules import RulesBService
from backend.store import BBox, Evidence, Source


def seed(repo, tmp_path):
    source = tmp_path / "b.pdf"
    source.write_bytes(b"%PDF-test")
    p = repo.create_project("B")
    v = repo.create_version(p["id"], "v1")
    o = repo.import_original(v["id"], source)["record"]
    return v["id"], o["id"]


def add(repo, vid, unit, text, **annotation):
    fact = repo.add_fact(vid, Evidence(unit["id"], Source.VISION, text, BBox(.1, .1, .2, .2)), numeric_value=annotation.pop("numeric_value", None))
    fresh = repo.get("drawing_units", unit["id"])
    meta = fresh["metadata"]
    meta.setdefault("fact_annotations", {})[fact["id"]] = annotation
    repo._update("drawing_units", unit["id"], {"metadata_json": json.dumps(meta)})
    return fact


def test_all_group_b_answer_cases(repo, tmp_path, answers):
    vid, oid = seed(repo, tmp_path)
    units = {}
    for n in ["S-003", "S-004", "S-005", "S-006"]:
        units[n] = repo.create_unit(vid, oid, kind="pdf_page", ordinal=len(units)+1, name=n, drawing_number=n, metadata={"extract_status":"ok"})
    # Absence requires a complete list and all actual identities corroborated.
    repo.create_unit(vid, oid, kind="pdf_page", ordinal=5, name="目录", drawing_number="S-000",
                     metadata={"extract_status":"ok", "is_catalog_sheet":True, "catalog_complete":True,
                               "catalog_listed_numbers":[*units, "S-099"]})
    expected = set()
    for case in answers["issues"]:
        if case.get("rule_group") != "B" or case.get("revision") or case["category"] == "同名不同对象":
            continue
        if case["expectation"] == "应报":
            expected.add(case["id"])
        for side in ["side_a", "side_b"]:
            value = case.get(side)
            if value:
                add(repo, vid, units[value["drawing_number"]], value["text"], category=case["category"], object_id=case["id"], comparison_key=case["id"], identity_confirmed=True, numeric_value=value.get("numeric_value"))
    result = RulesBService(repo).run(vid)
    assert {i["rule_result"]["fixture_id"] for i in result.issues} == expected
    assert len(result.issues) == 4
    assert not result.pendings
    assert all(i["review_status"] == "疑似" for i in result.issues)
    assert len(RulesBService(repo).run(vid).issues) == 4
    assert len(repo.list_records("issues", version_id=vid)) == 4


def test_identity_required_across_specialties_and_unclear_pending(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    a = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="土建")
    b = repo.create_unit(vid, oid, kind="pdf_page", ordinal=2, name="电气")
    add(repo, vid, a, "YJV-3×95", category="材料规格", object_id="线路1", identity_confirmed=False)
    add(repo, vid, b, "YJV-3×70", category="材料规格", object_id="线路1", identity_confirmed=False)
    result = RulesBService(repo).run(vid)
    assert result.issues == []
    assert len(result.pendings) == 2
    add(repo, vid, a, "数量12", category="数量与材料表", object_id="设备2", identity_confirmed=True)
    add(repo, vid, b, "数量10", category="数量与材料表", object_id="设备2", identity_confirmed=True, unclear=True)
    result = RulesBService(repo).run(vid)
    assert result.issues == []
    assert any("看不清" in p["reason"] for p in result.pendings)


def test_detail_inventory_missing_or_not_verified(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    a = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="索引")
    target = repo.create_unit(vid, oid, kind="pdf_page", ordinal=2, name="详图", drawing_number="S-003")
    add(repo, vid, a, "见详图S-003", category="详图索引断链", target_detail_number="2")
    result = RulesBService(repo).run(vid)
    assert result.issues == []
    assert len(result.pendings) == 1
    meta = {**target["metadata"], "detail_inventory_complete": True}
    repo._update("drawing_units", target["id"], {"metadata_json": json.dumps(meta)})
    assert len(RulesBService(repo).run(vid).issues) == 1
