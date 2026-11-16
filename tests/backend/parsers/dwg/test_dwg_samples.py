"""客户样本只在显式设置目录时读取；本机缺样本必须显示跳过。"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.parsers.base import ParseInput
from backend.parsers.dwg import DwgFileStatus, DwgOptions, parse_dwg_file, discover_tools


KEYS = ("D0103", "D0202", "D0203", "D0302", "D0402", "D0501")
EXPECTED = {"D0103": "成功", "D0202": "部分成功", "D0203": "部分成功",
            "D0302": "部分成功", "D0402": "部分成功", "D0501": "读不了"}


@pytest.mark.skipif(not os.environ.get("DRAWCHECK_SAMPLES"), reason="未设 DRAWCHECK_SAMPLES；样本实跑留给 box")
@pytest.mark.parametrize("key", KEYS)
def test_six_file_status_table(key, tmp_path):
    samples = Path(os.environ["DRAWCHECK_SAMPLES"])
    matches = [p for p in samples.rglob("*") if p.suffix.lower() == ".dwg" and key in p.name]
    assert len(matches) == 1, f"样本 {key} 缺失或不唯一"
    source = matches[0]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    request = ParseInput(key, source, digest, tmp_path / "cache", "dwg")
    result = parse_dwg_file(request)
    # 即使组件不可用，也必须为六文件逐一给出状态；不能记成样本解析成功。
    if discover_tools() is None:
        assert result.status == DwgFileStatus.UNREADABLE
        assert result.reason == "DWG 解析组件不可用"
    elif key == "D0501":
        assert result.status == DwgFileStatus.UNREADABLE, "D0501 与 Spike 不同，需保留证据复核"
    elif key == "D0103":
        # 缺字体、外参等运行环境差异允许降为部分成功，必须有预览。
        assert result.status in {DwgFileStatus.SUCCESS, DwgFileStatus.PARTIAL}, result.reason
        assert all(unit.preview_relative_path for unit in result.result.units)
        # R2/M2 + N1：4 张 TK-* + 2 张非 TK；单元名自带「图框待确认」，原因用裁定句式。
        assert len(result.result.units) == 6, [u.name for u in result.result.units]
        pending = [u for u in result.result.units if "图框待确认" in u.name]
        assert {u.name for u in pending} >= {
            "Model / ZS-ZJ-ZW-ML1（图框待确认）",
            "Model / ZS-ZJ-ZW-A4P（图框待确认）",
        }
        assert "块名不是常见图框（当前为：ZS-ZJ-ZW-ML1）" in result.reason
        assert "块名不是常见图框（当前为：ZS-ZJ-ZW-A4P）" in result.reason
        assert "请核对是不是一张图" in result.reason
        # R2/M3：缺字体须逐个列出，中文可读性须如实说明。
        assert "缺字体" in result.reason and "hztxt.shx" in result.reason
    else:
        assert result.status == DwgFileStatus.PARTIAL, result.reason
        # R2/M1：JSON 兜底必须抽出模型空间文字。
        assert result.statistics.get("cad_text_positioned", 0) > 0, result.reason
    for unit in result.result.units:
        if unit.preview_relative_path is not None:
            relative = Path(unit.preview_relative_path)
            assert not relative.is_absolute() and ".." not in relative.parts
            with (request.cache_dir / relative).open("rb") as preview:
                assert preview.read(8) == b"\x89PNG\r\n\x1a\n"
    assert result.reason and result.sha256_before == result.sha256_after == digest
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    texts = sum(len(unit.texts) for unit in result.result.units)
    if discover_tools() is not None and key != "D0501":
        assert texts > 0, f"{key} 未提取到 CAD 文字：{result.reason}"
    row = {"文件": key, "Spike预期": EXPECTED[key], "实测状态": result.status.value,
           "原因": result.reason, "单元数": len(result.result.units), "CAD文字条数": texts,
           "单元": [{"序号": u.ordinal, "名称": u.name, "文字条数": len(u.texts),
                    "有预览": bool(u.preview_relative_path)} for u in result.result.units],
           "统计": result.statistics}
    out_dir = Path(os.environ.get("DRAWCHECK_DWG_STATUS_DIR") or tmp_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{key}-状态.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in row.items() if k not in {"统计", "单元"}}, ensure_ascii=False))


@pytest.mark.skipif(not os.environ.get("DRAWCHECK_SAMPLES"), reason="未设 DRAWCHECK_SAMPLES；样本实跑留给 box")
def test_d0202_small_memory_limit_reason(tmp_path):
    """R2/M4：上限压小时必须写明内存原因，不能是空串。D0202 实测约 1000 MB 才能完成 JSON 兜底。"""
    if discover_tools() is None:
        pytest.skip("LibreDWG 不可用")
    import sys
    if not sys.platform.startswith("linux"):
        pytest.skip("RLIMIT_AS 仅 Linux 施加")
    samples = Path(os.environ["DRAWCHECK_SAMPLES"])
    source = next(p for p in samples.rglob("*") if p.suffix.lower() == ".dwg" and "D0202" in p.name)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = parse_dwg_file(ParseInput("D0202", source, digest, tmp_path / "cache", "dwg"),
                            options=DwgOptions(memory_mb=768))
    print(f"D0202 上限 768 MB：{result.status.value}：{result.reason}")
    assert result.status == DwgFileStatus.UNREADABLE
    assert "768 MB" in result.reason and "内存" in result.reason
