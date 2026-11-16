"""Resume failed-page extraction once per user start, keeping caches and audit."""
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.extract import ExtractResult, ExtractService, PROMPT_VERSION
from backend.jobs import JobService, StepRegistry, StepResult
from backend.jobs.codes import STEPS
from backend.jobs.plugins import NoOpPlugin, get_default_registry
from backend.models.errors import ModelError
from backend.models.openai_compat import CallResult
from backend.rules.dimension import run_dimension
from backend.store import BBox, Evidence, Source
from tests.backend.jobs.conftest import configure_dual_model


def update_meta(repo,unit,**changes):
    meta=dict(repo.get("drawing_units",unit["id"])["metadata"])
    meta.update(changes)
    repo._update("drawing_units",unit["id"],{"metadata_json":json.dumps(meta)})


def fact(text="100mm",x=.4):
    return {"text":text,"numeric_value":text.removesuffix("mm"),"unit":"mm","category":"尺寸",
        "object_id":"F1","comparison_key":"length","identity_confirmed":True,"confidence":.9,
        "bbox":{"x0":x,"y0":.1,"x1":x+.1,"y1":.2}}


def test_done_partial_extract_then_failed_dual_retries_only_missing_region_and_reconciles(repo,settings_svc,drawing,monkeypatch):
    configure_dual_model(settings_svc)
    vid=drawing["version"]["id"]; first_unit=drawing["unit"]
    Image.new("RGB",(200,100),"blue").save(repo.root/"first.png")
    update_meta(repo,first_unit,review_relative_path="first.png")
    target=repo.create_unit(vid,drawing["original"]["id"],kind="pdf_page",ordinal=2,name="target",drawing_number="S-002",
        metadata={"review_relative_path":"target.png"})
    Image.new("RGB",(3000,1200),"white").save(repo.root/"target.png")
    seeded=ExtractResult(target["id"],vid,"ok",PROMPT_VERSION,facts=[fact("90mm",.1),fact("110mm",.4)],
        title_blocks=[{"drawing_number":"S-002","bbox":{"x0":.8,"y0":.8,"x1":1,"y1":1},"confidence":.9}])
    ExtractService(repo,use_mock=False)._persist(seeded,target)
    old=run_dimension(repo,vid)["issues"][0]
    left=Evidence(target["id"],Source.VISION,old["left_text"],BBox(old["left_x0"],old["left_y0"],old["left_x1"],old["left_y1"]))
    right=Evidence(target["id"],Source.VISION,old["right_text"],BBox(old["right_x0"],old["right_y0"],old["right_x1"],old["right_y1"]))
    manual=repo.create_issue(vid,"human conclusion",left,right,rule_result=old["rule_result"],primary_opinion={"engine":"rules_a"})
    manual=repo.update_issue(manual["id"],review_status="已确认",review_note="人工原证据")
    monkeypatch.setattr("backend.models.cost.estimate_cost_micros",lambda **kwargs:SimpleNamespace(estimated_cost_micros=7))

    class OfflineClient:
        calls=0
        def chat_completions(self,**kwargs):
            self.calls+=1
            if self.calls==4:
                raise ModelError("TIMEOUT","controlled missing region")
            instruction=kwargs["messages"][0]["content"]
            overview="本次是整张图纸概览" in instruction
            tile="本次输入是图纸的局部裁剪区域" in instruction
            payload={"title_blocks":[] if tile else [{"drawing_number":"S-002" if overview else "S-001","name":"基础",
                "bbox":{"x0":.8,"y0":.8,"x1":1,"y1":1},"confidence":.9}],"facts":[] if overview else [fact()]}
            return CallResult(content=json.dumps(payload),input_tokens=1,output_tokens=1,raw_status=200,
                model="offline",provider="offline",latency_ms=0)
    client=OfflineClient()
    counters=Counter(); registry=get_default_registry()
    class Counted:
        def __init__(self,plugin):
            self.plugin=plugin; self.name=plugin.name; self.uses_model=plugin.uses_model
        def run(self,ctx):
            counters[self.name]+=1
            return self.plugin.run(ctx)
    for name in STEPS:
        registry.register(Counted(registry.get(name)))
    jobs=JobService(repo,settings_service=settings_svc,registry=registry)
    jobs.extract_factory=lambda:ExtractService(repo,use_mock=False,model_client=client,provider="mock",model_name="offline",
        task_id=jobs._running[vid],format_retries=0)
    def review(ctx):
        return StepResult("failed",incomplete=[{"reason":"controlled review failure","category":"incomplete"}]) if counters["dual_review"]==1 else StepResult("done")
    jobs.extras["dual_review_hook"]=review
    from backend.rules.registry import reconcile_rule_results
    reconciliations=[]
    def reconcile(*args,**kwargs):
        reconciliations.append(kwargs["trigger"])
        return reconcile_rule_results(*args,**kwargs)
    monkeypatch.setattr("backend.rules.registry.reconcile_rule_results",reconcile)

    first=jobs.start_review(vid)
    task=repo.get("review_tasks",first["task_id"])
    assert first["status"]=="失败" and task["checkpoints"]["extract"]["state"]=="done"
    assert client.calls==4 and task["estimated_cost_micros"]==21
    assert repo.get("drawing_units",target["id"])["metadata"]["extract_status"]=="incomplete"
    original_snapshot=task["tolerance_snapshot"]
    settings_svc.save_settings({"dimension_tolerance_mm":9})
    jobs.start_review(vid,run=False)
    assert client.calls==4 and repo.get("review_tasks",task["id"])["checkpoints"]["extract"]["state"]=="done"
    second=jobs.start_review(vid)
    after=repo.get("review_tasks",task["id"])
    assert second["task_id"]==first["task_id"] and second["status"]=="已完成"
    assert client.calls==5  # Completed page, overview and first tile were reused; only the missing tile ran.
    assert len(repo.list_records("model_calls",version_id=vid))==5
    assert after["estimated_cost_micros"]==28 and after["tolerance_snapshot"]==original_snapshot
    assert counters=={"extract":2,"semantic":2,"link":2,"rules_a":2,"rules_b":2,"dual_review":2,"finalize":1}
    assert reconciliations==["partial_extract_retry"]
    assert after["checkpoints"]["_meta"]["partial_extract_retry"]=={"attempt":1,"unit_ids":[target["id"]]}
    assert not any("controlled missing region" in row["reason"] or "controlled review failure" in row["reason"] for row in after["incomplete"])
    assert old["id"] not in {row["id"] for row in repo.list_records("issues",version_id=vid)}
    kept=repo.get("issues",manual["id"])
    assert kept["review_status"]=="已确认" and kept["review_note"].startswith("人工原证据")
    assert kept["rule_result"]["evidence_changed_after_confirmation"]
    for side in ("left","right"):
        for key in ("source","text","x0","y0","x1","y1"):
            assert kept[f"{side}_{key}"]==manual[f"{side}_{key}"]
    assert repo.verify_originals(vid)


def failed_task(repo,drawing,incomplete):
    task=repo.create_task(drawing["version"]["id"])
    checkpoints={name:{"state":"done"} for name in STEPS}
    checkpoints.update(dual_review={"state":"failed"},finalize={"state":"pending"})
    repo.update_task(task["id"],status="失败",checkpoints=checkpoints,incomplete=incomplete,estimated_cost_micros=13)
    return repo.get("review_tasks",task["id"])


@pytest.mark.parametrize("kind",["source_warning","ordinary_pending","no_preview","out_of_scope"])
def test_import_warnings_pending_and_unrendered_sources_do_not_reset_done_extract(repo,settings_svc,drawing,monkeypatch,kind):
    configure_dual_model(settings_svc)
    meta={"extract_status":"ok","review_relative_path":"preview.png"}
    if kind!="no_preview":
        Image.new("RGB",(20,10)).save(repo.root/"preview.png")
    else:
        meta["extract_status"]="incomplete"
    if kind=="out_of_scope":
        meta.update(extract_status="incomplete",out_of_scope=True)
    if kind=="ordinary_pending":
        meta["title_identity_uncertain"]=True
        repo.create_pending(drawing["version"]["id"],Evidence(drawing["unit"]["id"],Source.VISION,"图签?",BBox(.1,.1,.2,.2)),"看不清")
    update_meta(repo,drawing["unit"],**meta)
    task=failed_task(repo,drawing,[{"step":"extract","reason":"DWG_INCOMPLETE" if kind=="source_warning" else kind,"category":"incomplete"}])
    registry=StepRegistry()
    for name in STEPS:
        registry.register(NoOpPlugin(name))
    class NoExtract:
        name="extract";uses_model=False
        def run(self,ctx):
            pytest.fail("A done extraction without a retryable failed page must stay skipped")
    registry.register(NoExtract())
    monkeypatch.setattr("backend.rules.registry.reconcile_rule_results",lambda *args,**kwargs:pytest.fail("Ordinary resume must retain finished rules"))
    jobs=JobService(repo,settings_service=settings_svc,registry=registry)
    result=jobs.start_review(drawing["version"]["id"])
    after=repo.get("review_tasks",task["id"])
    assert result["task_id"]==task["id"] and after["checkpoints"]["extract"]["state"]=="done"
    assert after["estimated_cost_micros"]==13 and after["incomplete"]==task["incomplete"]
    assert not after["checkpoints"].get("_meta",{}).get("partial_extract_retry")


def test_retry_cost_pause_preserves_old_failed_page_evidence_and_spend(repo,settings_svc,drawing,monkeypatch):
    configure_dual_model(settings_svc)
    Image.new("RGB",(20,10)).save(repo.root/"preview.png")
    update_meta(repo,drawing["unit"],extract_status="incomplete",review_relative_path="preview.png")
    prior={"step":"extract","unit_id":drawing["unit"]["id"],"reason":"original timeout","category":"incomplete"}
    task=failed_task(repo,drawing,[prior])
    jobs=JobService(repo,settings_service=settings_svc)
    monkeypatch.setattr(jobs,"_cost_allows_new_call",lambda:False)
    result=jobs.start_review(drawing["version"]["id"])
    after=repo.get("review_tasks",task["id"])
    assert result["status"]=="暂停" and after["estimated_cost_micros"]==13
    assert prior in after["incomplete"] and not repo.list_records("model_calls",version_id=drawing["version"]["id"])


def test_a_still_failed_page_gets_one_attempt_per_explicit_start_not_an_internal_loop(repo,settings_svc,drawing):
    configure_dual_model(settings_svc)
    Image.new("RGB",(20,10)).save(repo.root/"preview.png")
    update_meta(repo,drawing["unit"],review_relative_path="preview.png")
    registry=StepRegistry();counts=Counter()
    for name in STEPS:
        registry.register(NoOpPlugin(name))
    class Extract:
        name="extract";uses_model=False
        def run(self,ctx):
            counts["extract"]+=1
            update_meta(repo,drawing["unit"],extract_status="incomplete")
            return StepResult("done",incomplete=[{"unit_id":drawing["unit"]["id"],"reason":"timeout","category":"incomplete"}])
    class Dual:
        name="dual_review";uses_model=False
        def run(self,ctx):
            counts["dual_review"]+=1
            return StepResult("failed",incomplete=[{"reason":"review unavailable","category":"incomplete"}])
    registry.register(Extract());registry.register(Dual())
    jobs=JobService(repo,settings_service=settings_svc,registry=registry)
    first=jobs.start_review(drawing["version"]["id"])
    second=jobs.start_review(drawing["version"]["id"])
    assert first["status"]==second["status"]=="失败" and first["task_id"]==second["task_id"]
    assert counts=={"extract":2,"dual_review":2}
    assert repo.get("review_tasks",second["task_id"])["checkpoints"]["_meta"]["partial_extract_retry"]["attempt"]==1


def test_selected_page_retries_only_that_page_and_keeps_other_coverage(repo, settings_svc, drawing, monkeypatch):
    import httpx
    from backend.jobs import JobError
    from backend.api.review_status import list_issues

    configure_dual_model(settings_svc)
    vid, target = drawing["version"]["id"], drawing["unit"]
    good = repo.create_unit(vid, drawing["original"]["id"], kind="pdf_page", ordinal=2, name="good",
                            drawing_number="A-02", metadata={"extract_status": "ok"})
    other = repo.create_unit(vid, drawing["original"]["id"], kind="pdf_page", ordinal=3, name="other",
                             drawing_number="A-03", metadata={})
    for unit, color in ((target, "blue"), (other, "red")):
        path = f"{unit['id']}.png"
        Image.new("RGB", (200, 100), color).save(repo.root / path)
        update_meta(repo, unit, extract_status="incomplete", review_relative_path=path,
                    extract_incomplete_reason="模型调用失败：TIMEOUT: 等待模型响应超时")
    update_meta(repo, target, manual_drawing_number_confirmed=True, title_drawing_number="A-01")
    source_warning = {"step": "extract", "reason": "字体替代", "category": "unreadable"}
    other_failure = {"step": "extract", "unit_id": other["id"], "reason": "旧失败", "category": "incomplete"}
    target_failure = {**other_failure, "unit_id": target["id"]}
    old_title = {"step": "rules_a", "unit_id": target["id"],
                 "reason": "图纸身份尚未核实，目录缺图与张数核对未完成", "category": "incomplete"}
    jobs = JobService(repo, settings_service=settings_svc)
    route = jobs._model_route(repo.get_settings())
    checkpoints = {name: {"state": "done"} for name in STEPS}
    checkpoints["_meta"] = {"model_route": route, "semantic_disclosed": True}
    task = repo.create_task(vid, checkpoints=checkpoints)
    repo.update_task(task["id"], status="已完成", checkpoints=checkpoints,
                     incomplete=[source_warning, other_failure, target_failure, old_title], estimated_cost_micros=0)
    api_row, = [item for item in list_issues(repo, vid)["incomplete_items"] if item["unit_id"] == target["id"]]
    assert api_row["title_verified"] and api_row["retryable"] and api_row["phase"] == "视觉识别未完成"

    model_calls = []
    result_payload = {"title_blocks": [{"drawing_number": "A-01", "name": "演示图", "confidence": .99,
        "bbox": {"x0": .8, "y0": .8, "x1": .99, "y1": .99}}], "facts": []}

    def handler(request):
        body = json.loads(request.content)
        model_calls.append(body)
        if len(model_calls) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        event = {"choices": [{"delta": {"content": json.dumps(result_payload)}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 3122, "completion_tokens": 100}}
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")

    configured = jobs._model_client_for_slot("primary", route["primary_model"])
    configured._transport = httpx.MockTransport(handler)
    jobs.extras["primary_client_factory"] = lambda: configured
    jobs.extras["review_client_factory"] = lambda: pytest.fail("单页续审不得调用复核模型")
    good_before = repo.get("drawing_units", good["id"])
    other_before = repo.get("drawing_units", other["id"])

    disclosure = jobs.prepare_disclosure(vid, retry_unit_id=target["id"])["disclosure_id"]
    with pytest.raises(JobError):
        jobs.start_review(vid, retry_unit_id=other["id"], disclosure_id=disclosure)
    with pytest.raises(JobError):
        jobs.start_review(vid, retry_unit_id=target["id"])
    first = jobs.start_review(vid, retry_unit_id=target["id"], disclosure_id=disclosure)
    assert first["task_id"] == task["id"] and len(model_calls) == 1
    first_log, = repo.list_records("model_calls", version_id=vid)
    assert first_log["unit_id"] == target["id"] and first_log["diagnostics"]["failure_code"] == "TIMEOUT"
    assert repo.get("drawing_units", target["id"])["metadata"]["extract_model_failure"]["retryable"] is True
    with pytest.raises(JobError):
        jobs.start_review(vid, retry_unit_id=target["id"], disclosure_id=disclosure)

    disclosure = jobs.prepare_disclosure(vid, retry_unit_id=target["id"])["disclosure_id"]
    second = jobs.start_review(vid, retry_unit_id=target["id"], disclosure_id=disclosure)
    assert second["status"] == "已完成" and len(model_calls) == 2
    import base64
    for body in model_calls:
        url = body["messages"][1]["content"][0]["image_url"]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == (repo.root / f"{target['id']}.png").read_bytes()
        assert body["stream"] is True
    assert repo.get("drawing_units", good["id"]) == good_before
    assert repo.get("drawing_units", other["id"]) == other_before
    current = repo.get("review_tasks", task["id"])
    assert source_warning in current["incomplete"] and other_failure in current["incomplete"]
    assert not any(row.get("step") == "extract" and row.get("unit_id") == target["id"] for row in current["incomplete"])
    assert current["checkpoints"]["semantic"] == checkpoints["semantic"]
    assert current["checkpoints"]["dual_review"] == checkpoints["dual_review"]
    assert repo.get("drawing_units", target["id"])["metadata"]["manual_drawing_number_confirmed"] is True
    logs = repo.list_records("model_calls", version_id=vid)
    assert [row["status"] for row in logs] == ["error", "ok"]
    assert logs[-1]["diagnostics"]["usage_reported"] is True
    update_meta(repo, other, extract_model_failure={"code": "AUTH_INVALID", "retryable": False})
    with pytest.raises(JobError):
        jobs.prepare_disclosure(vid, retry_unit_id=other["id"])
    assert len(model_calls) == 2
