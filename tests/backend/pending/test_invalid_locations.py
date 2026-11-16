"""Unlocatable machine questions are coverage gaps, never executable confirmations."""
import json
import math

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader

from backend.app import create_app
from backend.api.review_status import list_issues
from backend.export.service import ExportService
from backend.jobs import JobService
from backend.pending import confirm_pending_item, list_pending
from backend.pending.current import current_incomplete, current_pending
from backend.pending.service import serialize_pending
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.store import BBox, Evidence, Repository, Source, StoreError
from tests.backend.rules.test_reference_reliability import seed, unit


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CredentialService, "_detect_backend", lambda self: MemoryCredentialBackend())
    with Repository() as repo:
        vid, oid = seed(repo, tmp_path)
        first = unit(repo, vid, oid, "S-001", 1)
        second = unit(repo, vid, oid, "S-002", 2)
        yield repo, vid, first, second


def pending(repo, vid, drawing, text="施工图", *, bbox=None, status="待确认", reason="看不清", source=Source.VISION):
    row = repo.create_pending(vid, Evidence(drawing["id"], source, text, bbox or BBox(.1, .2, .3, .4)), reason)
    if status != "待确认":
        row = repo.confirm_pending(row["id"], status=status, confirmed_text="人工旧值" if status == "已确认" else None)
    return row


@pytest.mark.parametrize("changes", [
    {"x0": 1, "x1": 1}, {"y0": .8, "y1": .8}, {"x0": -.1}, {"x1": 1.1},
    {"x0": .5, "x1": .2}, {"y0": .7, "y1": .4}, {"x0": math.inf},
    {"y1": -math.inf}, {"x1": math.nan}, {"y0": None}, {"x0": "bad"},
    {"unit_id": "missing-unit"},
])
def test_invalid_raw_location_is_flagged_not_actionable_and_never_loads_facts(case, monkeypatch, changes):
    repo, vid, first, _ = case
    original = pending(repo, vid, first)
    raw = {**original, **changes}
    read = repo.list_records

    def records(table, **kwargs):
        assert table != "facts", "Location checks must stay on the cheap path without loading facts"
        return [raw] if table == "pending_confirmations" else read(table, **kwargs)

    monkeypatch.setattr(repo, "list_records", records)
    serial = serialize_pending(raw, {first["id"]: first})
    assert serial["bbox_out_of_range"]
    json.dumps(serial, allow_nan=False)
    assert current_pending(repo, vid) == []
    gaps = current_incomplete(repo, vid, [])
    assert len(gaps) == 1 and gaps[0]["pending_ids"] == [raw["id"]]
    assert gaps[0]["original_texts"] == ["施工图"]
    assert current_incomplete(repo, vid, gaps) == gaps
    get = repo.get
    monkeypatch.setattr(repo, "get", lambda table, key: raw if table == "pending_confirmations" else get(table, key))
    monkeypatch.setattr(repo, "confirm_pending", lambda *args, **kwargs: pytest.fail("Invalid machine row was written"))
    with pytest.raises(StoreError, match="无有效识读位置"):
        confirm_pending_item(repo, raw["id"], {"status": "已确认", "confirmed_text": "2400"})


def test_group_by_page_preserve_valid_questions_human_history_and_raw_rows(case):
    repo, vid, first, second = case
    bad = [pending(repo, vid, first, text, bbox=BBox(1, .82, 1, .95))
           for text in ("施工图", "某公司", "2?00", "旧原文")]
    bad.append(pending(repo, vid, second, "CAM?", bbox=BBox(.2, .3, .4, .3)))
    good = [pending(repo, vid, first, text, reason=reason)
            for text, reason in (("施工图", "看不清"), ("某公司", "两模型读数不一致"))]
    history = [pending(repo, vid, first, status=status, bbox=BBox(1, .82, 1, .95))
               for status in ("已确认", "无法确定")]
    before = repo.list_records("pending_confirmations", version_id=vid)
    for _ in range(2):
        result = list_pending(repo, vid)
        assert result["pending_count"] == 2
        assert result["incomplete_pending_count"] == 3  # two open + one unsure; page gaps are machine coverage
        assert result["location_gap_count"] == 2
        assert {row["id"] for row in result["items"]} == {row["id"] for row in good + history}
        assert len(result["incomplete_items"]) == 2
        assert sum(len(row["pending_ids"]) for row in result["incomplete_items"]) == len(bad)
        assert repo.list_records("pending_confirmations", version_id=vid) == before


def test_invalid_location_does_not_revive_retired_rule_questions(case):
    from backend.linking.service import SAME_NAME_PENDING_REASON
    from backend.references import AMBIGUOUS_REFERENCE_REASON
    from backend.rules.dimension import NO_UNIT_REASON
    from backend.rules.rules_b import INVALID_NUMBER_REASON, UNPAIRED_REASON

    repo, vid, first, _ = case
    for text, reason in (("A", SAME_NAME_PENDING_REASON),
                         ("材料详见D0501卷册", AMBIGUOUS_REFERENCE_REASON),
                         ("DXN-35Q", NO_UNIT_REASON),
                         ("ZC-YJV-26/35kV-3x70", UNPAIRED_REASON),
                         ("0", INVALID_NUMBER_REASON)):
        ev = Evidence(first["id"], Source.CAD_TEXT, text, BBox(1, .82, 1, .95))
        repo.add_fact(vid, ev)
        repo.create_pending(vid, ev, reason)
    before = repo.list_records("pending_confirmations", version_id=vid)
    assert current_pending(repo, vid) == []
    assert current_incomplete(repo, vid, []) == []
    bundle = list_pending(repo, vid)
    assert bundle["pending_count"] == bundle["incomplete_pending_count"] == 0
    assert bundle["incomplete_items"] == []
    assert repo.list_records("pending_confirmations", version_id=vid) == before


def test_current_rule_question_with_real_trigger_keeps_location_gap(case):
    from backend.references import AMBIGUOUS_REFERENCE_REASON
    from tests.backend.rules.test_reference_reliability import add

    repo, vid, first, _ = case
    fact = add(repo, vid, first, "详见04图", category="详图索引断链")
    repo._update("facts", fact["id"], {"x0": 1, "x1": 1})
    raw = pending(repo, vid, first, "详见04图", bbox=BBox(1, .1, 1, .11), reason=AMBIGUOUS_REFERENCE_REASON)
    result = list_pending(repo, vid)
    assert result["pending_count"] == result["incomplete_pending_count"] == 0
    assert result["location_gap_count"] == 1
    assert result["incomplete_items"][0]["pending_ids"] == [raw["id"]]


@pytest.mark.parametrize("status", ["已确认", "无法确定"])
def test_old_open_id_cannot_write_a_decision_or_fact_for_unlocatable_machine_row(case, status):
    repo, vid, first, _ = case
    raw = pending(repo, vid, first, bbox=BBox(1, .82, 1, .95))
    with pytest.raises(StoreError, match="无有效识读位置"):
        confirm_pending_item(repo, raw["id"], {"status": status, "confirmed_text": "2400"})
    assert repo.get("pending_confirmations", raw["id"]) == raw
    assert not repo.list_records("facts", version_id=vid)
    assert not repo.list_records("model_calls", version_id=vid)


@pytest.mark.parametrize("status", ["已确认", "无法确定"])
def test_invalid_processed_human_history_can_still_be_reedited(case, monkeypatch, status):
    repo, vid, first, _ = case
    raw = pending(repo, vid, first, status=status, bbox=BBox(1, .82, 1, .95))
    monkeypatch.setattr("backend.pending.service.recalculate_affected", lambda *args: {"skipped": True})
    result = confirm_pending_item(repo, raw["id"], {"status": "已确认", "confirmed_text": "人工再核实"})
    assert result["item"]["status"] == "已确认"
    assert result["item"]["bbox_out_of_range"]
    assert result["model_calls_delta"] == 0
    assert repo.get("facts", result["fact_id"])["source"] == Source.USER.value


def test_valid_edge_box_is_actionable_regardless_of_text(case, monkeypatch):
    repo, vid, first, _ = case
    raw = pending(repo, vid, first, "施工图", bbox=BBox(0, 0, 1, 1))
    monkeypatch.setattr("backend.pending.service.recalculate_affected", lambda *args: {"skipped": True})
    assert not list_pending(repo, vid)["items"][0]["bbox_out_of_range"]
    result = confirm_pending_item(repo, raw["id"], {"status": "已确认", "confirmed_text": "保留正常人工确认"})
    assert result["pending_count"] == 0 and result["fact_id"]


def test_user_sourced_open_row_is_not_reclassified_as_machine_coverage(case):
    repo, vid, first, _ = case
    raw = pending(repo, vid, first, source=Source.USER, bbox=BBox(1, .82, 1, .95))
    bundle = list_pending(repo, vid)
    assert [row["id"] for row in bundle["items"]] == [raw["id"]]
    assert bundle["pending_count"] == 1 and bundle["incomplete_items"] == []


def test_results_job_api_and_exports_keep_the_same_page_gap_and_original_text(case, tmp_path):
    repo, vid, first, _ = case
    bad = [pending(repo, vid, first, text, bbox=BBox(1, .82, 1, .95))
           for text in ("施工图", "某公司", "2?00", "旧原文")]
    task = repo.create_task(vid)
    repo.update_task(task["id"], status="已完成", checkpoints={}, incomplete=[], estimated_cost_micros=0)
    before = repo.list_records("pending_confirmations", version_id=vid)
    for _ in range(2):
        progress = JobService(repo).get_progress(task["id"])
        result = list_issues(repo, vid)
        confirmations = list_pending(repo, vid)
        assert confirmations["pending_count"] == confirmations["incomplete_pending_count"] == 0
        assert confirmations["location_gap_count"] == 1
        assert progress["outcome"] == result["outcome"] == "partial"
        assert progress["incomplete_count"] == result["incomplete_count"] == 1
        assert result["pending_count"] == 0
        assert len(result["incomplete_items"]) == 1
        gap = result["incomplete_items"][0]
        assert set(gap["pending_ids"]) == {row["id"] for row in bad}
        assert "无有效识读位置" in gap["reason"] and "重新识别" in gap["reason"]
        assert not gap["retryable"]
    with TestClient(create_app("test-location-token")) as client:
        client.headers["Authorization"] = "Bearer test-location-token"
        api = client.get("/api/issues", params={"version_id": vid}).json()
        assert api["incomplete_count"] == 1 and api["pending_count"] == 0
        assert api["outcome"] == "partial" and len(api["incomplete_items"]) == 1
        assert client.get("/api/pending", params={"version_id": vid}).json()["pending_count"] == 0
        rejected = client.patch(f"/api/pending/{bad[0]['id']}", json={"status": "已确认", "confirmed_text": "2400"})
        assert rejected.status_code == 422 and "无有效识读位置" in rejected.json()["error"]["message"]
    service = ExportService(repo, output_root=tmp_path / "exports")
    exported = service.generate(vid)
    assert exported["pending_count"] == 0
    book = load_workbook(service.resolve(exported["files"][1]["id"]))
    gaps = [row for row in book["未完成与读不了"].values if "无有效识读位置" in str(row[1])]
    assert len(gaps) == 1 and gaps[0][0] == "S-001"
    pdf = "".join(page.extract_text() for page in PdfReader(service.resolve(exported["files"][0]["id"])).pages)
    assert "无有效识读位置" in pdf and "需补定位或重新识别" in pdf
    for row in bad:
        assert row["text"] in gaps[0][1] and row["text"] in pdf
    assert repo.list_records("pending_confirmations", version_id=vid) == before
    assert not repo.list_records("facts", version_id=vid)
    assert not repo.list_records("model_calls", version_id=vid)


def test_location_gap_pdf_wraps_and_paginates_without_losing_original_text(case, tmp_path):
    repo, vid, first, _ = case
    for index in range(30):
        pending(repo, vid, first, f"标记{index:02d}：" + "需补定位的识读原文" * 10, bbox=BBox(1, .82, 1, .95))
    task = repo.create_task(vid)
    repo.update_task(task["id"], status="已完成", checkpoints={}, incomplete=[], estimated_cost_micros=0)
    service = ExportService(repo, output_root=tmp_path / "exports")
    exported = service.generate(vid)
    pages = PdfReader(service.resolve(exported["files"][0]["id"])).pages
    assert len(pages) > 1
    text = "".join(page.extract_text() for page in pages)
    # PDF line wrapping may split a marker; check the complete logical source text.
    compact = "".join(text.split())
    for index in range(30):
        assert f"标记{index:02d}：" + "需补定位的识读原文" * 10 in compact
    for page in pages:
        positions = []
        page.extract_text(visitor_text=lambda text, cm, tm, font, size: positions.append(tm[5]) if text.strip() else None)
        assert all(60 <= y <= 790 for y in positions)
