"""Native-pixel straight-line candidates for a preselected table crop."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import re

from PIL import Image, ImageDraw, ImageFont

from backend.extract.layout_context import LayoutContextError


VERSION = "table-ink-grid-v1"
TABLE_TRANSCRIBE_SYSTEM_PROMPT = """第一张图是原图整页，只供了解表名和上下文。第二张图是带固定ID的单元格原像素拼版，只从第二张图逐格转录。标签是程序给的ID，不是表内文字。每个ID只返回对应图块内实际可见文字。保留原符号、空格及同格内换行。只输出一个JSON对象。"""
_INK = re.compile(b"\xff+")


def _long_bands(image):
    """Project consecutive ink runs. A band retains its actual supported runs."""
    length, count = image.size
    mask = image.tobytes()
    short_run = max(12, round(length * .12))
    longest = max(24, round(length * .38))
    total = max(24, round(length * .55))
    groups = []
    for index in range(count):
        pixels = mask[index * length:(index + 1) * length]
        runs = [match.span() for match in _INK.finditer(pixels)]
        long_runs = [run for run in runs if run[1] - run[0] >= short_run]
        lengths = [right - left for left, right in long_runs]
        if not lengths or (max(lengths) < longest and sum(lengths) < total):
            continue
        score = sum(lengths)
        if not groups or index - groups[-1]["band"][1] > 1:
            groups.append({"band": [index, index + 1], "runs": runs, "long_runs": long_runs, "score": score})
        else:
            group = groups[-1]
            group["band"][1] = index + 1
            if score > group["score"]:
                group.update(runs=runs, long_runs=long_runs, score=score)
    for group in groups:
        start, end = group["band"]
        group["position"] = (start + end - 1) // 2
    return groups


def _covered(line, start, end):
    if start >= end:
        return False
    support = sum(max(0, min(end, right) - max(start, left)) for left, right in line["runs"])
    return support > 0 and (end - start) - support <= max(2, round((end - start) * .04))


def _boundary_issues(horizontal, vertical, size):
    width, height = size
    top, bottom = horizontal[0], horizontal[-1]
    left, right = vertical[0], vertical[-1]
    x0, x1 = left["position"], right["position"] + 1
    y0, y1 = top["position"], bottom["position"] + 1
    slack = max(2, round(min(size) * .006))
    issues = []
    for side, edge, limit, first, last in (
        ("top", top, 0, x0, x1), ("bottom", bottom, height, x0, x1),
        ("left", left, 0, y0, y1), ("right", right, width, y0, y1),
    ):
        if limit in edge["band"]:
            issues.append({"side": side, "reason": "touches_crop_edge"})
        if not _covered(edge, first, last):
            issues.append({"side": side, "reason": "outer_line_does_not_close"})

    # An internal separator can look like an outer border when the crop cuts
    # through a row. Opposing long strokes extending beyond it are a witness.
    for side, opposing, index, boundary in (
        ("top", vertical, 0, top["band"][0]), ("bottom", vertical, 1, bottom["band"][1]),
        ("left", horizontal, 0, left["band"][0]), ("right", horizontal, 1, right["band"][1]),
    ):
        endpoints = [run[index] for line in opposing for run in line["long_runs"]]
        extension = boundary - min(endpoints) if index == 0 else max(endpoints) - boundary
        if extension > slack:
            issues.append({"side": side, "reason": "opposing_strokes_extend_past_edge", "extension_pixels": extension})
    return issues


def locate_table_grid(png):
    """Return physical row/column/cell candidates and withhold open-frame crops."""
    with Image.open(BytesIO(png)) as source:
        mask = source.convert("L").point(lambda level: 255 if level < 150 else 0)
    size = list(mask.size)
    horizontal = _long_bands(mask)
    vertical = _long_bands(mask.transpose(Image.Transpose.TRANSPOSE))
    result = {"version": VERSION, "image_sha256": sha256(png).hexdigest(), "image_size": size,
              "horizontal_lines": horizontal, "vertical_lines": vertical,
              "outer_boundary_status": "unknown", "frame_box": None, "candidate_frame_box": None,
              "boundary_issues": [], "table_title_state": "unknown", "coverage": "unknown",
              "quality_status": "unassessed", "rows": [], "columns": [], "cells": [], "reading_units": []}
    if len(horizontal) < 2 or len(vertical) < 2:
        result["boundary_issues"].append({"side": "frame", "reason": "insufficient_straight_lines"})
        return result

    left, right = vertical[0], vertical[-1]
    top, bottom = horizontal[0], horizontal[-1]
    frame = [left["band"][0], top["band"][0], right["band"][1], bottom["band"][1]]
    result["candidate_frame_box"] = frame
    issues = _boundary_issues(horizontal, vertical, size)
    result["boundary_issues"] = issues
    if not issues:
        result.update(outer_boundary_status="closed_candidate", frame_box=frame)
    for column_index, (first, last) in enumerate(zip(vertical, vertical[1:]), 1):
        result["columns"].append({"id": f"c{column_index}", "column_index": column_index - 1,
                                  "pixel_box": [first["band"][1], top["band"][1], last["band"][0], bottom["band"][0]]})
    for row_index, (first, last) in enumerate(zip(horizontal, horizontal[1:]), 1):
        row_id = f"r{row_index}"
        row_box = [left["band"][1], first["band"][1], right["band"][0], last["band"][0]]
        row = {"id": row_id, "row_index": row_index - 1, "pixel_box": row_box, "cell_ids": []}
        cells = []
        for column, left_line, right_line in zip(result["columns"], vertical, vertical[1:]):
            col_id = column["id"]
            x0, _, x1, _ = column["pixel_box"]
            _, y0, _, y1 = row_box
            box = [x0, y0, x1, y1]
            located = all((_covered(first, x0, x1), _covered(last, x0, x1),
                           _covered(left_line, y0, y1), _covered(right_line, y0, y1)))
            cell = {"id": row_id + col_id, "row_id": row_id, "column_id": col_id,
                    "state": "located" if located else "unknown", "pixel_box": box if located else None}
            cells.append(cell)
            row["cell_ids"].append(cell["id"])
        row["state"] = "located" if all(cell["state"] == "located" for cell in cells) else "unknown"
        result["rows"].append(row)
        result["cells"].extend(cells)
        if issues:
            continue
        if row["state"] == "located":
            result["reading_units"].extend({**cell, "kind": "cell"} for cell in cells)
        elif row_box[0] < row_box[2] and row_box[1] < row_box[3]:
            # Preserve the actual physical row for visual review. It supplies
            # no invented columns or merged-cell measurements.
            result["reading_units"].append({"id": row_id + "-context", "kind": "row_context",
                                             "row_id": row_id, "column_id": None, "pixel_box": row_box})
    return result


def _reading_ids(grid):
    if grid.get("outer_boundary_status") != "closed_candidate" or not grid.get("reading_units"):
        raise LayoutContextError("TABLE_FRAME_UNCONFIRMED", "grid")
    ids = [unit["id"] for unit in grid["reading_units"]]
    if len(set(ids)) != len(ids):
        raise LayoutContextError("DUPLICATE_TABLE_UNIT_ID", "grid.reading_units")
    return ids


def build_table_transcribe_prompt(grid):
    ids = _reading_ids(grid)
    example = {"readings": [{"id": "demo-A", "text": "示例字面"}, {"id": "demo-B", "text": ""},
                            {"id": "demo-C", "text": None}]}
    return f"""只转录第二张图中各个ID下的独立图块。空白格用空字符串，读不清用null。保持单元格内的原文和原有换行。相同字面的不同格仍分别输出。带context的ID只读取该整行图块的可见原文，不推断其列或补分格。
合成JSON示例的文字与ID仅演示格式。
{json.dumps(example, ensure_ascii=False)}
根对象只含readings。每项只含id和text。按下方实际顺序返回所有ID，每个恰好一次。
{json.dumps(ids, ensure_ascii=False)}"""


def validate_table_transcription(payload, grid):
    ids = _reading_ids(grid)
    if not isinstance(payload, dict) or payload.keys() != {"readings"}:
        raise LayoutContextError("INVALID_TABLE_TRANSCRIPTION_ROOT", "transcription")
    rows = payload["readings"]
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise LayoutContextError("TABLE_READING_COUNT", "readings")
    output = []
    for index, (row, expected) in enumerate(zip(rows, ids)):
        path = f"readings[{index}]"
        if not isinstance(row, dict) or row.keys() != {"id", "text"}:
            raise LayoutContextError("INVALID_TABLE_READING", path)
        if row["id"] != expected:
            raise LayoutContextError("TABLE_READING_ID_ORDER", path + ".id")
        if row["text"] is not None and not isinstance(row["text"], str):
            raise LayoutContextError("INVALID_TABLE_READING_TEXT", path + ".text")
        output.append({"id": expected, "text": row["text"]})
    return {"readings": output}


def make_table_sheet(png, grid):
    """Place disjoint original pixel blocks below external IDs, without resizing."""
    _reading_ids(grid)
    if sha256(png).hexdigest() != grid["image_sha256"]:
        raise LayoutContextError("TABLE_SOURCE_IMAGE_CHANGED", "grid.image_sha256")
    with Image.open(BytesIO(png)) as image:
        if image.mode not in ("RGB", "L"):
            raise LayoutContextError("UNSUPPORTED_TABLE_PIXEL_MODE", "source_png")
        source = image.copy()

    font = ImageFont.load_default()
    gap, label_height = 12, 20
    column_positions, next_x = {}, gap
    for column in grid["columns"]:
        x0, _, x1, _ = column["pixel_box"]
        longest = f"r{len(grid['rows'])}{column['id']}"
        column_positions[column["id"]] = next_x
        next_x += max(x1 - x0, int(font.getlength(longest)) + 2) + gap

    by_row = {row["id"]: [] for row in grid["rows"]}
    for unit in grid["reading_units"]:
        by_row[unit["row_id"]].append(unit)
    placements, y = [], gap
    width = next_x
    for units in by_row.values():
        height = 0
        for unit in units:
            x = column_positions.get(unit["column_id"], gap)
            x0, y0, x1, y1 = unit["pixel_box"]
            block = source.crop((x0, y0, x1, y1))
            pixel_box = [x, y + label_height, x + block.width, y + label_height + block.height]
            placements.append(({"id": unit["id"], "row_id": unit["row_id"], "column_id": unit["column_id"],
                                "kind": unit["kind"], "source_pixel_box": unit["pixel_box"], "sheet_pixel_box": pixel_box,
                                "source_pixel_sha256": sha256(block.tobytes()).hexdigest()}, block))
            height = max(height, block.height)
            width = max(width, pixel_box[2] + gap)
        y += label_height + height + gap
    sheet = Image.new(source.mode, (width, y), "white")
    draw = ImageDraw.Draw(sheet)
    for item, block in placements:
        x, y = item["sheet_pixel_box"][:2]
        draw.text((x, y - label_height + 2), item["id"], fill="black", font=font)
        sheet.paste(block, (x, y))
    output = BytesIO()
    sheet.save(output, format="PNG")
    payload = output.getvalue()
    manifest = {"version": VERSION, "source_image_sha256": grid["image_sha256"], "source_size": list(source.size),
                "sheet_sha256": sha256(payload).hexdigest(), "sheet_size": list(sheet.size), "source_pixels_resized": False,
                "units": [item for item, _ in placements], "quality_status": "unassessed"}
    return payload, manifest
