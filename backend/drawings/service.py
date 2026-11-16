"""Remove mistaken inputs by creating a fresh working version, preserving source evidence."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from backend.revisions.service import RevisionService, _remap
from backend.store import ErrorCode, Repository, StoreError
from backend.store.paths import safe_path


class RemovalError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _ids(value, name):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise StoreError(ErrorCode.INVALID_DATA, f"{name}须为有效记录编号列表")
    if len(value) != len(set(value)):
        raise StoreError(ErrorCode.INVALID_DATA, "选择中存在重复记录")
    return set(value)


class DrawingRemovalService:
    def __init__(self, store: Repository):
        self.store = store

    def _validate_version(self, version_id):
        version = self.store.get("drawing_versions", version_id)
        versions = self.store.list_records("drawing_versions", project_id=version["project_id"])
        run = RevisionService(self.store)._run(version_id)
        if version["id"] != versions[-1]["id"]:
            raise StoreError(ErrorCode.VERSION_READONLY, "请打开项目最新版本后移除图纸")
        if run and run["status"] == "draft":
            raise RemovalError("REMOVE_DRAFT_VERSION", "请先完成修订版导入后移除图纸")
        self.store._writable_version(version_id)
        active = self.store._connection.execute(
            "SELECT 1 FROM review_tasks t JOIN drawing_versions v ON t.version_id=v.id "
            "WHERE v.project_id=? AND t.status IN ('待开始','进行中') LIMIT 1", (version["project_id"],)).fetchone()
        if active:
            raise RemovalError("REMOVE_REVIEW_ACTIVE", "已有审查任务，请结束后再移除图纸")
        return version, versions

    def _write_manifest(self, destination, manifest):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise StoreError(ErrorCode.INVALID_DATA, "调整版本的导入记录已存在")
        temporary = destination.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(_json(manifest))
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, version_id, *, unit_ids=None, original_ids=None, unreadable_indices=None):
        selected_units = _ids([] if unit_ids is None else unit_ids, "图纸")
        selected_originals = _ids([] if original_ids is None else original_ids, "源文件")
        indices = [] if unreadable_indices is None else unreadable_indices
        if (not isinstance(indices, list) or any(type(index) is not int or index < 0 for index in indices)
                or len(indices) != len(set(indices))):
            raise StoreError(ErrorCode.INVALID_DATA, "读不了记录的选择无效，请刷新后重新选择")
        if not selected_units and not selected_originals and not indices:
            raise StoreError(ErrorCode.INVALID_DATA, "请先选择要移除的图纸或文件")
        destination = None
        created_manifest = False
        try:
            with self.store._transaction():
                source, versions = self._validate_version(version_id)
                units = self.store.list_records("drawing_units", version_id=version_id)
                originals = self.store.list_records("original_files", version_id=version_id)
                units_by_id = {unit["id"]: unit for unit in units}
                originals_by_id = {original["id"]: original for original in originals}
                if selected_units - units_by_id.keys() or selected_originals - originals_by_id.keys():
                    raise StoreError(ErrorCode.INVALID_DATA, "所选记录不属于当前图纸版本，请刷新后重新选择")
                before_hashes = self.store.verify_originals(version_id)
                manifest = RevisionService(self.store)._manifest(version_id)
                diagnostics = deepcopy(manifest.get("unreadable", []))
                if any(index >= len(diagnostics) for index in indices):
                    raise StoreError(ErrorCode.INVALID_DATA, "读不了记录已变化，请刷新后重新选择")
                sha_to_original = {original["sha256"]: original["id"] for original in originals}

                def normalize_original(identifier):
                    if identifier in originals_by_id:
                        return identifier
                    # Older revision manifests may still point to a prior same-project source ID.
                    if identifier:
                        try:
                            record = self.store.get("original_files", identifier)
                        except StoreError as exc:
                            if exc.code != ErrorCode.NOT_FOUND:
                                raise
                        else:
                            if record["project_id"] == source["project_id"]:
                                return sha_to_original.get(record["sha256"])
                    return None

                removed_units = selected_units | {unit["id"] for unit in units if unit["original_id"] in selected_originals}
                remaining_units = [unit for unit in units if unit["id"] not in removed_units]
                remaining_originals = {unit["original_id"] for unit in remaining_units}
                remaining_unit_ids = {unit["id"] for unit in remaining_units}
                removed_diagnostics = set(indices)
                for index, row in enumerate(diagnostics):
                    original_id = normalize_original(row.get("original_id"))
                    if not original_id and row.get("unit_id") in units_by_id:
                        original_id = units_by_id[row["unit_id"]]["original_id"]
                    if original_id:
                        row["original_id"] = original_id
                    if index in indices and (
                        original_id in remaining_originals or row.get("unit_id") in remaining_unit_ids
                    ):
                        raise RemovalError("REMOVE_DIAGNOSTIC_HAS_DRAWINGS",
                            "该记录关联的源文件仍有图纸，不能单独移除转换或读取风险。请移除整个源文件，或保留此记录继续审查。")
                    source_wide = row.get("code") in {"DWG_INCOMPLETE", "PDF_INCOMPLETE"}
                    if original_id in selected_originals or (not source_wide and row.get("unit_id") in removed_units):
                        removed_diagnostics.add(index)
                    if not source_wide and any(unit["original_id"] == original_id and row.get("page") == unit["ordinal"]
                           for uid, unit in units_by_id.items() if uid in removed_units):
                        removed_diagnostics.add(index)
                remaining_diagnostics = [row for index, row in enumerate(diagnostics) if index not in removed_diagnostics]
                kept_originals = {unit["original_id"] for unit in remaining_units}
                kept_originals.update(normalize_original(row.get("original_id")) for row in remaining_diagnostics)
                kept_originals.discard(None)
                visible_originals = {unit["original_id"] for unit in units}
                visible_originals.update(normalize_original(row.get("original_id")) for row in diagnostics)
                visible_originals.discard(None)
                new_version = self.store._insert("drawing_versions", {
                    "project_id": source["project_id"], "sequence": versions[-1]["sequence"] + 1, "name": "调整图纸"})
                new_id = new_version["id"]
                mapping = {version_id: new_id}
                for original in originals:
                    if original["id"] not in kept_originals:
                        continue
                    row = self.store._insert("original_files", {
                        key: value for key, value in original.items() if key not in {"id", "created_at"}} | {"version_id": new_id})
                    mapping[original["id"]] = row["id"]
                for unit in remaining_units:
                    row = self.store.create_unit(new_id, mapping[unit["original_id"]], kind=unit["kind"], ordinal=unit["ordinal"],
                        name=unit["name"], drawing_number=unit["drawing_number"], metadata={})
                    mapping[unit["id"]] = row["id"]
                for table in ("facts", "pending_confirmations"):
                    for record in self.store.list_records(table, version_id=version_id):
                        if record["unit_id"] not in mapping:
                            continue
                        row = self.store._insert(table, {
                            key: value for key, value in record.items() if key != "id"} | {
                            "version_id": new_id, "unit_id": mapping[record["unit_id"]]})
                        mapping[record["id"]] = row["id"]
                actual_fact_ids = {fact["id"] for fact in self.store.list_records("facts", version_id=new_id)}
                for unit in remaining_units:
                    metadata = _remap(deepcopy(unit.get("metadata") or {}), mapping)
                    if isinstance(metadata.get("fact_annotations"), dict):
                        metadata["fact_annotations"] = {key: value for key, value in metadata["fact_annotations"].items() if key in actual_fact_ids}
                    metadata.update(copied_from_unit_id=unit["id"], copied_from_version_id=version_id)
                    self.store._update("drawing_units", mapping[unit["id"]], {"metadata_json": _json(metadata)})
                new_units = self.store.list_records("drawing_units", version_id=new_id)
                new_diagnostics = _remap(remaining_diagnostics, mapping)
                files = []
                for original in originals:
                    if original["id"] not in kept_originals:
                        continue
                    original_id = mapping[original["id"]]
                    count = sum(unit["original_id"] == original_id for unit in new_units)
                    partial = any(row.get("original_id") == original_id for row in new_diagnostics)
                    files.append({"name": original["name"], "original_id": original_id, "sha256": original["sha256"],
                        "format": "pdf" if any(unit["original_id"] == original_id and unit["kind"] == "pdf_page" for unit in new_units)
                                  else "dwg" if any(unit["original_id"] == original_id and unit["kind"] == "dwg_frame" for unit in new_units)
                                  else Path(original["name"]).suffix.lower().lstrip("."),
                        "unit_count": count, "status": "partial" if partial and count else "imported" if count else "unreadable",
                        "status_label": "部分图纸未完成" if partial and count else "已导入" if count else "读不了"})
                new_manifest = {"project_id": source["project_id"], "version_id": new_id, "files": files,
                    "drawings": [{**unit, "unit_id": unit["id"], **unit["metadata"]} for unit in new_units],
                    "unreadable": new_diagnostics, "file_count": len(files), "unit_count": len(new_units),
                    "skipped_count": 0, "duplicate_message": "", "summary": f"已导入 {len(files)} 个文件 · 拆出 {len(new_units)} 张图",
                    "adjustment": {"kind": "remove_drawings", "previous_version_id": version_id,
                        "removed_unit_ids": sorted(removed_units), "removed_original_ids": sorted(selected_originals),
                        "removed_unreadable_indices": sorted(removed_diagnostics)}}
                destination = safe_path(self.store.root, f"projects/{source['project_id']}/cache/import-{new_id}.json")
                created_manifest = not destination.exists()
                self._write_manifest(destination, new_manifest)
                if before_hashes != self.store.verify_originals(version_id):
                    raise StoreError(ErrorCode.ORIGINAL_CHANGED, "移除前后原图校验不一致，调整未保存")
                result = RevisionService(self.store).open_project(source["project_id"], new_id)
                result.update(previous_version_id=version_id, removed_count=len(removed_units),
                    removed_file_count=len(visible_originals - kept_originals), removed_unreadable_count=len(removed_diagnostics),
                    review_required=True)
            return result
        except BaseException:
            if created_manifest and destination is not None:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    # Preserve the primary storage error; a rolled-back version can never activate this orphan.
                    pass
            raise
