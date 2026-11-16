"""覆盖真实 pypdf 拆页；Poppler 实测缺工具则明确跳过，模拟测试单独命名。"""
from dataclasses import replace
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

from pypdf import PdfReader
import pytest

from backend.parsers.base import Parser
from backend.parsers.pdf import PDFError, PDFParser
from backend.store import StoreError
from backend.store.originals import sha256_file


HAS_POPPLER = bool(shutil.which("pdfinfo") and shutil.which("pdftoppm"))


def test_split_and_dpi_with_mock_poppler(request_factory, mock_poppler):
    request = request_factory()
    before = request.readonly_path.read_bytes()
    parser = PDFParser()
    assert isinstance(parser, Parser)
    result = parser.parse(request)
    assert not result.incomplete
    assert [unit.ordinal for unit in result.units] == [1, 2, 3]
    assert [(unit.width_points, unit.height_points) for unit in result.units] == [(200, 100), (100, 300), (400, 200)]
    for unit in result.units:
        assert len(PdfReader(request.cache_dir / unit.page_relative_path).pages) == 1
        for field in (unit.page_relative_path, unit.preview_relative_path, unit.review_relative_path):
            assert (request.cache_dir / field).is_file()
    renders = [(cmd, options) for cmd, options in mock_poppler if cmd[0] == "pdftoppm"]
    assert [cmd[cmd.index("-r") + 1] for cmd, _ in renders] == ["72", "150"] * 3
    assert all(0 < opts["timeout"] <= 60 for _, opts in renders)
    assert request.readonly_path.read_bytes() == before
    assert not request.readonly_path.stat().st_mode & 0o222


@pytest.mark.parametrize("failed_page,stage,error_code", [(2, "preview", "RENDER_TIMEOUT"), (2, "review", "RENDER_FAILED")])
def test_only_failed_page_is_unreadable_with_mock_poppler(request_factory, monkeypatch, failed_page, stage, error_code):
    request = request_factory()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "pdftoppm":
            prefix = Path(command[-1])
            if prefix.parent.name == f"page-{failed_page}" and prefix.name == stage:
                if error_code == "RENDER_TIMEOUT":
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                raise subprocess.CalledProcessError(1, command)
            prefix.with_suffix(".png").write_bytes(("模拟图").encode("utf-8"))
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", run)
    result = PDFParser().parse(request)
    assert [unit.ordinal for unit in result.units] == [1, 3]
    assert [(f.page, f.code) for f in result.failures] == [(2, error_code)]
    assert "第 2 页" in result.incomplete[0]
    assert not list(request.cache_dir.glob("*/page-2"))
    assert any("page-3" in str(cmd) for cmd in calls)


@pytest.mark.parametrize("case,code", [("corrupt", "PDF_CORRUPT"), ("encrypted", "PDF_ENCRYPTED"), ("empty", "PDF_EMPTY")])
def test_invalid_pdf_rejected_before_poppler(request_factory, case, code, monkeypatch):
    request = request_factory(sizes=() if case == "empty" else ((100, 100),), encrypted=case == "encrypted")
    if case == "corrupt":
        request.readonly_path.chmod(0o644)
        request.readonly_path.write_bytes(("%PDF-1.7\n坏的最小文件").encode("utf-8"))
        request.readonly_path.chmod(0o444)
        request = replace(request, sha256=sha256_file(request.readonly_path))
    def unexpected(*_args, **_kwargs):
        pytest.fail("无效 PDF 不应调用渲染器")
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", unexpected)
    with pytest.raises(PDFError) as exc:
        PDFParser().parse(request)
    assert exc.value.code == code
    assert not request.cache_dir.exists()


def test_large_page_scale_cap_with_mock_poppler(request_factory, mock_poppler):
    request = request_factory(sizes=((20000, 1000),))
    PDFParser(max_dimension=2048).parse(request)
    renders = [cmd for cmd, _ in mock_poppler if cmd[0] == "pdftoppm"]
    assert len(renders) == 2
    assert all(cmd[cmd.index("-scale-to") + 1] == "2048" for cmd in renders)


def test_missing_poppler_is_explicit(request_factory, monkeypatch):
    request = request_factory()
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", unavailable)
    with pytest.raises(PDFError) as exc:
        PDFParser().parse(request)
    assert exc.value.code == "POPPLER_UNAVAILABLE"


def test_actual_subprocess_timeout_kills_and_reaps(tmp_path):
    marker = tmp_path / "子进程编号"
    code = "import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(marker)
    with pytest.raises(PDFError, match="渲染超时"):
        PDFParser._run([sys.executable, "-c", code], 1)
    pid = int(marker.read_text())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_cache_and_hash_boundaries(request_factory, mock_poppler, tmp_path):
    request = request_factory()
    for cache in (request.readonly_path.parent, request.readonly_path.parent / "cache", tmp_path):
        with pytest.raises(StoreError):
            PDFParser().parse(replace(request, cache_dir=cache))
    link = tmp_path / "链接"
    link.symlink_to(request.readonly_path.parent, target_is_directory=True)
    with pytest.raises(StoreError):
        PDFParser().parse(replace(request, cache_dir=link / "cache"))
    with pytest.raises(StoreError) as exc:
        PDFParser().parse(replace(request, sha256="0" * 64))
    assert exc.value.code == "ORIGINAL_CHANGED"
    assert not mock_poppler


@pytest.mark.skipif(not HAS_POPPLER, reason="本机缺少 pdfinfo/pdftoppm，真实 Poppler 渲染留待具备工具的环境")
def test_real_poppler_mixed_pages_and_pixel_cap(request_factory):
    request = request_factory(sizes=((200, 100), (100, 300), (20000, 1000)))
    result = PDFParser(max_dimension=512).parse(request)
    assert not result.incomplete
    assert len(result.units) == 3
    dimensions = []
    for unit in result.units:
        page_dimensions = []
        for field in (unit.preview_relative_path, unit.review_relative_path):
            data = (request.cache_dir / field).read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", data[16:24])
            assert 0 < max(width, height) <= 512
            page_dimensions.append((width, height))
        dimensions.append(page_dimensions)
    assert dimensions[0] == [(200, 100), (417, 209)]
    assert dimensions[1][0] == (100, 300)


def test_rotation_preserved_in_split_with_mock_poppler(request_factory, mock_poppler):
    from pypdf import PdfWriter
    request = request_factory(sizes=((100, 300),))
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=300).rotate(90)
    request.readonly_path.chmod(0o644)
    with request.readonly_path.open("wb") as output:
        writer.write(output)
    request.readonly_path.chmod(0o444)
    request = replace(request, sha256=sha256_file(request.readonly_path))
    unit = PDFParser().parse(request).units[0]
    assert (unit.width_points, unit.height_points) == (300, 100)
    assert PdfReader(request.cache_dir / unit.page_relative_path).pages[0].rotation == 90
