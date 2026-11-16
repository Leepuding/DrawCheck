"""Merger upgrades reuse raw region evidence without purchasing new inference."""
from copy import deepcopy
import json

import pytest
from PIL import Image

from backend.evidence_selection import selected_facts
from backend.extract import ExtractService
from backend.extract.tiles import MERGE_VERSION, STRATEGY
from backend.models.openai_compat import CallResult
from backend.store import BBox, Evidence, Repository, Source
from backend.store.originals import sha256_file


class Client:
    def __init__(self):
        self.calls = 0

    def chat_completions(self, **kwargs):
        self.calls += 1
        overview = "本次是整张图纸概览" in kwargs["messages"][0]["content"]
        payload = {"title_blocks": [{"drawing_number": "S-001", "name": "基础",
            "bbox": dict(x0=.8, y0=.8, x1=.95, y1=.95), "confidence": .9}],
            "facts": [] if overview else [{"text": "荷载由厂家核实；", "category": "说明",
                "bbox": dict(x0=.1, y0=.1, x1=.3, y1=.2), "confidence": .9,
                "unclear": self.calls == 3}]}
        return CallResult(content=json.dumps(payload), input_tokens=10, output_tokens=5,
            raw_status=200, model="test", provider="test", latency_ms=1)


@pytest.fixture
def legacy_case(tmp_path):
    with Repository(tmp_path / "data") as repo:
        project = repo.create_project("大图旧聚合缓存")
        vid = repo.create_version(project["id"], "A")["id"]
        original = tmp_path / "sheet.pdf"
        original.write_bytes(b"%PDF-local-fixture")
        record = repo.import_original(vid, original)["record"]
        Image.new("RGB", (3000, 1200), "white").save(repo.root / "review.png")
        unit = repo.create_unit(vid, record["id"], kind="pdf_page", ordinal=1,
            name="基础", metadata={"review_relative_path": "review.png"})
        client = Client()
        service = ExtractService(repo, use_mock=False, model_client=client)
        first = service.extract_unit(unit["id"])
        assert first.status == "ok" and client.calls == 3
        digest = sha256_file(repo.root / "review.png")
        role = service.cache_role + ":" + STRATEGY
        cache = service.cache.get(digest, service.prompt_version, role)
        cache.pop("merge_version")
        # Old synthetic conflict flags never belonged to the raw region response.
        cache["payload"]["facts"][0].update(unclear=True, region_conflict=True)
        service.cache.set(digest, service.prompt_version, role, cache)
        old_fact = first.facts[0]
        pending = repo.create_pending(vid,
            Evidence(unit["id"], Source.VISION, old_fact["text"], BBox(**old_fact["bbox"])),
            "分块读数不一致")
        yield repo, vid, unit, service, digest, role, pending


def forbid_client():
    pytest.fail("Cache migration must not construct a provider client")


def offline_service(repo):
    return ExtractService(repo, use_mock=False, model_factory=forbid_client)


def test_old_aggregate_rebuilds_from_raw_regions_and_keeps_model_uncertainty(legacy_case):
    repo, vid, unit, service, digest, role, _ = legacy_case
    before_calls = repo.list_records("model_calls", version_id=vid)
    result = offline_service(repo).extract_unit(unit["id"])
    assert result.status == "ok" and result.cache_hit and result.model_calls == 0
    assert not any(f.get("region_conflict") for f in result.facts)
    assert [f["unclear"] for f in result.facts] == [False, True]
    pending = repo.list_records("pending_confirmations", version_id=vid)
    assert len(pending) == 1 and pending[0]["reason"] == "看不清"
    assert service.cache.get(digest, service.prompt_version, role)["merge_version"] == MERGE_VERSION
    assert repo.get("drawing_units", unit["id"])["metadata"]["extract_merge_version"] == MERGE_VERSION
    ids = [f["id"] for f in repo.list_records("facts", version_id=vid)]
    again = offline_service(repo).extract_unit(unit["id"])
    assert again.cache_hit and again.model_calls == 0
    assert [f["id"] for f in repo.list_records("facts", version_id=vid)] == ids
    assert repo.list_records("model_calls", version_id=vid) == before_calls
    assert repo.verify_originals(vid)


def test_missing_raw_region_does_not_fall_back_to_paid_inference_or_old_aggregate(legacy_case):
    repo, vid, unit, service, _, _, _ = legacy_case
    raw_path = next(path for path in service.cache.dir.glob("*.json")
        if set(json.loads(path.read_text())) == {"payload"})
    raw_path.unlink()
    result = offline_service(repo).extract_unit(unit["id"])
    assert result.status == "incomplete" and result.model_calls == 0 and result.facts == []
    assert "原区域识别缓存缺失" in result.incomplete_reason
    assert "未自动发起新的模型请求" in result.incomplete_reason
    assert selected_facts(repo, vid)[1] == []
    assert not repo.list_records("model_calls", version_id=vid)


@pytest.mark.parametrize("status", ["已确认", "无法确定"])
def test_aggregate_upgrade_preserves_human_decisions(legacy_case, status):
    repo, vid, unit, _, _, _, pending = legacy_case
    repo.confirm_pending(pending["id"], status=status,
        confirmed_text="厂家复核" if status == "已确认" else None)
    before = deepcopy(repo.get("pending_confirmations", pending["id"]))
    result = offline_service(repo).extract_unit(unit["id"])
    assert result.status == "ok" and result.model_calls == 0
    assert repo.get("pending_confirmations", pending["id"]) == before


def test_unlocated_region_marker_survives_cache_and_store_without_inventing_unclear(legacy_case):
    repo, vid, unit, service, digest, role, _ = legacy_case
    cached = service.cache.get(digest, service.prompt_version, role)
    cached.update(merge_version=MERGE_VERSION, persisted_unit_ids=[])
    cached['payload']['facts'][0].pop('region_conflict')
    cached['payload']['facts'][0].update(unclear=False, region_bbox_unlocated=True)
    # This case injects current source evidence, rather than replaying the
    # pre-upgrade raw response retained by the separate migration cases.
    cached['raw_payload'] = deepcopy(cached['payload'])
    service.cache.set(digest, service.prompt_version, role, cached)
    result = offline_service(repo).extract_unit(unit['id'])
    assert result.status == 'ok' and result.facts[0]['region_bbox_unlocated']
    assert result.facts[0]['unclear'] is False
    annotations = repo.get('drawing_units', unit['id'])['metadata']['fact_annotations']
    assert annotations[result.fact_ids[0]]['region_bbox_unlocated'] is True
    again = offline_service(repo).extract_unit(unit['id'])
    assert again.facts[0]['region_bbox_unlocated'] and again.facts[0]['unclear'] is False
