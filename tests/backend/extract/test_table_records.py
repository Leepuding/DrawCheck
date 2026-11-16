from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from backend.extract.table_grid import locate_table_grid
from backend.extract.table_records import bind_table_records


def _table(headers, *rows):
    width = 38 * len(headers) + 20
    height = 30 * (len(rows) + 1) + 20
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(len(headers) + 1):
        x = 10 + index * 38
        draw.line((x, 10, x, height - 10), fill="black")
    for index in range(len(rows) + 2):
        y = 10 + index * 30
        draw.line((10, y, width - 10, y), fill="black")
    payload = BytesIO()
    image.save(payload, format="PNG")
    grid = locate_table_grid(payload.getvalue())
    readings = {"readings": [{"id": unit["id"], "text": text}
                             for unit, text in zip(grid["reading_units"], [value for row in (headers, *rows) for value in row])]}
    assert grid["outer_boundary_status"] == "closed_candidate"
    assert len(grid["reading_units"]) == len(headers) * (len(rows) + 1)
    return grid, readings


def test_reordered_header_aliases_bind_physical_tuples_without_merging_rows():
    headers = ["备 注", "工程 数量", "材料名称", "规格型号", "单位", "序号"]
    grid, readings = _table(headers,
                            ["", "", "甲区", "", "", "一、"],
                            ["利旧8台", "2.50", "设备甲", "24口", " 台 ", "1"],
                            ["利旧8台", "2.50", "设备甲", "24口", " 台 ", "2"],
                            ["", "", "乙区", "", "", "二、"],
                            ["利旧8台", "2.50", "设备甲", "24口", " 台 ", "1"])
    result = bind_table_records(grid, readings)
    assert result["binding_status"] == "candidate"
    assert result["header_bindings"]["quantity"]["text"] == "工程 数量"
    assert result["header_bindings"]["quantity"]["matched_label"] == "工程数量"
    assert len(result["rows"]) == 6
    first, left, duplicate, second, right = result["records"]
    assert first["kind"] == second["kind"] == "section_candidate"
    assert left["field_tuple"] == duplicate["field_tuple"] == right["field_tuple"] == ["设备甲", "24口", " 台 ", "2.50", "利旧8台"]
    assert left["quantity_value"] == duplicate["quantity_value"] == right["quantity_value"] == "2.50"
    assert left["row_ref"] != duplicate["row_ref"] != right["row_ref"]
    assert left["section_ref"]["row_id"] == duplicate["section_ref"]["row_id"] == "r2"
    assert right["section_ref"]["row_id"] == "r5"
    assert [item["cell_id"] for item in left["source_tuple"]] == ["r3c3", "r3c4", "r3c5", "r3c2", "r3c1"]
    assert all(item["image_sha256"] == grid["image_sha256"] and item["pixel_box"] for item in left["source_tuple"])
    assert result["identity_confirmed"] is False and all(not row["identity_confirmed"] for row in result["records"])


def test_missing_optional_header_is_distinct_from_blank_and_unread_cells():
    grid, readings = _table(["设备名称", "单位", "数量"], ["样品甲", "", None])
    result = bind_table_records(grid, readings)
    assert result["binding_status"] == "candidate"
    assert result["missing_headers"] == ["model", "remarks"]
    row = result["records"][0]
    assert row["field_tuple"] == ["样品甲", None, "", None, None]
    assert row["field_states"] == ["read", "missing_header", "blank", "unknown", "missing_header"]
    assert row["source_tuple"][1] is None and row["source_tuple"][4] is None
    assert row["source_tuple"][2]["cell_id"] == "r2c2" and row["source_tuple"][3]["cell_id"] == "r2c3"


@pytest.mark.parametrize(("headers", "code"), [
    (["名称", "单位", "数量", "总重量"], "UNKNOWN_HEADER"),
    (["名称", "单位", "数量", "工程数量"], "DUPLICATE_HEADER"),
    (["名称", "单位"], "REQUIRED_HEADER_MISSING"),
    (["名称", "单位", "数量", None], "UNKNOWN_HEADER"),
])
def test_unrecognized_duplicate_or_missing_required_header_leaves_rows_unbound(headers, code):
    grid, readings = _table(headers, [""] * len(headers))
    result = bind_table_records(grid, readings)
    assert result["binding_status"] == "header_unbound" and result["records"] == []
    assert len(result["rows"]) == 2
    assert code in {error["code"] for error in result["header_errors"]}


def test_unknown_quantity_or_unit_does_not_create_a_section_and_breaks_previous_context():
    grid, readings = _table(["序号", "构件名称", "单位", "数量"],
                            ["一、", "甲区", "", ""], ["1", "构件甲", "件", "3"],
                            ["二、", "可能分区", None, ""], ["1", "构件甲", "件", "3"])
    first, item, unknown, later = bind_table_records(grid, readings)["records"]
    assert first["kind"] == "section_candidate"
    assert item["section_ref"]["row_id"] == "r2"
    assert unknown["kind"] == "unbound_section" and unknown["section_ref"] is None
    assert later["section_ref"] is None


def test_repair_r5_unread_ordinal_cannot_attach_later_rows_to_previous_section():
    grid, readings = _table(["序号", "构件名称", "单位", "数量"],
                            ["一、", "甲区", "", ""], [None, "构件甲", "件", "3"],
                            [None, "可能分区", "", ""], ["1", "构件甲", "件", "3"],
                            ["三、", "丙区", "", ""], ["1", "构件甲", "件", "3"])
    first, item, unknown, later, third, restored = bind_table_records(grid, readings)["records"]
    assert item["section_ref"] == first["row_ref"]
    assert unknown["kind"] == "unbound_section" and unknown["section_ref"] is None
    assert unknown["field_tuple"] == ["可能分区", None, "", "", None]
    assert later["section_ref"] is None and later["kind"] == "item_candidate"
    assert restored["section_ref"] == third["row_ref"]
    assert [row["quantity_value"] for row in (item, later, restored)] == ["3", "3", "3"]


@pytest.mark.parametrize("literal", ["200米", "其中200", "200/300", "2×3", " 200 ", "2O0", "200\n300"])
def test_quantity_requires_a_complete_literal_from_its_own_column(literal):
    grid, readings = _table(["名称", "型号", "单位", "数量", "备注"], ["样品甲", "24口", "件", literal, "利旧19件"])
    row = bind_table_records(grid, readings)["records"][0]
    assert row["quantity_value"] is None and row["quantity_state"] == "unparseable"
    assert row["field_tuple"][3] == literal
