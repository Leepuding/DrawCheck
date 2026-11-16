"""生成自制测试图纸集（非金样本）。

用法：
  python -m tools.fixtures --out fixtures/synthetic
  python -m tools.fixtures.generate --out <目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import anomalies as anomalies_mod
from . import drawings as D
from .schema import validate_answers

ROOT = Path(__file__).resolve().parents[2]  # worktree root
DEFAULT_OUT = ROOT / "fixtures" / "synthetic"

CATALOG_A = [
    {"specialty": "综合", "drawing_number": "S-000", "name": "图纸目录", "file": "A/S-000-catalog.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "电气", "drawing_number": "S-001", "name": "总平面布置图", "file": "A/S-001-plan.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "土建", "drawing_number": "S-002", "name": "基础详图（一）", "file": "A/S-002-dims.pdf", "page": 1, "page_size": "A2_LANDSCAPE"},
    {"specialty": "土建", "drawing_number": "S-003", "name": "基础详图(二）", "file": "A/S-003-elev.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "电气", "drawing_number": "S-004", "name": "设备材料表", "file": "A/S-004-materials.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "电气", "drawing_number": "S-005", "name": "箱变编号图", "file": "A/S-005-numbers.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "土建", "drawing_number": "S-006", "name": "详图索引", "file": "A/S-006-index.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "电气", "drawing_number": "S-007A", "name": "多图框左", "file": "A/S-007-multi.pdf", "page": 1, "page_size": "A3_LANDSCAPE", "multi_frame": True},
    {"specialty": "电气", "drawing_number": "S-007B", "name": "多图框右", "file": "A/S-007-multi.pdf", "page": 1, "page_size": "A3_LANDSCAPE", "multi_frame": True},
    {"specialty": "土建", "drawing_number": "S-008", "name": "无图签页", "file": "A/S-008-notitle.pdf", "page": 1, "page_size": "A3_LANDSCAPE", "no_title_block": True},
    {"specialty": "电气", "drawing_number": "S-009", "name": "模糊标注页", "file": "A/S-009-blur.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "电气", "drawing_number": "S-010", "name": "同名对象页", "file": "A/S-010-samename.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
    {"specialty": "电气", "drawing_number": "S-PREFIX", "name": "前缀不符示例", "file": "A/S-PREFIX-bad.pdf", "page": 1, "page_size": "A3_LANDSCAPE"},
]

LISTED_MISSING = "S-MISSING"
OMIT_FROM_CATALOG_FILE = "S-EXTRA"  # 有图但目录 PDF 未列；catalog 期望仍记录为 unlisted


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _as_bbox(bbox) -> dict | None:
    if bbox is None:
        return None
    if isinstance(bbox, dict):
        return bbox
    x0, y0, x1, y1 = bbox
    return {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}


def _side(drawing_number: str, value: str, bbox, *, unit: str | None = None,
          numeric_value: str | None = None, unclear: bool = False) -> dict:
    side = {
        "drawing_number": drawing_number,
        "text": value,
        "bbox": _as_bbox(bbox),
        "unclear": unclear,
    }
    if unit is not None:
        side["unit"] = unit
    if numeric_value is not None:
        side["numeric_value"] = numeric_value
    return side


def build_answers(meta_a: dict, meta_b: dict) -> dict:
    """组装 answers.json；容差边界按裁定：等于不报、超过才报。"""
    issues = []

    def add(**kwargs):
        issues.append(kwargs)

    # --- 1 目录张数 / 目录与图签不符（两种都报，各一条；裁定归类「目录与图签不符」）---
    add(id="ISSUE-CAT-MISSING", category="目录与图签不符", expectation="应报",
        rule_group="A", description="目录列了但缺图 S-MISSING",
        side_a=_side("S-000", "S-MISSING", None), side_b=None,
        notes="目录列了但图纸缺；每张各一条")
    add(id="ISSUE-CAT-EXTRA", category="目录与图签不符", expectation="应报",
        rule_group="A", description="有图但目录未列 S-EXTRA",
        side_a=_side("S-EXTRA", "S-EXTRA", meta_a["extra"]["title_block"]), side_b=None,
        notes="有图但目录未列；每张各一条")
    add(id="ISSUE-CAT-COUNT", category="目录张数", expectation="应报",
        rule_group="A", description="目录张数与实到图不一致（含缺图与未列）",
        side_a=_side("S-000", "目录张数", None), side_b=None,
        notes="汇总口径；明细见 ISSUE-CAT-MISSING / EXTRA")
    add(id="ISSUE-CAT-OK", category="目录张数", expectation="不应报",
        rule_group="A", description="S-001 在目录与图纸中均存在",
        side_a=_side("S-001", "S-001", meta_a["s001"]["title_block"]), side_b=None)

    # --- 2 图签编号前缀 ---
    add(id="ISSUE-PREFIX-BAD", category="图签编号前缀", expectation="应报",
        rule_group="A", description="电气图签前缀 AB 与约定 S 不符",
        side_a=_side("S-PREFIX", "AB-001", meta_a["prefix"]["title_block"]), side_b=None)
    add(id="ISSUE-PREFIX-OK", category="图签编号前缀", expectation="不应报",
        rule_group="A", description="S-001 前缀符合约定",
        side_a=_side("S-001", "S-001", meta_a["s001"]["title_block"]), side_b=None)

    # --- 3 尺寸（含 100×100×100 vs 100×200×100）---
    la = meta_a["s002"]["labels"]
    add(id="ISSUE-DIM-CONFLICT", category="尺寸", expectation="应报",
        rule_group="A", description="100×100×100 对 100×200×100",
        side_a=_side("S-002", "100×100×100mm", la["dim_left"], unit="mm"),
        side_b=_side("S-002", "100×200×100mm", la["dim_right"], unit="mm"))
    add(id="ISSUE-DIM-OK", category="尺寸", expectation="不应报",
        rule_group="A", description="两侧 200×200 一致",
        side_a=_side("S-002", "200×200mm", la["same_ok"], unit="mm"),
        side_b=_side("S-002", "200×200mm", la["same_ok_b"], unit="mm"))
    add(id="ISSUE-DIM-TOL-EQ", category="尺寸", expectation="不应报",
        rule_group="A", description="尺寸差等于容差（1mm）不报",
        side_a=_side("S-002", "100mm", la["tol_equal_a"], unit="mm", numeric_value="100"),
        side_b=_side("S-002", "101mm", la["tol_equal_b"], unit="mm", numeric_value="101"),
        tolerance={"config_key": "dimension_tolerance_mm", "boundary": "equal",
                   "compare": "abs_diff", "decision": "lte_tolerance_no_report"})
    add(id="ISSUE-DIM-TOL-OVER", category="尺寸", expectation="应报",
        rule_group="A", description="尺寸差超过容差（2mm>1mm）应报",
        side_a=_side("S-002", "100mm", la["tol_over_a"], unit="mm", numeric_value="100"),
        side_b=_side("S-002", "102mm", la["tol_over_b"], unit="mm", numeric_value="102"),
        tolerance={"config_key": "dimension_tolerance_mm", "boundary": "over",
                   "compare": "abs_diff", "decision": "gt_tolerance_report"})
    add(id="ISSUE-DIM-NO-UNIT", category="尺寸", expectation="未核验",
        rule_group="A", description="一侧缺单位，待确认不报错误",
        side_a=_side("S-002", "150", la["no_unit"], unit=None, numeric_value="150"),
        side_b=_side("S-002", "150mm", la["no_unit_peer"], unit="mm", numeric_value="150"),
        tolerance={"config_key": "dimension_tolerance_mm", "boundary": "missing_unit",
                   "decision": "pending_confirm_no_error"})

    # --- 4 标高 ---
    el = meta_a["s003"]["labels"]
    add(id="ISSUE-ELV-CONFLICT", category="标高", expectation="应报",
        rule_group="A", description="标高 4.350 对 4.300",
        side_a=_side("S-003", "±0.000=4.350m", el["elev_a"], unit="m", numeric_value="4.350"),
        side_b=_side("S-003", "4.300m", el["elev_b"], unit="m", numeric_value="4.300"))
    add(id="ISSUE-ELV-OK", category="标高", expectation="不应报",
        rule_group="A", description="标高 3.000 一致",
        side_a=_side("S-003", "3.000m", el["ok_a"], unit="m", numeric_value="3.000"),
        side_b=_side("S-003", "3.000m", el["ok_b"], unit="m", numeric_value="3.000"))
    add(id="ISSUE-ELV-TOL-EQ", category="标高", expectation="不应报",
        rule_group="A", description="标高差等于容差（0.005m）不报",
        side_a=_side("S-003", "4.350m", el["tol_eq_a"], unit="m", numeric_value="4.350"),
        side_b=_side("S-003", "4.355m", el["tol_eq_b"], unit="m", numeric_value="4.355"),
        tolerance={"config_key": "elevation_tolerance_m", "boundary": "equal",
                   "compare": "abs_diff", "decision": "lte_tolerance_no_report"})
    add(id="ISSUE-ELV-TOL-OVER", category="标高", expectation="应报",
        rule_group="A", description="标高差超过容差（0.010>0.005）应报",
        side_a=_side("S-003", "4.350m", el["tol_over_a"], unit="m", numeric_value="4.350"),
        side_b=_side("S-003", "4.360m", el["tol_over_b"], unit="m", numeric_value="4.360"),
        tolerance={"config_key": "elevation_tolerance_m", "boundary": "over",
                   "compare": "abs_diff", "decision": "gt_tolerance_report"})

    # --- 5 编号写法 ---
    nb = meta_a["s005"]["labels"]
    add(id="ISSUE-NUM-STYLE", category="编号写法", expectation="应报",
        rule_group="B", description="XB-03 对 XB-3",
        side_a=_side("S-005", "XB-03", nb["num_a"]),
        side_b=_side("S-005", "XB-3", nb["num_b"]))
    add(id="ISSUE-NUM-OK", category="编号写法", expectation="不应报",
        rule_group="B", description="XB-11 一致",
        side_a=_side("S-005", "XB-11", nb["ok_a"]),
        side_b=_side("S-005", "XB-11", nb["ok_b"]))

    # --- 6 材料规格 ---
    mat = meta_a["s004"]["labels"]
    add(id="ISSUE-SPEC", category="材料规格", expectation="应报",
        rule_group="B", description="YJV-3×95 对 YJV-3×70",
        side_a=_side("S-004", "电缆YJV-3×95", mat["spec_table"]),
        side_b=_side("S-004", "电缆YJV-3×70", mat["spec_note"]))
    add(id="ISSUE-SPEC-OK", category="材料规格", expectation="不应报",
        rule_group="B", description="镀锌钢管DN100 一致",
        side_a=_side("S-004", "镀锌钢管DN100", mat["ok_spec"]),
        side_b=_side("S-004", "镀锌钢管DN100", mat["ok_spec_b"]))

    # --- 7 数量与材料表 ---
    add(id="ISSUE-QTY", category="数量与材料表", expectation="应报",
        rule_group="B", description="数量 12 对 10",
        side_a=_side("S-004", "数量12", mat["qty_table"], numeric_value="12"),
        side_b=_side("S-004", "数量10", mat["qty_note"], numeric_value="10"))
    add(id="ISSUE-QTY-OK", category="数量与材料表", expectation="不应报",
        rule_group="B", description="数量 4 与材料表一致",
        side_a=_side("S-004", "数量4", mat["ok_qty"], numeric_value="4"),
        side_b=_side("S-004", "数量4", mat["ok_qty_b"], numeric_value="4"))

    # --- 8 详图索引断链 ---
    ix = meta_a["s006"]["labels"]
    add(id="ISSUE-INDEX-BROKEN", category="详图索引断链", expectation="应报",
        rule_group="B", description="索引指向不存在的 S-099",
        side_a=_side("S-006", "见详图S-099", ix["index"]), side_b=None)
    add(id="ISSUE-INDEX-OK", category="详图索引断链", expectation="不应报",
        rule_group="B", description="索引指向存在的 S-003",
        side_a=_side("S-006", "见详图S-003", ix["ok_index"]), side_b=None)

    # --- 9 同名不同对象 → 不下结论 ---
    sn = meta_a["s010"]["labels"]
    add(id="ISSUE-SAME-NAME", category="同名不同对象", expectation="不下结论",
        rule_group="B", description="两处「基础F1」可能非同一对象，转待确认或不报",
        side_a=_side("S-010", "基础F1", sn["obj_a"]),
        side_b=_side("S-010", "基础F1", sn["obj_b"]),
        notes="对照例须为可比较的同一对象；此处故意同名异物")
    add(id="ISSUE-SAME-NAME-OK", category="同名不同对象", expectation="不应报",
        rule_group="B", description="唯一支架Z1，无冲突",
        side_a=_side("S-010", "支架Z1", sn["ok"]), side_b=None)

    # --- 模糊字 → 待你确认 ---
    bl = meta_a["s009"]["labels"]
    add(id="ISSUE-BLUR", category="尺寸", expectation="待确认",
        rule_group="pending", description="尺寸「2?00」看不清",
        side_a=_side("S-009", "2?00", bl["blur"], unclear=True),
        side_b=_side("S-009", "2400", bl["clear"]),
        notes="Q-05 情况①：看不清进待你确认；回放标 unclear")

    # --- 修订 B 套标签 ---
    # 仍存在：标高冲突在 B 仍保留
    # 已解决：尺寸冲突在 B 已改为一致
    # 新出现：B 套材料新冲突
    lb = meta_b["s002"]["labels"]
    el_b = meta_b["s003"]["labels"]
    mat_b = meta_b["s004"]["labels"]
    add(id="REV-STILL-ELV", category="标高", expectation="应报",
        rule_group="A", revision="仍存在", description="B 套标高冲突仍存在",
        side_a=_side("S-003", "±0.000=4.350m", el_b["elev_a"], unit="m", numeric_value="4.350"),
        side_b=_side("S-003", "4.300m", el_b["elev_b"], unit="m", numeric_value="4.300"),
        matches_issue="ISSUE-ELV-CONFLICT")
    add(id="REV-RESOLVED-DIM", category="尺寸", expectation="不应报",
        rule_group="A", revision="已解决", description="B 套尺寸冲突已改为一致",
        side_a=_side("S-002", "100×100×100mm", lb["dim_left"], unit="mm"),
        side_b=_side("S-002", "100×100×100mm", lb["dim_right"], unit="mm"),
        matches_issue="ISSUE-DIM-CONFLICT")
    add(id="REV-NEW-SPEC", category="材料规格", expectation="应报",
        rule_group="B", revision="新出现", description="B 套新出现母线规格冲突",
        side_a=_side("S-004", "母线TMY-80×8", mat_b["new_spec"]),
        side_b=_side("S-004", "母线TMY-60×6", mat_b["new_spec_b"]))

    catalog_b = []
    for row in CATALOG_A:
        item = dict(row)
        item["file"] = item["file"].replace("A/", "B/", 1)
        catalog_b.append(item)
    # B 套也含 EXTRA / SCOPE / 未改动说明
    catalog_b.append({"specialty": "电气", "drawing_number": "S-EXTRA", "name": "目录未列图",
                      "file": "B/S-EXTRA.pdf", "page": 1, "page_size": "A3_LANDSCAPE",
                      "unlisted_in_catalog_sheet": True})

    answers = {
        "kind": "synthetic-dev-answers",
        "is_golden_sample": False,
        "disclaimer": "自制开发用答案，不是金样本，不能替代最终验收。",
        "version": "DC-05-v1",
        "created": "2026-11-03",
        "tolerance_defaults_ref": {
            "dimension_tolerance_mm": "settings.dimension_tolerance_mm（默认 1）",
            "elevation_tolerance_m": "settings.elevation_tolerance_m（默认 0.005）",
            "equal_boundary": "差值≤容差不报；仅>容差才报（SPEC 第4波裁定）",
        },
        "catalog": {
            "A": CATALOG_A + [
                {"specialty": "电气", "drawing_number": "S-EXTRA", "name": "目录未列图",
                 "file": "A/S-EXTRA.pdf", "page": 1, "page_size": "A3_LANDSCAPE",
                 "unlisted_in_catalog_sheet": True},
                {"specialty": "装修", "drawing_number": "DEC-01", "name": "装修平面图",
                 "file": "A/DEC-01-scope.pdf", "page": 1, "page_size": "A3_LANDSCAPE",
                 "out_of_scope": True},
            ],
            "B": catalog_b + [
                {"specialty": "装修", "drawing_number": "DEC-01", "name": "装修平面图",
                 "file": "B/DEC-01-scope.pdf", "page": 1, "page_size": "A3_LANDSCAPE",
                 "out_of_scope": True},
            ],
            "listed_missing": [LISTED_MISSING],
            "catalog_sheet_omits": ["S-EXTRA"],
        },
        "issues": issues,
        "revision_summary": {"仍存在": 1, "已解决": 1, "新出现": 1},
        "unchanged_drawings": [
            {"drawing_number": "S-001", "file_a": "A/S-001-plan.pdf", "file_b": "B/S-001-plan.pdf",
             "identity": "byte_identical"},
        ],
        "pending_confirmations": [
            {"id": "PEND-BLUR", "issue_id": "ISSUE-BLUR", "drawing_number": "S-009",
             "reason": "看不清", "text": "2?00"},
        ],
    }
    validate_answers(answers)
    return answers


def _poppler_version() -> str:
    try:
        out = subprocess.run(["pdftoppm", "-v"], capture_output=True, text=True, check=False)
        line = (out.stderr or out.stdout or "").strip().splitlines()[0]
        return line
    except FileNotFoundError:
        return "unavailable"


def render_review_png(pdf: Path, dest: Path) -> str:
    """用与 DC-06 相同的 150dpi 单页渲染，返回 PNG SHA-256。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = dest.with_suffix("")
    subprocess.run(
        ["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-png", "-r", "150", str(pdf), str(prefix)],
        check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    png = Path(str(prefix) + ".png")
    if png != dest:
        png.replace(dest)
    return sha256_file(dest)


def build_replay(out: Path, suite: str, files: list[Path], facts_by_file: dict) -> dict:
    """按审查图 SHA 与备用键（PDF SHA + 页序）写回放。"""
    replay_dir = out / "replay" / suite
    replay_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "suite": suite,
        "poppler_version": _poppler_version(),
        "review_dpi": 150,
        "key_primary": "review_png_sha256",
        "key_fallback": "pdf_sha256+page",
        "entries": [],
    }
    for pdf in files:
        rel = ("build/" + pdf.relative_to(out / "build").as_posix())
        pdf_sha = sha256_file(pdf)
        png_path = replay_dir / f"{pdf.stem}.review.png"
        try:
            review_sha = render_review_png(pdf, png_path)
            # PNG 体积大，不进仓：生成后删，只留哈希
            png_path.unlink(missing_ok=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            review_sha = None
            index.setdefault("render_errors", []).append({"file": rel, "error": str(exc)})
        facts = facts_by_file.get(pdf.name, [])
        entry = {
            "file": rel,
            "pdf_sha256": pdf_sha,
            "page": 1,
            "review_png_sha256": review_sha,
            "fallback_key": f"{pdf_sha}:1",
            "facts": facts,
        }
        index["entries"].append(entry)
    (replay_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def facts_from_meta(meta: dict, suite: str) -> dict[str, list]:
    """从绘制元数据抽出回放事实。"""
    out: dict[str, list] = {}

    def fact(category, text, bbox, *, numeric=None, unit=None, confidence=0.99, unclear=False):
        item = {
            "category": category,
            "text": text,
            "bbox": _as_bbox(bbox),
            "confidence": confidence,
            "unclear": unclear,
            "source": "看图识别",
        }
        if numeric is not None:
            item["numeric_value"] = numeric
        if unit is not None:
            item["unit"] = unit
        return item

    # S-002
    la = meta["s002"]["labels"]
    out["S-002-dims.pdf"] = [
        fact("尺寸", "100×100×100mm", la["dim_left"], unit="mm"),
        fact("尺寸", "100×200×100mm" if suite == "A" else "100×100×100mm", la["dim_right"], unit="mm"),
        fact("尺寸", "100mm", la["tol_equal_a"], numeric="100", unit="mm"),
        fact("尺寸", "101mm", la["tol_equal_b"], numeric="101", unit="mm"),
        fact("尺寸", "100mm", la["tol_over_a"], numeric="100", unit="mm"),
        fact("尺寸", "102mm", la["tol_over_b"], numeric="102", unit="mm"),
        fact("尺寸", "150", la["no_unit"], numeric="150"),
        fact("尺寸", "150mm", la["no_unit_peer"], numeric="150", unit="mm"),
        fact("尺寸", "200×200mm", la["same_ok"], unit="mm"),
        fact("尺寸", "200×200mm", la["same_ok_b"], unit="mm"),
    ]
    el = meta["s003"]["labels"]
    out["S-003-elev.pdf"] = [
        fact("标高", "±0.000=4.350m", el["elev_a"], numeric="4.350", unit="m"),
        fact("标高", "4.300m", el["elev_b"], numeric="4.300", unit="m"),
        fact("标高", "4.350m", el["tol_eq_a"], numeric="4.350", unit="m"),
        fact("标高", "4.355m", el["tol_eq_b"], numeric="4.355", unit="m"),
        fact("标高", "4.350m", el["tol_over_a"], numeric="4.350", unit="m"),
        fact("标高", "4.360m", el["tol_over_b"], numeric="4.360", unit="m"),
        fact("标高", "3.000m", el["ok_a"], numeric="3.000", unit="m"),
        fact("标高", "3.000m", el["ok_b"], numeric="3.000", unit="m"),
    ]
    mat = meta["s004"]["labels"]
    facts4 = [
        fact("材料规格", "电缆YJV-3×95", mat["spec_table"]),
        fact("材料规格", "电缆YJV-3×70" if suite == "A" else "电缆YJV-3×95", mat["spec_note"]),
        fact("数量与材料表", "数量12", mat["qty_table"], numeric="12"),
        fact("数量与材料表", "数量10" if suite == "A" else "数量12", mat["qty_note"], numeric="10" if suite == "A" else "12"),
        fact("材料规格", "镀锌钢管DN100", mat["ok_spec"]),
        fact("材料规格", "镀锌钢管DN100", mat["ok_spec_b"]),
        fact("数量与材料表", "数量4", mat["ok_qty"], numeric="4"),
        fact("数量与材料表", "数量4", mat["ok_qty_b"], numeric="4"),
    ]
    if suite == "B":
        facts4.extend([
            fact("材料规格", "母线TMY-80×8", mat["new_spec"]),
            fact("材料规格", "母线TMY-60×6", mat["new_spec_b"]),
        ])
    out["S-004-materials.pdf"] = facts4
    nb = meta["s005"]["labels"]
    out["S-005-numbers.pdf"] = [
        fact("编号写法", "XB-03", nb["num_a"]),
        fact("编号写法", "XB-3", nb["num_b"]),
        fact("编号写法", "XB-11", nb["ok_a"]),
        fact("编号写法", "XB-11", nb["ok_b"]),
    ]
    ix = meta["s006"]["labels"]
    out["S-006-index.pdf"] = [
        fact("详图索引断链", "见详图S-099", ix["index"]),
        fact("详图索引断链", "见详图S-003", ix["ok_index"]),
    ]
    bl = meta["s009"]["labels"]
    out["S-009-blur.pdf"] = [
        fact("尺寸", "2400", bl["clear"], numeric="2400", unit="mm"),
        fact("尺寸", "2?00", bl["blur"], unclear=True, confidence=0.2),
    ]
    sn = meta["s010"]["labels"]
    out["S-010-samename.pdf"] = [
        fact("同名不同对象", "基础F1", sn["obj_a"]),
        fact("同名不同对象", "基础F1", sn["obj_b"]),
        fact("同名不同对象", "支架Z1", sn["ok"]),
    ]
    out["S-001-plan.pdf"] = [
        fact("图签编号前缀", meta["s001"]["drawing_number"], meta["s001"]["title_block"]),
    ]
    out["S-PREFIX-bad.pdf"] = [
        fact("图签编号前缀", "AB-001", meta["prefix"]["title_block"]),
    ]
    out["S-EXTRA.pdf"] = [
        fact("目录与图签不符", "S-EXTRA", meta["extra"]["title_block"]),
    ]
    out["S-000-catalog.pdf"] = []
    out["S-007-multi.pdf"] = []
    out["S-008-notitle.pdf"] = []
    out["DEC-01-scope.pdf"] = []
    return out


def try_make_dwg(out: Path) -> dict | None:
    """可选：ezdxf → dxf2dwg。失败则返回 None。"""
    found = shutil.which("dxf2dwg", path=os.environ.get("DRAWCHECK_LIBREDWG_BIN", os.environ.get("PATH", "")))
    if not found:
        return {"status": "skipped", "reason": "本机 dxf2dwg 不可用"}
    dxf2dwg = Path(found)
    try:
        import ezdxf
    except ImportError:
        return {"status": "skipped", "reason": "ezdxf 未安装"}
    build = out / "build" / "optional"
    build.mkdir(parents=True, exist_ok=True)
    dxf_path = build / "tiny.dxf"
    dwg_path = build / "tiny.dwg"
    doc = ezdxf.new("R2010")
    doc.header["$TDCREATE"] = 0.0
    doc.header["$TDUPDATE"] = 0.0
    doc.header["$TDUCREATE"] = 0.0
    doc.header["$TDUUPDATE"] = 0.0
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))
    msp.add_text("DC05", dxfattribs={"height": 1.0}).set_placement((1, 1))
    doc.saveas(dxf_path)
    try:
        subprocess.run(
            [str(dxf2dwg), "-o", str(dwg_path), str(dxf_path)],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"status": "skipped", "reason": f"dxf2dwg 失败: {exc}", "dxf": str(dxf_path)}
    if not dwg_path.is_file():
        return {"status": "skipped", "reason": "dxf2dwg 无输出", "dxf": str(dxf_path)}
    # build/optional 不强制提交；MANIFEST 只记路径与解析态，不记可变 sha256
    return {
        "status": "partial",
        "parse_status": "PARTIAL",
        "dxf": "build/optional/tiny.dxf",
        "dwg": "build/optional/tiny.dwg",
        "note": "LibreDWG/ezdxf 输出非确定；SHA 不入 MANIFEST，不做跨次一致性断言",
    }


def generate(out: Path) -> dict:
    out = out.resolve()
    build = out / "build"
    if build.exists():
        shutil.rmtree(build)
    (build / "A").mkdir(parents=True)
    (build / "B").mkdir(parents=True)
    (out / "anomalies").mkdir(parents=True, exist_ok=True)
    (out / "replay").mkdir(parents=True, exist_ok=True)

    # --- A 套 ---
    meta_a = {}
    rows_for_catalog = [r for r in CATALOG_A if r["drawing_number"] not in ("S-000", "S-007B")]
    # 目录页单独画
    D.draw_catalog(build / "A" / "S-000-catalog.pdf",
                   [r for r in CATALOG_A if not r.get("multi_frame") or r["drawing_number"] == "S-007A"],
                   missing_listed=LISTED_MISSING, omit="S-EXTRA")
    # 修正：S-EXTRA 不在 CATALOG_A 主列表里，omit 对目录行无影响；目录函数自己不列 EXTRA
    meta_a["s001"] = D.draw_s001_plan(build / "A" / "S-001-plan.pdf")
    meta_a["s002"] = D.draw_s002_dims(build / "A" / "S-002-dims.pdf", include_conflict=True)
    meta_a["s003"] = D.draw_s003_elev(build / "A" / "S-003-elev.pdf", conflict=True)
    meta_a["s004"] = D.draw_s004_materials(build / "A" / "S-004-materials.pdf",
                                           spec_conflict=True, qty_conflict=True, new_issue=False)
    meta_a["s005"] = D.draw_s005_numbers(build / "A" / "S-005-numbers.pdf", conflict=True)
    meta_a["s006"] = D.draw_s006_index(build / "A" / "S-006-index.pdf", broken=True)
    meta_a["s007"] = D.draw_s007_multi(build / "A" / "S-007-multi.pdf")
    meta_a["s008"] = D.draw_s008_no_title(build / "A" / "S-008-notitle.pdf")
    meta_a["s009"] = D.draw_s009_blur(build / "A" / "S-009-blur.pdf")
    meta_a["s010"] = D.draw_s010_same_name(build / "A" / "S-010-samename.pdf")
    meta_a["prefix"] = D.draw_prefix_bad(build / "A" / "S-PREFIX-bad.pdf")
    meta_a["extra"] = D.draw_extra(build / "A" / "S-EXTRA.pdf")
    meta_a["scope"] = D.draw_scope_out(build / "A" / "DEC-01-scope.pdf")

    # --- B 套：S-001 逐字节相同；S-002 尺寸冲突已解决；S-003 仍冲突；S-004 新冲突 ---
    shutil.copy2(build / "A" / "S-001-plan.pdf", build / "B" / "S-001-plan.pdf")
    meta_b = {"s001": meta_a["s001"]}
    meta_b["s002"] = D.draw_s002_dims(build / "B" / "S-002-dims.pdf", include_conflict=False)
    meta_b["s003"] = D.draw_s003_elev(build / "B" / "S-003-elev.pdf", conflict=True)
    meta_b["s004"] = D.draw_s004_materials(build / "B" / "S-004-materials.pdf",
                                           spec_conflict=False, qty_conflict=False, new_issue=True)
    # 其余与 A 相同内容重新生成（非逐字节要求）；S-005.. 及目录等
    D.draw_catalog(build / "B" / "S-000-catalog.pdf",
                   [r for r in CATALOG_A if not r.get("multi_frame") or r["drawing_number"] == "S-007A"],
                   missing_listed=LISTED_MISSING, omit="S-EXTRA")
    meta_b["s005"] = D.draw_s005_numbers(build / "B" / "S-005-numbers.pdf", conflict=True)
    meta_b["s006"] = D.draw_s006_index(build / "B" / "S-006-index.pdf", broken=True)
    meta_b["s007"] = D.draw_s007_multi(build / "B" / "S-007-multi.pdf")
    meta_b["s008"] = D.draw_s008_no_title(build / "B" / "S-008-notitle.pdf")
    meta_b["s009"] = D.draw_s009_blur(build / "B" / "S-009-blur.pdf")
    meta_b["s010"] = D.draw_s010_same_name(build / "B" / "S-010-samename.pdf")
    meta_b["prefix"] = D.draw_prefix_bad(build / "B" / "S-PREFIX-bad.pdf")
    meta_b["extra"] = D.draw_extra(build / "B" / "S-EXTRA.pdf")
    meta_b["scope"] = D.draw_scope_out(build / "B" / "DEC-01-scope.pdf")

    answers = build_answers(meta_a, meta_b)
    (out / "answers.json").write_text(
        json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 回放
    files_a = sorted((build / "A").glob("*.pdf"))
    files_b = sorted((build / "B").glob("*.pdf"))
    replay_a = build_replay(out, "A", files_a, facts_from_meta(meta_a, "A"))
    replay_b = build_replay(out, "B", files_b, facts_from_meta(meta_b, "B"))

    # 异常文件
    anomaly_entries = anomalies_mod.write_anomalies(out / "anomalies")

    # 可选 DWG
    dwg_info = try_make_dwg(out)

    # MANIFEST
    # 仓库内 MANIFEST 仅稳定字段：无 generated_at_local；optional_dwg 不含可变 sha256
    manifest = {
        "kind": "synthetic-fixtures-manifest",
        "is_golden_sample": False,
        "disclaimer": "自制开发夹具，不是金样本。",
        "poppler_version": _poppler_version(),
        "determinism_scope": (
            "第6条仅约束 PDF 与 answers/replay/anomalies 等固定产物；"
            "不含 MANIFEST 元数据时间戳，亦不含 optional DWG/DXF 字节"
        ),
        "files": [],
        "anomalies": anomaly_entries,
        "optional_dwg": dwg_info,
        "unchanged_check": {
            "A/S-001-plan.pdf": sha256_file(build / "A" / "S-001-plan.pdf"),
            "B/S-001-plan.pdf": sha256_file(build / "B" / "S-001-plan.pdf"),
            "byte_identical": sha256_file(build / "A" / "S-001-plan.pdf")
            == sha256_file(build / "B" / "S-001-plan.pdf"),
        },
    }
    for folder in ("A", "B"):
        for path in sorted((build / folder).glob("*")):
            if path.is_file():
                manifest["files"].append({
                    "relative_path": f"build/{folder}/{path.name}",
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                    "purpose": f"{folder} 套图纸",
                })
    for path in sorted((out / "anomalies").glob("*")):
        if path.is_file():
            # anomalies 已在 anomaly_entries；补 sha
            for entry in manifest["anomalies"]:
                if entry["relative_path"].endswith(path.name):
                    entry["sha256"] = sha256_file(path)
    # 提交进仓的元数据文件
    for name in ("answers.json",):
        p = out / name
        manifest["files"].append({
            "relative_path": name,
            "sha256": sha256_file(p),
            "size": p.stat().st_size,
            "purpose": "开发答案",
        })
    for suite, idx in (("A", replay_a), ("B", replay_b)):
        p = out / "replay" / suite / "index.json"
        manifest["files"].append({
            "relative_path": f"replay/{suite}/index.json",
            "sha256": sha256_file(p),
            "size": p.stat().st_size,
            "purpose": f"{suite} 套 Mock 回放索引",
        })

    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # meta 供测试比对（不强制提交）
    (build / "meta_a.json").write_text(json.dumps(meta_a, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (build / "meta_b.json").write_text(json.dumps(meta_b, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "out": str(out),
        "answers": answers,
        "manifest": manifest,
        "replay_a": replay_a,
        "replay_b": replay_b,
        "dwg": dwg_info,
    }


def compare_dirs(a: Path, b: Path) -> list[str]:
    """比较两棵生成树中「第6条」约束产物的相对路径与 SHA；返回差异列表。

    明确只约束：PDF、answers.json、replay/*/index.json、anomalies/*。
    不纳入：MANIFEST.json（元数据）、build/optional/（LibreDWG/ezdxf 非确定）、meta_*.json。
    """
    def collect(root: Path) -> dict[str, str]:
        mapping = {}
        for path in root.rglob("*"):
            if not path.is_file() or "meta_" in path.name:
                continue
            rel = path.relative_to(root).as_posix()
            if path.name == "MANIFEST.json" or rel.startswith("build/optional/"):
                continue
            mapping[rel] = sha256_file(path)
        return mapping
    ma, mb = collect(a), collect(b)
    diffs = []
    for key in sorted(set(ma) | set(mb)):
        if key not in ma:
            diffs.append(f"仅在 B: {key}")
        elif key not in mb:
            diffs.append(f"仅在 A: {key}")
        elif ma[key] != mb[key]:
            diffs.append(f"SHA 不同: {key}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DC-05 自制测试图纸集生成器（非金样本）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    parser.add_argument("--compare-twice", action="store_true",
                        help="生成到临时目录两次并比对 SHA")
    args = parser.parse_args(argv)
    if args.compare_twice:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="dc05-a-") as da, tempfile.TemporaryDirectory(prefix="dc05-b-") as db:
            generate(Path(da))
            generate(Path(db))
            diffs = compare_dirs(Path(da), Path(db))
            if diffs:
                print("确定性比对失败:", *diffs, sep="\n  ")
                return 1
            print("确定性比对通过：两次生成的 PDF/answers/replay/anomalies SHA 一致（第6条范围）")
            return 0
    result = generate(args.out)
    print(f"已生成到 {result['out']}")
    print(f"异常文件 {len(result['manifest']['anomalies'])} 个；"
          f"A 回放 {len(result['replay_a']['entries'])} 条；"
          f"B 回放 {len(result['replay_b']['entries'])} 条")
    print(f"可选 DWG: {result['dwg']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
