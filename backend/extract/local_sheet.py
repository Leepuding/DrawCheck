"""Local, positioned readings for sheet identities and ruled schedules."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from backend.references import sheet_number
from backend.store import Source, StoreError
from backend.store.originals import sha256_file
from backend.store.paths import safe_path

from . import engineering
from .table_records import HEADER_FIELDS
from .table_grid import locate_table_grid


VERSION = "rapidocr-ppocr6-engineering-v8-positioned-units"
_WEIGHTS = {
    "Det": ("PP-OCRv6_det_small.onnx", "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f"),
    "Cls": ("ch_ppocr_mobile_v2.0_cls_mobile.onnx", "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"),
    "Rec": ("PP-OCRv6_rec_small.onnx", "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"),
}
_ID_LABELS = {"图号", "图纸编号", "图纸号", "图纸代号"}
_CATALOG_TITLES = {"图纸目录", "图纸文件目录", "图纸清单", "图纸文件清单", "目录"}
_NAME_HEADERS = {"图名", "图纸名称", "文件名称", "名称"}
_SERIAL = re.compile(r"\d{1,4}\Z")
_PAGINATION = re.compile(r"第(\d+)页共(\d+)页")
_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_SECTION = re.compile(r"[一二三四五六七八九十]+[.、]?\Z")


def compact(text):
    return "".join(unicodedata.normalize("NFKC", text).split())


def _number(text):
    return re.sub(r"\s*([-_./])\s*", r"\1", unicodedata.normalize("NFKC", text).strip())


@dataclass(frozen=True)
class Word:
    text: str
    box: tuple[float, float, float, float]
    score: float
    source: str

    @property
    def x(self):
        return (self.box[0] + self.box[2]) / 2

    @property
    def y(self):
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self):
        return self.box[3] - self.box[1]

    def evidence(self):
        return {"text": self.text, "source": self.source, "confidence": self.score,
                "bbox": dict(zip(("x0", "y0", "x1", "y1"), self.box))}


def _same_line(first, second):
    return abs(first.y - second.y) <= max(first.height, second.height) * .65


def _named(words):
    return [word for word in words if re.search(r"[\u3400-\u9fff]{3}", word.text)]


def title_field(words):
    words = _unique_positioned_words(words)
    candidates = []
    for label in words:
        raw_label = compact(label.text)
        inline = re.fullmatch(r"(?:图号|图纸编号|图纸号|图纸代号)[:：]?(.+)", raw_label)
        if inline and sheet_number(inline[1]) and label.score >= .85:
            candidates.append((label, label, inline[1]))
        if raw_label not in _ID_LABELS or label.score < .70:
            continue
        for value in words:
            number = _number(value.text)
            if (value.score >= .84 and sheet_number(number) and _same_line(label, value)
                    and label.box[2] <= value.box[0] and value.box[0] - label.box[2] < .18):
                candidates.append((label, value, number))
    unique = {(value.box, number): (label, value, number) for label, value, number in candidates}
    if len(unique) != 1:
        return None
    label, value, number = next(iter(unique.values()))
    # Title names are typically centred in the field directly above the number.
    names = [word for word in _named(words)
             if .014 < value.y - word.y < .09 and abs(word.x - value.x) < .075
             and word.score >= .78 and compact(word.text) not in {"设计阶段", "施工图", "图纸名称"}]
    name = min(names, key=lambda word: value.y - word.y, default=None)
    return {"number": number, "value": value.evidence(), "label": label.evidence(),
            "name": name.evidence() if name else None}


def catalog_identity_field(words):
    """Read a catalogue's explicitly labelled volume identifier, never a sheet ID.

    A native catalogue heading + table headers establish the document kind. The
    volume label/value must be unique, above the table and in one bounded field;
    catalogue row contents, filenames and expected prefixes never supply a code.
    """
    words = _unique_positioned_words(words)
    catalog = catalog_rows(words)
    if not catalog:
        return None
    header_y = catalog["header"]["bbox"]["y0"]
    headings = [word for word in words if compact(word.text) in _CATALOG_TITLES
                and word.score >= .85 and word.box[3] < header_y]
    if len(headings) != 1:
        return None
    labels = {"卷册检索号", "卷册编号", "卷册号", "卷号"}
    boundaries = labels | {"版次", "版号", "版本", "打印件", "本卷册总计图纸张"}
    candidates = []
    for label in words:
        if compact(label.text) not in labels or label.score < .85 or label.y >= header_y:
            continue
        right = min([word.box[0] for word in words if word is not label
                     and compact(word.text) in boundaries and _same_line(label, word)
                     and word.box[0] > label.box[2]], default=1.)
        right = min(right, label.box[2] + max(6*(label.box[2]-label.box[0]), 8*label.height))
        for value in words:
            number = _number(value.text)
            if (value.score < .9 or not re.fullmatch(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*", number)
                    or not any(c.isalpha() for c in number) or not any(c.isdigit() for c in number)
                    or not _same_line(label, value) or value.box[0] < label.box[2]
                    or value.box[2] > right or value.box[0] - label.box[2] >= .18):
                continue
            candidates.append((label, value, number))
    unique = {(value.box, number): (label, value, number) for label, value, number in candidates}
    if len(unique) != 1:
        return None
    label, value, number = next(iter(unique.values()))
    return {"identity_kind": "catalog_volume", "number": number,
            "value": value.evidence(), "label": label.evidence(),
            "name": headings[0].evidence(), "heading": headings[0].evidence()}


def _unique_positioned_words(words):
    """Deduplicate renditions at one physical anchor, retaining repeated text elsewhere."""
    selected = []
    for word in sorted(words, key=lambda word: word.source == Source.VISION.value):
        if not (0 <= word.box[0] < word.box[2] <= 1 and 0 <= word.box[1] < word.box[3] <= 1):
            continue
        duplicate = False
        for prior in selected:
            if compact(prior.text) != compact(word.text):
                continue
            overlap = (max(0., min(prior.box[2], word.box[2]) - max(prior.box[0], word.box[0])) *
                       max(0., min(prior.box[3], word.box[3]) - max(prior.box[1], word.box[1])))
            area = min((prior.box[2]-prior.box[0]) * prior.height,
                       (word.box[2]-word.box[0]) * word.height)
            if overlap > .6 * area:
                duplicate = True
                break
        if not duplicate:
            selected.append(word)
    return selected


def positioned_title_readings(words, title):
    """Independent original readings inside a successfully labelled title region.

    A repeated literal at a different anchor stays ambiguous. These readings are
    not human decisions, and never replace the historical machine records.
    """
    if not title:
        return []
    if title.get("identity_kind") == "catalog_volume":
        return [{**title["value"], "scope": "catalog_identity", "field": "catalog_volume_number",
                 "identity_kind": "catalog_volume", "candidate_count": 1,
                 "label_evidence": title["label"], "heading_evidence": title["heading"],
                 "reader": "local_ocr" if title["value"]["source"] == Source.VISION.value else "positioned_native_text"}]
    words = _unique_positioned_words([word for word in words if word.x > .38 and word.y > .72])
    counts = {}
    for word in words:
        counts[compact(word.text)] = counts.get(compact(word.text), 0) + 1
    labelled = {}
    aliases = {"项目": "project_name", "项目名称": "project_name", "工程名称": "project_name",
               "设计阶段": "drawing_type", "阶段": "drawing_type"}
    for label in words:
        field = aliases.get(compact(label.text))
        if not field or label.score < .85:
            continue
        candidates = [value for value in words if value is not label and value.score >= .9
                      and compact(value.text) not in aliases and 0 <= value.box[1] - label.box[3] < .04
                      and label.box[0] - .01 <= value.box[0] <= label.box[2] + .04]
        if len(candidates) == 1:
            labelled.setdefault(field, []).append((candidates[0], label))
    result = []
    for word in words:
        evidence = word.evidence()
        item = {**evidence, "scope": "title_region", "candidate_count": counts[compact(word.text)],
                "reader": "local_ocr" if word.source == Source.VISION.value else "positioned_native_text"}
        for field, value in (("drawing_number", title.get("value")), ("drawing_name", title.get("name"))):
            if value and compact(word.text) == compact(value["text"]) and counts[compact(word.text)] == 1:
                item["field"] = field
                item["label_evidence"] = title["label"] if field == "drawing_number" else None
        for field, candidates in labelled.items():
            if len(candidates) == 1 and candidates[0][0] is word:
                item["field"] = field
                item["label_evidence"] = candidates[0][1].evidence()
        result.append(item)
    return result


def _header(words, aliases, required):
    for anchor in words:
        if aliases.get(compact(anchor.text)) != required[0] or anchor.score < .65:
            continue
        fields = {}
        duplicates = set()
        for word in words:
            field = aliases.get(compact(word.text))
            if not field or word.score < .65 or not _same_line(anchor, word):
                continue
            if field in fields:
                duplicates.add(field)
            fields[field] = word
        if all(field in fields for field in required) and not duplicates:
            return fields
    return None


def _cell_words(words, headers, anchor):
    by_field = {field: [] for field in headers}
    cols = sorted(headers.items(), key=lambda item: item[1].x)
    header_y = max(header.y for header in headers.values())
    for word in words:
        if word.y <= header_y or not _same_line(anchor, word):
            continue
        field, _ = min(cols, key=lambda pair: abs(pair[1].x - word.x))
        by_field[field].append(word)
    return by_field


def _single(words, min_score=.65):
    return words[0] if len(words) == 1 and words[0].score >= min_score else None


def catalog_rows(words):
    from backend.catalog.coverage import PRINTED_SHEET_TOTAL, SHEET_TOTAL_TEMPLATES, has_complete_rows

    words = _unique_positioned_words(words)
    headings = [word for word in words if compact(word.text) in _CATALOG_TITLES and word.y < .3]
    if not headings:
        return None
    aliases = {label: "number" for label in _ID_LABELS} | {label: "name" for label in _NAME_HEADERS}
    headers = _header(words, aliases | {"序号": "serial"}, ("serial", "number", "name"))
    if not headers or headers["number"].x >= headers["name"].x:
        return None
    serial = headers["serial"]
    serial_anchors = [word for word in words if word.y > serial.y + serial.height
                      and abs(word.x - serial.x) < .035 and _SERIAL.fullmatch(compact(word.text))]
    rows, unread_rows = [], []
    for anchor in sorted(serial_anchors, key=lambda word: word.y):
        cells = _cell_words(words, headers, anchor)
        numbers = [word for word in cells["number"] if sheet_number(_number(word.text))]
        number = _single(numbers, .82)
        if number is None:
            unread_rows.append({"serial": compact(anchor.text), "serial_evidence": anchor.evidence(),
                                "reason": "此物理行没有唯一可读的图号"})
            continue
        names = [word for word in _named(cells["name"]) if word.x < headers["name"].x + .16]
        name = _single(names, .75)
        rows.append({"serial": compact(anchor.text), "number": _number(number.text),
                     "value": number.evidence(), "name": name.evidence() if name else None})
    # A declared total plus a fully seen list is stronger than the last row
    # detected by OCR. Physical duplicates remain separate entries.
    total_fields, pagination_fields = [], []
    for word in words:
        if word.y >= serial.y or word.score < .8:
            continue
        text = compact(word.text)
        if match := PRINTED_SHEET_TOTAL.fullmatch(text):
            total_fields.append({"value": int(match["count"]), "label": word.evidence(), "values": [word.evidence()]})
        elif text in SHEET_TOTAL_TEMPLATES:
            candidates = [value for value in words if value is not word and value.score >= .8
                          and _SERIAL.fullmatch(compact(value.text)) and _same_line(word, value)
                          and word.box[0] <= value.box[0] < value.box[2] <= word.box[2]]
            if len(candidates) == 1:
                total_fields.append({"value": int(compact(candidates[0].text)), "label": word.evidence(),
                                     "values": [candidates[0].evidence()]})
        if match := _PAGINATION.search(text):
            pagination_fields.append({"page": int(match[1]), "pages": int(match[2]),
                                      "label": word.evidence(), "values": [word.evidence()]})
        elif text == "第页共页":
            candidates = sorted([value for value in words if value is not word and value.score >= .8
                                 and _SERIAL.fullmatch(compact(value.text)) and _same_line(word, value)
                                 and word.box[0] <= value.box[0] < value.box[2] <= word.box[2]], key=lambda value: value.x)
            if len(candidates) == 2 and candidates[0].box[2] < candidates[1].box[0]:
                pagination_fields.append({"page": int(compact(candidates[0].text)),
                                          "pages": int(compact(candidates[1].text)), "label": word.evidence(),
                                          "values": [value.evidence() for value in candidates]})
    catalog = {"rows": rows, "header": headers["number"].evidence(),
               "physical_row_count": len(serial_anchors), "read_row_count": len(rows), "unread_rows": unread_rows,
               "total_fields": total_fields, "pagination_fields": pagination_fields}
    return {**catalog, "complete": has_complete_rows(catalog)}


def material_rows(words, *, grid):
    headers = _header(words, HEADER_FIELDS, ("serial", "name", "unit", "quantity"))
    if not headers or not grid:
        return None

    def interval_index(intervals, value):
        return next((index for index, (low, high) in enumerate(intervals) if low < value < high), None)

    columns, bands = grid
    header_rows = {interval_index(bands, word.y) for word in headers.values()}
    column_fields = {interval_index(columns, word.x): field for field, word in headers.items()}
    if len(header_rows) != 1 or None in header_rows or None in column_fields or len(column_fields) != len(headers):
        return None
    header_row, = header_rows
    by_row = {}
    for word in words:
        column, row = interval_index(columns, word.x), interval_index(bands, word.y)
        if column not in column_fields or row is None or row <= header_row:
            continue
        x0, y0, x1, y1 = word.box
        left, right = columns[column]
        top, bottom = bands[row]
        if (min(x1, right) - max(x0, left) < .8 * (x1 - x0)
                or min(y1, bottom) - max(y0, top) < .8 * (y1 - y0)):
            continue
        by_row.setdefault(row, {}).setdefault(column_fields[column], []).append(word)

    rows, section = [], None
    for row, cells in sorted(by_row.items()):
        anchor = _single(cells.get("serial", []), .55)
        if not anchor:
            continue
        serial = compact(anchor.text)
        fields = {field: value.evidence() if (value := _single(cells.get(field, []), .50)) else None
                  for field in ("name", "model", "unit", "quantity", "remarks")}
        if _SECTION.fullmatch(serial):
            if fields["name"] and not fields["quantity"]:
                section = fields["name"]
            continue
        if not _SERIAL.fullmatch(serial):
            continue
        quantity = fields["quantity"]
        if not (quantity and _AMOUNT.fullmatch(compact(quantity["text"]))):
            fields["quantity"] = None
        # A serial alone does not distinguish body rows from the surrounding drawing.
        if fields["name"] or fields["unit"] and fields["quantity"]:
            rows.append({"row": row, "serial": anchor.evidence(), "section": section, "fields": fields})
    return {"rows": rows, "headers": {field: word.evidence() for field, word in headers.items()}} if rows else None


def _image_words(engine, image, region):
    import numpy as np

    width, height = image.size
    x0, y0, x1, y1 = (round(scale * value) for scale, value in zip((width, height, width, height), region))
    crop = image.crop((x0, y0, x1, y1)).convert("RGB")
    lo, hi = crop.convert("L").getextrema()
    if hi - lo < 6:
        return []
    pixels = np.asarray(crop)[:, :, ::-1].copy()
    result = engine(pixels)
    if result.txts is None:
        return []
    words = []
    for text, points, score in zip(result.txts, result.boxes, result.scores):
        lo, hi = points.min(axis=0), points.max(axis=0)
        box = (max(0., (x0 + lo[0]) / width), max(0., (y0 + lo[1]) / height),
               min(1., (x0 + hi[0]) / width), min(1., (y0 + hi[1]) / height))
        if text.strip() and math.isfinite(float(score)) and box[0] < box[2] and box[1] < box[3]:
            words.append(Word(text, tuple(float(edge) for edge in box), float(score), Source.VISION.value))
    return words



def _read_detected_line(engine, image, box):
    """Read a detector's original rectangle without estimating character boxes."""
    import numpy as np

    bounds = tuple(round(scale * value) for scale, value in zip(image.size * 2, box))
    line = image.crop(bounds).convert("RGB")
    for scale in (1., 1.15):
        sample = line if scale == 1. else line.resize((round(line.width * scale), line.height), Image.Resampling.BICUBIC)
        pixels = np.asarray(sample)[:, :, ::-1].copy()
        # RapidOCR stores call options on the instance. Restore the standard
        # detection/classification settings for the next page observation.
        try:
            result = engine(pixels, use_det=False, use_cls=False)
        finally:
            engine.update_params(use_det=True, use_cls=True)
        if result.txts and len(result.txts) == 1 and result.scores:
            yield result.txts[0], float(result.scores[0]), scale

def _closed_grid(image, region):
    width, height = image.size
    bounds = tuple(round(scale * value) for scale, value in zip((width, height, width, height), region))
    with BytesIO() as buf:
        image.crop(bounds).save(buf, format="PNG")
        grid = locate_table_grid(buf.getvalue())
    if grid["outer_boundary_status"] != "closed_candidate":
        return None
    columns = [(bounds[0] + line["position"]) / width for line in grid["vertical_lines"]]
    rows = [(bounds[1] + line["position"]) / height for line in grid["horizontal_lines"]]
    return list(zip(columns, columns[1:])), list(zip(rows, rows[1:]))


def _native_words(path):
    if path is None:
        return []
    import pdfplumber
    from pdfminer.pdfexceptions import PDFException

    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) != 1:
                return []
            page = pdf.pages[0]
            x0, y0, _, _ = page.bbox
            words = []
            for word in page.extract_words():
                box = ((word["x0"] - x0) / page.width, (word["top"] - y0) / page.height,
                       (word["x1"] - x0) / page.width, (word["bottom"] - y0) / page.height)
                if 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1:
                    words.append(Word(word["text"], box, 1., Source.PDF_TEXT.value))
            return words
    except PDFException:
        return []


def _may_have_native_text(path):
    """Skip the detailed PDF text layout on known text-free vector pages."""
    try:
        from pypdfium2 import PdfDocument, PdfiumError
    except ImportError:
        return True
    try:
        with PdfDocument(path) as doc:
            page = doc[0]
            try:
                text_page = page.get_textpage()
                try:
                    return text_page.count_chars() > 0
                finally:
                    text_page.close()
            finally:
                page.close()
    except (IndexError, OSError, ValueError, PdfiumError):
        return True


def _engine():
    from onnxruntime import disable_telemetry_events
    from rapidocr import RapidOCR
    import rapidocr

    disable_telemetry_events()
    weights = Path(rapidocr.__file__).with_name("models")
    paths = {}
    for part, (name, expected) in _WEIGHTS.items():
        path = weights / name
        if not path.is_file() or sha256_file(path) != expected:
            raise OSError("RapidOCR bundled model file missing or changed")
        paths[f"{part}.model_path"] = str(path)
    return RapidOCR(params={**paths, "Global.log_level": "error", "Global.text_score": .45,
        "Det.limit_side_len": 1536, "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1})


class LocalSheetReader:
    def __init__(self, store):
        self.store = store
        self.engine = None
        self.cad_words = {}
        self.available = importlib.util.find_spec("rapidocr") is not None

    def read_import_title(self, unit, *, scan=False):
        """Read only the labelled title field; import never runs a full review."""
        meta = unit.get("metadata") or {}
        try:
            if unit["kind"] == "dwg_frame":
                native = self._cad_unit_words(unit)
            else:
                relative = meta.get("page_relative_path")
                page = safe_path(self.store.root, relative) if relative else None
                native = _native_words(page) if page and _may_have_native_text(page) else []
            positioned = [word for word in native if word.x > .38 and word.y > .72]
            title = title_field(positioned) or catalog_identity_field(native)
            image_path = meta.get("review_relative_path") or meta.get("preview_relative_path")
            if title or not scan or not self.available or not image_path:
                return title
            with Image.open(safe_path(self.store.root, image_path)) as image:
                if min(image.size) >= 450:
                    if self.engine is None:
                        self.engine = _engine()
                    return title_field(positioned + _image_words(self.engine, image, (.38, .72, 1, 1)))
        except (ImportError, OSError, ValueError, RuntimeError, StoreError):
            return None

    def read_unit(self, unit_id, *, catalog_hint=False, material_hint=False):
        unit = self.store.get("drawing_units", unit_id)
        meta = unit.get("metadata") or {}
        path = meta.get("review_relative_path") or meta.get("preview_relative_path")
        is_cad = unit["kind"] == "dwg_frame"
        if not path:
            return self._read_cad(unit) if is_cad else None
        try:
            image_path = safe_path(self.store.root, path)
            if not image_path.is_file():
                return self._read_cad(unit) if is_cad else None
            pdf_path = safe_path(self.store.root, meta["page_relative_path"]) if meta.get("page_relative_path") else None
            image_sha = sha256_file(image_path)
            pdf_sha = sha256_file(pdf_path) if pdf_path and pdf_path.is_file() else None
            previous = meta.get("local_sheet") or {}
            identity = {"version": VERSION, "image_sha": image_sha, "pdf_sha": pdf_sha,
                        "catalog_hint": catalog_hint, "material_hint": material_hint}
            if is_cad:
                identity["native_evidence_sha256"] = hashlib.sha256(json.dumps(
                    {key: meta.get(key) or [] for key in ("native_measurements", "native_signals", "native_connections")},
                    sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if all(previous.get(key) == value for key, value in identity.items()):
                return previous
            with Image.open(image_path) as image:
                if min(image.size) < 450:
                    return self._read_cad(unit) if is_cad else None
                try:
                    native = self._cad_unit_words(unit) if is_cad else _native_words(pdf_path if pdf_sha else None)
                except (ImportError, OSError, ValueError, TypeError):
                    native = []
                title_words = [word for word in native if word.x > .38 and word.y > .72]
                title = title_field(title_words)
                if not title and self.available:
                    if self.engine is None:
                        self.engine = _engine()
                    title_words += _image_words(self.engine, image, (.38, .72, 1, 1))
                    title = title_field(title_words)
                caption = compact((title.get("name") or {}).get("text", "")) if title else ""
                catalog = catalog_rows(native)
                if not title:
                    title = catalog_identity_field(native)
                if self.available and not catalog and (catalog_hint or not title or "目录" in caption):
                    if self.engine is None:
                        self.engine = _engine()
                    catalog = catalog_rows(_image_words(self.engine, image, (.04, .025, .97, .83)))
                materials = None
                if material_hint or any(key in caption for key in ("材料表", "设备表", "设备清单", "材料清单")):
                    region = (.10, .07, .88, .81)
                    grid = _closed_grid(image, region)
                    if grid:
                        materials = material_rows(native, grid=grid)
                        if not materials and self.available:
                            if self.engine is None:
                                self.engine = _engine()
                            materials = material_rows(_image_words(self.engine, image, region), grid=grid)
                extra = self._read_cad(unit, caption) if is_cad else {}
                if self.available and unit["kind"] == "pdf_page" and not catalog and not materials:
                    extra = self._read_engineering(image, pdf_path if pdf_sha else None, native, caption)
                observations = extra.get("engineering", {}).get("measurements", [])
                if pole := engineering.pole_heights(observations, materials, caption):
                    extra.setdefault("engineering", {}).setdefault("measurements", []).extend(pole)
                return {**identity, "title": title, "title_readings": positioned_title_readings(title_words, title),
                        "catalog": catalog, "materials": materials, **extra}
        except (ImportError, OSError, ValueError, RuntimeError, StoreError):
            return self._read_cad(unit) if is_cad else None

    def _cad_unit_words(self, unit):
        version = unit["version_id"]
        if version not in self.cad_words:
            by_unit = {}
            for fact in self.store.list_records("facts", version_id=version):
                if fact["source"] == Source.CAD_TEXT.value and fact["x0"] < fact["x1"] and fact["y0"] < fact["y1"]:
                    box = tuple(fact[key] for key in ("x0", "y0", "x1", "y1"))
                    by_unit.setdefault(fact["unit_id"], []).append(Word(fact["text"], box, 1., fact["source"]))
            self.cad_words[version] = by_unit
        return self.cad_words[version].get(unit["id"], [])

    def _read_cad(self, unit, caption=""):
        from backend.rules.unit_declaration import local_declaration_evidence

        words = self._cad_unit_words(unit)
        native = (unit.get("metadata") or {}).get("native_measurements") or []
        if not caption:
            names = {word.text for word in words if word.y > .84 and word.x > .54 and "一次接线图" in word.text}
            if len(names) == 1:
                caption = names.pop()
        catalog = catalog_rows(words)
        title = (title_field([word for word in words if word.x > .38 and word.y > .72])
                 or catalog_identity_field(words))
        return {**({"catalog": catalog} if catalog else {}),
                **({"title": title, "title_readings": positioned_title_readings(words, title)} if title else {}),
                "engineering": {"measurements": list(native) + engineering.read_values(words),
                                "links": engineering.stated_links(words), "assets": engineering.stated_assets(words, caption),
                                "signals": (unit.get("metadata") or {}).get("native_signals") or [],
                                "connections": (unit.get("metadata") or {}).get("native_connections") or [],
                                "unit_declarations": local_declaration_evidence(words),
                                "cables": engineering.cad_cable_labels(words, caption,
                                    native_connections=(unit.get("metadata") or {}).get("native_connections") or ())}}

    def _read_engineering(self, image, pdf_path, native, caption):
        from backend.rules.unit_declaration import local_declaration_evidence

        if any(marker in caption for marker in ("目录", "材料表", "设备清单", "设备表")):
            return {}
        aspect, segments, rectangles = engineering.page_geometry(pdf_path)
        if not segments:
            aspect, segments = engineering.raster_segments(image)
        if self.engine is None:
            self.engine = _engine()
        words = list(native)
        for x in (.02, .32, .62):
            for y in (.025, .40):
                region = (x, y, min(.99, x+.38), min(.875, y+.475))
                words.extend(_image_words(self.engine, image, region))
        guides = engineering.elevation_guides(segments)
        for x, y, right in guides[:40]:
            region = (max(0.,x-.035), max(0.,y/aspect-.026), min(1.,right+.029), min(1.,y/aspect+.003))
            words.extend(_image_words(self.engine, image, region))
        words = engineering.unique_words(words)
        links, assets = engineering.optical_links(words, segments=segments, rectangles=rectangles,
                                                  aspect=aspect, caption=caption)
        measurements = [] if assets else engineering.read_values(
            words, segments=segments, aspect=aspect, guides=guides, graphical="说明" not in caption)
        return {"engineering": {"measurements": measurements, "unit_declarations": local_declaration_evidence(words),
                                "links": links + engineering.stated_links(words), "assets": assets}}
