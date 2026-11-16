from hashlib import sha256
from io import BytesIO
import json

from PIL import Image, ImageDraw
import pytest

from backend.extract.layout_context import LayoutContextError
from backend.extract.table_grid import (
    build_table_transcribe_prompt, locate_table_grid, make_table_sheet, validate_table_transcription,
)


def _png(image):
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _sample(*, missing_bottom=False, merged=False):
    image = Image.new("RGB", (144, 115), "white")
    draw = ImageDraw.Draw(image)
    x_lines = [12, 46, 81, 131]
    y_lines = [10, 35, 60, 85, 105]
    for y in y_lines[:-1] if missing_bottom else y_lines:
        draw.line((x_lines[0], y, x_lines[-1], y), fill="black")
    for x in x_lines:
        if merged and x == x_lines[1]:
            draw.line((x, y_lines[0], x, y_lines[1]), fill="black")
            draw.line((x, y_lines[2], x, y_lines[-1]), fill="black")
        else:
            draw.line((x, y_lines[0], x, y_lines[-1]), fill="black")
    for x, y in ((17, 41), (17, 65), (52, 65)):
        draw.text((x, y), "09", fill="black")
    return image


def test_closed_frame_preserves_every_physical_cell_including_blanks():
    image = _sample()
    result = locate_table_grid(_png(image))
    assert result["outer_boundary_status"] == "closed_candidate"
    assert result["frame_box"] == [12, 10, 132, 106]
    assert result["coverage"] == "unknown" and result["table_title_state"] == "unknown"
    assert len(result["rows"]) == 4 and len(result["columns"]) == 3
    assert [unit["id"] for unit in result["reading_units"]] == [f"r{r}c{c}" for r in range(1, 5) for c in range(1, 4)]
    blank = result["reading_units"][0]
    assert image.crop(blank["pixel_box"]).getextrema() == ((255, 255),) * 3


@pytest.mark.parametrize("case", ["missing_bottom", "cropped_top"])
def test_open_or_cropped_frame_does_not_generate_transcription_units(case):
    image = _sample(missing_bottom=case == "missing_bottom")
    if case == "cropped_top":
        image = image.crop((0, 10, image.width, image.height))
    grid = locate_table_grid(_png(image))
    assert grid["outer_boundary_status"] == "unknown"
    assert grid["frame_box"] is None and grid["reading_units"] == []
    assert {item["side"] for item in grid["boundary_issues"]} == {"bottom" if case == "missing_bottom" else "top"}
    with pytest.raises(LayoutContextError, match="TABLE_FRAME_UNCONFIRMED"):
        build_table_transcribe_prompt(grid)


def test_missing_inner_divider_preserves_row_context_without_invented_cells():
    grid = locate_table_grid(_png(_sample(merged=True)))
    assert grid["outer_boundary_status"] == "closed_candidate"
    row_cells = [cell for cell in grid["cells"] if cell["row_id"] == "r2"]
    assert [cell["state"] for cell in row_cells] == ["unknown", "unknown", "located"]
    assert row_cells[0]["pixel_box"] is None and row_cells[1]["pixel_box"] is None
    row_units = [unit for unit in grid["reading_units"] if unit["row_id"] == "r2"]
    assert [unit["id"] for unit in row_units] == ["r2-context"]
    assert row_units[0]["kind"] == "row_context"
    assert row_units[0]["column_id"] is None


def test_sheet_cell_pixels_are_exact_and_id_labels_are_outside_them():
    image = _sample(merged=True)
    raw = _png(image)
    grid = locate_table_grid(raw)
    sheet_png, manifest = make_table_sheet(raw, grid)
    with Image.open(BytesIO(sheet_png)) as sheet:
        for unit in manifest["units"]:
            source = image.crop(unit["source_pixel_box"])
            observed = sheet.crop(unit["sheet_pixel_box"])
            assert source.size == observed.size
            assert source.tobytes() == observed.tobytes()
            assert sha256(observed.tobytes()).hexdigest() == unit["source_pixel_sha256"]
    assert [unit["id"] for unit in manifest["units"]] == [unit["id"] for unit in grid["reading_units"]]
    assert manifest["source_pixels_resized"] is False
    image.putpixel((0, 0), (0, 0, 0))
    with pytest.raises(LayoutContextError, match="TABLE_SOURCE_IMAGE_CHANGED"):
        make_table_sheet(_png(image), grid)


def test_transcription_keeps_same_value_physical_cells_blank_and_unknown():
    grid = locate_table_grid(_png(_sample()))
    ids = [unit["id"] for unit in grid["reading_units"]]
    texts = ["09", "09", "", None, "同格\n续行"] + [""] * (len(ids) - 5)
    raw = {"readings": [{"id": key, "text": text} for key, text in zip(ids, texts)]}
    assert validate_table_transcription(raw, grid) == raw
    prompt = build_table_transcribe_prompt(grid)
    assert json.loads(prompt.splitlines()[-1]) == ids
    raw["readings"][1], raw["readings"][0] = raw["readings"][0], raw["readings"][1]
    with pytest.raises(LayoutContextError, match="TABLE_READING_ID_ORDER"):
        validate_table_transcription(raw, grid)
