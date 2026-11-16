import errno
import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.drawings import DrawingRemovalService, RemovalError
from backend.drawings.router import create_router
from backend.importer import ImportService
from backend.parsers.base import ParseResult, ParsedUnit
from backend.revisions import RevisionService
from backend.store import Repository, Evidence, Source, BBox, StoreError


@pytest.fixture
def repo(tmp_path):
    with Repository(tmp_path / "data") as store:
        yield store


def ev(unit, text="200", source=Source.CAD_TEXT):
    return Evidence(unit["id"], source, text, BBox(.1, .2, .3, .4))


@pytest.fixture
def imported(repo, tmp_path):
    project = repo.create_project("误导入处理")
    version = repo.create_version(project["id"], "第一版")
    source = tmp_path / "多页.pdf"
    source.write_bytes(b"%PDF-1.7\nlocal test fixture")
    original = repo.import_original(version["id"], source)["record"]
    first = repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=1, name="第一页", drawing_number="T1")
    second = repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=2, name="第二页", drawing_number="T2")
    cad_source = tmp_path / "图框.dwg"
    cad_source.write_bytes(b"AC1027cad")
    cad = repo.import_original(version["id"], cad_source)["record"]
    third = repo.create_unit(version["id"], cad["id"], kind="dwg_frame", ordinal=1, name="CAD图框", drawing_number="T3")
    bad_source = tmp_path / "读不了.pdf"
    bad_source.write_bytes(b"%PDF-bad")
    bad = repo.import_original(version["id"], bad_source)["record"]
    fact = repo.add_fact(version["id"], ev(second), numeric_value="200", unit="mm")
    visual = repo.add_fact(version["id"], ev(second, "2?0", Source.VISION), unit="mm")
    pending = repo.create_pending(version["id"], ev(second, "2?0", Source.VISION), "看不清")
    repo.confirm_pending(pending["id"], status="已确认", confirmed_text="240")
    user = repo.add_fact(version["id"], ev(second, "240", Source.USER), unit="mm")
    repo._update("drawing_units", second["id"], {"metadata_json": json.dumps({
        "extract_status": "ok", "extract_prompt_version": "test-v1", "extract_image_sha256": "a"*64,
        "fact_annotations": {visual["id"]: {"category": "尺寸", "identity_confirmed": True, "object_id": "B1"}},
        "audit_fact_id": user["id"], "pending_ids": [pending["id"]]})})
    repo.create_pending(version["id"], ev(first, "删除页"), "看不清")
    problem = repo.create_issue(version["id"], "旧问题", ev(second), ev(third, "210"), rule_result={"category": "尺寸"})
    repo.update_issue(problem["id"], review_status="已确认", review_note="原报告结论")
    task = repo.create_task(version["id"])
    repo.update_task(task["id"], status="已完成", checkpoints={}, incomplete=[], estimated_cost_micros=0)
    report_relative = f"projects/{project['id']}/exports/原报告.pdf"
    report = repo.root / report_relative
    report.write_bytes(b"unchanged-old-report")
    export = repo.record_export(task["id"], format="pdf", relative_path=report_relative)
    manifest_path = repo.root / f"projects/{project['id']}/cache/import-{version['id']}.json"
    manifest = {"project_id": project["id"], "version_id": version["id"],
        "files": [{"name": original["name"], "original_id": original["id"], "unit_count": 2}],
        "drawings": [{"unit_id": u["id"]} for u in (first, second, third)],
        "unreadable": [{"name": bad["name"], "original_id": bad["id"], "reason": "损坏，读不了", "code": "PDF_CORRUPT"},
                       {"name": "不支持.docx", "reason": "不支持的格式", "code": "UNSUPPORTED_FORMAT"}]}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    return locals()


def test_single_page_removal_preserves_source_history_report_and_confirmations(repo, imported):
    data = imported
    old_id = data["version"]["id"]
    old_hashes = repo.verify_originals(old_id)
    old_manifest = data["manifest_path"].read_bytes()
    old_issue = repo.get("issues", data["problem"]["id"])
    result = DrawingRemovalService(repo).remove(old_id, unit_ids=[data["first"]["id"]])
    new_id = result["version_id"]
    assert new_id != old_id and result["previous_version_id"] == old_id
    assert result["version_name"] == "调整图纸" and result["is_revision"] is False
    assert result["removed_count"] == 1 and result["unit_count"] == 2 and result["review_required"]
    assert result["task_id"] is None and result["can_continue"]
    assert {u["ordinal"] for u in result["drawings"] if u["kind"] == "pdf_page"} == {2}
    assert repo.list_records("issues", version_id=new_id) == []
    assert repo.list_records("review_tasks", version_id=new_id) == []
    assert repo.list_records("associations", version_id=new_id) == []
    facts = repo.list_records("facts", version_id=new_id)
    assert len(facts) == 3 and {f["source"] for f in facts} == {"CAD文字", "看图识别", "用户确认"}
    assert not {f["id"] for f in facts} & {data[key]["id"] for key in ("fact", "visual", "user")}
    second = next(u for u in result["drawings"] if u["drawing_number"] == "T2")
    assert second["metadata"]["copied_from_unit_id"] == data["second"]["id"]
    assert set(second["metadata"]["fact_annotations"]) <= {f["id"] for f in facts}
    confirmations = repo.list_records("pending_confirmations", version_id=new_id)
    assert len(confirmations) == 1 and confirmations[0]["status"] == "已确认"
    assert confirmations[0]["confirmed_text"] == "240"
    assert confirmations[0]["id"] in second["metadata"]["pending_ids"]
    assert repo.verify_originals(old_id) == old_hashes
    assert data["manifest_path"].read_bytes() == old_manifest
    assert repo.get("issues", data["problem"]["id"]) == old_issue
    assert repo.get("exports", data["export"]["id"]) == data["export"]
    assert hashlib.sha256(data["report"].read_bytes()).hexdigest() == data["export"]["sha256"]
    with pytest.raises(StoreError) as caught:
        repo.update_issue(data["problem"]["id"], review_status="疑似")
    assert caught.value.code.value == "VERSION_READONLY"


def test_file_removal_removes_its_pages_and_unreadable_file_diagnostics(repo, imported):
    data = imported
    result = DrawingRemovalService(repo).remove(data["version"]["id"],
        original_ids=[data["original"]["id"], data["bad"]["id"]])
    assert result["removed_count"] == 2 and result["removed_file_count"] == 2
    assert result["unit_count"] == 1 and result["drawings"][0]["kind"] == "dwg_frame"
    assert result["removed_unreadable_count"] == 1
    assert [row["name"] for row in result["unreadable"]] == ["不支持.docx"]
    assert result["file_count"] == 1


def test_remove_all_returns_empty_and_allows_normal_reimport(repo, imported):
    data = imported
    result = DrawingRemovalService(repo).remove(data["version"]["id"],
        original_ids=[data[key]["id"] for key in ("original", "cad", "bad")], unreadable_indices=[1])
    assert result["unit_count"] == result["file_count"] == 0
    assert result["drawings"] == result["unreadable"] == []
    assert repo.list_records("original_files", version_id=result["version_id"]) == []
    assert all(repo.original_path(data[key]["id"]).is_file() for key in ("original", "cad", "bad"))
    class Parser:
        def parse(self, request):
            return ParseResult([ParsedUnit("pdf_page", 1, "重新导入", "T1", [])], [])
    with ImportService(repo.root, pdf_parser=Parser()) as importer:
        job = importer.wait(importer.submit([data["source"]], version_id=result["version_id"]), 5)
    assert job["status"] == "completed" and job["result"]["unit_count"] == 1


def test_diagnostic_without_original_can_be_removed_by_index(repo, imported):
    result = DrawingRemovalService(repo).remove(imported["version"]["id"], unreadable_indices=[1])
    assert result["removed_count"] == 0 and result["removed_unreadable_count"] == 1
    assert len(result["drawings"]) == 3 and len(result["unreadable"]) == 1


@pytest.mark.parametrize("status", ["待开始", "进行中"])
def test_active_task_prevents_removal(repo, imported, status):
    task = repo.create_task(imported["version"]["id"])
    repo.update_task(task["id"], status=status, checkpoints={}, incomplete=[], estimated_cost_micros=0)
    with pytest.raises(RemovalError) as caught:
        DrawingRemovalService(repo).remove(imported["version"]["id"], unit_ids=[imported["first"]["id"]])
    assert caught.value.code == "REMOVE_REVIEW_ACTIVE"
    assert len(repo.list_records("drawing_versions", project_id=imported["project"]["id"])) == 1


def test_old_and_draft_versions_prevent_removal(repo, imported):
    draft = RevisionService(repo).create_revision(imported["version"]["id"])
    with pytest.raises(StoreError):
        DrawingRemovalService(repo).remove(imported["version"]["id"], unit_ids=[imported["first"]["id"]])
    with pytest.raises(RemovalError) as caught:
        DrawingRemovalService(repo).remove(draft["version_id"], unit_ids=[imported["first"]["id"]])
    assert caught.value.code == "REMOVE_DRAFT_VERSION"


@pytest.mark.parametrize("selection", [{}, {"unit_ids": ["unknown"]}, {"original_ids": ["unknown"]},
    {"unreadable_indices": [99]}, {"unreadable_indices": [True]}, {"unreadable_indices": [-1]},
    {"unit_ids": "not-a-list"}, {"original_ids": [3]}, {"unit_ids": ["same", "same"]}])
def test_invalid_selection_is_rejected_without_new_version(repo, imported, selection):
    with pytest.raises(StoreError):
        DrawingRemovalService(repo).remove(imported["version"]["id"], **selection)
    assert len(repo.list_records("drawing_versions", project_id=imported["project"]["id"])) == 1


def test_foreign_version_ids_rejected(repo, imported):
    other = repo.create_project("另一项目")
    version = repo.create_version(other["id"], "A")
    with pytest.raises(StoreError):
        DrawingRemovalService(repo).remove(version["id"], unit_ids=[imported["first"]["id"]])
    with pytest.raises(StoreError):
        DrawingRemovalService(repo).remove(version["id"], original_ids=[imported["original"]["id"]])


@pytest.mark.parametrize("after_write", [False, True])
def test_manifest_failure_rolls_back_version_and_keeps_old_writable(repo, imported, monkeypatch, after_write):
    service = DrawingRemovalService(repo)
    before = set(repo.root.rglob("import-*.json"))
    original_write = service._write_manifest
    def fail(destination, manifest):
        if after_write:
            original_write(destination, manifest)
        raise OSError(errno.ENOSPC, "模拟磁盘满")
    monkeypatch.setattr(service, "_write_manifest", fail)
    with pytest.raises(StoreError) as caught:
        service.remove(imported["version"]["id"], unit_ids=[imported["first"]["id"]])
    assert caught.value.code.value == "DISK_FULL"
    assert set(repo.root.rglob("import-*.json")) == before
    assert len(repo.list_records("drawing_versions", project_id=imported["project"]["id"])) == 1
    assert repo.create_task(imported["version"]["id"])["version_id"] == imported["version"]["id"]


def test_api_contract_and_status_codes(repo, imported):
    app = FastAPI()
    app.include_router(create_router(lambda: Repository(repo.root)))
    path = f"/api/versions/{imported['version']['id']}/remove-drawings"
    with TestClient(app) as client:
        for body in ({}, [], {"unit_ids": None}, {"unknown": []}, {"unit_ids": ["unknown"]}):
            assert client.post(path, json=body).status_code == 422
        assert client.post("/api/versions/missing/remove-drawings", json={"unit_ids": ["unknown"]}).status_code == 404
        response = client.post(path, json={"unit_ids": [imported["first"]["id"]]})
        assert response.status_code == 200, response.json()
        assert response.json()["removed_count"] == 1 and response.json()["task_id"] is None
        assert client.post(path, json={"unit_ids": [imported["second"]["id"]]}).status_code == 409


@pytest.mark.parametrize("state", ["待开始", "进行中", "draft"])
def test_api_conflicts_are_explicit_and_do_not_create_version(repo, imported, state):
    version_id = imported["version"]["id"]
    if state == "draft":
        version_id = RevisionService(repo).create_revision(version_id)["version_id"]
    else:
        task = repo.create_task(version_id)
        repo.update_task(task["id"], status=state, checkpoints={}, incomplete=[], estimated_cost_micros=0)
    expected_versions = len(repo.list_records("drawing_versions", project_id=imported["project"]["id"]))
    app = FastAPI()
    app.include_router(create_router(lambda: Repository(repo.root)))
    with TestClient(app) as client:
        response = client.post(f"/api/versions/{version_id}/remove-drawings", json={"unit_ids": [imported["first"]["id"]]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ("REMOVE_DRAFT_VERSION" if state == "draft" else "REMOVE_REVIEW_ACTIVE")
    assert len(repo.list_records("drawing_versions", project_id=imported["project"]["id"])) == expected_versions


def test_api_storage_failure_reports_507_and_preserves_source(repo, imported, monkeypatch):
    def fail(*args):
        raise OSError(errno.ENOSPC, "disk full")
    monkeypatch.setattr(DrawingRemovalService, "_write_manifest", fail)
    app = FastAPI()
    app.include_router(create_router(lambda: Repository(repo.root)))
    with TestClient(app) as client:
        response = client.post(f"/api/versions/{imported['version']['id']}/remove-drawings", json={"unit_ids": [imported["first"]["id"]]})
    assert response.status_code == 507 and response.json()["error"]["code"] == "DISK_FULL"
    assert len(repo.list_records("drawing_versions", project_id=imported["project"]["id"])) == 1
    assert repo.create_task(imported["version"]["id"])["status"] == "待开始"
