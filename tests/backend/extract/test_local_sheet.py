from backend.catalog import CatalogService
import json

import pytest

from backend.extract.local_sheet import (LocalSheetReader, Word, catalog_rows, material_rows,
                                       positioned_title_readings, title_field)
from backend.rules.catalog_titleblock import run_catalog_titleblock
from backend.store import Repository, Source


def word(text, x, y, *, score=.98, source=Source.VISION):
    return Word(text, (x-.015, y-.007, x+.015, y+.007), score, source.value)


def title(number, name, *, source=Source.VISION):
    return title_field([word("图 号", .62, .94, source=source),
                        word(number, .76, .94, source=source),
                        word(name, .76, .90, source=source)])


def test_exact_title_requires_label_and_unique_spatial_candidate():
    assert title("33-AB123-D0601-01", "控制系统接线图")["number"] == "33-AB123-D0601-01"
    assert title("33 - AB123 - D0601 - 01", "控制系统接线图")["number"] == "33-AB123-D0601-01"
    assert title_field([word("33-AB123-D0601-01", .76, .94)]) is None
    assert title_field([word("图号", .62, .94), word("33-AB123-D0601-01", .76, .94),
                        word("33-AB123-D0601-02", .80, .94)]) is None
    assert title_field([word("图号", .62, .94), word("33-AB123-D0601-01", .76, .88)]) is None


def catalog_words():
    return [
        word("图纸文件目录", .4, .07), word("第1页共1页", .8, .07), word("本卷册总计图纸2张", .72, .14),
        word("序号", .11, .23), word("图号", .32, .23), word("文件名称", .55, .23),
        word("1", .11, .29), word("33-AB123-D0601-01", .32, .29), word("控制系统接线图", .55, .29),
        word("1", .11, .34), word("33-AB123-D0601-01", .32, .34), word("监控系统材料表", .55, .34),
    ]


def test_directory_keeps_duplicate_physical_rows_and_distinguishes_incomplete():
    result = catalog_rows(catalog_words())
    assert result["complete"] is True
    first, second = result["rows"]
    assert first["number"] == second["number"]
    assert first["value"]["bbox"] != second["value"]["bbox"]
    incomplete = catalog_rows([entry for entry in catalog_words() if entry.text != "第1页共1页"])
    assert len(incomplete["rows"]) == 2 and incomplete["complete"] is False


def test_material_fields_do_not_cross_rows_or_same_name_sections():
    def intervals(*edges):
        return list(zip(edges, edges[1:]))

    grid = (intervals(.05, .18, .34, .50, .62, .73, .91),
            intervals(.18, .22, .26, .30, .34, .38))
    rows = material_rows([
        word("序号", .1, .2), word("名称", .26, .2), word("型号", .42, .2),
        word("单位", .58, .2), word("数量", .66, .2), word("备注", .82, .2),
        word("一", .1, .24), word("安装于升压站", .26, .24),
        word("1", .1, .28), word("光缆熔纤盒", .26, .28), word("台", .58, .28),
        word("4", .66, .28), word("其中2台利旧", .82, .28),
        word("二", .1, .32), word("安装于场区", .26, .32),
        word("1", .1, .36), word("光缆熔纤盒", .26, .36), word("台", .58, .36), word("34", .66, .36),
    ], grid=grid)["rows"]
    assert [(row["section"]["text"], row["fields"]["quantity"]["text"]) for row in rows] == [
        ("安装于升压站", "4"), ("安装于场区", "34")]
    assert rows[0]["fields"]["remarks"]["text"] == "其中2台利旧"
    assert rows[1]["fields"]["remarks"] is None


def test_product_rule_pairs_duplicate_number_by_unique_name_with_two_original_anchors(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"synthetic fixture")
    with Repository(tmp_path / "data") as repo:
        pid = repo.create_project("test")["id"]
        vid = repo.create_version(pid, "A")["id"]
        original = repo.import_original(vid, source)["record"]
        catalog = CatalogService(repo)

        def add(ordinal, reading):
            unit = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=ordinal,
                                    name=f"第{ordinal}页", drawing_number=str(ordinal), metadata={"extract_status": "ok"})
            catalog.apply_extract_result({"unit_id": unit["id"], "version_id": vid, "title_blocks": []},
                                         local_reading=reading)
            return unit["id"]

        index = add(1, {"catalog": catalog_rows(catalog_words())})
        same = add(2, {"title": title("33-AB123-D0601-01", "控制系统接线图")})
        different = add(3, {"title": title("33-AB123-D0601-02", "监控系统材料表", source=Source.PDF_TEXT)})
        run = run_catalog_titleblock(repo, vid)
        assert {row["unit_id"]: row["outcome"] for row in run["local_comparisons"]} == {
            same: "match", different: "mismatch"}
        issue, = run["issues"]
        assert issue["left_unit_id"] == index and issue["right_unit_id"] == different
        assert issue["left_text"] == "33-AB123-D0601-01"
        assert issue["right_text"] == "33-AB123-D0601-02"
        assert issue["right_source"] == Source.PDF_TEXT.value
        assert issue["left_x0"] < issue["left_x1"] and issue["right_y0"] < issue["right_y1"]
        assert run["incomplete"] == []
        assert not any(item.get("fixture_id") == "ISSUE-CAT-COUNT" for item in run["issues"])
        assert repo.get("drawing_units", index)["metadata"]["catalog_complete"]
        assert run_catalog_titleblock(repo, vid)["issues"][0]["id"] == issue["id"]
        add(4, {"title": title("33-AB123-D0601-03", "监控系统材料表")})
        audited = run_catalog_titleblock(repo, vid, persist=False)
        assert {item["outcome"] for item in audited["local_comparisons"]} == {"match"}


def split_catalog_words():
    words = [entry for entry in catalog_words() if entry.text not in {"第1页共1页", "本卷册总计图纸2张"}]
    words += [Word("第    页 共    页", (.70, .05, .95, .07), 1, Source.CAD_TEXT.value),
              word("1", .75, .06, source=Source.CAD_TEXT), word("1", .88, .06, source=Source.CAD_TEXT),
              Word("本卷册总计图纸      张", (.64, .13, .87, .15), 1, Source.CAD_TEXT.value),
              word("2", .80, .14, source=Source.CAD_TEXT)]
    return words


@pytest.mark.parametrize("total", ["本卷册总计图纸2", "本卷册总计图纸2份"])
def test_initial_directory_requires_printed_sheet_count(total):
    words = [word(total, .72, .14) if item.text.endswith("2张") else item for item in catalog_words()]
    result = catalog_rows(words)
    assert len(result["rows"]) == 2 and not result["complete"]


def test_initial_directory_cannot_share_one_number_box_across_two_rows():
    words = catalog_words()[:6] + [
        word("1", .11, .29), word("2", .11, .31),
        Word("33-AB123-D0601-01", (.30, .28, .36, .32), .98, Source.VISION.value),
    ]
    result = catalog_rows(words)
    first, second = result["rows"]
    assert first["value"]["bbox"] == second["value"]["bbox"] and not result["complete"]


def test_split_native_total_and_pagination_retain_separate_field_evidence():
    words = split_catalog_words()
    result = catalog_rows(words + [words[-1]])
    assert result["complete"] and result["physical_row_count"] == result["read_row_count"] == 2
    total, = result["total_fields"]
    assert total["value"] == 2 and total["values"][0]["text"] == "2"
    assert total["label"]["text"] == "本卷册总计图纸      张"
    pagination, = result["pagination_fields"]
    assert pagination["page"] == pagination["pages"] == 1
    assert [item["text"] for item in pagination["values"]] == ["1", "1"]
    assert pagination["values"][0]["bbox"] != pagination["values"][1]["bbox"]


@pytest.mark.parametrize("fault", ["missing_total", "other_field", "ambiguous_total", "missing_page", "multipage", "unread_row"])
def test_split_header_missing_or_ambiguous_evidence_cannot_close_directory(fault):
    words = split_catalog_words()
    if fault == "missing_total":
        words = [entry for entry in words if not (entry.text == "2" and entry.y == .14)]
    elif fault == "other_field":
        words = [entry for entry in words if not (entry.text == "2" and entry.y == .14)]
        words.append(word("2", .92, .14))
    elif fault == "ambiguous_total":
        words.append(word("3", .72, .14))
    elif fault == "missing_page":
        words = [entry for entry in words if not (entry.text == "1" and entry.x == .88)]
    elif fault == "multipage":
        words = [word("2", .88, .06) if entry.text == "1" and entry.x == .88 else entry for entry in words]
    elif fault == "unread_row":
        words = [entry for entry in words if not (entry.text.startswith("33-") and entry.y > .3)]
    result = catalog_rows(words)
    assert not result["complete"]
    if fault == "unread_row":
        assert result["physical_row_count"] == 2 and result["read_row_count"] == 1
        assert len(result["unread_rows"]) == 1


def test_positioned_title_readings_use_native_anchor_and_leave_repeated_literals_ambiguous():
    words = [word("图号", .62, .94), word("AB-D0101-01", .76, .94), word("图纸名称示例", .76, .90),
             word("设计单位原文", .58, .84, source=Source.PDF_TEXT), word("设计单位原文", .58, .84),
             word("重复名称", .60, .78), word("重复名称", .85, .78), word("图外相同名称", .1, .3)]
    reading = positioned_title_readings(words, title_field(words))
    native, = [entry for entry in reading if entry["text"] == "设计单位原文"]
    assert native["source"] == Source.PDF_TEXT.value and native["candidate_count"] == 1
    duplicates = [entry for entry in reading if entry["text"] == "重复名称"]
    assert len(duplicates) == 2 and all(entry["candidate_count"] == 2 for entry in duplicates)
    assert not any(entry["text"] == "图外相同名称" for entry in reading)
    assert positioned_title_readings(words, None) == []


def test_title_project_and_stage_require_their_own_unique_labelled_cells():
    words = [word("图号", .62, .94), word("AB-D0101-01", .76, .94),
             word("项目", .76, .82), word("原文项目甲", .79, .85),
             word("设计阶段", .92, .82), word("施工阶段原文", .93, .85)]
    result = positioned_title_readings(words, title_field(words))
    fields = {entry["field"]: entry for entry in result if entry.get("field")}
    assert fields["project_name"]["text"] == "原文项目甲"
    assert fields["project_name"]["label_evidence"]["text"] == "项目"
    assert fields["drawing_type"]["text"] == "施工阶段原文"
    words.append(word("同格另一个候选", .78, .85))
    ambiguous = positioned_title_readings(words, title_field(words))
    assert not any(entry.get("field") == "project_name" for entry in ambiguous)


def test_pdf_native_title_remains_available_without_ocr_dependency(tmp_path, monkeypatch):
    from PIL import Image
    import backend.extract.local_sheet as local

    source = tmp_path / "book.pdf"
    source.write_bytes(b"synthetic")
    with Repository(tmp_path / "data") as repo:
        project = repo.create_project("native")
        vid = repo.create_version(project["id"], "A")["id"]
        original = repo.import_original(vid, source)["record"]
        Image.new("RGB", (600, 600), "white").save(repo.root / "preview.png")
        unit = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=1, name="old",
            metadata={"preview_relative_path": "preview.png"})
        native = [word("图号", .62, .94, source=Source.PDF_TEXT),
                  word("AB-D0101-01", .76, .94, source=Source.PDF_TEXT)]
        monkeypatch.setattr(local, "_native_words", lambda path: native)
        monkeypatch.setattr(local, "_engine", lambda: pytest.fail("Native text must not require OCR"))
        reader = LocalSheetReader(repo)
        reader.available = False
        reading = reader.read_unit(unit["id"])
        assert reading["title"]["number"] == "AB-D0101-01"
        assert all(entry["source"] == Source.PDF_TEXT.value for entry in reading["title_readings"])


def test_cad_catalog_reads_split_native_fields_without_image_or_ocr(tmp_path):
    from backend.store import BBox, Evidence
    path = tmp_path / "native.dwg"
    path.write_bytes(b"synthetic-cad")
    with Repository(tmp_path / "data") as repo:
        project = repo.create_project("native")
        vid = repo.create_version(project["id"], "A")["id"]
        original = repo.import_original(vid, path)["record"]
        unit = repo.create_unit(vid, original["id"], kind="dwg_frame", ordinal=1, name="目录")
        for entry in split_catalog_words():
            repo.add_fact(vid, Evidence(unit["id"], Source.CAD_TEXT, entry.text, BBox(*entry.box)))
        reader = LocalSheetReader(repo)
        reader.available = False
        reading = reader.read_unit(unit["id"])
        assert reading["catalog"]["complete"] and reading["catalog"]["physical_row_count"] == 2
        catalog = CatalogService(repo)
        catalog.apply_extract_result({"unit_id": unit["id"], "version_id": vid, "title_blocks": []}, local_reading=reading)
        meta = repo.get("drawing_units", unit["id"])["metadata"]
        assert meta["catalog_complete"] and len(meta["catalog_entries"]) == 2
        assert meta["catalog_duplicate_numbers"] == ["33-AB123-D0601-01"]
        assert "catalog_incomplete_reason" not in meta


def test_local_title_replaces_unusable_model_position_without_editing_human_or_machine_history(tmp_path):
    from backend.store import BBox, Evidence
    source = tmp_path / "book.pdf"
    source.write_bytes(b"synthetic")
    with Repository(tmp_path / "data") as repo:
        project = repo.create_project("read")
        vid = repo.create_version(project["id"], "A")["id"]
        original = repo.import_original(vid, source)["record"]
        unit = repo.create_unit(vid, original["id"], kind="pdf_page", ordinal=1, name="待识读", drawing_number="1",
                                metadata={"extract_status": "ok", "title_identity_uncertain": True})
        pending = repo.create_pending(vid, Evidence(unit["id"], Source.VISION, "AB-D0101-01", BBox(1, .8, 1, .95)), "看不清")
        reading = {"title": title("AB-D0101-01", "本地真实图名")}
        service = CatalogService(repo)
        for _ in range(2):
            service.apply_extract_result({"unit_id": unit["id"], "version_id": vid, "title_blocks": [{
                "drawing_number": "AB-D0101-01", "name": "旧机读", "bbox": {"x0": 1, "y0": .8, "x1": 1, "y1": .95},
                "bbox_clamped": True, "unclear": True}]}, local_reading=reading)
            updated = repo.get("drawing_units", unit["id"])
            assert updated["drawing_number"] == "AB-D0101-01" and updated["name"] == "本地真实图名"
            assert not updated["metadata"]["title_identity_uncertain"]
            assert updated["metadata"]["title_number_resolution"]["status"] == "local_selected"
            assert repo.get("pending_confirmations", pending["id"]) == pending
        assert len(service.list_catalog(vid)) == 1


def test_catalog_volume_identity_requires_its_own_label_heading_and_position():
    from backend.extract.local_sheet import catalog_identity_field
    words = catalog_words() + [word('卷册检索号', .15,.18),word('33-AB123-D0601', .32,.18)]
    result = catalog_identity_field(words + [words[-1]])
    assert result['identity_kind'] == 'catalog_volume'
    assert result['number'] == '33-AB123-D0601'
    assert result['label']['text'] == '卷册检索号'
    assert result['name']['text'] == '图纸文件目录'
    assert result['value']['bbox']['y1'] < .23
    readings = positioned_title_readings(words,result)
    assert readings[0]['field'] == 'catalog_volume_number'
    assert readings[0]['scope'] == 'catalog_identity'
    assert not any(row.get('field') == 'drawing_number' for row in readings)


@pytest.mark.parametrize('fault',['no_heading','no_table','no_label','ambiguous','different_row','row_value_only','unpositioned'])
def test_catalog_volume_identity_never_guesses_sheet_number_or_ambiguous_field(fault):
    from backend.extract.local_sheet import catalog_identity_field
    words = catalog_words() + [word('卷册检索号', .15,.18),word('33-AB123-D0601', .32,.18)]
    if fault == 'no_heading':
        words = [w for w in words if w.text != '图纸文件目录']
    elif fault == 'no_table':
        words = words[-2:]
    elif fault == 'no_label':
        words = [w for w in words if w.text != '卷册检索号']
    elif fault == 'ambiguous':
        words.append(word('33-AB123-D0602',.28,.18))
    elif fault == 'different_row':
        words[-1] = word('33-AB123-D0601',.32,.12)
    elif fault == 'row_value_only':
        words = words[:-1]
    elif fault == 'unpositioned':
        words[-1] = Word('33-AB123-D0601',(0,0,1,1),1.,Source.CAD_TEXT.value)
    assert catalog_identity_field(words) is None
