"""Deleting a diagnostic must not make retained, partially converted drawings complete."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.drawings import DrawingRemovalService, RemovalError
from backend.drawings.router import create_router
from backend.store import Repository


RISK = "INSERT B6880 当前转换链未恢复几何；字体已替代，字宽不同；44条文字在图框外"


@pytest.fixture
def drawings(tmp_path):
    with Repository(tmp_path / "data") as store:
        project = store.create_project("转换风险保留")
        version = store.create_version(project["id"], "第一版")
        source = tmp_path / "部分转换.dwg"
        source.write_bytes(b"AC1027-synthetic-only")
        original = store.import_original(version["id"], source)["record"]
        units = [store.create_unit(version["id"], original["id"], kind="dwg_frame", ordinal=i,
                    name=f"已生成PNG的图框{i}", drawing_number=f"D-{i}", metadata={"extract_status": "ok"})
                 for i in (1, 2)]
        other_source = tmp_path / "其他图.pdf"
        other_source.write_bytes(b"%PDF-synthetic-only")
        other = store.import_original(version["id"], other_source)["record"]
        other_unit = store.create_unit(version["id"], other["id"], kind="pdf_page", ordinal=1, name="其他图")
        path = store.root / f"projects/{project['id']}/cache/import-{version['id']}.json"
        manifest = {"project_id": project["id"], "version_id": version["id"],
            "files": [{"name": source.name, "original_id": original["id"], "status": "partial", "unit_count": 2}],
            "drawings": [{"unit_id": unit["id"]} for unit in [*units, other_unit]],
            "unreadable": [{"name": source.name, "original_id": original["id"], "code": "DWG_INCOMPLETE", "reason": RISK},
                           {"name": "非图纸.txt", "code": "UNSUPPORTED_FORMAT", "reason": "不支持的格式"}]}
        path.write_text(json.dumps(manifest, ensure_ascii=False))
        yield locals()


@pytest.mark.parametrize("remove_one_unit", [False, True])
def test_diagnostic_delete_rejected_while_any_same_source_drawing_remains(drawings, remove_one_unit):
    d = drawings; store = d["store"]
    before = d["path"].read_bytes()
    hashes = store.verify_originals(d["version"]["id"])
    with pytest.raises(RemovalError) as caught:
        DrawingRemovalService(store).remove(d["version"]["id"], unreadable_indices=[0],
            unit_ids=[d["units"][0]["id"]] if remove_one_unit else [])
    assert caught.value.code == "REMOVE_DIAGNOSTIC_HAS_DRAWINGS"
    assert "移除整个源文件" in str(caught.value)
    assert d["path"].read_bytes() == before
    assert len(store.list_records("drawing_versions", project_id=d["project"]["id"])) == 1
    assert len(store.list_records("drawing_units", version_id=d["version"]["id"])) == 3
    assert store.verify_originals(d["version"]["id"]) == hashes


def test_api_diagnostic_delete_returns_explicit_conflict(drawings):
    d = drawings
    app = FastAPI()
    app.include_router(create_router(lambda: Repository(d["store"].root)))
    with TestClient(app) as client:
        result = client.post(f"/api/versions/{d['version']['id']}/remove-drawings", json={"unreadable_indices": [0]})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "REMOVE_DIAGNOSTIC_HAS_DRAWINGS"
    assert "移除整个源文件" in result.json()["error"]["message"]
    assert len(d["store"].list_records("drawing_versions", project_id=d["project"]["id"])) == 1


@pytest.mark.parametrize("code", ["DWG_INCOMPLETE", "PDF_INCOMPLETE"])
def test_single_drawing_removal_retains_source_wide_risk_and_partial_status(drawings, code):
    d = drawings; store = d["store"]
    # Even if a source-wide warning carries a locator, deleting that locator does
    # not establish that the remaining drawings recovered the missing geometry.
    manifest = json.loads(d["path"].read_text())
    manifest["unreadable"][0].update(code=code, page=1, unit_id=d["units"][0]["id"])
    d["path"].write_text(json.dumps(manifest, ensure_ascii=False))
    before = d["path"].read_bytes()
    result = DrawingRemovalService(store).remove(d["version"]["id"], unit_ids=[d["units"][0]["id"]])
    assert result["unit_count"] == 2
    risk = next(row for row in result["unreadable"] if row["code"] == code)
    assert risk["reason"] == RISK
    file = next(row for row in result["files"] if row["name"] == d["source"].name)
    assert file["status"] == "partial" and file["unit_count"] == 1
    assert risk["original_id"] == file["original_id"]
    assert d["path"].read_bytes() == before
    assert len(store.list_records("drawing_units", version_id=d["version"]["id"])) == 3


def test_whole_source_removal_can_remove_its_risk_and_preserves_history(drawings):
    d = drawings; store = d["store"]
    before = d["path"].read_bytes()
    hashes = store.verify_originals(d["version"]["id"])
    result = DrawingRemovalService(store).remove(d["version"]["id"],
        original_ids=[d["original"]["id"]], unreadable_indices=[0])
    assert result["removed_count"] == 2 and result["unit_count"] == 1
    assert result["removed_unreadable_count"] == 1
    assert [row["code"] for row in result["unreadable"]] == ["UNSUPPORTED_FORMAT"]
    assert all(row["name"] != d["source"].name for row in result["files"])
    assert d["path"].read_bytes() == before
    assert store.verify_originals(d["version"]["id"]) == hashes


def test_old_same_project_original_reference_cannot_bypass_guard(drawings):
    d = drawings; store = d["store"]
    adjusted = DrawingRemovalService(store).remove(d["version"]["id"], unit_ids=[d["other_unit"]["id"]])
    path = store.root / f"projects/{d['project']['id']}/cache/import-{adjusted['version_id']}.json"
    manifest = json.loads(path.read_text())
    manifest["unreadable"][0]["original_id"] = d["original"]["id"]
    path.write_text(json.dumps(manifest, ensure_ascii=False))
    with pytest.raises(RemovalError) as caught:
        DrawingRemovalService(store).remove(adjusted["version_id"], unreadable_indices=[0])
    assert caught.value.code == "REMOVE_DIAGNOSTIC_HAS_DRAWINGS"
    assert len(store.list_records("drawing_versions", project_id=d["project"]["id"])) == 2


@pytest.mark.parametrize("remove_one_unit", [False, True])
def test_unit_linked_diagnostic_cannot_be_deleted_while_same_source_units_remain(drawings, remove_one_unit):
    d = drawings
    manifest = json.loads(d["path"].read_text())
    manifest["unreadable"][0].pop("original_id")
    manifest["unreadable"][0]["unit_id"] = d["units"][0]["id"]
    d["path"].write_text(json.dumps(manifest, ensure_ascii=False))
    with pytest.raises(RemovalError) as caught:
        DrawingRemovalService(d["store"]).remove(d["version"]["id"], unreadable_indices=[0],
            unit_ids=[d["units"][0]["id"]] if remove_one_unit else [])
    assert caught.value.code == "REMOVE_DIAGNOSTIC_HAS_DRAWINGS"


def test_unrelated_unreadable_input_can_still_be_removed(drawings):
    d = drawings
    result = DrawingRemovalService(d["store"]).remove(d["version"]["id"], unreadable_indices=[1])
    assert result["unit_count"] == 3 and result["removed_unreadable_count"] == 1
    assert result["unreadable"][0]["reason"] == RISK
    assert next(row for row in result["files"] if row["name"] == d["source"].name)["status"] == "partial"
