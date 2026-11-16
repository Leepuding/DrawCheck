"""Conditional fastener prose requires matching full original paragraphs."""
from copy import deepcopy
import json

from PIL import Image
import pytest

from backend.evidence_selection import selected_facts
from backend.extract import ExtractService
from backend.extract import local_sheet
from backend.extract.positioned_notes import VERSION, reread_fastener_test_note
from backend.store import BBox, Evidence, Repository, Source
from backend.store.originals import sha256_file


MODEL = "M18U型螺栓拉力为6.25kN，抗剪力为3.75kN，施工前需进行抗拔试验，若不满足要求需更换螺栓。"
START = "4.M18U型螺栓抗拔力应不低于6.25kN，抗剪力应≥3.75kN，施工前需进行抗拔试验，"
ACTION = "若不满足要求需提交设计复核。"
ORIGINAL = START + ACTION
BOX = (.25, .4, .63, .42)
WRAP_BOX = (.25, .426, .46, .446)
OLD = dict(x0=.25, y0=.41, x1=.63, y1=.41)


def _word(text, box=BOX, score=.99):
    return local_sheet.Word(text, box, score, Source.VISION.value)


def _bundle(sha, *words):
    return {"version": VERSION, "image_sha256": sha,
            "observations": [{**word.evidence(), "image_sha256": sha, "observation_id": str(i),
                              "crop": [0, 0, .82 + i * .01, 1]}
                             for i, word in enumerate(words)]}


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "review.png"
    Image.new("RGB", (900, 700), "white").save(path)
    return path


def _reread(image, monkeypatch, *, seed=None, first=None, second=None, query=MODEL):
    sha = sha256_file(image)
    seed = [_word(START)] if seed is None else seed
    first = [_word(ORIGINAL)] if first is None else first
    variants = iter([first, first if second is None else second])
    monkeypatch.setattr(local_sheet, "_image_words", lambda *args: next(variants))
    return reread_fastener_test_note(query, image, _bundle(sha, *seed),
                                    image_sha256=sha, engine=object)


@pytest.mark.parametrize("action", [ACTION, "若不满足要求需更换为M22螺栓。"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_complete_condition_action_and_thresholds_are_read_as_printed(image, monkeypatch, action, wrapped):
    first = [_word(START + action[:2]), _word(action[2:], WRAP_BOX)] if wrapped else [_word(START + action)]
    result = _reread(image, monkeypatch, first=first, seed=[_word(START), _word(START)])
    assert result["status"] == "unique_reread" and result["reread_text"] == START + action
    location, = result["locations"]
    assert location["condition"] == "若不满足要求" and location["action"] == action[6:-1]
    assert location["loads"] == [
        {"label": "抗拔力", "threshold": "不低于", "value": "6.25", "unit": "kN"},
        {"label": "抗剪力", "threshold": "≥", "value": "3.75", "unit": "kN"},
    ]
    assert len(location["observed_variants"]) == 2
    assert location["paragraph_binding"]["item"] == "4"
    assert location["bbox"]["y1"] == (WRAP_BOX if wrapped else BOX)[3]


def test_old_model_may_omit_condition_when_both_local_crops_read_it(image, monkeypatch):
    result = _reread(image, monkeypatch, query=MODEL.split("，若")[0] + "。")
    assert result["reread_text"] == ORIGINAL


def test_test_context_accepts_limited_wording_differences_and_a_source_item_number(image, monkeypatch):
    query = MODEL.replace("抗拔试验", "拉拔试验")
    source = ORIGINAL.replace("螺栓抗拔力", "螺栓按相关规定进行抗拔试验，抗拔力")
    source = source.replace("需进行抗拔试验", "需先进行抗拔试验")
    result = _reread(image, monkeypatch, query=query, seed=[_word(source)], first=[_word(source)])
    assert result["reread_text"] == source


@pytest.mark.parametrize("query,source", [
    (MODEL.replace("施工前", "东侧施工前"), ORIGINAL.replace("施工前", "西侧施工前")),
    (MODEL.replace("施工前", "施工后"), ORIGINAL),
    ("5." + MODEL, ORIGINAL),
    (MODEL, ORIGINAL.replace("螺栓抗拔力", "螺栓西侧抗拔力")),
    ("东侧" + MODEL, "4.西侧" + ORIGINAL[2:]),
    (MODEL.replace("若不满足要求", "若试验未达到标准"), ORIGINAL),
])
def test_same_loads_and_agreeing_crops_cannot_identify_a_different_paragraph(image, monkeypatch, query, source):
    result = _reread(image, monkeypatch, query=query, seed=[_word(source)], first=[_word(source)])
    assert result["status"] == "missing" and result["reason"] == "fastener_paragraph_context_mismatch"
    assert not result["locations"] and "reread_text" not in result


def test_two_loads_and_an_unspecified_trial_do_not_bind_an_unnumbered_query(image, monkeypatch):
    query = MODEL.replace("施工前需进行", "需进行")
    source = ORIGINAL.replace("施工前需进行", "需进行")
    result = _reread(image, monkeypatch, query=query, seed=[_word(source)], first=[_word(source)])
    assert result["reason"] == "insufficient_fastener_paragraph_context"
    assert "reread_text" not in result


def test_curved_designation_only_normalizes_with_two_source_crops(image, monkeypatch):
    curved = [_word(ORIGINAL.replace("U型", "∪型"))]
    assert _reread(image, monkeypatch, second=curved)["status"] == "ambiguous"
    result = _reread(image, monkeypatch, seed=[_word(START), _word(START)], second=curved)
    assert result["reread_text"] == ORIGINAL
    assert "∪型" in result["locations"][0]["observed_variants"][1]["observed_text"]


@pytest.mark.parametrize("bad", [
    START.rstrip("，") + "。",
    START + "满足要求需提交设计复核。",
    START + "若不满足要求需。",
    ORIGINAL.rstrip("。"),
    ORIGINAL.replace("不低于", ""),
    ORIGINAL.replace("6.25", "6.52"),
    ORIGINAL.replace("6.25kN", "6.25N"),
    ORIGINAL.replace("6.25kN", "6.25kN/mm"),
    ORIGINAL.replace("抗拔力", "抗拉力"),
    ORIGINAL.replace("抗拔试验", "抗拔检验"),
])
def test_insufficient_conditions_or_force_evidence_leave_original_unverified(image, monkeypatch, bad):
    result = _reread(image, monkeypatch, first=[_word(bad)])
    assert result["status"] == "missing" and "reread_text" not in result


def test_unrelated_nearby_conditions_are_not_joined(image, monkeypatch):
    for lines in (
        [_word(START), _word("5." + ACTION, WRAP_BOX)],
        [_word(START.rstrip("，") + "。"), _word(ACTION, WRAP_BOX)],
        [_word(START), _word(ACTION, WRAP_BOX)],
    ):
        assert _reread(image, monkeypatch, first=lines)["status"] == "missing"


def test_adjacent_paragraphs_with_same_spec_and_values_but_distinct_actions_are_ambiguous(image, monkeypatch):
    next_box = (.25, .43, .63, .45)
    other = ("5." + START[2:] + "若不满足要求需更换为M22螺栓。")
    result = _reread(image, monkeypatch, seed=[_word(ORIGINAL), _word(other, next_box)])
    assert result["status"] == "ambiguous" and result["reason"] == "multiple_fastener_paragraphs"
    assert "reread_text" not in result
    # A neighbouring paragraph missed by the page crops can surface in a
    # fresh detection; it must also block adoption.
    result = _reread(image, monkeypatch, second=[_word(ORIGINAL), _word(other, next_box)])
    assert result["status"] == "ambiguous" and result["competing_observations"]


@pytest.mark.parametrize("other,status", [
    ([], "missing"),
    ([_word(ORIGINAL, score=.94)], "missing"),
    ([_word(ORIGINAL.replace("提交设计复核", "更换为M22螺栓"))], "ambiguous"),
    ([_word(ORIGINAL.replace("不低于", "不超过"))], "ambiguous"),
])
def test_two_crops_must_agree_on_full_source_and_location(image, monkeypatch, other, status):
    assert _reread(image, monkeypatch, second=other)["status"] == status


@pytest.mark.parametrize("history", [None, "已确认", "无法确定"])
def test_normal_cache_entry_preserves_model_and_human_history(tmp_path, image, monkeypatch, history):
    sha = sha256_file(image)
    monkeypatch.setattr("backend.extract.service.cached_note_observations",
                        lambda *args, **kwargs: _bundle(sha, _word(START)))
    monkeypatch.setattr(local_sheet, "_image_words", lambda *args: [_word(ORIGINAL)])
    monkeypatch.setattr(local_sheet, "_engine", object)

    def forbidden():
        pytest.fail("cache replay must not construct a provider client")

    with Repository(tmp_path / "data") as repo:
        vid = repo.create_version(repo.create_project("试验条件")["id"], "A")["id"]
        source = tmp_path / "source.pdf"
        source.write_bytes(b"%PDF-test")
        oid = repo.import_original(vid, source)["record"]["id"]
        (repo.root / "review.png").write_bytes(image.read_bytes())
        unit = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="结构图",
                                metadata={"review_relative_path": "review.png"})
        uid = unit["id"]
        old = Evidence(uid, Source.VISION, MODEL, BBox(**OLD))
        record = repo.add_fact(vid, old)
        if history:
            pending = repo.create_pending(vid, old, "看不清")
            pending = repo.confirm_pending(pending["id"], status=history, confirmed_text=MODEL)
        repo._update("drawing_units", uid, {"metadata_json": json.dumps({
            "review_relative_path": "review.png",
            "fact_annotations": {record["id"]: {"category": "说明", "unclear": True}},
        })})
        service = ExtractService(repo, use_mock=False, model_factory=forbidden)
        raw = {"title_blocks": [], "facts": [
            {"category": "说明", "text": MODEL, "bbox": OLD, "confidence": .8,
             "unclear": True, "region_bbox_unlocated": True},
        ]}
        raw["facts"].append(deepcopy(raw["facts"][0]))
        service.cache.set(sha, service.prompt_version, service.cache_role,
                          {"payload": deepcopy(raw), "raw_payload": raw, "position_version": "previous"})
        result = service.extract_unit(uid)
        assert result.cache_hit and result.model_calls == 0
        assert service.cache.get(sha, service.prompt_version, service.cache_role)["raw_payload"] == raw
        vision = [row for row in repo.list_records("facts", version_id=vid)
                  if row["source"] == Source.VISION.value]
        stored, = vision
        if history:
            assert stored["id"] == record["id"] and stored["text"] == MODEL
            assert repo.get("pending_confirmations", pending["id"]) == pending
            return
        annotation = repo.get("drawing_units", uid)["metadata"]["fact_annotations"][stored["id"]]
        assert stored["text"] == ORIGINAL and annotation["model_original_bbox"] == OLD
        assert annotation["model_original_flags"]["unclear"] and not annotation["unclear"]
        assert annotation["position_recovery"]["original_text"] == MODEL
        assert annotation["position_recovery"]["reading_status"] == "independent_reread"
        assert selected_facts(repo, vid)[1][0]["text"] == ORIGINAL
        assert not repo.list_records("pending_confirmations", version_id=vid)
        assert service.extract_unit(uid).facts == result.facts
        assert repo.list_records("facts", version_id=vid) == vision

        # A subsequent human decision on the corrected anchor survives cache
        # replay of the older, differently worded model source.
        corrected = Evidence(uid, Source.VISION, ORIGINAL, BBox(*BOX))
        pending = repo.create_pending(vid, corrected, "看不清")
        pending = repo.confirm_pending(pending["id"], status="无法确定")
        service.extract_unit(uid)
        assert repo.get("pending_confirmations", pending["id"]) == pending
        assert repo.get("facts", stored["id"]) == stored


@pytest.mark.parametrize("failure", ["missing_bundle", "incomplete_reread"])
def test_failed_reread_preserves_existing_position(tmp_path, image, monkeypatch, failure):
    with Repository(tmp_path / "data") as repo:
        vid = repo.create_version(repo.create_project("试验原框")["id"], "A")["id"]
        source = tmp_path / "source.pdf"
        source.write_bytes(b"%PDF-test")
        oid = repo.import_original(vid, source)["record"]["id"]
        (repo.root / "review.png").write_bytes(image.read_bytes())
        unit = repo.create_unit(vid, oid, kind="pdf_page", ordinal=1, name="结构图",
                                metadata={"review_relative_path": "review.png"})
        sha = sha256_file(image)
        bundle = _bundle(sha, _word(ORIGINAL)) if failure == "incomplete_reread" else None
        monkeypatch.setattr("backend.extract.service.cached_note_observations", lambda *args, **kwargs: bundle)
        monkeypatch.setattr(local_sheet, "_image_words", lambda *args: [])
        monkeypatch.setattr(local_sheet, "_engine", object)

        def forbidden():
            pytest.fail("cache replay must not construct a provider client")

        service = ExtractService(repo, use_mock=False, model_factory=forbidden)
        bbox = dict(zip(("x0", "y0", "x1", "y1"), BOX))
        raw = {"title_blocks": [], "facts": [
            {"category": "说明", "text": MODEL, "bbox": bbox, "confidence": .8, "unclear": True}
        ]}
        service.cache.set(sha, service.prompt_version, service.cache_role, {"payload": raw})
        result = service.extract_unit(unit["id"])
        fact, = result.facts
        assert result.cache_hit and fact["bbox"] == bbox and fact["text"] == MODEL
        assert fact["unclear"] and not fact.get("region_bbox_unlocated")
        pending, = repo.list_records("pending_confirmations", version_id=vid)
        assert pending["reason"] == "看不清"
