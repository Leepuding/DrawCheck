"""先检查整批清单与限额，再单层展开；从不按成员路径直接解压。"""
from __future__ import annotations

import os
import math
import shutil
import stat
import subprocess
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from .formats import ARCHIVES, detect_format

LIMIT_MESSAGE = "一次最多导入 300 个文件、2 GB，请分批导入"


class ImportProblem(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ImportLimits:
    max_files: int = 300
    max_bytes: int = 2_000_000_000
    archive_timeout: float = 60

    def __post_init__(self):
        if (not isinstance(self.max_files, int) or not isinstance(self.max_bytes, int)
                or self.max_files <= 0 or self.max_bytes <= 0
                or not math.isfinite(self.archive_timeout) or self.archive_timeout <= 0):
            raise ValueError("导入上限与压缩包超时必须为正有限值")


@dataclass
class Candidate:
    path: Path
    name: str
    size: int
    archive: Path | None = None
    member: str | None = None
    format: str | None = None
    code: str | None = None
    reason: str | None = None
    member_index: int | None = None


def problem(name: str, code: str, reason: str, size: int = 0) -> Candidate:
    return Candidate(Path(), name, size, code=code, reason=reason)


def safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    return bool(name and parts and not normalized.startswith("/")
                and not PureWindowsPath(name).drive and ".." not in parts
                and ":" not in normalized and not any(ord(c) < 32 for c in name))


def _cli(command: list[str], output: BinaryIO, limit: int, timeout: float):
    """有界流式读取 stdout；超时/超限均杀死并回收 7-Zip，禁止密码交互。"""
    errors = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=errors,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        errors.close()
        raise
    expired = threading.Event()

    def kill():
        expired.set()
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        size = 0
        assert proc.stdout is not None
        while block := proc.stdout.read(65536):
            size += len(block)
            if size > limit:
                raise ImportProblem("IMPORT_LIMIT", LIMIT_MESSAGE)
            output.write(block)
        code = proc.wait()
        if expired.is_set():
            raise ImportProblem("ARCHIVE_TIMEOUT", "压缩包读取超时，读不了")
        if code:
            errors.seek(0)
            diagnostic = errors.read(8192).lower()
            if b"password" in diagnostic or b"encrypted" in diagnostic:
                raise ImportProblem("ARCHIVE_ENCRYPTED", "压缩包有密码")
            raise ImportProblem("ARCHIVE_CORRUPT", "压缩包损坏，读不了")
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
        errors.close()


def sevenzip_binary() -> str | None:
    configured = os.environ.get("DRAWCHECK_7Z", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")


def _seven_members(path: Path, limits: ImportLimits) -> list[Candidate]:
    import io
    binary = sevenzip_binary()
    if binary is None:
        raise ImportProblem("SEVENZIP_UNAVAILABLE", "缺少 7-Zip 工具，压缩包读不了")
    output = io.BytesIO()
    _cli([binary, "l", "-slt", "-ba", "-sccUTF-8", "-pDRAWCHECK_NO_PASSWORD", "--", str(path)],
         output, 4 * 1024 * 1024, limits.archive_timeout)
    records = []
    listing = output.getvalue().decode("utf-8", errors="strict").replace("\r\n", "\n")
    # 勿对整段 listing.strip()：RAR 末行常为「Key = 」空值字段，剥掉尾随空格后会丢 " = "。
    for block in listing.strip("\n").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition("=")
            if not sep:
                raise ImportProblem("ARCHIVE_CORRUPT", "压缩包目录异常，读不了")
            key, value = key.rstrip(), value.lstrip()
            if key in fields:
                raise ImportProblem("ARCHIVE_CORRUPT", "压缩包目录异常，读不了")
            fields[key] = value
        if not fields:
            continue
        name = fields.get("Path", "")
        if fields.get("Encrypted") == "+":
            raise ImportProblem("ARCHIVE_ENCRYPTED", "压缩包有密码")
        if fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
            continue
        size = int(fields["Size"])
        if size < 0:
            raise ImportProblem("ARCHIVE_CORRUPT", "压缩包目录异常，读不了")
        # RAR5 常输出空的 Symbolic/Hard/Copy Link 字段；仅值非空才算链接。属性首字符 l 仍保留。
        linked = (bool(fields.get("Symbolic Link")) or bool(fields.get("Hard Link"))
                  or bool(fields.get("Copy Link"))
                  or fields.get("Attributes", "").split(" ")[-1][:1] == "l")
        if not safe_member(name) or linked:
            records.append(problem(f"{path.name}/{name}", "UNSAFE_ARCHIVE_PATH", "压缩包含不安全路径，读不了", size))
        else:
            records.append(Candidate(path, f"{path.name}/{name}", size, path, name, "7z"))
    return records


def _members(path: Path, format: str, limits: ImportLimits) -> list[Candidate]:
    if format != "zip":
        return _seven_members(path, limits)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if any(info.flag_bits & 1 for info in infos):
            raise ImportProblem("ARCHIVE_ENCRYPTED", "压缩包有密码")
        # Office Open XML 也是 ZIP 容器，应识别为不支持的文档，而非递归展开文档内部 XML。
        names = {info.filename for info in infos}
        if "[Content_Types].xml" in names and any(name.startswith(("word/", "xl/", "ppt/")) for name in names):
            raise ImportProblem("UNSUPPORTED_FORMAT", "不支持的格式")
        result = []
        for index, info in enumerate(infos):
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if not safe_member(info.filename) or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                result.append(problem(f"{path.name}/{info.filename}", "UNSAFE_ARCHIVE_PATH", "压缩包含不安全路径，读不了", info.file_size))
            else:
                result.append(Candidate(path, f"{path.name}/{info.filename}", info.file_size,
                                        path, info.filename, "zip", member_index=index))
        return result


def collect(paths: list[Path], limits: ImportLimits) -> list[Candidate]:
    result, total = [], 0

    def append(item: Candidate):
        nonlocal total
        result.append(item)
        total += item.size
        if len(result) > limits.max_files or total > limits.max_bytes:
            raise ImportProblem("IMPORT_LIMIT", LIMIT_MESSAGE)

    def add(path: Path):
        try:
            if path.is_symlink() or not path.is_file():
                append(problem(path.name, "INPUT_UNREADABLE", "文件不存在、不是常规文件或为符号链接，读不了"))
                return
            size = path.stat().st_size
            # 压缩包本身也不得超过整批容量；配额以展开后的成员字节数累计。
            if size > limits.max_bytes:
                raise ImportProblem("IMPORT_LIMIT", LIMIT_MESSAGE)
            format = detect_format(path)
            if format in ARCHIVES:
                try:
                    members = _members(path, format, limits)
                    for item in members:
                        append(item)
                except ImportProblem as exc:
                    if exc.code == "IMPORT_LIMIT":
                        raise
                    append(problem(path.name, exc.code, str(exc), size))
                except (zipfile.BadZipFile, UnicodeError, ValueError, KeyError, RuntimeError):
                    append(problem(path.name, "ARCHIVE_CORRUPT", "压缩包损坏，读不了", size))
            else:
                append(Candidate(path, path.name, size, format=format))
        except OSError:
            append(problem(path.name, "INPUT_UNREADABLE", "文件无法读取，读不了"))

    for source in paths:
        path = source.absolute()
        if path.is_dir() and not path.is_symlink():
            def walk_error(_error):
                append(problem(path.name, "INPUT_UNREADABLE", "文件夹无法读取，读不了"))
            for directory, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
                for name in sorted(files):
                    if not name.startswith("."):
                        add(Path(directory) / name)
        else:
            add(path)
    return result


def expand(items: list[Candidate], staging: Path, limits: ImportLimits) -> list[Candidate]:
    """先全部展开成功再入库；超限即抛错，由调用方丢弃整批临时文件。"""
    for index, item in enumerate(items):
        if item.code:
            continue
        destination = staging / str(index)
        destination.mkdir()
        # 目录使用程序生成序号，文件名只取 basename；拒绝的成员从未交给解压器写磁盘。
        target = destination / (PurePosixPath(item.member.replace("\\", "/")).name if item.member else item.path.name)
        try:
            with target.open("xb") as output:
                if item.archive is None:
                    # 普通文件同样快照化；扫描后增长不得绕过整批容量限制。
                    with item.path.open("rb") as incoming:
                        size = 0
                        while block := incoming.read(65536):
                            size += len(block)
                            if size > item.size:
                                raise ImportProblem("INPUT_CHANGED", "文件在导入期间发生变化，请重新选择后导入")
                            output.write(block)
                elif item.format == "zip":
                    with zipfile.ZipFile(item.archive) as archive:
                        info = archive.infolist()[item.member_index]
                        with archive.open(info) as incoming:
                            size = 0
                            while block := incoming.read(65536):
                                size += len(block)
                                if size > item.size:
                                    raise ImportProblem("IMPORT_LIMIT", LIMIT_MESSAGE)
                                output.write(block)
                else:
                    binary = sevenzip_binary()
                    _cli([binary, "x", "-so", "-spd", "-y", "-pDRAWCHECK_NO_PASSWORD", "--",
                          str(item.archive), item.member], output, item.size, limits.archive_timeout)
            if target.stat().st_size != item.size:
                if item.archive is None:
                    raise ImportProblem("INPUT_CHANGED", "文件在导入期间发生变化，请重新选择后导入")
                raise ImportProblem("ARCHIVE_CORRUPT", "压缩包损坏，读不了")
            item.path = target
            item.format = detect_format(target)
            if item.archive is not None and item.format in ARCHIVES:
                if item.format == "zip":
                    with zipfile.ZipFile(target) as archive:
                        names = set(archive.namelist())
                    if "[Content_Types].xml" in names and any(n.startswith(("word/", "xl/", "ppt/")) for n in names):
                        item.format = "unsupported"
                        continue
                item.code, item.reason = "NESTED_ARCHIVE", "压缩包内还有压缩包，请解压后再导入"
        except ImportProblem as exc:
            if exc.code in ("IMPORT_LIMIT", "INPUT_CHANGED"):
                raise
            item.code, item.reason = exc.code, str(exc)
        except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError):
            item.code, item.reason = "ARCHIVE_CORRUPT", "压缩包损坏，读不了"
    return items
