import json

import pytest

from backend.api.review_status import list_issues, serialize_issue
from backend.evidence_selection import selected_facts
from backend.pending import list_pending
from backend.rules import RulesAService, run_dimension, run_elevation
from backend.rules.dimension import NO_UNIT_REASON
from backend.rules.unit_declaration import declaration_units, unit_conflict_reason, volume_units
from backend.store import BBox, Evidence, Source
from tests.backend.rules.test_reference_reliability import add, seed, unit

DECLARATION = "（八）其他 除图中特别注明外，尺寸均以毫米为单位，标高以米为单位。"
STEM = "33-N16091S-T0107"


@pytest.mark.parametrize("text,expected", [
    (DECLARATION, [("尺寸","mm"), ("标高","m")]),
    ("本卷册尺寸单位为厘米（cm）", [("尺寸","cm")]),
    ("除注明外，所注平面尺寸以毫米(mm)为单位，标高以米(m)为单位。", [("标高","m")]),
    ("某构件尺寸均以毫米为单位", []),
    ("本图尺寸均以毫米为单位", []),
    ("本图中，尺寸均以毫米为单位，标高以米为单位。", []),
    ("说明：本图中，尺寸以毫米为单位，标高以米为单位。", []),
    ("注：本详图，尺寸以毫米为单位，标高以米为单位。", []),
    ("仅屋面支架布置图，尺寸以毫米为单位，标高以米为单位。", []),
    ("尺寸以毫米为单位，仅适用于本图。", []),
    ("注：本卷册尺寸以毫米为单位，标高以米为单位。", [("尺寸","mm"), ("标高","m")]),
    ("（三）本详图尺寸以毫米为单位，标高以米为单位。", []),
    ("本页尺寸单位为厘米；本卷册标高以米为单位。", [("标高","m")]),
    ("标高单位为米（mm）", []),
    ("尺寸以mm²为单位", []),
    ("尺寸均以毫米为单位？", []),
    ("不按尺寸以毫米为单位", []),
])
def test_declaration_requires_complete_applicable_clauses(text, expected):
    assert declaration_units(text) == expected


def measurements(repo, tmp_path, category, a_text, b_text, *, different_volume=False, structured_unit=None):
    vid, oid = seed(repo,tmp_path)
    note = unit(repo,vid,oid,f"{STEM}-01",1)
    a = unit(repo,vid,oid,f"{STEM}-02",2)
    b = unit(repo,vid,oid,f"{'33-NZ16181S-T0107' if different_volume else STEM}-03",3)
    for sheet, text in ((a,a_text), (b,b_text)):
        add(repo,vid,sheet,text,category=category,object_id="基础F1",identity_confirmed=True,
            measurement=structured_unit)
    return vid, note, a, b


@pytest.mark.parametrize("category,first,second,expected", [
    ("尺寸", "1000", "1m", "match"), ("尺寸", "1000", "1.02m", "mismatch"),
    ("标高", "4.350", "4350mm", "match"), ("标高", "4.350", "4360mm", "mismatch"),
])
def test_default_is_used_only_for_bare_reading_with_source_location(repo,tmp_path,category,first,second,expected):
    vid, note, _, _ = measurements(repo,tmp_path,category,first,second)
    fact = add(repo,vid,note,DECLARATION,confidence=.98,source=Source.PDF_TEXT)
    fn = run_dimension if category=="尺寸" else run_elevation
    result = fn(repo,vid,comparison_facts=selected_facts(repo,vid)[1])
    pair, = result["comparisons"]
    assert pair["outcome"] == expected
    proof, = pair["unit_declarations"]
    assert (proof["text"], proof["unit_id"], proof["bbox"]["x0"]) == (DECLARATION, note["id"], fact["x0"])
    if expected == "mismatch":
        finding = serialize_issue(result["issues"][0], {note["id"]: note})["finding"]
        assert finding["reason"] != finding["basis"]
        assert finding["unit_declarations"][0]["text"] == DECLARATION
    else:
        assert not result["issues"]
        assert [row["text"] for row in list_issues(repo,vid)["unit_declarations"]
                if row["category"] == category] == [DECLARATION]


@pytest.mark.parametrize("scenario", ["other_project","no_declaration","no_position","unverified_title","area","only_model_box"])
def test_unverified_never_means_equal_or_a_machine_todo(repo,tmp_path,scenario):
    vid, note, a, b = measurements(repo,tmp_path,"尺寸","1000","1000", different_volume=scenario=="other_project")
    if scenario == "unverified_title":
        repo._update("drawing_units", note["id"], {"metadata_json":json.dumps({
            "extract_status":"ok","title_identity_uncertain":True})})
    if scenario != "no_declaration":
        add(repo,vid,note,"尺寸以平方米为单位" if scenario == "area" else DECLARATION,
            source=Source.VISION if scenario=="only_model_box" else Source.PDF_TEXT,
            region_bbox_unlocated=scenario=="no_position")
    result = run_dimension(repo,vid,comparison_facts=selected_facts(repo,vid)[1])
    pair, = result["comparisons"]
    assert pair["outcome"] == "unverified"
    assert not result["issues"] and not result["pendings"]
    assert list_pending(repo,vid)["pending_count"] == 0


def test_conflicting_defaults_one_question_and_explicit_units_still_compare(repo,tmp_path):
    vid, note, a, b = measurements(repo,tmp_path,"尺寸","1000","1000")
    add(repo,vid,note,DECLARATION,confidence=1,source=Source.PDF_TEXT)
    add(repo,vid,b,"本卷册尺寸均以米为单位。",y=.4,source=Source.PDF_TEXT)
    add(repo,vid,a,"1000mm",category="尺寸",object_id="基础F2",identity_confirmed=True,y=.5)
    add(repo,vid,b,"1m",category="尺寸",object_id="基础F2",identity_confirmed=True,y=.5)
    old = repo.create_pending(vid,Evidence(a["id"],Source.VISION,"1000",BBox(.1,.1,.3,.11)),NO_UNIT_REASON)
    for _ in range(2):
        result = RulesAService(repo).run(vid)
        assert not result.issues
        row, = list_pending(repo,vid)["items"]
        assert row["reason"] == unit_conflict_reason("尺寸")
        assert {item["unit"] for item in row["unit_declarations"]} == {"mm","m"}
        assert {item["unit_id"] for item in row["unit_declarations"]} == {note["id"],b["id"]}
    assert repo.get("pending_confirmations",old["id"]) == old
    comparisons = run_dimension(repo,vid,comparison_facts=selected_facts(repo,vid)[1])["comparisons"]
    assert {pair["label"]:pair["outcome"] for pair in comparisons} == {"基础F1":"unverified","基础F2":"match"}
    repo.confirm_pending(row["id"],status="已确认",confirmed_text="本卷册尺寸均以米为单位。")
    assert list_pending(repo,vid)["pending_count"] == 0
    assert repo.get("pending_confirmations",row["id"])["status"] == "已确认"
    assert not list(volume_units(repo,vid).conflicts())


def test_bare_elevation_does_not_inherit_m_from_model_field(repo,tmp_path):
    vid, note, a, b = measurements(repo,tmp_path,"标高","4.35","4.38",structured_unit="m")
    add(repo,vid,note,"尺寸均以毫米为单位。",source=Source.PDF_TEXT)
    result = run_elevation(repo,vid,comparison_facts=selected_facts(repo,vid)[1])
    assert not result["issues"]
    assert result["comparisons"][0]["outcome"] == "unverified"


def test_local_clause_does_not_replace_previous_volume_unit_after_revision(repo, tmp_path):
    old_version, old_note, _, _ = measurements(repo, tmp_path, "标高", "4.350", "4350mm")
    add(repo, old_version, old_note, DECLARATION, source=Source.PDF_TEXT)
    old_pair, = run_elevation(repo, old_version, comparison_facts=selected_facts(repo, old_version)[1])["comparisons"]
    assert old_pair["outcome"] == "match"

    original = tmp_path / "revision.pdf"
    original.write_bytes(b"%PDF-revision")
    project_id = repo.get("drawing_versions", old_version)["project_id"]
    revised = repo.create_version(project_id, "修订")["id"]
    oid = repo.import_original(revised, original)["record"]["id"]
    note, a, b = [unit(repo, revised, oid, f"{STEM}-0{i}", i) for i in (1, 2, 3)]
    add(repo, revised, note, "本图中，尺寸均以毫米为单位，标高以米为单位。", source=Source.PDF_TEXT)
    for sheet, text in ((a, "4.350"), (b, "4350mm")):
        add(repo, revised, sheet, text, category="标高", object_id="基础F1", identity_confirmed=True)
    pair, = run_elevation(repo, revised, comparison_facts=selected_facts(repo, revised)[1])["comparisons"]
    assert pair["outcome"] == "unverified" and not pair.get("unit_declarations")


def test_legacy_model_box_needs_independent_local_position_and_keeps_audit(repo,tmp_path,monkeypatch):
    from PIL import Image
    from backend.rules import unit_declaration

    vid, note, _, _ = measurements(repo,tmp_path,"尺寸","1000","1m")
    raw = add(repo,vid,note,DECLARATION,confidence=1,y=.55)
    image = repo.root / "source.png"
    Image.new("RGB",(600,600),"white").save(image)
    metadata = repo.get("drawing_units",note["id"])["metadata"]
    metadata["review_relative_path"] = image.name
    repo._update("drawing_units",note["id"],{"metadata_json":json.dumps(metadata)})
    box = {"x0":.14,"y0":.53,"x1":.28,"y1":.545}
    observed = {"text":"除图中特别注明外，尺寸均以毫米为单位，标高以米为单位。", "bbox":box,
                "confidence":.99,"source":Source.VISION.value}
    reads = []
    def locate(path, hints, _, **options):
        assert path == image and hints[0]["id"] == raw["id"]
        reads.append(path)
        return [observed]
    monkeypatch.setattr(unit_declaration,"_locate_in_original",locate)
    assert not list_issues(repo,vid)["unit_declarations"]
    RulesAService(repo).run(vid)
    RulesAService(repo).run(vid)
    proofs = list_issues(repo,vid)["unit_declarations"]
    assert len(proofs) == 2 and all(row["bbox"] == box and row["text"] == observed["text"] for row in proofs)
    assert len(reads) == 1 and repo.get("facts",raw["id"]) == raw
    image.write_bytes(b"changed preview")
    assert not list_issues(repo,vid)["unit_declarations"]
