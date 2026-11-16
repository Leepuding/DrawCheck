"""Positioned evidence for a printed catalogue's total and volume header."""
from __future__ import annotations

import re

from backend.store import StoreError
from backend.store.originals import sha256_file
from backend.store.paths import safe_path

_REVISION = 2
PRINTED_SHEET_TOTAL = re.compile(r"(?:本卷册总计图纸|图纸合计|本册图纸共)(?P<count>[0-9]{1,4})张(?:打印件.*本?)?$")
_TOTAL_LABELS = {"本卷册总计图纸", "图纸合计", "本册图纸共"}
SHEET_TOTAL_TEMPLATES = {label + "张" for label in _TOTAL_LABELS}
_SHEET_UNIT = re.compile(r"张(?:打印件.*本?)?$")
_VOLUME_LABELS = {"卷册检索号", "卷册编号", "卷册号", "卷号"}


def positioned_header(words, header_y):
    from backend.extract.local_sheet import _CATALOG_TITLES, _number, _same_line, compact
    from backend.references import sheet_number

    words = [w for w in words if w.score >= .85 and w.box[3] < header_y]
    headings = [w for w in words if compact(w.text) in _CATALOG_TITLES]
    if len(headings) != 1:
        return [], None

    def right_of(label, word, distance):
        # OCR may slightly overlap adjacent boxes. Require ordered centers and
        # do not borrow a number from the preceding '本次新出' or following row.
        return (word.x > label.box[2] and -.012 <= word.box[0] - label.box[2] <= distance
                and _same_line(label, word))

    totals, volumes = [], []
    for label in words:
        raw = compact(label.text)
        if match := PRINTED_SHEET_TOTAL.fullmatch(raw):
            totals.append({"value": int(match["count"]), "label": label.evidence(),
                           "values": [label.evidence()]})
        elif raw in _TOTAL_LABELS:
            candidates = [w for w in words if right_of(label, w, .085)]
            for w in candidates:
                num = compact(w.text)
                if not re.fullmatch(r"[0-9]{1,4}", num):
                    continue
                units = [u for u in words if right_of(w, u, .06) and _SHEET_UNIT.fullmatch(compact(u.text))]
                if units:
                    unit = min(units, key=lambda u: u.x)
                    totals.append({"value": int(num), "label": label.evidence(),
                                   "values": [w.evidence()], "unit": unit.evidence()})
        if raw in _VOLUME_LABELS:
            for w in words:
                number = _number(w.text)
                if right_of(label, w, .18) and sheet_number(number):
                    volumes.append({"identity_kind": "catalog_volume", "number": number,
                                    "label": label.evidence(), "value": w.evidence(),
                                    "heading": headings[0].evidence(), "name": headings[0].evidence()})
    return totals, volumes[0] if len(volumes) == 1 else None


def has_complete_rows(catalog):
    """Use the same printed count and independent row proof for fresh and saved readings."""
    from backend.extract.local_sheet import compact

    totals = catalog.get("total_fields", [])
    counts = {f["value"] for f in totals}
    printed_total = False
    for field in totals:
        label = compact((field.get("label") or {}).get("text") or "")
        unit = compact((field.get("unit") or {}).get("text") or "")
        match = PRINTED_SHEET_TOTAL.fullmatch(label)
        if match and int(match["count"]) == field["value"]:
            printed_total = True
        elif label in SHEET_TOTAL_TEMPLATES or (label in _TOTAL_LABELS and _SHEET_UNIT.fullmatch(unit)):
            values = field.get("values") or []
            printed_total |= (len(values) == 1 and compact(values[0].get("text") or "") == str(field["value"]))
    pagination = {(f["page"], f["pages"]) for f in catalog.get("pagination_fields", [])}
    rows = catalog["rows"]
    vertical_bands = sorted((row["value"]["bbox"]["y0"], row["value"]["bbox"]["y1"]) for row in rows)
    separate_rows = all(0 <= top < bottom <= 1 for top, bottom in vertical_bands) and all(
        previous[1] <= following[0] for previous, following in zip(vertical_bands, vertical_bands[1:]))
    return (bool(rows) and printed_total and pagination == {(1, 1)} and counts == {len(rows)}
            and catalog.get("physical_row_count") == catalog.get("read_row_count") == len(rows)
            and catalog.get("unread_rows") == [] and separate_rows)


def complete_reading(reading, words):
    catalog = reading["catalog"]
    totals, volume = positioned_header(words, catalog["header"]["bbox"]["y0"])
    refreshed = {**catalog, "total_fields": [*catalog.get("total_fields", []), *totals],
                 "header_volume": volume or catalog.get("header_volume"), "coverage_revision": _REVISION}
    return {**reading, "catalog": {**refreshed, "complete": has_complete_rows(refreshed)}}


def recover_catalog_header(store, unit, reading, *, get_engine):
    catalog = reading.get("catalog")
    if (unit.get("kind") != "pdf_page" or not catalog or not catalog.get("rows")
            or catalog.get("coverage_revision") == _REVISION):
        return reading
    if catalog.get("complete"):
        if has_complete_rows(catalog):
            return {**reading, "catalog": {**catalog, "coverage_revision": _REVISION}}
        reading = {**reading, "catalog": {**catalog, "complete": False}}
    meta = unit.get("metadata") or {}
    path = meta.get("review_relative_path") or meta.get("preview_relative_path")
    if not path or not reading.get("image_sha"):
        return reading
    try:
        from PIL import Image
        from backend.extract.local_sheet import _image_words
        image_path = safe_path(store.root, path)
        # Do not attach a header from a different render to cached row evidence.
        if sha256_file(image_path) != reading["image_sha"]:
            return reading
        edge = catalog["header"]["bbox"]["y0"] - .006
        if not .08 < edge < .65:
            return reading
        with Image.open(image_path) as image:
            words = _image_words(get_engine(), image, (.09, .035, .96, edge))
        return complete_reading(reading, words)
    except (ImportError, OSError, StoreError, RuntimeError, ValueError):
        return reading
