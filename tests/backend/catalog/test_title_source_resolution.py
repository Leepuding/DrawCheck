"""CAD title authority requires positioned, labelled, unique original evidence."""
from copy import deepcopy

import pytest

from backend.catalog import CatalogService
from backend.catalog.title_resolver import resolve_title_number
from backend.extract import ExtractResult, PROMPT_VERSION
from backend.rules.catalog_titleblock import run_catalog_titleblock
from backend.store import BBox, Evidence, Repository, Source


@pytest.fixture
def title_case(tmp_path):
    with Repository(tmp_path/"data") as store:
        project=store.create_project("source provenance")
        version=store.create_version(project["id"],"v1")
        source=tmp_path/"EXPECTED-PREFIX-WRONG.dwg"; source.write_bytes(b"AC1027-original")
        original=store.import_original(version["id"],source)["record"]
        unit=store.create_unit(version["id"],original["id"],kind="dwg_frame",ordinal=1,name="filename must not decide title",metadata={"extract_status":"ok"})
        yield store,version,unit,CatalogService(store)


def add(case,text,box=(.70,.90,.75,.92),source=Source.CAD_TEXT):
    store,version,unit,_=case
    return store.add_fact(version["id"],Evidence(unit["id"],source,text,BBox(*box)))


def block(**extra):
    return {"drawing_number":"JOB-815-X-04","name":"visible title", "confidence":.95,
            "bbox":{"x0":.65,"y0":.80,"x1":.98,"y1":.96}, **extra}


def apply(case,title=None):
    store,version,unit,catalog=case
    result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,title_blocks=[title or block()])
    snapshot=deepcopy(result.title_blocks)
    rows=catalog.apply_extract_result(result)
    assert result.title_blocks == snapshot  # Original visual response stays authoritative as an audit record.
    return rows[0],store.get("drawing_units",unit["id"])


def pair(case,value="JOB-81S-X-04",box=(.78,.90,.91,.92)):
    label=add(case,"图号")
    value=add(case,value,box)
    return label,value


def test_unique_positioned_cad_original_wins_and_preserves_visual_and_fact(title_case):
    store,version,unit,catalog=title_case
    label,value=pair(title_case)
    before=store.list_records("facts",version_id=version["id"])
    row,current=apply(title_case)
    assert current["drawing_number"] == row["drawing_number"] == value["text"]
    assert not current["metadata"]["title_identity_uncertain"]
    provenance=current["metadata"]["title_number_resolution"]
    assert provenance["status"] == "cad_selected" and provenance["differs_from_vision"]
    assert provenance["vision_drawing_number"] == "JOB-815-X-04"
    assert provenance["selected_source"] == Source.CAD_TEXT.value
    assert provenance["selected_evidence"]["fact_id"] == value["id"]
    assert provenance["label_evidence"]["fact_id"] == label["id"]
    assert provenance["selected_evidence"]["bbox"] == {"x0":.78,"y0":.90,"x1":.91,"y1":.92}
    assert catalog.get_unit_directory(unit["id"])["vision_title_drawing_number"] == "JOB-815-X-04"
    assert catalog.list_catalog(version["id"])[0]["title_number_resolution"] == provenance
    assert store.list_records("facts",version_id=version["id"]) == before
    assert store.list_records("model_calls",version_id=version["id"]) == []


def test_revision_label_bounds_field_so_revision_zero_is_not_a_second_number(title_case):
    pair(title_case)
    add(title_case,"版号",(.93,.895,.97,.912))
    add(title_case,"0",(.94,.912,.95,.928))
    row,current=apply(title_case)
    assert current["drawing_number"] == "JOB-81S-X-04"
    assert len(row["title_number_resolution"]["candidate_evidence"]) == 1


def test_partial_intersection_is_recorded_without_expanding_title_bbox(title_case):
    pair(title_case,box=(.78,.909,.91,.929))
    title=block(bbox={"x0":.65,"y0":.80,"x1":.98,"y1":.915})
    row,current=apply(title_case,title)
    assert current["drawing_number"] == "JOB-81S-X-04"
    assert current["metadata"]["title_bbox"] == title["bbox"]
    assert row["title_number_resolution"]["selected_evidence"]["bbox"]["y1"] == .929


def test_multiple_candidates_remain_uncertain_and_pending_without_choosing(title_case):
    store,version,unit,catalog=title_case
    pair(title_case,box=(.78,.90,.84,.92))
    add(title_case,"OTHER-007",(.86,.90,.92,.92))
    row,current=apply(title_case)
    assert row["drawing_number"] == "JOB-815-X-04" and current["drawing_number"] == ""
    assert row["title_identity_uncertain"] and current["metadata"]["title_identity_uncertain"]
    assert row["title_number_resolution"]["status"] == "ambiguous"
    assert row["title_number_resolution"]["selected_evidence"] is None
    assert len(row["title_number_resolution"]["candidate_evidence"]) == 2
    assert len(store.list_records("pending_confirmations",version_id=version["id"])) == 1
    apply(title_case)
    assert len(store.list_records("pending_confirmations",version_id=version["id"])) == 1


def test_wrong_position_is_diagnostic_and_never_repairs_visual_characters(title_case):
    store,version,unit,catalog=title_case
    label=add(title_case,"图号",(.10,.10,.15,.12))
    value=add(title_case,"JOB-81S-X-04",(.18,.10,.31,.12))
    row,current=apply(title_case)
    resolution=row["title_number_resolution"]
    assert resolution["status"] == "position_unverified" and resolution["requires_confirmation"]
    assert resolution["rejected_evidence"][0]["evidence"]["fact_id"] == value["id"]
    assert resolution["selected_evidence"] is None
    assert row["drawing_number"] == "JOB-815-X-04" and current["drawing_number"] == ""
    assert current["metadata"]["title_identity_uncertain"]


def test_labelled_value_just_outside_title_bbox_is_not_pulled_inside(title_case):
    pair(title_case,box=(.78,.965,.91,.98))
    # Both texts are below the declared box; a small distance is not proof.
    store,version,unit,_=title_case
    store._connection.execute("UPDATE facts SET y0=.963,y1=.983 WHERE text='图号'")
    row,current=apply(title_case)
    assert row["title_number_resolution"]["status"] == "position_unverified"
    assert row["title_identity_uncertain"] and current["drawing_number"] == ""


@pytest.mark.parametrize("value",["NC09811-D-07","NZ16181-X-02","X/20.A_4","结施-01"])
def test_real_different_prefix_is_kept_verbatim_not_forced_to_expected_or_visual(title_case,value):
    pair(title_case,value)
    row,current=apply(title_case)
    assert current["drawing_number"] == row["drawing_number"] == value


def test_manual_number_has_priority_and_provenance_does_not_claim_cad_selection(title_case):
    store,version,unit,catalog=title_case
    pair(title_case)
    apply(title_case)
    catalog.rename(unit_id=unit["id"],drawing_number="MANUAL-X-9",name="human title")
    row,current=apply(title_case,block(drawing_number="ANOTHER-815",confidence=.1,unclear=True))
    assert row["drawing_number"] == current["drawing_number"] == "MANUAL-X-9"
    assert row["manual_correction"] and current["name"] == "human title"
    resolution=current["metadata"]["title_number_resolution"]
    assert resolution["status"] == "manual" and resolution["selected_source"] == Source.USER.value
    assert resolution["selected_evidence"] is None and resolution["vision_drawing_number"] == "ANOTHER-815"
    assert not current["metadata"]["title_identity_uncertain"]


@pytest.mark.parametrize("source",[Source.VISION,Source.PDF_TEXT,Source.USER])
def test_non_cad_text_cannot_silently_become_cad_authority(title_case,source):
    add(title_case,"图号",source=source)
    add(title_case,"JOB-81S-X-04",(.78,.90,.91,.92),source)
    row,current=apply(title_case)
    assert current["drawing_number"] == "JOB-815-X-04"
    assert row["title_number_resolution"]["status"] == "not_applicable"


def test_pdf_and_unlabelled_cad_keep_existing_visual_flow(title_case):
    store,version,unit,catalog=title_case
    add(title_case,"JOB-81S-X-04",(.78,.90,.91,.92))
    row,current=apply(title_case)
    assert current["drawing_number"] == "JOB-815-X-04"
    assert row["title_number_resolution"]["status"] == "no_label"
    pair(title_case)
    store._connection.execute("UPDATE drawing_units SET kind='pdf_page' WHERE id=?",(unit["id"],))
    row,current=apply(title_case)
    assert current["drawing_number"] == "JOB-815-X-04"
    assert row["title_number_resolution"]["status"] == "not_applicable"


@pytest.mark.parametrize("text",["图号：ACTUAL-81S","Drawing No.: ACTUAL-81S"])
def test_inline_label_preserves_full_raw_fact_and_exact_value(title_case,text):
    value=add(title_case,text,(.70,.90,.91,.92))
    row,current=apply(title_case)
    assert current["drawing_number"] == "ACTUAL-81S"
    assert row["title_number_resolution"]["selected_evidence"]["text"] == text
    assert row["title_number_resolution"]["selected_evidence"]["fact_id"] == value["id"]


def test_invalid_or_missing_title_position_never_fabricates_cad_authority(title_case):
    pair(title_case)
    for title in (block(bbox=None),block(bbox_clamped=True),block(no_title_block=True)):
        row,current=apply(title_case,title)
        assert row["title_identity_uncertain"]
        assert row["title_number_resolution"]["selected_evidence"] is None
        assert current["drawing_number"] == ""


def directory(case, listed):
    store,version,unit,_=case
    return store.create_unit(version["id"],unit["original_id"],kind="dwg_frame",ordinal=2,name="catalog",drawing_number="CAT",
        metadata={"extract_status":"ok","is_catalog_sheet":True,"catalog_complete":True,"catalog_listed_numbers":listed})


def test_rules_preserve_real_prefix_difference_with_actual_cad_evidence(title_case):
    store,version,unit,catalog=title_case
    label,value=pair(title_case,"NC09811-X-07")
    apply(title_case,block(drawing_number="NZ16181-X-07"))
    directory(title_case,["NZ16181-X-07"])
    result=run_catalog_titleblock(store,version["id"],expected_prefix="NZ16181")
    assert "目录张数" not in {issue["rule_result"]["category"] for issue in result["issues"]}
    for fixture in ("ISSUE-CAT-EXTRA","ISSUE-PREFIX-BAD"):
        issue=next(i for i in result["issues"] if i["rule_result"]["fixture_id"] == fixture)
        assert issue["left_source"] == Source.CAD_TEXT.value and issue["left_text"] == value["text"]
        assert issue["rule_result"]["left_value"] == "NC09811-X-07"
        assert [issue[f"left_{key}"] for key in ("x0","y0","x1","y1")] == [.78,.9,.91,.92]
        assert issue["rule_result"]["title_number_resolution"]["selected_evidence"]["fact_id"] == value["id"]
        assert issue["rule_result"]["vision_title_drawing_number"] == "NZ16181-X-07"
    assert store.get("drawing_units",unit["id"])["drawing_number"] == "NC09811-X-07"


def test_rule_evidence_keeps_inline_cad_raw_text_and_effective_value(title_case):
    store,version,unit,_=title_case
    add(title_case,"图号：NC09811-X-07",(.70,.90,.91,.92))
    apply(title_case)
    result=run_catalog_titleblock(store,version["id"],expected_prefix="NZ")
    issue=next(i for i in result["issues"] if i["rule_result"]["fixture_id"] == "ISSUE-PREFIX-BAD")
    assert issue["left_source"] == Source.CAD_TEXT.value and issue["left_text"] == "图号：NC09811-X-07"
    assert issue["rule_result"]["left_value"] == "NC09811-X-07"


def test_rename_immediately_changes_rule_source_to_user_without_another_model_or_replay(title_case):
    store,version,unit,catalog=title_case
    _,value=pair(title_case,"CAD-007")
    apply(title_case)
    catalog.rename(unit_id=unit["id"],drawing_number="HUMAN-019")
    result=run_catalog_titleblock(store,version["id"],expected_prefix="CAD")
    issue=next(i for i in result["issues"] if i["rule_result"]["fixture_id"] == "ISSUE-PREFIX-BAD")
    assert issue["left_source"] == Source.USER.value and issue["left_text"] == "HUMAN-019"
    resolution=issue["rule_result"]["title_number_resolution"]
    assert resolution["selected_source"] == Source.USER.value and resolution["selected_evidence"] is None
    assert resolution["prior_cad_evidence"]["fact_id"] == value["id"]


def test_stale_cad_reference_cannot_supply_catalog_absence_or_prefix_evidence(title_case):
    store,version,unit,_=title_case
    _,value=pair(title_case,"CAD-007")
    apply(title_case)
    directory(title_case,["EXPECTED-001"])
    store._connection.execute("DELETE FROM facts WHERE id=?",(value["id"],))
    result=run_catalog_titleblock(store,version["id"],expected_prefix="EXPECTED")
    assert result["incomplete"] and result["issues"] == []


def test_effective_directory_value_wins_over_temporary_raw_title_metadata(title_case):
    import json
    store,version,unit,_=title_case
    pair(title_case,"CAD-007")
    _,current=apply(title_case,block(drawing_number="RAW-015"))
    directory(title_case,["CAD-007"])
    metadata=current["metadata"]
    metadata["title_drawing_number"]="RAW-015"  # Extraction may store raw reading before catalog synchronization.
    store._update("drawing_units",unit["id"],{"metadata_json":json.dumps(metadata)})
    assert run_catalog_titleblock(store,version["id"],expected_prefix="CAD")["issues"] == []


def test_stale_cad_label_cannot_authorize_catalog_extra_or_absence(title_case):
    store,version,unit,catalog=title_case
    label,value=pair(title_case,"NC09811-X-07")
    apply(title_case,block(drawing_number="NZ16181-X-07"))
    directory(title_case,["NZ16181-X-07"])
    store._connection.execute('DELETE FROM facts WHERE id=?',(label['id'],))
    result=run_catalog_titleblock(store,version['id'],expected_prefix='NZ16181')
    # Prefix remains a visible positive discrepancy. Unknown identity must not
    # become a proven missing/extra/count mismatch through stale metadata.
    assert result['incomplete']
    fixtures={issue['rule_result']['fixture_id'] for issue in result['issues']}
    assert not fixtures & {'ISSUE-CAT-MISSING','ISSUE-CAT-EXTRA','ISSUE-CAT-COUNT'}


def catalog_volume_pair(case,*,duplicate=False):
    for text,box in [
        ('图纸文件目录',(.4,.06,.6,.08)),('卷册检索号',(.1,.18,.18,.20)),
        ('JOB-D0103',(.28,.18,.46,.20)),('版次',(.54,.18,.58,.20)),
        ('序号',(.1,.25,.14,.27)),('图号',(.24,.25,.29,.27)),
        ('文件名称',(.5,.25,.58,.27)),('1',(.1,.30,.14,.32)),
        ('JOB-D0103-01',(.2,.30,.35,.32)),('卷册设计说明',(.5,.30,.58,.32))]:
        add(case,text,box)
    if duplicate:
        add(case,'JOB-D0104',(.19,.18,.26,.20))


def test_catalog_volume_has_independent_native_header_identity_not_model_sheet_bbox(title_case):
    store,version,unit,_ = title_case
    catalog_volume_pair(title_case)
    before = store.list_records('facts',version_id=version['id'])
    row,current = apply(title_case,block(drawing_number='WRONG-VOLUME',unclear=True,confidence=.1))
    assert current['drawing_number'] == 'JOB-D0103'
    assert current['name'] == '图纸文件目录'
    assert current['metadata']['is_catalog_sheet'] is True
    assert current['metadata']['identity_kind'] == row['identity_kind'] == 'catalog_volume'
    assert current['metadata']['catalog_volume_number'] == 'JOB-D0103'
    assert current['metadata']['title_bbox'] == {'x0':.1,'y0':.18,'x1':.46,'y1':.20}
    resolution = current['metadata']['title_number_resolution']
    assert resolution['selected_evidence']['fact_id']
    assert resolution['label_evidence']['text'] == '卷册检索号'
    assert resolution['heading_evidence']['text'] == '图纸文件目录'
    assert not resolution['requires_confirmation']
    assert not current['metadata']['title_identity_uncertain']
    assert store.list_records('pending_confirmations',version_id=version['id']) == []
    assert store.list_records('facts',version_id=version['id']) == before
    assert store.list_records('model_calls',version_id=version['id']) == []


def test_catalog_duplicate_volume_field_stays_unconfirmed(title_case):
    catalog_volume_pair(title_case,duplicate=True)
    row,current = apply(title_case)
    assert row['title_number_resolution'].get('identity_kind') != 'catalog_volume'
    assert current['metadata']['title_identity_uncertain']
    assert current['drawing_number'] == ''


def test_native_catalog_header_remains_resolvable_when_model_returns_no_title(title_case):
    store,version,unit,catalog = title_case
    catalog_volume_pair(title_case)
    rows = catalog.apply_extract_result({'unit_id':unit['id'],'version_id':version['id'],'title_blocks':[]})
    current = store.get('drawing_units',unit['id'])
    assert rows[0]['drawing_number'] == current['drawing_number'] == 'JOB-D0103'
    assert rows[0]['identity_kind'] == 'catalog_volume'
    assert not current['metadata']['title_identity_uncertain']
    assert not rows[0]['no_title_block']
