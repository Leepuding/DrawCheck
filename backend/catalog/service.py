"""由识别结果生成目录；改名并持久化「人工修正」。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.extract.service import ExtractResult, ExtractService
from backend.extract.prompt import DEFAULT_CONFIDENCE_THRESHOLD
from backend.store import Repository, BBox, Evidence, Source, StoreError
from backend.title_identity import MANUAL_NUMBER_FIELD, has_manual_number

from .coverage import recover_catalog_header
from .errors import CatalogError
from .title_resolver import resolve_title_number

logger = logging.getLogger("drawcheck.catalog")

MANUAL_CORRECTION_LABEL = "人工修正"
MANUAL_CORRECTION_FIELD = "manual_correction"  # bool
MANUAL_CORRECTION_LABEL_FIELD = "manual_correction_label"  # 展示文案


class CatalogService:
    """版本级目录：specialty / drawing_number / name；支持改名。"""

    def __init__(self, store: Repository):
        self.store = store
        self._header_engine = None
        self.state_path = Path(store.root) / "dc09_catalog_state.json"

    # ---------- 查询 ----------

    def list_catalog(self, version_id: str) -> list[dict[str, Any]]:
        state = self._load_state()
        rows = list((state.get("versions") or {}).get(version_id) or [])
        # 与单元上的人工修正/目录字段对齐（单元改名后刷新）
        refreshed = []
        for row in rows:
            unit_id = row.get("unit_id")
            if unit_id:
                try:
                    unit = self.store.get("drawing_units", unit_id)
                except Exception:
                    unit = None
                if unit is not None:
                    meta = unit.get("metadata") or {}
                    if meta.get(MANUAL_CORRECTION_FIELD) or row.get(MANUAL_CORRECTION_FIELD):
                        row = dict(row)
                        row[MANUAL_CORRECTION_FIELD] = True
                        row[MANUAL_CORRECTION_LABEL_FIELD] = MANUAL_CORRECTION_LABEL
                        # 若该行是单元主行（frame_index==0 或唯一），用单元上的图号图名
                        if row.get("is_primary", True):
                            if has_manual_number(unit):
                                row["drawing_number"] = unit.get("drawing_number") or ""
                            row["name"] = unit.get("name") or ""
            refreshed.append(self._public_row(row))
        return refreshed

    def get_unit_directory(self, unit_id: str) -> dict[str, Any]:
        unit = self.store.get("drawing_units", unit_id)
        meta = unit.get("metadata") or {}
        return {
            "unit_id": unit_id,
            "version_id": unit["version_id"],
            "specialty": meta.get("specialty") or "",
            "drawing_number": unit.get("drawing_number") or "",
            "name": unit.get("name") or "",
            MANUAL_CORRECTION_FIELD: bool(meta.get(MANUAL_CORRECTION_FIELD)),
            **({MANUAL_NUMBER_FIELD:meta[MANUAL_NUMBER_FIELD]} if MANUAL_NUMBER_FIELD in meta else {}),
            MANUAL_CORRECTION_LABEL_FIELD: (
                MANUAL_CORRECTION_LABEL if meta.get(MANUAL_CORRECTION_FIELD) else None
            ),
            "title_identity_uncertain":bool(meta.get("title_identity_uncertain")),
            "extract_status": meta.get("extract_status"),
            "extract_incomplete_reason": meta.get("extract_incomplete_reason"),
            "vision_title_drawing_number": meta.get("vision_title_drawing_number"),
            "title_number_resolution": meta.get("title_number_resolution"),
        }

    # ---------- 从识别结果生成 ----------

    def apply_extract_result(
        self, result: ExtractResult | dict[str, Any], *, local_reading: dict | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(result, ExtractResult):
            data = result.as_dict()
        else:
            data = result
        unit_id = data["unit_id"]
        version_id = data["version_id"]
        unit = self.store.get("drawing_units", unit_id)
        title_blocks = list(data.get("title_blocks") or [])
        manual_title = has_manual_number(unit)
        local_title = None
        if local_reading is not None:
            local_reading = self._recover_catalog_header(unit, local_reading)
            self._write_local_reading(unit, local_reading)
            unit = self.store.get("drawing_units", unit_id)
            local_title = local_reading.get("title") or (local_reading.get("catalog") or {}).get("header_volume")
        if local_title and not title_blocks:
            value = local_title["value"]
            title_blocks.append({"drawing_number": "", "name": unit["name"],
                "bbox": value["bbox"], "out_of_scope": (unit.get("metadata") or {}).get("out_of_scope", False)})
        cad_facts = [dict(fact) for fact in self.store._connection.execute(
            "SELECT id,unit_id,source,text,x0,y0,x1,y1 FROM facts WHERE version_id=? AND unit_id=? AND source=?",
            (version_id,unit_id,Source.CAD_TEXT.value))] if unit.get("kind") == "dwg_frame" else []

        if not title_blocks:
            native_identity = resolve_title_number(unit, {}, cad_facts)
            if native_identity.get("identity_kind") == "catalog_volume":
                title_blocks.append({"drawing_number": "", "name": native_identity["heading_evidence"]["text"],
                                     "bbox": native_identity["selected_evidence"]["bbox"]})
        rows: list[dict[str, Any]] = []
        if not title_blocks:
            resolution = resolve_title_number(unit, {}, cad_facts)
            # 模型返回空 / 无图签：保留单元，写入明确占位，不得静默丢图
            placeholder_name = unit.get("name") or "（无图签）"
            if "图框待确认" in str(unit.get("name") or ""):
                placeholder_name = unit["name"]  # 保留 DC-07 语义
            row = self._make_row(
                version_id=version_id,
                unit_id=unit_id,
                specialty="",
                drawing_number=unit.get("drawing_number") or "",
                name=placeholder_name,
                is_primary=True,
                no_title_block=True,
                source="empty_model",
                title_number_resolution=resolution,
                vision_title_drawing_number="",
            )
            row["title_identity_uncertain"] = not (manual_title and (unit.get("drawing_number") or "").strip())
            rows.append(row)
            self._write_unit_directory(unit, specialty="", drawing_number=row["drawing_number"],
                                       name=row["name"], extra={"no_title_block": True, "title_identity_uncertain":row["title_identity_uncertain"],
                                           "vision_title_drawing_number":"", "title_number_resolution":resolution})
        else:
            for index, block in enumerate(title_blocks):
                local = local_title if index == 0 else None
                resolution = resolve_title_number(unit, block, cad_facts)
                if local:
                    boxes = [local[field]["bbox"] for field in ("value", "label")]
                    local_box = {key: reduce(edge[key] for edge in boxes) for key, reduce in
                                 (("x0", min), ("y0", min), ("x1", max), ("y1", max))}
                    block = {**block, "bbox": local_box, "bbox_clamped": False, "no_title_block": False,
                             "name": (local.get("name") or {}).get("text") or block.get("name")}
                    if resolution["status"] == "cad_selected":
                        block["bbox"] = title_blocks[index].get("bbox")
                    else:
                        resolution = resolve_title_number(unit, block, cad_facts)
                    title_blocks[index] = block
                if local and not manual_title and resolution["status"] != "cad_selected":
                    resolution.update(status="local_selected", selected_source=local["value"]["source"],
                        selected_drawing_number=local["number"], selected_evidence=local["value"],
                        label_evidence=local["label"], requires_confirmation=False,
                        differs_from_vision=local["number"] != resolution["vision_drawing_number"],
                        reason="在图号标签旁定位的本地原文")
                    if local.get("identity_kind") == "catalog_volume":
                        resolution.update(identity_kind="catalog_volume", heading_evidence=local["heading"],
                            reason="在目录卷册检索标签旁定位的本地原文；卷册身份与单张图号分别记录")
                row = self._make_row(
                    version_id=version_id,
                    unit_id=unit_id,
                    specialty=str(block.get("specialty") or ""),
                    drawing_number=resolution["selected_drawing_number"],
                    name=str(block.get("name") or "") or ("（无图签）" if block.get("no_title_block") else ""),
                    is_primary=(index == 0),
                    no_title_block=bool(block.get("no_title_block")),
                    multi_frame=bool(block.get("multi_frame")) or len(title_blocks) > 1,
                    out_of_scope=bool(block.get("out_of_scope")),
                    unlisted_in_catalog_sheet=bool(block.get("unlisted_in_catalog_sheet")),
                    file=block.get("file"),
                    page=block.get("page"),
                    page_size=block.get("page_size"),
                    frame_index=index,
                    source="extract",
                    title_number_resolution=resolution,
                    vision_title_drawing_number=resolution["vision_drawing_number"],
                )
                rows.append(row)
                if resolution["requires_confirmation"] and not manual_title:
                    self._pending_title_source(version_id, unit_id, block, resolution)
            primary = rows[0]
            block = title_blocks[0]
            try:
                confidence = float(block["confidence"]) if block.get("confidence") is not None else 1.0
            except (TypeError, ValueError):
                confidence = 0.0
            uncertain = (not primary["drawing_number"].strip() or bool(block.get("unclear"))
                or bool(block.get("no_title_block")) or not DEFAULT_CONFIDENCE_THRESHOLD <= confidence <= 1.0)
            resolution = primary["title_number_resolution"]
            if resolution["status"] in {"cad_selected", "local_selected"}:
                uncertain = False
            if resolution.get("identity_kind") == "catalog_volume":
                # Replace a broad/model area with the exact native header field.
                edges = [resolution[field]["bbox"] for field in ("selected_evidence", "label_evidence")]
                block = {**block, "bbox": {key: reduce(edge[key] for edge in edges) for key, reduce in
                                 (("x0", min), ("y0", min), ("x1", max), ("y1", max))}}
                primary["identity_kind"] = "catalog_volume"
                if resolution.get("heading_evidence"):
                    primary["name"] = resolution["heading_evidence"]["text"]
            elif resolution["requires_confirmation"]:
                uncertain = True
            identity_uncertain = not bool((unit.get("drawing_number") or "").strip()) if manual_title else uncertain
            primary["title_identity_uncertain"] = identity_uncertain
            if manual_title:
                primary["drawing_number"], primary["name"] = unit["drawing_number"], unit["name"]
                primary["no_title_block"] = False
                primary[MANUAL_CORRECTION_FIELD] = True
                primary[MANUAL_CORRECTION_LABEL_FIELD] = MANUAL_CORRECTION_LABEL
            self._write_unit_directory(
                unit,
                specialty=primary["specialty"],
                drawing_number=primary["drawing_number"],
                name=primary["name"],
                extra={
                    "no_title_block": primary.get("no_title_block"),
                    "multi_frame": primary.get("multi_frame"),
                    "catalog_frame_count": len(rows),
                    "title_identity_uncertain":identity_uncertain,
                    "title_bbox":block.get("bbox"),
                    "vision_title_drawing_number":resolution["vision_drawing_number"],
                    "title_number_resolution":resolution,
                    **({"identity_kind": "catalog_volume", "is_catalog_sheet": True,
                        "catalog_volume_number": resolution["selected_drawing_number"]}
                       if resolution.get("identity_kind") == "catalog_volume" else {}),
                },
            )

        # The primary row mirrors the unit's field-level authority, including a
        # False marker that must defeat older unqualified row-level manual flags.
        unit_meta = unit.get("metadata") or {}
        if MANUAL_NUMBER_FIELD in unit_meta:
            rows[0][MANUAL_NUMBER_FIELD] = unit_meta[MANUAL_NUMBER_FIELD]
        elif unit_meta.get(MANUAL_CORRECTION_FIELD) and not manual_title:
            rows[0][MANUAL_NUMBER_FIELD] = False
        if unit_meta.get(MANUAL_CORRECTION_FIELD):
            rows[0][MANUAL_CORRECTION_FIELD] = True
            rows[0]["name"] = unit["name"]
        rows = self._upsert_version_rows(version_id, unit_id, rows)
        return [self._public_row(r) for r in rows]

    def _recover_catalog_header(self, unit, reading):
        def get_engine():
            if self._header_engine is None:
                from backend.extract.local_sheet import _engine
                self._header_engine = _engine()
            return self._header_engine

        return recover_catalog_header(self.store, unit, reading, get_engine=get_engine)

    def refresh_catalog_evidence(self, version_id):
        """Retry the local header on saved rows when the recognition step is reused."""
        for unit in self.store.list_records("drawing_units", version_id=version_id):
            meta = unit.get("metadata") or {}
            reading = meta.get("local_sheet")
            if not reading or meta.get("out_of_scope") or meta.get("extract_status") == "incomplete":
                continue
            recovered = self._recover_catalog_header(unit, reading)
            if recovered is not reading:
                from backend.references import sheet_number

                volume = recovered["catalog"].get("header_volume")
                if (volume and not reading.get("title") and not has_manual_number(unit)
                        and not sheet_number(unit.get("drawing_number"))):
                    # Promote only an unstructured catalogue placeholder. Use
                    # the normal title path and the explicit labelled header.
                    self.apply_extract_result({"unit_id":unit["id"], "version_id":version_id}, local_reading=recovered)
                else:
                    self._write_local_reading(unit, recovered)

    def _write_local_reading(self, unit, reading):
        meta = dict(unit.get("metadata") or {})
        meta["local_sheet"] = reading
        catalog = reading.get("catalog")
        if catalog and catalog["rows"]:
            rows = catalog["rows"]
            numbers = [row["number"] for row in rows]
            meta.update(is_catalog_sheet=True, catalog_listed_numbers=numbers,
                catalog_entries=[{"drawing_number": row["number"], "bbox": row["value"]["bbox"],
                                  "source": row["value"]["source"]} for row in rows],
                catalog_bbox=catalog["header"]["bbox"],
                catalog_complete=bool(catalog["complete"]),
                catalog_physical_row_count=catalog.get("physical_row_count", len(rows)),
                catalog_duplicate_numbers=sorted({number for number in numbers if numbers.count(number) > 1}))
            if volume := catalog.get("header_volume"):
                meta["catalog_volume_number"] = volume["number"]
                meta["catalog_volume_evidence"] = volume
                # The locally read number column and literal labelled header
                # supersede a model's split "01卷"/"03册" catalogue codes.
                meta["catalog_volume_numbers"] = [volume["number"]]
                meta.pop("catalog_volume_numbers_uncertain", None)
            if not meta["catalog_complete"]:
                meta["catalog_incomplete_reason"] = "本地未证实目录行全部读齐"
            else:
                meta.pop("catalog_incomplete_reason", None)
        self.store._update("drawing_units", unit["id"], {"metadata_json": json.dumps(meta, ensure_ascii=False)})

    def build_from_version(
        self,
        version_id: str,
        *,
        extract: ExtractService | None = None,
        persist_extract: bool = True,
    ) -> list[dict[str, Any]]:
        """对版本内全部单元识别并生成目录。"""
        svc = extract or ExtractService(self.store)
        results = svc.extract_version(version_id, persist=persist_extract)
        all_rows: list[dict[str, Any]] = []
        for result in results:
            all_rows.extend(self.apply_extract_result(result))
        return self.list_catalog(version_id)

    # ---------- 改名 ----------

    def rename(
        self,
        *,
        unit_id: str | None = None,
        row_id: str | None = None,
        version_id: str | None = None,
        drawing_number: str | None = None,
        name: str | None = None,
        specialty: str | None = None,
    ) -> dict[str, Any]:
        """更新图号和/或图名；保存后带「人工修正」。"""
        if drawing_number is None and name is None and specialty is None:
            raise CatalogError("INVALID_RENAME", "须提供图号、图名或专业之一")

        state = self._load_state()
        target_row = None
        target_version = version_id

        if row_id:
            for vid, rows in (state.get("versions") or {}).items():
                for row in rows:
                    if row.get("row_id") == row_id:
                        target_row = row
                        target_version = vid
                        break
                if target_row:
                    break
            if target_row is None:
                raise CatalogError("ROW_NOT_FOUND", "目录行不存在")
            unit_id = target_row.get("unit_id") or unit_id
        elif unit_id:
            unit = self.store.get("drawing_units", unit_id)
            target_version = unit["version_id"]
            rows = (state.get("versions") or {}).get(target_version) or []
            for row in rows:
                if row.get("unit_id") == unit_id and row.get("is_primary", True):
                    target_row = row
                    break
            if target_row is None:
                # 尚无目录行：以单元现值为底新建
                target_row = self._make_row(
                    version_id=target_version,
                    unit_id=unit_id,
                    specialty=(self.store.get("drawing_units", unit_id).get("metadata") or {}).get("specialty") or "",
                    drawing_number=unit.get("drawing_number") or "",
                    name=unit.get("name") or "",
                    is_primary=True,
                    source="rename",
                    **({MANUAL_NUMBER_FIELD:unit["metadata"][MANUAL_NUMBER_FIELD]} if MANUAL_NUMBER_FIELD in (unit.get("metadata") or {}) else {}),
                )
                self._upsert_version_rows(target_version, unit_id, [target_row])
                state = self._load_state()
                for row in (state.get("versions") or {}).get(target_version) or []:
                    if row.get("unit_id") == unit_id and row.get("is_primary", True):
                        target_row = row
                        break
        else:
            raise CatalogError("INVALID_RENAME", "须提供 unit_id 或 row_id")

        assert target_row is not None and target_version is not None
        prior_number_authority = has_manual_number(target_row)
        if drawing_number is not None:
            target_row[MANUAL_NUMBER_FIELD] = True
        elif MANUAL_NUMBER_FIELD not in target_row and not prior_number_authority:
            target_row[MANUAL_NUMBER_FIELD] = False
        if drawing_number is not None:
            target_row["drawing_number"] = drawing_number
            target_row["title_identity_uncertain"] = not bool(drawing_number.strip())
            if drawing_number.strip():
                target_row["no_title_block"] = False
        if name is not None:
            target_row["name"] = name
        if specialty is not None:
            target_row["specialty"] = specialty
        target_row[MANUAL_CORRECTION_FIELD] = True
        target_row[MANUAL_CORRECTION_LABEL_FIELD] = MANUAL_CORRECTION_LABEL
        if drawing_number is not None:
            previous = target_row.get("title_number_resolution") or {}
            target_row["title_number_resolution"] = resolve_title_number(
                {"drawing_number":target_row["drawing_number"], "metadata":target_row},
                {"drawing_number":target_row.get("vision_title_drawing_number", previous.get("vision_drawing_number", ""))}, [])

        # 写回 state
        rows = (state.setdefault("versions", {})).setdefault(target_version, [])
        for i, row in enumerate(rows):
            if row.get("row_id") == target_row["row_id"]:
                rows[i] = target_row
                break
        else:
            rows.append(target_row)
        self._save_state(state)

        # 主行同步到 drawing_units + metadata（store 持久化）
        if unit_id and target_row.get("is_primary", True):
            unit = self.store.get("drawing_units", unit_id)
            # An uncertain catalogue row can display a raw visual reading while
            # the unit still has a placeholder. A name edit must not promote it.
            new_number = target_row["drawing_number"] if drawing_number is not None else unit["drawing_number"]
            new_name = target_row["name"]
            self.store.update_unit_directory(unit_id, name=new_name, drawing_number=new_number)
            meta = dict(unit.get("metadata") or {})
            prior_number_authority = has_manual_number(unit)
            if drawing_number is not None:
                meta[MANUAL_NUMBER_FIELD] = True
            elif MANUAL_NUMBER_FIELD not in meta and not prior_number_authority:
                meta[MANUAL_NUMBER_FIELD] = False
            meta[MANUAL_CORRECTION_FIELD] = True
            meta[MANUAL_CORRECTION_LABEL_FIELD] = MANUAL_CORRECTION_LABEL
            if drawing_number is not None:
                meta.update(title_identity_uncertain=not bool(new_number.strip()), title_drawing_number=new_number)
                if new_number.strip():
                    meta["no_title_block"] = False
            # A rename is a user decision. Keep the earlier CAD anchor as audit
            # context, but never continue labelling the renamed value CAD text.
            if drawing_number is not None:
                resolved_unit = {**unit, "drawing_number":new_number, "metadata":meta}
                previous_resolution = meta.get("title_number_resolution") or {}
                meta["title_number_resolution"] = resolve_title_number(resolved_unit,
                    {"drawing_number":meta.get("vision_title_drawing_number", previous_resolution.get("vision_drawing_number", ""))}, [])
            if specialty is not None or target_row.get("specialty") is not None:
                meta["specialty"] = target_row.get("specialty") or meta.get("specialty") or ""
            self.store._update(
                "drawing_units",
                unit_id,
                {"metadata_json": json.dumps(meta, ensure_ascii=False)},
            )

        return self._public_row(target_row)

    # ---------- 内部 ----------

    def _make_row(self, **kwargs) -> dict[str, Any]:
        row = {
            "row_id": uuid4().hex,
            MANUAL_CORRECTION_FIELD: False,
            MANUAL_CORRECTION_LABEL_FIELD: None,
            "no_title_block": False,
            "multi_frame": False,
            "out_of_scope": False,
            "unlisted_in_catalog_sheet": False,
            "frame_index": 0,
            "file": None,
            "page": None,
            "page_size": None,
            "source": "extract",
        }
        row.update(kwargs)
        return row

    def _public_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "row_id": row.get("row_id"),
            "unit_id": row.get("unit_id"),
            "version_id": row.get("version_id"),
            "specialty": row.get("specialty") or "",
            "drawing_number": row.get("drawing_number") or "",
            "name": row.get("name") or "",
            MANUAL_CORRECTION_FIELD: bool(row.get(MANUAL_CORRECTION_FIELD)),
            **({MANUAL_NUMBER_FIELD:row[MANUAL_NUMBER_FIELD]} if MANUAL_NUMBER_FIELD in row else {}),
            MANUAL_CORRECTION_LABEL_FIELD: (
                MANUAL_CORRECTION_LABEL if row.get(MANUAL_CORRECTION_FIELD) else None
            ),
            "title_identity_uncertain":bool(row.get("title_identity_uncertain")),
            **({"identity_kind": row["identity_kind"]} if row.get("identity_kind") else {}),
            "no_title_block": bool(row.get("no_title_block")),
            "multi_frame": bool(row.get("multi_frame")),
            "out_of_scope": bool(row.get("out_of_scope")),
            "unlisted_in_catalog_sheet": bool(row.get("unlisted_in_catalog_sheet")),
            "file": row.get("file"),
            "page": row.get("page"),
            "page_size": row.get("page_size"),
            "frame_index": row.get("frame_index", 0),
            "is_primary": bool(row.get("is_primary", True)),
            "vision_title_drawing_number": row.get("vision_title_drawing_number"),
            "title_number_resolution": row.get("title_number_resolution"),
        }

    def _pending_title_source(self, version_id, unit_id, block, resolution):
        """Retain an explicit human check without fabricating a CAD value/bbox."""
        candidates = resolution["candidate_evidence"]
        if candidates:
            evidence = candidates[0]
            source, text, box = Source.CAD_TEXT, evidence["text"], evidence["bbox"]
        else:
            if block.get("bbox_clamped"):
                return
            source, text, box = Source.VISION, str(block.get("drawing_number") or "图签"), block.get("bbox")
        try:
            evidence = Evidence(unit_id, source, text, BBox(**box))
        except (TypeError, ValueError, StoreError) as exc:
            # A missing/invalid position remains metadata-level uncertainty.
            # Do not create a pretend precise anchor for the confirmation UI.
            logger.debug("图签来源确认缺少有效位置：%s", type(exc).__name__)
            return
        reason = "图签原文来源或候选位置需确认"
        columns = evidence.columns()
        for pending in self.store.list_records("pending_confirmations", version_id=version_id):
            if pending["reason"] == reason and all(pending.get(key) == value for key, value in columns.items()):
                return
        self.store.create_pending(version_id, evidence, reason)

    def _write_unit_directory(
        self,
        unit: dict,
        *,
        specialty: str,
        drawing_number: str,
        name: str,
        extra: dict | None = None,
    ) -> None:
        # 不覆盖已有人工修正的图号图名
        meta = dict(unit.get("metadata") or {})
        if meta.get(MANUAL_CORRECTION_FIELD):
            meta["specialty"] = meta.get("specialty") or specialty
            number_authority = has_manual_number(unit)
            if not number_authority and MANUAL_NUMBER_FIELD not in meta:
                meta[MANUAL_NUMBER_FIELD] = False
            if not number_authority and not (extra or {}).get("title_identity_uncertain"):
                self.store.update_unit_directory(unit["id"], name=unit["name"], drawing_number=drawing_number)
                if drawing_number:
                    meta["title_drawing_number"] = drawing_number
            if extra:
                meta.update({k: v for k, v in extra.items() if v is not None})
            if number_authority:
                number = unit.get("drawing_number") or ""
                meta.update(title_identity_uncertain=not bool(number.strip()), title_drawing_number=number)
                if number.strip():
                    meta["no_title_block"] = False
            self.store._update(
                "drawing_units",
                unit["id"],
                {"metadata_json": json.dumps(meta, ensure_ascii=False)},
            )
            return
        if not (extra or {}).get("title_identity_uncertain"):
            self.store.update_unit_directory(unit["id"], name=name, drawing_number=drawing_number)
            if drawing_number:
                meta["title_drawing_number"] = drawing_number
        meta["specialty"] = specialty
        if extra:
            meta.update({k: v for k, v in extra.items() if v is not None})
        self.store._update(
            "drawing_units",
            unit["id"],
            {"metadata_json": json.dumps(meta, ensure_ascii=False)},
        )

    def _upsert_version_rows(self, version_id: str, unit_id: str, rows: list[dict]) -> list[dict]:
        state = self._load_state()
        existing = list((state.setdefault("versions", {})).get(version_id) or [])
        kept: list[dict] = []
        manual_by_frame: dict[int, dict] = {}
        for row in existing:
            if row.get("unit_id") != unit_id:
                kept.append(row)
            elif row.get(MANUAL_CORRECTION_FIELD):
                manual_by_frame[int(row.get("frame_index", 0))] = row
        merged: list[dict] = []
        for row in rows:
            frame = int(row.get("frame_index", 0))
            prev = manual_by_frame.pop(frame, None)
            if prev is not None:
                row = dict(row)
                number_authority = has_manual_number(prev)
                if MANUAL_NUMBER_FIELD not in row and number_authority:
                    row["drawing_number"] = prev.get("drawing_number", row["drawing_number"])
                    row["title_number_resolution"] = resolve_title_number(
                        {"drawing_number":row["drawing_number"], "metadata":prev},
                        {"drawing_number":row.get("vision_title_drawing_number", "")}, [])
                if MANUAL_NUMBER_FIELD not in row and MANUAL_NUMBER_FIELD in prev:
                    row[MANUAL_NUMBER_FIELD] = prev[MANUAL_NUMBER_FIELD]
                elif MANUAL_NUMBER_FIELD not in row and not number_authority:
                    row[MANUAL_NUMBER_FIELD] = False
                row["name"] = prev.get("name", row["name"])
                if prev.get("specialty"):
                    row["specialty"] = prev["specialty"]
                row[MANUAL_CORRECTION_FIELD] = True
                row[MANUAL_CORRECTION_LABEL_FIELD] = MANUAL_CORRECTION_LABEL
                row["row_id"] = prev.get("row_id") or row["row_id"]
            merged.append(row)
        merged.extend(manual_by_frame.values())
        state["versions"][version_id] = kept + merged
        self._save_state(state)
        return merged

    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {"versions": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("catalog state 损坏，重建空状态")
            return {"versions": {}}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
