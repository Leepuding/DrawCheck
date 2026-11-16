"""A/B 套图纸页面内容绘制。"""
from __future__ import annotations

from pathlib import Path

from .pdfdraw import (
    draw_frame,
    draw_title_block,
    label_box,
    new_canvas,
    save_page,
    stroke_text,
)


def _base(path: Path, page_key: str, *, drawing_number: str | None, name: str, specialty: str,
          title: bool = True):
    c = new_canvas(path, page_key)
    draw_frame(c)
    tb = None
    if title and drawing_number:
        tb = draw_title_block(c, drawing_number=drawing_number, name=name, specialty=specialty)
    return c, tb


def draw_catalog(path: Path, rows: list[dict], *, missing_listed: str, omit: str):
    """目录页：列出 rows，并故意列入 missing_listed、漏掉 omit。"""
    c = new_canvas(path, "A3_LANDSCAPE")
    draw_frame(c)
    draw_title_block(c, drawing_number="S-000", name="图纸目录", specialty="综合")
    stroke_text(c, "图纸目录", 40, 250, 14)
    y = 220
    stroke_text(c, "序号  专业  图号  图名", 40, y, 8)
    y -= 16
    idx = 1
    for row in rows:
        if row["drawing_number"] == omit:
            continue
        line = f"{idx:02d}  {row['specialty']}  {row['drawing_number']}  {row['name']}"
        stroke_text(c, line, 40, y, 7)
        y -= 14
        idx += 1
    stroke_text(c, f"{idx:02d}  电气  {missing_listed}  目录列了但缺图", 40, y, 7)
    save_page(c)


def draw_s001_plan(path: Path, *, prefix_ok: bool = True):
    """总平面：图签前缀用例。"""
    number = "S-001" if prefix_ok else "X-001"
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number=number, name="总平面布置图", specialty="电气")
    stroke_text(c, "总平面布置图", 40, 250, 12)
    box = label_box(c, "区域A", 80, 160, 10)
    save_page(c)
    return {"title_block": tb, "labels": {"area": box}, "drawing_number": number}


def draw_s002_dims(path: Path, *, dim_a: str = "100×100×100", dim_b_side: str = "100×200×100",
                   tol_equal: str = "100", tol_over: str = "102", include_conflict: bool = True,
                   include_tol: bool = True, missing_unit: bool = True):
    """基础详图（一）：尺寸冲突、容差边界、缺单位。A2 横向。"""
    c, tb = _base(path, "A2_LANDSCAPE", drawing_number="S-002", name="基础详图（一）", specialty="土建")
    stroke_text(c, "基础详图（一）", 40, 380, 12)
    boxes = {}
    if include_conflict:
        boxes["dim_left"] = label_box(c, f"{dim_a}mm", 80, 280, 11)
        boxes["dim_right"] = label_box(c, f"{dim_b_side}mm", 320, 280, 11)
    else:
        # 修订后两侧一致
        boxes["dim_left"] = label_box(c, f"{dim_a}mm", 80, 280, 11)
        boxes["dim_right"] = label_box(c, f"{dim_a}mm", 320, 280, 11)
    if include_tol:
        boxes["tol_equal_a"] = label_box(c, f"{tol_equal}mm", 80, 200, 10)
        boxes["tol_equal_b"] = label_box(c, "101mm", 200, 200, 10)  # diff 1mm = 容差
        boxes["tol_over_a"] = label_box(c, f"{tol_equal}mm", 80, 150, 10)
        boxes["tol_over_b"] = label_box(c, f"{tol_over}mm", 200, 150, 10)  # diff 2mm > 1
    if missing_unit:
        boxes["no_unit"] = label_box(c, "150", 80, 100, 10)
        boxes["no_unit_peer"] = label_box(c, "150mm", 200, 100, 10)
    # 不应报：两侧完全相同
    boxes["same_ok"] = label_box(c, "200×200mm", 400, 200, 10)
    boxes["same_ok_b"] = label_box(c, "200×200mm", 520, 200, 10)
    save_page(c)
    return {"title_block": tb, "labels": boxes}


def draw_s003_elev(path: Path, *, elev_a: str = "4.350", elev_b: str = "4.300",
                   conflict: bool = True, tol_equal_peer: str = "4.355", tol_over_peer: str = "4.360"):
    """基础详图(二）：全半角括号；标高冲突与容差。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-003", name="基础详图(二）", specialty="土建")
    stroke_text(c, "基础详图(二）", 40, 250, 12)
    boxes = {}
    if conflict:
        boxes["elev_a"] = label_box(c, f"±0.000={elev_a}m", 70, 180, 9)
        boxes["elev_b"] = label_box(c, f"{elev_b}m", 280, 180, 9)
    else:
        boxes["elev_a"] = label_box(c, f"±0.000={elev_a}m", 70, 180, 9)
        boxes["elev_b"] = label_box(c, f"{elev_a}m", 280, 180, 9)
    # 容差：差 0.005 = 容差；差 0.010 > 容差
    boxes["tol_eq_a"] = label_box(c, "4.350m", 70, 130, 9)
    boxes["tol_eq_b"] = label_box(c, f"{tol_equal_peer}m", 180, 130, 9)
    boxes["tol_over_a"] = label_box(c, "4.350m", 70, 90, 9)
    boxes["tol_over_b"] = label_box(c, f"{tol_over_peer}m", 180, 90, 9)
    # 不应报：相同标高
    boxes["ok_a"] = label_box(c, "3.000m", 320, 130, 9)
    boxes["ok_b"] = label_box(c, "3.000m", 400, 130, 9)
    save_page(c)
    return {"title_block": tb, "labels": boxes}


def draw_s004_materials(path: Path, *, spec_conflict: bool = True, qty_conflict: bool = True,
                        new_issue: bool = False):
    """设备材料表：规格与数量。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-004", name="设备材料表", specialty="电气")
    stroke_text(c, "设备材料表", 40, 250, 12)
    boxes = {}
    stroke_text(c, "材料表", 60, 210, 8)
    if spec_conflict:
        boxes["spec_table"] = label_box(c, "电缆YJV-3×95", 60, 180, 8)
        boxes["spec_note"] = label_box(c, "电缆YJV-3×70", 260, 180, 8)
    else:
        boxes["spec_table"] = label_box(c, "电缆YJV-3×95", 60, 180, 8)
        boxes["spec_note"] = label_box(c, "电缆YJV-3×95", 260, 180, 8)
    if qty_conflict:
        boxes["qty_table"] = label_box(c, "数量12", 60, 140, 8)
        boxes["qty_note"] = label_box(c, "数量10", 200, 140, 8)
    else:
        boxes["qty_table"] = label_box(c, "数量12", 60, 140, 8)
        boxes["qty_note"] = label_box(c, "数量12", 200, 140, 8)
    # 不应报
    boxes["ok_spec"] = label_box(c, "镀锌钢管DN100", 60, 100, 8)
    boxes["ok_spec_b"] = label_box(c, "镀锌钢管DN100", 260, 100, 8)
    boxes["ok_qty"] = label_box(c, "数量4", 60, 80, 8)
    boxes["ok_qty_b"] = label_box(c, "数量4", 200, 80, 8)
    if new_issue:
        boxes["new_spec"] = label_box(c, "母线TMY-80×8", 60, 60, 8)
        boxes["new_spec_b"] = label_box(c, "母线TMY-60×6", 260, 60, 8)
    save_page(c)
    return {"title_block": tb, "labels": boxes}


def draw_s005_numbers(path: Path, *, conflict: bool = True):
    """箱变编号：XB-03 vs XB-3。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-005", name="箱变编号图", specialty="电气")
    stroke_text(c, "箱变编号图", 40, 250, 12)
    boxes = {}
    if conflict:
        boxes["num_a"] = label_box(c, "XB-03", 80, 170, 12)
        boxes["num_b"] = label_box(c, "XB-3", 250, 170, 12)
    else:
        boxes["num_a"] = label_box(c, "XB-03", 80, 170, 12)
        boxes["num_b"] = label_box(c, "XB-03", 250, 170, 12)
    boxes["ok_a"] = label_box(c, "XB-11", 80, 110, 12)
    boxes["ok_b"] = label_box(c, "XB-11", 250, 110, 12)
    save_page(c)
    return {"title_block": tb, "labels": boxes}


def draw_s006_index(path: Path, *, broken: bool = True):
    """详图索引。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-006", name="详图索引", specialty="土建")
    stroke_text(c, "详图索引", 40, 250, 12)
    boxes = {}
    if broken:
        boxes["index"] = label_box(c, "见详图S-099", 80, 170, 10)
    else:
        boxes["index"] = label_box(c, "见详图S-002", 80, 170, 10)
    boxes["ok_index"] = label_box(c, "见详图S-003", 80, 120, 10)
    save_page(c)
    return {"title_block": tb, "labels": boxes}


def draw_s007_multi(path: Path):
    """一页多图框。"""
    c = new_canvas(path, "A3_LANDSCAPE")
    draw_frame(c)
    # 左图框
    c.setLineWidth(0.8)
    c.rect(30, 40, 250, 220, stroke=1, fill=0)
    tb1 = draw_title_block(
        c, drawing_number="S-007A", name="多图框左", specialty="电气",
        box=(30 + 250 - 150, 40 + 4, 145, 60),
    )
    stroke_text(c, "多图框左", 50, 200, 10)
    # 右图框
    c.rect(300, 40, 250, 220, stroke=1, fill=0)
    tb2 = draw_title_block(
        c, drawing_number="S-007B", name="多图框右", specialty="电气",
        box=(300 + 250 - 150, 40 + 4, 145, 60),
    )
    stroke_text(c, "多图框右", 320, 200, 10)
    save_page(c)
    return {"frames": [{"drawing_number": "S-007A", "title_block": tb1},
                       {"drawing_number": "S-007B", "title_block": tb2}]}


def draw_s008_no_title(path: Path):
    """无图签。"""
    c, _ = _base(path, "A3_LANDSCAPE", drawing_number=None, name="", specialty="", title=False)
    stroke_text(c, "无图签页内容", 40, 200, 12)
    box = label_box(c, "备注区", 80, 140, 10)
    save_page(c)
    return {"title_block": None, "labels": {"note": box}}


def draw_s009_blur(path: Path):
    """故意模糊字。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-009", name="模糊标注页", specialty="电气")
    stroke_text(c, "模糊标注页", 40, 250, 12)
    clear = label_box(c, "2400", 80, 170, 12)
    blur = label_box(c, "2?00", 220, 170, 12, blur=True)
    save_page(c)
    return {"title_block": tb, "labels": {"clear": clear, "blur": blur}}


def draw_s010_same_name(path: Path):
    """同名不同对象。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-010", name="同名对象页", specialty="电气")
    stroke_text(c, "同名对象页", 40, 250, 12)
    a = label_box(c, "基础F1", 80, 170, 10)
    b = label_box(c, "基础F1", 280, 120, 10)
    ok = label_box(c, "支架Z1", 80, 100, 10)
    save_page(c)
    return {"title_block": tb, "labels": {"obj_a": a, "obj_b": b, "ok": ok}}


def draw_extra(path: Path):
    """有图但目录未列。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="S-EXTRA", name="目录未列图", specialty="电气")
    stroke_text(c, "目录未列图", 40, 200, 12)
    save_page(c)
    return {"title_block": tb}


def draw_scope_out(path: Path):
    """不在审查范围：建筑装修类。"""
    c, tb = _base(path, "A4_PORTRAIT", drawing_number="DEC-01", name="装修平面图", specialty="装修")
    stroke_text(c, "装修平面图", 40, 250, 12)
    stroke_text(c, "外审范围外", 40, 200, 10)
    save_page(c)
    return {"title_block": tb, "out_of_scope": True}


def draw_prefix_bad(path: Path):
    """图签编号前缀错误对照图（专业电气但前缀不匹配）。"""
    c, tb = _base(path, "A3_LANDSCAPE", drawing_number="AB-001", name="前缀不符示例", specialty="电气")
    stroke_text(c, "前缀不符示例", 40, 200, 12)
    save_page(c)
    return {"title_block": tb, "drawing_number": "AB-001"}
