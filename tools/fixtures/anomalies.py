"""异常文件生成：固定字节写入 anomalies/，供 DC-06/07 自检。"""
from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

from pypdf import PdfWriter

_FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buf.getvalue()


def _encrypt_flag_zip(path: Path, member: str, payload: bytes):
    """写入带加密标志位的 ZIP（与 DC-06 测试同手法），不设真实密码流。"""
    path.write_bytes(_zip_bytes([(member, payload)]))
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = data.index(signature)
        flags = struct.unpack_from("<H", data, position + offset)[0]
        struct.pack_into("<H", data, position + offset, flags | 1)
    path.write_bytes(bytes(data))


def write_anomalies(out_dir: Path) -> list[dict]:
    """生成异常文件并返回清单条目（含期望原因码）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    def add(name: str, data: bytes, *, expect_code: str, expect_status: str, purpose: str):
        path = out_dir / name
        path.write_bytes(data)
        entries.append({
            "relative_path": f"anomalies/{name}",
            "purpose": purpose,
            "expect_code": expect_code,
            "expect_status": expect_status,
            "size": len(data),
        })

    add("corrupt.pdf", "%PDF-1.7\n坏文件".encode("utf-8"),
        expect_code="PDF_CORRUPT", expect_status="unreadable",
        purpose="损坏 PDF（头合法但体损坏）")

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("fixture-pass")
    writer.write(buf)
    add("encrypted.pdf", buf.getvalue(),
        expect_code="PDF_ENCRYPTED", expect_status="unreadable",
        purpose="加密 PDF")

    add("empty.pdf", b"",
        expect_code="EMPTY_FILE", expect_status="unreadable",
        purpose="空文件")

    dxf_body = b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n"
    add("disguised_dxf.pdf", dxf_body,
        expect_code="UNSUPPORTED_FORMAT", expect_status="unreadable",
        purpose="扩展名伪装为 PDF 的 DXF")

    add("sample.dxf", dxf_body,
        expect_code="UNSUPPORTED_FORMAT", expect_status="unreadable",
        purpose="DXF（不支持）")

    add("sample.docx", _zip_bytes([
        ("[Content_Types].xml", b"<Types/>"),
        ("word/document.xml", b"<document/>"),
    ]),
        expect_code="UNSUPPORTED_FORMAT", expect_status="unreadable",
        purpose="docx（不支持）")

    add("path_traversal.zip", _zip_bytes([
        ("../escape.dwg", b"AC1018junk"),
        ("ok.txt", b"safe"),
    ]),
        expect_code="UNSAFE_ARCHIVE_PATH", expect_status="unreadable",
        purpose="路径穿越压缩包")

    inner = _zip_bytes([("inner.dwg", b"AC1018inner")])
    add("nested.zip", _zip_bytes([("layer2.zip", inner)]),
        expect_code="NESTED_ARCHIVE", expect_status="unreadable",
        purpose="嵌套压缩包")

    pwd_path = out_dir / "password.zip"
    _encrypt_flag_zip(pwd_path, "secret.dwg", b"AC1018x")
    entries.append({
        "relative_path": "anomalies/password.zip",
        "purpose": "带密码压缩包",
        "expect_code": "ARCHIVE_ENCRYPTED",
        "expect_status": "unreadable",
        "size": pwd_path.stat().st_size,
    })

    # N4：不用自造 DC-06 枚举码；拆成 import 状态 + DC-07 解析期望
    fake_bytes = b"AC1018" + b"\x00garbage-not-a-real-dwg" * 4
    fake_path = out_dir / "fake.dwg"
    fake_path.write_bytes(fake_bytes)
    entries.append({
        "relative_path": "anomalies/fake.dwg",
        "purpose": "伪 DWG 头（AC1018+垃圾）：导入实际解析后明确读不了",
        "expect_import_status": "unreadable",
        "expect_code": "DWG_INCOMPLETE",
        "expect_dc07": "UNREADABLE",
        "expect_status": "unreadable",
        "size": len(fake_bytes),
    })

    return entries
