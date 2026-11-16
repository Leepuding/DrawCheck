"""存储层稳定错误码；上层负责按既有产品文案映射。"""
from __future__ import annotations

import errno
import sqlite3
from enum import Enum


class ErrorCode(str, Enum):
    DATA_DIR_NOT_WRITABLE = "DATA_DIR_NOT_WRITABLE"
    DISK_FULL = "DISK_FULL"
    DATABASE_BUSY = "DATABASE_BUSY"
    DATABASE_ERROR = "DATABASE_ERROR"
    SCHEMA_TOO_NEW = "SCHEMA_TOO_NEW"
    VERSION_READONLY = "VERSION_READONLY"
    ORIGINAL_IMMUTABLE = "ORIGINAL_IMMUTABLE"
    ORIGINAL_CHANGED = "ORIGINAL_CHANGED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_DATA = "INVALID_DATA"
    INVALID_PATH = "INVALID_PATH"
    STORAGE_IO = "STORAGE_IO"


class StoreError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        super().__init__(message)


def translate_error(exc: OSError | sqlite3.Error) -> StoreError:
    """保留异常链，不把底层路径或数据库内容作为接口错误正文。"""
    message = str(exc)
    if isinstance(exc, sqlite3.Error):
        number = getattr(exc, "sqlite_errorcode", 0) & 255
        if "VERSION_READONLY" in message:
            return StoreError(ErrorCode.VERSION_READONLY, "旧版记录只读")
        if "ORIGINAL_IMMUTABLE" in message:
            return StoreError(ErrorCode.ORIGINAL_IMMUTABLE, "原图记录不可改写")
        if number in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return StoreError(ErrorCode.DATABASE_BUSY, "数据库正被占用，请稍后重试")
        if number == sqlite3.SQLITE_FULL:
            return StoreError(ErrorCode.DISK_FULL, "数据磁盘空间不足")
        if number in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PERM):
            return StoreError(ErrorCode.DATA_DIR_NOT_WRITABLE, "数据目录或数据库不可写")
        if isinstance(exc, sqlite3.IntegrityError):
            return StoreError(ErrorCode.INVALID_DATA, "数据不符合约束或引用关系")
        return StoreError(ErrorCode.DATABASE_ERROR, "数据库操作失败")
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return StoreError(ErrorCode.DISK_FULL, "数据磁盘空间不足")
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return StoreError(ErrorCode.DATA_DIR_NOT_WRITABLE, "数据目录不可写")
    return StoreError(ErrorCode.STORAGE_IO, "本地文件操作失败")
