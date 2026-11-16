"""本机数据根目录和受限相对路径；不使用项目名称拼接目录。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path, PurePosixPath

from .errors import ErrorCode, StoreError, translate_error


def data_root() -> Path:
    override = os.environ.get("DRAWCHECK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "DrawCheck"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/DrawCheck"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "DrawCheck"


def safe_path(root: Path, relative: str) -> Path:
    """数据库使用 POSIX 相对路径；拒绝跨平台分隔符和符号链接逃逸。"""
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or ".." in rel.parts or "\\" in relative or ":" in relative:
        raise StoreError(ErrorCode.INVALID_PATH, "数据路径必须为目录内相对路径")
    resolved_root = root.resolve()
    candidate = resolved_root
    for part in rel.parts:
        candidate = candidate / part
        # 内部符号链接也拒绝，避免 cache/exports 被重定向到 originals。
        if candidate.is_symlink():
            raise StoreError(ErrorCode.INVALID_PATH, "数据子路径不能经过符号链接")
    result = candidate.resolve()
    if not result.is_relative_to(resolved_root):
        raise StoreError(ErrorCode.INVALID_PATH, "数据路径越出根目录")
    return result


def ensure_writable(directory: Path) -> None:
    """实际写入探针；POSIX 权限位检查同时覆盖以特权账号运行测试的情形。"""
    try:
        ancestor = directory
        while not ancestor.exists():
            ancestor = ancestor.parent
        if os.name != "nt" and not ancestor.stat().st_mode & 0o222:
            raise PermissionError("目录没有写权限")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"drawcheck")
            probe.flush()
    except OSError as exc:
        # 人工权限检查没有 errno，统一给出明确不可写错误。
        if isinstance(exc, PermissionError):
            raise StoreError(ErrorCode.DATA_DIR_NOT_WRITABLE, "数据目录没有写权限") from exc
        raise translate_error(exc) from exc


def project_dirs(root: Path, project_id: str) -> dict[str, Path]:
    result = {name: safe_path(root, f"projects/{project_id}/{name}")
              for name in ("originals", "cache", "exports")}
    for path in result.values():
        ensure_writable(path)
    return result
