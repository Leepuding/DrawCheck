"""按文件头识别格式；扩展名仅用于错误分类，不用来认定 PDF/DWG。"""
from pathlib import Path
import re

ARCHIVES = {"zip", "rar", "7z"}


def detect_format(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(1024)
    if not head:
        return "empty"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if re.match(rb"AC10[0-9]{2}", head):
        return "dwg"
    if head.startswith(b"AutoCAD Binary DXF") or re.match(rb"\s*0\s+SECTION\b", head):
        return "dxf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    return "unsupported"
