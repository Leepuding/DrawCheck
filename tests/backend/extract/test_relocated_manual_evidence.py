"""Fresh machine positions must not bypass an existing same-claim human decision."""
import copy
import hashlib
import json

import pytest
from PIL import Image

from backend.evidence_selection import selected_facts
from backend.extract import ExtractService, PROMPT_VERSION
from backend.pending import confirm_pending_item
from backend.rules import RulesAService
from backend.store import BBox, Evidence, Repository, Source


OLD_BOX = dict(x0=0., y0=0., x1=1., y1=1.)
BOXES = [dict(x0=.2, y0=.2, x1=.3, y1=.3), dict(x0=.6, y0=.2, x1=.7, y1=.3)]
IDENTITY = dict(category="尺寸", object_id="F1", comparison_key="length", identity_confirmed=True, confidence=.9)


@pytest.fixture
def case(data_dir, tmp_path):
    with Repository(data_dir) as repo:
        project = repo.create_project("人工优先")
        vid = repo.create_version(project["id"], "A")["id"]
        path = tmp_path / "synthetic.pdf"
        path.write_bytes(b"%PDF-synthetic")
        original = repo.import_original(vid, path)["record"]
        units = []
        for ordinal in (1, 2):
            Image.new("RGB", (200, 100), (255, 200+ordinal, 255)).save(repo.root / f"p{ordinal}.png")
            units.append(repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=ordinal,
                name=f"S-00{ordinal}", drawing_number=f"S-00{ordinal}",
                metadata={"review_relative_path": f"p{ordinal}.png", "extract_status": "ok"}))
        yield repo, vid, units


def add_fact(repo, vid, unit, text, box, annotation):
    row = repo.add_fact(vid, Evidence(unit["id"], Source.VISION, text, BBox(**box)), numeric_value="14.0" if text == "14.000" else text, unit="m")
    current = repo.get("drawing_units", unit["id"])
    metadata = current["metadata"]
    metadata.setdefault("fact_annotations", {})[row["id"]] = annotation
    repo._update("drawing_units", unit["id"], {"metadata_json": json.dumps(metadata)})
    return row


def legacy_decision(case, status, **identity):
    repo, vid, units = case
    annotation = {**IDENTITY, "unclear": True, "bbox_clamped": True, **identity}
    old = add_fact(repo, vid, units[0], "14.000", OLD_BOX, annotation)
    pending = repo.create_pending(vid, Evidence(units[0]["id"], Source.VISION, "14.000", BBox(**OLD_BOX)), "看不清")
    # This is a decision already stored by an older version. The current API
    # correctly rejects a new decision on this known unlocated machine box.
    repo.confirm_pending(pending["id"], status=status,
        confirmed_text="15.000" if status == "已确认" else None)
    if status == "已确认":
        repo.add_fact(vid, Evidence(units[0]["id"], Source.USER, "15.000", BBox(**OLD_BOX)))
    return old, copy.deepcopy(annotation), repo.get("pending_confirmations", pending["id"])


def cached_read(case, *, target=0, bbox=None, **overrides):
    repo, vid, units = case
    unit = units[target]
    def forbidden():
        pytest.fail("A cache-only relocation must never construct a provider client")
    service = ExtractService(repo, use_mock=False, provider="offline", model_name="offline", model_factory=forbidden)
    source = repo.root / f"p{target+1}.png"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    fact = {**IDENTITY, "text": "14.000", "numeric_value": 14., "unit": "m", "unclear": False,
            "bbox": copy.deepcopy(BOXES if bbox is None else bbox), **overrides}
    payload = {"title_blocks": [{"drawing_number": unit["drawing_number"], "name": unit["name"],
                "confidence": 1, "bbox": dict(x0=.7, y0=.8, x1=.9, y1=.95)}], "facts": [fact]}
    service.cache.set(digest, PROMPT_VERSION, service.cache_role, {"payload": payload, "persisted_unit_ids": []})
    result = service.extract_unit(unit["id"])
    assert result.status == "ok" and result.cache_hit and result.model_calls == 0
    again = service.extract_unit(unit["id"])
    assert again.cache_hit and again.model_calls == 0
    return result


@pytest.mark.parametrize("status", ["已确认", "无法确定"])
@pytest.mark.parametrize("multiple", [True, False])
def test_same_claim_relocation_keeps_human_priority_without_copying_value_to_new_positions(case, status, multiple):
    repo, vid, units = case
    old, annotation, pending = legacy_decision(case, status)
    add_fact(repo, vid, units[1], "15.000", dict(x0=.1, y0=.2, x1=.3, y1=.3), {**IDENTITY, "unclear": False})
    before_users = [row for row in repo.list_records("facts", version_id=vid) if row["source"] == Source.USER.value]
    assert not RulesAService(repo).run(vid, persist=False).issues
    cached_read(case, bbox=BOXES if multiple else BOXES[0])
    stored = repo.list_records("facts", version_id=vid)
    assert repo.get("facts", old["id"]) == old
    assert repo.get("drawing_units", units[0]["id"])["metadata"]["fact_annotations"][old["id"]] == annotation
    assert repo.get("pending_confirmations", pending["id"]) == pending
    assert [row for row in stored if row["source"] == Source.USER.value] == before_users
    machine = [row for row in stored if row["unit_id"] == units[0]["id"] and row["source"] == Source.VISION.value]
    assert len(machine) == (3 if multiple else 2)  # Original audit plus actual fresh positions.
    active = [row for row in selected_facts(repo, vid)[1] if row["unit_id"] == units[0]["id"]]
    if status == "已确认":
        assert len(active) == 1 and active[0]["text"] == "15.000" and active[0]["source"] == Source.USER.value
        assert {key: active[0][key] for key in OLD_BOX} == OLD_BOX
    else:
        assert active == []
    assert not RulesAService(repo).run(vid, persist=False).issues
    assert not repo.list_records("model_calls", version_id=vid)


@pytest.mark.parametrize("status", ["已确认", "无法确定"])
def test_reusing_the_original_anchor_never_overwrites_its_audit_annotation(case, status):
    repo, vid, units = case
    old, annotation, pending = legacy_decision(case, status)
    cached_read(case, bbox=[OLD_BOX, BOXES[0]])
    assert repo.get("facts", old["id"]) == old
    assert repo.get("drawing_units", units[0]["id"])["metadata"]["fact_annotations"][old["id"]] == annotation
    assert repo.get("pending_confirmations", pending["id"]) == pending


@pytest.mark.parametrize("changes", [
    {"object_id": "F2"}, {"comparison_key": "width"}, {"category": "标高"},
    {"identity_confirmed": False}, {"object_id": "设备"}, {"object_id": "", "comparison_key": "长度"},
    {"comparison_key": ""}, {"unit": "mm"}, {"quantity_role": "design_total"},
    {"text": "16.000", "numeric_value": 16.},
])
def test_unrelated_or_unbound_machine_claim_is_not_given_the_old_human_scope(case, changes):
    repo, vid, units = case
    legacy_decision(case, "无法确定")
    cached_read(case, **changes)
    active = [row for row in selected_facts(repo, vid)[1] if row["unit_id"] == units[0]["id"]]
    assert len(active) == 2 and all(row["source"] == Source.VISION.value for row in active)


@pytest.mark.parametrize("identity", [
    {"identity_confirmed": False}, {"object_id": "设备"}, {"confidence": .4}, {"region_conflict": True},
])
def test_unreliable_original_identity_cannot_be_guessed_onto_fresh_positions(case, identity):
    repo, vid, units = case
    legacy_decision(case, "无法确定", **identity)
    cached_read(case)
    assert len([row for row in selected_facts(repo, vid)[1] if row["unit_id"] == units[0]["id"]]) == 2


def test_human_scope_does_not_cross_to_another_unit(case):
    repo, vid, units = case
    legacy_decision(case, "无法确定")
    cached_read(case, target=1)
    active = selected_facts(repo, vid)[1]
    assert len(active) == 2 and all(row["unit_id"] == units[1]["id"] for row in active)


def test_same_sheet_material_table_rows_do_not_gain_identity_from_repeated_object_text(case):
    repo, vid, units = case
    legacy_decision(case, "无法确定", category="数量与材料表", quantity_role="material_table")
    cached_read(case, category="数量与材料表", quantity_role="material_table")
    active = [row for row in selected_facts(repo, vid)[1] if row["unit_id"] == units[0]["id"]]
    assert len(active) == 2 and all(row["source"] == Source.VISION.value for row in active)


def test_normal_unreviewed_multi_location_claim_keeps_both_actual_boxes(case):
    repo, vid, units = case
    cached_read(case, identity_confirmed=False, object_id="", comparison_key="")
    active = selected_facts(repo, vid)[1]
    assert len(active) == 2 and not any(row["identity_confirmed"] for row in active)
    assert [{key: row[key] for key in BOXES[0]} for row in active] == BOXES
