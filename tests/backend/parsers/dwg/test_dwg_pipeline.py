"""从公共入口验证状态、隔离、缓存、定位与预览契约。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.parsers.base import Parser
from backend.parsers.dwg import (DwgFileStatus, DwgOptions, DwgParser, discover_tools,
                                 parse_dwg_file, parse_dwg_file_async, parse_dwg_files)
from backend.store.models import Source


def test_missing_components_all_unreadable(request_dwg, tmp_path, monkeypatch):
    monkeypatch.setenv("DRAWCHECK_LIBREDWG_BIN", str(tmp_path / "无组件"))
    results = parse_dwg_files([replace(request_dwg, original_id=f"自制{i}") for i in range(6)])
    assert len(results) == 6
    for result in results:
        assert result.status == DwgFileStatus.UNREADABLE
        assert result.message == "读不了：DWG 解析组件不可用"
        assert not result.result.units
        assert result.sha256_before == result.sha256_after == request_dwg.sha256
    assert not request_dwg.cache_dir.exists()
    assert isinstance(DwgParser(), Parser)
    assert DwgParser().parse(request_dwg).incomplete == ["DWG 解析组件不可用"]


def test_path_discovery(stub_tools, monkeypatch):
    assert discover_tools().dwgread == str(stub_tools / "dwgread")
    monkeypatch.delenv("DRAWCHECK_LIBREDWG_BIN")
    monkeypatch.setenv("PATH", str(stub_tools))
    assert discover_tools().dwg2dxf == str(stub_tools / "dwg2dxf")
    (stub_tools / "dwgread").unlink()
    assert discover_tools() is None


def wait_marker(path):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), "替身未启动，无法证明解析进行中"
    return int(path.read_text())


@pytest.mark.parametrize("mode", ["timeout", "ignore_term"])
def test_timeout_terminates_process(request_dwg, stub_tools, tmp_path, monkeypatch, mode):
    marker = tmp_path / "转换进程.pid"
    monkeypatch.setenv("DC07_STUB_MODE", mode)
    monkeypatch.setenv("DC07_MARKER", str(marker))
    start = time.monotonic()
    result = parse_dwg_file(request_dwg, options=DwgOptions(timeout_sec=0.5, worker_timeout_sec=8))
    assert time.monotonic() - start < 8
    assert result.status == DwgFileStatus.UNREADABLE
    assert "超时" in result.reason and "已终止" in result.reason
    pid = wait_marker(marker)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_worker_timeout_terminates_conversion(request_dwg, stub_tools, tmp_path, monkeypatch):
    marker = tmp_path / "工作进程内转换.pid"
    monkeypatch.setenv("DC07_STUB_MODE", "timeout")
    monkeypatch.setenv("DC07_MARKER", str(marker))
    result = parse_dwg_file(request_dwg, options=DwgOptions(timeout_sec=30, worker_timeout_sec=5))
    assert result.status == DwgFileStatus.UNREADABLE
    assert "解析工作进程 超时" in result.reason
    pid = wait_marker(marker)
    # 允许系统短暂回收后代进程；不得继续运行。
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        if sys.platform.startswith("linux"):
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                if any(line.startswith("State:") and "Z" in line for line in status.splitlines()):
                    # 容器 PID 1 可能延迟回收孤儿；僵尸已终止，不等同仍在转换。
                    break
            except FileNotFoundError:
                break
        time.sleep(0.02)
    else:
        pytest.fail("工作进程超时后转换器仍存活")


def test_health_with_async_parse_in_same_app(request_dwg, stub_tools, tmp_path, monkeypatch):
    marker, release = tmp_path / "健康检查.pid", tmp_path / "释放慢转换"
    monkeypatch.setenv("DC07_STUB_MODE", "slow")
    monkeypatch.setenv("DC07_MARKER", str(marker))
    monkeypatch.setenv("DC07_RELEASE", str(release))
    app = create_app("dc07-test")

    @app.get("/api/test-dwg")
    async def run_parse():
        result = await parse_dwg_file_async(request_dwg, options=DwgOptions(timeout_sec=10))
        return {"status": result.status.value}

    headers = {"Authorization": "Bearer dc07-test"}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, "/api/test-dwg", headers=headers)
        try:
            wait_marker(marker)
            assert not pending.done()
            start = time.monotonic()
            response = client.get("/api/health", headers=headers)
            elapsed = time.monotonic() - start
            assert response.status_code == 200 and response.json()["status"] == "ok"
            assert elapsed < 1, f"健康检查耗时 {elapsed:.3f} 秒"
            assert not pending.done()
            print(f"慢转换进行中，健康检查耗时 {elapsed:.4f} 秒")
        finally:
            release.touch()
        assert pending.result(timeout=10).json()["status"] == "读不了"


def test_cache_sha_text_and_split_frames(request_dwg, stub_tools):
    before = hashlib.sha256(request_dwg.readonly_path.read_bytes()).hexdigest()
    result = parse_dwg_file(request_dwg)
    assert result.status in {DwgFileStatus.SUCCESS, DwgFileStatus.PARTIAL}, result.reason
    assert len(result.result.units) == 2
    for ordinal, unit in enumerate(result.result.units, 1):
        assert unit.kind == "dwg_frame" and unit.ordinal == ordinal
        assert unit.drawing_number == ""
        assert unit.preview_relative_path and "\\" not in unit.preview_relative_path
        relative = Path(unit.preview_relative_path)
        assert not relative.is_absolute() and ".." not in relative.parts
        preview = request_dwg.cache_dir / relative
        assert preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert len(unit.texts) == 1
        assert unit.texts[0].source == Source.CAD_TEXT
        assert unit.texts[0].text == f"位置{(ordinal - 1) * 200}"
        box = unit.texts[0].bbox
        assert 0 <= box.x0 <= box.x1 <= 1 and 0 <= box.y0 <= box.y1 <= 1
        assert box.y0 < 0.5, "CAD 的上方应映射到左上原点的上半区"
    after = hashlib.sha256(request_dwg.readonly_path.read_bytes()).hexdigest()
    assert before == after == result.sha256_before == result.sha256_after
    assert list(request_dwg.cache_dir.rglob("*.dxf"))
    assert list(request_dwg.readonly_path.parent.iterdir()) == [request_dwg.readonly_path]


def test_clean_frame_has_success_status(request_dwg, stub_tools, tmp_path, monkeypatch):
    import ezdxf

    doc = ezdxf.new()
    block = doc.blocks.new("TK-CLEAN")
    block.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True)
    doc.modelspace().add_blockref("TK-CLEAN", (0, 0))
    path = tmp_path / "纯几何图框.dxf"
    doc.saveas(path)
    monkeypatch.setenv("DC07_DXF", str(path))
    result = parse_dwg_file(request_dwg)
    assert result.status == DwgFileStatus.SUCCESS, result.reason
    assert result.result.units and all(u.preview_relative_path for u in result.result.units)
    assert not result.result.incomplete


def test_layout_fallback_and_missing_resources_are_partial(request_dwg, stub_tools, tmp_path, monkeypatch):
    import ezdxf

    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True)
    doc.styles.get("Standard").dxf.font = "DC07-不存在的字体.shx"
    doc.modelspace().add_text("TEST", dxfattribs={"insert": (10, 60), "height": 4})
    xref = doc.blocks.new("外部参照")
    xref.block.dxf.flags = 4
    xref.block.dxf.xref_path = "不存在的外参.dwg"
    path = tmp_path / "缺资源且无图框.dxf"
    doc.saveas(path)
    monkeypatch.setenv("DC07_DXF", str(path))
    result = parse_dwg_file(request_dwg)
    assert result.status == DwgFileStatus.PARTIAL, result.reason
    assert "整布局" in result.reason and "外部参照" in result.reason and "缺字体" in result.reason
    assert result.result.units[0].preview_relative_path


@pytest.mark.parametrize("mode", ["broken", "unreadable_dxf"])
def test_json_fallback_is_partial(request_dwg, stub_tools, monkeypatch, mode):
    monkeypatch.setenv("DC07_STUB_MODE", mode)
    result = parse_dwg_file(request_dwg)
    assert result.status == DwgFileStatus.PARTIAL, result.reason
    assert "JSON" in result.reason and "预览" in result.reason
    assert result.statistics["source"] == "libredwg_json"
    unit = result.result.units[0]
    assert unit.preview_relative_path is None
    assert [t.text for t in unit.texts] == ["JSON中文文字"]
    assert unit.texts[0].source == Source.CAD_TEXT
    assert list(request_dwg.cache_dir.rglob("drawing.json"))


@pytest.mark.parametrize("mode", ["invalid_json", "empty_json", "crash"])
def test_failures_never_report_success(request_dwg, stub_tools, monkeypatch, mode):
    monkeypatch.setenv("DC07_STUB_MODE", mode)
    result = parse_dwg_file(request_dwg)
    assert result.status == DwgFileStatus.UNREADABLE
    assert result.reason and not result.result.units
    if mode == "crash":
        assert "崩溃" in result.reason


def test_sha_mismatch_does_not_start_conversion(request_dwg, stub_tools):
    result = parse_dwg_file(replace(request_dwg, sha256="0" * 64))
    assert result.status == DwgFileStatus.UNREADABLE
    assert "SHA-256" in result.reason
    assert not request_dwg.cache_dir.exists()


def test_cache_cannot_redirect_to_originals(request_dwg, stub_tools, tmp_path):
    request_dwg.cache_dir.symlink_to(request_dwg.readonly_path.parent, target_is_directory=True)
    result = parse_dwg_file(request_dwg)
    assert result.status == DwgFileStatus.UNREADABLE and "符号链接" in result.reason
    assert list(request_dwg.readonly_path.parent.iterdir()) == [request_dwg.readonly_path]


def test_options_from_environment(monkeypatch):
    monkeypatch.setenv("DRAWCHECK_DWG_TIMEOUT_SEC", "0.25")
    monkeypatch.setenv("DRAWCHECK_DWG_MEMORY_MB", "1024")
    assert DwgOptions.from_env().timeout_sec == 0.25
    assert DwgOptions.from_env().memory_mb == 1024
    with pytest.raises(ValueError):
        DwgOptions(timeout_sec=float("nan"))


def test_json_fallback_tolerates_non_utf8(tmp_path):
    """LibreDWG JSON 可能夹杂非 UTF-8 字节；不得整文件读不了。"""
    from backend.parsers.dwg.frames import read_json_fallback
    # 合法 JSON 结构 + 字符串内非法 UTF-8 字节（0xA8）
    payload = (
        b'{"OBJECTS":[{"entity":"TEXT","text":"ok'
        + bytes([0xA8])
        + b'","ins_pt":{"x":1,"y":2},"ownerhandle":1},'
        + b'{"object":"BLOCK_HEADER","name":"*MODEL_SPACE","handle":1}]}'
    )
    path = tmp_path / "badenc.json"
    path.write_bytes(payload)
    stats, texts, reasons = read_json_fallback(path)
    assert stats["source"] == "libredwg_json"
    assert stats["entity_total"] >= 1
    assert any("JSON" in r for r in reasons)


# ---------------- R2 回归：M1 JSON 模型空间文字 ----------------

def test_json_fallback_model_space_by_entmode(tmp_path):
    """真实 LibreDWG 0.13.3 字段形状：模型空间实体 entmode=2 且无 ownerhandle；
    块内实体 entmode=0、ownerhandle 为 [code,size,value,absref]；图纸空间 entmode=1。"""
    import json
    from backend.parsers.dwg.frames import read_json_fallback

    objects = [
        {"object": "BLOCK_HEADER", "name": "*Model_Space", "handle": [0, 1, 31],
         "ownerhandle": [4, 1, 1, 1], "entities": [[3, 2, 100, 100]]},
        {"object": "BLOCK_HEADER", "name": "TK-A2", "handle": [0, 2, 542]},
        {"entity": "TEXT", "handle": [0, 2, 100], "entmode": 2, "ins_pt": [2843.73, 1602.13],
         "text_value": "300*150槽式电缆桥架"},
        {"entity": "MTEXT", "handle": [0, 2, 101], "entmode": 2, "ins_pt": [2675.9, 1912.6, 0.0],
         "text_value": None, "text": "1、工程概况\\P本工程为光伏回建项目"},
        {"entity": "TEXT", "handle": [0, 2, 564], "entmode": 0, "ownerhandle": [12, 1, 22, 542],
         "ins_pt": [585.1, 181.9], "text_value": "图框内块文字"},
        {"entity": "ATTRIB", "handle": [0, 2, 600], "entmode": 0, "ownerhandle": [4, 2, 900, 900],
         "ins_pt": [1.0, 2.0], "text_value": "1-6-18"},
        {"entity": "TEXT", "handle": [0, 2, 700], "entmode": 1, "ins_pt": [5.0, 5.0], "text_value": "图纸空间"},
        # 旧判断保留作补充：ownerhandle 指向 *Model_Space。
        {"entity": "TEXT", "handle": [0, 2, 800], "entmode": 0, "ownerhandle": [4, 1, 31, 31],
         "ins_pt": [3000.0, 1500.0], "text_value": "归属模型空间"},
        {"entity": "TEXT", "handle": [0, 2, 801], "entmode": 2, "ins_pt": [1.0, 1.0], "text_value": "  "},
    ]
    path = tmp_path / "real-shape.json"
    path.write_text(json.dumps({"OBJECTS": objects}, ensure_ascii=False), encoding="utf-8")
    stats, texts, reasons = read_json_fallback(path)
    assert [t.text for t in texts] == ["300*150槽式电缆桥架", "1、工程概况\n本工程为光伏回建项目", "归属模型空间"]
    assert all(t.source == Source.CAD_TEXT for t in texts)
    assert all(0 <= t.bbox.x0 <= 1 and 0 <= t.bbox.y0 <= 1 for t in texts)
    assert stats["cad_text_positioned"] == 3
    assert stats["cad_text_skipped"] == {"块内（含图框属性）": 2, "图纸空间": 1}
    joined = "；".join(reasons)
    assert "模型空间 CAD 文字 3 条" in joined and "未纳入单元" in joined


# ---------------- R2 回归：M2 非 TK-* 图框不得静默丢弃 ----------------

def test_match_sheet_standard_sizes():
    from backend.parsers.dwg.frames import match_sheet

    assert match_sheet(842, 595)[0] == "A1"
    assert match_sheet(211, 298)[0] == "A4"   # D0103 目录页 / A4 说明页实测外包框
    assert match_sheet(421, 298)[0] == "A3"
    assert match_sheet(4200, 2970) is not None  # 放大插入仍可识别（A 系列长宽比相同，幅面名只作说明）
    assert match_sheet(100, 80) is None
    assert match_sheet(58.6, 29.1) is None     # 版本章等小图块不是图幅


def test_non_tk_sheet_frame_kept_and_outside_text_reported(tmp_path):
    import ezdxf
    from backend.parsers.dwg.frames import find_frames

    doc = ezdxf.new("R2010")
    tk = doc.blocks.new("TK-A3")
    tk.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    ml = doc.blocks.new("ZS-ML1")
    ml.add_lwpolyline([(0, 0), (210, 0), (210, 297), (0, 297)], close=True)
    stamp = doc.blocks.new("ZS-VERSION")
    stamp.add_lwpolyline([(0, 0), (58, 0), (58, 29), (0, 29)], close=True)
    model = doc.modelspace()
    model.add_blockref("TK-A3", (500, 0))
    model.add_blockref("ZS-ML1", (0, 0))
    model.add_blockref("ZS-VERSION", (20, 20))      # 在目录页内的小图块，不得另成单元
    model.add_text("图纸目录", dxfattribs={"insert": (50, 250), "height": 5})
    model.add_text("图框内", dxfattribs={"insert": (600, 100), "height": 5})
    model.add_text("游离文字", dxfattribs={"insert": (2000, 2000), "height": 5})
    frames, reasons, _ = find_frames(doc)
    names = [f.name for f in frames]
    assert names == ["Model / TK-A3", "Model / ZS-ML1（图框待确认）"]
    assert [t.text for t in frames[1].texts] == ["图纸目录"]
    joined = "；".join(reasons)
    assert "块名不是常见图框（当前为：ZS-ML1）" in joined
    assert "请核对是不是一张图" in joined and "A4" in joined
    assert "1 条 CAD 文字不在已识别图框内" in joined


# ---------------- R2 回归：M3 中文字形自检与原因 ----------------

def _cjk_doc(font):
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.styles.new("HZ", dxfattribs={"font": font})
    block = doc.blocks.new("TK-A3")
    block.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    doc.modelspace().add_blockref("TK-A3", (0, 0))
    doc.modelspace().add_text("中文图纸说明", dxfattribs={"insert": (50, 150), "height": 20, "style": "HZ"})
    return doc


def test_cjk_preview_uses_verified_font_or_says_unreadable(tmp_path):
    from backend.parsers.dwg.frames import find_frames
    from backend.parsers.dwg.render import find_cjk_font, missing_glyphs, render_frame

    doc = _cjk_doc("DC07-缺失中文.shx")
    frames, _, _ = find_frames(doc)
    reasons = render_frame(doc, frames[0], tmp_path / "u.png")
    joined = "；".join(reasons)
    assert (tmp_path / "u.png").read_bytes().startswith(b"\x89PNG")
    assert "缺字体" in joined and "DC07-缺失中文.shx" in joined
    cjk = find_cjk_font()
    if cjk is None:
        assert "本机无可用中文字体" in joined and "预览中文不可读" in joined
        pytest.skip("本机无可通过字形自检的中文字体，仅验证了「不可读」原因分支")
    assert not missing_glyphs(cjk[0], "中文图纸说明")
    assert f"已用本机 {cjk[0]} 替代" in joined
    assert "预览中文不可读" not in joined
    print(f"中文替代字体：{cjk[0]}")


def test_cjk_unreadable_reason_when_no_font(tmp_path, monkeypatch):
    from backend.parsers.dwg import render
    from backend.parsers.dwg.frames import find_frames
    from ezdxf.fonts import fonts

    monkeypatch.setattr(render, "find_cjk_font", lambda: None)
    fallback = fonts.font_manager.fallback_font_name()
    if not render.missing_glyphs(fallback, "中"):
        pytest.skip(f"本机默认字体 {fallback} 自带中文字形，无法构造「不可读」场景")
    doc = _cjk_doc("DC07-缺失中文.shx")
    frames, _, _ = find_frames(doc)
    joined = "；".join(render.render_frame(doc, frames[0], tmp_path / "u.png"))
    assert "本机无可用中文字体，未能替代" in joined
    assert "中文字形无法渲染，预览中文不可读" in joined and "显示为□" in joined


# ---------------- R2 回归：M4 内存不足原因非空 ----------------

def test_memory_error_reason_is_explicit(tmp_path, monkeypatch):
    from backend.parsers.dwg import _worker

    def boom(path):
        raise MemoryError()

    monkeypatch.setattr(_worker, "read_json_fallback", boom)
    stats, texts, reasons, ok = _worker.json_fallback(tmp_path / "x.json", 333)
    assert not ok and not texts
    assert reasons == ["LibreDWG JSON 兜底时解析内存超过上限 333 MB（DRAWCHECK_DWG_MEMORY_MB，Linux 地址空间限额），未完成；可调大上限后重试"]
    assert _worker.describe_error(ValueError(), "某步", 1) == "某步失败：ValueError"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS 仅 Linux 施加")
def test_small_memory_limit_gives_nonempty_reason(request_dwg, stub_tools, monkeypatch):
    monkeypatch.setenv("DC07_STUB_MODE", "bigjson")
    result = parse_dwg_file(request_dwg, options=DwgOptions(memory_mb=256))
    print(f"小内存上限 256 MB：{result.status.value}：{result.reason}")
    assert result.status == DwgFileStatus.UNREADABLE
    assert "内存" in result.reason and "256 MB" in result.reason
    assert "兜底失败：；" not in result.reason and not result.reason.endswith("：")
