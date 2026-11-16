"""PDF 夹具均在临时目录自制；模拟 Poppler 仅用于协议与故障隔离测试。"""
from pathlib import Path
import subprocess

from pypdf import PdfWriter
import pytest

from backend.parsers.base import ParseInput
from backend.store.originals import sha256_file


@pytest.fixture
def request_factory(tmp_path):
    def make(*, sizes=((200, 100), (100, 300), (400, 200)), encrypted=False):
        source = tmp_path / "originals" / "原图（混合）.pdf"
        source.parent.mkdir(exist_ok=True)
        writer = PdfWriter()
        for width, height in sizes:
            writer.add_blank_page(width=width, height=height)
        if encrypted:
            writer.encrypt("测试密码")
        with source.open("wb") as output:
            writer.write(output)
        source.chmod(0o444)
        return ParseInput("original-1", source, sha256_file(source), tmp_path / "cache", "pdf")
    return make


@pytest.fixture
def mock_poppler(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[0] == "pdftoppm":
            Path(command[-1]).with_suffix(".png").write_bytes(("模拟 PNG，仅用于调度测试").encode("utf-8"))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", run)
    return commands
