"""原图只读：唯一创建入口只用 xb；已存在的副本从不覆盖或修复。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .db import storage_errors
from .errors import ErrorCode, StoreError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with storage_errors():
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, expected: str) -> None:
    with storage_errors():
        if not path.is_file() or sha256_file(path) != expected:
            raise StoreError(ErrorCode.ORIGINAL_CHANGED, "原图副本缺失或哈希已改变")
        # Windows 的 chmod 只读位和 POSIX 模式均能通过写位检查。
        if path.stat().st_mode & 0o222:
            raise StoreError(ErrorCode.ORIGINAL_CHANGED, "原图副本只读属性已改变")


def _import_copy(source: Path, destination: Path, expected: str) -> int:
    """私有的首次导入操作；中途失败保留现场，禁止覆写已有文件。"""
    if destination.exists():
        verify_file(destination, expected)
        return destination.stat().st_size
    with storage_errors():
        digest = hashlib.sha256()
        size = 0
        # 独占创建避免竞态覆盖；源文件始终只读打开，未调用源文件 chmod。
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(block)
                digest.update(block)
                size += len(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        destination.chmod(0o444)
        if digest.hexdigest() != expected:
            raise StoreError(ErrorCode.ORIGINAL_CHANGED, "导入期间源文件内容改变，副本未入库")
        verify_file(destination, expected)
        return size
