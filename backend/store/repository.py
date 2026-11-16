"""本机业务存取接口；不实现解析、审查算法、联网或导出文件生成。"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .db import connect, storage_errors
from .errors import ErrorCode, StoreError
from .models import Evidence, default_settings
from .originals import _import_copy, sha256_file, verify_file
from .paths import data_root, project_dirs, safe_path

JSON_FIELDS = {"metadata_json", "details_json", "rule_result_json", "primary_opinion_json",
               "review_opinion_json", "checkpoints_json", "incomplete_json", "tolerance_snapshot_json", "diagnostics_json"}
# 表名只来自代码常量，不接收客户端传入的表名或 SQL。
READ_TABLES = {"projects", "drawing_versions", "original_files", "drawing_units", "facts",
               "associations", "issues", "pending_confirmations", "review_tasks", "model_calls", "exports"}


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise StoreError(ErrorCode.INVALID_DATA, "数据必须可编码为有限数值的 JSON") from exc


def _decode(row) -> dict:
    result = dict(row)
    for key in JSON_FIELDS & result.keys():
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def _finite_nonnegative(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise StoreError(ErrorCode.INVALID_DATA, f"{name}必须为非负有限数值")


class Repository:
    def __init__(self, root: Path | str | None = None, *, timeout: float = 1.0, read_only: bool = False):
        # 延迟导入，避免以 -m 运行迁移模块时出现重复加载警告。
        from .migrate import SCHEMA_VERSION, migrate

        self.root = Path(root).expanduser().resolve() if root is not None else data_root().resolve()
        if read_only:
            path = safe_path(self.root, "drawcheck.sqlite3")
            with storage_errors():
                if not path.is_file():
                    raise StoreError(ErrorCode.NOT_FOUND, "本地数据库不存在")
                self._connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=timeout)
                try:
                    self._connection.row_factory = sqlite3.Row
                    self._connection.execute("PRAGMA query_only=ON")
                    # 一次请求使用同一个读取快照，避免在桌面写入时混用不同状态的数据。
                    self._connection.execute("BEGIN")
                    schema = self._connection.execute("PRAGMA user_version").fetchone()[0]
                    if schema != SCHEMA_VERSION:
                        raise StoreError(ErrorCode.SCHEMA_TOO_NEW if schema > SCHEMA_VERSION else ErrorCode.DATABASE_ERROR,
                                         "请用相应版本的图纸审查打开本地数据库")
                except BaseException:
                    self._connection.close()
                    raise
            return
        self._connection = connect(self.root, timeout)
        try:
            migrate(self._connection)
        except BaseException:
            self._connection.close()
            raise

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @contextmanager
    def _transaction(self):
        with storage_errors():
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                # SQLITE_FULL 等错误可能已经由 SQLite 自动回滚，不能覆盖原始错误码。
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _one(self, sql: str, parameters=()) -> dict:
        with storage_errors():
            row = self._connection.execute(sql, parameters).fetchone()
        if row is None:
            raise StoreError(ErrorCode.NOT_FOUND, "记录不存在")
        return _decode(row)

    def _insert(self, table: str, values: dict) -> dict:
        identifier = uuid4().hex
        values = {"id": identifier, **values}
        columns = ",".join(values)
        with storage_errors():
            self._connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})",
                                     tuple(values.values()))
        return self.get(table, identifier)

    def _update(self, table: str, identifier: str, values: dict) -> dict:
        with storage_errors():
            cursor = self._connection.execute(
                f"UPDATE {table} SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), identifier))
        if not cursor.rowcount:
            raise StoreError(ErrorCode.NOT_FOUND, "记录不存在")
        return self.get(table, identifier)

    def get(self, table: str, identifier: str) -> dict:
        """只读通用查询；业务写入只能走各显式方法。"""
        if table not in READ_TABLES:
            raise StoreError(ErrorCode.INVALID_DATA, "未知记录类型")
        return self._one(f"SELECT * FROM {table} WHERE id=?", (identifier,))

    def list_records(self, table: str, *, version_id: str | None = None, project_id: str | None = None) -> list[dict]:
        if table not in READ_TABLES or (version_id and table in {"projects", "drawing_versions"}):
            raise StoreError(ErrorCode.INVALID_DATA, "不支持此查询")
        if project_id and table not in {"drawing_versions", "original_files"}:
            raise StoreError(ErrorCode.INVALID_DATA, "此表不支持项目筛选")
        filters, values = [], []
        for key, value in (("version_id", version_id), ("project_id", project_id)):
            if value is not None:
                filters.append(f"{key}=?")
                values.append(value)
        sql = f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(filters) if filters else "")
        sql += " ORDER BY sequence" if table == "drawing_versions" else " ORDER BY rowid"
        with storage_errors():
            return [_decode(row) for row in self._connection.execute(sql, values)]

    def _writable_version(self, version_id: str) -> dict:
        version = self.get("drawing_versions", version_id)
        draft = self._connection.execute("SELECT 1 FROM revision_runs WHERE version_id=? AND status='draft'", (version_id,)).fetchone()
        if draft:
            return version
        latest = self._one("SELECT MAX(sequence) AS sequence FROM drawing_versions v WHERE project_id=? "
                           "AND NOT EXISTS (SELECT 1 FROM revision_runs r WHERE r.version_id=v.id AND r.status='draft')",
                           (version["project_id"],))
        if version["sequence"] != latest["sequence"]:
            raise StoreError(ErrorCode.VERSION_READONLY, "旧版记录只读")
        return version

    def create_project(self, name: str) -> dict:
        with self._transaction():
            project = self._insert("projects", {"name": name})
            project_dirs(self.root, project["id"])
        return project

    def rename_project(self, project_id: str, name: str) -> dict:
        return self._update("projects", project_id, {"name": name})

    def create_version(self, project_id: str, name: str) -> dict:
        with self._transaction():
            self.get("projects", project_id)
            current = self._one("SELECT COALESCE(MAX(sequence),0)+1 AS next FROM drawing_versions WHERE project_id=?",
                                (project_id,))
            return self._insert("drawing_versions", {"project_id": project_id, "sequence": current["next"], "name": name})

    def import_original(self, version_id: str, source: Path | str) -> dict:
        """返回 record 与 reused；同版同内容跳过，跨版同项目复用只读副本。"""
        source = Path(source).resolve()
        digest = sha256_file(source)
        with self._transaction():
            version = self._writable_version(version_id)
            existing = self.list_records("original_files", version_id=version_id)
            for record in existing:
                if record["sha256"] == digest:
                    verify_file(safe_path(self.root, record["relative_path"]), digest)
                    return {"record": record, "reused": True}
            project_id = version["project_id"]
            project_dirs(self.root, project_id)
            relative = f"projects/{project_id}/originals/{digest}"
            destination = safe_path(self.root, relative)
            size = _import_copy(source, destination, digest)
            record = self._insert("original_files", {"version_id": version_id, "project_id": project_id,
                                  "name": source.name, "relative_path": relative, "sha256": digest, "size_bytes": size})
            return {"record": record, "reused": False}

    def original_path(self, original_id: str) -> Path:
        """仅返回经核验的只读输入路径，供解析器后续读取。"""
        record = self.get("original_files", original_id)
        path = safe_path(self.root, record["relative_path"])
        verify_file(path, record["sha256"])
        return path

    def verify_originals(self, version_id: str) -> dict[str, str]:
        self.get("drawing_versions", version_id)
        return {record["id"]: sha256_file(self.original_path(record["id"]))
                for record in self.list_records("original_files", version_id=version_id)}

    def create_unit(self, version_id: str, original_id: str, *, kind: str, ordinal: int,
                    name: str, drawing_number: str = "", metadata: dict | None = None) -> dict:
        return self._insert("drawing_units", {"version_id": version_id, "original_id": original_id,
                            "kind": kind, "ordinal": ordinal, "name": name, "drawing_number": drawing_number,
                            "metadata_json": _json(metadata or {})})

    def update_unit_directory(self, unit_id: str, *, name: str, drawing_number: str) -> dict:
        return self._update("drawing_units", unit_id, {"name": name, "drawing_number": drawing_number})

    def add_fact(self, version_id: str, evidence: Evidence, *, numeric_value: str | None = None,
                 unit: str | None = None) -> dict:
        if numeric_value is not None:
            try:
                if not isinstance(numeric_value, str) or not Decimal(numeric_value).is_finite():
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise StoreError(ErrorCode.INVALID_DATA, "数值须为有限十进制字符串") from exc
        return self._insert("facts", {"version_id": version_id, **evidence.columns(),
                            "numeric_value": numeric_value, "unit": unit})

    def add_association(self, version_id: str, left_unit_id: str, right_unit_id: str,
                        kind: str, details: dict | None = None) -> dict:
        return self._insert("associations", {"version_id": version_id, "left_unit_id": left_unit_id,
                            "right_unit_id": right_unit_id, "kind": kind, "details_json": _json(details or {})})

    def create_issue(self, version_id: str, title: str, left: Evidence, right: Evidence, *,
                     rule_result: dict, primary_opinion: dict | None = None, review_opinion: dict | None = None,
                     comparison_tag: str | None = None, previous_issue_id: str | None = None) -> dict:
        with self._transaction():
            version = self._writable_version(version_id)
            if previous_issue_id:
                previous = self.get("issues", previous_issue_id)
                previous_version = self.get("drawing_versions", previous["version_id"])
                if (previous_version["project_id"] != version["project_id"]
                        or previous_version["sequence"] >= version["sequence"]):
                    raise StoreError(ErrorCode.INVALID_DATA, "上一版问题须属于同项目更早版本")
            return self._insert("issues", {"version_id": version_id, "title": title,
                                **left.columns("left_"), **right.columns("right_"),
                                "rule_result_json": _json(rule_result), "primary_opinion_json": _json(primary_opinion),
                                "review_opinion_json": _json(review_opinion), "comparison_tag": comparison_tag,
                                "previous_issue_id": previous_issue_id})

    def update_issue(self, issue_id: str, *, review_status: str, review_note: str = "") -> dict:
        return self._update("issues", issue_id, {"review_status": review_status, "review_note": review_note})

    def create_pending(self, version_id: str, evidence: Evidence, reason: str) -> dict:
        return self._insert("pending_confirmations", {"version_id": version_id, **evidence.columns(), "reason": reason})

    def confirm_pending(self, pending_id: str, *, status: str, confirmed_text: str | None = None) -> dict:
        if status == "已确认" and not confirmed_text:
            raise StoreError(ErrorCode.INVALID_DATA, "确认时须填写正确内容")
        return self._update("pending_confirmations", pending_id, {"status": status, "confirmed_text": confirmed_text})

    def get_settings(self) -> dict:
        with storage_errors():
            row = self._connection.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
        return json.loads(row[0]) if row else default_settings()

    def save_settings(self, changes: dict) -> dict:
        with self._transaction():
            settings = self.get_settings()
            if not isinstance(changes, dict) or set(changes) - settings.keys():
                raise StoreError(ErrorCode.INVALID_DATA, "设置含未知字段，密钥禁止存入本库")
            settings.update(changes)
            for key in ("dimension_tolerance_mm", "elevation_tolerance_m"):
                _finite_nonnegative(settings[key], key)
            limit = settings["monthly_limit_yuan"]
            if limit is not None:
                _finite_nonnegative(limit, "费用上限")
                if limit == 0:
                    raise StoreError(ErrorCode.INVALID_DATA, "费用上限须大于零")
            _finite_nonnegative(settings["cost_warning_ratio"], "提醒比例")
            if settings["cost_warning_ratio"] > 1:
                raise StoreError(ErrorCode.INVALID_DATA, "提醒比例不能超过 1")
            if settings["currency"] != "CNY" or settings["billing_period"] != "calendar_month":
                raise StoreError(ErrorCode.INVALID_DATA, "费用口径为自然月、人民币元")
            for key in ("primary_model", "review_model"):
                model = settings[key]
                if model is not None:
                    allowed = {"provider", "model", "base_url", "credential_ref"}
                    if not isinstance(model, dict) or set(model) - allowed:
                        raise StoreError(ErrorCode.INVALID_DATA, "模型仅保存服务商、模型、地址、凭据引用")
                    if any(not isinstance(value, str) for value in model.values()):
                        raise StoreError(ErrorCode.INVALID_DATA, "模型配置字段须为字符串")
            self._connection.execute("INSERT INTO settings(id,value_json) VALUES(1,?) "
                                     "ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json", (_json(settings),))
            return settings

    def create_task(self, version_id: str, *, checkpoints: dict | None = None) -> dict:
        with self._transaction():
            self._writable_version(version_id)
            settings = self.get_settings()
            snapshot = {key: settings[key] for key in ("dimension_tolerance_mm", "elevation_tolerance_m")}
            return self._insert("review_tasks", {"version_id": version_id, "checkpoints_json": _json(checkpoints or {}),
                                "incomplete_json": "[]", "tolerance_snapshot_json": _json(snapshot)})

    def update_task(self, task_id: str, *, status: str, checkpoints: dict, incomplete: list,
                    estimated_cost_micros: int) -> dict:
        self._check_cost(estimated_cost_micros)
        return self._update("review_tasks", task_id, {"status": status, "checkpoints_json": _json(checkpoints),
                            "incomplete_json": _json(incomplete), "estimated_cost_micros": estimated_cost_micros})

    @contextmanager
    def review_session(self, task_id: str):
        """供未来审查流程包围只读处理；不是审查实现，也不自动判为完成。"""
        task = self.get("review_tasks", task_id)
        self._writable_version(task["version_id"])
        before = self.verify_originals(task["version_id"])
        try:
            yield task
        finally:
            after = self.verify_originals(task["version_id"])
            if before != after:
                raise StoreError(ErrorCode.ORIGINAL_CHANGED, "审查前后原图集合或哈希不一致")

    @staticmethod
    def _check_cost(cost: int):
        if type(cost) is not int or cost < 0:
            raise StoreError(ErrorCode.INVALID_DATA, "费用须为非负整数微元")

    def record_model_call(self, task_id: str, unit_id: str, *, role: str, provider: str, model: str,
                          status: str, input_tokens: int, output_tokens: int, estimated_cost_micros: int,
                          image_sha256: str, prompt_version: str, diagnostics: dict | None = None) -> dict:
        self._check_cost(estimated_cost_micros)
        for count in (input_tokens, output_tokens):
            if type(count) is not int or count < 0:
                raise StoreError(ErrorCode.INVALID_DATA, "调用量须为非负整数")
        task = self.get("review_tasks", task_id)
        return self._insert("model_calls", {"version_id": task["version_id"], "task_id": task_id, "unit_id": unit_id,
                            "role": role, "provider": provider, "model": model, "status": status,
                            "input_tokens": input_tokens, "output_tokens": output_tokens,
                            "estimated_cost_micros": estimated_cost_micros, "image_sha256": image_sha256,
                            "prompt_version": prompt_version, "diagnostics_json": _json(diagnostics or {})})

    def record_export(self, task_id: str, *, format: str, relative_path: str) -> dict:
        """只登记已生成的缓存导出文件；不写 PDF/Excel，更不写原图。"""
        task = self.get("review_tasks", task_id)
        version = self.get("drawing_versions", task["version_id"])
        prefix = f"projects/{version['project_id']}/exports/"
        path = safe_path(self.root, relative_path)
        export_dir = safe_path(self.root, prefix)
        if not relative_path.startswith(prefix) or not path.is_relative_to(export_dir):
            raise StoreError(ErrorCode.INVALID_PATH, "导出记录必须位于项目 exports 目录")
        if format not in {"pdf", "xlsx"} or path.suffix.lower() != f".{format}":
            raise StoreError(ErrorCode.INVALID_DATA, "导出格式只支持 PDF 或 Excel")
        return self._insert("exports", {"version_id": task["version_id"], "task_id": task_id, "format": format,
                            "relative_path": relative_path, "sha256": sha256_file(path)})
