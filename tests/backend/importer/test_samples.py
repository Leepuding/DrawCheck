"""仅通过环境变量读取客户样本；不拷贝到仓库，不向外部服务发送内容。"""
import os
from pathlib import Path
import shutil

import pytest

from backend.importer import ImportService, detect_format
from backend.parsers.dwg import discover_tools
from backend.store import Repository
from backend.store.originals import sha256_file


def sample_paths(format):
    location = os.environ.get("DRAWCHECK_SAMPLES")
    if not location:
        pytest.skip("未设置 DRAWCHECK_SAMPLES，客户样本由 box 侧补跑")
    root = Path(location)
    assert root.is_dir(), "DRAWCHECK_SAMPLES 目录不存在"
    return sorted(path for path in root.rglob("*") if path.is_file() and detect_format(path) == format)


def test_customer_five_pdf_to_34_units(tmp_path):
    import re
    import struct
    import subprocess

    paths = sample_paths("pdf")
    assert len(paths) == 5
    if not shutil.which("pdfinfo") or not shutil.which("pdftoppm"):
        pytest.skip("缺少 Poppler，不能以模拟渲染替代客户 PDF 实测")
    before = {path: sha256_file(path) for path in paths}
    root = tmp_path / "样本临时数据"
    with ImportService(root) as service:
        job = service.wait(service.submit(paths), timeout=3600)
    assert job["status"] == "completed", job
    result = job["result"]
    assert not result["unreadable"]
    assert result["file_count"] == 5 and result["unit_count"] == 34
    assert {path: sha256_file(path) for path in paths} == before
    by_name = {path.name: path for path in paths}
    for drawing in result["drawings"]:
        source = by_name[drawing["source_original"]]
        info = subprocess.run(
            ["pdfinfo", "-f", str(drawing["ordinal"]), "-l", str(drawing["ordinal"]), str(source)],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout
        match = re.search(r"Page\s+%d size:\s+([\d.]+) x ([\d.]+)" % drawing["ordinal"], info)
        rot = int(re.search(r"Page\s+%d rot:\s+(\d+)" % drawing["ordinal"], info).group(1))
        width, height = float(match.group(1)), float(match.group(2))
        if rot % 180:
            width, height = height, width
        assert abs(drawing["width_points"] - width) < 1
        assert abs(drawing["height_points"] - height) < 1
        assert (drawing["width_points"] > drawing["height_points"]) == (width > height)
        preview = root / drawing["preview_relative_path"]
        review = root / drawing["review_relative_path"]
        pw, ph = struct.unpack(">II", preview.read_bytes()[16:24])
        rw, rh = struct.unpack(">II", review.read_bytes()[16:24])
        assert abs(pw - round(width)) <= 2 and abs(ph - round(height)) <= 2
        assert max(rw, rh) <= 8192
        assert (rw > rh) == (width > height)
    with Repository(root) as repo:
        originals = repo.list_records("original_files", version_id=result["version_id"])
        assert sorted(repo.verify_originals(result["version_id"]).values()) == sorted(before.values())
        assert len(originals) == 5
        assert len(repo.list_records("drawing_units", version_id=result["version_id"])) == 34


def assert_dwg_import(result, root):
    files = [row for row in result["files"] if row["format"] == "dwg"]
    assert len(files) == 6
    notices = {row["original_id"] for row in result["unreadable"] if row["code"] == "DWG_INCOMPLETE"}
    previews = 0
    for file in files:
        drawings = [row for row in result["drawings"] if row["original_id"] == file["original_id"]]
        assert len(drawings) == file["unit_count"]
        assert file["status"] in {"imported", "partial", "unreadable"}
        if file["status"] != "imported":
            assert file["original_id"] in notices
        for unit in drawings:
            if relative := unit.get("preview_relative_path"):
                assert (root / relative).is_file()
                previews += 1
    assert previews > 0


def test_customer_six_dwg_import(tmp_path):
    paths = sample_paths("dwg")
    assert len(paths) == 6
    if not discover_tools():
        pytest.skip("缺少 LibreDWG，客户 DWG 解析待补跑")
    root = tmp_path / "样本临时数据"
    before = {path: sha256_file(path) for path in paths}
    with ImportService(root) as service:
        job = service.wait(service.submit(paths), timeout=3600)
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["file_count"] == 6
    assert_dwg_import(result, root)
    assert {path: sha256_file(path) for path in paths} == before


def customer_rar_path():
    explicit = os.environ.get("DRAWCHECK_SAMPLE_RAR")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            pytest.skip("DRAWCHECK_SAMPLE_RAR 指向的文件不存在")
        return path
    location = os.environ.get("DRAWCHECK_SAMPLES")
    if not location:
        pytest.skip("未设置 DRAWCHECK_SAMPLES / DRAWCHECK_SAMPLE_RAR，客户 RAR 由 box 侧补跑")
    path = Path(location).resolve().parent / "图纸测试包.rar"
    if not path.is_file():
        pytest.skip("DRAWCHECK_SAMPLES 上级未找到 图纸测试包.rar")
    return path


def test_customer_rar_import_11_files(tmp_path):
    from backend.importer.archives import sevenzip_binary

    if not sevenzip_binary():
        pytest.skip("缺少 7z/7za/7zz（或 DRAWCHECK_7Z），客户 RAR 实测待补跑")
    if not shutil.which("pdfinfo") or not shutil.which("pdftoppm"):
        pytest.skip("缺少 Poppler，客户 RAR 内 PDF 不能实测渲染")
    if not discover_tools():
        pytest.skip("缺少 LibreDWG，客户 RAR 内 DWG 不能实测解析")
    archive = customer_rar_path()
    before = sha256_file(archive)
    root = tmp_path / "客户RAR临时数据"
    with ImportService(root) as service:
        job = service.wait(service.submit([archive]), timeout=3600)
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["file_count"] == 11
    assert sum(row["unit_count"] for row in result["files"] if row["format"] == "pdf") == 34
    assert all(row["code"] == "DWG_INCOMPLETE" for row in result["unreadable"])
    assert_dwg_import(result, root)
    assert sha256_file(archive) == before

