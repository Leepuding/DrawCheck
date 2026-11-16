from backend.pending.service import confirm_pending_item
from backend.rules import RulesAService
from backend.store import BBox, Evidence, Source
from tests.backend.rules.test_rules_b import seed


def setup_case(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    u = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="同一构件", drawing_number="S-001")
    left = Evidence(u["id"], Source.VISION, "100mm", BBox(.1,.1,.2,.2))
    right = Evidence(u["id"], Source.VISION, "102mm", BBox(.3,.1,.4,.2))
    import json
    facts = [repo.add_fact(vid, value, unit="mm") for value in (left, right)]
    repo._update("drawing_units", u["id"], {"metadata_json": json.dumps({"fact_annotations": {f["id"]: {"category":"尺寸", "object_id":"构件1", "comparison_key":"构件1长度", "identity_confirmed":True} for f in facts}})})
    result = RulesAService(repo).run(vid)
    issue = next(i for i in result.issues if i["rule_result"]["category"] == "尺寸")
    pending = repo.create_pending(vid, right, "看不清")
    return vid, issue, pending


def dimensions(repo, vid):
    return [i for i in repo.list_records("issues", version_id=vid) if i["rule_result"]["category"] == "尺寸"]


def test_confirmation_reedit_and_unsure_recalculate_no_calls(repo, tmp_path):
    vid, issue, pending = setup_case(repo, tmp_path)
    out = confirm_pending_item(repo, pending["id"], {"status":"已确认", "confirmed_text":"100mm"})
    assert out["recalc"]["invoked"]
    assert out["model_calls_delta"] == 0
    assert dimensions(repo, vid) == []
    confirm_pending_item(repo, pending["id"], {"status":"已确认", "confirmed_text":"103mm"})
    assert len(dimensions(repo, vid)) == 1
    assert dimensions(repo, vid)[0]["right_source"] == Source.USER.value
    confirm_pending_item(repo, pending["id"], {"status":"已确认", "confirmed_text":"103mm"})
    assert len(dimensions(repo, vid)) == 1
    confirm_pending_item(repo, pending["id"], {"status":"无法确定"})
    assert dimensions(repo, vid) == []
    # Raw machine and appended human evidence is retained for traceability.
    assert len(repo.list_records("facts", version_id=vid)) == 5


def test_manual_decision_preserved_when_evidence_changes(repo, tmp_path):
    vid, issue, pending = setup_case(repo, tmp_path)
    repo.update_issue(issue["id"], review_status="已确认", review_note="人工核对")
    confirm_pending_item(repo, pending["id"], {"status":"已确认", "confirmed_text":"100mm"})
    prior = repo.get("issues", issue["id"])
    assert prior["review_status"] == "已确认"
    assert prior["review_note"].startswith("人工核对")
    assert prior["rule_result"]["evidence_changed_after_confirmation"] is True
    assert len(dimensions(repo, vid)) == 1
