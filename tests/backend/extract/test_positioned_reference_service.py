"""Local position proof is consumed by ordinary extraction/cache/persistence."""
from copy import deepcopy
import json

import pytest
from PIL import Image

from backend.extract import ExtractService
from backend.extract.local_sheet import Word
from backend.extract.service import REFERENCE_LOCATION_REASON, POSITION_VERSION
from backend.extract.positioned_notes import VERSION
from backend.extract.tiles import STRATEGY, MERGE_VERSION, plan_regions
from backend.references import drawing_reference
from backend.store import Repository, Evidence, BBox, Source
from backend.store.originals import sha256_file

TEXT="基础详图见-04图。"
OLD=dict(x0=.7,y0=.1,x1=.9,y1=.2)
ACTUAL=dict(x0=.1,y0=.5,x1=.5,y1=.52)


@pytest.fixture
def reference_case(tmp_path,monkeypatch):
    with Repository(tmp_path/"data") as repo:
        project=repo.create_project("本地引用定位")
        vid=repo.create_version(project["id"],"A")["id"]
        pdf=tmp_path/"source.pdf";pdf.write_bytes(b"%PDF-synthetic")
        source=repo.import_original(vid,pdf)["record"]
        Image.new("RGB",(900,700),"white").save(repo.root/"review.png")
        unit=repo.create_unit(vid,source["id"],kind="pdf_page",ordinal=1,name="结构",
                             metadata={"review_relative_path":"review.png"})
        bundles=[]
        def observe(path,**kwargs):
            bundles.append(str(path))
            return {"version":VERSION,"image_sha256":sha256_file(path),"observations":[
                {"text":"5."+TEXT,"confidence":.99,"bbox":ACTUAL,"crop":[0,0,1,1],
                 "observation_id":"actual","image_sha256":sha256_file(path)}]}
        monkeypatch.setattr("backend.extract.service.cached_note_observations",observe)
        yield repo,vid,unit,bundles


def source_payload(*,unclear=False,text=TEXT):
    return {"title_blocks":[],"facts":[{"text":text,"category":"详图索引断链","bbox":deepcopy(OLD),
                                         "confidence":.99,"unclear":unclear}]}


def service(repo):
    def forbidden():
        pytest.fail("No provider client permitted")
    return ExtractService(repo,use_mock=False,provider="offline",model_name="offline",model_factory=forbidden)


def seed_cache(repo,svc,payload):
    sha=sha256_file(repo.root/"review.png")
    svc.cache.set(sha,svc.prompt_version,svc.cache_role,{"payload":payload,"persisted_unit_ids":[]})


@pytest.fixture
def short_reference_case(reference_case, monkeypatch):
    repo, vid, unit, _ = reference_case
    sha = sha256_file(repo.root / "review.png")
    actual = dict(x0=.36, y0=.4, x1=.39, y1=.42)
    label = Word("支架侧视图", (.42, .44, .49, .46), .99, Source.VISION.value)
    short = Word("见图27", tuple(actual.values()), .99, Source.VISION.value)
    context = [short, label]
    rereads = [("见图27", .99, 1.), ("见图27", .99, 1.15)]
    def row(text, bbox, oid, crop=(0, 0, 1, 1)):
        return {"text":text, "bbox":bbox, "confidence":.99, "crop":crop,
                "observation_id":oid, "image_sha256":sha}
    observations = [
        row(short.text, actual, "short"),
        row("节点说明见—27图", dict(x0=.1, y0=.6, x1=.2, y1=.62), "long"),
    ]
    monkeypatch.setattr("backend.extract.service.cached_note_observations",
                        lambda *args, **kwargs: {"version":VERSION, "image_sha256":sha, "observations":observations})
    monkeypatch.setattr("backend.extract.local_sheet._image_words", lambda *args: context)
    monkeypatch.setattr("backend.extract.local_sheet._read_detected_line", lambda *args: rereads)
    svc = service(repo)
    svc._position_engine = object()
    seed_cache(repo, svc, source_payload(text="见27"))
    return repo, vid, unit, svc, actual, row, observations, context, rereads


def test_short_reference_reread_supersedes_only_the_machine_reading(short_reference_case):
    repo, vid, unit, svc, actual, row, observations, _, _ = short_reference_case
    observations.append(row("见图27", actual, "duplicate-crop", (.1, .1, .9, .9)))
    old = repo.add_fact(vid, Evidence(unit["id"], Source.VISION, "见27", BBox(**OLD)))
    repo._update("drawing_units", unit["id"], {"metadata_json":json.dumps({
        **unit["metadata"], "fact_annotations":{old["id"]:{"category":"详图索引断链"}},
    })})
    repo.create_pending(vid, Evidence(unit["id"], Source.VISION, "见27", BBox(**OLD)), REFERENCE_LOCATION_REASON)
    result = svc.extract_unit(unit["id"])
    fact, = result.facts
    assert result.status == "ok" and result.cache_hit and result.model_calls == 0
    assert fact["text"] == "见图27" and fact["bbox"] == actual
    assert fact["model_original_bbox"] == OLD and fact["model_original_flags"]["unclear"] is False
    proof = fact["position_recovery"]
    assert proof["original_text"] == "见27" and proof["reading_status"] == "locally_reread"
    assert proof["locations"][0]["observation_ids"] == ["short", "duplicate-crop"]
    assert proof["different_reference_forms"][0]["text"] == "节点说明见—27图"
    assert proof["context_labels"][0]["text"] == "支架侧视图"
    assert drawing_reference(fact, home_number="T0201-26")["target"] is None
    stored, = repo.list_records("facts", version_id=vid)
    assert stored["text"] == "见图27"
    assert not repo.list_records("pending_confirmations", version_id=vid)
    assert svc.extract_unit(unit["id"]).facts == result.facts
    assert repo.list_records("facts", version_id=vid) == [stored]

    pending = repo.create_pending(vid, Evidence(unit["id"], Source.VISION, stored["text"], BBox(**actual)), "人工判断")
    repo.confirm_pending(pending["id"], status="已确认", confirmed_text="已核对该短注")
    history = repo.get("pending_confirmations", pending["id"])
    observations.clear()
    again, = svc.extract_unit(unit["id"]).facts
    assert again["text"] == stored["text"] and again["bbox"] == actual
    assert repo.list_records("facts", version_id=vid) == [stored]
    assert repo.list_records("pending_confirmations", version_id=vid) == [history]


@pytest.mark.parametrize(("case", "status", "reason"), [
    ("duplicate_line", "ambiguous", "competing_short_reference_lines"),
    ("same_plain_token_in_prose", "ambiguous", "competing_short_reference_lines"),
    ("only_number", "missing", "no_complete_standalone_reference"),
    ("partial_digits", "missing", "no_complete_standalone_reference"),
    ("extended_digits", "missing", "no_complete_standalone_reference"),
    ("only_other_form", "missing", "no_complete_standalone_reference"),
    ("no_context", "missing", "short_reference_without_nearby_labels"),
    ("context_disagrees", "missing", "short_reference_context_reread_disagrees"),
    ("context_finds_duplicate", "ambiguous", "competing_short_reference_lines"),
    ("native_disagrees", "missing", "short_reference_native_reread_disagrees"),
])
def test_short_reference_without_unique_corroboration_keeps_location_unverified(
        short_reference_case, case, status, reason):
    repo, vid, unit, svc, _, row, observations, context, rereads = short_reference_case
    other = dict(x0=.5, y0=.6, x1=.54, y1=.62)
    if case in {"duplicate_line", "same_plain_token_in_prose"}:
        text = "见图27" if case == "duplicate_line" else "节点布置见图27"
        observations.append(row(text, other, "competitor"))
    elif case in {"only_number", "partial_digits", "extended_digits"}:
        observations[0]["text"] = {"only_number":"27", "partial_digits":"见图7", "extended_digits":"见图270"}[case]
    elif case == "only_other_form":
        observations.pop(0)
    elif case == "no_context":
        context.pop()
    elif case == "context_disagrees":
        context.pop(0)
    elif case == "context_finds_duplicate":
        context.append(Word("见图27", (.46, .43, .49, .45), .99, Source.VISION.value))
    elif case == "native_disagrees":
        rereads[-1] = ("见图21", .99, 1.15)
    fact, = svc.extract_unit(unit["id"]).facts
    proof = fact["position_recovery"]
    assert proof["status"] == status and proof["reason"] == reason
    if case == "duplicate_line":
        assert {line["observation_id"] for line in proof["candidate_lines"]} == {"short", "competitor"}
    assert not proof["locations"]
    assert fact["text"] == "见27" and fact["bbox"] == OLD and fact["region_bbox_unlocated"]
    assert repo.list_records("pending_confirmations", version_id=vid)[0]["reason"] == REFERENCE_LOCATION_REASON


@pytest.mark.parametrize("unclear",[False,True])
def test_normal_cache_replay_positions_reference_and_preserves_original_reading(reference_case,unclear):
    repo,vid,unit,bundles=reference_case
    svc=service(repo);raw=source_payload(unclear=unclear);seed_cache(repo,svc,raw)
    result=svc.extract_unit(unit["id"])
    assert result.status=="ok" and result.cache_hit and result.model_calls==0
    fact,=result.facts
    assert fact["bbox"]==ACTUAL and fact["unclear"] is unclear and fact["text"]==TEXT
    assert fact["model_original_bbox"]==OLD and fact["model_original_flags"]["unclear"] is unclear
    annotation=repo.get("drawing_units",unit["id"])["metadata"]["fact_annotations"][result.fact_ids[0]]
    assert annotation["position_recovery"]["locations"][0]["observed_text"]=="5."+TEXT
    cache=svc.cache.get(result.image_sha256,svc.prompt_version,svc.cache_role)
    assert cache["raw_payload"]==raw and cache["position_version"]==POSITION_VERSION
    before=repo.list_records("facts",version_id=vid)
    again=svc.extract_unit(unit["id"])
    assert again.facts==result.facts and repo.list_records("facts",version_id=vid)==before
    assert not repo.list_records("model_calls",version_id=vid)


@pytest.mark.parametrize("mode",["missing","ambiguous","changed_image"])
def test_unverified_location_gets_dedicated_diagnostic_without_changing_unclear(reference_case,monkeypatch,mode):
    repo,vid,unit,_=reference_case
    def observe(path,**kwargs):
        sha=sha256_file(path)
        rows=[] if mode=="missing" else [dict(text="5."+TEXT,confidence=.99,bbox=ACTUAL,crop=[0,0,1,1],
                                            observation_id="first",image_sha256=sha)]
        if mode=="ambiguous":
            rows.append({**rows[0],"bbox":dict(x0=.1,y0=.7,x1=.5,y1=.72),"observation_id":"second"})
        return {"version":VERSION,"image_sha256":"b"*64 if mode=="changed_image" else sha,"observations":rows}
    monkeypatch.setattr("backend.extract.service.cached_note_observations",observe)
    svc=service(repo);seed_cache(repo,svc,source_payload())
    result=svc.extract_unit(unit["id"]);fact,=result.facts
    assert fact["unclear"] is False and fact["region_bbox_unlocated"]
    pending,=repo.list_records("pending_confirmations",version_id=vid)
    assert pending["reason"]==REFERENCE_LOCATION_REASON and pending["text"]==TEXT
    assert not repo.list_records("model_calls",version_id=vid)


@pytest.mark.parametrize("status",["已确认","无法确定"])
def test_processed_original_anchor_is_never_relocated_or_given_a_new_question(reference_case,status):
    repo,vid,unit,bundles=reference_case
    old=repo.add_fact(vid,Evidence(unit["id"],Source.VISION,TEXT,BBox(**OLD)))
    annotation={"category":"详图索引断链","confidence":.99,"unclear":True}
    meta=unit["metadata"];meta["fact_annotations"]={old["id"]:annotation}
    repo._update("drawing_units",unit["id"],{"metadata_json":json.dumps(meta)})
    p=repo.create_pending(vid,Evidence(unit["id"],Source.VISION,TEXT,BBox(**OLD)),"看不清")
    repo.confirm_pending(p["id"],status=status,confirmed_text="人工原决定" if status=="已确认" else None)
    before=repo.get("pending_confirmations",p["id"])
    svc=service(repo);seed_cache(repo,svc,source_payload(unclear=True))
    assert svc.extract_unit(unit["id"]).facts[0]["bbox"]==OLD
    assert not bundles
    assert repo.get("facts",old["id"])==old
    assert repo.get("drawing_units",unit["id"])["metadata"]["fact_annotations"][old["id"]]==annotation
    assert repo.list_records("pending_confirmations",version_id=vid)==[before]


def test_tiled_position_upgrade_uses_readonly_raw_region_caches_and_no_provider(reference_case):
    repo,vid,unit,_=reference_case
    Image.new("RGB",(3000,1200),"white").save(repo.root/"review.png")
    svc=service(repo);path=repo.root/"review.png";sha=sha256_file(path)
    old=source_payload();old["facts"][0]["bbox"]=dict(x0=.8,y0=.8,x1=.9,y1=.9)
    role=svc.cache_role+":"+STRATEGY
    svc.cache.set(sha,svc.prompt_version,role,{"payload":old,"merge_version":MERGE_VERSION,"position_version":"old"})
    raw_files={}
    with Image.open(path) as image:
        regions=[("overview",[0,0,*image.size])]+[(f"region-{i+1}",list(box)) for i,box in enumerate(plan_regions(*image.size))]
        for i,(name,box) in enumerate(regions):
            crop=repo.root/f"crop{i}.png";image.crop(box).save(crop)
            digest=sha256_file(path if i==0 else crop)
            key=f"{svc.cache_role}:{STRATEGY}:{sha}:{name}:{box}"
            payload={"title_blocks":[{"drawing_number":"S-001","name":"结构","bbox":OLD,"confidence":.99}],
                     "facts":source_payload()["facts"] if i==1 else []}
            svc.cache.set(digest,svc.prompt_version,key,{"payload":payload})
            file=svc.cache._path(digest,svc.prompt_version,key);raw_files[file]=file.read_bytes()
    result=svc.extract_unit(unit["id"])
    assert result.status=="ok" and result.cache_hit and result.model_calls==0
    assert result.facts[0]["bbox"]==ACTUAL
    assert all(p.read_bytes()==content for p,content in raw_files.items())
    assert svc.cache.get(sha,svc.prompt_version,role)["position_version"]==POSITION_VERSION


def test_recovered_bbox_keeps_clamped_source_flags_and_unclear_in_audit(reference_case):
    repo,vid,unit,_=reference_case
    svc=service(repo);raw=source_payload(unclear=True)
    raw["facts"][0].update(bbox_clamped=True,region_bbox_unlocated=True)
    seed_cache(repo,svc,raw)
    result=svc.extract_unit(unit["id"]);fact,=result.facts
    assert fact["bbox"]==ACTUAL and fact["unclear"] is True
    assert not fact.get("bbox_clamped") and not fact.get("region_bbox_unlocated")
    assert fact["model_original_flags"]["bbox_clamped"] and fact["model_original_flags"]["region_bbox_unlocated"]
    assert fact["position_recovery"]["original_unclear"] is True
    pending,=repo.list_records("pending_confirmations",version_id=vid)
    assert pending["reason"]=="看不清"


@pytest.mark.parametrize("status",["已确认","无法确定"])
def test_human_decision_at_previously_recovered_position_survives_cache_rebuild(reference_case,monkeypatch,status):
    repo,vid,unit,_=reference_case
    svc=service(repo);seed_cache(repo,svc,source_payload(unclear=True))
    first=svc.extract_unit(unit["id"])
    original=repo.get("facts",first.fact_ids[0])
    annotation=deepcopy(repo.get("drawing_units",unit["id"])["metadata"]["fact_annotations"][original["id"]])
    pending=repo.list_records("pending_confirmations",version_id=vid)[0]
    repo.confirm_pending(pending["id"],status=status,confirmed_text="人工保留" if status=="已确认" else None)
    before=repo.get("pending_confirmations",pending["id"])
    def forbidden(*args,**kwargs):
        pytest.fail("Already processed raw anchor must not request a new position")
    monkeypatch.setattr("backend.extract.service.cached_note_observations",forbidden)
    repeated=svc.extract_unit(unit["id"])
    assert repeated.facts[0]["bbox"]==ACTUAL
    assert repo.get("facts",original["id"])==original
    assert repo.get("drawing_units",unit["id"])["metadata"]["fact_annotations"][original["id"]]==annotation
    assert repo.list_records("pending_confirmations",version_id=vid)==[before]


def test_replacement_retires_only_matching_open_old_reference_question(reference_case):
    repo,vid,unit,_=reference_case
    original=repo.add_fact(vid,Evidence(unit["id"],Source.VISION,TEXT,BBox(**OLD)))
    meta=unit["metadata"];meta["fact_annotations"]={original["id"]:{"category":"详图索引断链","unclear":False}}
    repo._update("drawing_units",unit["id"],{"metadata_json":json.dumps(meta)})
    stale=repo.create_pending(vid,Evidence(unit["id"],Source.VISION,TEXT,BBox(**OLD)),"局部数字简写或引用图号不明确，不能确认目标图身份")
    other=repo.create_pending(vid,Evidence(unit["id"],Source.VISION,"另一条原始说明",BBox(**OLD)),"看不清")
    svc=service(repo);seed_cache(repo,svc,source_payload())
    svc.extract_unit(unit["id"])
    assert repo.list_records("pending_confirmations",version_id=vid)==[other]


def test_two_literal_sources_do_not_copy_an_existing_human_value(reference_case):
    repo,vid,unit,_=reference_case
    first=repo.add_fact(vid,Evidence(unit["id"],Source.VISION,TEXT,BBox(**OLD)))
    meta=unit["metadata"];meta["fact_annotations"]={first["id"]:{"category":"详图索引断链","unclear":True}}
    repo._update("drawing_units",unit["id"],{"metadata_json":json.dumps(meta)})
    p=repo.create_pending(vid,Evidence(unit["id"],Source.VISION,TEXT,BBox(**OLD)),"看不清")
    repo.confirm_pending(p["id"],status="已确认",confirmed_text="只对原锚点的人工值")
    raw=source_payload(unclear=True)
    second={**raw["facts"][0],"bbox":dict(x0=.2,y0=.2,x1=.4,y1=.3)}
    raw["facts"].append(second)
    svc=service(repo);seed_cache(repo,svc,raw)
    result=svc.extract_unit(unit["id"])
    assert result.facts[0]["bbox"]==OLD and result.facts[1]["bbox"]==ACTUAL
    pending=repo.list_records("pending_confirmations",version_id=vid)
    assert len(pending)==2 and sum(p["status"]=="已确认" for p in pending)==1
    assert not [f for f in repo.list_records("facts",version_id=vid) if f["source"]==Source.USER.value]


def test_copied_unit_with_old_position_metadata_cannot_skip_audit_upgrade_on_shared_cache(reference_case):
    import hashlib
    repo,vid,unit,_=reference_case
    svc=service(repo);seed_cache(repo,svc,source_payload())
    first=svc.extract_unit(unit["id"])
    metadata={"review_relative_path":"review.png","copied_from_unit_id":unit["id"],"extract_status":"ok",
              "extract_image_sha256":first.image_sha256,"extract_prompt_version":svc.prompt_version,
              "extract_cache_identity":hashlib.sha256(svc.cache_role.encode()).hexdigest(),"extract_strategy":"whole"}
    copied=repo.create_unit(vid,unit["original_id"],kind="pdf_page",ordinal=2,name="复制单元",metadata=metadata)
    old=repo.add_fact(vid,Evidence(copied["id"],Source.VISION,TEXT,BBox(**OLD)))
    metadata["fact_annotations"]={old["id"]:{"category":"详图索引断链","confidence":.99,"unclear":False}}
    repo._update("drawing_units",copied["id"],{"metadata_json":json.dumps(metadata)})
    copied=repo.get("drawing_units",copied["id"])
    assert not svc._copied_recognition_is_persisted(copied,first.image_sha256,svc.cache_role)
    result=svc.extract_unit(copied["id"])
    assert result.facts[0]["bbox"]==ACTUAL and result.model_calls==0
    final=repo.get("drawing_units",copied["id"])["metadata"]
    assert final["extract_position_version"]==POSITION_VERSION
    assert all(a.get("position_recovery",{}).get("status")=="unique_exact" for a in final["fact_annotations"].values())
    assert svc._copied_recognition_is_persisted(repo.get("drawing_units",copied["id"]),first.image_sha256,svc.cache_role)


@pytest.mark.parametrize("changed", ["image", "category"])
def test_short_reference_history_requires_same_image_and_category(short_reference_case, monkeypatch, changed):
    repo, vid, unit, svc, actual, *_ = short_reference_case
    initial, = svc.extract_unit(unit["id"]).facts
    stored, = repo.list_records("facts", version_id=vid)
    decision = repo.create_pending(vid, Evidence(unit["id"], Source.VISION, initial["text"], BBox(**actual)),
                                   "人工判断")
    decision = repo.confirm_pending(decision["id"], status="已确认", confirmed_text="原图复核结果")
    raw = source_payload(text="见27")
    if changed == "image":
        Image.new("RGB", (900, 700), "black").save(repo.root / "review.png")
    else:
        raw["facts"][0]["category"] = "设计要求"
    seed_cache(repo, svc, raw)
    attempted = []

    def unavailable(*args, **kwargs):
        attempted.append(True)

    monkeypatch.setattr("backend.extract.service.cached_note_observations", unavailable)
    current, = svc.extract_unit(unit["id"]).facts
    assert attempted and current["text"] == "见27" and current["bbox"] == OLD
    assert current["region_bbox_unlocated"]
    assert repo.get("facts", stored["id"]) == stored
    assert repo.get("pending_confirmations", decision["id"]) == decision
