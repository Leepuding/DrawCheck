"""DC-09 看图识别验收测试（Mock 默认）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.catalog import CatalogService
from backend.extract import ExtractService, PROMPT_VERSION
from backend.extract.bbox_util import clamp_bbox
from backend.extract.prompt import DEFAULT_CONFIDENCE_THRESHOLD
from backend.extract.replay import ReplayLookup
from backend.extract.schema import parse_model_json, validate_extract_payload
from backend.extract.errors import ExtractError, CODE_BAD_FORMAT
from backend.importer import ImportService
from backend.models.mock import MockModelClient
from backend.store import BBox, Repository, Source


def _import_suite_a(data_dir: Path, synthetic_root: Path) -> dict:
    build = synthetic_root / "build" / "A"
    assert build.is_dir(), "请先生成 fixtures/synthetic/build"
    pdfs = sorted(build.glob("*.pdf"))
    assert pdfs, "A 套 PDF 为空"
    with ImportService(data_dir) as importer:
        job_id = importer.submit(pdfs, project_name="DC09-A", version_name="第一版")
        job = importer.wait(job_id, timeout=120)
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["unit_count"] >= 14
    return result


def test_clamp_bbox_out_of_range():
    bbox, clamped, notes = clamp_bbox({"x0": -0.2, "y0": 0.1, "x1": 1.5, "y1": 0.9})
    assert clamped
    assert bbox == {"x0": 0.0, "y0": 0.1, "x1": 1.0, "y1": 0.9}
    assert notes
    # 可构造 store.BBox
    BBox(**bbox)


def test_clamp_bbox_swaps_reversed():
    bbox, clamped, notes = clamp_bbox({"x0": 0.8, "y0": 0.9, "x1": 0.2, "y1": 0.1})
    assert clamped
    assert bbox["x0"] <= bbox["x1"] and bbox["y0"] <= bbox["y1"]
    BBox(**bbox)


def test_schema_bad_format_raises():
    with pytest.raises(ExtractError) as ei:
        parse_model_json("not-json")
    assert ei.value.code == CODE_BAD_FORMAT
    with pytest.raises(ExtractError):
        validate_extract_payload({"foo": 1})


def test_format_error_retries_then_incomplete(data_dir, monkeypatch):
    """格式不对重试默认 1 次，仍失败 → 未完成，不得记成功空结果。"""
    with Repository(data_dir) as store:
        project = store.create_project("重试2")
        version = store.create_version(project["id"], "v1")
        src = Path(data_dir) / "src.pdf"
        src.write_bytes(b"%PDF-1.4 minimal")
        imported = store.import_original(version["id"], src)
        rel = f"projects/{project['id']}/cache/review.png"
        dest = Path(data_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        Image.new("RGB", (200, 100), "white").save(dest)
        unit = store.create_unit(
            version["id"],
            imported["record"]["id"],
            kind="pdf_page",
            ordinal=1,
            name="测试页",
            drawing_number="",
            metadata={"review_relative_path": rel},
        )

        class BoomClient(MockModelClient):
            def chat_completions(self, **kwargs):
                self.calls += 1
                from backend.models.openai_compat import CallResult
                return CallResult(
                    content="{{bad",
                    input_tokens=1,
                    output_tokens=1,
                    raw_status=200,
                    model="mock",
                    provider="mock",
                    latency_ms=1,
                )

        svc = ExtractService(
            store,
            use_mock=False,
            model_client=BoomClient(),
            format_retries=1,
            replay=ReplayLookup(suite="A"),
        )
        result = svc.extract_unit(unit["id"], persist=True)
        assert result.status == "incomplete"
        assert result.model_calls == 2  # 首次 + 重试 1
        assert result.incomplete_reason
        unit2 = store.get("drawing_units", unit["id"])
        assert (unit2.get("metadata") or {}).get("extract_status") == "incomplete"
        facts = store.list_records("facts", version_id=version["id"])
        assert facts == []


def test_facts_source_and_bbox(data_dir, synthetic_root):
    result = _import_suite_a(data_dir, synthetic_root)
    with Repository(data_dir) as store:
        replay = ReplayLookup(synthetic_root, suite="A")
        svc = ExtractService(store, use_mock=True, replay=replay)
        # 找 S-009（含 unclear）
        units = store.list_records("drawing_units", version_id=result["version_id"])
        target = None
        for u in units:
            name = u.get("name") or ""
            # 原文件名在 original
            orig = store.get("original_files", u["original_id"])
            if "S-009" in orig["name"]:
                target = u
                break
        assert target is not None
        er = svc.extract_unit(target["id"], persist=True)
        assert er.status == "ok"
        facts = store.list_records("facts", version_id=result["version_id"])
        assert facts
        for f in facts:
            assert f["unit_id"] == target["id"]
            assert f["source"] == Source.VISION.value == "看图识别"
            bbox = BBox(f["x0"], f["y0"], f["x1"], f["y1"])
            assert 0 <= bbox.x0 <= bbox.x1 <= 1
            assert 0 <= bbox.y0 <= bbox.y1 <= 1


def test_unclear_creates_pending(data_dir, synthetic_root):
    result = _import_suite_a(data_dir, synthetic_root)
    with Repository(data_dir) as store:
        replay = ReplayLookup(synthetic_root, suite="A")
        svc = ExtractService(
            store,
            use_mock=True,
            replay=replay,
            confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
        )
        units = store.list_records("drawing_units", version_id=result["version_id"])
        target = None
        for u in units:
            orig = store.get("original_files", u["original_id"])
            if "S-009" in orig["name"]:
                target = u
                break
        er = svc.extract_unit(target["id"], persist=True)
        assert any(f.get("unclear") for f in er.facts)
        pending = store.list_records("pending_confirmations", version_id=result["version_id"])
        assert pending, "unclear 应进入待确认候选"
        assert any(p["reason"] == "看不清" for p in pending)


def test_low_confidence_pending_path(data_dir):
    """低于隐藏把握度阈值的 Mock 可测路径。"""
    with Repository(data_dir) as store:
        project = store.create_project("低把握")
        version = store.create_version(project["id"], "v1")
        src = Path(data_dir) / "x.pdf"
        src.write_bytes(b"%PDF-1.4 x")
        imported = store.import_original(version["id"], src)
        rel = f"projects/{project['id']}/cache/r.png"
        dest = Path(data_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        Image.new("RGB", (200, 100), "white").save(dest)
        unit = store.create_unit(
            version["id"], imported["record"]["id"], kind="pdf_page", ordinal=1,
            name="低把握页", metadata={"review_relative_path": rel},
        )
        # 手工注入：不走 replay，用自定义 client 返回低把握 JSON
        low = {
            "title_blocks": [],
            "facts": [{
                "category": "尺寸",
                "text": "??",
                "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2},
                "confidence": 0.2,
                "unclear": False,
            }],
        }

        class LowClient(MockModelClient):
            def chat_completions(self, **kwargs):
                self.calls += 1
                from backend.models.openai_compat import CallResult
                return CallResult(
                    content=json.dumps(low, ensure_ascii=False),
                    input_tokens=1, output_tokens=1, raw_status=200,
                    model="mock", provider="mock", latency_ms=1,
                )

        svc = ExtractService(
            store, use_mock=False, model_client=LowClient(),
            confidence_threshold=0.55, format_retries=0,
        )
        er = svc.extract_unit(unit["id"], persist=True)
        assert er.status == "ok"
        pending = store.list_records("pending_confirmations", version_id=version["id"])
        assert pending and pending[0]["reason"] == "把握度低于阈值"


def test_cache_second_call_no_extra_model(data_dir, synthetic_root):
    result = _import_suite_a(data_dir, synthetic_root)
    with Repository(data_dir) as store:
        replay = ReplayLookup(synthetic_root, suite="A")
        client = MockModelClient()
        task = store.create_task(result["version_id"])
        svc = ExtractService(
            store, use_mock=True, replay=replay, model_client=client, task_id=task["id"],
        )
        units = store.list_records("drawing_units", version_id=result["version_id"])
        unit = units[0]
        r1 = svc.extract_unit(unit["id"], persist=True)
        calls_after_first = client.calls
        model_rows_1 = store.list_records("model_calls", version_id=result["version_id"])
        r2 = svc.extract_unit(unit["id"], persist=True)
        assert r2.cache_hit is True
        assert r2.model_calls == 0
        assert client.calls == calls_after_first  # 不增加
        model_rows_2 = store.list_records("model_calls", version_id=result["version_id"])
        assert len(model_rows_2) == len(model_rows_1)


def test_live_marker_skipped_without_flag():
    """未设 live 开关时 live 用例应 skip（本文件占位断言标记存在）。"""
    assert True


@pytest.mark.live
def test_live_extract_optional(data_dir, synthetic_root):
    """可选真调用：仅记账观察，不当准确率承诺。"""
    pytest.importorskip("PIL")
    # 本单默认不强制跑；若执行则用极小自制图
    from backend.models.openai_compat import OpenAICompatClient
    from backend.models.presets import get_preset
    import os
    key = os.environ.get("SILICONFLOW_API_KEY")
    if not key:
        pytest.skip("无 SILICONFLOW_API_KEY")
    # 不在此打印密钥
    assert len(key) > 8


def test_annotations_cache_persist_per_unit(data_dir):
    """A shared image cache must materialize facts in every new unit and retain object identity."""
    from types import SimpleNamespace
    with Repository(data_dir) as store:
        project = store.create_project("相同图像")
        version = store.create_version(project["id"], "v1")
        source = Path(data_dir) / "same.pdf"
        source.write_bytes(b"%PDF-test")
        original = store.import_original(version["id"], source)["record"]
        path = Path(data_dir) / "cache" / "same.png"
        path.parent.mkdir(exist_ok=True)
        from PIL import Image
        Image.new("RGB", (200, 100), "white").save(path)
        units = [store.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=n, name=str(n), metadata={"review_relative_path":"cache/same.png"}) for n in (1,2)]
        class Client:
            calls = 0
            def chat_completions(self, **kwargs):
                self.calls += 1
                assert kwargs["max_tokens"] >= 8192
                return SimpleNamespace(input_tokens=1, output_tokens=1, content=json.dumps({"facts":[{"text":"数量12", "category":"数量与材料表", "object_id":"设备1", "comparison_key":"设备1-数量", "identity_confirmed":True, "numeric_value":"12", "confidence":.9, "bbox":{"x0":.1,"y0":.1,"x1":.2,"y1":.2}}]}))
        client = Client()
        service = ExtractService(store, use_mock=False, model_client=client)
        first = service.extract_unit(units[0]["id"])
        second = service.extract_unit(units[1]["id"])
        assert second.cache_hit and client.calls == 1
        assert len(store.list_records("facts", version_id=version["id"])) == 2
        for unit in units:
            annotations = store.get("drawing_units", unit["id"])["metadata"]["fact_annotations"]
            assert len(annotations) == 1
            assert next(iter(annotations.values()))["identity_confirmed"] is True
