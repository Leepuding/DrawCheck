"""A positioned literal may recover a missing glyph without changing audit history."""
from copy import deepcopy

import pytest
from PIL import Image

from backend.evidence_selection import selected_facts
from backend.extract import local_sheet
from backend.extract.positioned_notes import (
    VERSION, corroborated_note_omission, locate_note, reread_note_omission, single_note_omission,
)
from backend.extract.service import ExtractService
from backend.store import BBox, Evidence, Repository, Source
from backend.store.originals import sha256_file

RAW = "所有安装完成的设备外壳应按图纸标记位置悬挂检修记录并归入档案。"
READ = "5.所有安装完成的设备外克壳应按图纸标记位置悬挂检修记录并归入档案。"
BOX = dict(x0=.28, y0=.42, x1=.7, y1=.44)
INVALID = dict(x0=.14, y0=.51, x1=.58, y1=.51)


def observation(text, sha, *, crop=(0., 0., .9, .9), box=BOX, oid="a"):
    return {"text": text, "bbox": dict(box), "confidence": .99, "observation_id": oid,
            "image_sha256": sha, "crop": list(crop)}


def observations(sha):
    return {"version": VERSION, "image_sha256": sha, "observations": [
        observation(READ, sha), observation(READ, sha, crop=(.1, .2, .95, 1.), oid="b")]}


def both_scales(*args):
    return [(READ, .99, 1.), (READ, .98, 1.15)]


@pytest.mark.parametrize("variant", ["verified", "duplicate_position", "same_crop", "clipped",
                                     "adjacent_partial", "condition_variant", "numeric_variant",
                                     "different_native", "missing_scale", "wrong_image"])
def test_only_complete_unambiguous_corroboration_recovers_the_line(tmp_path, monkeypatch, variant):
    image = tmp_path / "sheet.png"
    Image.new("RGB", (900, 700), "white").save(image)
    sha = sha256_file(image)
    bundle = observations(sha)
    rows = bundle["observations"]
    adjacent = {**BOX, "y0": .45, "y1": .47}
    read = both_scales
    if variant == "duplicate_position":
        rows.append(observation(READ, sha, oid="c", box=adjacent))
    elif variant == "same_crop":
        rows[1]["crop"] = rows[0]["crop"]
    elif variant == "clipped":
        rows[1]["crop"][2] = BOX["x1"]
    elif variant == "adjacent_partial":
        rows[:] = [observation(READ[:22], sha), observation(READ[22:], sha, oid="b", box=adjacent)]
    elif variant in {"condition_variant", "numeric_variant"}:
        literal = READ.replace("应按", "不得按" if variant == "condition_variant" else "按20个")
        rows.append(observation(literal, sha, oid="c", box=adjacent))
    elif variant == "different_native":
        def read(*args):
            return [(READ, .99, 1.), ("5." + RAW, 1., 1.15)]
    elif variant == "missing_scale":
        def read(*args):
            return [(READ, .99, 1.)]
    elif variant == "wrong_image":
        Image.new("RGB", (900, 700), "black").save(image)
    monkeypatch.setattr(local_sheet, "_read_detected_line", read)
    prior = deepcopy(bundle)
    assert locate_note(RAW, bundle, image_sha256=sha)["status"] == "missing"
    proof = reread_note_omission(image, bundle, RAW, engine=object)
    assert bundle == prior
    if variant != "verified":
        assert proof is None
        return
    assert proof["original_text"] == RAW and proof["transcribed_text"] == READ
    location, = proof["locations"]
    assert location["bbox"] == BOX and location["observed_text"] == READ
    assert len(location["observed_variants"]) == 4
    for observed in location["observed_variants"]:
        line, = observed["source_lines"]
        assert line["text"] == READ and line["line_reread"]["original_text"] == READ
    location["observed_variants"].pop()
    assert not corroborated_note_omission(proof, READ)


@pytest.mark.parametrize("revised", [
    RAW.replace("应按", "不应按"),
    RAW.replace("按图纸", "先按图纸"),
    RAW.replace("图纸", "图纸甲").removesuffix("。") + "；",
    RAW.replace("图纸", "图纸一"),
    RAW.replace("检修", "检测"),
    RAW.replace("位置", "位置12mm"),
    RAW.replace("位置", "位置甲乙"),
    "2." + READ.split(".", 1)[1],
])
def test_no_number_condition_substitution_or_multi_character_correction(revised):
    assert not single_note_omission("5." + RAW, revised if revised.startswith("2.") else "5." + revised)


@pytest.mark.parametrize("human_status", [None, "已确认", "无法确定", "初读无法确定"])
def test_existing_cache_replays_and_keeps_human_history(tmp_path, monkeypatch, human_status):
    with Repository(tmp_path / "data") as repo:
        vid = repo.create_version(repo.create_project("原文复读")["id"], "A")["id"]
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"%PDF-synthetic")
        oid = repo.import_original(vid, pdf)["record"]["id"]
        image = repo.root / "review.png"
        Image.new("RGB", (900, 700), "white").save(image)
        sha = sha256_file(image)
        unit = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="结构图",
                                metadata={"review_relative_path": "review.png"})
        monkeypatch.setattr("backend.extract.service.cached_note_observations", lambda *a, **k: observations(sha))
        monkeypatch.setattr(local_sheet, "_engine", object)
        monkeypatch.setattr(local_sheet, "_read_detected_line", both_scales)
        def forbidden():
            pytest.fail("cache replay must not construct a provider client")
        service = ExtractService(repo, use_mock=False, model_factory=forbidden)
        raw = {"title_blocks": [], "facts": [{"category": "施工要求", "text": RAW,
               "bbox": INVALID, "region_bbox_unlocated": True, "unclear": True, "confidence": .99}]}
        service.cache.set(sha, service.prompt_version, service.cache_role, {"payload": raw})
        if human_status == "初读无法确定":
            with monkeypatch.context() as no_ocr:
                no_ocr.setattr("backend.extract.service.cached_note_observations", lambda *a, **k: None)
                service.extract_unit(unit["id"])
            old, = repo.list_records("facts", version_id=vid)
            pending, = repo.list_records("pending_confirmations", version_id=vid)
            decision = repo.confirm_pending(pending["id"], status="无法确定")
            assert service.extract_unit(unit["id"]).facts[0]["text"] == RAW
            assert repo.list_records("facts", version_id=vid) == [old]
            assert repo.list_records("pending_confirmations", version_id=vid) == [decision]
            return
        result = service.extract_unit(unit["id"])
        fact, = result.facts
        assert result.cache_hit and result.model_calls == 0
        assert fact["text"] == READ and fact["bbox"] == BOX and fact["unclear"] is True
        assert fact["model_original_bbox"] == INVALID and fact["model_original_flags"]["region_bbox_unlocated"]
        assert fact["position_recovery"]["original_text"] == RAW
        assert service.cache.get(sha, service.prompt_version, service.cache_role)["raw_payload"] == raw
        assert not repo.list_records("pending_confirmations", version_id=vid)
        assert not selected_facts(repo, vid)[1]
        stored, = repo.list_records("facts", version_id=vid)
        if human_status:
            evidence = Evidence(unit["id"], Source.VISION, READ, BBox(**BOX))
            pending = repo.create_pending(vid, evidence, "人工复核")
            confirmed = repo.confirm_pending(pending["id"], status=human_status,
                                             confirmed_text=READ if human_status == "已确认" else None)
        assert service.extract_unit(unit["id"]).facts == result.facts
        assert repo.list_records("facts", version_id=vid) == [stored]
        if human_status:
            assert repo.get("pending_confirmations", pending["id"]) == confirmed
