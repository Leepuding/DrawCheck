"""Bind literal cell readings to candidate records from their physical headers."""
from __future__ import annotations

from decimal import Decimal
import re

from backend.extract.layout_context import LayoutContextError
from backend.extract.table_grid import validate_table_transcription


VERSION = "table-records-v1"
HEADER_FIELDS = {"名称": "name", "材料名称": "name", "设备名称": "name", "构件名称": "name",
                 "型号": "model", "规格": "model", "规格型号": "model", "单位": "unit",
                 "数量": "quantity", "工程数量": "quantity", "备注": "remarks", "序号": "serial"}
TUPLE_FIELDS = ("name", "model", "unit", "quantity", "remarks")
REQUIRED_FIELDS = ("name", "unit", "quantity")
_QUANTITY = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")
_CHINESE_ORDINAL = r"[一二三四五六七八九十百零〇两]+"
_SECTION = re.compile(rf"(?:{_CHINESE_ORDINAL}[、．.]?|（{_CHINESE_ORDINAL}）|\({_CHINESE_ORDINAL}\))\Z")


def _source(grid, row_id, unit):
    return {"image_sha256": grid["image_sha256"], "row_id": row_id, "column_id": unit.get("column_id"),
            "cell_id": unit["id"] if unit.get("kind") != "row_context" else None,
            "reading_id": unit["id"], "pixel_box": unit["pixel_box"]}


def _physical_rows(grid, readings):
    columns = [column["id"] for column in grid["columns"]]
    row_ids = [row["id"] for row in grid["rows"]]
    if len(set(columns)) != len(columns) or len(set(row_ids)) != len(row_ids):
        raise LayoutContextError("DUPLICATE_PHYSICAL_ID", "grid")
    cells = {}
    for cell in grid["cells"]:
        key = (cell["row_id"], cell["column_id"])
        if key in cells:
            raise LayoutContextError("DUPLICATE_PHYSICAL_CELL", "grid.cells")
        cells[key] = cell
    if cells.keys() != {(row, column) for row in row_ids for column in columns}:
        raise LayoutContextError("PHYSICAL_CELL_SET_MISMATCH", "grid.cells")
    contexts = {unit["row_id"]: unit for unit in grid["reading_units"] if unit["kind"] == "row_context"}
    rows = []
    for row in grid["rows"]:
        row_id = row["id"]
        row_cells = []
        for column in columns:
            cell = cells[row_id, column]
            row_cells.append({"text": readings.get(cell["id"]), "transcribed": cell["id"] in readings,
                              "geometry_state": cell["state"], "source": _source(grid, row_id, cell)})
        context = contexts.get(row_id)
        row_reading = {"text": readings[context["id"]], "source": _source(grid, row_id, context)} if context else None
        rows.append({"row_ref": {"image_sha256": grid["image_sha256"], "row_id": row_id,
                                 "row_index": row["row_index"], "pixel_box": row["pixel_box"]},
                     "geometry_state": row["state"], "cells": row_cells, "row_context": row_reading})
    return rows


def _header_bindings(header):
    bindings, errors = {}, []
    for cell in header["cells"]:
        text, source = cell["text"], cell["source"]
        compact = "".join(character for character in text if not character.isspace()) if text is not None else ""
        field = HEADER_FIELDS.get(compact) if cell["transcribed"] and cell["geometry_state"] == "located" else None
        if field is None:
            errors.append({"code": "UNKNOWN_HEADER", "cell_id": source["cell_id"]})
        elif field in bindings:
            errors.append({"code": "DUPLICATE_HEADER", "field": field, "cell_id": source["cell_id"]})
        else:
            bindings[field] = {"text": text, "matched_label": compact, "source": source}
    for field in REQUIRED_FIELDS:
        if field not in bindings:
            errors.append({"code": "REQUIRED_HEADER_MISSING", "field": field})
    return bindings, errors


def _quantity(text):
    if text is None:
        return None, "unknown"
    if text == "":
        return None, "blank"
    if not _QUANTITY.fullmatch(text):
        return None, "unparseable"
    return format(Decimal(text), "f"), "parsed"


def bind_table_records(grid, transcription):
    """Return source-aligned tuples for each row. The first row is the header candidate."""
    raw = validate_table_transcription(transcription, grid)
    rows = _physical_rows(grid, {row["id"]: row["text"] for row in raw["readings"]})
    result = {"version": VERSION, "binding_status": "header_unbound", "quality_status": "unassessed",
              "identity_confirmed": False, "coordinate_frame": "grid_source_png_pixels_top_left",
              "tuple_fields": list(TUPLE_FIELDS), "rows": rows, "records": [], "header_bindings": {},
              "missing_headers": [], "header_errors": []}
    if not rows or rows[0]["geometry_state"] != "located":
        result["header_errors"].append({"code": "HEADER_ROW_UNBOUND"})
        return result
    header, errors = _header_bindings(rows[0])
    result.update(header_bindings=header, missing_headers=[field for field in TUPLE_FIELDS if field not in header],
                  header_errors=errors)
    if errors:
        return result

    result["binding_status"] = "candidate"
    section_ref = None
    for row in rows[1:]:
        record = {"row_ref": row["row_ref"], "section_ref": None, "kind": "unbound_row_context",
                  "field_tuple": None, "source_tuple": None, "field_states": None, "serial": None, "quantity_value": None,
                  "quantity_state": "unknown", "identity_confirmed": False}
        result["records"].append(record)
        if row["geometry_state"] != "located":
            section_ref = None
            continue

        by_column = {cell["source"]["column_id"]: cell for cell in row["cells"]}
        fields = {field: by_column[item["source"]["column_id"]] for field, item in header.items()}
        literals = [fields[field]["text"] if field in fields else None for field in TUPLE_FIELDS]
        sources = [{**fields[field]["source"], "header_cell_id": header[field]["source"]["cell_id"]}
                   if field in fields else None for field in TUPLE_FIELDS]
        states = ["missing_header" if field not in fields else "unknown" if text is None else "blank" if text == "" else "read"
                  for field, text in zip(TUPLE_FIELDS, literals)]
        name, _, unit, quantity, _ = literals
        serial = fields.get("serial")
        ordinal = serial["text"] if serial else None
        section_number = bool(ordinal and _SECTION.fullmatch(ordinal))
        name_visible = isinstance(name, str) and bool(name.strip())
        numeric, quantity_state = _quantity(quantity)
        unknown_section = (section_number and (unit is None or quantity is None or (unit == "" and quantity == "")))
        # An unread ordinal can hide a new section. A readable item unit
        # and quantity can still distinguish an ordinary material row.
        unread_ordinal = serial is not None and ordinal is None and (numeric is None or not (unit or "").strip())
        kind = "item_candidate" if name_visible else "unbound_item"
        if section_number and unit == "" and quantity == "" and name_visible:
            kind = "section_candidate"
            section_ref = row["row_ref"]
        elif unknown_section or unread_ordinal:
            kind = "unbound_section"
            section_ref = None
        record.update(section_ref=section_ref, kind=kind, field_tuple=literals, source_tuple=sources,
                      field_states=states, serial=serial, quantity_value=numeric, quantity_state=quantity_state)
    return result
