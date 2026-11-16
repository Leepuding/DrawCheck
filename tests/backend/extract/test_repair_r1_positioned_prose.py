"""Position proof must cover the original claim without borrowing its model box."""
from copy import deepcopy
import json

import pytest

from backend.extract.positioned_notes import VERSION, locate_note
from backend.extract.service import ExtractService, POSITION_VERSION
from backend.evidence_selection import selected_facts
from backend.pending import list_pending
from backend.pending.service import confirm_pending_item
from backend.store import BBox, Evidence, Repository, Source, StoreError
from backend.store.originals import sha256_file
from PIL import Image

SHA = "c" * 64
BOX = dict(x0=.25, y0=.4, x1=.63, y1=.42)
TEXT = "构件应按设计要求安装，未检验合格不得施工。"


def row(text, *, box=None, index="line", sha=SHA):
    return {"text": text, "bbox": dict(BOX if box is None else box),
            "confidence": .99, "observation_id": index,
            "image_sha256": sha, "crop": [0, 0, 1, 1]}


def locate(text, *rows):
    return locate_note(text, {"version": VERSION, "image_sha256": SHA,
                              "observations": list(rows)}, image_sha256=SHA)


def test_body_may_exclude_printed_list_marker_but_position_retains_observed_line():
    original = "12、" + TEXT
    result = locate(TEXT, row(original))
    assert result["status"] == "unique_format_equivalent"
    assert result["original_text"] == TEXT
    location, = result["locations"]
    assert location["observed_text"] == original and location["bbox"] == BOX
    assert locate("13、" + TEXT, row(original))["status"] == "missing"


@pytest.mark.parametrize("observed", [
    TEXT.replace("不得", "可以"),
    TEXT.replace("未检验", "检验"),
    TEXT.replace("。", "，无须复验。"),
    TEXT.replace("安装，", "安装："),
    "要求复核" + TEXT,
])
def test_no_partial_or_fuzzy_prose_match(observed):
    assert locate(TEXT, row("12、" + observed))["status"] == "missing"


def test_comma_width_does_not_change_spec_numbers_units_or_negation():
    text = "预埋件材质为Q355B,防护层厚度不得小于1.50mm。"
    observed = text.replace(",", "，")
    assert locate(text, row(observed))["status"] == "unique_format_equivalent"
    for changed in (observed.replace("1.50", "1.05"), observed.replace("mm", "cm"),
                    observed.replace("不得", "应当"), observed.replace("Q355B", "Q235B")):
        assert locate(text, row(changed))["status"] == "missing"
    number = "设计水平荷载应为1,500kN并进行复核。"
    assert locate(number, row(number.replace(",", "，")))["status"] == "missing"


def test_exact_and_formatted_crop_variants_merge_only_at_one_position():
    a = row(TEXT)
    b = row("12、" + TEXT, box={**BOX, "x0": .247}, index="shifted")
    result = locate(TEXT, a, b)
    assert result["status"] == "unique_exact"
    assert len(result["locations"][0]["observed_variants"]) == 2
    b["bbox"].update(y0=.5, y1=.52)
    assert locate(TEXT, a, b)["status"] == "ambiguous"


def test_numbered_intervening_paragraph_cannot_be_skipped_to_complete_a_match():
    first = row("构件安装前应完成防腐施工")
    intervening = row("2.支座材质应有合格证明。", box={**BOX, "y0": .421, "y1": .426}, index="paragraph")
    tail = row("并完成检查后方可投入使用。", box={**BOX, "y0": .429, "y1": .449}, index="tail")
    query = first["text"] + tail["text"]
    assert locate(query, first, tail)["status"] == "unique_exact"
    assert locate(query, first, intervening, tail)["status"] == "missing"
    first["text"] += "。"
    assert locate(first["text"] + tail["text"], first, tail)["status"] == "missing"


@pytest.mark.parametrize("category,tiled", [("施工要求", False), ("设计要求", False), ("材料规格", True)])
def test_cache_replay_recovers_invalid_position_without_changing_reading_or_calling_model(
        tmp_path, monkeypatch, category, tiled):
    from backend.extract.tiles import MERGE_VERSION, STRATEGY
    def forbidden():
        pytest.fail("cache replay must not construct a provider client")

    with Repository(tmp_path / "data") as repo:
        version = repo.create_version(repo.create_project("定位复验")["id"], "A")
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"%PDF-synthetic")
        original = repo.import_original(version["id"], pdf)["record"]
        path = repo.root / "review.png"
        Image.new("RGB", (3000, 1000) if tiled else (900, 700), "white").save(path)
        sha = sha256_file(path)
        unit = repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=1, name="结构图",
                                metadata={"review_relative_path": "review.png"})
        monkeypatch.setattr("backend.extract.service.cached_note_observations",
                            lambda *a, **k: {"version": VERSION, "image_sha256": sha,
                                             "observations": [row("1." + TEXT, sha=sha)]})
        service = ExtractService(repo, use_mock=False, model_factory=forbidden)
        old = dict(x0=.4, y0=.8, x1=.8, y1=.8)
        original_fact = {"category": category, "text": TEXT, "bbox": old, "unclear": True,
                         "bbox_clamped": True, "region_bbox_unlocated": True, "confidence": .98}
        raw = {"title_blocks": [], "facts": [original_fact]}
        role = f"{service.cache_role}:{STRATEGY}" if tiled else service.cache_role
        service.cache.set(sha, service.prompt_version, role,
                          {"payload": deepcopy(raw), "raw_payload": raw,
                           "merge_version": MERGE_VERSION, "position_version": "previous"})

        result = service.extract_unit(unit["id"])
        fact, = result.facts
        assert result.cache_hit and result.model_calls == 0
        assert fact["bbox"] == BOX and fact["text"] == TEXT and fact["unclear"] is True
        assert fact["position_recovery"]["version"] == POSITION_VERSION
        assert fact["model_original_bbox"] == old and fact["model_original_flags"]["bbox_clamped"]
        assert not fact.get("region_bbox_unlocated")
        assert service.cache.get(sha, service.prompt_version, role)["raw_payload"] == raw
        assert service.extract_unit(unit["id"]).facts == result.facts
        records = repo.list_records("facts", version_id=version["id"])
        assert len(records) == 1 and records[0]["source"] == Source.VISION.value
        assert not repo.list_records("pending_confirmations", version_id=version["id"])
        assert not selected_facts(repo, version["id"])[1]


@pytest.mark.parametrize("case", ["verified", "changed_image", "uncertain_ocr", "different_literal",
                                  "prefix_only", "comparison_field", "tile_conflict", "human_decision",
                                  "specific_reason", "other_position"])
def test_old_prose_question_requires_full_literal_same_image_and_exact_anchor(tmp_path, case):
    with Repository(tmp_path / "data") as repo:
        vid = repo.create_version(repo.create_project("定位复核")["id"], "A")["id"]
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(b"%PDF-synthetic")
        oid = repo.import_original(vid, pdf)["record"]["id"]
        image = repo.root / "review.png"
        Image.new("RGB", (900, 700), "white").save(image)
        sha = sha256_file(image)
        unit = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="结构图")
        ev = Evidence(unit["id"], Source.VISION, TEXT, BBox(**BOX))
        fact = repo.add_fact(vid, ev)
        proof = locate_note(TEXT, {"version": VERSION, "image_sha256": sha,
                            "observations": [row("1." + TEXT, sha=sha)]}, image_sha256=sha)
        proof.update(version=POSITION_VERSION, scope="note")
        ann = {"category": "施工要求", "unclear": True, "position_recovery": proof}
        if case == "changed_image":
            Image.new("RGB", (900, 700), "black").save(image)
        elif case == "uncertain_ocr":
            proof["locations"][0]["confidence"] = .91
        elif case == "different_literal":
            location = proof["locations"][0]
            location["observed_text"] = location["observed_text"].replace("不得", "可以")
            location["source_lines"][0]["text"] = location["observed_text"]
        elif case == "prefix_only":
            proof["status"] = "unique_prefix"
        elif case == "comparison_field":
            ann["comparison_key"] = "支架安装条件"
        elif case == "tile_conflict":
            ann["model_original_flags"] = {"region_conflict": True}
        repo._update("drawing_units", unit["id"], {"metadata_json": json.dumps(
            {"extract_status": "ok", "extract_image_sha256": sha, "review_relative_path": "review.png",
             "fact_annotations": {fact["id"]: ann}})})
        other = Evidence(unit["id"], Source.VISION, TEXT, BBox(.25, .5, .63, .52))
        old = repo.create_pending(vid, other if case == "other_position" else ev,
                                  "两模型读数不一致" if case == "specific_reason" else "看不清")
        if case == "human_decision":
            old = repo.confirm_pending(old["id"], status="无法确定")
        for _ in range(2):
            rows = list_pending(repo, vid)["items"]
            assert [item["id"] for item in rows] == ([] if case == "verified" else [old["id"]])
            assert repo.get("pending_confirmations", old["id"]) == old
        if case == "verified":
            with pytest.raises(StoreError, match="已有本地识读依据"):
                confirm_pending_item(repo, old["id"], {"status": "已确认", "confirmed_text": TEXT})
            assert repo.list_records("facts", version_id=vid) == [fact]


def test_native_reread_requires_two_crops_and_unmodified_complete_reference_token(tmp_path, monkeypatch):
    from backend.extract import local_sheet, positioned_notes as notes

    image = tmp_path / "sheet.png"
    Image.new("RGB", (1200, 900), "white").save(image)
    sha = sha256_file(image)
    query = "支座做法详见D0102-03"
    prior = "支座做法详见DO102-03"
    first = row(prior, index="a", sha=sha)
    second = {**row(prior, index="b", sha=sha), "crop": [.05, .1, .9, .9]}
    token = row("详见D0102-03", box={**BOX, "x0": .49}, index="token", sha=sha)
    bundle = {"version": VERSION, "image_sha256": sha, "observations": [first, second, token]}
    original = deepcopy(bundle)

    def reread(engine, image, box):
        yield query, .97, 1.15

    monkeypatch.setattr(local_sheet, "_read_detected_line", reread)
    expanded = notes.reread_note_lines(image, bundle, [query], engine=lambda: object())
    assert bundle == original and notes.locate_reference(query, bundle, image_sha256=sha)["status"] == "missing"
    located = notes.locate_reference(query, expanded, image_sha256=sha)
    assert notes.independently_reread_location(located, expanded, reference=True)
    assert located["locations"][0]["bbox"] == BOX
    assert located["reread_token_corroboration"]["observation_id"] == "token"

    for remove in ("b:line:1.15", "token"):
        partial = {**expanded, "observations":[o for o in expanded["observations"] if o["observation_id"] != remove]}
        result = notes.locate_reference(query, partial, image_sha256=sha)
        assert not notes.independently_reread_location(result, partial, reference=True)

    # A similar complete token in another drawing row does not corroborate.
    other_row = {**token, "bbox":{**token["bbox"], "y0":.65, "y1":.67}}
    nearby = {**expanded, "observations":[other_row if o["observation_id"] == "token" else o
                                         for o in expanded["observations"]]}
    located = notes.locate_reference(query, nearby, image_sha256=sha)
    assert not notes.independently_reread_location(located, nearby, reference=True)


@pytest.mark.parametrize("prior", [
    "设计荷载不应低于1.1kN并在施工前复验。",
    "设计荷载应低于1.0kN并在施工前复验。",
    "设计荷载不应低于1.0kW并在施工前复验。",
])
def test_reread_cannot_override_printed_digit_negation_or_unit(prior, tmp_path, monkeypatch):
    from backend.extract import local_sheet, positioned_notes as notes

    image = tmp_path / "sheet.png"
    Image.new("RGB", (1200, 900), "white").save(image)
    sha = sha256_file(image)
    query = "设计荷载不应低于1.0kN并在施工前复验。"
    monkeypatch.setattr(local_sheet, "_read_detected_line",
                        lambda *args: [(query, .99, 1.), (query, .99, 1.15)])
    bundle = {"version": VERSION, "image_sha256": sha, "observations": [row(prior, sha=sha)]}
    assert notes.reread_note_lines(image, bundle, [query], engine=lambda: object()) == bundle
