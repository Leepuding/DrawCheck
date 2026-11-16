"""事务内版本化迁移，可重复执行；失败时表与版本号一起回滚。"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .db import connect, storage_errors
from .errors import ErrorCode, StoreError
from .paths import data_root

SCHEMA_VERSION = 3
VERSION_TABLES = ("original_files", "drawing_units", "facts", "associations", "issues",
                  "pending_confirmations", "review_tasks", "model_calls", "exports")


def _version_guards(connection: sqlite3.Connection, *, revisions: bool = False):
    for table in VERSION_TABLES:
        for action, refs in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
            active = (" AND NOT EXISTS (SELECT 1 FROM revision_runs r WHERE r.version_id=n.id AND r.status='draft')"
                      if revisions else "")
            writable_draft = (f" AND NOT EXISTS (SELECT 1 FROM revision_runs r WHERE r.version_id={{ref}}.version_id AND r.status='draft')"
                              if revisions else "")
            condition = " OR ".join(
                f"EXISTS (SELECT 1 FROM drawing_versions v WHERE v.id={ref}.version_id "
                f"AND v.sequence < (SELECT MAX(n.sequence) FROM drawing_versions n WHERE n.project_id=v.project_id{active}))"
                + writable_draft.format(ref=ref)
                for ref in refs)
            connection.execute(f"DROP TRIGGER IF EXISTS {table}_{action.lower()}_readonly")
            connection.execute(f"CREATE TRIGGER {table}_{action.lower()}_readonly BEFORE {action} ON {table} "
                               f"WHEN {condition} BEGIN SELECT RAISE(ABORT,'VERSION_READONLY'); END")
    for table in ("original_files", "drawing_versions"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT,'ORIGINAL_IMMUTABLE'); END")


def migrate(connection: sqlite3.Connection) -> int:
    with storage_errors():
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise StoreError(ErrorCode.SCHEMA_TOO_NEW, "数据版本高于当前程序，请使用更新版本")
            if current < 1:
                script = (Path(__file__).parent / "migrations/001_initial.sql").read_text(encoding="utf-8")
                statement = ""
                for line in script.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise StoreError(ErrorCode.DATABASE_ERROR, "迁移脚本语句不完整")
                _version_guards(connection)
                connection.execute("PRAGMA user_version=1")
            if current < 2:
                script = (Path(__file__).parent / "migrations/002_revisions.sql").read_text(encoding="utf-8")
                statement = ""
                for line in script.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise StoreError(ErrorCode.DATABASE_ERROR, "迁移脚本语句不完整")
                _version_guards(connection, revisions=True)
                connection.execute("PRAGMA user_version=2")
            if current < 3:
                connection.execute("ALTER TABLE model_calls ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}' "
                                   "CHECK(json_valid(diagnostics_json))")
                connection.execute("PRAGMA user_version=3")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    return SCHEMA_VERSION


def main():
    parser = argparse.ArgumentParser(description="初始化或迁移 DrawCheck 本地数据库")
    parser.add_argument("--data-dir", type=Path, default=None, help="开发数据目录")
    args = parser.parse_args()
    connection = connect((args.data_dir or data_root()).resolve())
    try:
        print(f"迁移完成，数据库版本：{migrate(connection)}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
