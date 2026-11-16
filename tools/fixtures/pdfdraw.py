"""用 reportlab 路径绘制自制图纸；不嵌入字体、不写文字算子。"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .glyphs import iter_stroke_segments, measure_width

# ISO 幅面（点）
PAGE_SIZES = {
    "A3_LANDSCAPE": (420 * mm, 297 * mm),
    "A2_LANDSCAPE": (594 * mm, 420 * mm),
    "A4_PORTRAIT": (210 * mm, 297 * mm),
}


def new_canvas(path: Path, page_key: str) -> canvas.Canvas:
    width, height = PAGE_SIZES[page_key]
    # invariant=1 去掉时间戳/随机 ID，保证确定性
    c = canvas.Canvas(str(path), pagesize=(width, height), invariant=1, pageCompression=1)
    c.setTitle("")
    c.setAuthor("")
    c.setSubject("")
    c.setCreator("drawcheck-DC-05")
    return c


def stroke_text(c: canvas.Canvas, text: str, x: float, y: float, size: float,
                *, tracking: float = 1.05, stroke_width: float = 0.6, blur: bool = False):
    """将字符串画成路径。blur=True 时用多道错位细线模拟看不清。"""
    if blur:
        for dx, dy, w in ((0, 0, 0.35), (0.6, 0.4, 0.25), (-0.5, 0.3, 0.25), (0.3, -0.5, 0.2)):
            c.setStrokeGray(0.35)
            c.setLineWidth(w)
            for x0, y0, x1, y1 in iter_stroke_segments(text, x + dx, y + dy, size, tracking=tracking):
                c.line(x0, y0, x1, y1)
        return
    c.setStrokeGray(0)
    c.setLineWidth(stroke_width)
    c.setLineCap(1)
    c.setLineJoin(1)
    for x0, y0, x1, y1 in iter_stroke_segments(text, x, y, size, tracking=tracking):
        c.line(x0, y0, x1, y1)


def draw_frame(c: canvas.Canvas, margin: float = 18):
    w, h = c._pagesize
    c.setStrokeGray(0)
    c.setLineWidth(1.2)
    c.rect(margin, margin, w - 2 * margin, h - 2 * margin, stroke=1, fill=0)
    c.setLineWidth(0.4)
    c.rect(margin + 4, margin + 4, w - 2 * margin - 8, h - 2 * margin - 8, stroke=1, fill=0)


def draw_title_block(c: canvas.Canvas, *, drawing_number: str, name: str, specialty: str,
                     box: tuple[float, float, float, float] | None = None):
    """右下角图签。返回归一化 bbox (x0,y0,x1,y1) 左上原点。"""
    w, h = c._pagesize
    if box is None:
        tw, th = 160, 70
        x0, y0 = w - 18 - 4 - tw, 18 + 4
    else:
        # box 为页面点坐标左下系 (x, y, w, h)
        x0, y0, tw, th = box
    c.setStrokeGray(0)
    c.setLineWidth(0.8)
    c.rect(x0, y0, tw, th, stroke=1, fill=0)
    c.line(x0, y0 + th * 2 / 3, x0 + tw, y0 + th * 2 / 3)
    c.line(x0, y0 + th / 3, x0 + tw, y0 + th / 3)
    c.line(x0 + tw * 0.35, y0, x0 + tw * 0.35, y0 + th)
    size = 7
    stroke_text(c, "专业", x0 + 4, y0 + th * 2 / 3 + 8, size)
    stroke_text(c, specialty, x0 + tw * 0.35 + 4, y0 + th * 2 / 3 + 8, size)
    stroke_text(c, "图名", x0 + 4, y0 + th / 3 + 8, size)
    stroke_text(c, name, x0 + tw * 0.35 + 4, y0 + th / 3 + 8, size - 0.5 if len(name) > 8 else size)
    stroke_text(c, "图号", x0 + 4, y0 + 8, size)
    stroke_text(c, drawing_number, x0 + tw * 0.35 + 4, y0 + 8, size)
    # 归一化：左上原点
    return (
        x0 / w,
        1.0 - (y0 + th) / h,
        (x0 + tw) / w,
        1.0 - y0 / h,
    )


def page_norm_bbox(c: canvas.Canvas, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    """页面点坐标（左下原点）矩形 → 归一化左上原点 bbox。"""
    pw, ph = c._pagesize
    return (x / pw, 1.0 - (y + h) / ph, (x + w) / pw, 1.0 - y / ph)


def label_box(c: canvas.Canvas, text: str, x: float, y: float, size: float = 9,
              *, blur: bool = False, pad: float = 3.0) -> tuple[float, float, float, float]:
    """绘制标注并返回归一化 bbox。"""
    tw = measure_width(text, size)
    th = size * 1.2
    if not blur:
        c.setStrokeGray(0.2)
        c.setLineWidth(0.3)
        c.rect(x - pad, y - pad, tw + 2 * pad, th + 2 * pad, stroke=1, fill=0)
    stroke_text(c, text, x, y, size, blur=blur, stroke_width=0.7)
    return page_norm_bbox(c, x - pad, y - pad, tw + 2 * pad, th + 2 * pad)


def save_page(c: canvas.Canvas):
    c.showPage()
    c.save()
