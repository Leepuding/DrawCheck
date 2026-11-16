"""迁移幂等、原子性、数据库锁和磁盘满边界。"""
import errno
import importlib
import sqlite3
from pathlib import Path

import pytest

from backend.store import ErrorCode, Repository, StoreError
from backend.store.db import connect
from backend.store.errors import translate_error
from backend.store.migrate import VERSION_TABLES, SCHEMA_VERSION, migrate


def test_empty_migration_twice(tmp_path):
    conn = connect(tmp_path)
    try:
        assert migrate(conn) == SCHEMA_VERSION
        first = list(conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name"))
        assert migrate(conn) == SCHEMA_VERSION
        assert first == list(conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name"))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not list(conn.execute("PRAGMA foreign_key_check"))
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {*VERSION_TABLES, "projects", "drawing_versions", "settings"} <= names
    finally:
        conn.close()


def test_migration_rolls_back_all_changes(tmp_path, monkeypatch):
    module = importlib.import_module("backend.store.migrate")
    conn = connect(tmp_path)
    def fail(_connection):
        raise sqlite3.OperationalError("模拟迁移中断")
    monkeypatch.setattr(module, "_version_guards", fail)
    try:
        with pytest.raises(StoreError):
            migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not list(conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


def test_schema_one_upgrade_preserves_projects_and_supports_recoverable_drafts(tmp_path):
    from backend.store.migrate import _version_guards
    from backend.revisions import RevisionService

    conn = connect(tmp_path)
    initial = Path(__file__).resolve().parents[3] / "backend/store/migrations/001_initial.sql"
    conn.executescript(initial.read_text(encoding="utf-8"))
    _version_guards(conn)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO projects(id,name) VALUES('p','升级前工程')")
    conn.execute("INSERT INTO drawing_versions(id,project_id,sequence,name) VALUES('v','p',1,'A')")
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()
    with Repository(tmp_path) as repo:
        assert repo.get("projects", "p")["name"] == "升级前工程"
        draft = RevisionService(repo).create_revision("v")
        assert draft["status"] == "draft"
        assert repo.create_task("v")["status"] == "待开始"


def test_future_schema_is_not_downgraded(tmp_path):
    conn = connect(tmp_path)
    conn.execute("PRAGMA user_version=999")
    try:
        with pytest.raises(StoreError) as caught:
            migrate(conn)
        assert caught.value.code == ErrorCode.SCHEMA_TOO_NEW
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
    finally:
        conn.close()


def test_real_database_lock_has_explicit_code(repo):
    with Repository(repo.root, timeout=0.01) as second:
        repo._connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StoreError) as caught:
                second.create_project("被占用")
            assert caught.value.code == ErrorCode.DATABASE_BUSY
        finally:
            repo._connection.execute("ROLLBACK")


def test_sqlite_page_limit_simulates_full_disk(repo):
    pages = repo._connection.execute("PRAGMA page_count").fetchone()[0]
    repo._connection.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(StoreError) as caught:
        repo.rename_project(repo.create_project("小项目")["id"], "大" * 200000)
    assert caught.value.code == ErrorCode.DISK_FULL
    assert len(repo.list_records("projects")) == 1
    assert repo.list_records("projects")[0]["name"] == "小项目"


def test_full_disk_auto_rollback_keeps_original_error_code(repo):
    pages = repo._connection.execute("PRAGMA page_count").fetchone()[0]
    repo._connection.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(StoreError) as caught:
        repo.create_project("大" * 200000)
    assert caught.value.code == ErrorCode.DISK_FULL
    assert repo.list_records("projects") == []
    assert repo.create_project("小项目")["name"] == "小项目"


@pytest.mark.parametrize("number,expected", [(errno.ENOSPC, ErrorCode.DISK_FULL),
                         (errno.EACCES, ErrorCode.DATA_DIR_NOT_WRITABLE),
                         (errno.EROFS, ErrorCode.DATA_DIR_NOT_WRITABLE),
                         (errno.EIO, ErrorCode.STORAGE_IO)])
def test_os_error_mapping(number, expected):
    assert translate_error(OSError(number, "模拟文件系统错误")).code == expected
