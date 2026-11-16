"""SQLite 连接边界；单进程本机使用，写事务短持有。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import translate_error
from .paths import ensure_writable, safe_path


@contextmanager
def storage_errors():
    try:
        yield
    except (OSError, sqlite3.Error) as exc:
        raise translate_error(exc) from exc


def connect(root: Path, timeout: float = 1.0) -> sqlite3.Connection:
    ensure_writable(root)
    path = safe_path(root, "drawcheck.sqlite3")
    with storage_errors():
        connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            # REPLACE 隐含删除也必须触发不可变/旧版保护。
            connection.execute("PRAGMA recursive_triggers=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
        return connection
