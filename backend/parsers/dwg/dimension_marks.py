"""Read native linear dimensions on a verifiably shared CAD block."""
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import math
import re
import unicodedata

from backend.store.models import Source

from .frames import normalized_box
from .render import rendered_text_bounds


_LENGTH = re.compile(r"(?P<number>(?:\d+(?:\.\d+)?|\.\d+))\s*(?P<unit>毫米|厘米|米|mm|cm|m)?", re.I)
_UNITS = {"毫米": "mm", "厘米": "cm", "米": "m"}


def _point(value):
    return tuple(round(float(part), 6) + 0.0 for part in value)


def _line_basis(block):
    # Compare linework independent of handle order and displayed dimension values.
    # One line or a small repeated symbol does not establish a shared drawing base.
    lines = set()
    directions = set()
    for line in block.query("LINE"):
        a, b = sorted((_point(line.dxf.start), _point(line.dxf.end)))
        if not all(math.isfinite(v) for v in (*a, *b)):
            return None
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx or dy:
            directions.add(round(math.atan2(dy, dx) % math.pi, 6))
            lines.add((line.dxf.layer.casefold(), a, b))
    if len(lines) < 8 or len(directions) < 2:
        return None
    basis = (_point(block.block.dxf.base_point), sorted(lines))
    return sha256(repr(basis).encode("utf-8")).hexdigest()


def _mark_key(dimension):
    dxf = dimension.dxf
    if dimension.dimtype not in (0, 1) or not dxf.hasattr("defpoint2") or not dxf.hasattr("defpoint3"):
        return None
    a, b = sorted((_point(dxf.defpoint2), _point(dxf.defpoint3)))
    if a[:2] == b[:2] or not all(math.isfinite(v) for v in (*a, *b)):
        return None
    angle = dxf.get("angle", 0.0) if dimension.dimtype == 0 else 0.0
    if not math.isfinite(angle):
        return None
    return dimension.dimtype, a, b, round(angle % 180, 6)


def _visible(entity):
    layer = entity.doc.layers.get(entity.dxf.layer) if entity.dxf.layer in entity.doc.layers else None
    return not entity.dxf.invisible and (layer is None or not (layer.is_off() or layer.is_frozen()))


def _literal(text):
    return unicodedata.normalize("NFKC", text).strip().lower()


def read_shared_dimensions(layout, frame_bounds):
    """Return per-frame rows; unitless glyphs remain unitless in the stored evidence."""
    from ezdxf.math import Vec2, is_convex_polygon_2d, is_point_in_polygon_2d
    from ezdxf.xclip import XClip

    rows = {bounds: [] for bounds in frame_bounds}
    if not layout.is_modelspace or not rows:
        return rows
    inserts = layout.query("INSERT")
    counts = Counter(e.dxf.name.casefold() for e in layout.doc.entitydb.values() if e.dxftype() == "INSERT")
    candidates = []
    for insert in inserts:
        name = insert.dxf.name
        if not name or name.startswith(("*", "A$C")) or counts[name.casefold()] != 1 or insert.mcount != 1:
            continue
        block = insert.block()
        if block is None or block.block.dxf.flags & 12 or not _visible(insert):
            continue
        dimensions = block.query("DIMENSION")
        if not dimensions:
            continue
        if any(insert.dxf.get(key, 1.0) != 1.0 for key in ("xscale", "yscale", "zscale")):
            continue
        if not insert.dxf.extrusion.isclose((0.0, 0.0, 1.0), abs_tol=1e-12):
            continue
        clip = XClip(insert)
        polygon = []
        if clip.is_clipping_enabled:
            polygon = list(clip.get_wcs_clipping_path().vertices)
            if clip.is_inverted_clip or not is_convex_polygon_2d(polygon):
                continue
        basis = _line_basis(block)
        if basis is None:
            continue
        keys = [_mark_key(dimension) for dimension in dimensions]
        key_counts = Counter(keys)
        transform = insert.matrix44()
        for dimension, key in zip(dimensions, keys):
            if key is None or key_counts[key] != 1 or not _visible(dimension):
                continue
            override = dimension.dxf.get("text", "")
            if override and not override.strip():
                continue
            # Use the literal in the original anonymous dimension graphics, with
            # its insertion transform. The geometry length cannot supply the text.
            texts = [item for item in dimension.virtual_entities() if item.dxftype() in {"TEXT", "MTEXT"}]
            if len(texts) != 1 or not _visible(texts[0]):
                continue
            graphic = texts[0]
            shown = graphic.plain_text()
            number = _LENGTH.fullmatch(_literal(shown))
            if not number or (override and "<>" not in override and _literal(override) != _literal(shown)):
                continue
            graphic.transform(transform)
            mark = {"block": name, "line_basis_sha256": basis, "key": repr(key),
                    "insert_handle": insert.dxf.handle, "dimension_handle": dimension.dxf.handle}
            endpoints = [tuple(transform.transform(p))[:2] for p in (dimension.dxf.defpoint2, dimension.dxf.defpoint3)]
            candidates.append((graphic, shown, number, mark, polygon, endpoints))
    if not candidates:
        return rows

    boxes, _ = rendered_text_bounds(layout.doc, [row[0] for row in candidates])
    for (_, shown, number, mark, polygon, endpoints), box in zip(candidates, boxes):
        if box is None:
            continue
        x0, y0, x1, y1 = box
        points = [*endpoints, (x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if polygon and any(is_point_in_polygon_2d(Vec2(point), polygon) != 1 for point in points):
            continue
        containing = [b for b in rows if all(b[0] <= x <= b[2] and b[1] <= y <= b[3] for x, y in points)]
        if len(containing) != 1:
            continue
        bounds = containing[0]
        unit = number.group("unit") or ""
        rows[bounds].append({
            "category": "尺寸", "value": number.group("number"), "unit": _UNITS.get(unit, unit),
            "label": "", "datum": "", "method": "DWG 底图尺寸标注", "shared_block_mark": mark,
            "evidence": {"text": shown, "source": Source.CAD_TEXT.value, "confidence": 1.0,
                         "bbox": asdict(normalized_box(bounds, box))},
        })
    return rows
