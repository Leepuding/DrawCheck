"""压缩包单层展开、路径边界、密码、CLI 超时及 RAR/7z 适配。"""
import io
from pathlib import Path
import stat
import struct
import subprocess
import sys
import zipfile

import pytest

from backend.importer import ImportService
from backend.importer.archives import ImportProblem, ImportLimits, _cli, _seven_members, collect, expand, sevenzip_binary


def import_job(tmp_path, path):
    with ImportService(tmp_path / "数据") as service:
        return service.wait(service.submit([path]), 15)


def test_zip_chinese_nested_duplicates_and_unsafe_paths(tmp_path, make_pdf, mock_poppler):
    source = make_pdf(pages=2)
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as z:
        z.writestr("二层.dwg", b"AC1018")
    archive = tmp_path / "中文（单层）.bin"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("目录/图纸(二）.pdf", source.read_bytes())
        z.writestr("重复.pdf", source.read_bytes())
        z.writestr("电气.dwg", ("AC1027\x00最小识别夹具").encode("utf-8"))
        z.writestr("里层.data", nested.getvalue())
        for name in ("../逃逸.dwg", "/绝对逃逸.dwg", "C:\\逃逸.dwg", "目录\\..\\逃逸.dwg"):
            z.writestr(name, b"AC1018")
        link = zipfile.ZipInfo("链接.dwg")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "../外部.dwg")
    job = import_job(tmp_path, archive)
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["file_count"] == 2 and result["unit_count"] == 2
    assert result["skipped_count"] == 1
    assert sum(u["code"] == "UNSAFE_ARCHIVE_PATH" for u in result["unreadable"]) == 5
    assert any(u["reason"] == "压缩包内还有压缩包，请解压后再导入" for u in result["unreadable"])
    assert not (tmp_path / "逃逸.dwg").exists()
    assert not (tmp_path.parent / "逃逸.dwg").exists()
    assert not Path("/绝对逃逸.dwg").exists()
    assert not list(tmp_path.rglob("逃逸.dwg"))


def test_zip_encrypted_flag_minimal_fixture(tmp_path):
    path = tmp_path / "密码.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("图.dwg", b"AC1018")
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = data.index(signature)
        flags = struct.unpack_from("<H", data, position + offset)[0]
        struct.pack_into("<H", data, position + offset, flags | 1)
    path.write_bytes(data)
    result = import_job(tmp_path, path)["result"]
    assert result["file_count"] == 0
    assert result["unreadable"][0]["reason"] == "压缩包有密码"


def test_corrupt_archive_and_missing_7z(tmp_path, monkeypatch):
    zip_path = tmp_path / "坏.zip"
    zip_path.write_bytes(("PK\x03\x04损坏").encode("utf-8"))
    assert import_job(tmp_path, zip_path)["result"]["unreadable"][0]["code"] == "ARCHIVE_CORRUPT"
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: None)
    for suffix, magic in (("rar", b"Rar!\x1a\x07\x01\x00"), ("7z", b"7z\xbc\xaf\x27\x1c")):
        path = tmp_path / f"包.{suffix}"
        path.write_bytes(magic)
        job = import_job(tmp_path, path)
        assert job["status"] == "completed"
        assert job["result"]["unreadable"][0]["code"] == "SEVENZIP_UNAVAILABLE"
        assert job["result"]["file_count"] == 0


def test_cli_rar_adapter_mock_listing_and_bounded_extraction(tmp_path, monkeypatch):
    path = tmp_path / "中文.rar"
    path.write_bytes(b"Rar!\x1a\x07\x01\x00")
    calls = []
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: "模拟7z")
    def cli(command, output, limit, timeout):
        calls.append(command)
        if command[1] == "l":
            output.write(("Path = 目录/图纸.dwg\r\nSize = 6\r\nEncrypted = -\r\nAttributes = A\r\n\r\n"
                          "Path = ../逃逸.dwg\r\nSize = 6\r\nEncrypted = -\r\nAttributes = A\r\n").encode())
        else:
            assert limit == 6 and timeout == 60
            output.write(b"AC1018")
    monkeypatch.setattr("backend.importer.archives._cli", cli)
    result = import_job(tmp_path, path)["result"]
    assert result["file_count"] == 1
    assert result["files"][0]["status"] == "unreadable"
    assert any(item["code"] == "UNSAFE_ARCHIVE_PATH" for item in result["unreadable"])
    assert len([c for c in calls if c[1] == "x"]) == 1
    assert "-so" in calls[1] and "-spd" in calls[1] and "--" in calls[1]


def test_cli_encrypted_manifest_mock(tmp_path, monkeypatch):
    path = tmp_path / "有密码.7z"
    path.write_bytes(b"7z\xbc\xaf\x27\x1c")
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: "模拟7z")
    def cli(_command, output, _limit, _timeout):
        output.write(b"Path = test.dwg\nSize = 6\nEncrypted = +\n")
    monkeypatch.setattr("backend.importer.archives._cli", cli)
    result = import_job(tmp_path, path)["result"]
    assert result["unreadable"][0]["reason"] == "压缩包有密码"
    assert result["file_count"] == 0


def test_cli_timeout_limit_and_password_are_real_subprocesses(tmp_path):
    with pytest.raises(ImportProblem) as exc:
        _cli([sys.executable, "-c", "import time; time.sleep(30)"], io.BytesIO(), 100, .1)
    assert exc.value.code == "ARCHIVE_TIMEOUT"
    with pytest.raises(ImportProblem) as exc:
        _cli([sys.executable, "-c", "import sys; sys.stdout.write('X' * 100000)"], io.BytesIO(), 10, 3)
    assert exc.value.code == "IMPORT_LIMIT"
    with pytest.raises(ImportProblem) as exc:
        _cli([sys.executable, "-c", "import sys; sys.stderr.write('Wrong password'); sys.exit(2)"], io.BytesIO(), 100, 3)
    assert exc.value.code == "ARCHIVE_ENCRYPTED"


def test_file_growth_during_scan_aborts_before_import(tmp_path):
    source = tmp_path / "变动.dwg"
    source.write_bytes(b"AC1018")
    limits = ImportLimits()
    items = collect([source], limits)
    source.write_bytes(("AC1018变大").encode("utf-8"))
    stage = tmp_path / "临时展开"
    stage.mkdir()
    with pytest.raises(ImportProblem) as exc:
        expand(items, stage, limits)
    assert exc.value.code == "INPUT_CHANGED"


@pytest.mark.skipif(not sevenzip_binary(), reason="本机无 7z/7za/7zz（或 DRAWCHECK_7Z），真实 7z/RAR 解包待补跑")
@pytest.mark.parametrize("password", [False, True, "header"])
def test_real_7z_archive(tmp_path, password):
    source = tmp_path / "图纸.dwg"
    source.write_bytes(("AC1027\x00自制识别夹具").encode("utf-8"))
    archive = tmp_path / "图纸.7z"
    command = [sevenzip_binary(), "a", "-t7z", str(archive), str(source)]
    if password:
        command += ["-p夹具密码"]
    if password == "header":
        command += ["-mhe=on"]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    result = import_job(tmp_path, archive)["result"]
    if password:
        assert result["file_count"] == 0
        assert result["unreadable"][0]["reason"] == "压缩包有密码"
    else:
        assert result["file_count"] == 1
        assert result["files"][0]["status"] == "unreadable"


def test_nested_office_document_is_unsupported(tmp_path):
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    outer = tmp_path / "外层.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("文档.docx", docx.getvalue())
    result = import_job(tmp_path, outer)["result"]
    assert result["file_count"] == 0
    assert result["unreadable"][0]["reason"] == "不支持的格式"



# --- 真实 7-Zip -slt 列表片段（RAR4/RAR5 末行空值字段 + 空链接字段）---

RAR5_SLT_FRAGMENT = """Path = 图纸.dwg
Folder = -
Size = 6
Packed Size = 6
Modified = 2026-01-01 00:00:00
Created = 
Accessed = 
Attributes = A
Encrypted = -
Solid = -
Split Before = -
Split After = -
CRC = 00000000
Host OS = Windows
Method = Store
Characteristics = 
Symbolic Link = 
Hard Link = 
Copy Link = 
Volume Index = 
Checksum = 
NT Security = 
"""

RAR4_SLT_FRAGMENT = """Path = 目录/图.dwg
Folder = -
Size = 6
Packed Size = 6
Modified = 2026-01-01 00:00:00
Attributes = A
Encrypted = -
Solid = -
CRC = 00000000
Host OS = Win32
Method = Store
Volume Index = 
"""

ENCRYPTED_RAR_SLT_FRAGMENT = """Path = 密文.dwg
Folder = -
Size = 6
Packed Size = 6
Attributes = A
Encrypted = +
Solid = -
Symbolic Link = 
Hard Link = 
Copy Link = 
Volume Index = 
NT Security = 
"""


def test_sevenzip_binary_recognizes_7zz_and_env(tmp_path, monkeypatch):
    fake = tmp_path / "7zz"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.delenv("DRAWCHECK_7Z", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert sevenzip_binary() == str(fake)
    missing = tmp_path / "missing-7z"
    monkeypatch.setenv("DRAWCHECK_7Z", str(missing))
    assert sevenzip_binary() is None
    monkeypatch.setenv("DRAWCHECK_7Z", str(fake))
    assert sevenzip_binary() == str(fake)


def test_parse_real_rar5_slt_trailing_empty_fields(tmp_path, monkeypatch):
    archive = tmp_path / "样本.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00")
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: "模拟7z")

    def cli(command, output, limit, timeout):
        assert command[1] == "l"
        # 末行保留「NT Security = 」尾随空格，模拟真实 7z 输出；禁止调用方整段 strip。
        output.write(RAR5_SLT_FRAGMENT.encode("utf-8"))

    monkeypatch.setattr("backend.importer.archives._cli", cli)
    members = _seven_members(archive, ImportLimits())
    assert len(members) == 1
    assert members[0].code is None
    assert members[0].member == "图纸.dwg"
    assert members[0].size == 6


def test_parse_real_rar4_slt_trailing_volume_index(tmp_path, monkeypatch):
    archive = tmp_path / "样本4.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x00")
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: "模拟7z")

    def cli(command, output, limit, timeout):
        output.write(RAR4_SLT_FRAGMENT.encode("utf-8"))

    monkeypatch.setattr("backend.importer.archives._cli", cli)
    members = _seven_members(archive, ImportLimits())
    assert len(members) == 1 and members[0].code is None
    assert members[0].member == "目录/图.dwg"


def test_parse_encrypted_rar_slt_fragment(tmp_path, monkeypatch):
    """无 rar 工具做不出加密夹具；用真实 -slt 的 Encrypted = + 片段单测。"""
    archive = tmp_path / "密码.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00")
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: "模拟7z")

    def cli(command, output, limit, timeout):
        output.write(ENCRYPTED_RAR_SLT_FRAGMENT.encode("utf-8"))

    monkeypatch.setattr("backend.importer.archives._cli", cli)
    with pytest.raises(ImportProblem) as exc:
        _seven_members(archive, ImportLimits())
    assert exc.value.code == "ARCHIVE_ENCRYPTED"


def test_parse_nonempty_symlink_still_unsafe(tmp_path, monkeypatch):
    archive = tmp_path / "链接.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00")
    monkeypatch.setattr("backend.importer.archives.sevenzip_binary", lambda: "模拟7z")
    listing = RAR5_SLT_FRAGMENT.replace("Symbolic Link = ", "Symbolic Link = ../外部.dwg")

    def cli(command, output, limit, timeout):
        output.write(listing.encode("utf-8"))

    monkeypatch.setattr("backend.importer.archives._cli", cli)
    members = _seven_members(archive, ImportLimits())
    assert len(members) == 1
    assert members[0].code == "UNSAFE_ARCHIVE_PATH"


def _store_rar5(members):
    """最小 RAR5 store 夹具；算法参考仓外 review-scripts/rar5.py，不引入外部文件。"""
    import zlib
    import struct

    def vint(n):
        out = b""
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out += bytes([b | 0x80])
            else:
                return out + bytes([b])

    def hdr(body):
        size = vint(len(body))
        data = size + body
        return struct.pack("<I", zlib.crc32(data)) + data

    out = b"Rar!\x1a\x07\x01\x00" + hdr(vint(1) + vint(0) + vint(0))
    for name, data in members:
        nb = name.encode()
        body = (vint(2) + vint(0x0002) + vint(len(data)) + vint(0x0004) + vint(len(data))
                + vint(0x20) + struct.pack("<I", zlib.crc32(data)) + vint(0) + vint(0)
                + vint(len(nb)) + nb)
        out += hdr(body) + data
    return out + hdr(vint(5) + vint(0) + vint(0))


@pytest.mark.skipif(not sevenzip_binary(), reason="本机无 7z/7za/7zz（或 DRAWCHECK_7Z），真实 RAR5 解包待补跑")
def test_real_rar5_ok_traversal_and_duplicate(tmp_path, make_pdf, mock_poppler):
    dwg = ("AC1027\x00自制识别夹具").encode("utf-8")
    pdf = make_pdf(name="页.pdf", pages=1).read_bytes()
    ordinary = tmp_path / "普通.rar"
    ordinary.write_bytes(_store_rar5([("a.dwg", dwg), ("dir/b.pdf", pdf), ("dup.dwg", dwg)]))
    result = import_job(tmp_path / "r1", ordinary)["result"]
    assert result["file_count"] == 2 and result["unit_count"] == 1
    assert result["skipped_count"] == 1
    assert len(result["unreadable"]) == 1
    assert result["unreadable"][0]["code"] == "DWG_INCOMPLETE"

    traversal = tmp_path / "穿越.rar"
    traversal.write_bytes(_store_rar5([("../evil.dwg", dwg), ("ok.dwg", dwg)]))
    job = import_job(tmp_path / "r2", traversal)
    result = job["result"]
    assert job["status"] == "completed"
    assert result["file_count"] == 1
    assert result["files"][0]["status"] == "unreadable"
    assert sum(u["code"] == "UNSAFE_ARCHIVE_PATH" for u in result["unreadable"]) == 1
    assert not (tmp_path / "evil.dwg").exists()
    assert not list(tmp_path.rglob("evil.dwg"))
