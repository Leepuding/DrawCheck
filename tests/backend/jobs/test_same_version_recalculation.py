"""Same-version reruns use each task's conditions, without mutating exported artifacts or resumes."""
import json
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfWriter

from backend.export import ExportService
from backend.jobs import JobService
from backend.settings import SettingsService
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.store import BBox, Evidence, Source
from tests.backend.jobs.conftest import configure_single_model, configure_dual_model


@pytest.fixture
def completed_case(repo, tmp_path, monkeypatch, request):
    monkeypatch.setenv("DRAWCHECK_EXPORT_DIR", str(tmp_path / "reports"))
    project = repo.create_project("容差重审")
    version = repo.create_version(project["id"], "v1")
    source = tmp_path / "two.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    writer.add_blank_page(width=300, height=200)
    writer.write(source)
    original = repo.import_original(version["id"], source)["record"]
    repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=1, name="目录", drawing_number="S-000",
        metadata={"extract_status":"ok","catalog_complete":True,"is_catalog_sheet":True,"catalog_listed_numbers":["S-001"]})
    unit = repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=2, name="基础", drawing_number="S-001")
    facts = [repo.add_fact(version["id"], Evidence(unit["id"], Source.VISION, text, BBox(x,.1,x+.1,.2)), unit="mm") for x,text in ((.1,"100mm"),(.4,"102mm"))]
    preview = repo.root / "page.png"
    Image.new("RGB", (300,200), "white").save(preview)
    repo._update("drawing_units", unit["id"], {"metadata_json":json.dumps({"extract_status":"ok", "review_relative_path":"page.png", "fact_annotations":{
        fact["id"]:{"category":"尺寸", "object_id":"F1", "comparison_key":"F1长度", "identity_confirmed":True} for fact in facts}})})
    settings = SettingsService(repo, credentials=CredentialService(MemoryCredentialBackend()))
    dual = bool(getattr(request, "param", False))
    if dual:
        configure_dual_model(settings)
    else:
        configure_single_model(settings)
    calls = []
    class ExistingFacts:
        def extract_version(self, version_id, **kwargs):
            calls.append(version_id)
            return [SimpleNamespace(unit_id=u["id"], status="ok", model_calls=0) for u in repo.list_records("drawing_units", version_id=version_id)]
    jobs = JobService(repo, settings_service=settings, extract_factory=ExistingFacts)
    if dual:
        from tests.backend.review.test_review import Client
        jobs.extras["review_client"] = Client()
    first = jobs.start_review(version["id"])
    assert first["outcome"] == "completed"
    return version["id"], first, jobs, settings, calls


def dimensions(repo, version_id):
    return [i for i in repo.list_records("issues", version_id=version_id) if i["rule_result"].get("category") == "尺寸"]


def test_new_task_reconciles_tolerance_and_preserves_old_export(repo, completed_case):
    version_id, first, jobs, settings, calls = completed_case
    assert len(dimensions(repo, version_id)) == 1
    service = ExportService(repo)
    export = service.generate(version_id)
    files = [(service.resolve(f["id"]), service.resolve(f["id"]).read_bytes()) for f in export["files"]]
    settings.save_settings({"dimension_tolerance_mm":3})
    second = jobs.start_review(version_id)
    assert second["task_id"] != first["task_id"]
    assert dimensions(repo, version_id) == []
    assert second["issue_count"] == 0
    assert all(path.read_bytes() == original for path,original in files)
    settings.save_settings({"dimension_tolerance_mm":1})
    third = jobs.start_review(version_id)
    assert third["task_id"] != second["task_id"]
    assert len(dimensions(repo, version_id)) == 1
    assert dimensions(repo, version_id)[0]["rule_result"]["tolerance_mm"] == 1
    assert len(calls) == 3
    assert repo.list_records("model_calls", version_id=version_id) == []


def test_manual_conclusion_survives_conditions_change_as_incomplete(repo, completed_case):
    version_id, first, jobs, settings, calls = completed_case
    issue = dimensions(repo, version_id)[0]
    repo.update_issue(issue["id"], review_status="已确认", review_note="现场复核原结论")
    settings.save_settings({"dimension_tolerance_mm":3})
    second = jobs.start_review(version_id)
    prior = repo.get("issues", issue["id"])
    assert prior["review_status"] == "已确认"
    assert prior["review_note"].startswith("现场复核原结论")
    assert "审查条件已修改，请重新核对" in prior["review_note"]
    assert prior["rule_result"]["review_conditions_changed_after_confirmation"]
    assert second["outcome"] == "partial" and second["incomplete_count"] >= 1
    assert not [i for i in dimensions(repo, version_id) if i["review_status"] == "疑似"]


def test_resume_preserves_completed_steps_and_does_not_reconcile_again(repo, completed_case, monkeypatch):
    version_id, first, jobs, settings, calls = completed_case
    task = repo.get("review_tasks", first["task_id"])
    checkpoints = task["checkpoints"]
    checkpoints["finalize"] = {"state":"pending"}
    repo.update_task(task["id"], status="失败", checkpoints=checkpoints, incomplete=[], estimated_cost_micros=0)
    original = dimensions(repo, version_id)[0]
    settings.save_settings({"dimension_tolerance_mm":3})
    monkeypatch.setattr("backend.rules.registry.reconcile_rule_results", lambda *args, **kwargs: pytest.fail("Resume must not reset completed local rule steps"))
    resumed = jobs.start_review(version_id)
    assert resumed["task_id"] == first["task_id"]
    assert len(calls) == 1
    assert dimensions(repo, version_id)[0]["id"] == original["id"]
    assert repo.get("review_tasks", first["task_id"])["tolerance_snapshot"]["dimension_tolerance_mm"] == 1


def test_manual_reconfirmation_clears_coverage_and_remains_acknowledged(repo, completed_case):
    from backend.rules.registry import acknowledge_manual_recheck
    version_id, first, jobs, settings, calls = completed_case
    issue = dimensions(repo, version_id)[0]
    repo.update_issue(issue["id"], review_status="已确认", review_note="原判断")
    settings.save_settings({"dimension_tolerance_mm":3})
    second = jobs.start_review(version_id)
    assert second["incomplete_count"] >= 1
    repo.update_issue(issue["id"], review_status="需补充资料", review_note="仍不确定")
    assert acknowledge_manual_recheck(repo, issue["id"])["rule_result"]["evidence_changed_after_confirmation"]
    repo.update_issue(issue["id"], review_status="排除", review_note="按当前容差重新核对")
    acknowledged = acknowledge_manual_recheck(repo, issue["id"])
    assert not acknowledged["rule_result"].get("evidence_changed_after_confirmation")
    assert not acknowledged["rule_result"].get("review_conditions_changed_after_confirmation")
    assert jobs.get_progress(second["task_id"])["incomplete_count"] == 0
    assert jobs.start_review(version_id)["incomplete_count"] == 0


@pytest.mark.parametrize("completed_case", [True], indirect=True)
def test_identical_second_review_cache_survives_rule_issue_replacement(repo, completed_case):
    version_id, first, jobs, settings, calls = completed_case
    client = jobs.extras["review_client"]
    assert len(client.calls) == 1
    first_id = dimensions(repo, version_id)[0]["id"]
    settings.save_settings({"dimension_tolerance_mm":3})
    assert jobs.start_review(version_id)["issue_count"] == 0
    assert len(client.calls) == 1
    settings.save_settings({"dimension_tolerance_mm":1})
    third = jobs.start_review(version_id)
    assert dimensions(repo, version_id)[0]["id"] != first_id
    assert len(client.calls) == 1
    assert repo.get("review_tasks", third["task_id"])["checkpoints"]["dual_review"]["review_model_calls"] == 0


def test_explicit_issue_patch_acknowledges_changed_conditions(repo, completed_case):
    from backend.api.review_status import patch_issue, serialize_issue
    version_id, first, jobs, settings, calls = completed_case
    issue = dimensions(repo, version_id)[0]
    patch_issue(repo, issue['id'], {'review_status': '确认'})
    settings.save_settings({'dimension_tolerance_mm': 3})
    second = jobs.start_review(version_id)
    units = {u['id']: u for u in repo.list_records('drawing_units', version_id=version_id)}
    before = serialize_issue(repo.get('issues', issue['id']), units)
    assert '审查条件已修改' in before['description']
    updated = patch_issue(repo, issue['id'], {'review_status': '排除', 'review_note': '按新容差重新核对'})
    assert updated['review_status'] == '排除'
    assert '请重新核对' not in updated['description']
    assert not [row for row in repo.get('review_tasks', second['task_id'])['incomplete'] if row.get('issue_id') == issue['id']]
