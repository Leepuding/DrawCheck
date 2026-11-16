"""Offline regressions for cached/model evidence and failed-page audit boundaries."""
import hashlib
import io
import json

import pytest
from PIL import Image

from backend.evidence_selection import selected_facts
from backend.extract import ExtractResult, ExtractService, PROMPT_VERSION
from backend.extract.schema import validate_extract_payload
from backend.extract.prompt import build_user_prompt, SYSTEM_PROMPT
from backend.extract.tiles import STRATEGY, plan_regions
from backend.linking import LinkingService
from backend.models.openai_compat import CallResult
from backend.rules.dimension import run_dimension
from backend.store import BBox, Evidence, Repository, Source


class NoCall:
    def chat_completions(self, **kwargs):
        raise AssertionError("This regression must never call a model")


def fact(text="100", **extra):
    return {"text":text,"numeric_value":text,"unit":"mm","category":"尺寸",
            "bbox":{"x0":.1,"y0":.1,"x1":.2,"y1":.2},"confidence":.9,
            "object_id":"B1","comparison_key":"长度","identity_confirmed":True, **extra}


@pytest.fixture
def boundary_case(tmp_path):
    with Repository(tmp_path/"data") as store:
        project=store.create_project("boundary")
        version=store.create_version(project["id"],"v1")
        original=tmp_path/"source.pdf"; original.write_bytes(b"%PDF-immutable-test")
        record=store.import_original(version["id"],original)["record"]
        Image.new("RGB",(200,100),"white").save(store.root/"review.png")
        unit=store.create_unit(version["id"],record["id"],kind="pdf_page",ordinal=1,name="source",metadata={"review_relative_path":"review.png"})
        yield store,version,unit,ExtractService(store,use_mock=False,model_client=NoCall())


def cached(case, payload, **extra):
    store,version,unit,service=case
    digest=hashlib.sha256((store.root/"review.png").read_bytes()).hexdigest()
    service.cache.set(digest,PROMPT_VERSION,service.cache_role,{"payload":payload,**extra})
    return service.extract_unit(unit["id"])


def persist(case, facts):
    store,version,unit,service=case
    result=ExtractResult(unit["id"],version["id"],"ok",PROMPT_VERSION,facts=facts)
    service._persist(result,unit)
    return result


@pytest.mark.parametrize("bad_unit", [["mm"],{},1,True])
def test_invalid_unit_is_unknown_pending_in_cache_and_direct_persistence(boundary_case,bad_unit):
    store,version,unit,service=boundary_case
    raw=fact("原文100",numeric_value="100",unit=bad_unit,region_ids=["region-1"],region_conflict=True)
    result=cached(boundary_case,{"facts":[raw]})
    assert result.status == "ok" and result.cache_hit and result.model_calls == 0
    assert result.facts[0]["unit"] is None and result.facts[0]["unit_invalid"]
    assert result.facts[0]["region_ids"] == ["region-1"] and result.facts[0]["region_conflict"]
    assert len(result.pending_ids) == 1
    row=store.get("facts",result.fact_ids[0])
    assert row["text"] == "原文100" and row["unit"] is None
    persist(boundary_case,[raw])
    assert store.verify_originals(version["id"])


@pytest.mark.parametrize("field,value", [("confidence","NaN"),("confidence","Infinity"),("confidence",9),
                                        ("confidence",10**400),("confidence",True),
                                        ("bbox",{"x0":"NaN","y0":.1,"x1":.2,"y1":.2}),
                                        ("bbox",{"x0":0,"y0":.1,"x1":"Infinity","y1":.2})])
def test_invalid_aggregate_cache_is_page_incomplete_without_new_calls(boundary_case,field,value):
    store,version,unit,service=boundary_case
    result=cached(boundary_case,{"facts":[fact(**{field:value})]})
    assert result.status == "incomplete" and result.model_calls == 0
    assert store.list_records("facts",version_id=version["id"]) == []
    assert store.get("drawing_units",unit["id"])["metadata"]["extract_status"] == "incomplete"


def test_bad_page_does_not_interrupt_version_and_does_not_invent_bbox(boundary_case):
    store,version,unit,service=boundary_case
    cached(boundary_case,{"facts":[fact(confidence="NaN")]})
    Image.new("RGB",(200,100),"blue").save(store.root/"second.png")
    other=store.create_unit(version["id"],unit["original_id"],kind="pdf_page",ordinal=2,name="other",metadata={"review_relative_path":"second.png"})
    digest=hashlib.sha256((store.root/"second.png").read_bytes()).hexdigest()
    service.cache.set(digest,PROMPT_VERSION,service.cache_role,{"payload":{"facts":[fact(bbox=None)]}})
    results=service.extract_version(version["id"])
    assert [r.status for r in results] == ["incomplete","ok"]
    assert results[1].facts[0]["bbox"] == {"x0":0,"y0":0,"x1":1,"y1":1}
    assert results[1].facts[0]["unclear"] and results[1].pending_ids
    assert all(r.model_calls == 0 for r in results)


@pytest.mark.parametrize("extra,status", [({"unit":["mm"]},"ok"),({"confidence":"NaN"},"incomplete"),
    ({"bbox":{"x0":"NaN","y0":.1,"x1":.2,"y1":.2}},"incomplete")])
def test_new_response_has_same_boundary_as_cache(boundary_case,extra,status):
    store,version,unit,_=boundary_case
    class SyntheticClient:
        calls=0
        def chat_completions(self,**kwargs):
            self.calls+=1
            return CallResult(content=json.dumps({"facts":[fact(**extra)]}),input_tokens=1,output_tokens=1,
                raw_status=200,model="offline-test",provider="offline-test",latency_ms=0)
    client=SyntheticClient()
    service=ExtractService(store,use_mock=False,model_client=client,format_retries=0)
    result=service.extract_unit(unit["id"])
    assert result.status == status and result.model_calls == 1 and client.calls == 1
    if status == "ok":
        assert result.pending_ids and result.facts[0]["unit"] is None
    else:
        assert not store.list_records("facts",version_id=version["id"])


@pytest.mark.parametrize("blob", ["{broken", "[]", '{"payload": []}'])
def test_unreadable_or_wrong_shape_cache_is_incomplete(boundary_case,blob):
    store,version,unit,service=boundary_case
    digest=hashlib.sha256((store.root/"review.png").read_bytes()).hexdigest()
    service.cache._path(digest,PROMPT_VERSION,service.cache_role).write_text(blob)
    assert service.extract_unit(unit["id"]).status == "incomplete"


def test_format_retry_explains_missing_text_without_replaying_reasoning(boundary_case):
    store,version,unit,_=boundary_case
    visible=[fact(f"测试原图第{i}处可见原文",numeric_value="6",unit="件") for i in range(46)]
    bad={"facts":[dict(row) for row in visible]}
    for index in (2,7,13,19,25,31,37,43):
        del bad["facts"][index]["text"]
    class RetryClient:
        requests=[]
        def chat_completions(self,**kwargs):
            self.requests.append(kwargs)
            return CallResult(content=json.dumps(bad if len(self.requests)==1 else {"facts":visible}),
                input_tokens=1,output_tokens=1,raw_status=200,model="offline-test",provider="offline-test",latency_ms=0,
                extras={"reasoning_content":"DO_NOT_REPLAY_PROVIDER_REASONING"})
    client=RetryClient()
    service=ExtractService(store,use_mock=False,model_client=client,format_retries=1)
    result=service.extract_unit(unit["id"])
    assert result.status == "ok" and result.model_calls == 2 and len(result.facts) == 46
    first,second=client.requests
    assert first["messages"][0]["content"] == SYSTEM_PROMPT+"\n"
    assert first["messages"][1]["content"][1]["text"] == build_user_prompt(PROMPT_VERSION)+"\n"
    assert second["messages"][0] == first["messages"][0]
    assert second["messages"][1]["content"][0] == first["messages"][1]["content"][0]
    feedback=second["messages"][1]["content"][1]["text"]
    assert "facts[2] 缺 text" in feedback and "请重新看图" in feedback and "不能凭 numeric_value" in feedback
    assert "unclear=true" in feedback and "保持原有审查覆盖范围" in feedback
    assert "DO_NOT_REPLAY_PROVIDER_REASONING" not in json.dumps(second)
    assert [row["text"] for row in result.facts] == [row["text"] for row in visible]


def test_catalog_auxiliary_uncertainty_survives_revalidation(boundary_case):
    result=cached(boundary_case,{"facts":[],"is_catalog_sheet":True,
        "catalog_listed_numbers":["S-001"],"catalog_volume_numbers":[1],"catalog_complete":True})
    assert result.status == "ok"
    assert result.catalog["catalog_listed_numbers"] == ["S-001"]
    assert result.catalog["catalog_volume_numbers_uncertain"]
    assert validate_extract_payload({"facts":[],**result.catalog})["catalog_volume_numbers_uncertain"]


def test_valid_cache_can_restore_page_status_after_temporary_failure(boundary_case):
    store,version,unit,service=boundary_case
    first=cached(boundary_case,{"facts":[fact()]})
    service._mark_unit_incomplete(unit,"temporary image unavailable")
    assert selected_facts(store,version["id"])[1] == []
    again=service.extract_unit(unit["id"])
    assert again.status == "ok" and again.cache_hit and again.model_calls == 0
    assert store.get("drawing_units",unit["id"])["metadata"]["extract_status"] == "ok"
    assert len(selected_facts(store,version["id"])[1]) == 1


@pytest.mark.parametrize("invalid", [False,True])
def test_region_cache_revalidation_keeps_flags_and_contains_bad_format(boundary_case,invalid):
    store,version,unit,service=boundary_case
    image=Image.new("RGB",(3000,1200),"white"); image.save(store.root/"review.png")
    image_sha=hashlib.sha256((store.root/"review.png").read_bytes()).hexdigest()
    boxes=[[0,0,*image.size]]+[list(box) for box in plan_regions(*image.size)]
    for i,box in enumerate(boxes):
        region_id="overview" if i == 0 else f"region-{i}"
        buffer=io.BytesIO(); image.crop(tuple(box)).save(buffer,format="PNG")
        digest=hashlib.sha256(buffer.getvalue()).hexdigest()
        payload={"title_blocks":[{"drawing_number":"S-001","confidence":.9,"bbox":fact()["bbox"]}] if i == 0 else [],
                 "facts":[] if i == 0 else [fact(unit=["mm"],confidence="NaN" if invalid else .9,region_conflict=True)]}
        role=f"{service.cache_role}:{STRATEGY}:{image_sha}:{region_id}:{box}"
        service.cache.set(digest,PROMPT_VERSION,role,{"payload":payload})
    result=service.extract_unit(unit["id"])
    assert result.model_calls == 0
    if invalid:
        assert result.status == "incomplete" and not store.list_records("facts",version_id=version["id"])
    else:
        assert result.status == "ok" and result.pending_ids
        assert all(f["unit_invalid"] and f["region_conflict"] and f["region_ids"] for f in result.facts)


def test_failed_page_retains_audit_but_drops_unconfirmed_machine_values(boundary_case):
    store,version,unit,service=boundary_case
    result=persist(boundary_case,[fact("100mm",numeric_value="100"),
        fact("200mm",numeric_value="200",bbox={"x0":.5,"y0":.1,"x1":.6,"y1":.2})])
    assert len(run_dimension(store,version["id"],persist=False)["issues"]) == 1
    service._mark_unit_incomplete(unit,"current failure")
    assert selected_facts(store,version["id"])[1] == []
    assert run_dimension(store,version["id"],persist=False)["issues"] == []
    assert len(store.list_records("facts",version_id=version["id"])) == 2
    first=store.get("facts",result.fact_ids[0])
    pending=store.create_pending(version["id"],Evidence(unit["id"],Source.VISION,first["text"],BBox(first["x0"],first["y0"],first["x1"],first["y1"])),"看不清")
    store.confirm_pending(pending["id"],status="已确认",confirmed_text="150")
    store.add_fact(version["id"],Evidence(unit["id"],Source.USER,"150",BBox(first["x0"],first["y0"],first["x1"],first["y1"])))
    facts=selected_facts(store,version["id"])[1]
    assert len(facts) == 1 and facts[0]["text"] == "150" and facts[0]["source"] == Source.USER.value
    store.confirm_pending(pending["id"],status="无法确定",confirmed_text=None)
    assert selected_facts(store,version["id"])[1] == []
    store.confirm_pending(pending["id"],status="已确认",confirmed_text="160")
    assert [f["text"] for f in selected_facts(store,version["id"])[1]] == ["160"]
    for source in (Source.USER,Source.PDF_TEXT,Source.CAD_TEXT):
        store.add_fact(version["id"],Evidence(unit["id"],source,"independent",BBox(.7,.7,.8,.8)))
    assert len(selected_facts(store,version["id"])[1]) == 4


def test_linking_uses_confirmed_override_after_failed_page(boundary_case):
    store,version,unit,service=boundary_case
    target=store.create_unit(version["id"],unit["original_id"],kind="pdf_page",ordinal=2,name="target",drawing_number="S-002")
    correct=store.create_unit(version["id"],unit["original_id"],kind="pdf_page",ordinal=3,name="correct",drawing_number="S-003")
    result=persist(boundary_case,[fact("见 S-002",numeric_value=None,unit=None)])
    service._mark_unit_incomplete(unit,"current failure")
    assert LinkingService(store).link_version(version["id"],persist=False).associations == []
    pending=store.create_pending(version["id"],Evidence(unit["id"],Source.VISION,"见 S-002",BBox(.1,.1,.2,.2)),"看不清")
    store.confirm_pending(pending["id"],status="已确认",confirmed_text="见 S-003")
    links=LinkingService(store).link_version(version["id"],persist=False).associations
    assert len(links) == 1 and correct["id"] in {links[0]["left_unit_id"],links[0]["right_unit_id"]}
    assert store.get("facts",result.fact_ids[0])["text"] == "见 S-002"
    service._mark_unit_incomplete(correct,"target failure")
    assert LinkingService(store).link_version(version["id"],persist=False).associations == []
    metadata=store.get("drawing_units",correct["id"])["metadata"]
    metadata["manual_correction"]=True
    store._update("drawing_units",correct["id"],{"metadata_json":json.dumps(metadata)})
    assert len(LinkingService(store).link_version(version["id"],persist=False).associations) == 1


def test_stale_generated_links_are_removed_but_manual_records_remain(boundary_case):
    store,version,unit,service=boundary_case
    target=store.create_unit(version["id"],unit["original_id"],kind="pdf_page",ordinal=2,name="target",drawing_number="S-002")
    persist(boundary_case,[fact("见 S-002",numeric_value=None,unit=None)])
    first=LinkingService(store).link_version(version["id"])
    assert len(first.associations) == 1
    again=LinkingService(store).link_version(version["id"])
    assert again.associations[0]["id"] == first.associations[0]["id"]
    manual=store.add_association(version["id"],unit["id"],target["id"],"detail_index",{"basis":"user verified"})
    service._mark_unit_incomplete(unit,"current failure")
    assert LinkingService(store).link_version(version["id"]).associations == []
    assert [row["id"] for row in store.list_records("associations",version_id=version["id"])] == [manual["id"]]
