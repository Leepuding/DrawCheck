"""自制最小文件；不依赖 DC-05，不复制客户样本。"""
from pathlib import Path
import subprocess

from pypdf import PdfWriter
import pytest


@pytest.fixture
def make_pdf(tmp_path):
    def make(name="图纸（全半角).pdf", *, pages=3, encrypted=False):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        for index in range(pages):
            writer.add_blank_page(width=100 + index * 100, height=200)
        if encrypted:
            writer.encrypt("夹具密码")
        with path.open("wb") as output:
            writer.write(output)
        return path
    return make


@pytest.fixture
def mock_poppler(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "pdftoppm":
            Path(command[-1]).with_suffix(".png").write_bytes(("模拟图，仅验证导入调度").encode("utf-8"))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", run)
    return calls
