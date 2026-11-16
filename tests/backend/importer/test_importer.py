"""导入结果、异步进度、只读哈希、重复、异常与整批限额。"""
import hashlib
import json
from pathlib import Path
import threading
import zipfile

import pytest

from backend.importer import ImportLimits, ImportService, detect_format
from backend.parsers.pdf import PDFParser
from backend.store import ErrorCode, Repository, StoreError


def run_import(tmp_path, paths, **kwargs):
    with ImportService(tmp_path / "数据", **kwargs) as service:
        return service.wait(service.submit(paths), timeout=15)


def test_a4_fields_sha_and_unreadable_dwg(tmp_path, make_pdf, mock_poppler, monkeypatch):
    pdf = make_pdf(name="文件头识别.bin")
    dwg = tmp_path / "AC1018（中文）.data"
    dwg.write_bytes(("AC1018\x00自制 DWG 头，仅用于识别").encode("utf-8"))
    root = tmp_path / "数据"
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(root))
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (pdf, dwg)}
    with ImportService() as service:
        identifier = service.submit([pdf, dwg], project_name="自制项目")
        job = service.wait(identifier, 15)
        assert service.get_job(identifier) == job
    assert job["status"] == "completed", job
    assert job["processed_count"] == job["total_count"] == 2
    result = job["result"]
    assert result["summary"] == "已导入 2 个文件 · 拆出 3 张图"
    assert result["file_count"] == 2 and result["unit_count"] == 3
    assert len(result["unreadable"]) == 1
    assert result["unreadable"][0]["code"] == "DWG_INCOMPLETE"
    assert result["files"][1]["format"] == "dwg"
    assert result["files"][1]["status"] == "unreadable"
    assert result["files"][1]["status_label"] == "读不了"
    for index, drawing in enumerate(result["drawings"], 1):
        assert drawing["unit_id"] and drawing["original_id"]
        assert drawing["ordinal"] == index
        assert drawing["drawing_number"] == str(index)
        assert drawing["name"] == f"文件头识别.bin · 第 {index} 页"
    with Repository(root) as repo:
        originals = repo.list_records("original_files", version_id=result["version_id"])
        assert len(originals) == 2
        assert len(repo.list_records("drawing_units", version_id=result["version_id"])) == 3
        for record in originals:
            copy = repo.original_path(record["id"])
            assert hashlib.sha256(copy.read_bytes()).hexdigest() == digests[record["name"]]
            assert not copy.stat().st_mode & 0o222
        assert all((root / d["review_relative_path"]).is_file() for d in result["drawings"])
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (pdf, dwg)} == digests
    json.dumps(job, ensure_ascii=False)


def test_same_version_duplicates_skipped_without_reparse(tmp_path, make_pdf, mock_poppler):
    source = make_pdf()
    duplicate = tmp_path / "相同内容.data"
    duplicate.write_bytes(source.read_bytes())
    with ImportService(tmp_path / "数据") as service:
        first = service.wait(service.submit([source, duplicate]), 15)
        call_count = len(mock_poppler)
        version = first["result"]["version_id"]
        second = service.wait(service.submit([duplicate], version_id=version), 15)
    assert first["result"]["file_count"] == 1
    assert first["result"]["unit_count"] == 3
    assert first["result"]["skipped_count"] == 1
    assert second["result"]["file_count"] == second["result"]["unit_count"] == 0
    assert second["result"]["duplicate_message"] == "已跳过 1 个重复文件"
    assert len(mock_poppler) == call_count
    with Repository(tmp_path / "数据") as repo:
        assert len(repo.list_records("original_files", version_id=version)) == 1
        assert len(repo.list_records("drawing_units", version_id=version)) == 3


def test_invalid_and_unsupported_not_counted(tmp_path, make_pdf, mock_poppler):
    corrupt = tmp_path / "损坏.pdf"
    corrupt.write_bytes(("%PDF-1.7\n坏文件").encode("utf-8"))
    empty = tmp_path / "空.pdf"
    empty.touch()
    encrypted = make_pdf("加密.pdf", encrypted=True)
    zero_pages = make_pdf("无页.pdf", pages=0)
    dxf = tmp_path / "伪装.pdf"
    dxf.write_bytes(b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF")
    real_dxf = tmp_path / "图纸.dxf"
    real_dxf.write_bytes(dxf.read_bytes())
    docx = tmp_path / "文档.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    job = run_import(tmp_path, [corrupt, empty, encrypted, zero_pages, dxf, real_dxf, docx])
    assert job["status"] == "completed"
    result = job["result"]
    assert result["file_count"] == result["unit_count"] == 0
    assert len(result["unreadable"]) == 7
    reasons = {row["name"]: row["reason"] for row in result["unreadable"]}
    assert reasons["文档.docx"] == reasons["图纸.dxf"] == "不支持的格式"
    assert reasons["伪装.pdf"] == "不支持的格式"
    assert "读不了" in reasons["损坏.pdf"]
    assert "密码" in reasons["加密.pdf"]
    assert "为空" in reasons["空.pdf"]
    assert not mock_poppler


def test_folder_recursive_hidden_policy_and_symlink(tmp_path, make_pdf, mock_poppler):
    folder = tmp_path / "输入"
    make_pdf("输入/二层/图纸.pdf", pages=1)
    make_pdf("输入/.忽略.pdf", pages=2)
    make_pdf("输入/.隐藏目录/图.pdf", pages=2)
    outside = make_pdf("外部.pdf", pages=4)
    (folder / "外链.pdf").symlink_to(outside)
    job = run_import(tmp_path, [folder])
    assert job["result"]["unit_count"] == 1
    assert len(job["result"]["unreadable"]) == 1
    assert job["result"]["unreadable"][0]["code"] == "INPUT_UNREADABLE"


@pytest.mark.parametrize("kind", ["file_count", "file_bytes", "archive_count", "archive_bytes"])
def test_over_limit_rejects_whole_batch_before_database(tmp_path, kind):
    source = tmp_path / "有效.dwg"
    source.write_bytes(("AC1027\x00最小识别夹具").encode("utf-8"))
    limits = ImportLimits(max_files=1, max_bytes=100)
    paths = [source]
    if kind == "file_count":
        paths += [source]
    elif kind == "file_bytes":
        limits = ImportLimits(max_bytes=2)
    else:
        archive = tmp_path / "超限.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            if kind == "archive_count":
                z.writestr("一.dwg", b"AC1018")
                z.writestr("二.dwg", b"AC1027")
                limits = ImportLimits(max_files=1, max_bytes=10000)
            else:
                z.writestr("膨胀.dwg", b"AC1027" + b"0" * 10000)
                limits = ImportLimits(max_bytes=1000)
        paths = [source, archive]
    job = run_import(tmp_path, paths, limits=limits)
    assert job["status"] == "rejected", job
    assert job["error"]["code"] == "IMPORT_LIMIT"
    assert job["error"]["message"] == "一次最多导入 300 个文件、2 GB，请分批导入"
    assert job["result"]["project_id"] is None
    assert not (tmp_path / "数据").exists()


def test_page_timeout_a4_result_continues(tmp_path, make_pdf, monkeypatch):
    import subprocess
    pdf = make_pdf()
    def run(command, **options):
        if command[0] == "pdftoppm":
            prefix = Path(command[-1])
            if prefix.parent.name == "page-2":
                raise subprocess.TimeoutExpired(command, options["timeout"])
            prefix.with_suffix(".png").write_bytes(("模拟图").encode("utf-8"))
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", run)
    job = run_import(tmp_path, [pdf])
    assert job["status"] == "completed"
    result = job["result"]
    assert result["file_count"] == 1 and result["unit_count"] == 2
    assert [d["ordinal"] for d in result["drawings"]] == [1, 3]
    assert result["files"][0]["status"] == "partial"
    assert result["unreadable"][0]["page"] == 2
    assert result["unreadable"][0]["reason"] == "渲染超时"
    assert result["unreadable"][0]["original_id"]


def test_async_progress_snapshots_are_isolated(tmp_path, make_pdf, mock_poppler):
    entered, release = threading.Event(), threading.Event()
    class BlockingParser(PDFParser):
        def parse(self, request):
            entered.set()
            assert release.wait(5)
            return super().parse(request)
    with ImportService(tmp_path / "数据", pdf_parser=BlockingParser()) as service:
        identifier = service.submit([make_pdf(pages=1)])
        try:
            assert entered.wait(5)
            snapshot = service.get_job(identifier)
            assert snapshot["status"] == "running"
            assert snapshot["total_count"] == 1 and snapshot["processed_count"] == 0
            snapshot["status"] = "伪造"
            assert service.get_job(identifier)["status"] == "running"
        finally:
            release.set()
        job = service.wait(identifier, 15)
        assert job["status"] == "completed"
        job["result"]["drawings"].clear()
        assert service.get_job(identifier)["result"]["unit_count"] == 1
        assert len(service.get_job(identifier)["result"]["drawings"]) == 1


@pytest.mark.parametrize("code", [ErrorCode.DISK_FULL, ErrorCode.DATA_DIR_NOT_WRITABLE])
def test_storage_failure_preserves_store_code(tmp_path, monkeypatch, code):
    source = tmp_path / "图.dwg"
    source.write_bytes(b"AC1027")
    def fail(*_args, **_kwargs):
        raise StoreError(code, "数据磁盘空间不足" if code == ErrorCode.DISK_FULL else "数据目录不可写")
    monkeypatch.setattr(Repository, "import_original", fail)
    job = run_import(tmp_path, [source])
    assert job["status"] == "failed"
    assert job["error"]["code"] == code.value
    assert job["result"]["file_count"] == 0


def test_old_version_refused(tmp_path):
    root = tmp_path / "数据"
    with Repository(root) as repo:
        project = repo.create_project("测试")
        old = repo.create_version(project["id"], "旧版")
        repo.create_version(project["id"], "新版")
    with ImportService(root) as service:
        job = service.wait(service.submit([], version_id=old["id"]), 15)
    assert job["status"] == "failed" and job["error"]["code"] == "VERSION_READONLY"


@pytest.mark.parametrize("header,format", [(b"AC1018", "dwg"), (b"AC1027", "dwg"), (b"AC1024", "dwg"),
                                           (b"%PDF-1.7", "pdf"), (b"Rar!\x1a\x07\x01\x00", "rar"),
                                           (b"7z\xbc\xaf\x27\x1c", "7z"), (b"", "empty")])
def test_magic_identification_ignores_extension(tmp_path, header, format):
    source = tmp_path / "任意.xyz"
    source.write_bytes(header)
    assert detect_format(source) == format


def test_default_300_file_boundary(tmp_path):
    source = tmp_path / "边界.dwg"
    source.write_bytes(b"AC1018")
    assert ImportLimits().max_files == 300
    assert ImportLimits().max_bytes == 2_000_000_000
    accepted_root = tmp_path / "300个数据"
    with ImportService(accepted_root) as service:
        accepted = service.wait(service.submit([source] * 300), 15)
    assert accepted["status"] == "completed"
    assert accepted["total_count"] == 300
    assert accepted["result"]["file_count"] == 1
    assert accepted["result"]["skipped_count"] == 299
    rejected_root = tmp_path / "301个数据"
    with ImportService(rejected_root) as service:
        rejected = service.wait(service.submit([source] * 301), 15)
    assert rejected["status"] == "rejected"
    assert not rejected_root.exists()


def test_exact_byte_boundary_includes_duplicates(tmp_path):
    source = tmp_path / "边界.dwg"
    source.write_bytes(b"AC1027")
    limits = ImportLimits(max_bytes=12)
    job = run_import(tmp_path, [source, source], limits=limits)
    assert job["status"] == "completed"
    assert job["result"]["file_count"] == 1
    assert job["result"]["skipped_count"] == 1


def test_all_pages_failed_is_not_an_imported_file(tmp_path, make_pdf, monkeypatch):
    import subprocess
    def run(command, **_kwargs):
        if command[0] == "pdftoppm":
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr("backend.parsers.pdf.parser.subprocess.run", run)
    job = run_import(tmp_path, [make_pdf(pages=2)])
    assert job["status"] == "completed"
    assert job["result"]["file_count"] == job["result"]["unit_count"] == 0
    assert len(job["result"]["unreadable"]) == 2
    assert job["result"]["files"][0]["status"] == "unreadable"
