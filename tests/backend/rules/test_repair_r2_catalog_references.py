"""Catalogue scope and identity decisions require literal, positioned evidence."""
import pytest

from backend.catalog.coverage import complete_reading, recover_catalog_header
from backend.extract.local_sheet import Word
from backend.linking import LinkingService
from backend.pending.current import current_pending
from backend.references import (AMBIGUOUS_REFERENCE_REASON, SAME_VOLUME_RANGE, TITLE_CONFLICT_REASON,
                                UNLISTED_REFERENCE_REASON, drawing_reference, resolve_reference)
from backend.rules import RulesBService
from backend.store import BBox, Evidence, Source
from test_reference_reliability import add, seed, unit


@pytest.mark.parametrize("case", ["good", "conflicting_total", "new_issue_count_only", "missing_unit",
                                   "wrong_line", "unread_row", "multi_page", "same_box"])
def test_catalog_needs_positioned_total_pagination_and_distinct_read_rows(case):
    def word(text, x, y, width):
        return Word(text, (x, y, x + width, y + .015), .98, Source.VISION.value)

    rows = [{"number": f"P-T0103-0{i}", "value": word(f"P-T0103-0{i}", .2, .3 + .03*i, .17).evidence()}
            for i in (1, 2)]
    if case == "same_box":
        rows[1]["value"]["bbox"] = rows[0]["value"]["bbox"]
    reading = {"catalog": {"header": word("图号", .2, .265, .05).evidence(), "rows": rows,
        "total_fields": ([{"value":3}] if case == "conflicting_total" else []),
        "pagination_fields": [{"page":1, "pages":2 if case == "multi_page" else 1}],
        "physical_row_count": 2, "read_row_count": 2,
        "unread_rows": [{}] if case == "unread_row" else []}}
    words = [word("图纸文件目录", .4, .055, .24),
             word("卷册检索号", .1, .234, .08), word("P-T0103", .29, .234, .17),
             word("本次新出图纸" if case == "new_issue_count_only" else "本卷册总计图纸", .64, .234, .13),
             word("2", .787, .205 if case == "wrong_line" else .235, .018)]
    if case != "missing_unit":
        words.append(word("张打印件", .813, .234, .08))
    result = complete_reading(reading, words)["catalog"]
    assert result["complete"] is (case == "good")
    assert result["header_volume"]["number"] == "P-T0103"
    assert result["rows"] == rows


def test_legacy_complete_without_printed_unit_cannot_skip_validation():
    reading = {"catalog": {"complete": True, "coverage_revision": 1,
        "header": {"bbox": {"y0": .25}}, "rows": [{"value": {"bbox": {"y0": .3, "y1": .32}}}],
        "total_fields": [{"value": 1, "label": {"text": "本卷册总计图纸1份"}}],
        "pagination_fields": [{"page": 1, "pages": 1}], "physical_row_count": 1,
        "read_row_count": 1, "unread_rows": []}}
    checked = recover_catalog_header(None, {"kind": "pdf_page"}, reading, get_engine=None)
    assert reading["catalog"]["complete"] and not checked["catalog"]["complete"]
    assert checked["catalog"]["rows"] == reading["catalog"]["rows"]


@pytest.mark.parametrize("case", ["all", "catalog_only", "cross_project", "duplicate", "unverified"])
def test_range_requires_every_same_volume_arrival(repo, tmp_path, case):
    vid, oid = seed(repo, tmp_path)
    src = unit(repo, vid, oid, "P-T0103-02", 1)
    for index in range(3, 6):
        if index == 4 and case == "catalog_only":
            continue
        prefix = "Q" if index == 4 and case == "cross_project" else "P"
        unit(repo, vid, oid, f"{prefix}-T0103-0{index}", index,
             title_identity_uncertain=(index == 4 and case == "unverified"))
    if case == "duplicate":
        unit(repo, vid, oid, "P-T0103-04", 8)
    if case == "catalog_only":
        unit(repo, vid, oid, "P-T0103", 9, is_catalog_sheet=True, catalog_complete=True,
             catalog_listed_numbers=["P-T0103-03", "P-T0103-04", "P-T0103-05"])
    for text in ("13.A-A剖面位置详见-03～-05图。", "A-A剖面位置详见-03~-05图。"):
        fact = add(repo, vid, src, text)
        repo.create_pending(vid, Evidence(src["id"], Source.VISION, fact["text"], BBox(.1,.1,.3,.11)),
                            AMBIGUOUS_REFERENCE_REASON)
    result = RulesBService(repo).run(vid, persist=True)
    links = LinkingService(repo).link_version(vid, persist=False)
    assert not result.issues
    detail_links = [edge for edge in links.associations if edge["kind"] == "detail_index"]
    history = repo.list_records("pending_confirmations", version_id=vid)
    assert len(history) == 2
    assert all(r["target_basis"] == SAME_VOLUME_RANGE for r in result.detail["references"])
    if case == "all":
        assert len(detail_links) == 3 and not result.pendings and not current_pending(repo, vid)
    else:
        assert not detail_links and len(links.incomplete) == 1
        assert len(current_pending(repo, vid)) == 1


@pytest.mark.parametrize("text", ["详见-05～-03图", "详见-03～05图", "详见-03～-05图、-06图",
                                 "详见-03、-05图", "详见（T0103-03、T0104-03）",
                                 "详见S-003及S-004", "详见S-003/S-004", "详见S-003，做法详见-04图"])
def test_no_first_target_for_non_range_or_multi_clause(text):
    ref = drawing_reference({"text": text}, "33-NZ16181S-T0103-02")
    assert ref["reason"] == AMBIGUOUS_REFERENCE_REASON and not ref["target"] and not ref.get("targets")


def test_partial_and_volume_keep_projects_distinct(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    src = unit(repo, vid, oid, "33-N16091S-T0107-02", 1)
    target = unit(repo, vid, oid, "33-NZ16181S-T0103-03", 2)
    volume = unit(repo, vid, oid, "33-NZ16181S-T0103", 3, is_catalog_sheet=True,
                  catalog_volume_evidence={"number": "33-NZ16181S-T0103"})
    units = {u["id"]:u for u in (src, target, volume)}
    home = src["drawing_number"]
    short = resolve_reference(drawing_reference({"text":"详见T0103-03"}, home), units)
    assert short["status"] == "unverified"
    bare = resolve_reference(drawing_reference({"text":"具体定位坐标详见T0103"}, home), units)
    assert bare["status"] == "unverified" and bare["catalog_candidate_numbers"] == [volume["drawing_number"]]
    full = resolve_reference(drawing_reference({"text":"详见33-NZ16181S-T0103-03"}, home), units)
    assert full["status"] == "resolved"
    same = resolve_reference(drawing_reference({"text":"详见T0103"}, "33-NZ16181S-T0107-02"), units)
    assert same["status"] == "resolved" and same["unit_ids"] == [volume["id"]]


@pytest.mark.parametrize("suffix", ["-04图", "33-NZ16181S-T0102-04", "T0102-04"])
def test_conflicting_actual_title_is_not_a_missing_reference(repo, tmp_path, suffix):
    vid, oid = seed(repo, tmp_path)
    src = unit(repo, vid, oid, "33-NZ16181S-T0102-02", 1)
    printed = "33-NC09811S-T0102-04"
    listed = "33-NZ16181S-T0102-04"
    name = {"text":"地面支架基础、节点详图"}
    actual = unit(repo, vid, oid, printed, 2, local_sheet={
        "title":{"number":printed, "name":name, "value":{"text":printed}}})
    cat = unit(repo, vid, oid, "33-NZ16181S-T0102", 3, is_catalog_sheet=True,
               catalog_complete=True, catalog_listed_numbers=[listed],
               local_sheet={"catalog":{"rows":[{"number":listed,"name":name,"value":{"text":listed}}]}})
    add(repo, vid, src, "基础详图见" + suffix)
    result = RulesBService(repo).run(vid, persist=True)
    ref, = result.detail["references"]
    assert not result.issues
    pending, = current_pending(repo, vid)
    if suffix == "T0102-04":
        assert ref["status"] == "ambiguous" and "多个图纸身份" in pending["reason"]
        return
    assert ref["status"] == "unverified"
    assert ref["catalog_title_conflicts"][0]["unit_id"] == actual["id"]
    assert ref["catalog_title_conflicts"][0]["catalog_unit_id"] == cat["id"]
    assert pending["reason"] == TITLE_CONFLICT_REASON
    if suffix == "-04图":
        assert pending["reference_target"] == listed


def test_material_after_detail_caption_is_not_an_index(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    src = unit(repo, vid, oid, "S-001", 1)
    text = "M12预埋螺栓详图 Q235B,镀锌,厚度不小于80μm"
    fact = add(repo, vid, src, text, category="详图索引断链", target_drawing_number="Q235B")
    old = repo.create_pending(vid, Evidence(src["id"], Source.VISION, text,
                             BBox(*(fact[key] for key in ("x0", "y0", "x1", "y1")))),
                             AMBIGUOUS_REFERENCE_REASON)
    assert drawing_reference(fact) is None and not current_pending(repo, vid)
    assert repo.get("pending_confirmations", old["id"]) == old
    assert not RulesBService(repo).run(vid).pendings
    assert drawing_reference({"text": "详图 S-002"})["target"] == "S-002"
    assert drawing_reference({"text": "做法详见Q235B"})["reason"] == AMBIGUOUS_REFERENCE_REASON


def test_unlisted_other_volume_reference_does_not_claim_same_volume(repo, tmp_path):
    vid, oid = seed(repo, tmp_path)
    source = unit(repo, vid, oid, "33-NZ16181S-T0103-02", 1)
    text = "做法详见T0104-03"
    add(repo, vid, source, text)
    result = RulesBService(repo).run(vid, persist=True)
    assert not result.issues
    pending, = current_pending(repo, vid)
    assert pending["reason"] == UNLISTED_REFERENCE_REASON
    assert "reference_target" not in pending
