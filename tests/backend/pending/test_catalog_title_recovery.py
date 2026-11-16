"""A catalogue question retires only after its real source field is revalidated."""
from copy import deepcopy

import pytest

from backend.catalog import CatalogService
from backend.extract import ExtractResult, PROMPT_VERSION
from backend.pending import list_pending
from backend.pending.title_source import TITLE_SOURCE_REASON, resolved_catalog_questions
from backend.store import BBox, Evidence, Repository, Source


@pytest.fixture
def catalog_case(tmp_path):
    with Repository(tmp_path/"data") as repo:
        project=repo.create_project("catalogue native field")
        version=repo.create_version(project["id"],"v1")
        path=tmp_path/"WRONG-FILENAME.dwg";path.write_bytes(b"AC1027")
        original=repo.import_original(version["id"],path)["record"]
        unit=repo.create_unit(version["id"],original["id"],kind="dwg_frame",ordinal=1,
            name="unverified catalogue",metadata={"extract_status":"ok"})
        fields={}
        for text,box in [
            ('图纸文件目录',(.4,.06,.6,.08)),('卷册检索号',(.1,.18,.18,.20)),
            ('JOB-D0103',(.28,.18,.46,.20)),('版次',(.54,.18,.58,.20)),
            ('序号',(.1,.25,.14,.27)),('图号',(.24,.25,.29,.27)),
            ('文件名称',(.5,.25,.58,.27)),('1',(.1,.30,.14,.32)),
            ('JOB-D0103-01',(.2,.30,.35,.32)),('卷册设计说明',(.5,.30,.58,.32))]:
            fields[text]=repo.add_fact(version["id"],Evidence(unit["id"],Source.CAD_TEXT,text,BBox(*box)))
        pending=repo.create_pending(version["id"],Evidence(unit["id"],Source.VISION,"JOB-D0103",BBox(.08,.1,.6,.15)),TITLE_SOURCE_REASON)
        result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,title_blocks=[
            {"drawing_number":"JOB-D0103","name":"图纸文件目录","confidence":.95,
             "bbox":{"x0":.08,"y0":.1,"x1":.6,"y1":.15}}])
        CatalogService(repo).apply_extract_result(result)
        yield repo,version["id"],unit["id"],pending,fields


def test_actual_native_field_retires_question_but_preserves_original_history(catalog_case):
    repo,vid,uid,pending,fields=catalog_case
    original=deepcopy(repo.list_records("pending_confirmations",version_id=vid))
    view=list_pending(repo,vid)
    assert view["pending_count"]==0
    assert len(view["resolved_title_sources"])==1
    proof=view["resolved_title_sources"][0]
    assert proof["selected_evidence"]["fact_id"]==fields["JOB-D0103"]["id"]
    assert proof["label_evidence"]["fact_id"]==fields["卷册检索号"]["id"]
    assert proof["heading_evidence"]["fact_id"]==fields["图纸文件目录"]["id"]
    assert proof["human_confirmation"] is False
    assert repo.list_records("pending_confirmations",version_id=vid)==original


@pytest.mark.parametrize("fault",["missing_label","missing_heading","moved_value","ambiguous","changed_text"])
def test_saved_success_does_not_hide_changed_or_ambiguous_source(catalog_case,fault):
    repo,vid,uid,pending,fields=catalog_case
    if fault.startswith("missing_"):
        key="卷册检索号" if fault=="missing_label" else "图纸文件目录"
        repo._connection.execute("DELETE FROM facts WHERE id=?",(fields[key]["id"],))
    elif fault=="moved_value":
        repo._connection.execute("UPDATE facts SET x0=.30 WHERE id=?",(fields["JOB-D0103"]["id"],))
    elif fault=="changed_text":
        repo._connection.execute("UPDATE facts SET text='JOB-D0104' WHERE id=?",(fields["JOB-D0103"]["id"],))
    else:
        repo.add_fact(vid,Evidence(uid,Source.CAD_TEXT,"JOB-D0104",BBox(.19,.18,.26,.20)))
    repo._connection.commit()
    assert list_pending(repo,vid)["pending_count"]==1
    assert resolved_catalog_questions(repo,vid)=={}


@pytest.mark.parametrize("status",["已确认","无法确定"])
def test_processed_history_and_another_original_reading_are_not_overridden(catalog_case,status):
    repo,vid,uid,pending,_=catalog_case
    prior=repo.create_pending(vid,Evidence(uid,Source.VISION,"original uncertain catalogue",BBox(.08,.1,.6,.15)),TITLE_SOURCE_REASON)
    repo.confirm_pending(prior["id"],status=status,confirmed_text="JOB-D0103" if status=="已确认" else None)
    view=list_pending(repo,vid)
    assert view["pending_count"]==1
    assert {row["id"] for row in view["items"]}=={pending["id"],prior["id"]}
    assert not view["resolved_title_sources"]


@pytest.mark.parametrize("change",[{"text":"JOB-D0104"},{"version_id":"other"},{"source":Source.USER.value}])
def test_other_literal_version_or_human_source_is_not_automatic_recovery(catalog_case,change):
    repo,vid,uid,pending,_=catalog_case
    assert not resolved_catalog_questions(repo,vid,rows=[{**pending,**change}])
