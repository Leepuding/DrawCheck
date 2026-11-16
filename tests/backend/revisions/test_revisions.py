import json
import errno

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.revisions import RevisionService
from backend.revisions.router import create_router
from backend.store import BBox, Evidence, Repository, Source, StoreError


@pytest.fixture
def store(tmp_path):
    with Repository(tmp_path / "data") as value:
        yield value


def sheet(store, tmp_path, version, number, content, filename=None):
    source = tmp_path / (filename or f"{number}.pdf")
    source.write_bytes(content)
    original = store.import_original(version["id"], source)["record"]
    return store.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=1, name=number or source.name, drawing_number=number)


def evidence(unit, text="200", x=0):
    return Evidence(unit["id"], Source.VISION, text, BBox(x, .1, x + .1, .2))


def issue(store, version, left, right, category="尺寸", x=0):
    return store.create_issue(version["id"], "跨图不一致", evidence(left, x=x), evidence(right, "210", x), rule_result={"category": category})


def completed_task(store, version, incomplete=None, missing_rule=False):
    task = store.create_task(version["id"])
    checkpoints = {key: {"state": "done"} for key in ("extract", "link", "rules_a", "rules_b")}
    if missing_rule:
        checkpoints["rules_b"] = {"state": "skipped", "reason": "no_plugin"}
    return store.update_task(task["id"], status="已完成", checkpoints=checkpoints, incomplete=incomplete or [], estimated_cost_micros=0)


def prepare_all(service, version_id):
    service.prepare(version_id)
    return service.rematch(version_id)


def test_partial_revision_matches_number_reuses_facts_and_marks_associated(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    first = sheet(store, tmp_path, old, "T0102", b"old")
    second = sheet(store, tmp_path, old, "T0103", b"keep")
    third = sheet(store, tmp_path, old, "T0104", b"keep-other")
    fact = store.add_fact(old["id"], evidence(second), numeric_value="200", unit="mm")
    store._update("drawing_units", second["id"], {"metadata_json": json.dumps({"fact_annotations": {fact["id"]: {"category": "尺寸"}}})})
    store.add_association(old["id"], first["id"], second["id"], "构件")
    revision = RevisionService(store)
    new = revision.create_revision(old["id"])
    changed = sheet(store, tmp_path, {"id": new["version_id"]}, "T0102", b"new", "unrelated-name.pdf")
    plan = prepare_all(revision, new["version_id"])
    assert plan["changed_unit_ids"] == [changed["id"]]
    assert set(plan["affected_unit_ids"]) == {changed["id"], plan["old_to_new"][second["id"]]}
    assert plan["old_to_new"][third["id"]] not in plan["affected_unit_ids"]
    assert len(store.list_records("drawing_units", version_id=new["version_id"])) == 3
    facts = store.list_records("facts", version_id=new["version_id"])
    assert len(facts) == 1 and facts[0]["numeric_value"] == "200"
    copied = store.get("drawing_units", plan["old_to_new"][second["id"]])
    assert facts[0]["id"] in copied["metadata"]["fact_annotations"]
    assert revision.prepare(new["version_id"])["old_to_new"] == plan["old_to_new"]
    assert len(store.list_records("facts", version_id=new["version_id"])) == 1


def test_failed_draft_keeps_source_writable_and_retry_reuses_draft(store, tmp_path):
    project = store.create_project("历史工程")
    old = store.create_version(project["id"], "A")
    unit = sheet(store, tmp_path, old, "T1", b"old")
    original_issue = issue(store, old, unit, unit)
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    with pytest.raises(StoreError, match="尚未导入"):
        service.prepare(draft["version_id"])
    store.update_issue(original_issue["id"], review_status="排除")
    assert service.create_revision(old["id"])["version_id"] == draft["version_id"]
    assert service.open_project(project["id"])["version_id"] == old["id"]
    assert service.open_project(project["id"])["can_continue"] is True


def test_failed_revision_manifest_write_rolls_back_activation(store, tmp_path, monkeypatch):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    sheet(store, tmp_path, old, "T1", b"old")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    sheet(store, tmp_path, {"id": draft["version_id"]}, "T1", b"new")
    def full(*args):
        raise OSError(errno.ENOSPC, "模拟磁盘已满")
    monkeypatch.setattr(service, "_inherit_import_diagnostics", full)
    with pytest.raises(StoreError) as caught:
        service.prepare(draft["version_id"])
    assert caught.value.code.value == "DISK_FULL"
    assert service.comparison(draft["version_id"])["status"] == "draft"
    assert store.create_task(old["id"])["version_id"] == old["id"]


def test_hash_unchanged_and_unknown_number_fallback(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    old_unit = sheet(store, tmp_path, old, "T0102", b"same", "T0102_A.pdf")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    current = sheet(store, tmp_path, {"id": draft["version_id"]}, "", b"same", "T0102_B.pdf")
    plan = prepare_all(service, draft["version_id"])
    assert plan["old_to_new"] == {old_unit["id"]: current["id"]}
    assert plan["changed_unit_ids"] == []
    assert plan["reused_unit_ids"] == [current["id"]]
    assert store.get("drawing_units", current["id"])["drawing_number"] == "T0102"


@pytest.mark.parametrize("filename", ["图纸（修订版）.pdf", "图纸_修订版B.pdf", "图纸_第2版.pdf"])
def test_unknown_drawing_number_uses_filename_without_revision_words(store, tmp_path, filename):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    original = sheet(store, tmp_path, old, "T0102", b"old", "图纸.pdf")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    new = sheet(store, tmp_path, {"id": draft["version_id"]}, "", b"new", filename)
    assert service.prepare(draft["version_id"])["old_to_new"] == {original["id"]: new["id"]}


def test_number_mismatch_is_new_sheet(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    unit = sheet(store, tmp_path, old, "T1", b"old", "same.pdf")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    incoming = sheet(store, tmp_path, {"id": draft["version_id"]}, "T2", b"new", "same.pdf")
    plan = prepare_all(service, draft["version_id"])
    assert incoming["id"] in plan["changed_unit_ids"]
    assert plan["old_to_new"][unit["id"]] != incoming["id"]
    # Known, different drawing numbers must not be silently paired by filename.
    assert len(store.list_records("drawing_units", version_id=draft["version_id"])) == 2


def test_recognized_number_rematches_renamed_pdf_before_carrying_old_sheets(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    first = sheet(store, tmp_path, old, "T1", b"old-one", "旧文件甲.pdf")
    replaced = sheet(store, tmp_path, old, "T2", b"old-two", "旧文件乙.pdf")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    incoming = sheet(store, tmp_path, {"id": draft["version_id"]}, "1", b"new-two", "完全改名.pdf")
    initial = service.prepare(draft["version_id"])
    assert initial["old_to_new"] == {} and initial["match_complete"] is False
    assert len(store.list_records("drawing_units", version_id=draft["version_id"])) == 1
    # Catalog synchronization after actual recognition supplies the authoritative number.
    store.update_unit_directory(incoming["id"], name="修订T2", drawing_number="T2")
    store._update("drawing_units", incoming["id"], {"metadata_json": json.dumps({"extract_status": "ok"})})
    final = service.rematch(draft["version_id"])
    assert final["old_to_new"][replaced["id"]] == incoming["id"]
    assert final["old_to_new"][first["id"]] != incoming["id"]
    assert final["match_complete"] is True
    assert len(store.list_records("drawing_units", version_id=draft["version_id"])) == 2
    assert service.rematch(draft["version_id"])["old_to_new"] == final["old_to_new"]


def test_pdf_page_ordinals_are_not_drawing_numbers(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    first = sheet(store, tmp_path, old, "1", b"one", "图纸甲_A.pdf")
    second = sheet(store, tmp_path, old, "1", b"two", "图纸乙_A.pdf")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    current = sheet(store, tmp_path, {"id": draft["version_id"]}, "1", b"changed", "图纸乙_B.pdf")
    plan = prepare_all(service, draft["version_id"])
    assert plan["old_to_new"][second["id"]] == current["id"]
    assert plan["old_to_new"][first["id"]] != current["id"]


@pytest.mark.parametrize("unchanged,new_value", [(False, 200), (False, 300), (True, 200)])
def test_job_pipeline_recognizes_then_rematches_and_inherits_real_rule_decision(store, tmp_path, unchanged, new_value):
    from backend.extract.service import ExtractResult
    from backend.jobs import JobService
    from backend.rules import RulesAService

    project = store.create_project("管道接线核验")
    old = store.create_version(project["id"], "A")
    first = sheet(store, tmp_path, old, "T1", b"old-one", "甲_A.pdf")
    second = sheet(store, tmp_path, old, "T2", b"old-two", "乙_A.pdf")

    def persist_fact(unit, value):
        fact = store.add_fact(unit["version_id"], evidence(unit, f"{value}mm"), numeric_value=str(value), unit="mm")
        metadata = {"extract_status": "ok", "fact_annotations": {fact["id"]: {
            "category": "尺寸", "object_id": "B-1", "identity_confirmed": True, "comparison_key": "width"}}}
        store._update("drawing_units", unit["id"], {"metadata_json": json.dumps(metadata)})

    persist_fact(first, 200)
    persist_fact(second, 210)
    old_problem = RulesAService(store).run(old["id"]).issues[0]
    store.update_issue(old_problem["id"], review_status="排除", review_note="原版人工备注")
    revision = RevisionService(store)
    draft = revision.create_revision(old["id"])
    incoming = sheet(store, tmp_path, {"id": draft["version_id"]}, "1", b"old-one" if unchanged else b"changed", "甲_B.pdf" if unchanged else "完全新文件名.pdf")
    called = []

    class Extractor:
        def extract_unit(self, uid, persist=True):
            called.append(uid)
            unit = store.get("drawing_units", uid)
            persist_fact(unit, new_value)
            return ExtractResult(unit_id=uid, version_id=unit["version_id"], status="ok", prompt_version="test",
                                 title_blocks=[{"drawing_number": "T1", "name": "甲图", "specialty": "结构"}])

    from backend.settings import SettingsService
    from backend.settings.credentials import CredentialService, MemoryCredentialBackend

    settings = SettingsService(store, credentials=CredentialService(MemoryCredentialBackend()))
    settings.save_settings({"monthly_limit_yuan": 100, "primary_model": {
        "provider": "custom", "model": "synthetic-vision", "base_url": "https://review.example.invalid/v1"}})
    view = JobService(store, settings_service=settings, extract_factory=Extractor).start_review(draft["version_id"])
    assert view["status"] == "已完成", json.dumps(store.list_records("review_tasks", version_id=draft["version_id"]), ensure_ascii=False)
    assert called == ([] if unchanged else [incoming["id"]])
    plan = revision.prepare(draft["version_id"])
    assert plan["match_complete"] is True and plan["old_to_new"][first["id"]] == incoming["id"]
    assert len(store.list_records("drawing_units", version_id=draft["version_id"])) == 2
    problems = store.list_records("issues", version_id=draft["version_id"])
    continued = next(row for row in problems if row.get("previous_issue_id") == old_problem["id"])
    assert continued["comparison_tag"] == "仍存在"
    assert continued["review_status"] == "排除" and continued["review_note"] == "原版人工备注"


@pytest.mark.parametrize("new_unit, status", [("mm", "排除"), ("cm", "疑似")])
def test_revision_does_not_inherit_exclusion_when_declared_unit_changes(store,tmp_path,new_unit,status):
    from backend.rules import RulesAService
    from tests.backend.rules.test_reference_reliability import add

    project = store.create_project("卷册修订")
    old = store.create_version(project["id"], "A")
    a = sheet(store,tmp_path,old,"P-T0102-01",b"original one")
    b = sheet(store,tmp_path,old,"P-T0102-02",b"original two")

    def put(version, row, raw, length_unit=None):
        store._update("drawing_units",row["id"], {"metadata_json": json.dumps({"extract_status": "ok"})})
        add(store,version,row,raw,category="尺寸",object_id="基础F1",identity_confirmed=True)
        if length_unit:
            add(store,version,row,f"尺寸均以{length_unit}为单位。",y=.6,source=Source.PDF_TEXT)

    put(old["id"],a,"100","mm")
    put(old["id"],b,"105")
    previous, = RulesAService(store).run(old["id"]).issues
    store.update_issue(previous["id"],review_status="排除",review_note="旧单位口径")
    revision = RevisionService(store)
    current = {"id": revision.create_revision(old["id"])["version_id"]}
    incoming = sheet(store,tmp_path,current,"P-T0102-01",b"revised one")
    prepare_all(revision,current["id"])
    put(current["id"],incoming,"100",new_unit)
    updated, = RulesAService(store).run(current["id"]).issues
    revision.finalize(current["id"],completed_task(store,current)["id"])
    assert store.get("issues",updated["id"])["review_status"] == status
    assert store.get("issues",previous["id"])["review_note"] == "旧单位口径"


@pytest.mark.parametrize("new_codes,expected", [
    (("F-01", "F1"), "新出现"),
    (("T-001", "T01"), "仍存在"),
])
def test_revision_identifier_identity_controls_manual_decision_inheritance(store, tmp_path, new_codes, expected):
    from backend.rules import RulesBService
    from tests.backend.rules.test_rules_b import add

    project = store.create_project("编号修订")
    old = store.create_version(project["id"], "A")
    old_units = [sheet(store, tmp_path, old, f"S-00{i}", f"old{i}".encode()) for i in (1, 2)]
    for drawing, code in zip(old_units, ("T-01", "T1")):
        add(store, old["id"], drawing, code, category="编号写法")
    previous, = RulesBService(store).run(old["id"]).issues
    store.update_issue(previous["id"], review_status="排除", review_note="T-01 已人工核对")
    service = RevisionService(store)
    new = {"id": service.create_revision(old["id"])["version_id"]}
    new_units = [sheet(store, tmp_path, new, f"S-00{i}", f"new{i}".encode()) for i in (1, 2)]
    prepare_all(service, new["id"])
    for drawing, code in zip(new_units, new_codes):
        add(store, new["id"], drawing, code, category="编号写法")
    current, = RulesBService(store).run(new["id"]).issues
    assert current["review_status"] == "疑似"

    service.finalize(new["id"], completed_task(store, new)["id"])
    result = store.get("issues", current["id"])
    assert result["comparison_tag"] == expected
    assert result["left_text"] == new_codes[0] and result["right_text"] == new_codes[1]
    if expected == "新出现":
        assert result["review_status"] == "疑似" and result["review_note"] == ""
        assert result["previous_issue_id"] is None
    else:
        assert result["review_status"] == "排除" and result["review_note"] == "T-01 已人工核对"
        assert result["previous_issue_id"] == previous["id"]


def test_comparison_inherits_review_only_marks_resolved_after_complete(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    a = sheet(store, tmp_path, old, "T1", b"old")
    b = sheet(store, tmp_path, old, "T2", b"b")
    persisted = issue(store, old, a, b)
    store.update_issue(persisted["id"], review_status="排除", review_note="设计变更说明")
    resolved = issue(store, old, a, b, "标高", .5)
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    new = {"id": draft["version_id"]}
    a2 = sheet(store, tmp_path, new, "T1", b"changed")
    plan = prepare_all(service, new["id"])
    b2 = store.get("drawing_units", plan["old_to_new"][b["id"]])
    continued = issue(store, new, a2, b2)
    new_issue = issue(store, new, a2, b2, "说明", .7)
    # An unfinished or skipped rule cannot prove absence.
    task = completed_task(store, new, missing_rule=True)
    comparison = service.finalize(new["id"], task["id"])
    assert comparison["counts"] == {"仍存在": 1, "已解决": 0, "新出现": 1}
    assert comparison["comparison_complete"] is False
    assert store.get("issues", continued["id"])["review_status"] == "排除"
    assert store.get("issues", continued["id"])["review_note"] == "设计变更说明"
    assert store.get("issues", new_issue["id"])["review_status"] == "疑似"
    store.save_settings({"dimension_tolerance_mm": 3})
    task2 = completed_task(store, new)
    assert task2["tolerance_snapshot"]["dimension_tolerance_mm"] == 3
    comparison = service.finalize(new["id"], task2["id"])
    assert comparison["counts"] == {"仍存在": 1, "已解决": 1, "新出现": 1}
    assert service.finalize(new["id"], task2["id"])["counts"] == comparison["counts"]
    assert store.get("issues", resolved["id"])["comparison_tag"] is None
    assert len(store.list_records("issues", version_id=old["id"])) == 2


@pytest.mark.parametrize("blocker", ["incomplete", "pending"])
def test_partial_review_never_marks_missing_old_issue_resolved(store, tmp_path, blocker):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    first = sheet(store, tmp_path, old, "T1", b"old")
    issue(store, old, first, first)
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    current = sheet(store, tmp_path, {"id": draft["version_id"]}, "T1", b"new")
    prepare_all(service, draft["version_id"])
    if blocker == "pending":
        store.create_pending(draft["version_id"], evidence(current), "无法识别")
    task = completed_task(store, {"id": draft["version_id"]}, incomplete=[{"reason": "读不了"}] if blocker == "incomplete" else [])
    assert service.finalize(draft["version_id"], task["id"])["counts"]["已解决"] == 0


def test_new_task_recomputes_completed_comparison_and_preserves_task_history(store, tmp_path):
    project = store.create_project("重复审查工程")
    old = store.create_version(project["id"], "A")
    first = sheet(store, tmp_path, old, "T1", b"old")
    previous = issue(store, old, first, first)
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    current_version = {"id": draft["version_id"]}
    current = sheet(store, tmp_path, current_version, "T1", b"new")
    prepare_all(service, current_version["id"])
    active_issue = issue(store, current_version, current, current)
    task1 = completed_task(store, current_version)
    assert service.finalize(current_version["id"], task1["id"])["counts"] == {"仍存在": 1, "已解决": 0, "新出现": 0}

    store.save_settings({"dimension_tolerance_mm": 100})
    task2 = completed_task(store, current_version)
    service.invalidate_comparison(current_version["id"], task2["id"])
    # The rules reconciliation contract removes a suspected finding that no longer exceeds tolerance.
    store._connection.execute("DELETE FROM issues WHERE id=?", (active_issue["id"],))
    new_summary = service.finalize(current_version["id"], task2["id"])
    assert new_summary["counts"] == {"仍存在": 0, "已解决": 1, "新出现": 0}
    assert new_summary["task_id"] == task2["id"]
    derived = store.list_records("issues", version_id=current_version["id"])
    assert len(derived) == 1 and derived[0]["previous_issue_id"] == previous["id"]
    # Resuming/repeating the same task must retain its comparison checkpoint.
    service.invalidate_comparison(current_version["id"], task2["id"])
    assert service.finalize(current_version["id"], task2["id"])["counts"] == new_summary["counts"]
    history = {row["task_id"]: row for row in service.history()["items"] if row["task_id"]}
    assert history[task1["id"]]["issue_count"] == 1
    assert history[task1["id"]]["comparison_counts"]["仍存在"] == 1
    assert history[task2["id"]]["issue_count"] == 0
    assert history[task2["id"]]["comparison_counts"]["已解决"] == 1
    assert history[task2["id"]]["tolerance_snapshot"]["dimension_tolerance_mm"] == 100


def test_later_task_does_not_overwrite_current_manual_conclusion_or_changed_condition_note(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    source = sheet(store, tmp_path, old, "T1", b"old")
    previous = issue(store, old, source, source)
    store.update_issue(previous["id"], review_status="排除", review_note="上一版备注")
    service = RevisionService(store)
    draft = service.create_revision(old["id"])
    version = {"id": draft["version_id"]}
    unit = sheet(store, tmp_path, version, "T1", b"new")
    prepare_all(service, version["id"])
    problem = issue(store, version, unit, unit)
    first = completed_task(store, version)
    service.finalize(version["id"], first["id"])
    store.update_issue(problem["id"], review_status="已确认", review_note="本版人工结论；审查条件已修改，请重新核对")
    store._update("issues", problem["id"], {"rule_result_json": json.dumps({"category": "尺寸", "evidence_changed_after_confirmation": True})})
    second = completed_task(store, version, incomplete=[{"reason": "需重新核对"}])
    service.finalize(version["id"], second["id"])
    result = store.get("issues", problem["id"])
    assert result["review_status"] == "已确认" and "本版人工结论" in result["review_note"]
    assert "审查条件已修改" in result["review_note"]
    store.update_issue(problem["id"], review_status="疑似", review_note="本版改回待复核")
    store._update("issues", problem["id"], {"rule_result_json": json.dumps({"category": "尺寸"})})
    third = completed_task(store, version)
    service.finalize(version["id"], third["id"])
    assert store.get("issues", problem["id"])["review_status"] == "疑似"
    assert store.get("issues", problem["id"])["review_note"] == "本版改回待复核"


def test_ordinary_task_history_uses_frozen_counts_and_unknown_legacy_is_not_zero(store, tmp_path):
    project = store.create_project("工程")
    version = store.create_version(project["id"], "A")
    unit = sheet(store, tmp_path, version, "T1", b"one")
    issue(store, version, unit, unit)
    service = RevisionService(store)
    first = completed_task(store, version)
    assert service.finalize(version["id"], first["id"]) is None
    issue(store, version, unit, unit, "标高")
    second = completed_task(store, version)
    service.finalize(version["id"], second["id"])
    legacy = store.create_task(version["id"])
    rows = {row["task_id"]: row for row in service.history()["items"]}
    assert rows[first["id"]]["issue_count"] == 1
    assert rows[second["id"]]["issue_count"] == 2
    assert rows[legacy["id"]]["issue_count"] is None
    assert rows[legacy["id"]]["count_source"] == "unrecorded"


def test_history_source_continues_latest_but_explicit_old_version_stays_readonly(store, tmp_path):
    project = store.create_project("工程")
    old = store.create_version(project["id"], "A")
    sheet(store, tmp_path, old, "T1", b"old")
    service = RevisionService(store)
    task = completed_task(store, old)
    service.finalize(old["id"], task["id"])
    newer = store.create_version(project["id"], "B")
    sheet(store, tmp_path, newer, "T1", b"new")
    readonly = service.open_project(project["id"], version_id=old["id"])
    assert readonly["version_id"] == old["id"] and readonly["can_continue"] is False
    continued = service.open_project(project["id"], source_version_id=old["id"])
    assert continued["version_id"] == newer["id"] and continued["can_continue"] is True
    assert continued["source_version_id"] == old["id"]
    old_row = next(row for row in service.history()["items"] if row["task_id"] == task["id"])
    assert old_row["can_continue"] is False and old_row["latest_version_id"] == newer["id"]


def test_history_all_projects_open_and_continue_with_manifest_and_api(store, tmp_path):
    project = store.create_project("旧项目")
    old = store.create_version(project["id"], "A")
    unit = sheet(store, tmp_path, old, "T1", b"old")
    problem = issue(store, old, unit, unit)
    service = RevisionService(store)
    other = store.create_project("新项目")
    store.create_version(other["id"], "A")
    manifest = store.root / f"projects/{project['id']}/cache/import-{old['id']}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"unreadable": [{"name": "损坏.pdf", "reason": "读不了"}]}))
    app = FastAPI()
    app.include_router(create_router(lambda: Repository(store.root)))
    with TestClient(app) as client:
        history = client.get("/api/history").json()
        assert len(history["items"]) == 2
        opened = client.post("/api/history/open", json={"project_id": project["id"]}).json()
        assert opened["latest_version_id"] == old["id"]
        assert opened["can_continue"] and opened["drawings"][0]["unit_id"] == unit["id"]
        assert opened["unreadable"][0]["name"] == "损坏.pdf"
        assert client.post("/api/history/open", json=[]).status_code == 422
    assert store.update_issue(problem["id"], review_status="已确认")["review_status"] == "已确认"
    assert store.create_task(old["id"])["version_id"] == old["id"]


def test_corrupt_manifest_keeps_history_accessible_with_explicit_uncovered_scope(store, tmp_path):
    project = store.create_project("历史工程")
    version = store.create_version(project["id"], "A")
    sheet(store, tmp_path, version, "T1", b"test")
    path = store.root / f"projects/{project['id']}/cache/import-{version['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{invalid")
    opened = RevisionService(store).open_project(project["id"])
    assert opened["can_continue"] and opened["unit_count"] == 1
    assert opened["unreadable"][0]["code"] == "IMPORT_MANIFEST_UNREADABLE"
