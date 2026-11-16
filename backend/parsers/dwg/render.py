"""ezdxf → matplotlib PNG；按图框裁切，PNG 与文字归一化共用同一范围。

字体：不随产品分发 SHX/商业字体。缺失时用本机已安装、经字形自检可用的中文字体替代，
并如实写明缺哪些字体、替代成了什么、中文是否可读。
"""
import logging
from copy import copy
import hashlib
import math
from dataclasses import dataclass
from weakref import WeakKeyDictionary
from pathlib import Path
import warnings

# 按偏好排列的本机中文字体文件名（ezdxf 字体管理器以文件名索引）。只用系统已安装的，不下载、不打包。
CJK_FONT_FILES = (
    "simhei.ttf", "msyh.ttc", "msyh.ttf", "simsun.ttc", "simkai.ttf", "simfang.ttf",
    "NotoSansCJK-Regular.ttc", "NotoSansSC-Regular.otf", "NotoSansCJKsc-Regular.otf",
    "NotoSerifCJK-Regular.ttc", "SourceHanSansSC-Regular.otf",
    "wqy-microhei.ttc", "wqy-zenhei.ttc", "PingFang.ttc", "STHeiti Medium.ttc", "Arial Unicode.ttf",
)
_TTF_SUFFIXES = (".ttf", ".ttc", ".otf")
_PROBE = "中文图纸"


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0x3000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF or 0x20000 <= code <= 0x2FA1F)


def _cmap_of(font_name: str):
    """返回 ezdxf 实际会用来出字形的字体 cmap；非 TrueType（如 SHX）或读取失败返回 None。"""
    from ezdxf.fonts import fonts

    if not font_name.lower().endswith(_TTF_SUFFIXES):
        return None
    try:
        return fonts.font_manager.get_ttf_font(font_name).getBestCmap() or {}
    except Exception:
        return None


def missing_glyphs(font_name: str, chars) -> set:
    """字形自检：字体 cmap 中没有的字符集合（cmap 不可读时视为全部缺失）。"""
    if not font_name.lower().endswith(_TTF_SUFFIXES):
        return set()  # 本机确有的 SHX 等非 TrueType 字体无法查 cmap，按原字体信任，不强行替换
    cmap = _cmap_of(font_name)
    if cmap is None:
        return set(chars)
    return {ch for ch in chars if ord(ch) not in cmap}


def find_cjk_font():
    """在 ezdxf 字体管理器已登记的本机字体中，挑第一个通过「中文图纸」字形自检的。

    返回 (文件名, FontFace) 或 None。注意必须用 ezdxf 登记的 FontFace（带真实 family），
    用 FontFace(filename=...) 会被 ezdxf 按 family 重新匹配到无中文字形的默认字体，中文变 □。
    """
    from ezdxf.fonts import fonts

    for name in CJK_FONT_FILES:
        if fonts.font_manager.has_font(name) and not missing_glyphs(name, _PROBE):
            face = fonts.font_manager.get_font_face(name)
            if fonts.font_manager.find_font_name(face).lower() == name.lower():
                return name, face
    return None


class _FontWarnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        message = record.getMessage()
        if any(word in message.lower() for word in ("font", "glyph", "shx")):
            self.messages.append("字体缺失或替换，仅部分提取：" + message[:200])


def _style_texts(doc):
    from ezdxf.lldxf.validator import make_table_key

    chars = {}
    for entity in doc.entitydb.values():
        kind = entity.dxftype()
        if kind not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            continue
        try:
            text = entity.plain_text() if kind == "MTEXT" else entity.dxf.get("text", "")
        except Exception:
            text = ""
        key = make_table_key(entity.dxf.get("style", "Standard"))
        chars.setdefault(key, set()).update(ch for ch in text if _is_cjk(ch))
    return chars


def _all_style_chars(doc):
    from ezdxf.lldxf.validator import make_table_key
    result = {}
    for entity in doc.entitydb.values():
        if entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            continue
        try:
            text = entity.plain_text()
        except Exception:
            text = entity.dxf.get("text", "")
        result.setdefault(make_table_key(entity.dxf.get("style", "Standard")), set()).update(
            char for char in text if char not in "\r\n\t")
    return result


def _bigfont_header(font_name: str) -> bool:
    """Inspect only the named local shape font, never a manager fallback font."""
    from ezdxf.fonts import fonts

    path = Path(font_name)
    if path.suffix.lower() not in {".shx", ".shp"}:
        return False
    from .fonts import exact_font_path
    candidates = [path]
    exact = exact_font_path(font_name)
    if exact:
        candidates.append(exact)
    for candidate in candidates:
        try:
            with Path(candidate).open("rb") as stream:
                header = stream.read(4096)
        except (OSError, ValueError):
            continue
        if header.startswith(b"AutoCAD-86 bigfont "):
            return True
        if any(line.strip().upper().split(b",", 1)[0].split()[:1] == [b"*BIGFONT"]
               for line in header.splitlines()):
            return True
    return False


def bigfont_diagnostics(doc, used_styles=None):
    """Confirm BIGFONT from STYLE's dedicated slot or the actual file header.

    A missing SHX name or a filename containing 'bigfont' is not format evidence.
    These source-level limitations remain even when CJK substitution makes a PNG.
    """
    from ezdxf.lldxf.validator import make_table_key
    from .fonts import exact_font_path, font_for_style, font_evidence, FontUnsupported

    used_styles = set(_style_texts(doc)) if used_styles is None else used_styles
    style_chars = _all_style_chars(doc)
    diagnostics = []
    for style in doc.styles:
        if make_table_key(style.dxf.name) not in used_styles:
            continue
        for slot in ("font", "bigfont"):
            font = style.dxf.get(slot, "").strip()
            if not font:
                continue
            evidence = "STYLE.bigfont" if slot == "bigfont" else "font_header" if _bigfont_header(font) else None
            if evidence is None:
                continue
            name = Path(font.replace("\\", "/")).name
            checks = style_chars.get(make_table_key(style.dxf.name), set())
            try:
                local_font = font_for_style(style)
                if local_font is None:
                    raise FontUnsupported("primary font is not SHX")
                failures = {}
                for char in sorted(checks):
                    try:
                        local_font.glyph_cache.glyph(char)
                    except (FontUnsupported, ValueError, IndexError) as exc:
                        failures[char] = str(exc)
            except (FontUnsupported, ValueError, OSError, IndexError) as exc:
                failures = {"font": str(exc)}
            else:
                if slot == "bigfont" and exact_font_path(font) is None:
                    if not failures:
                        diagnostics.append({"code": "FONT_BIGFONT_NOT_USED", "style": style.dxf.name,
                            "font": name, "evidence": "primary_font_bytecode_and_used_glyphs",
                            "resolved": True, "checked_characters": len(checks),
                            "local_files": font_evidence(style), "native_cad_compared": False,
                            "reason": (f"样式「{style.dxf.name}」的现有字符均由本机同名主字体描画，"
                                       f"无需「{name}」的字形")})
                    else:
                        diagnostics.append({"code": "FONT_BIGFONT_MISSING", "style": style.dxf.name,
                            "font": name, "evidence": evidence, "resolved": False,
                            "checked_characters": len(checks), "failed_characters": failures,
                            "reason": f"缺大字体「{name}」（样式「{style.dxf.name}」），部分字符需要替代"})
                    continue
                diagnostics.append({"code": "FONT_LOCAL_SHX_RENDERED", "style": style.dxf.name,
                    "font": name, "evidence": "local_font_bytecode_and_used_glyphs",
                    "resolved": not failures, "checked_characters": len(checks),
                    "failed_characters": failures, "local_files": font_evidence(style),
                    "native_cad_compared": False,
                    "reason": (f"本机同名 SHX/BIGFONT 已实际描画（样式「{style.dxf.name}」，"
                               f"逐字符检查 {len(checks)} 字，缺失或不支持 {len(failures)} 字）；"
                               "与制图方原字库是否相同、原生 CAD 显示保真仍未核验")})
                continue
            reason = (f"BIGFONT 字体格式不受当前 ezdxf 渲染链支持（文字样式「{style.dxf.name}」，"
                      f"字体「{name}」，依据：{evidence}）；补齐字体文件仍不能证明预览与原图保真，"
                      "字形与字宽可能不同，覆盖完整性待核对")
            diagnostics.append({"code": "FONT_FORMAT_UNSUPPORTED_BIGFONT", "style": style.dxf.name,
                                "font": name, "evidence": evidence, "reason": reason})
    return diagnostics



@dataclass
class _LayoutRecording:
    """Shared immutable drawing primitives; valid while the imported document is read-only."""
    player: object
    bounds: list
    warnings: list
    supported: bool
    selected_count: int = 0


# Values contain drawing primitives, not layouts/documents, so imported documents can be collected.
_RECORDINGS = WeakKeyDictionary()


def _record_bounds(record):
    """Bounds of the actual rendered geometry, including text outlines and Bezier control points.

    Unlike ezdxf.bbox's approximate TEXT boxes, these bounds describe exactly the
    geometry passed to Matplotlib. Empty/unsupported/non-finite bounds are unknown:
    retain those records rather than interpreting them as off-page.
    """
    try:
        box = record.bbox()
        if not box.has_data:
            return None
        values = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
        return values if all(math.isfinite(v) for v in values) else None
    except Exception:
        return None


def _layout_recording(doc, layout, context, font_signature, handler):
    from ezdxf.addons.drawing.recorder import (
        Recorder, PointsRecord, SolidLinesRecord, PathRecord, FilledPathsRecord, ImageRecord,
    )

    # Import workers never edit drawing geometry between frames. Counts additionally
    # invalidate common additions/removals; in-place edits require a new import/doc.
    key = (layout.layout_key, len(layout), len(doc.entitydb), font_signature)
    cache = _RECORDINGS.setdefault(doc, {})
    existing = cache.get(key)
    if existing is not None:
        return existing
    recorder = Recorder()
    warning_start = len(handler.messages)
    _frontend(context, recorder).draw_layout(layout, finalize=True)
    player = recorder.player()
    known_types = (PointsRecord, SolidLinesRecord, PathRecord, FilledPathsRecord, ImageRecord)
    recording = _LayoutRecording(player, [_record_bounds(r) for r in player.records],
        list(handler.messages[warning_start:]), all(type(r) in known_types for r in player.records))
    # Keep one recording per layout/font setup; obsolete count signatures do not grow unbounded.
    for old_key in list(cache):
        if old_key[0] == key[0]:
            del cache[old_key]
    cache[key] = recording
    return recording


def _cjk_mtext_renderer(context, pipeline, mtext, properties):
    """MTEXT layout with CJK break opportunities and the actual drawing font.

    Layout construction adapted from ezdxf AbstractMTextRenderer (MIT,
    Copyright (c) 2021-2025 Manfred Moitzi). See third-party/font-bigfont.md.
    The stock renderer treats a whole Chinese sentence as an unbreakable word.
    """
    import re
    from ezdxf.addons.drawing.mtext_complex import ComplexMTextRenderer
    from ezdxf.render.abstract_mtext_renderer import (
        defined_width, make_default_tab_stops, new_paragraph, column_heights, super_glue,
    )
    from ezdxf.tools import text_layout as tl
    from ezdxf.tools.text import MTextParser, TokenType, valid_text_height

    class MeasuredFont:
        def __init__(self, ctx):
            self.ctx = ctx
            self.font = pipeline.text_engine.get_font(ctx.font_face)

        def text_width(self, text):
            return self.font.text_width_ex(text, self.ctx.cap_height, self.ctx.width_factor)

        def space_width(self):
            return self.font.space_width() * self.ctx.cap_height * self.ctx.width_factor

    def words(text):
        # Keep Latin model/unit tokens intact. A Chinese character is a line-break
        # opportunity; closing punctuation stays with its preceding character.
        parts = re.findall(r"[\u3400-\u9fff\uf900-\ufaff]|[^\u3400-\u9fff\uf900-\ufaff]+", text)
        result = []
        closing = "，。；：！？、）】》〉」』％℃°"
        for part in parts:
            if result and part and part[0] in closing:
                result[-1] += part
            else:
                result.append(part)
        return result

    class CJKRenderer(ComplexMTextRenderer):
        def get_font(self, ctx):
            return MeasuredFont(ctx)

        def layout_engine(self, entity):
            height = valid_text_height(entity.dxf.char_height)
            spacing = entity.dxf.line_spacing_factor
            width = defined_width(entity)
            stops = make_default_tab_stops(height, width)
            layout = tl.Layout(width=width)
            background = self.make_bg_renderer(entity)
            if entity.has_columns:
                columns = entity.columns
                for column_height in column_heights(columns):
                    layout.append_column(width=columns.width, height=column_height,
                                         gutter=columns.gutter_width, renderer=background)
            else:
                layout.append_column(renderer=background)
            ctx = self.make_mtext_context(entity)
            cells = []

            def paragraph():
                layout.append_paragraphs([new_paragraph(
                    cells, ctx, height, spacing, width, stops)])
                cells.clear()

            for token in MTextParser(entity.all_columns_raw_content(), ctx):
                ctx = token.ctx
                kind = token.type
                if kind == TokenType.NEW_PARAGRAPH:
                    paragraph()
                elif kind == TokenType.NEW_COLUMN:
                    paragraph()
                    layout.next_column()
                elif kind == TokenType.SPACE:
                    cells.append(self.space(ctx))
                elif kind == TokenType.NBSP:
                    cells.append(self.non_breaking_space(ctx))
                elif kind == TokenType.TABULATOR:
                    cells.append(self.tabulator(ctx))
                elif kind == TokenType.WORD:
                    pieces = words(token.data)
                    if cells and isinstance(cells[-1], (tl.Text, tl.Fraction)):
                        cells.append(super_glue())
                    for index, piece in enumerate(pieces):
                        if index:
                            cells.append(tl.Space(width=0, min_width=0, max_width=0))
                        cells.append(self.word(piece, ctx))
                elif kind == TokenType.STACK:
                    if cells and isinstance(cells[-1], (tl.Text, tl.Fraction)):
                        cells.append(super_glue())
                    cells.append(self.fraction(token.data, ctx))
            if cells:
                paragraph()
            return layout

    renderer = CJKRenderer(context, pipeline, properties)
    layout = renderer.layout_engine(mtext)
    layout.place(align=tl.LayoutAlignment(mtext.dxf.attachment_point))
    layout.render(mtext.ucs().matrix)


def _frontend(context, backend):
    from ezdxf.addons.drawing import Frontend

    class ReliableFrontend(Frontend):
        def _font_properties(self, entity, properties):
            face = properties.font
            local = getattr(context, "_drawcheck_fonts", {}).get(face.filename if face else "")
            if local is None:
                return properties
            try:
                for char in entity.plain_text():
                    if char not in "\r\n\t":
                        local.glyph_cache.glyph(char)
                return properties
            except (ValueError, IndexError):
                fallback = find_cjk_font()
                if fallback:
                    properties = copy(properties)
                    properties.font = fallback[1]
                logging.getLogger("ezdxf").warning(
                    "SHX glyph/MTEXT unsupported; entity %s uses explicit fallback font",
                    entity.dxf.get("handle", "virtual"))
                return properties

        def draw_text_entity(self, entity, properties):
            super().draw_text_entity(entity, self._font_properties(entity, properties))

        def draw_mtext_entity(self, entity, properties):
            super().draw_mtext_entity(entity, self._font_properties(entity, properties))

        def draw_complex_mtext(self, entity, properties):
            _cjk_mtext_renderer(context, self.pipeline, entity, properties)

        def draw_simple_mtext(self, entity, properties):
            # CJK wrapping is required even when MTEXT has no formatting codes.
            _cjk_mtext_renderer(context, self.pipeline, entity, properties)

        def draw_composite_entity(self, entity, properties):
            if entity.dxftype() == "INSERT" and entity.block() is None:
                # Missing reference has no recoverable geometry. Keep its ATTRIBs
                # and the remaining layout; find_frames records the coverage gap.
                inserts = entity.multi_insert() if entity.mcount > 1 else (entity,)
                for insert in inserts:
                    self.draw_entities(insert.attribs)
                return
            super().draw_composite_entity(entity, properties)

    frontend = ReliableFrontend(context, backend)
    # ezdxf's default engine is a class singleton. Isolate this import's custom
    # font mappings, so concurrent documents cannot contaminate each other.
    from ezdxf.addons.drawing.unified_text_renderer import UnifiedTextRenderer
    frontend.pipeline.text_engine = UnifiedTextRenderer()
    frontend.pipeline.text_engine._font_cache.update(
        {name.lower(): font for name, font in getattr(context, "_drawcheck_fonts", {}).items()})
    return frontend


def _select_records(recording, bounds, *, dpi=100):
    """Keep complete crossing records; axes perform the final clip without flattening curves."""
    from ezdxf.addons.drawing.recorder import Player

    x0, y0, x1, y1 = bounds
    width, height = x1-x0, y1-y0
    aspect = height / width
    inch_width, inch_height = ((16, max(1.0, 16*aspect)) if aspect <= 1
                               else (max(1.0, 16/aspect), 16))
    player = recording.player
    # Geometry bboxes exclude stroke width. Preserve a full maximum lineweight plus
    # four pixels of antialias/point extent beyond every frame edge.
    max_weight = max((abs(p.lineweight) for p in player.properties.values()), default=0)
    scale = abs(player.config.lineweight_scaling)
    points = max(max_weight * scale, player.config.min_lineweight or 72/dpi)
    pad_x = (points/72 + 4/dpi) * width/inch_width
    pad_y = (points/72 + 4/dpi) * height/inch_height
    def intersects(box):
        if box is None:
            return True
        a, b, c, d = box
        return not (c < x0-pad_x or a > x1+pad_x or d < y0-pad_y or b > y1+pad_y)
    selected = Player()
    selected.config = player.config
    selected.background = player.background
    selected.properties = player.properties
    selected.records = [record for record, box in zip(player.records, recording.bounds) if intersects(box)]
    selected.has_shared_recordings = True
    recording.selected_count = len(selected.records)
    return selected

def _render_context(doc):
    """Build the font context shared by image rendering and actual text bounds."""
    from ezdxf.addons.drawing import RenderContext
    from ezdxf.fonts import fonts
    from ezdxf.lldxf.validator import make_table_key
    from .fonts import exact_font_path, font_for_style, FontUnsupported, font_evidence

    reasons = []
    style_chars = _style_texts(doc)
    used_styles = set(style_chars)
    diagnostics = bigfont_diagnostics(doc, used_styles)
    primary_only = {make_table_key(row["style"]) for row in diagnostics if row["code"] == "FONT_BIGFONT_NOT_USED"}
    reasons.extend(row["reason"] for row in diagnostics if row["code"] != "FONT_BIGFONT_NOT_USED")
    missing_fonts = {}
    for style in doc.styles:
        key = make_table_key(style.dxf.name)
        if key not in used_styles:
            continue
        for slot in ("font", "bigfont"):
            if slot == "bigfont" and key in primary_only:
                continue
            font = style.dxf.get(slot, "")
            if font and exact_font_path(font) is None:
                missing_fonts.setdefault(key, []).append(font.replace("\\", "/").rsplit("/", 1)[-1])
    cjk = find_cjk_font()
    context = RenderContext(doc)
    context._drawcheck_fonts = {}
    for style in doc.styles:
        key = make_table_key(style.dxf.name)
        if key not in used_styles:
            continue
        try:
            local = font_for_style(style)
        except (FontUnsupported, OSError, ValueError, IndexError):
            continue
        if local is not None:
            token = "drawcheck-" + hashlib.sha256(repr(font_evidence(style)).encode()).hexdigest() + ".shx"
            context._drawcheck_fonts[token] = local
            context.fonts[key] = fonts.FontFace(filename=token)
    unreadable = set()
    substituted_styles = set()
    glyph_substitutions = []
    for key, chars in style_chars.items():
        face = context.fonts.get(key)
        if face and face.filename in context._drawcheck_fonts:
            # The frontend checks every glyph and falls back only for entities
            # that actually need it. The generic font manager has no SHX token.
            continue
        effective = fonts.font_manager.find_font_name(face) if face else fonts.font_manager.fallback_font_name()
        lacking = missing_glyphs(effective, chars) if chars else set()
        if cjk and (key in missing_fonts or lacking):
            fallback_name, fallback_face = cjk
            fallback_lacking = missing_glyphs(fallback_name, chars) if chars else set()
            # Preserve the current face when the candidate loses known glyphs.
            if fallback_lacking < lacking or (key in missing_fonts and fallback_lacking == lacking):
                context.fonts[key] = fallback_face
                if key in missing_fonts:
                    substituted_styles.add(key)
                elif fallback_name.casefold() != effective.casefold():
                    glyph_substitutions.append(key)
                lacking = fallback_lacking
        unreadable |= lacking
    if missing_fonts:
        unavailable = "、".join(dict.fromkeys(font for names in missing_fonts.values() for font in names))
        reason = f"缺字体（产品不随包提供 SHX/商业字体）：{unavailable}"
        if substituted_styles:
            affected = "、".join(key for key in missing_fonts if key in substituted_styles)
            reason += f"；文字样式「{affected}」已用本机 {cjk[0]} 替代，字形与字宽和原图不同"
        if missing_fonts.keys() - substituted_styles:
            if cjk is None and unreadable:
                reason += "；本机无可用中文字体，未能替代"
            reason += "；缺字体且未完成替代的样式，其原字形与字宽未核验"
        reasons.append(reason)
    if glyph_substitutions:
        affected = "、".join(glyph_substitutions)
        reasons.append(f"文字样式「{affected}」的字体缺少所需中文字形，已用本机 {cjk[0]} 替代，"
                       "字形与字宽和原图不同")
    if unreadable:
        sample = "".join(sorted(unreadable)[:10])
        reasons.append(f"中文字形无法渲染，预览中文不可读（{len(unreadable)} 个汉字无字形，如「{sample}」，显示为□）")
    return context, reasons


def rendered_text_bounds(doc, entities):
    """Return (boxes, diagnostics), in the same order as the supplied entities.

    Each box is (xmin, ymin, xmax, ymax), or None when no reliable rendered
    primitives exist. Uses the exact same context, text layout, alignment,
    rotation, font fallback and recorder as PNG/PDF. No source entity is edited.
    Diagnostics are dictionaries with scope/code/reason and, for entity issues,
    index/handle; document font limitations remain distinct from missing bounds.
    """
    from ezdxf.addons.drawing.recorder import Recorder

    context, reasons = _render_context(doc)
    diagnostics = [{"scope": "document", "code": "FONT_DIAGNOSTIC", "reason": reason}
                   for reason in reasons]
    recorder = Recorder()
    frontend = _frontend(context, recorder)
    handler = _FontWarnings()
    logger = logging.getLogger("ezdxf")
    logger.addHandler(handler)
    boxes = []
    try:
        for index, entity in enumerate(entities):
            recorder.records.clear()
            recorder.properties.clear()
            warning_start = len(handler.messages)
            handle = entity.dxf.get("handle", "virtual")
            try:
                frontend.draw_entities([entity])
                bounds = [_record_bounds(record) for record in recorder.records]
                if not bounds or any(box is None for box in bounds):
                    boxes.append(None)
                    diagnostics.append({"scope": "entity", "code": "TEXT_NO_RENDERED_BOUNDS",
                        "index": index, "handle": handle,
                        "reason": "文字未产生可靠渲染笔画，未生成近似文字框"})
                else:
                    boxes.append((min(b[0] for b in bounds), min(b[1] for b in bounds),
                                  max(b[2] for b in bounds), max(b[3] for b in bounds)))
            except Exception as exc:
                boxes.append(None)
                diagnostics.append({"scope": "entity", "code": "TEXT_RENDER_FAILED",
                    "index": index, "handle": handle,
                    "reason": f"文字实际渲染失败（{type(exc).__name__}），未生成近似文字框"})
            for reason in handler.messages[warning_start:]:
                diagnostics.append({"scope": "entity", "code": "FONT_FALLBACK",
                                    "index": index, "handle": handle, "reason": reason})
    finally:
        logger.removeHandler(handler)
        recorder.records.clear()
        recorder.properties.clear()
    return boxes, diagnostics


def render_frame(doc, frame, target: Path):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    reasons = []
    figure = Figure(figsize=(16, 10), dpi=100)
    if target.suffix.lower() == ".pdf":
        from matplotlib.backends.backend_pdf import FigureCanvasPdf

        FigureCanvasPdf(figure)
    else:
        FigureCanvasAgg(figure)
    axis = figure.add_axes([0, 0, 1, 1])
    handler = _FontWarnings()
    logger = logging.getLogger("ezdxf")
    logger.addHandler(handler)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            context, reasons = _render_context(doc)
            signature = tuple(sorted((key, repr(face)) for key, face in context.fonts.items()))
            try:
                recording = _layout_recording(doc, frame.layout, context, signature, handler)
            except Exception as exc:
                # A recorder/backend incompatibility must preserve the original rendering path.
                reasons.append(f"图框渲染优化不可用，已用完整布局路径（{type(exc).__name__}）")
                _frontend(context, MatplotlibBackend(axis)).draw_layout(frame.layout, finalize=True)
            else:
                if recording.supported:
                    _select_records(recording, frame.bounds).replay(MatplotlibBackend(axis))
                    reasons.extend(recording.warnings)
                else:
                    reasons.append("存在无法可靠回放的渲染记录，已用完整布局路径")
                    _frontend(context, MatplotlibBackend(axis)).draw_layout(frame.layout, finalize=True)
            x0, y0, x1, y1 = frame.bounds
            axis.set_xlim(x0, x1)
            axis.set_ylim(y0, y1)
            axis.set_aspect("auto")
            # 按原始比例固定画布，留白为零，避免 bbox 与 PNG 坐标不一致。
            # 竖版图幅（如 A4 竖向目录页）按长边 16 英寸缩放，避免被压成方形而变形。
            aspect = (y1 - y0) / (x1 - x0)
            if aspect <= 1:
                figure.set_size_inches(16, max(1.0, 16.0 * aspect))
            else:
                figure.set_size_inches(max(1.0, 16.0 / aspect), 16)
            axis.set_axis_off()
            figure.savefig(target, dpi=100, facecolor=axis.get_facecolor())
            for item in caught:
                if any(word in str(item.message).lower() for word in ("font", "glyph", "shx")):
                    reasons.append("字体缺失或替换，仅部分提取：" + str(item.message)[:200])
    finally:
        logger.removeHandler(handler)
        figure.clear()
    return list(dict.fromkeys(reasons + handler.messages))
