"""自制 DXF 与可执行转换器替身，不依赖客户样本或本机 LibreDWG。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

from backend.parsers.base import ParseInput


@pytest.fixture
def request_dwg(tmp_path):
    originals = tmp_path / "project" / "originals"
    originals.mkdir(parents=True)
    source = originals / "只读原图"
    source.write_bytes(b"AC1027\x00\x01\x02")
    source.chmod(0o444)
    return ParseInput("自制原图", source, hashlib.sha256(source.read_bytes()).hexdigest(),
                      tmp_path / "project" / "cache", "dwg")


@pytest.fixture
def fixture_dxf(tmp_path):
    import ezdxf

    doc = ezdxf.new("R2010")
    block = doc.blocks.new("TK-TEST")
    block.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True)
    model = doc.modelspace()
    for x in (0, 200):
        model.add_blockref("TK-TEST", (x, 0))
        model.add_text(f"位置{x}", dxfattribs={"insert": (x + 10, 60), "height": 4})
    path = tmp_path / "自制输入.dxf"
    doc.saveas(path)
    return path


@pytest.fixture
def stub_tools(tmp_path, monkeypatch, fixture_dxf):
    directory = tmp_path / "假组件"
    directory.mkdir()
    script = '''
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

if "--version" in sys.argv:
    print("LibreDWG 0.13.3")
    sys.exit(0)
kind = Path(sys.argv[0]).name
mode = os.environ.get("DC07_STUB_MODE", "valid")
target = Path(sys.argv[sys.argv.index("-o") + 1])
marker = os.environ.get("DC07_MARKER")
if mode in ("timeout", "slow", "ignore_term") and kind == "dwg2dxf":
    if mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if marker:
        Path(marker).write_text(str(os.getpid()))
    if mode == "slow":
        release = Path(os.environ["DC07_RELEASE"])
        while not release.exists():
            time.sleep(0.02)
    else:
        time.sleep(30)
    sys.exit(1)
if mode == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if kind == "dwg2dxf":
    if mode in ("broken", "invalid_json", "empty_json", "fallback", "slow", "bigjson"):
        target.write_text("0\\nSECTION\\n2\\nENTITIES\\n")
    elif mode == "unreadable_dxf":
        target.write_text("不可读的 DXF\\n0\\nEOF\\n")
    else:
        shutil.copyfile(os.environ["DC07_DXF"], target)
else:
    if mode in ("timeout", "slow", "ignore_term"):
        sys.exit(1)
    if mode == "bigjson":
        # 约 40 MB 的合法 JSON；完整 json.loads 会放大到数百 MB，用于在小内存上限下触发 MemoryError。
        with target.open("w", encoding="utf-8") as out:
            out.write('{"OBJECTS": [')
            row = '{"entity": "LINE", "entmode": 2, "start": [0.0, 0.0, 0.0], "end": [1.0, 1.0, 0.0]},'
            for _ in range(500_000):
                out.write(row)
            out.write('{"entity": "LINE", "entmode": 2}]}')
    elif mode == "invalid_json":
        target.write_text('{"OBJECTS": [')
    elif mode == "empty_json":
        target.write_text('{"OBJECTS": []}')
    else:
        target.write_text(json.dumps({"OBJECTS": [
            {"object": "BLOCK_HEADER", "name": "*Model_Space", "handle": [0, 1, 42]},
            {"object": "LAYOUT", "name": "模型"},
            {"entity": "LINE", "start": [0, 0, 0], "end": [100, 80, 0]},
            {"entity": "TEXT", "text_value": "JSON中文文字", "ins_pt": [10, 20, 0],
             "ownerhandle": [4, 1, 42]},
            {"entity": "TEXT", "text_value": "未知块归属，不定位", "ins_pt": [1, 2, 0]},
            {"entity": "ACAD_PROXY_ENTITY"}
        ]}), encoding="utf-8")
'''
    for name in ("dwg2dxf", "dwgread"):
        path = directory / name
        path.write_text(f"#!{sys.executable}\n" + script, encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("DRAWCHECK_LIBREDWG_BIN", str(directory))
    monkeypatch.setenv("DC07_DXF", str(fixture_dxf))
    return directory
