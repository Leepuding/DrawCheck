"""DC-09 自动目录与改名验收。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.catalog import CatalogService, MANUAL_CORRECTION_LABEL
from backend.catalog.service import MANUAL_CORRECTION_FIELD
from backend.extract import ExtractService
from backend.extract.replay import ReplayLookup
from backend.importer import ImportService
from backend.store import Repository


def _import_suite_a(data_dir: Path, synthetic_root: Path) -> dict:
    build = synthetic_root / "build" / "A"
    pdfs = sorted(build.glob("*.pdf"))
    with ImportService(data_dir) as importer:
        job_id = importer.submit(pdfs, project_name="DC09-CAT-A", version_name="第一版")
        job = importer.wait(job_id, timeout=120)
    assert job["status"] == "completed", job
    return job["result"]


def test_mock_catalog_matches_answers_a(data_dir, synthetic_root):
    """验收 1：Mock 下目录与 answers.json catalog.A 完全一致。"""
    answers = json.loads((synthetic_root / "answers.json").read_text(encoding="utf-8"))
    expected = answers["catalog"]["A"]
    result = _import_suite_a(data_dir, synthetic_root)
    with Repository(data_dir) as store:
        replay = ReplayLookup(synthetic_root, suite="A")
        extract = ExtractService(store, use_mock=True, replay=replay)
        catalog = CatalogService(store)
        rows = catalog.build_from_version(result["version_id"], extract=extract)
        got = [
            {
                "specialty": r["specialty"],
                "drawing_number": r["drawing_number"],
                "name": r["name"],
            }
            for r in rows
        ]
        exp = [
            {
                "specialty": r["specialty"],
                "drawing_number": r["drawing_number"],
                "name": r["name"],
            }
            for r in expected
        ]
        # 顺序：按 answers 图号集合比对（允许排序不同，但集合与字段须全一致）
        assert sorted(got, key=lambda x: (x["drawing_number"], x["name"])) == sorted(
            exp, key=lambda x: (x["drawing_number"], x["name"])
        ), (got, exp)
        # 多图框 / 无图签 / 范围外须存在
        numbers = {r["drawing_number"] for r in got}
        assert {"S-007A", "S-007B", "S-008", "DEC-01"} <= numbers


def test_rename_manual_correction_persists(data_dir, synthetic_root):
    """验收 5：改名后带人工修正；同 DRAWCHECK_DATA_DIR 重开仍在。"""
    result = _import_suite_a(data_dir, synthetic_root)
    with Repository(data_dir) as store:
        replay = ReplayLookup(synthetic_root, suite="A")
        extract = ExtractService(store, use_mock=True, replay=replay)
        catalog = CatalogService(store)
        catalog.build_from_version(result["version_id"], extract=extract)
        rows = catalog.list_catalog(result["version_id"])
        row = next(r for r in rows if r["drawing_number"] == "S-001")
        updated = catalog.rename(
            unit_id=row["unit_id"],
            name="总平面布置图（改）",
            drawing_number="S-001-R",
        )
        assert updated[MANUAL_CORRECTION_FIELD] is True
        assert updated["manual_correction_label"] == MANUAL_CORRECTION_LABEL
        assert updated["name"] == "总平面布置图（改）"
        assert updated["drawing_number"] == "S-001-R"
        view = catalog.get_unit_directory(row["unit_id"])
        assert view[MANUAL_CORRECTION_FIELD] is True
        assert view["name"] == "总平面布置图（改）"

    # 重启同一 data_dir
    with Repository(data_dir) as store:
        catalog = CatalogService(store)
        view = catalog.get_unit_directory(row["unit_id"])
        assert view["name"] == "总平面布置图（改）"
        assert view["drawing_number"] == "S-001-R"
        assert view[MANUAL_CORRECTION_FIELD] is True
        assert view["manual_correction_label"] == MANUAL_CORRECTION_LABEL
        rows2 = catalog.list_catalog(result["version_id"])
        hit = next(r for r in rows2 if r["unit_id"] == row["unit_id"] and r.get("is_primary", True))
        assert hit[MANUAL_CORRECTION_FIELD] is True
        assert hit["name"] == "总平面布置图（改）"


def test_empty_title_keeps_unit(data_dir):
    """模型返回空列表：保留单元，不静默丢图。"""
    with Repository(data_dir) as store:
        project = store.create_project("空图签")
        version = store.create_version(project["id"], "v1")
        src = Path(data_dir) / "e.pdf"
        src.write_bytes(b"%PDF-1.4 e")
        imported = store.import_original(version["id"], src)
        unit = store.create_unit(
            version["id"], imported["record"]["id"], kind="pdf_page", ordinal=1,
            name="某页", drawing_number="", metadata={},
        )
        from backend.extract.service import ExtractResult
        er = ExtractResult(
            unit_id=unit["id"], version_id=version["id"], status="ok",
            prompt_version="dc09-extract-v1", title_blocks=[], facts=[],
        )
        catalog = CatalogService(store)
        rows = catalog.apply_extract_result(er)
        assert len(rows) == 1
        assert rows[0]["no_title_block"] is True
        assert store.get("drawing_units", unit["id"])  # 仍在
